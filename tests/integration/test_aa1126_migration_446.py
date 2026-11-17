"""AA-1126 (S283) — CIS 446: the gateway's spend controls in shared.setting_definition and the anomaly breaker reset
columns on shared.llm_app_profile (conftest replays every migration). Registered; each row equals the compiled default
the reader serves; values within their bounds only; enum values only from the allowed list; platform scope,
maker-checker, enforced, guided; the defaults keep today's Dev behaviour; cis_app may write the reset columns and the
actor length is checked; a re-run changes nothing. Runs in a transaction that is rolled back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import settings  # noqa: E402
from shared.llm_client import caps_settings as cs  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M446 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "446_aa1126_gateway_spend_controls.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
# key: (default, min, max, impact)
EXPECTED = {
    cs.CONNECTION_CAP_MODE: ("hard_stop", None, None, "high"),
    cs.ANOMALY_MODE: ("alert", None, None, "high"),
    cs.ANOMALY_MULTIPLE: (4, 1.5, 100, "high"),
    cs.ANOMALY_FLOOR_USD: (25, 0, 10000, "high"),
    cs.ANOMALY_BASELINE_DAYS: (14, 7, 60, "medium"),
    cs.RUN_CAP_S1_REWRITE_USD: (2, 0.05, 20, "high"),
    cs.RUN_CAP_EVAL_USD: (10, 0.05, 50, "high"),
    cs.RUN_CAP_ADHOC_USD: (5, 0.05, 50, "high"),
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


async def _refused(c, *args):
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with c.transaction():
            await c.fetchval(INS, *args)


@pytest.mark.asyncio
async def test_446_registered_and_each_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '446'") == "446"
    assert set(EXPECTED) == set(cs.ALL)
    for key, (default, lo, hi, impact) in EXPECTED.items():
        row = await conn.fetchrow(
            "SELECT default_value::text AS d, min_value::float AS lo, max_value::float AS hi, area, scope_kind, "
            "impact, approval, evidence_required, env_override, enforced, label, allowed_values::text AS allowed, "
            "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1", key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == default == d.default_value
        assert (row["lo"], row["hi"]) == (lo, hi)
        assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == ("spend", "platform", impact,
                                                                                     "maker_checker")
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True and row["label"] == d.label
    assert json.loads(await conn.fetchval("SELECT allowed_values::text AS ok FROM shared.setting_definition "
                                          "WHERE key = $1", cs.CONNECTION_CAP_MODE)) == list(cs.CAP_MODES)
    assert json.loads(await conn.fetchval("SELECT allowed_values::text AS ok FROM shared.setting_definition "
                                          "WHERE key = $1", cs.ANOMALY_MODE)) == list(cs.ANOMALY_MODES)


def test_the_defaults_keep_todays_dev_behaviour():
    assert cs.connection_cap_mode() == cs.HARD_STOP
    assert cs.anomaly_mode() == cs.ALERT                       # alerts only: nothing is refused by the breaker
    policy = cs.anomaly_policy()
    assert policy is not None and policy.multiple == 4 and policy.floor_usd == 25 and policy.baseline_days == 14
    assert cs.run_cap_usd("s1_rewrite") == 2 and cs.run_cap_usd("eval") == 10 and cs.run_cap_usd("adhoc") == 5
    assert cs.run_cap_usd("t2_rewrite") is None


@pytest.mark.asyncio
async def test_a_value_must_stay_within_its_bounds(conn):
    await _refused(conn, cs.CONNECTION_CAP_MODE, '"always_continue"')
    await _refused(conn, cs.ANOMALY_MODE, '"panic"')
    await _refused(conn, cs.ANOMALY_MULTIPLE, "1")
    await _refused(conn, cs.ANOMALY_BASELINE_DAYS, "3")
    await _refused(conn, cs.RUN_CAP_S1_REWRITE_USD, "25")
    assert await conn.fetchval(INS, cs.CONNECTION_CAP_MODE, '"failover_continue"') is not None
    assert await conn.fetchval(INS, cs.ANOMALY_MODE, '"enforce"') is not None
    assert await conn.fetchval(INS, cs.RUN_CAP_ADHOC_USD, "12.5") is not None


@pytest.mark.asyncio
async def test_the_reset_columns_and_their_grant(conn):
    cols = {r["column_name"]: r["data_type"] for r in await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'shared' AND table_name = 'llm_app_profile' AND column_name LIKE 'anomaly_reset_%'")}
    assert cols == {"anomaly_reset_at": "timestamp with time zone", "anomaly_reset_by": "text"}
    for col in ("anomaly_reset_at", "anomaly_reset_by"):
        assert await conn.fetchval("SELECT has_column_privilege('cis_app', 'shared.llm_app_profile', $1, 'UPDATE') "
                                   "AS ok", col) is True
    assert await conn.fetchval("SELECT has_table_privilege('cis_app', 'shared.llm_app_profile', 'DELETE') AS ok") \
        is False
    await conn.execute("UPDATE shared.llm_app_profile SET anomaly_reset_at = now(), anomaly_reset_by = 'admin:zz-b' "
                       "WHERE app = 'cis'")
    row = await conn.fetchrow("SELECT to_jsonb(p) AS row FROM shared.llm_app_profile p WHERE app = 'cis'")
    raw = json.loads(row["row"]) if isinstance(row["row"], str) else row["row"]
    assert raw["anomaly_reset_by"] == "admin:zz-b" and raw["anomaly_reset_at"]
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with conn.transaction():
            await conn.execute("UPDATE shared.llm_app_profile SET anomaly_reset_by = $1 WHERE app = 'cis'", "x" * 129)


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M446, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    cons = await conn.fetchval("SELECT count(*) AS n FROM pg_constraint "
                               "WHERE conname = 'llm_app_profile_anomaly_reset_by_check'")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
    assert await conn.fetchval("SELECT count(*) AS n FROM pg_constraint "
                               "WHERE conname = 'llm_app_profile_anomaly_reset_by_check'") == cons == 1
