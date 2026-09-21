"""AA-448 — T7 Content Planning router (api/routers/v1_planning.py).

Same conventions as test_aa444_marketplace_view.py: mocked asyncpg pool, `tenant=` dependency
bypassed (called directly, not through FastAPI's Depends() machinery).

AA-578 (2026-09-11) — TestPreviewQuarterPlan/TestFinalizeQuarterPlan/TestGetQuarterPlan/
TestGetSlotSuggestions (and the _trip_row/_atom_row/_plan_side_effect helpers only they used)
removed along with the 4 routes they tested (POST /quarter-plan/preview, GET+POST /quarter-plan,
GET /slot-grid, GET /slot-suggestions) — see v1_planning.py's own removal note at the same spot.
Never keep a test for code that no longer exists.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import v1_planning

TENANT_ID = str(uuid.uuid4())


def _make_pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _make_request(pool):
    request = MagicMock()
    request.app.state.pool = pool
    return request


class TestAuthReusesV1ToursDependency:
    def test_get_tenant_imported_not_redefined(self):
        from api.routers.v1_tours import get_tenant

        assert v1_planning.get_tenant is get_tenant


# AA-603 — TestMetricsEndpoints removed: the /metrics + /metrics/rollup feedback-loop endpoints
# (and their content_metrics backing service) were deleted (dead N7/N8 loop, never ran on real
# data). Trip-reallocation endpoints below are unaffected and kept.


class TestTripReallocationEndpoints:
    @pytest.mark.asyncio
    async def test_suggest_wires_through_to_service(self):
        with patch(
            "api.routers.v1_planning.suggest_trip_reallocation",
            new=AsyncMock(return_value={"added": [], "removed": []}),
        ) as mock_suggest:
            request = _make_request(MagicMock())
            result = await v1_planning.get_trip_reallocation_suggestion(
                request, tenant={"sub": TENANT_ID}, year=2026, quarter=1,
            )
            mock_suggest.assert_awaited_once()
            assert result == {"added": [], "removed": []}

    @pytest.mark.asyncio
    async def test_confirm_wires_through_to_service(self):
        with patch(
            "api.routers.v1_planning.confirm_trip_reallocation",
            new=AsyncMock(return_value={"accepted": True}),
        ) as mock_confirm:
            body = v1_planning.TripReallocationConfirmRequest(year=2026, quarter=1, accept=True)
            request = _make_request(MagicMock())
            result = await v1_planning.post_trip_reallocation_confirm(
                body, request, tenant={"sub": TENANT_ID},
            )
            mock_confirm.assert_awaited_once()
            assert result == {"accepted": True}
