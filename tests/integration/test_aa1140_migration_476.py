"""AA-1140 (S289, the Ecosystem status page) — CIS 476 on a real Postgres (conftest replays every migration): the
twenty-one cis.ops.status_* settings rows equal the compiled defaults with a complete guide, values outside the bounds
are refused, acp_shared.ops_sample / acp_shared.ops_event take the rows the status sampler writes and refuse the shapes
it must never write (unknown kind or state, an oversized value, an event that ends before it starts, a duplicate
dedupe key), cis_app may read, insert and update but not delete, and a re-run changes nothing. Runs in a transaction
that is rolled back; invented service and alarm names only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import ops_status_thresholds as t  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M476 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "476_aa1140_ops_status_samples_and_thresholds.sql")
INS_VALUE = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
             "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) "
             "RETURNING id")
INS_SAMPLE = ("INSERT INTO acp_shared.ops_sample (sampled_at, kind, subject, app, state, value) "
              "VALUES (now(), $1, $2, $3, $4, $5::jsonb) RETURNING sample_id")
INS_EVENT = ("INSERT INTO acp_shared.ops_event "
             "(occurred_at, ended_at, kind, severity, app, subject, title, dedupe_key) "
             "VALUES ($1::text::timestamptz, $2::text::timestamptz, $3, $4, $5, $6, $7, $8) RETURNING event_id")


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


async def _refused(c, sql, *args, error=asyncpg.exceptions.CheckViolationError):
    with pytest.raises(error):
        async with c.transaction():
            await c.fetchval(sql, *args)


@pytest.mark.asyncio
async def test_476_registered_and_each_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '476'") == "476"
    for key in t.ALL:
        row = await conn.fetchrow(
            "SELECT default_value::text AS d, min_value::float AS lo, max_value::float AS hi, area, scope_kind, "
            "approval, evidence_required, env_override, enforced, label, "
            "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1", key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == d.default_value
        assert (row["lo"], row["hi"]) == (float(d.min_value), float(d.max_value))
        assert (row["area"], row["scope_kind"], row["approval"]) == ("ops", "platform", "maker_checker")
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True and row["label"] == d.label


@pytest.mark.asyncio
async def test_a_value_must_stay_within_its_bounds(conn):
    await _refused(conn, INS_VALUE, t.ERROR_RATE_WARN_PCT, "25")
    await _refused(conn, INS_VALUE, t.UPTIME_SLO_PCT, "100")
    await _refused(conn, INS_VALUE, t.HISTORY_DAYS, "31")
    await _refused(conn, INS_VALUE, t.STALE_FACTOR, "1")
    assert await conn.fetchval(INS_VALUE, t.P95_WARN_MS, "2000") is not None
    assert await conn.fetchval(INS_VALUE, t.RDS_FREE_STORAGE_CRIT_GIB, "7.5") is not None


@pytest.mark.asyncio
async def test_sample_rows_and_their_checks(conn):
    assert await conn.fetchval(INS_SAMPLE, "ecs_service", "zz-svc-api", "cis", "ok",
                               json.dumps({"running": 1, "desired": 1, "rollout": "COMPLETED"})) is not None
    await _refused(conn, INS_SAMPLE, "lambda", "zz-fn", "tripplanner", "ok", "{}")
    await _refused(conn, INS_SAMPLE, "queue", "shared.job", "cis", "green", "{}")
    await _refused(conn, INS_SAMPLE, "queue", "shared.job", "partner", "ok", "{}")
    await _refused(conn, INS_SAMPLE, "alarms", "all", None, "ok", json.dumps({"x": "y" * 9000}))
    await _refused(conn, INS_SAMPLE, "alarms", "all", None, "ok", "[]")
    # one row per (kind, subject, sampled_at): now() is the transaction time, so a second insert is a duplicate
    await _refused(conn, INS_SAMPLE, "ecs_service", "zz-svc-api", "cis", "ok", "{}",
                   error=asyncpg.exceptions.UniqueViolationError)


@pytest.mark.asyncio
async def test_event_rows_and_their_checks(conn):
    eid = await conn.fetchval(INS_EVENT, "2026-11-20T01:00:00Z", None, "alarm", "sev2", "crm", "zz-alarm-p95",
                              "Alarm: zz p95", "alarm:zz-alarm-p95:2026-11-20T01:00:00Z")
    await conn.execute("UPDATE acp_shared.ops_event SET ended_at = '2026-11-20T01:30:00Z' WHERE event_id = $1", eid)
    await _refused(conn, INS_EVENT, "2026-11-20T02:00:00Z", None, "alarm", "sev2", "crm", "zz-alarm-p95", "again",
                   "alarm:zz-alarm-p95:2026-11-20T01:00:00Z", error=asyncpg.exceptions.UniqueViolationError)
    await _refused(conn, INS_EVENT, "2026-11-20T02:00:00Z", "2026-11-20T01:00:00Z", "deploy", "info", "cis",
                   "zz-svc", "Deploy", "deploy:zz-1")
    await _refused(conn, INS_EVENT, "2026-11-20T02:00:00Z", None, "lambda_publish", "info", "cis", "zz-svc",
                   "Deploy", "deploy:zz-2")
    await _refused(conn, INS_EVENT, "2026-11-20T02:00:00Z", None, "deploy", "sev4", "cis", "zz-svc", "Deploy",
                   "deploy:zz-3")


@pytest.mark.asyncio
async def test_cis_app_reads_inserts_updates_but_never_deletes(conn):
    privs = await conn.fetch(
        "SELECT table_name AS t, privilege_type AS p FROM information_schema.role_table_grants "
        "WHERE grantee = 'cis_app' AND table_schema = 'acp_shared' AND table_name IN ('ops_sample', 'ops_event')")
    got = {(r["t"], r["p"]) for r in privs}
    for table in ("ops_sample", "ops_event"):
        assert {(table, p) for p in ("SELECT", "INSERT", "UPDATE")} <= got
        assert (table, "DELETE") not in got


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M476, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
