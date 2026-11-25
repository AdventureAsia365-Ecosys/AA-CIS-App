"""AA-1154 (A1) — CIS 486 on a real Postgres (conftest replays every migration): the AI gateway stage ops_assist (with
the free-text scrub) and its own month cap (10 USD, hard stop) exist with the values the tech lead decided (S285 AI2 /
AI3); the four A1 settings are registered and equal the compiled defaults; a value outside its allowed set is refused;
a re-run keeps an admin's edit; the runtime role reads both rows. Runs in a transaction that is rolled back."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import ops_assist_settings as a  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M486 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "486_aa1154_ops_assist_stage_and_budget.sql")
STAGE_ROW = ("SELECT role, provider, model_id, account_route, is_active, fallback_model_ids, shadow_model_id, "
             "shadow_sample_pct, allowed_scopes, route_deadline_s, degrade_mode, pii_class, free_text "
             "FROM shared.llm_role_config WHERE stage = $1")
STAGE = "ops_assist"
SCOPE = "stage:ops_assist"
BUDGET_ROW = ("SELECT per_run_usd, per_day_usd, per_month_usd::float8 AS month, hard_stop, alert_pct "
              "FROM shared.spend_budget WHERE provider = 'llm' AND scope = $1")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
SETTING = ("SELECT default_value::text AS d, allowed_values::text AS allowed, value_type, unit, min_value::float8 AS lo, "
           "max_value::float8 AS hi, area, scope_kind, impact, approval, evidence_required, env_override, enforced, "
           "label, description, shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition "
           "WHERE key = $1")
# key: (default, value type, unit, (min, max), allowed values, impact)
EXPECTED = {
    a.AUTO_SEVERITIES: (["sev1", "sev2"], "list", None, (None, None), ["sev1", "sev2", "sev3"], "medium"),
    a.AUTO_INCIDENT_GAP_MIN: (240, "int", "minutes", (30, 2880), None, "low"),
    a.DEEP_MODEL: ("sonnet-5-5", "enum", None, (None, None), ["haiku-5-5", "sonnet-5-5", "opus-5-5"], "medium"),
    a.LOOKUP_WINDOWS_MIN: ([15, 60, 360, 1440], "list", "minutes", (None, None),
                           [15, 30, 60, 120, 180, 360, 720, 1440], "low"),
}


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
async def test_486_registered_with_the_stage_and_its_month_cap(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '486'") == "486"
    row = await conn.fetchrow(STAGE_ROW, STAGE)
    assert row is not None
    assert (row["role"], row["provider"], row["model_id"], row["account_route"]) == ("writer", "claude", "haiku-5-5",
                                                                                     None)
    assert row["is_active"] is True and list(row["fallback_model_ids"]) == [] and row["shadow_model_id"] is None
    assert row["shadow_sample_pct"] == 0 and sorted(row["allowed_scopes"]) == ["aws_only", "vendor_direct"]
    assert (row["route_deadline_s"], row["degrade_mode"], row["pii_class"], row["free_text"]) == (
        25, "basic_narration", "content", True)
    b = await conn.fetchrow(BUDGET_ROW, SCOPE)
    assert b is not None
    assert (b["per_run_usd"], b["per_day_usd"], b["month"], b["hard_stop"], b["alert_pct"]) == (None, None, 10.0,
                                                                                                True, 80)


@pytest.mark.asyncio
async def test_a_rerun_keeps_an_admins_edit(conn):
    await conn.execute("UPDATE shared.llm_role_config SET is_active = false WHERE stage = $1", STAGE)
    await conn.execute("UPDATE shared.spend_budget SET per_month_usd = 4 WHERE provider = 'llm' AND scope = $1",
                       SCOPE)
    sql = open(M486, encoding="utf-8").read().replace("BEGIN;", "").replace("COMMIT;", "")
    await conn.execute(sql)
    assert await conn.fetchval("SELECT is_active AS ok FROM shared.llm_role_config WHERE stage = $1",
                               STAGE) is False
    assert (await conn.fetchrow(BUDGET_ROW, SCOPE))["month"] == 4.0


@pytest.mark.asyncio
async def test_the_runtime_role_reads_both_rows(conn):
    # The test database has no Dev grants (Dev: cis_app SELECT on both tables, db-schema-reference); give them inside
    # the rolled-back transaction only.
    await conn.execute("GRANT USAGE ON SCHEMA shared TO cis_app")
    await conn.execute("GRANT SELECT ON shared.llm_role_config, shared.spend_budget TO cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")
    assert await conn.fetchval("SELECT is_active AS ok FROM shared.llm_role_config WHERE stage = $1", STAGE) is True
    assert await conn.fetchval("SELECT hard_stop AS ok FROM shared.spend_budget WHERE provider = 'llm' AND scope = $1",
                               SCOPE) is True


def test_every_a1_reader_key_is_seeded():
    assert set(EXPECTED) == set(a.A1)


@pytest.mark.asyncio
async def test_the_a1_settings_are_the_compiled_defaults(conn):
    for key, (default, vtype, unit, bounds, allowed, impact) in EXPECTED.items():
        row = await conn.fetchrow(SETTING, key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == default == d.default_value, key
        assert (row["value_type"], row["unit"], (row["lo"], row["hi"])) == (vtype, unit, bounds), key
        assert (json.loads(row["allowed"]) if row["allowed"] else None) == allowed, key
        assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
            "ops", "platform", impact, "maker_checker"), key
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True and row["label"] == d.label and row["description"] == d.description


@pytest.mark.asyncio
async def test_an_a1_value_must_stay_inside_its_allowed_set(conn):
    for key, bad in ((a.AUTO_SEVERITIES, '["sev4"]'), (a.AUTO_SEVERITIES, '"sev1"'), (a.DEEP_MODEL, '"zz-model"'),
                     (a.LOOKUP_WINDOWS_MIN, "[45]"), (a.AUTO_INCIDENT_GAP_MIN, "29"),
                     (a.AUTO_INCIDENT_GAP_MIN, "2881")):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INS, key, bad)
    assert await conn.fetchval(INS, a.AUTO_SEVERITIES, "[]") is not None
    assert await conn.fetchval(INS, a.LOOKUP_WINDOWS_MIN, "[30, 180]") is not None
