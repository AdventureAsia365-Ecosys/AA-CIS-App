"""
tests/unit/test_aa615_gate_telemetry.py — AA-615: GET /admin/a4/gate-telemetry.

Per-tenant/channel gate/severity/retry/publish telemetry — the admin-only signal the tenant
never sees (their view is flat ready_state, AA-613). Mocks asyncpg via pool.acquire(), same
convention as test_aa560_platform_stats.py.

Handler conn.fetch call order: by_tenant_channel (0), top_gate_failures (1), publish (2),
export (3). No fetchval calls.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

_TEST_SECRET = "test-admin-secret"


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _TEST_SECRET)


def _make_pool(fetch_side_effect=None):
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=fetch_side_effect)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _make_request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    return req


def _row(**kwargs):
    return kwargs


@pytest.mark.asyncio
class TestGetGateTelemetry:
    async def test_requires_admin_secret(self):
        from api.routers.admin_a4 import get_gate_telemetry

        pool, _ = _make_pool()
        req = _make_request(pool)

        with pytest.raises(HTTPException) as exc_info:
            await get_gate_telemetry(req, x_admin_secret="wrong")
        assert exc_info.value.status_code == 403

    async def test_merges_tenant_channel_publish_export(self):
        from api.routers.admin_a4 import get_gate_telemetry

        pool, conn = _make_pool(fetch_side_effect=[
            # by_tenant_channel
            [_row(tenant_id="t1", tenant_name="Tenant One", channel="blog",
                  total=10, warn_count=3, held_count=2, failed_count=1, retry_count=4)],
            # top_gate_failures_by_channel
            [_row(channel="blog", gate="F1_grounding", blocking=True, fail_count=5),
             _row(channel="blog", gate="F9_brand_voice", blocking=False, fail_count=2)],
            # publish
            [_row(tenant_id="t1", channel="blog", publish_count=6)],
            # export
            [_row(tenant_id="t1", channel="blog", export_count=7)],
        ])
        req = _make_request(pool)

        result = await get_gate_telemetry(req, x_admin_secret=_TEST_SECRET)

        data = result["data"]
        assert len(data["by_tenant_channel"]) == 1
        row = data["by_tenant_channel"][0]
        assert row["tenant_id"] == "t1"
        assert row["channel"] == "blog"
        assert row["total"] == 10
        assert row["warn_count"] == 3
        assert row["held_count"] == 2
        assert row["failed_count"] == 1
        assert row["retry_count"] == 4
        # publish/export folded in by (tenant_id, channel) key
        assert row["publish_count"] == 6
        assert row["export_count"] == 7

        gates = data["top_gate_failures_by_channel"]
        assert gates == [
            {"channel": "blog", "gate": "F1_grounding", "blocking": True, "fail_count": 5},
            {"channel": "blog", "gate": "F9_brand_voice", "blocking": False, "fail_count": 2},
        ]

    async def test_publish_only_channel_still_gets_a_row(self):
        """A publish/export whose piece was written outside the content_piece window must still
        surface — the merge keys on (tenant_id, channel) and creates a row if none exists."""
        from api.routers.admin_a4 import get_gate_telemetry

        pool, conn = _make_pool(fetch_side_effect=[
            [],  # no content_piece rows in window
            [],  # no gate failures
            [_row(tenant_id="t9", channel="facebook", publish_count=3)],
            [],  # no exports
        ])
        req = _make_request(pool)

        result = await get_gate_telemetry(req, x_admin_secret=_TEST_SECRET)

        rows = result["data"]["by_tenant_channel"]
        assert len(rows) == 1
        assert rows[0]["tenant_id"] == "t9"
        assert rows[0]["channel"] == "facebook"
        assert rows[0]["publish_count"] == 3
        assert rows[0]["total"] == 0
        assert rows[0]["warn_count"] == 0

    async def test_empty_window_returns_empty_lists(self):
        from api.routers.admin_a4 import get_gate_telemetry

        pool, _ = _make_pool(fetch_side_effect=[[], [], [], []])
        req = _make_request(pool)

        result = await get_gate_telemetry(req, x_admin_secret=_TEST_SECRET)

        assert result["data"]["by_tenant_channel"] == []
        assert result["data"]["top_gate_failures_by_channel"] == []

    async def test_warn_uses_flags_array_length_and_severity_from_status(self):
        """Regression guard for the AA-613 severity model: warn = approved + non-empty flags,
        held = status='held'. The SQL must count them separately, server-side."""
        from api.routers.admin_a4 import get_gate_telemetry

        pool, conn = _make_pool(fetch_side_effect=[[], [], [], []])
        req = _make_request(pool)

        await get_gate_telemetry(req, x_admin_secret=_TEST_SECRET)

        by_tc_sql = conn.fetch.call_args_list[0].args[0]
        assert "jsonb_array_length(cp.flags)" in by_tc_sql
        assert "cp.status = 'approved'" in by_tc_sql
        assert "FILTER (WHERE cp.status = 'held')" in by_tc_sql
        assert "cp.attempt_number >= 2" in by_tc_sql
        # gate-failure query unnests server-side, same as platform-stats
        top_gate_sql = conn.fetch.call_args_list[1].args[0]
        assert "jsonb_array_elements(cp.gate_ledger)" in top_gate_sql

    async def test_date_filters_scope_all_three_windows(self):
        from api.routers.admin_a4 import get_gate_telemetry

        pool, conn = _make_pool(fetch_side_effect=[[], [], [], []])
        req = _make_request(pool)

        await get_gate_telemetry(
            req, date_from="2026-09-01", date_to="2026-09-19", x_admin_secret=_TEST_SECRET,
        )

        # content_piece window (by_tc + top_gate) binds created_at
        by_tc_sql = conn.fetch.call_args_list[0].args[0]
        assert "cp.created_at >=" in by_tc_sql and "cp.created_at <" in by_tc_sql
        # publish window binds published_at
        pub_sql = conn.fetch.call_args_list[2].args[0]
        assert "pl.published_at >=" in pub_sql
        # export window binds audit_log.created_at + filters the exported action
        exp_sql = conn.fetch.call_args_list[3].args[0]
        assert "al.action = 'content_piece.exported'" in exp_sql
        assert "al.created_at >=" in exp_sql
