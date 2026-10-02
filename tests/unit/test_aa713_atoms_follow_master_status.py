"""AA-713 — atom reads go through v_active_tour_atoms (which gates on published_tours.master_status),
and a master_status change recomputes the Score + Route caches so inactive atoms are evicted."""
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _src(rel):
    return (ROOT / rel).read_text()


def test_a3_atom_reads_use_the_active_view_not_raw_tour_atoms():
    seg = _src("services/acp_contract/segment_matching.py")
    rank = _src("services/acp_contract/atom_ranking.py")
    route = _src("services/acp_contract/route_detection.py")
    # The main per-tour atom load, both ranking member loads, and the route day-join all read the view.
    assert "FROM acp_contract.v_active_tour_atoms ta" in seg
    assert rank.count("JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = asm.atom_id") == 2
    assert "JOIN acp_contract.v_active_tour_atoms ta" in route
    # The view already filters deleted/empty, so the old per-site predicate is gone from those reads.
    assert "WHERE ta.itinerary_day IS NOT NULL AND NOT ta.deleted" not in route


def test_migration_203_defines_the_view_with_the_status_gate():
    mig = _src("api/migrations/203_v_active_tour_atoms.sql")
    assert "CREATE OR REPLACE VIEW acp_contract.v_active_tour_atoms" in mig
    assert "master_status = 'active'" in mig and "deleted_at IS NULL" in mig
    assert "NOT ta.deleted" in mig and "NOT ta.is_empty_marker" in mig


def test_all_five_status_endpoints_trigger_a_recompute():
    admin = _src("api/routers/admin.py")
    # one helper call per endpoint: toggle, trash, restore, activate, deactivate
    assert admin.count("_recompute_after_status_change(") >= 6  # 5 calls + the def


@pytest.mark.asyncio
async def test_recompute_rankings_and_routes_runs_every_market_then_routes():
    from services.export import handler
    calls = []
    with patch("services.acp_contract.atom_ranking.precompute_question_landings",
               new=AsyncMock(return_value={})) as pql, \
         patch("services.acp_contract.atom_ranking.run_atom_ranking",
               new=AsyncMock(side_effect=lambda m, *_: calls.append(m) or {})) as rar, \
         patch("services.acp_contract.route_detection.run_route_detection",
               new=AsyncMock(return_value={"routes": 0})) as rrd, \
         patch("services.seo_intelligence.seed_builder.DFS_LOCATION_MAP", {"US": 1, "UK": 2}):
        out = await handler.recompute_rankings_and_routes(object(), log_reason="test")
    assert pql.await_count == 1
    assert set(calls) == {"US", "UK"} and rar.await_count == 2
    assert rrd.await_count == 1
    assert "route" in out and "ranking" in out
