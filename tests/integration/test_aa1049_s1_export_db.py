"""AA-1049 W3 (S1 train) — the read-only S1 replay export (scripts/quality/export_aa1049_s1.py) against real Postgres
(migrations applied by conftest): its two queries run on the live schema (every column it names exists and casts), in a
read-only transaction, and a stored v2 Master becomes one replay record with the source tour the S1 job builds. Ids and
texts are invented and deleted at the end.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import asyncpg
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "quality" / "export_aa1049_s1.py"


def _export():
    spec = importlib.util.spec_from_file_location("export_aa1049_s1", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.asyncio
async def test_the_export_queries_run_read_only_on_the_real_schema():
    ex = _export()
    conn = await asyncpg.connect(DSN)
    try:
        async with conn.transaction(readonly=True):
            rows = await conn.fetch(ex.MASTERS_SQL, ex.MASTER_TENANT, 60)
            await conn.fetchrow(ex.BRAND_SQL, ex.MASTER_TENANT)
        cols = {r["column_name"] for r in await conn.fetch(
            "SELECT column_name AS column_name FROM information_schema.columns "
            "WHERE table_schema = 'silver_aa_internal' AND table_name = 'generated_content'")}
    finally:
        await conn.close()
    assert {"aa_name", "aa_itineraries", "seo_meta", "metadata", "human_edited", "version_num"} <= cols
    for r in rows:   # whatever an earlier module left: every row is a v2 Master of the master tenant
        assert json.loads(r["metadata"]).get("writer_version") == "v2"


def test_a_row_becomes_one_replay_record():
    ex = _export()
    row = {"tour_id": "t-1", "version_id": "v-1", "version_num": 2, "status": "approved",
           "created_at": "2026-11-01", "aa_name": "Velmora Walk", "aa_subtitle": "Sub", "aa_summary": "Sum",
           "aa_description": "", "aa_highlights": '["Corran Ridge"]', "aa_itineraries": "Day 1 — Arrive\nArrive.",
           "seo_title": "Velmora Walk", "seo_meta": "Meta.",
           "metadata": json.dumps({"writer_version": "v2", "final_contract": {"verdict": "publish"}}),
           "src_name": "VELMORA 1D", "src_subtitle": None, "src_summary": "S", "src_description": "D",
           "src_highlights": '["A"]', "src_itineraries": "Day 1: Velmora.", "country": "Testland",
           "duration": "1 day", "price_raw": None, "inclusions": "Guide", "exclusions": None, "group_size": "2-8",
           "best_time_to_go": None}
    rec = ex.master_of(row, ["breathtaking"])
    assert rec["tour"] == {"name": "VELMORA 1D", "subtitle": None, "summary": "S", "description": "D",
                           "highlights": ["A"], "itineraries": "Day 1: Velmora.", "country": "Testland",
                           "duration": "1 day", "price": None, "inclusions": "Guide", "exclusions": None}
    assert rec["tour_agency"] == {"group_size": "2-8", "best_time_to_go": None}
    assert rec["stored"]["highlights"] == ["Corran Ridge"] and rec["stored_contract_verdict"] == "publish"
    assert rec["brand_rules"]["forbidden_words"] == ["breathtaking"]
