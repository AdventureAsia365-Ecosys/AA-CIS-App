"""AA-1016 UI / AA-900 UI / AA-804 F5 (S262 slice C4) — the SQL of the tenant AI budget, the Experiments read and the
Budgets tab actor names against real Postgres (migrations applied by conftest).

- The tenant month spend (tenant_ai_budget.MONTH_SPEND_SQL) counts only that tenant's rows of the month, shadow rows
  out, both the ledger v2 ``is_shadow`` flag and the older ``quality_signal`` flag.
- The actor names (ai_gateway.ACTOR_NAMES_SQL) resolve an admin_users id to its display name, else its username.
- The evaluation runs read (ai_gateway_experiments.EVAL_RUNS_SQL) runs on the real schema.
- With migration 321 (shared.tenant_ai_budget): PUT / DELETE write the row and one audit row each; the tier and the
  one-tenant read show it. Before 321 the reads say not ready and the writes raise TenantBudgetUnavailable.

Fixture rows live in July 2024 or under invented ids and are deleted at the end. Names are invented.
"""
import os
import sys
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _audit_cleanup import delete_audit_rows  # noqa: E402  (audit_log is append-only, CIS 248)
from api.services import ai_gateway as gw  # noqa: E402
from api.services import ai_gateway_experiments as exp  # noqa: E402
from api.services import tenant_ai_budget as tb  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
TENANT = "5e0d6c1a-2b3c-4d5e-8f60-aa1016000001"
OTHER = "5e0d6c1a-2b3c-4d5e-8f60-aa1016000002"
STAGE = "aa1016_probe_stage"
NOW = datetime(2024, 7, 20, 12, tzinfo=timezone.utc)


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    async with p.acquire() as c:
        for tid, slug in ((TENANT, "aa1016-lotus"), (OTHER, "aa1016-river")):
            await c.execute(
                "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week) "
                "VALUES ($1::uuid, $2, $3, 'starter', 3) "
                "ON CONFLICT DO NOTHING", tid, f"Probe {slug}", slug)
    yield p
    async with p.acquire() as c:
        await c.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)
        await delete_audit_rows(c, "resource_type = $1 AND resource_id = ANY($2::text[])", tb.AUDIT_RESOURCE,
                                [TENANT, OTHER])
        if await c.fetchval("SELECT to_regclass('shared.tenant_ai_budget') IS NOT NULL"):
            await c.execute("DELETE FROM shared.tenant_ai_budget WHERE tenant_id = ANY($1::uuid[])", [TENANT, OTHER])
        await c.execute("DELETE FROM shared.tenants WHERE tenant_id = ANY($1::uuid[])", [TENANT, OTHER])
        await c.execute("DELETE FROM shared.admin_users WHERE username LIKE 'aa1016-%'")
    await p.close()


async def _llm(c, tenant, at, cost, signal="{}", is_shadow=None):
    await c.execute(
        """INSERT INTO shared.llm_call_log (tenant_id, stage, role, model, cost_usd, quality_signal, created_at,
                                            is_shadow)
           VALUES ($1::uuid, $2, 'writer', 'probe-model', $3, $4::jsonb, $5, $6)""",
        tenant, STAGE, cost, signal, at, is_shadow)


@pytest.mark.asyncio
async def test_month_spend_counts_the_tenant_month_without_shadow(pool):
    async with pool.acquire() as c:
        await _llm(c, TENANT, _utc(2024, 7, 2), 1.25)
        await _llm(c, TENANT, _utc(2024, 7, 19), 0.75)
        await _llm(c, TENANT, _utc(2024, 7, 19), 9.0, is_shadow=True)                 # ledger v2 shadow flag
        await _llm(c, TENANT, _utc(2024, 7, 19), 9.0, signal='{"shadow": true}')       # older shadow flag
        await _llm(c, TENANT, _utc(2024, 6, 30, 23, 59), 9.0)                          # last month
        await _llm(c, OTHER, _utc(2024, 7, 5), 4.0)
        start, end = tb.month_window(NOW)
        rows = {r["tenant_id"]: (r["cost_usd"], r["calls"]) for r in await c.fetch(tb.MONTH_SPEND_SQL, [TENANT],
                                                                                     start, end)}
    assert rows == {TENANT: (2.0, 2)}


@pytest.mark.asyncio
async def test_actor_names_resolve_display_name_then_username(pool):
    a, b = uuid.uuid4(), uuid.uuid4()
    async with pool.acquire() as c:
        await c.execute("INSERT INTO shared.admin_users (id, username, role, is_active, display_name) "
                        "VALUES ($1, 'aa1016-named', 'admin'::shared.admin_role, true, 'Probe Person')", a)
        await c.execute("INSERT INTO shared.admin_users (id, username, role, is_active) "
                        "VALUES ($1, 'aa1016-plain', 'admin'::shared.admin_role, true)", b)
        names = await gw.actor_names(c, [str(a).upper(), str(b), "migration-226", None])
    assert names == {str(a): "Probe Person", str(b): "aa1016-plain"}


@pytest.mark.asyncio
async def test_eval_runs_and_prompt_register_reads_run(pool):
    out = await exp.read(pool)
    assert out["eval_runs"]["ready"] is True and out["eval_runs"]["error"] is None
    has_321 = await pool.fetchval("SELECT to_regclass('shared.prompt_version') IS NOT NULL")
    assert out["prompt_versions"]["ready"] is bool(has_321) and out["prompt_versions"]["error"] is None


@pytest.mark.asyncio
async def test_tenant_budget_write_read_and_clear(pool):
    if not await pool.fetchval("SELECT to_regclass('shared.tenant_ai_budget') IS NOT NULL"):
        # Before migration 321 (it merges with the registry v2 PR): reads not ready, writes refused.
        assert (await tb.read_tier(pool, now=NOW)) == {"ready": False, "rows": []}
        assert (await tb.read_one(pool, TENANT, now=NOW))["ready"] is False
        with pytest.raises(tb.TenantBudgetUnavailable):
            await tb.upsert(pool, TENANT, {"monthly_usd": 5.0}, actor="admin:probe")
        with pytest.raises(tb.TenantBudgetUnavailable):
            await tb.clear(pool, TENANT, actor="admin:probe")
        return
    async with pool.acquire() as c:
        await _llm(c, TENANT, _utc(2024, 7, 10), 4.0)
    out = await tb.upsert(pool, TENANT, {"monthly_usd": 5.0, "alert_pct": 50, "approval_pct": 100,
                                         "hard_stop": True, "notes": "probe"}, actor="admin:probe")
    assert out["created"] is True
    one = await tb.read_one(pool, TENANT, now=NOW)
    assert one["ready"] and one["budget"]["monthly_usd"] == 5.0 and one["budget"]["state"] == "alert"
    assert one["budget"]["used_pct"] == 80.0 and one["budget"]["updated_by"] == "admin:probe"
    tier = await tb.read_tier(pool, now=NOW)
    assert [r["tenant_id"] for r in tier["rows"] if r["tenant_id"] in (TENANT, OTHER)] == [TENANT]
    assert (await tb.upsert(pool, TENANT, {"monthly_usd": None}, actor="admin:probe"))["created"] is False
    assert (await tb.clear(pool, TENANT, actor="admin:probe")) == {"tenant_id": TENANT, "deleted": True}
    assert (await tb.clear(pool, TENANT, actor="admin:probe"))["deleted"] is False
    assert (await tb.read_one(pool, TENANT, now=NOW))["budget"] is None
    actions = [r["action"] for r in await pool.fetch(
        "SELECT action FROM acp_shared.audit_log WHERE resource_type = $1 AND resource_id = $2 ORDER BY created_at",
        tb.AUDIT_RESOURCE, TENANT)]
    assert actions == ["tenant_ai_budget_set", "tenant_ai_budget_set", "tenant_ai_budget_cleared"]
    with pytest.raises(LookupError):
        await tb.upsert(pool, str(uuid.uuid4()), {}, actor="admin:probe")
