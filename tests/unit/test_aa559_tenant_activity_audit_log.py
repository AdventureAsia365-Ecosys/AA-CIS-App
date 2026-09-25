"""AA-559 — semantic tenant-activity logging into `acp_shared.audit_log`.

STEP0 (docs/implementation-notes/AA-559.md) found this table already has 5 real call sites
(agency.onboard/offboard, hitl.gate3_social.*, publish_mode_transition,
trip_reallocation_suggestion) — none of them touched by this issue. This file covers the 3 new
call sites NOT already covered by tests/unit/test_aa450_content_writing_service.py (content_
piece.created/finished) or tests/unit/test_aa450_v1_content_writing.py (write.started):
  - services/acp_shared/audit_log.py — the shared write_audit_log() helper itself
  - api/main.py::tenant_login() — tenant.login (NOT api/routers/auth.py::tenant_login(), which
    is dead code — see docs/implementation-notes/AA-559.md's STEP0-correction section for why:
    api.routers.auth's own `router` object is never app.include_router()'d, only individual
    helpers/models are imported into api/main.py, which redeclares the real, live route itself
    — same pattern AA-232 already hit once for the admin-login routes, per that commit's own
    message: "fix: mount admin auth endpoints in main.py — router was never included")
  - services/acp_shared/slate.py::pick_subject() — slate.subject_picked
  - api/routers/v1_tours.py::trigger_rewrite() — tour.rewrite_triggered
"""
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import api.main as main
from api.routers import v1_tours
from services.acp_shared import slate
from services.acp_shared.audit_log import write_audit_log

TENANT_ID = uuid.uuid4()
SUBJECT_ID = uuid.uuid4()
REQUEST_ID = uuid.uuid4()


class _TxnCM:
    """conn.transaction() is a sync method returning an async context manager — AsyncMock's
    default mocks the METHOD as async, not its return value, so `async with conn.transaction():`
    breaks unless overridden this way. Same fix as test_aa449_angle_gate_service.py/
    test_aa309_tenant_onboarding.py/test_aa367_packets.py/test_aa448_v1_planning.py."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _pool_with_conn(conn, with_transaction=False):
    if with_transaction:
        conn.transaction = MagicMock(return_value=_TxnCM())
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


@pytest.mark.asyncio
class TestWriteAuditLogHelper:
    async def test_inserts_expected_columns_and_json_encodes_details(self):
        conn = AsyncMock()
        await write_audit_log(
            conn, tenant_id=TENANT_ID, actor="tenant:x", action="some.action",
            resource_type="thing", resource_id="thing-1", details={"a": 1},
        )
        conn.execute.assert_awaited_once()
        query, *params = conn.execute.call_args[0]
        assert "INSERT INTO acp_shared.audit_log" in query
        assert "(tenant_id, actor, action, resource_type, resource_id, details)" in query
        assert params == [str(TENANT_ID), "tenant:x", "some.action", "thing", "thing-1", json.dumps({"a": 1})]

    async def test_details_defaults_to_empty_object_not_null(self):
        conn = AsyncMock()
        await write_audit_log(
            conn, tenant_id="t1", actor="a", action="act", resource_type="rt", resource_id="r1",
        )
        params = conn.execute.call_args[0][1:]
        assert params[-1] == "{}"

    async def test_pool_works_the_same_as_a_connection(self):
        """Pool.execute() acquires internally — callers with only a Pool in scope (no existing
        `async with pool.acquire()` block) pass it directly, same call shape as a Connection."""
        pool = AsyncMock()
        await write_audit_log(
            pool, tenant_id="t1", actor="a", action="act", resource_type="rt", resource_id="r1",
        )
        pool.execute.assert_awaited_once()


@pytest.mark.asyncio
class TestTenantLoginAuditLog:
    """Targets api/main.py::tenant_login() — the REAL live route (api/routers/auth.py's own
    tenant_login() is dead code, see this file's module docstring)."""

    async def test_successful_login_writes_audit_row(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {
            "tenant_id": str(TENANT_ID), "name": "WanderLux", "plan_tier": "pro",
        }
        pool = _pool_with_conn(conn)
        with patch.object(main, "write_audit_log", new=AsyncMock()) as mock_audit, \
             patch.object(main, "_create_jwt", return_value="signed.jwt.token"):
            body = main.TenantLoginRequest(api_key="cis_" + "x" * 20)
            result = await main.tenant_login(body, db=pool)

        assert result.token == "signed.jwt.token"
        mock_audit.assert_awaited_once()
        kwargs = mock_audit.call_args.kwargs
        assert kwargs["tenant_id"] == str(TENANT_ID)
        assert kwargs["actor"] == f"tenant:{TENANT_ID}"
        assert kwargs["action"] == "tenant.login"
        assert kwargs["resource_type"] == "tenant"
        assert kwargs["resource_id"] == str(TENANT_ID)
        assert kwargs["details"] == {"login_method": "api_key"}

    async def test_invalid_api_key_never_logs(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = None
        pool = _pool_with_conn(conn)
        with patch.object(main, "write_audit_log", new=AsyncMock()) as mock_audit:
            body = main.TenantLoginRequest(api_key="cis_" + "x" * 20)
            with pytest.raises(HTTPException):
                await main.tenant_login(body, db=pool)
        mock_audit.assert_not_awaited()

    async def test_audit_log_failure_does_not_break_login(self):
        """AA-559 Decision — login's audit write is best-effort: a real tenant must never be
        locked out of the portal because the audit table has a transient problem."""
        conn = AsyncMock()
        conn.fetchrow.return_value = {
            "tenant_id": str(TENANT_ID), "name": "WanderLux", "plan_tier": "pro",
        }
        pool = _pool_with_conn(conn)
        with patch.object(main, "write_audit_log", new=AsyncMock(side_effect=RuntimeError("db down"))), \
             patch.object(main, "_create_jwt", return_value="signed.jwt.token"):
            body = main.TenantLoginRequest(api_key="cis_" + "x" * 20)
            result = await main.tenant_login(body, db=pool)
        assert result.token == "signed.jwt.token"


def _subject_row(state="proposed", channel="facebook", segment_id="seg1", route_id=None):
    return {
        "subject_id": SUBJECT_ID, "channel": channel, "state": state,
        "segment_id": segment_id, "route_id": route_id,
    }


def _request_row():
    return {
        "request_id": REQUEST_ID, "tenant_id": TENANT_ID, "atom_id": "atom_abc123",
        "trip_id": None, "channel": "facebook", "status": "pending_goal",
        "created_at": None, "route_segment_ids": None,
    }


@pytest.mark.asyncio
class TestPickSubjectAuditLog:
    async def test_writes_audit_row_inside_same_transaction(self):
        conn = AsyncMock()
        conn.fetchrow.side_effect = [_subject_row(), _request_row()]
        pool = _pool_with_conn(conn, with_transaction=True)

        with patch.object(slate, "_resolve_representative_atom",
                           new=AsyncMock(return_value=("atom_abc123", None, None))):
            await slate.pick_subject(TENANT_ID, SUBJECT_ID, pool, selected_by=f"tenant:{TENANT_ID}")

        # 2 execute() calls on the SAME conn: subject state UPDATE, then the audit_log INSERT —
        # both inside the one `async with conn.transaction():` block.
        assert conn.execute.await_count == 2
        query, *params = conn.execute.call_args_list[1][0]
        assert "INSERT INTO acp_shared.audit_log" in query
        assert params[0] == str(TENANT_ID)
        assert params[1] == f"tenant:{TENANT_ID}"
        assert params[2] == "slate.subject_picked"
        assert params[3] == "subject"
        assert params[4] == str(SUBJECT_ID)
        details = json.loads(params[5])
        assert details["channel"] == "facebook"
        assert details["request_id"] == str(REQUEST_ID)
        assert details["atom_id"] == "atom_abc123"

    async def test_not_eligible_subject_never_logs(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = _subject_row(state="picked")
        pool = _pool_with_conn(conn)

        with pytest.raises(slate.SubjectNotEligibleError):
            await slate.pick_subject(TENANT_ID, SUBJECT_ID, pool, selected_by=f"tenant:{TENANT_ID}")
        conn.execute.assert_not_called()

    async def test_subject_not_found_never_logs(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = None
        pool = _pool_with_conn(conn)

        with pytest.raises(slate.SubjectNotFoundError):
            await slate.pick_subject(TENANT_ID, SUBJECT_ID, pool, selected_by=f"tenant:{TENANT_ID}")
        conn.execute.assert_not_called()


# ── trigger_rewrite() (T1 Browse Pool "rewrite" action) ─────────────────────────────────────
#
# Same mocking shape as tests/unit/test_aa469_viec1_t4_t5_split.py's _drive_trigger_rewrite()
# (one shared conn returned by every pool.acquire() call, matching real single-threaded asyncio
# execution order). Short-circuited here via _rewrite_tour returning status='failed' — the audit
# row this issue adds is written in the FIRST pool.acquire() block, before _rewrite_tour is even
# imported, so nothing past that point needs exercising for this file's purposes.

REWRITE_TENANT_ID = "33333333-3333-3333-3333-333333333333"
REWRITE_TOUR_ID = "44444444-4444-4444-4444-444444444444"
REWRITE_PUBLISHED_TOUR_ID = "55555555-5555-5555-5555-555555555555"
REWRITE_VERSION_ID = "66666666-6666-6666-6666-666666666666"

REWRITE_PT_ROW = {
    "id": REWRITE_PUBLISHED_TOUR_ID, "tour_id": REWRITE_TOUR_ID, "aa_name": "Sapa Trek",
    "aa_subtitle": "Original subtitle", "aa_summary": "Original summary",
    "aa_description": "desc", "aa_highlights": [], "aa_itineraries": "",
    "seo_title": "st", "seo_meta": "sm", "seo_keywords_used": None,
    "country": "Vietnam", "duration": "3D2N",
}
REWRITE_EXISTING_SEO_ROW = {"top_keywords": "[]", "keyword_ideas": "[]", "people_also_ask": "[]"}


def _rewrite_pool_ctx(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


class _FakeRewriteRequest:
    def __init__(self, pool):
        self.app = SimpleNamespace(state=SimpleNamespace(pool=pool))


async def _drive_trigger_rewrite():
    conn = AsyncMock()
    # AA-640: plan quota is a fetchrow (plan + membership_plans quota) ahead of the others
    conn.fetchrow.side_effect = [{"plan": "starter", "quota": 50}, REWRITE_PT_ROW, None, REWRITE_EXISTING_SEO_ROW]
    conn.fetchval.side_effect = [1, 1, REWRITE_VERSION_ID]
    pool = _rewrite_pool_ctx(conn)
    request = _FakeRewriteRequest(pool)
    tenant = {"sub": REWRITE_TENANT_ID}
    body = v1_tours.RewriteRequest(rewrite_language="en-US", seo_mode="standard")

    with patch("api.routers.v1_pipeline._rewrite_tour", AsyncMock(return_value={"status": "failed"})):
        before = set(v1_tours._background_tasks)
        resp = await v1_tours.trigger_rewrite(REWRITE_PUBLISHED_TOUR_ID, body, request, tenant)
        new_tasks = v1_tours._background_tasks - before
        assert len(new_tasks) == 1
        await next(iter(new_tasks))  # drain the background task so nothing is left pending

    return resp, conn


@pytest.mark.asyncio
class TestTriggerRewriteAuditLog:
    async def test_writes_audit_row_before_background_rewrite_starts(self):
        resp, conn = await _drive_trigger_rewrite()

        assert resp["status"] == "pending"
        query, *params = conn.execute.call_args_list[0][0]
        assert "INSERT INTO acp_shared.audit_log" in query
        assert params[0] == REWRITE_TENANT_ID
        assert params[1] == f"tenant:{REWRITE_TENANT_ID}"
        assert params[2] == "tour.rewrite_triggered"
        assert params[3] == "tenant_tour_version"
        assert params[4] == REWRITE_VERSION_ID
        details = json.loads(params[5])
        assert details["published_tour_id"] == REWRITE_PUBLISHED_TOUR_ID
        assert details["version_number"] == 1
        assert details["tour_name"] == "Sapa Trek"
        assert details["rewrite_language"] == "en-US"
