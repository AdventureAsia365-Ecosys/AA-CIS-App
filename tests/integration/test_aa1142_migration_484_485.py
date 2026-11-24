"""AA-1142 — CIS 484 + 485 on a real Postgres (conftest replays every migration): the incident, incident update and
external check tables with their constraints and grants (484), the ten P2 settings of the Ecosystem status page equal to
the compiled defaults with a complete guide (485), values outside their bounds refused, a re-run of 485 changes nothing.
Runs in a transaction that is rolled back; invented values only."""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import ops_status_thresholds as th  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
M485 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "485_aa1142_ops_status_p2_settings.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
ROW = ("SELECT default_value::text AS d, value_type, unit, min_value::float8 AS lo, max_value::float8 AS hi, area, "
       "scope_kind, impact, approval, evidence_required, env_override, enforced, label, description, "
       "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1")
INC = ("INSERT INTO acp_shared.ops_incident (title, severity, status, app, item_id, owner, declared_by, resolved_at) "
       "VALUES ($1, $2, $3, $4, $5, 'admin:zz-owner', 'admin:zz-owner', $6) RETURNING incident_id")
UPD = ("INSERT INTO acp_shared.ops_incident_update (incident_id, actor, kind, from_value, to_value, note) "
       "VALUES ($1, 'admin:zz-owner', $2, $3, $4, $5) RETURNING update_id")
EXT = ("INSERT INTO acp_shared.ops_external_check (checked_at, target, app, host, state, dns_ok, tls_ok, http_status, "
       "latency_ms) VALUES ('2026-11-25T10:00:00Z', $1, $2, $3, $4, true, true, $5, 120) RETURNING check_id")

EXPECTED = {
    th.SLO_TARGET_PCT: (99.5, "numeric", "%", (90, 99.99)),
    th.SLO_WINDOW_DAYS: (30, "int", "days", (7, 90)),
    th.BURN_FAST_WINDOW_MIN: (60, "int", "minutes", (5, 240)),
    th.BURN_FAST_FACTOR: (14.4, "numeric", "x", (1, 100)),
    th.BURN_SLOW_WINDOW_HOURS: (6, "int", "hours", (1, 72)),
    th.BURN_SLOW_FACTOR: (6, "numeric", "x", (1, 100)),
    th.CERT_WARN_DAYS: (21, "int", "days", (1, 90)),
    th.CERT_CRIT_DAYS: (7, "int", "days", (1, 60)),
    th.INCIDENT_LOOKBACK_MIN: (60, "int", "minutes", (0, 1440)),
    th.TENANT_TOP_N: (10, "int", "agencies", (3, 50)),
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


def test_every_new_reader_key_is_seeded():
    assert set(EXPECTED) <= set(th.ALL)


@pytest.mark.asyncio
async def test_484_485_registered(conn):
    got = await conn.fetch("SELECT version AS v FROM shared.schema_versions WHERE version IN ('484', '485')")
    assert sorted(r["v"] for r in got) == ["484", "485"]


@pytest.mark.asyncio
async def test_485_keys_are_the_compiled_defaults(conn):
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
    for key, bad in ((th.SLO_TARGET_PCT, "89"), (th.SLO_TARGET_PCT, "100"), (th.SLO_WINDOW_DAYS, "6"),
                     (th.BURN_FAST_WINDOW_MIN, "241"), (th.BURN_FAST_FACTOR, "0.5"), (th.BURN_SLOW_WINDOW_HOURS, "0"),
                     (th.BURN_SLOW_FACTOR, "101"), (th.CERT_WARN_DAYS, "91"), (th.CERT_CRIT_DAYS, "0"),
                     (th.INCIDENT_LOOKBACK_MIN, "1441"), (th.TENANT_TOP_N, "2"), (th.TENANT_TOP_N, '"10"')):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INS, key, bad)
    assert await conn.fetchval(INS, th.SLO_TARGET_PCT, "99.9") is not None
    assert await conn.fetchval(INS, th.BURN_FAST_FACTOR, "10") is not None


@pytest.mark.asyncio
async def test_rerun_485_changes_nothing(conn):
    body = open(M485, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]
    q = "SELECT key, label, description, guide::text AS g FROM shared.setting_definition ORDER BY key"
    before = await conn.fetch(q)
    await conn.execute(body)
    assert [tuple(r) for r in await conn.fetch(q)] == [tuple(r) for r in before]


@pytest.mark.asyncio
async def test_incident_constraints(conn):
    iid = await conn.fetchval(INC, "Invented outage of the zz api", "sev2", "investigating", "cis", "errors-cis", None)
    assert iid is not None
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):   # one unresolved incident per item
        async with conn.transaction():
            await conn.fetchval(INC, "A second zz incident", "sev3", "identified", "cis", "errors-cis", None)
    for args in (("zz", "sev2", "investigating", "cis", None, None),                  # title too short
                 ("Invented zz title", "sev4", "investigating", "cis", None, None),   # unknown severity
                 ("Invented zz title", "sev2", "paused", "cis", None, None),          # unknown status
                 ("Invented zz title", "sev2", "investigating", "nowhere", None, None),   # unknown app
                 ("Invented zz title", "sev2", "resolved", "cis", None, None)):       # resolved without a time
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INC, *args)
    # a resolved incident frees the item for a new declaration
    await conn.execute("UPDATE acp_shared.ops_incident SET status = 'resolved', resolved_at = now() "
                       "WHERE incident_id = $1", iid)
    assert await conn.fetchval(INC, "Invented zz relapse", "sev2", "investigating", "cis", "errors-cis",
                               None) is not None


@pytest.mark.asyncio
async def test_incident_update_constraints(conn):
    iid = await conn.fetchval(INC, "Invented zz slowness", "sev3", "investigating", "booking", None, None)
    assert await conn.fetchval(UPD, iid, "status", "investigating", "identified", None) is not None
    assert await conn.fetchval(UPD, iid, "note", None, None, "zz invented note") is not None
    for args in ((iid, "pause", None, None, None), (iid, "note", None, None, "  "), (iid, "status", "x" * 65, "y",
                                                                                       None)):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(UPD, *args)
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError):
        async with conn.transaction():
            await conn.fetchval(UPD, 987654321, "note", None, None, "zz orphan")


@pytest.mark.asyncio
async def test_external_check_constraints(conn):
    assert await conn.fetchval(EXT, "zz-api", "cis", "api.zz.example", "ok", 200) is not None
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):   # one result per target and time
        async with conn.transaction():
            await conn.fetchval(EXT, "zz-api", "cis", "api.zz.example", "down", 503)
    for args in (("ZZ api", "cis", "api.zz.example", "ok", 200), ("zz-web", "nowhere", "web.zz.example", "ok", 200),
                 ("zz-web", "cis", "Web.ZZ/x", "ok", 200), ("zz-web", "cis", "web.zz.example", "green", 200),
                 ("zz-web", "cis", "web.zz.example", "ok", 99)):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(EXT, *args)


@pytest.mark.asyncio
async def test_runtime_role_grants(conn):
    q = "SELECT has_table_privilege('cis_app', $1, $2) AS ok"
    for priv in ("SELECT", "INSERT", "UPDATE"):
        assert await conn.fetchval(q, "acp_shared.ops_incident", priv) is True
    for table in ("acp_shared.ops_incident_update", "acp_shared.ops_external_check"):
        for priv in ("SELECT", "INSERT"):
            assert await conn.fetchval(q, table, priv) is True
