"""AA-1127 (S1, S283 UI audit) — one ledger for tour rewrites.

The Content studio dashboard said "Rewrites used 2 / 200" (GET /v1/billing, v_tenant_monthly_usage.tours_rewritten) while
the Billing and Marketplace meters said 0 used (GET /v1/quota, shared.tenant_rewrite_usage). Since CIS 452 the view
reads the counter; these tests pin the code side: GET /v1/quota reads the counter for the month key trigger_rewrite
writes, the counter is written only there, and the dashboard's per-day rewrite line leaves a tenant's own edits out.
The view half is tests/integration/test_aa1127_migration_452.py (migration PR).
"""
import inspect
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routers import v1_tours
from api.services import rewrite_ledger

pytestmark = pytest.mark.usefixtures("seeded_plans")


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


def test_rewrite_sources_leave_tenant_edits_out():
    assert rewrite_ledger.REWRITE_EDIT_SOURCES == ("ai_generated", "re_rewrite")
    assert rewrite_ledger.REWRITE_EDIT_SOURCES_SQL == "('ai_generated', 're_rewrite')"
    assert "tenant_edit" not in rewrite_ledger.REWRITE_EDIT_SOURCES_SQL


def test_the_counter_is_written_only_by_the_rewrite_quota_helper():
    src = inspect.getsource(v1_tours)
    writes = [m.start() for m in re.finditer(r"INSERT INTO shared\.tenant_rewrite_usage", src)]
    assert len(writes) == 1
    helper = inspect.getsource(v1_tours._check_and_consume_rewrite_quota)
    assert "INSERT INTO shared.tenant_rewrite_usage" in helper
    assert "_check_and_consume_rewrite_quota(conn, tenant_id)" in inspect.getsource(v1_tours.trigger_rewrite)


@pytest.mark.asyncio
async def test_quota_reads_the_counter_for_the_month_key_the_rewrite_writes():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"plan": "growth", "quota": 200})
    conn.fetchval = AsyncMock(return_value=2)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = False
    request = MagicMock()
    request.app.state.pool = pool
    out = await v1_tours.get_quota(request, tenant={"sub": "tenant-1"})
    sql, tenant_id, month = conn.fetchval.await_args.args
    assert "FROM shared.tenant_rewrite_usage" in sql and tenant_id == "tenant-1"
    assert month == rewrite_ledger.current_year_month_and_reset()[0]
    assert (out["rewrites_used"], out["rewrites_remaining"], out["tours_per_month"]) == (2, 198, 200)


@pytest.mark.asyncio
async def test_dashboard_daily_rewrites_count_only_rewrite_versions():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(side_effect=[[], [], [], []])
    conn.fetchval = AsyncMock(return_value=300)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = False
    request = MagicMock()
    request.app.state.pool = pool
    await v1_tours.get_my_billing(request, tenant={"sub": "tenant-1"})
    daily = next(_norm(c.args[0]) for c in conn.fetch.await_args_list if "generate_series" in c.args[0])
    rw = daily[daily.index("rw AS ("):daily.index("SELECT days.day")]
    assert "edit_source IN ('ai_generated', 're_rewrite')" in rw
