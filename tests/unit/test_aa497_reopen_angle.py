"""AA-497 (AA-494 Decision 3) — angle_gate_request gains 'reusable' (already live, migration
124), the tenant-triggered reopen_request() action, choose_angle()'s widened guard, and the
content_piece angle_gate_option_id wiring needed for a second real T9 write to work.

Reuses test_aa449_angle_gate_service.py's pool/fixture helpers (same mocking shape, no need to
duplicate) — this file only covers the NEW behavior, not re-testing choose_angle()'s existing
happy path or set_goal_and_generate()/create_request() (already covered there).
"""
import uuid
from unittest.mock import AsyncMock

import pytest

from services.acp_angle_gate import service
from tests.unit.test_aa449_angle_gate_service import (
    OTHER_TENANT_ID, REQUEST_ID, TENANT_ID, _make_pool, _option_row, _request_row, _StatefulAngleOptionConn,
)


# ── reopen_request() ─────────────────────────────────────────────────────────
# AA-859 W3 — reopen_request(conn, tenant_id, request_id) runs on the caller's connection, in the
# caller's transaction: one conditional UPDATE (RETURNING), and on 0 rows a status re-read that tells
# 404 from 409. The response (fetch_request) is the router's, after commit.

def _reopen_conn(returned, status=None):
    conn = AsyncMock()
    conn.fetchval.side_effect = [returned, status]
    return conn


@pytest.mark.asyncio
class TestReopenRequest:
    async def test_happy_path_approved_to_reusable(self):
        conn = _reopen_conn(REQUEST_ID)
        assert await service.reopen_request(conn, TENANT_ID, REQUEST_ID) is None
        assert conn.fetchval.call_args_list[0][0] == (service._REOPEN_SQL, REQUEST_ID, TENANT_ID)
        assert conn.fetchval.await_count == 1
        assert "SET status = 'reusable'" in service._REOPEN_SQL
        assert "status = 'approved'" in service._REOPEN_SQL, "only an approved request is reopened"
        assert "tenant_id = $2::uuid" in service._REOPEN_SQL

    @pytest.mark.parametrize("status", ["pending_goal", "pending_choice", "reusable"])
    async def test_wrong_status_raises(self, status):
        conn = _reopen_conn(None, status)
        with pytest.raises(service.WrongStatusError, match=repr(status)):
            await service.reopen_request(conn, TENANT_ID, REQUEST_ID)
        assert conn.fetchval.call_args_list[1][0] == (service._REQUEST_STATUS_SQL, REQUEST_ID, TENANT_ID)
        conn.execute.assert_not_called()

    async def test_cross_tenant_request_not_found(self):
        conn = _reopen_conn(None, None)
        with pytest.raises(service.RequestNotFoundError):
            await service.reopen_request(conn, OTHER_TENANT_ID, REQUEST_ID)
        assert conn.fetchval.call_args_list[1][0][2] == OTHER_TENANT_ID


# ── choose_angle() widened guard ─────────────────────────────────────────────

@pytest.mark.asyncio
class TestChooseAngleAcceptsReusable:
    async def test_choose_from_reusable_status_succeeds(self):
        conn = AsyncMock()
        conn.fetchval.side_effect = ["reusable", 1]
        conn.execute.side_effect = ["UPDATE 3", "UPDATE 1"]

        await service.choose_angle(conn, TENANT_ID, REQUEST_ID, 2)

        # lands back on 'approved' regardless of whether it came from pending_choice or reusable
        status_update = [c for c in conn.execute.call_args_list if c[0][0] == service._REQUEST_APPROVE_SQL]
        assert len(status_update) == 1
        assert "SET status = 'approved'" in service._REQUEST_APPROVE_SQL
        assert "status IN ('pending_choice', 'reusable')" in service._REQUEST_APPROVE_SQL

    async def test_choose_from_approved_status_still_rejected(self):
        """'approved' itself is not a valid source status — only 'pending_choice' (first pick)
        or 'reusable' (re-pick, via reopen_request() above) can call choose_angle()."""
        conn = AsyncMock()
        conn.fetchval.side_effect = ["approved"]
        with pytest.raises(service.WrongStatusError):
            await service.choose_angle(conn, TENANT_ID, REQUEST_ID, 0)
        conn.execute.assert_not_called()


# ── Full reopen -> re-choose -> approved cycle, real state mutation ─────────

class _StatefulReopenConn(_StatefulAngleOptionConn):
    """test_aa449_angle_gate_service.py's _StatefulAngleOptionConn (which also implements reopen's
    conditional UPDATE), starting from an approved request whose idx 0 was the first pick, so this test
    exercises the ACTUAL guard/status logic end-to-end, not a bypass."""

    def __init__(self):
        super().__init__()
        self.options = {0: True, 1: False, 2: False}  # idx 0 chosen from the "first" pick
        self.request_status = "approved"


@pytest.mark.asyncio
async def test_full_reopen_reselect_cycle_lands_on_approved_with_new_choice():
    conn = _StatefulReopenConn()

    assert conn.request_status == "approved"
    assert conn.options == {0: True, 1: False, 2: False}

    await service.reopen_request(conn, TENANT_ID, REQUEST_ID)
    assert conn.request_status == "reusable"

    with pytest.raises(service.WrongStatusError):  # a double click: the second reopen is a 409
        await service.reopen_request(conn, TENANT_ID, REQUEST_ID)

    await service.choose_angle(conn, TENANT_ID, REQUEST_ID, 2)

    assert conn.request_status == "approved"
    assert conn.options == {0: False, 1: False, 2: True}, "the re-selected option must be the only one chosen=true"

    reread = await service.fetch_request(TENANT_ID, REQUEST_ID, _make_pool(conn))
    assert reread["status"] == "approved"
    assert [a["chosen"] for a in reread["angles"]] == [False, False, True]


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
