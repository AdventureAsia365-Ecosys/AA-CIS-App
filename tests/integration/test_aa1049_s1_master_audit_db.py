"""AA-1049 — the read-only S1 Master audit (scripts/quality/audit_s1_masters.py) on real Postgres (conftest replays
every migration; each test runs in one transaction that is rolled back): every query it runs prepares and runs on the
live schema inside a read-only transaction, an active published Master becomes one audit line with its contract
verdict and compose diff, a draft-only source is counted as a live source without an active Master, and an inactive
Master is left out of the audited set.

Invented tours and places only (the country is a real one: raw_tours.country must be a listed country name).
"""
import importlib.util
import json
import os
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "quality" / "audit_s1_masters.py"

SRC_ITIN = "Day 1: Arrive in Velmora.\nMeals: D\nDay 2: Walk the Karsh gorge.\nMeals: B"
PAGE_ITIN = "Day 1 — Velmora\nArrive in Velmora by the canal.\n\nDay 2 — Karsh gorge\nWalk the Karsh gorge."
META = ("A two-day walk from Velmora through the Karsh gorge, with a canal arrival, a gorge trail and a night in "
        "the old town before the walk back.")


def _audit():
    spec = importlib.util.spec_from_file_location("audit_s1_masters", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _tour(conn, *, master_status=None, writer="v2"):
    tour_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "src_itineraries) VALUES ($1, $2, $3, 'Karsh Gorge 2D', 'Vietnam', '2 days', $4)",
        tour_id, MASTER, BATCH_ID, SRC_ITIN)
    meta = {"grounding": {"found": 0, "violations": []}, **({"writer_version": writer} if writer else {})}
    gc = await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name, aa_summary, "
        "aa_highlights, aa_itineraries, seo_title, seo_meta, status, metadata, model_editorial) "
        "VALUES ($1, $2, 1, 'Karsh Gorge from Velmora', 'A short walking trip.', '[\"The gorge\"]'::jsonb, $3, "
        "'Karsh Gorge Walk', $4, 'approved', $5::jsonb, 'invented-model') RETURNING id",
        tour_id, MASTER, PAGE_ITIN, META, json.dumps(meta))
    await conn.execute(
        "INSERT INTO silver_aa_internal.quality_scores (generated_content_id, tour_id, tenant_id, score_overall, "
        "failure_codes, brand_audit_status, brand_audit_codes, brand_audit_issues) "
        "VALUES ($1, $2, $3, 9.5, '[]', 'pass', '[]', '[]')", gc, tour_id, MASTER)
    if master_status:
        await conn.execute(
            "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
            "master_status) VALUES ($1, $2, $3, 'Karsh Gorge from Velmora', $4::gold_aa_internal.master_status_enum)",
            tour_id, gc, MASTER, master_status)
    return str(tour_id)


@pytest.mark.asyncio
async def test_every_audit_query_runs_in_a_read_only_transaction():
    a = _audit()
    c = await asyncpg.connect(DSN)
    try:
        async with c.transaction(readonly=True):
            await c.fetch(a.masters_sql(), a.MASTER_TENANT)
            await c.fetchrow(a.BRAND_SQL, a.MASTER_TENANT)
            await c.fetch(a.POPULATION_SQL, a.MASTER_TENANT)
            await c.fetch(a.UNMASTERED_SQL, a.MASTER_TENANT)
            await c.fetchrow(a.ACTIVE_COUNT_SQL, a.MASTER_TENANT)
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_an_active_master_becomes_one_line_and_the_rest_are_counted(conn):
    a = _audit()
    active = await _tour(conn, master_status="active")
    older = await _tour(conn, master_status="active", writer=None)
    inactive = await _tour(conn, master_status="inactive")
    draft = await _tour(conn)
    rows = await conn.fetch(a.masters_sql(), a.MASTER_TENANT)
    pop = await conn.fetch(a.POPULATION_SQL, a.MASTER_TENANT)
    unm = await conn.fetch(a.UNMASTERED_SQL, a.MASTER_TENANT)
    counts = await conn.fetchrow(a.ACTIVE_COUNT_SQL, a.MASTER_TENANT)
    ids = {r["tour_id"] for r in rows}
    assert {active, older} <= ids and inactive not in ids and draft not in ids
    assert counts["master_tenant"] >= 2
    unmastered = {u["tour_id"] for u in unm}
    assert {inactive, draft} <= unmastered and active not in unmastered
    population = a.population_of(list(pop), list(unm), counts)
    assert population["raw_tours"] >= 4 and population["master"].get("inactive", 0) >= 1

    by_id = {r["tour_id"]: r for r in rows}
    line = a.audit_one(by_id[active], ["velvety"])
    assert line["errors"] == {}, line["errors"]
    assert line["writer_group"] == "v2" and line["contract"]["verdict"] in ("publish", "review")
    assert line["compose"]["days"]["planned"] == 2 and line["compose"]["applies"] is True
    old = a.audit_one(by_id[older], [])
    assert old["writer"] == "invented-model" and old["writer_group"] == "older" and old["errors"] == {}
