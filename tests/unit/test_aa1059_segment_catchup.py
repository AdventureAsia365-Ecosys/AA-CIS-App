"""AA-1059 — the `segment_catchup` recompute stage: which active Masters it picks, the cap, the busy / failed counts,
and how its Segments reach the landing stage. No database (the pool and the segment run are fakes)."""
import random
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.recompute import PLATFORM_STAGES, RecomputeScope, run_stages
from services.recompute import segment_catchup as sc

T1, T2, T3 = "00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002", \
    "00000000-0000-4000-8000-000000000003"


def _row(tour, atom, place, action, activity_type=None):
    return {"tour_id": tour, "atom_id": atom, "place": place, "action": action, "activity_type": activity_type}


ROWS = [
    _row(T1, "a1", "Zzlo Fortress", "visit", "culture"),
    _row(T1, "a2", "Zzlo", "drive to Qarvi", "transit"),
    _row(T2, "b1", "Zzlo airport", "arrive at", None),          # transit by rule, untyped: excluded
    _row(T2, "b2", "Qarvi", "drive to Zzlo", "transit"),         # transit by rule and by type: excluded
    _row(T3, "c1", "Qarvi", "drive to Zzlo", "trek"),            # transit by rule, typed something else: kept
]


def test_plan_keeps_tours_with_a_forming_atom_and_counts_the_rest():
    out = sc.plan(ROWS)
    assert out["tours"] == [T1, T3]
    assert out["all_excluded"] == [T2]


def test_plan_of_nothing_is_empty():
    assert sc.plan([]) == {"tours": [], "all_excluded": []}


@pytest.mark.parametrize("raw,expected", [("", 10), ("3", 3), ("0", 0), ("-4", 0), ("x", 10), (" 7 ", 7)])
def test_catchup_max_reads_the_env_value(raw, expected):
    assert sc.catchup_max(raw) == expected


def test_pick_caps_with_a_sample_and_keeps_all_under_the_cap():
    tours = [f"t{i}" for i in range(20)]
    assert sc.pick(tours[:3], 5) == tours[:3]
    assert sc.pick(tours, 0) == []
    chosen = sc.pick(tours, 4, random.Random(7))
    assert len(chosen) == 4 and set(chosen) <= set(tours) and chosen == sorted(chosen)
    # a different draw can choose other tours: a tour failing every pass cannot hold a slot forever
    draws = {tuple(sc.pick(tours, 4, random.Random(seed))) for seed in range(10)}
    assert len(draws) > 1


def _pool(rows, segment_rows=()):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=lambda sql, *a: rows if "seg_tours" in sql else list(segment_rows))
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield conn

    pool.acquire = acquire
    return pool, conn


def _lock(busy=()):
    from services.export.handler import TourLockTimeout

    @asynccontextmanager
    async def fake(tour_id, should_stop=None, *, poll_seconds=None, max_wait_seconds=None):
        assert poll_seconds == 0 and max_wait_seconds == 0   # never waits for a running a3_atomize
        if tour_id in busy:
            raise TourLockTimeout("busy")
        yield

    return fake


@pytest.mark.asyncio
async def test_run_segments_each_candidate_under_its_tour_lock_and_returns_counts():
    pool, _ = _pool(ROWS, [{"segment_id": "seg-b"}, {"segment_id": "seg-a"}])
    seg = AsyncMock(side_effect=lambda tour, p: {"segments_written": 1 if tour == T1 else 0})
    with patch("services.acp_contract.segment_matching.run_segment_matching", seg), \
         patch("services.export.handler.a3_tour_lock", _lock()):
        out = await sc.run_segment_catchup(pool, max_tours=10)
    assert sorted(c.args[0] for c in seg.await_args_list) == [T1, T3]
    assert out["status"] == "ok" and out["candidates"] == 2 and out["all_excluded"] == 1
    assert out["run"] == 2 and out["segmented"] == 1 and out["busy"] == 0 and out["failed"] == 0
    assert out["deferred"] == 0 and out["sample_segmented"] == [T1]
    assert out["segment_ids"] == ["seg-a", "seg-b"]


@pytest.mark.asyncio
async def test_a_busy_tour_is_skipped_and_a_failure_is_counted_by_class_only():
    pool, _ = _pool(ROWS)

    async def seg(tour, p):
        raise ValueError("a message that must not reach the result")

    with patch("services.acp_contract.segment_matching.run_segment_matching", AsyncMock(side_effect=seg)), \
         patch("services.export.handler.a3_tour_lock", _lock(busy={T1})):
        out = await sc.run_segment_catchup(pool, max_tours=10)
    assert out["busy"] == 1 and out["failed"] == 1 and out["failed_by"] == {"ValueError": 1}
    assert out["run"] == 0 and out["segment_ids"] == []
    assert "message" not in repr(out)


@pytest.mark.asyncio
async def test_cap_defers_the_rest_and_zero_turns_the_stage_off():
    pool, conn = _pool(ROWS)
    seg = AsyncMock(return_value={"segments_written": 0})
    with patch("services.acp_contract.segment_matching.run_segment_matching", seg), \
         patch("services.export.handler.a3_tour_lock", _lock()):
        out = await sc.run_segment_catchup(pool, max_tours=1, rng=random.Random(1))
        assert out["run"] == 1 and out["deferred"] == 1
        assert await sc.run_segment_catchup(pool, max_tours=0) == {"status": "off"}
    assert seg.await_count == 1


@pytest.mark.asyncio
async def test_scope_tour_ids_limits_the_pass():
    pool, _ = _pool(ROWS)
    seg = AsyncMock(return_value={"segments_written": 0})
    with patch("services.acp_contract.segment_matching.run_segment_matching", seg), \
         patch("services.export.handler.a3_tour_lock", _lock()):
        out = await sc.run_segment_catchup(pool, max_tours=10, scope_tour_ids=[T3])
    assert [c.args[0] for c in seg.await_args_list] == [T3] and out["candidates"] == 1 and out["all_excluded"] == 0


@pytest.mark.asyncio
async def test_platform_pass_runs_catchup_first_and_lands_its_segments():
    calls = []
    landing = AsyncMock(side_effect=lambda *a, **k: calls.append("landing") or {})

    async def catchup(pool):
        calls.append("catchup")
        return {"status": "ok", "segment_ids": ["new-1"]}

    with patch("services.recompute.segment_catchup.run_segment_catchup", AsyncMock(side_effect=catchup)), \
         patch("services.acp_contract.atom_ranking.precompute_question_landings", landing), \
         patch("services.acp_contract.atom_ranking.run_atom_ranking", AsyncMock(return_value={})), \
         patch("services.acp_contract.route_detection.run_route_detection", AsyncMock(return_value={})), \
         patch("services.acp_shared.retarget.run_retarget", AsyncMock(return_value={})), \
         patch("services.seo_intelligence.seed_builder.DFS_LOCATION_MAP", {"US": 1}), \
         patch("services.acp_contract.segment_gc.run_segment_gc", AsyncMock(return_value={})):
        res = await run_stages(PLATFORM_STAGES, RecomputeScope(segment_ids={"a"}), pool=object())
        assert calls[:2] == ["catchup", "landing"]
        assert landing.await_args.args[1] == {"a", "new-1"}
        # the ids go to landing, not into the job result
        assert res["segment_catchup"] == {"status": "ok"}
        # a backfill pass (segment_ids None) stays a from-scratch landing
        await run_stages(PLATFORM_STAGES, RecomputeScope(segment_ids=None), pool=object())
        assert landing.await_args.args[1] is None


def test_candidates_sql_reads_only_the_active_atom_view_and_members():
    sql = sc.CANDIDATES_SQL
    assert "acp_contract.v_active_tour_atoms" in sql and "acp_contract.atom_segment_member" in sql
    assert "place IS NOT NULL AND a.action IS NOT NULL" in sql
    for verb in ("INSERT", "UPDATE", "DELETE"):
        assert verb not in sql.upper()
