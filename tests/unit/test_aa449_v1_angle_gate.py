"""AA-449 — api/routers/v1_angle_gate.py. Same convention test_aa448_v1_planning.py uses:
`tenant=` dependency bypassed (endpoint functions called directly, not through FastAPI's
Depends() machinery), service.py itself patched (already unit-tested separately in
test_aa449_angle_gate_service.py) — this file checks HTTP status-code mapping.

AA-522 — TestCreateRequest and TestSetChannel removed: `POST /requests` (atom-only creation) and
`POST /requests/{id}/channel` were deleted along with Luồng B's FE (the only real caller of
either). See api/routers/v1_angle_gate.py's own module docstring for the full rationale."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from api.routers import v1_angle_gate
from services.acp_angle_gate import service

TENANT_ID = str(uuid.uuid4())
REQUEST_ID = uuid.uuid4()


def _make_request():
    """AA-859 W3 — choose / reopen open a scoped connection (flag off here: Redis says "false", so the main
    pool with its own transaction) and re-read with service.fetch_request_on (AA-859 wave 4: the GET and the
    re-read run it in a block behind ANGLE_GATE_READ_FLAG, off here too), which the tests below patch."""
    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_async_cm(None))
    request = MagicMock()
    request.app.state.pool = MagicMock()
    request.app.state.pool.acquire = MagicMock(return_value=_async_cm(conn))
    request.app.state.redis.get = AsyncMock(return_value="false")  # S241 D-12: explicit OFF
    return request


def _async_cm(value):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class TestListGoals:
    @pytest.mark.asyncio
    async def test_returns_eight_goals(self):
        result = await v1_angle_gate.list_goals()
        assert len(result["goals"]) == 8
        assert result["goals"][0]["key"] == "promotion"


class TestSetGoal:
    @pytest.mark.asyncio
    async def test_wrong_status_409(self):
        body = v1_angle_gate.SetGoalBody(goal="promotion")
        with patch.object(
            v1_angle_gate.service, "start_goal_generation",
            new=AsyncMock(side_effect=service.WrongStatusError("already set")),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_angle_gate.set_goal(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_invalid_goal_422(self):
        body = v1_angle_gate.SetGoalBody(goal="not_a_goal")
        with patch.object(
            v1_angle_gate.service, "start_goal_generation",
            new=AsyncMock(side_effect=service.InvalidGoalError("bad goal")),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_angle_gate.set_goal(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_success(self):
        body = v1_angle_gate.SetGoalBody(goal="promotion")
        with patch.object(
            v1_angle_gate.service, "start_goal_generation",
            new=AsyncMock(return_value={"status": "pending_goal", "generating": True, "angles": []}),
        ):
            result = await v1_angle_gate.set_goal(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        # AA-764 — angles arrive from the t8_angles job; the POST only records the goal.
        assert result["generating"] is True


class TestGetRequest:
    @pytest.mark.asyncio
    async def test_not_found_404(self):
        with patch.object(
            v1_angle_gate.service, "fetch_request_on",
            new=AsyncMock(side_effect=service.RequestNotFoundError("nope")),
        ):
            with pytest.raises(HTTPException) as exc:
                await v1_angle_gate.get_request(REQUEST_ID, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 404


class TestChoose:
    @pytest.mark.asyncio
    async def test_success(self):
        body = v1_angle_gate.ChooseBody(idx=1)
        request = _make_request()
        with patch.object(v1_angle_gate.service, "choose_angle", new=AsyncMock(return_value=None)) as choose, \
             patch.object(v1_angle_gate.service, "fetch_request_on",
                          new=AsyncMock(return_value={"status": "approved"})) as fetch:
            result = await v1_angle_gate.choose(REQUEST_ID, body, request, tenant={"sub": TENANT_ID})
        assert result["status"] == "approved"
        conn, tenant_id, request_id, idx = choose.call_args[0]
        assert (str(tenant_id), request_id, idx) == (TENANT_ID, REQUEST_ID, 1)
        assert fetch.call_args[0][1:] == (uuid.UUID(TENANT_ID), REQUEST_ID)

    @pytest.mark.asyncio
    async def test_wrong_status_409(self):
        body = v1_angle_gate.ChooseBody(idx=1)
        with patch.object(
            v1_angle_gate.service, "choose_angle",
            new=AsyncMock(side_effect=service.WrongStatusError("not ready")),
        ), patch.object(v1_angle_gate.service, "fetch_request_on", new=AsyncMock()) as fetch:
            with pytest.raises(HTTPException) as exc:
                await v1_angle_gate.choose(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 409
        fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_found_404(self):
        body = v1_angle_gate.ChooseBody(idx=1)
        with patch.object(
            v1_angle_gate.service, "choose_angle",
            new=AsyncMock(side_effect=service.RequestNotFoundError("nope")),
        ), patch.object(v1_angle_gate.service, "fetch_request_on", new=AsyncMock()):
            with pytest.raises(HTTPException) as exc:
                await v1_angle_gate.choose(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_bad_idx_422(self):
        body = v1_angle_gate.ChooseBody(idx=9)
        with patch.object(
            v1_angle_gate.service, "choose_angle",
            new=AsyncMock(side_effect=service.AngleGateError("bad idx")),
        ), patch.object(v1_angle_gate.service, "fetch_request_on", new=AsyncMock()):
            with pytest.raises(HTTPException) as exc:
                await v1_angle_gate.choose(REQUEST_ID, body, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 422


class TestReopen:
    """AA-497 — POST /v1/angle-gate/requests/{id}/reopen."""

    @pytest.mark.asyncio
    async def test_success(self):
        request = _make_request()
        with patch.object(v1_angle_gate.service, "reopen_request", new=AsyncMock(return_value=None)) as reopen, \
             patch.object(v1_angle_gate.service, "fetch_request_on",
                          new=AsyncMock(return_value={"status": "reusable"})) as fetch:
            result = await v1_angle_gate.reopen(REQUEST_ID, request, tenant={"sub": TENANT_ID})
        assert result["status"] == "reusable"
        _, tenant_id, request_id = reopen.call_args[0]
        assert (str(tenant_id), request_id) == (TENANT_ID, REQUEST_ID)
        assert fetch.call_args[0][1:] == (uuid.UUID(TENANT_ID), REQUEST_ID)

    @pytest.mark.asyncio
    async def test_wrong_status_409(self):
        with patch.object(
            v1_angle_gate.service, "reopen_request",
            new=AsyncMock(side_effect=service.WrongStatusError("not approved")),
        ), patch.object(v1_angle_gate.service, "fetch_request_on", new=AsyncMock()):
            with pytest.raises(HTTPException) as exc:
                await v1_angle_gate.reopen(REQUEST_ID, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_not_found_404(self):
        with patch.object(
            v1_angle_gate.service, "reopen_request",
            new=AsyncMock(side_effect=service.RequestNotFoundError("nope")),
        ), patch.object(v1_angle_gate.service, "fetch_request_on", new=AsyncMock()):
            with pytest.raises(HTTPException) as exc:
                await v1_angle_gate.reopen(REQUEST_ID, _make_request(), tenant={"sub": TENANT_ID})
        assert exc.value.status_code == 404
