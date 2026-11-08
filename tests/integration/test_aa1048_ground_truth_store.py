"""AA-1048 (Quality W2) — services/ground_truth/store.py on real Postgres (conftest replays every migration, CIS 388
included): the panel import (DRY RUN, apply, refused twice per version), the read back, the golden_claim queue items
(DRY RUN, apply, never twice), and the resolved items turned into tech-lead rows, as cis_app.

Invented values only; every row has set id ZZ-AA1048-S and is deleted at the end."""
import os
import sys
from datetime import date

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.ground_truth import panel as pn  # noqa: E402
from services.ground_truth import store  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SET = "ZZ-AA1048-S"
TENANT = "00000000-0000-4000-8000-000000a10483"
OUTPUT = {"content_text": "Arrive in Velmora and rest at the Harbour Lodge. Walk 40 km to Ostrin Valley on day "
                          "two. Breakfast is served at the lodge each morning. The valley is quiet in spring."}
SOURCE = "Day 1: Arrive in Velmora. Harbour Lodge. Dinner included.\nDay 2: Walk 12 km to Ostrin Valley."


def _rows(version="1.0.0"):
    lines = pn.export_records(set_id=SET, writer="t9_blog", scope="tenant", tenant_id=TENANT,
                              refs={"sample_id": "zz-901", "piece_id": "00000000-0000-4000-8000-0000000a1048"},
                              output=OUTPUT, source_text=SOURCE)
    raw = []
    for i, e in enumerate(lines):
        labels = ["violation", "violation", "ok"] if i == 1 else ["ok", "ok", "ok"]
        for lens, lab in zip(pn.LENSES, labels):
            raw.append({"claim_hash": e["claim_hash"], "lens": lens, "class": "fact.number", "label": lab,
                        "reason": f"{lens}: invented reason", "labeller": "panel:zz"})
    js = pn.parse_judgements(raw, export_by_hash={e["claim_hash"]: e for e in lines})
    return lines, pn.consolidate(lines, js, set_id=SET, set_version=version, policy_sha="c" * 64)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)

    async def clean():
        await c.execute("DELETE FROM shared.writing_memory_disagreement WHERE source = 'golden_claim' AND "
                        "source_ref IN (SELECT label_id FROM shared.eval_claim_label WHERE set_id = $1)", SET)
        await c.execute("DELETE FROM shared.eval_claim_label WHERE set_id = $1", SET)
    await clean()
    yield c
    await clean()
    await c.close()


async def _as_cis_app(c):
    """A transaction as cis_app, rolled back by the test. The CI replay leaves cis_app without USAGE on shared (CIS 244
    is skipped there), so the grants it has on Dev are given inside the same transaction and vanish with it."""
    tr = c.transaction()
    await tr.start()
    await c.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    await c.execute("GRANT SELECT, INSERT, UPDATE ON shared.writing_memory_disagreement TO cis_app")
    await c.execute("GRANT SELECT, INSERT, DELETE ON shared.eval_claim_label TO cis_app")
    await c.execute("SET LOCAL ROLE cis_app")
    return tr


@pytest.mark.asyncio
async def test_import_read_enqueue_and_pull(conn):
    _lines, cons = _rows()
    tr = await _as_cis_app(conn)
    try:
        dry = await store.insert_rows(conn, cons.rows, apply=False)
        assert dry["dry_run"] and dry["inserted"] == 0 and dry["panel"] == len(cons.rows)
        assert await store.panel_rows_exist(conn, SET, "1.0.0") == 0
        done = await store.insert_rows(conn, cons.rows, apply=True)
        assert done["inserted"] == len(cons.rows) and done["skipped"] == 0
        with pytest.raises(store.StoreRefused, match="already has panel rows"):
            await store.insert_rows(conn, cons.rows, apply=True)
        rows = await store.read_rows(conn, SET, "1.0.0")
        assert len(rows) == len(cons.rows) and rows[0]["tenant_id"] == TENANT
        assert {r["adjudication"] for r in rows} >= {"disputed", "audit"}
        finals = pn.final_labels(rows)
        assert pn.contradictions(finals) == []

        expected = len(store.queue_candidates(rows))
        assert expected == sum(1 for s in cons.states.values() if s in ("disputed", "audit"))
        q = await store.enqueue(conn, rows, today=date(2026, 11, 7), apply=False)
        assert q == {"candidates": expected, "added": expected, "week_start": "2026-11-02", "dry_run": True}
        q = await store.enqueue(conn, rows, today=date(2026, 11, 7), apply=True)
        assert q["added"] == expected
        q = await store.enqueue(conn, rows, today=date(2026, 11, 7), apply=True)
        assert q["added"] == 0                                                    # never twice
        items = await conn.fetch("SELECT d.item_id, d.machine_verdict, d.human_label FROM "
                                 "shared.writing_memory_disagreement d JOIN shared.eval_claim_label l ON "
                                 "l.label_id = d.source_ref WHERE d.source = 'golden_claim' AND l.set_id = $1 "
                                 "ORDER BY d.human_label, d.item_id", SET)
        disputed = [i for i in items if i["human_label"] == "disputed"]
        assert len(disputed) == 1 and disputed[0]["machine_verdict"] == "violation"
        # the tech lead resolves the disputed claim: the panel's majority (violation) stands
        await conn.execute("UPDATE shared.writing_memory_disagreement SET status = 'resolved', resolution = "
                           "'judge_right', resolved_by = 'tech_lead', resolved_at = now() WHERE item_id = $1",
                           disputed[0]["item_id"])
        dry = await store.pull_resolutions(conn, SET, "1.0.0", apply=False)
        assert dry["resolved_items"] == 1 and dry["tech_lead"] == 1 and dry["inserted"] == 0
        done = await store.pull_resolutions(conn, SET, "1.0.0", apply=True)
        assert done["inserted"] == 1
        again = await store.pull_resolutions(conn, SET, "1.0.0", apply=True)
        assert again["rows"] == 0                                                   # the claim has its lead row
        finals = pn.final_labels(await store.read_rows(conn, SET, "1.0.0"))
        lead = [f for f in finals.values() if f.source == "tech_lead"]
        assert len(lead) == 1 and lead[0].label == "violation"
        report = pn.agreement_report(await store.read_rows(conn, SET, "1.0.0"))
        assert report["classes"]["fact.number"]["adjudicated"] == 1
    finally:
        await tr.rollback()


@pytest.mark.asyncio
async def test_enqueue_in_batches_tops_up_after_resolutions(conn):
    """S273 "t9-heldout-first": max_open caps the open items of the set; a resolved item frees room for the next
    candidate in work order; the split filter keeps the other side out."""
    _lines, cons = _rows()
    tr = await _as_cis_app(conn)
    try:
        await store.insert_rows(conn, cons.rows, apply=True)
        rows = await store.read_rows(conn, SET, "1.0.0")
        side = rows[0]["split"]
        other = "train" if side == "held_out" else "held_out"
        order = [it["source_ref"] for it in store.queue_candidates(rows, split=side)]
        assert order, "the fixture must give at least one disputed or audit claim"
        assert (await store.enqueue(conn, rows, today=date(2026, 11, 7), apply=True, split=other))["added"] == 0
        first = await store.enqueue(conn, rows, today=date(2026, 11, 7), apply=True, split=side, max_open=1)
        assert first["added"] == 1 and first["open_after"] == 1 and first["remaining"] == len(order) - 1
        again = await store.enqueue(conn, rows, today=date(2026, 11, 7), apply=True, split=side, max_open=1)
        assert again["added"] == 0 and again["already_queued"] == 1
        queued = await conn.fetchval("SELECT source_ref::text AS ref FROM shared.writing_memory_disagreement "
                                     "WHERE source = 'golden_claim' AND source_ref = $1::uuid", order[0])
        assert queued == order[0]                                                   # the first in work order
        await conn.execute("UPDATE shared.writing_memory_disagreement SET status = 'resolved', resolution = "
                           "'unclear', resolved_by = 'tech_lead', resolved_at = now() WHERE source_ref = $1::uuid",
                           order[0])
        top = await store.enqueue(conn, rows, today=date(2026, 11, 7), apply=True, split=side, max_open=1)
        assert top["open_before"] == 0 and top["added"] == min(1, len(order) - 1)
    finally:
        await tr.rollback()


def test_resolution_label():
    assert store.resolution_label("violation", "judge_right") == "violation"
    assert store.resolution_label("violation", "human_right") == "ok"
    assert store.resolution_label("ok", "human_right") == "violation"
    assert store.resolution_label("split", "judge_right") == "unsure"
    assert store.resolution_label("ok", "unclear") == "unsure"
    assert store.week_start(date(2026, 11, 8)) == date(2026, 11, 2)
