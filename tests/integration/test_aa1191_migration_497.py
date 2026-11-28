"""AA-1191 — CIS 497 on a real Postgres (conftest replays every migration): the Enabled currencies key equals the
compiled default with a complete guide, a value outside the allowed currencies is refused, the value in force reaches
shared.v_setting_billing_effective (the SQL readers, CIS 498) and a re-run of 497 changes nothing.
Runs in a transaction that is rolled back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import settings  # noqa: E402
from services.tenant_account import currency  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M497 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "497_aa1191_enabled_currencies_setting.sql")
KEY = currency.ENABLED_CURRENCIES
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
ROW = ("SELECT default_value::text AS d, value_type, allowed_values::text AS allowed, area, scope_kind, impact, "
       "approval, evidence_required, env_override, enforced, label, description, "
       "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1")


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


def test_the_reader_serves_usd_only_by_default():
    assert currency.enabled() == ("USD",)
    assert settings.definition(KEY).default_value == ["USD"]


@pytest.mark.asyncio
async def test_497_registered(conn):
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.schema_versions WHERE version = '497'") == 1


@pytest.mark.asyncio
async def test_497_key_is_the_compiled_default(conn):
    row = await conn.fetchrow(ROW, KEY)
    assert row is not None
    d = settings.definition(KEY)
    assert json.loads(row["d"]) == ["USD"] == d.default_value
    assert row["value_type"] == "list" and json.loads(row["allowed"]) == ["USD", "VND"]
    assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
        "billing", "platform", "high", "maker_checker")
    assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
    assert row["guided"] is True and row["label"] == d.label == "Enabled currencies"
    assert row["description"] == d.description


@pytest.mark.asyncio
async def test_a_currency_outside_the_allowed_list_is_refused(conn):
    for bad in ('["EUR"]', '"USD"', '["USD", 5]'):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INS, KEY, bad)
    assert await conn.fetchval(INS, KEY, '["USD", "VND"]') is not None


@pytest.mark.asyncio
async def test_the_value_in_force_reaches_the_billing_view(conn):
    got = await conn.fetchval("SELECT value::text AS v FROM shared.v_setting_billing_effective WHERE key = $1", KEY)
    assert json.loads(got) == ["USD"]
    vid = await conn.fetchval(INS, KEY, '["USD", "VND"]')
    await conn.execute("UPDATE shared.setting_value SET status = 'approved', decided_by = 'admin:zz-b', "
                       "decided_at = greatest(now(), proposed_at) WHERE id = $1", vid)
    got = await conn.fetchval("SELECT value::text AS v FROM shared.v_setting_billing_effective WHERE key = $1", KEY)
    assert json.loads(got) == ["USD", "VND"]


@pytest.mark.asyncio
async def test_a_rerun_changes_nothing(conn):
    before = await conn.fetchval("SELECT row_to_json(d)::text AS r FROM shared.setting_definition d WHERE key = $1",
                                 KEY)
    sql = open(M497).read().replace("BEGIN;", "").replace("COMMIT;", "")
    await conn.execute(sql)
    after = await conn.fetchval("SELECT row_to_json(d)::text AS r FROM shared.setting_definition d WHERE key = $1",
                                KEY)
    assert before == after
