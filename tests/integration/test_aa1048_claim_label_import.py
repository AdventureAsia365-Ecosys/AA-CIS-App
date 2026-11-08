"""AA-1048 W2 (S273) — the API import path (services/ground_truth/importer.py) on real Postgres as cis_app: DRY RUN,
apply with its audit_log row, the idempotent second run, the refused relabel, a tech-lead row added later, and the
split-plan candidate queries (services/ground_truth/split_plan.py) against the replayed schema.

Invented values only; every row has set id ZZ-AA1048-API and is deleted at the end."""
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.ground_truth import importer  # noqa: E402
from services.ground_truth import panel as pn  # noqa: E402
from services.ground_truth import split_plan as sp  # noqa: E402
from services.ground_truth import store  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SET = "ZZ-AA1048-API"
TENANT = "00000000-0000-4000-8000-000000a10485"
OUTPUT = {"content_text": "Arrive in Velmora and rest at the Harbour Lodge. Walk 40 km to Ostrin Valley on day "
                          "two. Breakfast is served at the lodge each morning. The valley is quiet in spring."}
SOURCE = "Day 1: Arrive in Velmora. Harbour Lodge. Dinner included.\nDay 2: Walk 12 km to Ostrin Valley."


def _payload(dry_run=True, flip=False):
    lines = pn.export_records(set_id=SET, writer="t9_blog", scope="tenant", tenant_id=TENANT,
                              refs={"sample_id": "zz-921", "piece_id": "00000000-0000-4000-8000-0000000a1050",
                                    "tour_id": "00000000-0000-4000-8000-0000000a1051"},
                              output=OUTPUT, source_text=SOURCE)
    js = []
    for i, e in enumerate(lines):
        labels = ["violation", "violation", "ok"] if i == 1 else ["ok", "ok", "ok"]
        if flip and i == 0:
            labels = ["violation", "ok", "ok"]
        js += [{"claim_hash": e["claim_hash"], "lens": lens, "class": "fact.number", "label": lab,
                "reason": f"{lens}: invented reason", "labeller": "panel:zz"} for lens, lab in zip(pn.LENSES, labels)]
    return importer.build_payload(lines, js, set_id=SET, set_version="1.0.0", dry_run=dry_run)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)

    async def clean():
        await c.execute("DELETE FROM shared.eval_claim_label WHERE set_id = $1", SET)
    await clean()
    yield c
    await clean()
    await c.close()


async def _as_cis_app(c):
    """A transaction as cis_app, rolled back by the test (the CI replay leaves cis_app without the Dev grants)."""
    tr = c.transaction()
    await tr.start()
    await c.execute("GRANT USAGE ON SCHEMA shared, acp_shared TO cis_app")
    await c.execute("GRANT SELECT, INSERT, DELETE ON shared.eval_claim_label TO cis_app")
    await c.execute("GRANT SELECT, INSERT ON acp_shared.audit_log TO cis_app")
    await c.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA acp_shared TO cis_app")
    await c.execute("SET LOCAL ROLE cis_app")
    return tr


@pytest.mark.asyncio
async def test_api_import_dry_run_apply_idempotent_and_refused(conn):
    tr = await _as_cis_app(conn)
    try:
        dry = await importer.import_labels(conn, _payload(), actor="admin:unknown")
        assert dry["dry_run"] and dry["inserted"] == 0 and dry["new"] == dry["rows"]
        assert await store.existing_judgements(conn, SET, "1.0.0") == {}

        done = await importer.import_labels(conn, _payload(dry_run=False), actor="admin:unknown")
        assert done["inserted"] == done["rows"] and done["skipped"] == 0
        audit = await conn.fetchval("SELECT count(*) AS n FROM acp_shared.audit_log WHERE action = $1 AND "
                                    "resource_id = $2", importer.AUDIT_ACTION, f"{SET}@1.0.0")
        assert audit == 1

        again = await importer.import_labels(conn, _payload(dry_run=False), actor="admin:unknown")
        assert again["inserted"] == 0 and again["unchanged"] == again["rows"]
        assert await conn.fetchval("SELECT count(*) AS n FROM acp_shared.audit_log WHERE action = $1 AND "
                                   "resource_id = $2", importer.AUDIT_ACTION, f"{SET}@1.0.0") == 1

        with pytest.raises(store.StoreRefused, match="differ"):
            await importer.import_labels(conn, _payload(dry_run=False, flip=True), actor="admin:unknown")

        body = _payload(dry_run=False)
        lead = {**body, "judgements": [{"claim_hash": body["export"][1]["claim_hash"],
                                        "labeller_kind": "tech_lead", "class": "fact.number", "label": "violation",
                                        "reason": "invented"}]}
        out = await importer.import_labels(conn, lead, actor="admin:unknown")
        assert out["inserted"] == 1
        rows = await store.read_rows(conn, SET, "1.0.0")
        assert importer.counts(rows)["by_labeller_kind"] == {"agent_panel": done["rows"], "tech_lead": 1}
        finals = pn.final_labels(rows)
        assert pn.contradictions(finals) == []
        assert {f["source"] for f in importer.finals_rows(finals)} >= {"tech_lead"}
    finally:
        await tr.rollback()


@pytest.mark.asyncio
async def test_split_plan_candidate_queries_run_on_the_schema(conn):
    tr = conn.transaction()
    await tr.start()
    try:
        # CIS 124 (content_piece.channel and the reuse columns) is skipped by the CI replay (no pgvector,
        # tests/integration/_replay_guard.py); on Dev the column exists (db-schema-reference.md). Added for this
        # transaction only.
        await conn.execute("ALTER TABLE acp_shared.content_piece ADD COLUMN IF NOT EXISTS channel TEXT")
        cands = await sp.live_candidates(conn)
        assert all(c.writer in sp.LIVE_WRITERS for c in cands)
        res = sp.plan(cands, target_claims=sp.target_held_out_claims())
        assert set(res["writers"]) == set(sp.LIVE_WRITERS)
    finally:
        await tr.rollback()
