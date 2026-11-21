"""AA-1171 (S287 alerts catalogue P0) — CIS 469 on a real Postgres (conftest replays every migration): the eight
cis.ops.* settings rows equal the compiled defaults, values outside the bounds are refused, and
shared.alert_recipient.topic: default 'alerts', CHECK alerts | page, one active row per (topic, e-mail) so one address
may be on both topics; a re-run changes nothing. Runs in a transaction that is rolled back; invented addresses only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import ops_thresholds as t  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M469 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations",
                    "469_aa1171_ops_thresholds_and_recipient_topic.sql")
INS_VALUE = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
             "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) "
             "RETURNING id")
INS_RECIPIENT = ("INSERT INTO shared.alert_recipient (email, status, created_by, topic) "
                 "VALUES ($1, 'pending', 'admin:zz-test', $2) RETURNING id")


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
async def test_469_registered_and_each_row_is_the_compiled_default(conn):
    assert await conn.fetchval("SELECT version AS ok FROM shared.schema_versions WHERE version = '469'") == "469"
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


async def _refused(c, sql, *args, error=asyncpg.exceptions.CheckViolationError):
    with pytest.raises(error):
        async with c.transaction():
            await c.fetchval(sql, *args)


@pytest.mark.asyncio
async def test_a_value_must_stay_within_its_bounds(conn):
    await _refused(conn, INS_VALUE, t.JOBS_STARVED_AFTER_S, "60")
    await _refused(conn, INS_VALUE, t.ROLE_CONNECTIONS_PCT, "100")
    await _refused(conn, INS_VALUE, t.BELL_EVERY_HOURS, "0")
    assert await conn.fetchval(INS_VALUE, t.JOBS_STARVED_AFTER_S, "1200") is not None
    assert await conn.fetchval(INS_VALUE, t.JOBS_OVERDUE_FACTOR, "3.5") is not None


@pytest.mark.asyncio
async def test_recipient_topic_default_check_and_unique_per_topic(conn):
    rid = await conn.fetchval("INSERT INTO shared.alert_recipient (email, status, created_by) "
                              "VALUES ('zz-one@example.invalid', 'pending', 'admin:zz-test') RETURNING id")
    assert await conn.fetchval("SELECT topic AS ok FROM shared.alert_recipient WHERE id = $1", rid) == "alerts"
    assert await conn.fetchval(INS_RECIPIENT, "zz-one@example.invalid", "page") is not None
    await _refused(conn, INS_RECIPIENT, "zz-one@example.invalid", "page",
                   error=asyncpg.exceptions.UniqueViolationError)
    await _refused(conn, INS_RECIPIENT, "zz-two@example.invalid", "sms")
    idx = await conn.fetch("SELECT indexname AS name FROM pg_indexes WHERE schemaname = 'shared' "
                           "AND tablename = 'alert_recipient'")
    names = {r["name"] for r in idx}
    assert "alert_recipient_active_topic_email_uq" in names and "alert_recipient_active_email_uq" not in names


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    body = open(M469, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    await conn.execute(body)
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
