"""AA-1122 (S282) — CIS 440-452 (conftest replays every migration).

440: acp_contract.search_volume_monthly — a month is stored once (the primary key), the month is the first of a month,
     the runtime role reads and inserts but never updates nor deletes, a re-run changes nothing.
441: silver_aa_internal.seo_context.paa_source / paa_filled_at — NULL by default, only store / serp / none, a re-run
     changes nothing.
442: the research settings — registered; each row equals the compiled default the reader serves; every key of
     research_settings.AUTO / TOUR_PAA is seeded with a complete guide and enforced; spend keys are maker-checker; the two
     freshness windows are maker-checker at 180 days; bounds hold; a re-run changes nothing.
Runs in a transaction that is rolled back; invented keywords only."""
import datetime
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.seo_intelligence import research_settings as rs  # noqa: E402
from shared import settings  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIG = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
M450 = os.path.join(MIG, "440_aa1122_search_volume_monthly.sql")
M451 = os.path.join(MIG, "441_aa1122_seo_context_paa_source.sql")
M452 = os.path.join(MIG, "442_aa1122_research_auto_settings.sql")
INS = ("INSERT INTO shared.setting_value (key, value, version, proposed_by, reason, evidence_ref) "
       "VALUES ($1, $2::jsonb, 1, 'admin:zz-a', 'zz test: a reason of at least twenty characters', NULL) RETURNING id")
ROW = ("INSERT INTO acp_contract.search_volume_monthly (keyword, market, month, search_volume, source) "
       "VALUES ($1, 'US', $2, $3, 'search_volume_bulk') ON CONFLICT DO NOTHING")


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


@pytest.mark.asyncio
async def test_450_452_registered(conn):
    got = await conn.fetch("SELECT version AS v FROM shared.schema_versions WHERE version IN ('440', '441', '442')")
    assert sorted(r["v"] for r in got) == ["440", "441", "442"]


@pytest.mark.asyncio
async def test_450_a_month_is_stored_once(conn):
    may = datetime.date(2026, 5, 1)
    await conn.execute(ROW, "zz lantern valley trek", may, 1300)
    await conn.execute(ROW, "zz lantern valley trek", may, 9999)          # a later answer never overwrites
    assert await conn.fetchval(
        "SELECT search_volume AS v FROM acp_contract.search_volume_monthly WHERE keyword = $1 AND month = $2",
        "zz lantern valley trek", may) == 1300
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with conn.transaction():
            await conn.execute(ROW, "zz lantern valley trek", datetime.date(2026, 5, 2), 10)
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        async with conn.transaction():
            await conn.execute(ROW, "zz lantern valley trek", datetime.date(2026, 6, 1), -1)


@pytest.mark.asyncio
async def test_450_cis_app_reads_and_inserts_only(conn):
    privs = {p: await conn.fetchval("SELECT has_table_privilege('cis_app', 'acp_contract.search_volume_monthly', $1) "
                                    "AS ok", p) for p in ("SELECT", "INSERT", "UPDATE", "DELETE")}
    assert privs == {"SELECT": True, "INSERT": True, "UPDATE": False, "DELETE": False}


@pytest.mark.asyncio
async def test_451_paa_source_values(conn):
    cols = {r["column_name"]: r for r in await conn.fetch(
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = 'silver_aa_internal' AND table_name = 'seo_context' "
        "AND column_name IN ('paa_source', 'paa_filled_at')")}
    assert cols["paa_source"]["data_type"] == "text" and cols["paa_source"]["column_default"] is None
    assert cols["paa_filled_at"]["data_type"] == "timestamp with time zone"
    check = await conn.fetchval("SELECT pg_get_constraintdef(oid) AS d FROM pg_constraint "
                                "WHERE conname = 'seo_context_paa_source_check'")
    for word in ("store", "serp", "none"):
        assert word in check


@pytest.mark.asyncio
async def test_452_every_key_seeded_and_equal_to_the_compiled_default(conn):
    for key in rs.AUTO + rs.TOUR_PAA:
        row = await conn.fetchrow(
            "SELECT default_value::text AS d, approval, scope_kind, enforced, env_override, label, area, "
            "shared.setting_guide_complete(guide) AS guided FROM shared.setting_definition WHERE key = $1", key)
        assert row is not None, key
        d = settings.definition(key)
        assert json.loads(row["d"]) == d.default_value, key
        assert row["scope_kind"] == "platform" and row["enforced"] is True and row["env_override"] is None, key
        assert row["guided"] is True and row["label"] == d.label, key
        assert row["area"] == key.split(".")[1], key
        if key.startswith("cis.spend.") and key not in (rs.AUTO_MIN_RUN_USD, rs.JEV_ASK_ESTIMATE_USD):
            assert row["approval"] == "maker_checker", key
    assert settings.definition(rs.AUTO_ENABLED).default_value is False
    assert settings.definition(rs.MAX_USD_PER_MONTH).default_value == 5
    assert settings.definition(rs.AUTO_MAX_USD_PER_RUN).default_value == 3
    assert settings.definition(rs.MARKET_TENANT_STATUSES).default_value == ["active"]


@pytest.mark.asyncio
async def test_452_freshness_windows_are_maker_checker_at_180(conn):
    for key in (rs.VOLUME_FRESH_DAYS, rs.SERP_FRESH_DAYS):
        row = await conn.fetchrow("SELECT default_value::text AS d, approval, min_value::float AS lo, "
                                  "max_value::float AS hi FROM shared.setting_definition WHERE key = $1", key)
        assert (json.loads(row["d"]), row["approval"], row["lo"], row["hi"]) == (180, "maker_checker", 30.0, 730.0)
        assert settings.definition(key).default_value == 180


@pytest.mark.asyncio
async def test_452_bounds_hold(conn):
    for key, bad in ((rs.MAX_USD_PER_MONTH, "101"), (rs.AUTO_MAX_USD_PER_RUN, "0.05"), (rs.AUTO_WINDOW_END_UTC, "25"),
                     (rs.AUTO_DFS_QUEUE, '"priority"'), (rs.MARKET_TENANT_STATUSES, '["zz-unknown"]'),
                     (rs.REFRESH_MAX_SHARE_PCT, "101")):
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with conn.transaction():
                await conn.fetchval(INS, key, bad)
    assert await conn.fetchval(INS, rs.MAX_USD_PER_MONTH, "0") is not None          # 0 = no research spend
    assert await conn.fetchval(INS, rs.MARKET_TENANT_STATUSES, '["active", "trial"]') is not None


@pytest.mark.asyncio
async def test_rerun_changes_nothing(conn):
    before = await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition")
    for path in (M450, M451, M452):
        await conn.execute(_body(path))
    assert await conn.fetchval("SELECT count(*) AS n FROM shared.setting_definition") == before
    assert await conn.fetchval("SELECT count(*) AS n FROM pg_constraint "
                               "WHERE conname = 'seo_context_paa_source_check'") == 1
