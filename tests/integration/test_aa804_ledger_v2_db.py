"""AA-804 (ledger v2) — the writers and readers against real Postgres with migration 319 applied (conftest).

- call_log: a success row lands with its v2 columns (app, status, latency, attempt, connection, tour, prompt hash);
  a gateway failure row (refused, credential, 0 USD) lands through record_calls; the AA-729 relabelled v2 statement
  is valid SQL with the callers' parameter types and writes the eval stage, is_experiment and the tour in the signal.
- The retention rollup (v2 statement) counts refused / error rows and sums latency, app / shadow read with coalesce.
- dfs_call_log and geo.geocode_call v2 rows land.
- Every reader changed for the real columns runs (column names checked by Postgres) and reads coalesce(app, ...).
Every test removes its own rows.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import psycopg2
import psycopg2.extras
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from shared.llm_client import call_log  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "zz_aa804_db"
TOUR = "44444444-4444-4444-4444-444444444444"
RUN = "6f1c5f8e-1b9e-4c55-9f5e-1a2b3c4d5e6f"


@pytest.fixture
def cur(monkeypatch):
    real_connect = asyncpg.connect

    async def connect(*a, **k):                 # CI Postgres has no SSL; the code asks for it
        k.pop("ssl", None)
        return await real_connect(DSN, **{key: v for key, v in k.items() if key == "timeout"})
    monkeypatch.setattr(call_log.asyncpg, "connect", connect)
    monkeypatch.setattr(call_log, "get_database_url", lambda: DSN)
    monkeypatch.setattr(call_log, "_v2_missing_at", 0.0)
    conn = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    conn.autocommit = True
    c = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield c
    finally:
        c.execute("ROLLBACK")
        c.execute("DELETE FROM shared.llm_call_log WHERE stage IN (%s, 'adhoc_aa729')", (STAGE,))
        c.execute("DELETE FROM shared.llm_call_daily WHERE stage = %s", (STAGE,))
        c.execute("DELETE FROM shared.dfs_call_log WHERE endpoint = %s", (STAGE,))
        c.execute("DELETE FROM geo.geocode_call WHERE provider = %s", (STAGE,))
        conn.close()


def _rows(cur, stage=STAGE):
    cur.execute("SELECT * FROM shared.llm_call_log WHERE stage = %s ORDER BY created_at, id", (stage,))
    return cur.fetchall()


def test_a_success_row_lands_with_its_v2_columns(cur):
    asyncio.run(call_log.record_call(
        stage=STAGE, role="writer", model="satellite-sonnet-5-5", tokens_in=100, tokens_out=20, cost_usd=0.001,
        quality_signal={"passed": True}, account="acc3", latency_ms=812, attempt_no=2, tour_id=TOUR,
        deployment_wire_id="global.anthropic.claude-sonnet-5-5", request_id="req-9", prompt_hash="c" * 64,
        stop_reason="max_tokens"))
    (row,) = _rows(cur)
    assert (row["app"], row["status"], row["error_class"], row["latency_ms"], row["attempt_no"]) == \
        ("cis", "truncated", "truncated", 812, 2)
    assert (str(row["tour_id"]), row["connection_key"], row["is_shadow"], row["is_experiment"]) == \
        (TOUR, "acc3", False, False)
    assert row["deployment_wire_id"] == "global.anthropic.claude-sonnet-5-5" and row["request_id"] == "req-9"


def test_a_gateway_failure_row_lands_at_0_usd(cur):
    row = call_log.gateway_failure_row(
        stage=STAGE, role="validate", model="sonnet-5-5", status="refused", error_class="credential",
        error="ConnectionCredentialError: no key", provider="anthropic", connection_key="anthropic-direct",
        latency_ms=3, attempt_no=1, quality_signal_extra={"connection_test": "anthropic-direct"})
    asyncio.run(call_log.record_calls([row]))
    (got,) = _rows(cur)
    assert (float(got["cost_usd"]), got["status"], got["error_class"], got["account"], got["role"]) == \
        (0.0, "refused", "credential", "anthropic-direct", "validate")
    assert got["quality_signal"]["gateway_attempt"] is True and got["tokens_in"] is None


def test_the_relabelled_v2_statement_is_valid_sql(cur, monkeypatch):
    from scripts.aa729 import evalpatch
    monkeypatch.setattr(call_log, "_INSERT_SQL", evalpatch.relabel_sql(call_log._SHIPPED_INSERT_SQL, RUN))
    monkeypatch.setattr(call_log, "_INSERT_V2_SQL", evalpatch.relabel_v2_sql(call_log._SHIPPED_INSERT_V2_SQL, RUN))
    asyncio.run(call_log.record_call(
        stage=STAGE, role="judge", model="gpt-6-luna", tokens_in=5, tokens_out=1, cost_usd=0.0001,
        quality_signal={"passed": True}, tenant_id="00000000-0000-0000-0000-000000000001", tour_id=TOUR,
        latency_ms=40))
    assert _rows(cur) == []
    (row,) = [r for r in _rows(cur, "adhoc_aa729") if r["quality_signal"].get("aa729_run") == RUN]
    assert row["is_experiment"] is True and row["tour_id"] is None and row["tenant_id"] is None
    assert row["quality_signal"]["aa729_stage"] == STAGE and row["quality_signal"]["aa729_tour"] == TOUR
    assert row["latency_ms"] == 40


def test_the_rollup_counts_outcomes_and_sums_latency(cur):
    old = datetime.now(timezone.utc) - timedelta(days=500)
    for status, latency, qs in (("ok", 100, '{"app": "tripplanner"}'), ("refused", 5, '{}'), ("error", 7, '{}'),
                                (None, None, '{"shadow": true}')):
        cur.execute("INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, cost_usd, status, "
                    "latency_ms, created_at, provider) VALUES (%s, 'writer', 'm', %s::jsonb, 0, %s, %s, %s, 'p')",
                    (STAGE, qs, status, latency, old))

    async def roll():
        from services.retention import rollups
        conn = await asyncpg.connect(DSN)
        try:
            return await rollups.write(conn, "llm_call_daily", old + timedelta(days=1))
        finally:
            await conn.close()
    asyncio.run(roll())
    cur.execute("SELECT app, is_shadow, sum(calls)::int AS calls, sum(refused_calls)::int AS refused, "
                "sum(error_calls)::int AS errors, sum(latency_ms_sum)::int AS lat, sum(latency_ms_count)::int AS n "
                "FROM shared.llm_call_daily WHERE stage = %s GROUP BY 1, 2 ORDER BY 1, 2", (STAGE,))
    got = [tuple(r.values()) for r in cur.fetchall()]
    assert got == [("cis", False, 2, 1, 1, 12, 2), ("cis", True, 1, 0, 0, 0, 0),
                   ("tripplanner", False, 1, 0, 0, 100, 1)]


def test_dfs_and_geocode_v2_rows_land(cur, monkeypatch):
    from services.places import store
    from shared.dfs_client import call_log as dfs_log
    real = asyncpg.connect

    async def connect(*a, **k):
        return await real(DSN)
    monkeypatch.setattr(dfs_log, "_v2_missing_at", 0.0)
    monkeypatch.setattr(dfs_log.asyncpg, "connect", connect)
    monkeypatch.setattr(dfs_log, "get_database_url", lambda: DSN)
    asyncio.run(dfs_log.record_dfs_call(endpoint=STAGE, cost_usd=0.0, status="refused", error="budget: cap",
                                        stage="s1_rewrite"))
    cur.execute("SELECT status, error, stage, app FROM shared.dfs_call_log WHERE endpoint = %s", (STAGE,))
    assert tuple(cur.fetchone().values()) == ("refused", "budget: cap", "s1_rewrite", "cis")

    async def geo():
        conn = await asyncpg.connect(DSN)
        try:
            with call_log.bind_tenant("00000000-0000-0000-0000-000000000001"):
                await store.log_geocode_calls(conn, STAGE, 4, 0.002, None, failed_requests=1)
        finally:
            await conn.close()
    asyncio.run(geo())
    cur.execute("SELECT requests, failed_requests, tenant_id::text AS tenant FROM geo.geocode_call "
                "WHERE provider = %s", (STAGE,))
    assert tuple(cur.fetchone().values()) == (4, 1, "00000000-0000-0000-0000-000000000001")


def test_the_readers_run_on_the_v2_columns(cur):
    """Postgres checks every column name; the app of a row with the real column set wins over quality_signal."""
    from api.routers import admin_tenant360
    from api.services import llm_availability, platform_apps, platform_cost
    from services.reports import kpis, money_sources, planner
    cur.execute("INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, cost_usd, app, status) "
                "VALUES (%s, 'writer', 'm', '{\"app\": \"cis\"}', 0.5, 'tripplanner', 'ok')", (STAGE,))
    today = datetime.now(timezone.utc).date()

    async def run():
        conn = await asyncpg.connect(DSN)
        try:
            out = {}
            out["apps"] = await conn.fetch(platform_apps.TP_SPEND_TODAY_SQL, [STAGE], "tripplanner")
            out["cost"] = await conn.fetch(platform_cost.AI_SPEND_SQL, today, today + timedelta(days=1))
            out["kpi"] = await conn.fetch(kpis.LLM_DAY_SQL, datetime.now(timezone.utc) - timedelta(hours=1),
                                          datetime.now(timezone.utc) + timedelta(hours=1))
            out["money"] = await conn.fetch(money_sources.LLM_LEDGER_SQL, today, today + timedelta(days=1))
            out["planner"] = await conn.fetch(planner.SPEND_SQL, today, today, [STAGE], "tripplanner")
            out["avail"] = await conn.fetch(llm_availability.CALLS_SQL,
                                            datetime.now(timezone.utc) - timedelta(hours=1))
            out["t360"] = await conn.fetch(admin_tenant360._COST_BY_STAGE_SQL,
                                           "00000000-0000-0000-0000-000000000001")
            return out
        finally:
            await conn.close()
    out = asyncio.run(run())
    assert [r["calls"] for r in out["apps"]] == [1]
    assert any(r["app"] == "tripplanner" and r["cost_usd"] >= 0.5 for r in out["cost"])
    assert any(r["app"] == "tripplanner" for r in out["kpi"])
    assert any(r["app"] == "tripplanner" and r["stage"] == STAGE for r in out["money"])
    assert [r["calls"] for r in out["planner"]] == [1]
