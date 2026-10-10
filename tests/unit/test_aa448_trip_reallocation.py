"""AA-448 round 6 — services/acp_planning/trip_reallocation.py.

suggest_trip_reallocation()/confirm_trip_reallocation() are DB-backed — heavy mocking of the
whole compute chain (tenant_config/tenant_pool/runway/dfs_relevance/quarter), same convention
as test_aa448_v1_planning.py's router tests, via unittest.mock.patch on each imported name.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_planning.models import QuarterPlan, TripScore
from services.acp_planning.tenant_config import TenantPlanningConfig
from services.acp_planning.trip_reallocation import confirm_trip_reallocation, suggest_trip_reallocation

TENANT = uuid.uuid4()
TRIP_A = uuid.uuid4()
TRIP_B = uuid.uuid4()


def _fresh_plan(trip_ids):
    return QuarterPlan(
        tenant_id=TENANT, year=2026, quarter=2, trip_ids=list(trip_ids),
        trip_scores=[
            TripScore(trip_id=t, name="T", score=0.5, runway_fit=0.5, richness=0.5,
                      forced=False, selected=True, reason="x")
            for t in trip_ids
        ],
    )


PATCH_TARGETS = {
    "config": "services.acp_planning.trip_reallocation.fetch_tenant_planning_config",
    "trips": "services.acp_planning.trip_reallocation.fetch_tenant_trips",
    "atoms": "services.acp_planning.trip_reallocation.fetch_tenant_atoms_by_trip",
    "dfs": "services.acp_planning.trip_reallocation.fetch_dfs_relevance_by_tour",
    "existing": "services.acp_planning.trip_reallocation.fetch_approved_quarter_plan",
    "compute": "services.acp_planning.trip_reallocation.compute_quarter_plan",
}


class TestSuggestTripReallocation:
    @pytest.mark.asyncio
    async def test_never_writes_pure_compute_and_diff(self):
        with patch(PATCH_TARGETS["config"], new=AsyncMock(
                return_value=TenantPlanningConfig(markets=["US"], channels=["blog"], capacity_posts_per_week=2))), \
             patch(PATCH_TARGETS["trips"], new=AsyncMock(return_value=[])), \
             patch(PATCH_TARGETS["atoms"], new=AsyncMock(return_value={})), \
             patch(PATCH_TARGETS["dfs"], new=AsyncMock(return_value={})), \
             patch(PATCH_TARGETS["existing"], new=AsyncMock(
                 return_value=_fresh_plan([TRIP_A]))) as mock_existing, \
             patch(PATCH_TARGETS["compute"], return_value=_fresh_plan([TRIP_B])) as mock_compute:
            pool = MagicMock()
            result = await suggest_trip_reallocation(TENANT, 2026, 2, pool)

        mock_existing.assert_awaited_once()
        mock_compute.assert_called_once()
        assert result["has_existing_plan"] is True
        assert result["added"] == [str(TRIP_B)]
        assert result["removed"] == [str(TRIP_A)]
        assert result["unchanged"] == []

    @pytest.mark.asyncio
    async def test_no_existing_plan_everything_is_added(self):
        with patch(PATCH_TARGETS["config"], new=AsyncMock(
                return_value=TenantPlanningConfig(markets=["US"], channels=["blog"], capacity_posts_per_week=2))), \
             patch(PATCH_TARGETS["trips"], new=AsyncMock(return_value=[])), \
             patch(PATCH_TARGETS["atoms"], new=AsyncMock(return_value={})), \
             patch(PATCH_TARGETS["dfs"], new=AsyncMock(return_value={})), \
             patch(PATCH_TARGETS["existing"], new=AsyncMock(return_value=None)), \
             patch(PATCH_TARGETS["compute"], return_value=_fresh_plan([TRIP_A, TRIP_B])):
            pool = MagicMock()
            result = await suggest_trip_reallocation(TENANT, 2026, 2, pool)

        assert result["has_existing_plan"] is False
        assert set(result["added"]) == {str(TRIP_A), str(TRIP_B)}
        assert result["removed"] == []

    @pytest.mark.asyncio
    async def test_unchanged_trips_stay_unchanged_not_added_or_removed(self):
        with patch(PATCH_TARGETS["config"], new=AsyncMock(
                return_value=TenantPlanningConfig(markets=["US"], channels=["blog"], capacity_posts_per_week=2))), \
             patch(PATCH_TARGETS["trips"], new=AsyncMock(return_value=[])), \
             patch(PATCH_TARGETS["atoms"], new=AsyncMock(return_value={})), \
             patch(PATCH_TARGETS["dfs"], new=AsyncMock(return_value={})), \
             patch(PATCH_TARGETS["existing"], new=AsyncMock(return_value=_fresh_plan([TRIP_A]))), \
             patch(PATCH_TARGETS["compute"], return_value=_fresh_plan([TRIP_A])):
            pool = MagicMock()
            result = await suggest_trip_reallocation(TENANT, 2026, 2, pool)

        assert result["unchanged"] == [str(TRIP_A)]
        assert result["added"] == [] and result["removed"] == []


class TestConfirmTripReallocation:
    @pytest.mark.asyncio
    async def test_reject_logs_but_does_not_save_a_new_version(self):
        conn = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=ctx)

        suggestion = {"plan": _fresh_plan([TRIP_A]).model_dump(mode="json"),
                     "added": [], "removed": [], "has_existing_plan": True}
        with patch("services.acp_planning.trip_reallocation.suggest_trip_reallocation",
                   new=AsyncMock(return_value=suggestion)), \
             patch("services.acp_planning.trip_reallocation.save_quarter_plan_version",
                   new=AsyncMock()) as mock_save:
            result = await confirm_trip_reallocation(pool, TENANT, 2026, 2, accept=False, actor="tenant:x")

        mock_save.assert_not_awaited()
        assert result["accepted"] is False
        conn.execute.assert_awaited_once()  # the audit_log insert, and nothing else
        query = conn.execute.call_args[0][0]
        assert "audit_log" in query

    @pytest.mark.asyncio
    async def test_accept_saves_and_approves_via_gate_b_option_a_path(self):
        conn = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=ctx)

        suggestion = {"plan": _fresh_plan([TRIP_B]).model_dump(mode="json"),
                     "added": [str(TRIP_B)], "removed": [str(TRIP_A)], "has_existing_plan": True}
        new_version_id = uuid.uuid4()
        with patch("services.acp_planning.trip_reallocation.suggest_trip_reallocation",
                   new=AsyncMock(return_value=suggestion)), \
             patch("services.acp_planning.trip_reallocation.save_quarter_plan_version",
                   new=AsyncMock(return_value=new_version_id)) as mock_save, \
             patch("services.acp_planning.trip_reallocation.approve_quarter_plan_version",
                   new=AsyncMock()) as mock_approve:
            result = await confirm_trip_reallocation(pool, TENANT, 2026, 2, accept=True, actor="tenant:x")

        mock_save.assert_awaited_once()
        mock_approve.assert_awaited_once_with(new_version_id, f"tenant:{TENANT}", pool)
        assert result["accepted"] is True
        assert result["version_id"] == str(new_version_id)

    @pytest.mark.asyncio
    async def test_always_logs_regardless_of_accept_reject(self):
        """mirrors trust_ramp.py::confirm_ramp_transition()'s 'never silently' framing — a
        rejection is logged just as much as an acceptance."""
        for accept in (True, False):
            conn = AsyncMock()
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=conn)
            ctx.__aexit__ = AsyncMock(return_value=False)
            pool = MagicMock()
            pool.acquire = MagicMock(return_value=ctx)
            suggestion = {"plan": _fresh_plan([TRIP_A]).model_dump(mode="json"),
                         "added": [], "removed": [], "has_existing_plan": True}
            with patch("services.acp_planning.trip_reallocation.suggest_trip_reallocation",
                       new=AsyncMock(return_value=suggestion)), \
                 patch("services.acp_planning.trip_reallocation.save_quarter_plan_version",
                       new=AsyncMock(return_value=uuid.uuid4())), \
                 patch("services.acp_planning.trip_reallocation.approve_quarter_plan_version",
                       new=AsyncMock()):
                await confirm_trip_reallocation(pool, TENANT, 2026, 2, accept=accept, actor="tenant:x")
            assert conn.execute.await_count >= 1
