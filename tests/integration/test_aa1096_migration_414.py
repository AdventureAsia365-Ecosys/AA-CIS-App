"""S279 thresholds lane L5 (AA-1096) — CIS 414: the 17 TripPlanner settings in shared.setting_definition and the
TripPlanner daily AI cap on its app profile (conftest replays 345, 407 and 414). Registered; the rows equal the compiled
defaults the CIS reader serves (shared/settings/compiled_defaults.json); the profile budget is 5.0 and a re-run keeps
an administrator's value; the definition guard still refuses a write by cis_app. Runs in a transaction that is rolled
back."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M414 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "414_tripplanner_settings_registry.sql")


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


def _body() -> str:
    return open(M414, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]


@pytest.mark.asyncio
async def test_414_registered_and_the_rows_are_the_compiled_defaults(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '414'") == "414"
    rows = await conn.fetch("SELECT key, default_value::text AS d, scope_kind, enforced, env_override "
                            "FROM shared.setting_definition WHERE app = 'tripplanner'")
    compiled = [k for k in settings.READER.keys() if settings.definition(k).app == "tripplanner"]
    assert sorted(r["key"] for r in rows) == sorted(compiled) and len(rows) == 17
    for r in rows:
        d = settings.definition(r["key"])
        assert json.loads(r["d"]) == d.default_value and r["scope_kind"] == d.scope_kind
        assert r["enforced"] is True and r["env_override"] is None


@pytest.mark.asyncio
async def test_the_daily_cap_is_on_the_app_profile(conn):
    budget = await conn.fetchval("SELECT default_daily_budget_usd::float8 AS ok FROM shared.llm_app_profile "
                                 "WHERE app = 'tripplanner'")
    assert budget == 5.0


@pytest.mark.asyncio
async def test_rerun_changes_nothing_and_keeps_an_administrators_budget(conn):
    await conn.execute("UPDATE shared.llm_app_profile SET default_daily_budget_usd = 3.5, updated_by = 'admin:zz' "
                       "WHERE app = 'tripplanner'")
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(_body())
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
    row = await conn.fetchrow("SELECT default_daily_budget_usd::float8 AS b, updated_by FROM shared.llm_app_profile "
                              "WHERE app = 'tripplanner'")
    assert (row["b"], row["updated_by"]) == (3.5, "admin:zz")


@pytest.mark.asyncio
async def test_a_tenant_value_only_for_the_two_tenant_keys(conn):
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO shared.setting_value (key, tenant_id, value, version, proposed_by, reason) VALUES "
                "('tripplanner.rate.narrate_per_hour', '00000000-0000-0000-0000-000000000002'::uuid, '12'::jsonb, 1, "
                "'admin:zz', 'zz test: a reason of at least twenty characters')")
    with pytest.raises(asyncpg.exceptions.CheckViolationError):            # bounds 3..7 (DM-7.1)
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO shared.setting_value (key, value, version, proposed_by, reason) VALUES "
                "('tripplanner.leads.hold_days', '8'::jsonb, 1, 'admin:zz', "
                "'zz test: a reason of at least twenty characters')")
