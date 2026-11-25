"""AA-1142 follow-up — CIS 492 + 493 on a real Postgres (conftest replays every migration): the source column of
acp_shared.ops_external_check (default github, CHECK, unique with target and time; the S292 key still answers the old
ON CONFLICT), the four settings of the worker's external checks equal to the compiled defaults with a complete guide,
values outside their bounds refused, a re-run of both files changes nothing. Runs in a transaction that is rolled
back; invented values only."""
import json
import os
import sys
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import ops_external_settings as xs  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M492 = os.path.join(MIG, "492_aa1142_external_check_source.sql")
M493 = os.path.join(MIG, "493_aa1142_external_check_worker_settings.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
ROW = ("SELECT default_value::text AS d, value_type, unit, min_value::float8 AS lo, max_value::float8 AS hi, area, "
       "scope_kind, impact, approval, evidence_required, env_override, enforced, label, description, "
       "allowed_values::text AS allowed, shared.setting_guide_complete(guide) AS guided "
       "FROM shared.setting_definition WHERE key = $1")
EXPECTED = {
    xs.SOURCES_KEY: ("list", None, (None, None)),
    xs.INTERVAL_MIN_KEY: ("int", "minutes", (5, 60)),
    xs.TIMEOUT_S_KEY: ("int", "seconds", (2, 30)),
    xs.TARGETS_KEY: ("json", None, (None, None)),
}
OLD = ("INSERT INTO acp_shared.ops_external_check (checked_at, target, app, host, state) "
       "VALUES ('2026-11-25T10:00:00Z', 'zz-api', 'cis', 'api.zz-example.test', 'ok') "
       "ON CONFLICT (target, checked_at) DO NOTHING")
NEW = ("INSERT INTO acp_shared.ops_external_check (source, checked_at, target, app, host, state) "
       "VALUES ($1, $2::timestamptz, 'zz-api', 'cis', 'api.zz-example.test', 'ok') ON CONFLICT DO NOTHING")


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


def _body(path: str) -> str:
    return open(path, encoding="utf-8").read().replace("BEGIN;", "", 1).rsplit("COMMIT;", 1)[0]


def test_every_reader_key_is_seeded():
    assert set(EXPECTED) == set(xs.ALL)


@pytest.mark.asyncio
async def test_492_493_registered(conn):
    assert await conn.fetchval("SELECT count(*) FROM shared.schema_versions WHERE version IN ('492', '493')") == 2


@pytest.mark.asyncio
async def test_the_old_insert_still_works_and_labels_github(conn):
    await conn.execute(OLD)
    await conn.execute(OLD)   # the S292 code's ON CONFLICT (target, checked_at) still has its arbiter index
    rows = await conn.fetch("SELECT source AS s FROM acp_shared.ops_external_check WHERE target = 'zz-api'")
    assert [r["s"] for r in rows] == ["github"]


@pytest.mark.asyncio
async def test_source_is_checked_and_part_of_the_key(conn):
    at = datetime(2026, 11, 25, 10, 5, tzinfo=timezone.utc)
    await conn.execute(NEW, "worker", at)
    await conn.execute(NEW, "worker", at)    # same source, target, time: nothing new
    # another source a minute later (the same instant still meets the S292 key (target, checked_at) until it is dropped)
    await conn.execute(NEW, "github", datetime(2026, 11, 25, 10, 6, tzinfo=timezone.utc))
    n = await conn.fetchval("SELECT count(*) AS n FROM acp_shared.ops_external_check WHERE target = 'zz-api'")
    assert n == 2
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with conn.transaction():
            await conn.execute(NEW, "zz", datetime(2026, 11, 25, 10, 7, tzinfo=timezone.utc))
    idx = {r["indexname"] for r in await conn.fetch(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'acp_shared' AND tablename = 'ops_external_check'")}
    assert {"ops_external_check_source_target_at_key", "ops_external_check_source_at_idx",
            "ops_external_check_target_at_key"} <= idx


@pytest.mark.asyncio
async def test_493_keys_are_the_compiled_defaults(conn):
    for key, (vtype, unit, bounds) in EXPECTED.items():
        row = await conn.fetchrow(ROW, key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == d.default_value, key
        assert (row["value_type"], row["unit"], (row["lo"], row["hi"])) == (vtype, unit, bounds), key
        assert (row["area"], row["scope_kind"], row["impact"], row["approval"]) == (
            "ops", "platform", "medium", "maker_checker")
        assert row["evidence_required"] is False and row["env_override"] is None and row["enforced"] is True
        assert row["guided"] is True and row["label"] == d.label and row["description"] == d.description
    allowed = await conn.fetchval("SELECT allowed_values FROM shared.setting_definition WHERE key = $1",
                                  xs.SOURCES_KEY)
    assert json.loads(allowed) == ["worker", "github"]


@pytest.mark.asyncio
async def test_a_value_must_fit_its_definition(conn):
    for key, bads in ((xs.INTERVAL_MIN_KEY, ("4", "61", "1.5")), (xs.TIMEOUT_S_KEY, ("1", "31")),
                      (xs.SOURCES_KEY, ('["zz"]', '"worker"')), (xs.TARGETS_KEY, ('"zz"', "5"))):
        for bad in bads:
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                async with conn.transaction():
                    await conn.fetchval(INS, key, bad)
    assert await conn.fetchval(INS, xs.SOURCES_KEY, '["worker"]') is not None
    assert await conn.fetchval(INS, xs.INTERVAL_MIN_KEY, "5") is not None


@pytest.mark.asyncio
async def test_rerun_492_493_changes_nothing(conn):
    q = "SELECT key, label, description, guide::text AS g FROM shared.setting_definition ORDER BY key"
    before = await conn.fetch(q)
    await conn.execute(_body(M492))
    await conn.execute(_body(M493))
    assert [tuple(r) for r in await conn.fetch(q)] == [tuple(r) for r in before]
