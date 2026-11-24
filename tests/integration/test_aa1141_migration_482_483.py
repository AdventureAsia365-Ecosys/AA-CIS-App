"""AA-1141 — CIS 482 + 483 on a real Postgres (conftest replays every migration): the ack and saved view tables with
their constraints and grants (482), the ten P1 settings of the Ecosystem status page equal to the compiled defaults
with a complete guide (483), values outside their bounds refused, a re-run of 483 changes nothing. Runs in a
transaction that is rolled back; invented values only."""
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import ops_status_thresholds as th  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M483 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "483_aa1141_ops_status_p1_settings.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
ROW = ("SELECT default_value::text AS d, value_type, unit, min_value::float8 AS lo, max_value::float8 AS hi, area, "
       "scope_kind, impact, approval, evidence_required, env_override, enforced, label, description, "
       "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1")
ACK = ("INSERT INTO acp_shared.ops_alert_ack (item_id, app, severity, title, action, until, reason, actor) "
       "VALUES ($1, $2, $3, 'zz invented title', $4, now() + make_interval(hours => $5), $6, 'admin:zz-actor') "
       "RETURNING ack_id")
VIEW = "INSERT INTO acp_shared.ops_saved_view (staff_id, name, query) VALUES ($1, $2, $3::jsonb) RETURNING view_id"

EXPECTED = {
    th.ACK_REASON_MIN_CHARS: (20, "int", "characters", (10, 500)),
    th.ACK_HOURS: (24, "int", "hours", (1, 168)),
    th.MUTE_MAX_DAYS: (7, "int", "days", (1, 30)),
    th.SNOOZE_TOMORROW_HOUR: (8, "int", "hour", (0, 23)),
    th.COST_ANOMALY_FACTOR: (3, "numeric", "x", (1.5, 50)),
    th.COST_ANOMALY_BASELINE_DAYS: (7, "int", "days", (1, 30)),
    th.COST_ANOMALY_FLOOR_USD_H: (0.1, "usd", "USD/hour", (0, 100)),
    th.FORECAST_WINDOW_DAYS: (7, "int", "days", (1, 30)),
    th.FORECAST_WARN_DAYS: (30, "int", "days", (1, 365)),
    th.FORECAST_CRIT_DAYS: (7, "int", "days", (1, 180)),
}
REASON = "zz an invented reason long enough"


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


def test_every_new_reader_key_is_seeded():
    assert set(EXPECTED) <= set(th.ALL)


@pytest.mark.asyncio
async def test_482_483_registered(conn):
    got = await conn.fetch("SELECT version AS v FROM shared.schema_versions WHERE version IN ('482', '483')")
    assert sorted(r["v"] for r in got) == ["482", "483"]


@pytest.mark.asyncio
async def test_483_keys_are_the_compiled_defaults(conn):
    for key, (default, vtype, unit, bounds) in EXPECTED.items():
        row = await conn.fetchrow(ROW, key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == default == d.default_value, key
        assert (row["value_type"], row["unit"], (row["lo"], row["hi"])) == (vtype, unit, bounds), key
        assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
            "ops", "platform", "low", "maker_checker")
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True and row["label"] == d.label and row["description"] == d.description


@pytest.mark.asyncio
async def test_a_value_must_stay_inside_its_bounds(conn):
    for key, bad in ((th.ACK_REASON_MIN_CHARS, "9"), (th.ACK_HOURS, "0"), (th.ACK_HOURS, "169"),
                     (th.MUTE_MAX_DAYS, "31"), (th.SNOOZE_TOMORROW_HOUR, "24"), (th.COST_ANOMALY_FACTOR, "1.2"),
                     (th.COST_ANOMALY_BASELINE_DAYS, "0"), (th.COST_ANOMALY_FLOOR_USD_H, "101"),
                     (th.FORECAST_WINDOW_DAYS, "31"), (th.FORECAST_CRIT_DAYS, "181"), (th.FORECAST_WARN_DAYS, '"30"')):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INS, key, bad)
    assert await conn.fetchval(INS, th.FORECAST_WARN_DAYS, "45") is not None


@pytest.mark.asyncio
async def test_rerun_483_changes_nothing(conn):
    body = open(M483, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    q = "SELECT key, label, description, guide::text AS g FROM shared.setting_definition ORDER BY key"
    before = await conn.fetch(q)
    await conn.execute(body)
    assert [tuple(r) for r in await conn.fetch(q)] == [tuple(r) for r in before]


@pytest.mark.asyncio
async def test_ack_table_constraints(conn):
    assert await conn.fetchval(ACK, "errors-cis", "cis", "sev2", "ack", 1, REASON) is not None
    for args in (("x", "cis", "sev2", "pause", 1, REASON),          # unknown action
                 ("x", "nowhere", "sev2", "ack", 1, REASON),        # unknown app
                 ("x", "cis", "sev9", "ack", 1, REASON),            # unknown severity
                 ("x", "cis", "sev2", "ack", -1, REASON),           # ends before it starts
                 ("x", "cis", "sev2", "ack", 1, "too short"),      # reason under the floor of 10
                 ("", "cis", "sev2", "ack", 1, REASON)):            # empty item id
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(ACK, *args)
    aid = await conn.fetchval(ACK, "svc-zz", "shared", None, "mute", 48, REASON)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):   # cleared_at without cleared_by
        async with conn.transaction():
            await conn.execute("UPDATE acp_shared.ops_alert_ack SET cleared_at = now() WHERE ack_id = $1", aid)
    await conn.execute("UPDATE acp_shared.ops_alert_ack SET cleared_at = now(), cleared_by = 'admin:zz' "
                       "WHERE ack_id = $1", aid)


@pytest.mark.asyncio
async def test_saved_view_constraints(conn):
    staff = uuid.UUID("00000000-0000-4000-8000-00000000a141")
    assert await conn.fetchval(VIEW, staff, "Wave watch", '{"range": "6h"}') is not None
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):   # same name, other case and spaces
        async with conn.transaction():
            await conn.fetchval(VIEW, staff, "  wave WATCH ", '{"range": "1h"}')
    for name, query in (("", '{"range": "1h"}'), ("x" * 81, '{"range": "1h"}'), ("List", '["1h"]'),
                        ("Big", json.dumps({"q": "x" * 5000}))):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(VIEW, staff, name, query)


@pytest.mark.asyncio
async def test_runtime_role_grants(conn):
    q = "SELECT has_table_privilege('cis_app', $1, $2) AS ok"
    for priv in ("SELECT", "INSERT", "UPDATE"):
        assert await conn.fetchval(q, "acp_shared.ops_alert_ack", priv) is True
    for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
        assert await conn.fetchval(q, "acp_shared.ops_saved_view", priv) is True
