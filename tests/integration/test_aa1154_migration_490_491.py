"""AA-1154 (A2) — CIS 490 + 491 on a real Postgres (conftest replays every migration): the run record of the CIS admin
assistant (acp_shared.ops_assist_run) with its constraints and grants (490), the three A2 settings equal to the
compiled defaults with a complete guide (491), values outside their bounds refused, a re-run of 491 changes nothing.
Runs in a transaction that is rolled back; invented values only."""
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
M491 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "491_aa1154_ops_assist_a2_settings.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
SETTING = ("SELECT default_value::text AS d, allowed_values::text AS allowed, value_type, unit, min_value::float8 AS lo, "
           "max_value::float8 AS hi, area, scope_kind, impact, approval, evidence_required, env_override, enforced, "
           "label, description, shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition "
           "WHERE key = $1")
RUN = ("INSERT INTO acp_shared.ops_assist_run (item_id, card_id, action, playbook, risk_level, range_key, predicted, "
       "measures, check_after_min, checks, verdict, checked_at, recorded_by) "
       "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9::int[], $10::jsonb, $11, $12, 'admin:zz-a') "
       "RETURNING run_id")
GOOD = ("queue-zz", "queue-zz:job_cancel", "job_cancel", "queue_stuck", "R2", "24h", "[]", "[]", [10, 30], "[]", None,
        None)
# key: (default, value type, unit, (min, max), allowed values)
EXPECTED = {
    a.CHECK_AFTER_MIN: ([10, 30], "list", "minutes", (None, None), [5, 10, 15, 20, 30, 45, 60, 90, 120]),
    a.DIGEST_DAYS: (7, "int", "days", (1, 90), None),
    a.BUILD_TIMEOUT_S: (20, "int", "seconds", (5, 25), None),
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


def test_every_a2_reader_key_is_seeded():
    assert set(EXPECTED) == set(a.A2)


@pytest.mark.asyncio
async def test_490_491_registered(conn):
    got = await conn.fetch("SELECT version AS v FROM shared.schema_versions WHERE version IN ('490', '491')")
    assert sorted(r["v"] for r in got) == ["490", "491"]


@pytest.mark.asyncio
async def test_the_a2_settings_are_the_compiled_defaults(conn):
    for key, (default, vtype, unit, bounds, allowed) in EXPECTED.items():
        row = await conn.fetchrow(SETTING, key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == default == d.default_value, key
        assert (row["value_type"], row["unit"], (row["lo"], row["hi"])) == (vtype, unit, bounds), key
        assert (json.loads(row["allowed"]) if row["allowed"] else None) == allowed, key
        assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
            "ops", "platform", "low", "maker_checker"), key
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True and row["label"] == d.label and row["description"] == d.description


@pytest.mark.asyncio
async def test_an_a2_value_must_stay_inside_its_bounds(conn):
    for key, bad in ((a.CHECK_AFTER_MIN, "[7]"), (a.CHECK_AFTER_MIN, "10"), (a.DIGEST_DAYS, "0"),
                     (a.DIGEST_DAYS, "91"), (a.BUILD_TIMEOUT_S, "4"), (a.BUILD_TIMEOUT_S, "26")):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INS, key, bad)
    assert await conn.fetchval(INS, a.CHECK_AFTER_MIN, "[15, 60]") is not None


@pytest.mark.asyncio
async def test_rerun_491_changes_nothing(conn):
    await conn.execute("UPDATE shared.setting_definition SET label = 'zz edited label' WHERE key = $1", a.DIGEST_DAYS)
    sql = open(M491, encoding="utf-8").read().replace("BEGIN;", "").replace("COMMIT;", "")
    await conn.execute(sql)
    assert await conn.fetchval("SELECT label AS ok FROM shared.setting_definition WHERE key = $1",
                               a.DIGEST_DAYS) == "zz edited label"


@pytest.mark.asyncio
async def test_a_run_row_and_its_constraints(conn):
    run_id = await conn.fetchval(RUN, *GOOD)
    assert run_id is not None
    done = list(GOOD)
    done[9], done[10] = '[{"after_min": 30, "verdict": "hit"}]', "hit"
    done[11] = None
    bad_rows = (
        {4: "R5"}, {2: "Not An Action"}, {5: "2d"}, {6: "{}"}, {7: '"x"'}, {8: []}, {8: [0]}, {9: "{}"},
        {10: "maybe", 11: None}, {10: "hit"}, {0: ""},
    )
    for change in bad_rows:
        row = list(GOOD)
        for i, v in change.items():
            row[i] = v
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(RUN, *row)
    checked = await conn.fetchval(
        "UPDATE acp_shared.ops_assist_run SET verdict = 'hit', checked_at = recorded_at + interval '30 minutes', "
        "checks = $2::jsonb WHERE run_id = $1 RETURNING verdict", run_id, done[9])
    assert checked == "hit"


@pytest.mark.asyncio
async def test_a_run_may_name_an_incident_that_exists(conn):
    row = list(GOOD)
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        async with conn.transaction():
            await conn.fetchval(RUN.replace("recorded_by)", "recorded_by, incident_id)")
                                .replace("'admin:zz-a')", "'admin:zz-a', -1)"), *row)


@pytest.mark.asyncio
async def test_runtime_role_grants(conn):
    q = "SELECT has_table_privilege('cis_app', 'acp_shared.ops_assist_run', $1) AS ok"
    for priv, want in (("SELECT", True), ("INSERT", True), ("UPDATE", True), ("DELETE", False)):
        assert await conn.fetchval(q, priv) is want, priv
