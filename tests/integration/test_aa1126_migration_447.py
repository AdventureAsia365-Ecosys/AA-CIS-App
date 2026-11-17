"""AA-1126 l16 (S283) — CIS 447: the two spend environment keys (cis / tripplanner) in shared.setting_definition, the
tenant AI allowance notices (one per tenant, kind and month; TripPlanner may only INSERT its own kind) and the CRM agency
AI budget per plan (crm.ai_budget_usd_monthly, at today's CRM default). conftest replays every migration. Runs in a
transaction that is rolled back; invented ids and values only."""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M447 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "447_aa1126_spend_environment_and_quota_notices.sql")
KEYS = ("cis.spend.environment", "tripplanner.spend.environment")
CAP_KEYS = {"tripplanner.spend.stage_cap_compose_usd": 0, "tripplanner.spend.stage_cap_search_usd": 0,
            "tripplanner.spend.near_cap_pct": 80}
TENANT = "00000000-0000-4000-8000-0000000a1126"
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
NOTICE = ("INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, payload, "
          "target_roles) VALUES ($1::uuid, 'system', $2, $3, $4, '{}'::jsonb, ARRAY['tenant'])")


@pytest_asyncio.fixture
async def conn():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        yield c
    finally:
        await tr.rollback()
        await c.close()


@pytest.mark.asyncio
async def test_447_registered_and_each_key_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '447'") == "447"
    for key in KEYS:
        row = await conn.fetchrow(
            "SELECT app, default_value::text AS d, allowed_values::text AS allowed, area, scope_kind, impact, approval, "
            "evidence_required, env_override, enforced, label, shared.setting_guide_complete(guide) AS guided "
            "FROM shared.setting_definition WHERE key = $1", key)
        assert row is not None, key
        assert row["app"] == key.split(".")[0]
        assert json.loads(row["d"]) == "dev"
        assert json.loads(row["allowed"]) == ["dev", "prod"]
        assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == ("spend", "platform", "high",
                                                                                     "maker_checker")
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True
    # the CIS key is the compiled default the reader serves when the database cannot be read
    d = settings.definition("cis.spend.environment")
    assert d.default_value == "dev" and d.label


@pytest.mark.asyncio
async def test_the_tripplanner_cap_keys_are_registered_with_their_defaults(conn):
    for key, default in CAP_KEYS.items():
        row = await conn.fetchrow("SELECT app, default_value::text AS d, enforced, "
                                  "shared.setting_guide_complete(guide) AS guided "
                                  "FROM shared.setting_definition WHERE key = $1", key)
        assert row is not None and row["app"] == "tripplanner", key
        assert json.loads(row["d"]) == default and row["enforced"] is True and row["guided"] is True


@pytest.mark.asyncio
async def test_only_dev_or_prod(conn):
    for key in KEYS:
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INS, key, '"staging"')
        assert await conn.fetchval(INS, key, '"prod"') is not None


@pytest.mark.asyncio
async def test_one_allowance_notice_per_tenant_kind_and_month(conn):
    await conn.execute(NOTICE, TENANT, "ai.allowance_used", "ai_allowance", "content:2026-11")
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        async with conn.transaction():
            await conn.execute(NOTICE, TENANT, "ai.allowance_used", "ai_allowance", "content:2026-11")
    await conn.execute(NOTICE, TENANT, "ai.allowance_used", "ai_allowance", "content:2026-12")   # next month
    # other event types keep writing as many rows as they like
    for _ in range(2):
        await conn.execute(NOTICE, TENANT, "tour.zz_test", "tour", "zz-1")


@pytest.mark.asyncio
async def test_tripplanner_inserts_its_own_notice_only(conn):
    for priv in ("SELECT", "UPDATE", "DELETE"):
        assert await conn.fetchval("SELECT has_table_privilege('tripplanner', 'shared.notifications', $1) AS ok",
                                   priv) is False
    assert await conn.fetchval("SELECT has_table_privilege('tripplanner', 'shared.notifications', 'INSERT') AS ok")
    await conn.execute("SET LOCAL ROLE tripplanner")
    await conn.execute(NOTICE, TENANT, "tripplanner.ai_allowance_used", "tripplanner_ai", "tripplanner:2026-11")
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):          # the plain INSERT's "already noticed"
        async with conn.transaction():
            await conn.execute(NOTICE, TENANT, "tripplanner.ai_allowance_used", "tripplanner_ai",
                               "tripplanner:2026-11")
    with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):    # another kind: refused by the policy
        async with conn.transaction():
            await conn.execute(NOTICE, TENANT, "ai.allowance_used", "ai_allowance", "content:2026-11")
    await conn.execute("RESET ROLE")


PLATFORM = "00000000-0000-0000-0000-000000000001"
DEGRADED = ("INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, payload, "
            "target_roles) VALUES ($1::uuid, 'system', 'platform.tripplanner.spend_degraded', 'tripplanner_ai', $2, "
            "'{}'::jsonb, $3::text[])")


@pytest.mark.asyncio
async def test_tripplanner_raises_one_degraded_bell_per_check_and_day(conn):
    """AA-1126 L4: the staff bell of a degraded TripPlanner spend check, admin-only, on the AA internal tenant."""
    await conn.execute("SET LOCAL ROLE tripplanner")
    await conn.execute(DEGRADED, PLATFORM, "spend_unreadable:2026-11-17", ["admin"])
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):          # once per check and UTC day
        async with conn.transaction():
            await conn.execute(DEGRADED, PLATFORM, "spend_unreadable:2026-11-17", ["admin"])
    await conn.execute(DEGRADED, PLATFORM, "spend_unreadable:2026-11-18", ["admin"])        # the next day
    await conn.execute(DEGRADED, PLATFORM, "tenant_sessions:2026-11-17", ["admin"])         # another check
    for tenant_id, roles in ((TENANT, ["admin"]), (PLATFORM, ["tenant"]), (PLATFORM, ["content"])):
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):   # a tenant's row or a wider audience
            async with conn.transaction():
                await conn.execute(DEGRADED, tenant_id, "zz:2026-11-17", roles)
    await conn.execute("RESET ROLE")


@pytest.mark.asyncio
async def test_crm_plans_carry_the_ai_budget_at_todays_default(conn):
    row = await conn.fetchrow("SELECT product, value_kind, unit FROM shared.entitlement_feature "
                              "WHERE feature_key = 'crm.ai_budget_usd_monthly'")
    assert tuple(row) == ("crm", "limit", "usd_month")
    rows = await conn.fetch("SELECT p.plan_key, f.limit_value::float AS lim FROM shared.plan p "
                            "JOIN shared.plan_feature f ON f.plan_key = p.plan_key "
                            "AND f.feature_key = 'crm.ai_budget_usd_monthly' "
                            "WHERE p.product = 'crm' AND p.retired_at IS NULL")
    assert rows and all(r["lim"] == 2.0 for r in rows)
    live = await conn.fetchval("SELECT count(*) AS n FROM shared.plan WHERE product = 'crm' AND retired_at IS NULL")
    assert len(rows) == live


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M447, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    plans = await conn.fetchval("SELECT count(*) AS n FROM shared.plan_feature")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.plan_feature") == plans
    assert uuid.UUID(TENANT)
