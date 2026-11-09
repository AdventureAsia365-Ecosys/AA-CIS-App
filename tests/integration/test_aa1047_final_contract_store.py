"""AA-1047 W1 — the REPORT-mode final contract's DB side on real Postgres (conftest replays every migration; each test
runs in one transaction that is rolled back).

  * final_contract_store.S1_ROW_SQL prepares on the real schema and record_s1_exit writes metadata.final_contract and
    appends to metadata.final_contract_log (two exits -> two entries);
  * the report script's Master read (scripts/quality/final_contract_report.masters_sql) lists a live v2 Master whose
    page lost days against the stated duration in the publish-flip set, and leaves a clean one out.

Invented tours and places only (the country is a real one: raw_tours.country must be a listed country name).
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
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MASTER = "00000000-0000-0000-0000-000000000001"
BATCH_ID = "00000000-0000-0000-0000-000000000002"

SRC_ITIN = "Day 1: Arrive in Velmora.\nDay 2: Walk the Karsh gorge.\nDay 3: Boat to Lake Ondra.\nDay 4: Depart."
PAGE_ITIN = ("Day 1 — Velmora\nArrive.\n\nDay 2 — Karsh gorge\nWalk.\n\nDay 3 — Lake Ondra\nBoat.\n\n"
             "Day 4 — Departure\nTransfer.")
META = ("A four-day walk through the Karsh gorge and Lake Ondra, with village inns, a boat crossing and a local "
        "guide on every single day of the long route.")


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


async def _version(conn, *, page_itin=PAGE_ITIN, duration="4 days", src_itin=SRC_ITIN, publish=False,
                   country="Vietnam"):
    tour_id = uuid.uuid4()
    await conn.execute(
        "INSERT INTO silver_aa_internal.raw_tours (tour_id, tenant_id, batch_id, src_name, country, duration, "
        "src_itineraries) VALUES ($1, $2, $3, 'Karsh Gorge 4D', $4, $5, $6)",
        tour_id, MASTER, BATCH_ID, country, duration, src_itin)
    meta = {"writer_version": "v2", "grounding": {"found": 0, "violations": []}}
    gc = await conn.fetchval(
        "INSERT INTO silver_aa_internal.generated_content (tour_id, tenant_id, version_num, aa_name, aa_summary, "
        "aa_highlights, aa_itineraries, seo_title, seo_meta, status, metadata) "
        "VALUES ($1, $2, 1, 'Karsh Gorge and Lake Ondra', 'A short walking trip.', '[\"The gorge\"]'::jsonb, $3, "
        "'Karsh Gorge Walk', $4, 'approved', $5::jsonb) RETURNING id",
        tour_id, MASTER, page_itin, META, json.dumps(meta))
    await conn.execute(
        "INSERT INTO silver_aa_internal.quality_scores (generated_content_id, tour_id, tenant_id, score_overall, "
        "failure_codes, brand_audit_status, brand_audit_codes, brand_audit_issues) "
        "VALUES ($1, $2, $3, 9.5, '[]', 'pass', '[]', '[]')", gc, tour_id, MASTER)
    if publish:
        await conn.execute(
            "INSERT INTO gold_aa_internal.published_tours (tour_id, generated_content_id, tenant_id, aa_name) "
            "VALUES ($1, $2, $3, 'Karsh Gorge and Lake Ondra')", tour_id, gc, MASTER)
    return str(tour_id), str(gc)


async def _metadata(conn, gc):
    raw = await conn.fetchval("SELECT metadata FROM silver_aa_internal.generated_content WHERE id = $1::uuid", gc)
    return json.loads(raw) if isinstance(raw, str) else raw


@pytest.mark.asyncio
async def test_record_s1_exit_writes_the_record_and_the_exit_log(conn):
    from services.quality import final_contract_store as store
    _, gc = await _version(conn)
    first = await store.record_s1_exit(conn, gc, "auto_publish", actual="published")
    assert first is not None and first["verdict"] == "publish", first["findings"]
    assert first["sha_match"] is True and first["agrees"] is True
    await store.record_s1_exit(conn, gc, "promote", actual="published")
    meta = await _metadata(conn, gc)
    assert meta["final_contract"]["exit"] == "promote"
    assert [e["exit"] for e in meta["final_contract_log"]] == ["auto_publish", "promote"]
    assert meta["writer_version"] == "v2"      # the rest of metadata is kept


@pytest.mark.asyncio
async def test_the_report_lists_a_day_loss_master_in_the_flip_set(conn):
    from scripts.quality import final_contract_report as report
    lost_tour, _ = await _version(conn, page_itin="Day 1 — Velmora\nArrive.\n\nDay 2 — Gorge\nWalk.",
                                  duration="12 days", src_itin="Walk, sail and rest.", publish=True,
                                  country="Thailand")
    clean_tour, _ = await _version(conn, publish=True, country="Thailand")
    rows = [r for r in await conn.fetch(report.masters_sql(False)) if r["tour_id"] in (lost_tour, clean_tour)]
    assert len(rows) == 2
    out = report.summarize(rows)
    assert out["masters"] == 2 and out["publish_flip"]["count"] == 1
    [flip] = out["flips"]
    assert flip["tour_id"] == lost_tour and "DAY_COUNT_VS_DURATION" in flip["block_codes"]
    assert out["day_loss"] == [{"tour_id": lost_tour, "country": "Thailand", "page_days": 2, "duration_days": 12}]
    assert "Karsh" not in json.dumps(out)       # ids and counts only, never page text
