"""AA-1081 (S278) — the read-only cache-miss check (scripts/quality/diag_aa1081_debate_cache_miss.py) on the real
schema: one judged Segment per cause, the window taken from the tenant's previous succeeded slate, and the Segment the
slow run served from the cache left out.

Invented tours, places and ids, timestamps in 2001 so no other test's rows fall in the window; everything is rolled
back.
"""
import json
import os
import sys
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa986_l2_tour_lifecycle as l2  # noqa: E402  (seed helpers)

from scripts.quality import diag_aa1081_debate_cache_miss as diag  # noqa: E402


def _t(day, hour=0):
    return datetime(2001, 1, day, hour, tzinfo=timezone.utc)


PREV_SLATE_END = _t(10)
RUN_START = _t(20)


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(l2.DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


async def _cache(c, tenant, seg, version, at):
    await c.execute("INSERT INTO acp_shared.debate_brand_fit_cache (tenant_id, segment_id, brand_version, "
                    "judge_score, brand_fit_score, cross_brand_distinct, mission_present, computed_at) "
                    "VALUES ($1::uuid, $2, $3, 7, 7, 7, true, $4)", tenant, seg, version, at)


async def _job(c, kind, payload, *, status, created, finished, result=None):
    await c.execute("INSERT INTO shared.job (kind, payload, status, created_at, finished_at, result) "
                    "VALUES ($1, $2::jsonb, $3, $4, $5, $6::jsonb)",
                    kind, json.dumps(payload), status, created, finished,
                    json.dumps(result) if result is not None else None)


async def _seed(c):
    tenant = await l2._tenant(c, "zz1081d")
    await c.execute("INSERT INTO shared.tenant_brand_rules (tenant_id, brand_name, version, is_active) "
                    "VALUES ($1::uuid, 'default', 1, false), ($1::uuid, 'default', 2, true)", tenant)
    segs = {"zz1081d_brand": _t(1), "zz1081d_new": _t(15), "zz1081d_merged": _t(1), "zz1081d_gone": _t(1),
            "zz1081d_cleared": _t(1), "zz1081d_plain": _t(1), "zz1081d_hit": _t(1)}
    await c.executemany("INSERT INTO acp_contract.atom_segment (segment_id, canonical_place, canonical_action, "
                        "created_at) VALUES ($1, 'Vessa Cove', 'paddle', $2)", list(segs.items()))
    await c.execute("INSERT INTO acp_contract.atom_segment_alias (segment_id_old, segment_id_canonical, merged_at) "
                    "VALUES ('zz1081d_gone', 'zz1081d_merged', $1)", _t(12))
    tour = (await l2._tour(c, "zz1081d")).tour
    await c.execute("INSERT INTO acp_contract.tour_atoms (atom_id, tour_id, owner_scope, text, place, evidence) "
                    "VALUES ('zz1081d_a1', $1::uuid, 'platform', 'Paddle the cove', 'Vessa Cove', 'x')", tour)
    await c.execute("INSERT INTO acp_contract.atom_segment_member (segment_id, atom_id) "
                    "VALUES ('zz1081d_cleared', 'zz1081d_a1')")
    # the earlier slate (fills the cache) and the slow run
    await _job(c, "t7_slate", {"tenant_id": tenant}, status="succeeded", created=_t(9), finished=PREV_SLATE_END)
    await _job(c, "t7_slate", {"tenant_id": tenant}, status="succeeded", created=RUN_START, finished=_t(20, 1))
    # an atomize in the window that cleared rows of the tour, and one before it (out of the window)
    await _job(c, "a3_atomize", {"tour_id": tour}, status="succeeded", created=_t(12), finished=_t(12),
               result={"tour_id": tour, "brand_fit_cache_cleared": 3, "brand_fit_segments_changed": 1})
    # one judge call of the earlier slate
    await c.execute("INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, tenant_id, status, "
                    "created_at) VALUES ('t7_debate_judge', 'judge', 'zz-judge', '{}'::jsonb, $1::uuid, 'ok', $2)",
                    tenant, _t(9, 1))
    # cache: older rows (before the slow run) and what the slow run wrote
    await _cache(c, tenant, "zz1081d_brand", 1, _t(5))
    await _cache(c, tenant, "zz1081d_hit", 2, _t(5))
    for seg in ("zz1081d_brand", "zz1081d_new", "zz1081d_merged", "zz1081d_cleared", "zz1081d_plain"):
        await _cache(c, tenant, seg, 2, _t(20, 1))
    return tenant


@pytest.mark.asyncio
async def test_each_judged_segment_gets_its_cause(conn):
    tenant = await _seed(conn)
    out = await diag.diagnose_tenant(conn, tenant, RUN_START, _t(1))

    assert out["window_start"] == PREV_SLATE_END.isoformat()
    assert out["window_from_previous_slate"] is True
    assert out["listed_from"] == _t(9).isoformat()
    assert out["current_brand_version"] == 2
    assert {s["segment_id"]: s["cause"] for s in out["segments"]} == {
        "zz1081d_brand": "brand_version",
        "zz1081d_new": "new_or_merged_segment",
        "zz1081d_merged": "new_or_merged_segment",
        "zz1081d_cleared": "cleared_by_atomize",
        "zz1081d_plain": "not_cached_before",
    }
    assert out["by_cause"] == {"brand_version": 1, "new_or_merged_segment": 2, "cleared_by_atomize": 1,
                               "not_cached_before": 1}
    assert out["verdict"] == "new_or_merged_segment"
    assert out["brand_version_changed"] is False
    assert out["atomize_in_window"]["jobs_clearing"] >= 1
    assert out["atomize_in_window"]["rows_cleared"] >= 3
    assert [r["brand_version"] for r in out["cache_by_version"]] == [1, 2]
    assert [j["status"] for j in out["slate_jobs"]] == ["succeeded", "succeeded"]
    assert {r["version"] for r in out["brand_rows"]} == {1, 2}
    assert [(j["status"], j["is_shadow"], j["n"]) for j in out["judge_calls"]] == [("ok", False, 1)]
    json.dumps(out, default=str)   # the script prints it


@pytest.mark.asyncio
async def test_tenants_default_to_the_slow_run(conn):
    tenant = await _seed(conn)
    tenants = [r["tenant_id"] for r in await conn.fetch(diag._TENANTS_SQL, RUN_START)]
    assert tenant in tenants
    assert tenant not in [r["tenant_id"] for r in await conn.fetch(diag._TENANTS_SQL, _t(21))]
