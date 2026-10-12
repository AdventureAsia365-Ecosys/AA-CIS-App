"""AA-497 (AA-494 Decision 3) — angle_gate_request gains 'reusable' (already live, migration
124), the tenant-triggered reopen_request() action, choose_angle()'s widened guard, and the
content_piece angle_gate_option_id wiring needed for a second real T9 write to work.

Reuses test_aa449_angle_gate_service.py's pool/fixture helpers (same mocking shape, no need to
duplicate) — this file only covers the NEW behavior, not re-testing choose_angle()'s existing
happy path or set_goal_and_generate()/create_request() (already covered there).
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from services.acp_angle_gate import service
from tests.unit.test_aa449_angle_gate_service import (
    OTHER_TENANT_ID, REQUEST_ID, TENANT_ID, _make_pool, _option_row, _request_row,
)


# ── reopen_request() ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestReopenRequest:
    async def test_happy_path_approved_to_reusable(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _request_row(status="approved", goal="promotion")
        pool = _make_pool(conn)

        with patch.object(service, "fetch_request", new=AsyncMock(return_value={"status": "reusable"})):
            result = await service.reopen_request(TENANT_ID, REQUEST_ID, pool)

        assert result["status"] == "reusable"
        update_calls = [c for c in conn.execute.call_args_list if "SET status = 'reusable'" in c[0][0]]
        assert len(update_calls) == 1
        assert update_calls[0][0][1] == REQUEST_ID

    @pytest.mark.parametrize("status", ["pending_goal", "pending_choice", "reusable"])
    async def test_wrong_status_raises(self, status):
        conn = AsyncMock()
        conn.fetchrow.return_value = _request_row(status=status)
        pool = _make_pool(conn)
        with pytest.raises(service.WrongStatusError):
            await service.reopen_request(TENANT_ID, REQUEST_ID, pool)
        conn.execute.assert_not_called()

    async def test_cross_tenant_request_not_found(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = None
        pool = _make_pool(conn)
        with pytest.raises(service.RequestNotFoundError):
            await service.reopen_request(OTHER_TENANT_ID, REQUEST_ID, pool)


# ── choose_angle() widened guard ─────────────────────────────────────────────

@pytest.mark.asyncio
class TestChooseAngleAcceptsReusable:
    async def test_choose_from_reusable_status_succeeds(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _request_row(status="reusable", goal="promotion")
        conn.execute.side_effect = ["UPDATE 1", "UPDATE 2", "UPDATE 1"]
        pool = _make_pool(conn)

        with patch.object(service, "fetch_request", new=AsyncMock(return_value={"status": "approved"})):
            result = await service.choose_angle(TENANT_ID, REQUEST_ID, 2, pool)

        assert result["status"] == "approved"
        # lands back on 'approved' regardless of whether it came from pending_choice or reusable
        status_update = [c for c in conn.execute.call_args_list if "SET status = 'approved'" in c[0][0]]
        assert len(status_update) == 1

    async def test_choose_from_approved_status_still_rejected(self):
        """'approved' itself is not a valid source status — only 'pending_choice' (first pick)
        or 'reusable' (re-pick, via reopen_request() above) can call choose_angle()."""
        conn = AsyncMock()
        conn.fetchrow.return_value = _request_row(status="approved")
        pool = _make_pool(conn)
        with pytest.raises(service.WrongStatusError):
            await service.choose_angle(TENANT_ID, REQUEST_ID, 0, pool)


# ── Full reopen -> re-choose -> approved cycle, real state mutation ─────────

class _StatefulReopenConn:
    """Same spirit as test_aa449_angle_gate_service.py's _StatefulAngleOptionConn, extended with
    a real 'reusable' transition (reopen_request()'s own UPDATE) so this test exercises the ACTUAL
    guard/status logic end-to-end, not a bypass."""

    async def fetchval(self, *args, **kwargs):
        # AA-764 — fetch_request() reads the latest t8_angles job status; none in these tests.
        return None

    def __init__(self):
        self.options = {0: True, 1: False, 2: False}  # idx 0 chosen from the "first" pick
        self.request_status = "approved"

    async def fetchrow(self, query, *args):
        if "angle_gate_request" in query:
            return _request_row(status=self.request_status, goal="promotion")
        return None

    async def fetch(self, query, *args):
        return [_option_row(i, chosen=self.options[i]) for i in sorted(self.options)]

    async def execute(self, query, *args):
        if "SET status = 'reusable'" in query:
            self.request_status = "reusable"
            return "UPDATE 1"
        if "SET chosen = true" in query:
            _, idx = args
            if idx not in self.options:
                return "UPDATE 0"
            self.options[idx] = True
            return "UPDATE 1"
        if "SET chosen = false" in query:
            _, idx = args
            n = 0
            for i in self.options:
                if i != idx:
                    self.options[i] = False
                    n += 1
            return f"UPDATE {n}"
        if "SET status = 'approved'" in query:
            self.request_status = "approved"
            return "UPDATE 1"
        return "UPDATE 0"


@pytest.mark.asyncio
async def test_full_reopen_reselect_cycle_lands_on_approved_with_new_choice():
    conn = _StatefulReopenConn()
    pool = _make_pool(conn)

    assert conn.request_status == "approved"
    assert conn.options == {0: True, 1: False, 2: False}

    reopened = await service.reopen_request(TENANT_ID, REQUEST_ID, pool)
    assert reopened["status"] == "reusable"
    assert conn.request_status == "reusable"

    result = await service.choose_angle(TENANT_ID, REQUEST_ID, 2, pool)

    assert result["status"] == "approved"
    assert conn.request_status == "approved"
    assert conn.options == {0: False, 1: False, 2: True}, "the re-selected option must be the only one chosen=true"


# ── fetch_request() now returns option_id per angle (needed by T9's content_writing) ─

@pytest.mark.asyncio
async def test_fetch_request_includes_option_id_per_angle():
    option_id = uuid.uuid4()
    conn = AsyncMock()
    conn.fetchrow.return_value = _request_row(status="approved", goal="promotion")
    conn.fetch.return_value = [{**_option_row(0, chosen=True), "option_id": option_id}]
    pool = _make_pool(conn)

    result = await service.fetch_request(TENANT_ID, REQUEST_ID, pool)

    assert result["angles"][0]["option_id"] == option_id
    fetch_sql = conn.fetch.call_args.args[0]
    assert "option_id" in fetch_sql
