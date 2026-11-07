"""AA-1047 (Quality W1) — the FactSheet store on real Postgres (CIS 386 applied by conftest; the country stays NULL: the raw_tours CHECK accepts real countries only): a tour sheet stored once
and reused, the fs_id stamps on generated_content and tenant_tour_versions (stand-in: the CI replay has no CIS 001),
the stored record read back, the T2 source query (_PT_SQL with the raw fields the FactSheet reads) preparing on the
real schema and feeding the same sheet, and a failed write that leaves the caller's transaction usable.

Everything runs in one transaction per test, rolled back. Invented names and places only.
"""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, TTV_STANDIN_DDL  # noqa: E402
from _constants import BATCH_ID  # noqa: E402

from services.jobs import t2_rewrite_job as job  # noqa: E402
from services.quality import factsheet as fs  # noqa: E402
from services.quality import factsheet_store as store  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
AGENCY = "00000000-0000-4000-8000-000000a10472"


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    store._skip_until = 0.0
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()
        store._skip_until = 0.0


async def _raw_tour(conn) -> uuid.UUID:
    tour_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, src_summary, "
        "src_highlights, src_itineraries, country, duration, price_raw, inclusions, exclusions, group_size, "
        "best_time_to_go, source_status) VALUES ($1, $2, $3, 'Varn Valley Walk', 'A walk in the Varn Valley.', "
        "'[\"Rim path of Lumen Gorge\"]'::jsonb, $4, NULL, '2 days', '1,250 USD', 'Guide\nLodge nights', "
        "'Tips', '2-12', 'May to September', 'active')",
        tour_id, MASTER, BATCH_ID, "Day 1: Tavo\nWalk 12 km to Lumen Gorge.\nDay 2: Orsa\nRow 3 km to the chapel.")
    return tour_id


@pytest.mark.asyncio
async def test_s1_sheet_is_stored_once_and_stamped_on_the_version(conn):
    tour_id = await _raw_tour(conn)
    row = await conn.fetchrow("SELECT * FROM silver_aa_internal.raw_tours WHERE tour_id = $1", tour_id)
    version_id = await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name, status) "
        "VALUES ($1, $2, 1, 'Varn Valley Walk', 'approved') RETURNING id", tour_id, MASTER)
    fs_id = await store.save_tour_sheet(conn, row, tenant_id=MASTER, workflow="s1", tour_id=tour_id)
    assert fs_id and await store.save_tour_sheet(conn, row, tenant_id=MASTER, workflow="s1",
                                                 tour_id=tour_id) == fs_id
    assert await conn.fetchval("SELECT count(*) AS n FROM acp_shared.fact_sheet WHERE tour_id = $1", tour_id) == 1
    assert await store.stamp_generated_content(conn, version_id, fs_id)
    assert str(await conn.fetchval("SELECT fs_id FROM silver_aa_internal.generated_content WHERE id = $1",
                                   version_id)) == fs_id
    rec = await store.load(conn, fs_id)
    tour, agency = fs.raw_source_tour(row)
    sheet = fs.build_tour_factsheet(tour, agency=agency)
    assert rec["fs_sha"] == sheet.sha() and rec["render_text"] == sheet.render()
    assert rec["workflow"] == "s1" and rec["builder"] == "tour" and rec["pointer_failures"] == []
    assert [ln["id"] for ln in rec["lines"]] == [ln.id for ln in sheet.lines]
    assert any(ln["text"] == "Guide" and ln["field"] == "inclusions" for ln in rec["lines"])


@pytest.mark.asyncio
async def test_t2_source_query_feeds_the_same_sheet_and_the_version_stamp(conn):
    stmt = await conn.prepare(job._PT_SQL)
    cols = [a.name for a in stmt.get_attributes()]
    for col in ("exclusions", "group_size", "best_time_to_go", "inclusions", "src_itineraries", "src_highlights",
                "price_raw", "tour_id"):
        assert col in cols
    tour_id = await _raw_tour(conn)
    pt_id = await conn.fetchval(
        "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name, "
        "aa_summary, aa_highlights, master_status, published_at) VALUES ($1, $2, $3, 'Varn Valley Walk', 'S', "
        "'[]'::jsonb, 'active', now()) RETURNING id", tour_id, uuid.uuid4(), MASTER)
    pt = await conn.fetchrow(job._PT_SQL, pt_id)
    s1_row = await conn.fetchrow("SELECT * FROM silver_aa_internal.raw_tours WHERE tour_id = $1", tour_id)
    t2_tour, t2_agency = fs.raw_source_tour(pt)
    s1_tour, s1_agency = fs.raw_source_tour(s1_row)
    assert fs.build_tour_factsheet(t2_tour, agency=t2_agency).sha() == \
        fs.build_tour_factsheet(s1_tour, agency=s1_agency).sha()     # T2 and S1 read the same raw source
    fs_id = await store.save_tour_sheet(conn, pt, tenant_id=AGENCY, workflow="t2", tour_id=pt["tour_id"])
    assert fs_id
    if await conn.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions') IS NULL AS missing"):
        await conn.execute(TTV_STANDIN_DDL)
    await conn.execute("ALTER TABLE gold_aa_internal.tenant_tour_versions ADD COLUMN IF NOT EXISTS fs_id uuid")
    version_id = await conn.fetchval(
        "INSERT INTO gold_aa_internal.tenant_tour_versions (tenant_id, published_tour_id) VALUES ($1, $2) "
        "RETURNING id", AGENCY, pt_id)
    assert await store.stamp_tenant_version(conn, version_id, fs_id)
    assert str(await conn.fetchval("SELECT fs_id FROM gold_aa_internal.tenant_tour_versions WHERE id = $1",
                                   version_id)) == fs_id


@pytest.mark.asyncio
async def test_t9_route_sheet_round_trips_with_its_exclusions(conn):
    sheet = fs.build_route_factsheet(
        seed="Lumen Gorge — walk\nLocation: Varn, Zemlia\nSource itinerary: Walk to the gorge.",
        facts_text="[Fact id=f-1]\nEntry: e-visa.\nSource: board; platform fact", brand_audience=None,
        trip_name="Zemlia Loop", destination="Zemlia",
        excluded_facts=[{"fact_id": "f-2", "scope": "tenant", "chars": 50}])
    fs_id = await store.save(conn, sheet, tenant_id=AGENCY, workflow="t9")
    rec = await store.load(conn, fs_id)
    assert rec["excluded"] == [dict(e) for e in sheet.excluded] and rec["render_text"] == sheet.render()
    row = await conn.fetchrow("SELECT line_count, excluded_count FROM acp_shared.fact_sheet WHERE fs_id = $1::uuid",
                              fs_id)
    assert (row["line_count"], row["excluded_count"]) == (len(sheet.lines), 1)
    provs = {ln["id"]: ln["provenance"] for ln in rec["lines"]}
    assert provs["F1"] == fs.HUMAN_VERIFIED and provs["T2"] == fs.SOURCE_RAW
    assert json.loads(json.dumps(rec["lines"])) == rec["lines"]


@pytest.mark.asyncio
async def test_a_failed_write_leaves_the_callers_transaction_usable(conn):
    await conn.execute("ALTER TABLE acp_shared.fact_sheet RENAME TO fact_sheet_away")
    sheet = fs.build_route_factsheet(seed="x", facts_text="", brand_audience=None, trip_name=None, destination=None)
    assert await store.save(conn, sheet, tenant_id=AGENCY, workflow="t9") is None
    assert await conn.fetchval("SELECT 1 AS ok") == 1                 # the savepoint rolled back, not the transaction
    assert store._skipping()                                           # a missing table backs off
