"""AA-804 (UI part 1) — the read API of the admin "AI providers" page: GET /admin/ai-gateway/providers and
GET /admin/ai-gateway/trace (api/services/ai_gateway.py, api/routers/admin_ai_gateway.py).

No database: the builders are pure, the readers run on a fake pool that answers by SQL. The SQL itself runs on
PostgreSQL in tests/integration/test_aa804_ai_gateway_sql.py. Names below are invented test values.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.core import staff_authz
from api.routers import admin_ai_gateway as router_mod
from api.services import ai_gateway as gw

NOW = datetime(2026, 3, 14, 15, 30, tzinfo=timezone.utc)
DAY = datetime(2026, 3, 14, tzinfo=timezone.utc)
MONTH = datetime(2026, 3, 1, tzinfo=timezone.utc)
JOB = "6f1d2c3b-4a5e-4f60-8a7b-9c0d1e2f3a4b"


class FakePool:
    """Answers fetch / fetchrow by the first registered SQL fragment found in the query; records every call."""

    def __init__(self, answers: dict[str, object], fail: tuple[str, ...] = ()):
        self.answers = answers
        self.fail = fail
        self.calls: list[tuple[str, tuple]] = []

    def _answer(self, sql: str, args: tuple):
        self.calls.append((sql, args))
        for frag in self.fail:
            if frag in sql:
                raise OSError("db down")
        for frag, value in self.answers.items():
            if frag in sql:
                return value(*args) if callable(value) else value
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    async def fetch(self, sql, *args):
        return self._answer(sql, args)

    async def fetchrow(self, sql, *args):
        return self._answer(sql, args)

    @asynccontextmanager
    async def acquire(self):
        yield self

    def ran(self, frag: str) -> list[tuple]:
        return [args for sql, args in self.calls if frag in sql]


# ── route policy ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_routes_are_spend_read_and_admin_only():
    for path in ("/admin/ai-gateway/providers", "/admin/ai-gateway/trace", "/admin/ai-gateway/budgets"):
        assert staff_authz.POLICY[("GET", path)] == staff_authz.SPEND_READ
    assert staff_authz.SPEND_READ in staff_authz.ROLE_PERMS["admin"]
    for role in ("reviewer", "content", "viewer"):
        assert staff_authz.SPEND_READ not in staff_authz.ROLE_PERMS[role]


def test_routes_are_served_and_run_the_policy():
    from tests.unit.test_aa882_route_policy import _runs_policy, _staff_routes

    found = {(m, p): (r, inh) for m, p, r, inh in _staff_routes() if p.startswith("/admin/ai-gateway")}
    assert set(found) == {("GET", "/admin/ai-gateway/providers"), ("GET", "/admin/ai-gateway/trace"),
                          ("GET", "/admin/ai-gateway/budgets")}
    for (_m, p), (r, inh) in found.items():
        assert r.path == p and _runs_policy(r, inh, p)


# ── pure helpers ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_windows_are_utc_midnight_and_first_of_month():
    local = NOW.astimezone(timezone(timedelta(hours=7)))
    assert gw.day_and_month_start(local) == (DAY, MONTH)


@pytest.mark.parametrize("provider,expected", [
    ("bedrock-satellite", "bedrock"), ("bedrock-native", "bedrock"), ("typesafe", "jev"), ("Jev", "jev"),
    ("openai", "openai"), ("anthropic", "anthropic"), ("azure_openai", "azure_openai"), (None, None), ("", None)])
def test_budget_provider_follows_the_cost_guard_rules(provider, expected):
    assert gw.budget_provider(provider) == expected


LEDGER = [
    {"provider": "bedrock-satellite", "account": "llm-main", "today_usd": 1.5, "month_usd": 20.0, "month_calls": 400,
     "last_call_at": NOW - timedelta(minutes=5)},
    {"provider": "bedrock-native", "account": "llm-main", "today_usd": 0.5, "month_usd": 1.0, "month_calls": 10,
     "last_call_at": NOW - timedelta(hours=2)},
    {"provider": "openai", "account": "openai-direct", "today_usd": 0.2, "month_usd": 3.0, "month_calls": 30,
     "last_call_at": NOW - timedelta(hours=1)},
    {"provider": "typesafe", "account": None, "today_usd": 0.1, "month_usd": 0.9, "month_calls": 90,
     "last_call_at": NOW - timedelta(minutes=1)},
    {"provider": "openai", "account": None, "today_usd": 0.0, "month_usd": 0.4, "month_calls": 4,
     "last_call_at": NOW - timedelta(days=3)},
]


def test_ledger_by_account_leaves_jev_and_accountless_rows_out():
    by = gw.ledger_by_account(LEDGER)
    assert set(by) == {"llm-main", "openai-direct"}
    assert by["llm-main"]["month"] == 21.0 and by["llm-main"]["calls"] == 410
    assert by["llm-main"]["last"] == NOW - timedelta(minutes=5)


def test_ledger_by_budget_provider():
    by = gw.ledger_by_budget_provider(LEDGER)
    assert by["bedrock"]["today"] == 2.0 and by["jev"]["month"] == 0.9 and by["openai"]["calls"] == 34


def test_daily_cap_states():
    budgets = {"bedrock": {"per_day_usd": 4, "hard_stop": True, "alert_pct": 50}, "openai": {"per_day_usd": None}}
    cap = gw.daily_cap("bedrock", budgets, {"bedrock": 2.0})
    assert cap == {"budget_provider": "bedrock", "per_day_usd": 4.0, "spent_today_usd": 2.0, "used_pct": 50.0,
                   "hard_stop": True, "alert_pct": 50, "state": "alert"}
    assert gw.daily_cap("bedrock", budgets, {"bedrock": 4.0})["state"] == "cap_reached"
    assert gw.daily_cap("bedrock", budgets, {})["state"] == "ok"
    assert gw.daily_cap("openai", budgets, {}) is None          # a row without a daily figure
    assert gw.daily_cap("dfs", budgets, {}) is None             # no row
    assert gw.daily_cap(None, budgets, {}) is None


CONNS = [
    {"connection_key": "llm-main", "kind": "bedrock", "label": "Bedrock, test LLM account", "enabled": True,
     "data_scope": "aws_only", "account_id": "test-account-id"},
    {"connection_key": "openai-direct", "kind": "openai", "label": "OpenAI API (direct)", "enabled": True,
     "data_scope": "vendor_direct", "account_id": None},
    {"connection_key": "anthropic-direct", "kind": "anthropic", "label": "Anthropic API (direct)", "enabled": False,
     "data_scope": "vendor_direct", "account_id": None},
    {"connection_key": "llm-spare", "kind": "bedrock", "label": "Bedrock, spare", "enabled": True,
     "data_scope": "aws_only", "account_id": None},
]
HEALTH = {
    "llm-main": {"status": "open", "last_ok_at": "2026-03-14T10:00:00+00:00", "last_error_at": "x",
                 "last_error_class": "access_denied", "open_models": 1,
                 "models": [{"consecutive_failures": 3}, {"consecutive_failures": 0}]},
    "openai-direct": {"status": "ok", "open_models": 0, "models": []},
}


def test_llm_rows_status_spend_budget_and_order():
    monthly = {"openai-direct": {"state": "stopped", "monthly_budget_usd": 3.0, "month_spent_usd": 3.0}}
    budgets = {"bedrock": {"per_day_usd": 10, "hard_stop": True, "alert_pct": 80}}
    rows = gw.llm_rows(CONNS, HEALTH, gw.ledger_by_account(LEDGER), monthly, budgets, {"bedrock": 2.0})
    # enabled first; bedrock before openai; within bedrock the bigger month spend first; disabled last
    assert [r["connection_key"] for r in rows] == ["llm-main", "llm-spare", "openai-direct", "anthropic-direct"]
    main, spare, openai, anthropic = rows
    assert main["status"] == "open" and main["status_reason"] == "circuit_open"
    assert main["health"]["failing_models"] == 1 and main["health"]["models"] == 2
    assert main["spend"] == {"today_usd": 2.0, "month_usd": 21.0, "month_calls": 410,
                             "last_call_at": (NOW - timedelta(minutes=5)).isoformat()}
    assert main["daily_cap"]["spent_today_usd"] == 2.0 and main["key"] == "llm:llm-main"
    assert spare["status"] == "unknown" and spare["health"] is None and spare["spend"]["month_calls"] == 0
    assert openai["status"] == "cap_reached" and openai["status_reason"] == "monthly_budget_reached"
    assert openai["monthly_budget"]["state"] == "stopped" and openai["daily_cap"] is None
    assert anthropic["status"] == "disabled" and anthropic["enabled"] is False


@pytest.mark.parametrize("left,status,reason", [
    (None, "unknown", "no_topup_recorded"), (0.0, "cap_reached", "credit_used_up"),
    (-1.0, "cap_reached", "credit_used_up"),
    (2.5, "low", "below_threshold"), (10.0, "ok", None)])
def test_jev_status(left, status, reason):
    assert gw.jev_status(left, 3.0) == (status, reason)


def test_jev_row_health_and_degraded():
    est = {"estimated_left_usd": 12.0, "total_topped_up_usd": 20.0, "first_topup_on": "2026-01-02"}
    row = gw.jev_row(est, 3.0, {"today": 0.1, "month": 0.9, "calls": 90, "last": None},
                     {"calls": 10, "errors": 6, "cached": 2, "mean_latency_ms": 812.4, "last_at": NOW},
                     {"jev": {"per_day_usd": 2, "hard_stop": True, "alert_pct": 80}}, {"jev": 0.1})
    assert row["status"] == "degraded" and row["status_reason"] == "most_calls_failed"
    assert row["health"] == {"window_hours": 24, "calls": 10, "errors": 6, "error_pct": 60.0, "cached": 2,
                             "mean_latency_ms": 812, "last_at": NOW.isoformat()}
    assert row["balance"]["usd"] == 12.0 and row["balance"]["estimated"] is True
    assert row["daily_cap"]["per_day_usd"] == 2.0 and row["spend"]["month_usd"] == 0.9
    quiet = gw.jev_row(est, 3.0, {}, {"calls": 0, "errors": 0}, None, {})
    assert quiet["status"] == "ok" and quiet["health"]["error_pct"] is None and quiet["daily_cap"] is None


def test_dfs_row():
    spend = {"today_usd": 0.3, "month_usd": 4.2, "month_calls": 60, "month_live_calls": 20, "last_call_at": NOW}
    assert gw.dfs_row(None, 10.0, spend, None, {})["status"] == "unknown"
    low = gw.dfs_row({"balance_usd": 7.5, "currency": "USD", "fetched_at": NOW}, 10.0, spend, None, {})
    assert (low["status"], low["status_reason"]) == ("low", "below_threshold")
    assert low["balance"]["usd"] == 7.5 and low["balance"]["as_of"] == NOW.isoformat()
    ok = gw.dfs_row({"balance_usd": 50}, 10.0, spend, {"dfs": {"per_day_usd": 10, "hard_stop": True,
                                                                 "alert_pct": 80}}, {"dfs": 0.3})
    assert ok["status"] == "ok" and ok["live_calls_month"] == 20 and ok["daily_cap"]["used_pct"] == 3.0


@pytest.mark.parametrize("configured,used,status", [
    (False, 0, "disabled"), (True, 100, "ok"), (True, 2000, "low"), (True, 2500, "cap_reached")])
def test_geo_row(configured, used, status):
    row = gw.geo_row(configured, {"credits_today": used, "month_requests": 7}, 2500, None, {})
    assert row["status"] == status and row["credits"]["used_today"] == used and row["credits"]["daily_limit"] == 2500
    assert row["spend"]["month_calls"] == 7


# ── read_providers ─────────────────────────────────────────────────────────────────────────────────────────────────

def _provider_pool(fail=()):
    return FakePool({
        "FROM shared.llm_call_log": [dict(r) for r in LEDGER],
        "FROM shared.spend_budget": [{"provider": "bedrock", "per_day_usd": 10, "hard_stop": True, "alert_pct": 80},
                                     {"provider": "geocoder", "per_day_usd": 1, "hard_stop": True, "alert_pct": 80}],
        "FROM shared.decision_log": {"calls": 4, "errors": 0, "cached": 1, "mean_latency_ms": 300.0, "last_at": NOW},
        "FROM shared.dfs_balance_snapshot": {"balance_usd": 40.0, "currency": "USD", "fetched_at": NOW},
        "FROM shared.dfs_call_log": {"today_usd": 0.2, "month_usd": 2.0, "month_calls": 30, "month_live_calls": 10,
                                     "last_call_at": NOW},
        "FROM geo.geocode_call": {"credits_today": 120, "today_usd": 0.0, "month_usd": 0.0, "month_requests": 900,
                                  "last_call_at": NOW},
    }, fail=fail)


@pytest.fixture
def stubs(monkeypatch):
    async def conns(pool, now):
        return {"connections": [dict(c) for c in CONNS], "health": HEALTH, "monthly": {}}

    async def estimate(pool):
        return {"estimated_left_usd": 9.0, "total_topped_up_usd": 10.0, "first_topup_on": "2026-02-01",
                "spent_since_first_topup_usd": 1.0}

    from shared import jev_credit
    monkeypatch.setattr(gw, "_read_connections", conns)
    monkeypatch.setattr(jev_credit, "estimate", estimate)
    monkeypatch.setattr(gw, "geo_settings", lambda: (True, 2500))
    monkeypatch.setattr(gw, "dfs_threshold", lambda: 10.0)


def test_read_providers_lists_every_paid_source(stubs):
    pool = _provider_pool()
    out = asyncio.run(gw.read_providers(pool, now=NOW))
    keys = [p["key"] for p in out["providers"]]
    assert keys == ["llm:llm-main", "llm:llm-spare", "llm:openai-direct", "llm:anthropic-direct", "jev",
                    "dataforseo", "geoapify"]
    assert out["errors"] == [] and out["day_start"] == DAY.isoformat() and out["month_start"] == MONTH.isoformat()
    # the month window is [first of month, now); today's spend from UTC midnight
    assert pool.ran("FROM shared.llm_call_log") == [(DAY, MONTH, NOW)]
    assert pool.ran("FROM geo.geocode_call") == [(DAY, MONTH, NOW, "geoapify")]
    assert pool.ran("FROM shared.decision_log") == [(NOW - timedelta(hours=24), NOW)]
    by = {p["key"]: p for p in out["providers"]}
    assert by["jev"]["spend"]["month_usd"] == 0.9 and by["jev"]["balance"]["usd"] == 9.0
    assert by["dataforseo"]["balance"]["usd"] == 40.0 and by["dataforseo"]["spend"]["month_usd"] == 2.0
    assert by["geoapify"]["credits"]["used_pct"] == 4.8 and by["geoapify"]["daily_cap"]["per_day_usd"] == 1.0
    assert by["llm:llm-main"]["daily_cap"]["spent_today_usd"] == 2.0
    # the accountless OpenAI row is not lost: it is the unattributed line, and the totals match the ledger + DFS
    assert out["unattributed_llm"]["month_usd"] == 0.4 and out["unattributed_llm"]["month_calls"] == 4
    assert out["totals"]["month_usd"] == round(21.0 + 3.0 + 0.9 + 0.4 + 2.0, 4)


def test_one_failing_source_keeps_the_others(stubs):
    out = asyncio.run(gw.read_providers(_provider_pool(fail=("FROM shared.dfs_call_log",)), now=NOW))
    by = {p["key"]: p for p in out["providers"]}
    assert by["dataforseo"]["status"] == "unknown" and by["dataforseo"]["status_reason"] == "source_unreadable"
    assert "DataForSEO read failed (ref " in by["dataforseo"]["error"]
    assert by["jev"]["status"] == "ok" and by["geoapify"]["status"] == "ok"
    assert [e["source"] for e in out["errors"]] == ["dataforseo"]


def test_unreadable_ledger_blanks_spend_but_keeps_health(stubs):
    out = asyncio.run(gw.read_providers(_provider_pool(fail=("FROM shared.llm_call_log",)), now=NOW))
    main = out["providers"][0]
    assert main["status"] == "open" and main["spend"] is None and "LLM ledger read failed" in main["error"]
    assert out["unattributed_llm"] is None
    assert [e["source"] for e in out["errors"]] == ["llm_ledger"]


def test_unreadable_registry_gives_one_placeholder_row(stubs, monkeypatch):
    async def boom(pool, now):
        raise OSError("no registry")

    monkeypatch.setattr(gw, "_read_connections", boom)
    out = asyncio.run(gw.read_providers(_provider_pool(), now=NOW))
    assert out["providers"][0]["key"] == "llm" and out["providers"][0]["status"] == "unknown"
    assert [p["key"] for p in out["providers"][1:]] == ["jev", "dataforseo", "geoapify"]


# ── trace ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_parse_job_id():
    assert gw.parse_job_id(f"  {JOB.upper()} ") == JOB
    with pytest.raises(ValueError):
        gw.parse_job_id("job-1")


def test_trace_window():
    start = NOW - timedelta(hours=1)
    assert gw.trace_window({"started_at": None}, NOW) is None
    assert gw.trace_window({"started_at": start, "finished_at": NOW - timedelta(minutes=10)}, NOW) == (
        start, NOW - timedelta(minutes=9))
    assert gw.trace_window({"started_at": start, "finished_at": None}, NOW) == (start, NOW + timedelta(minutes=1))


def _job(started=True):
    return {"id": JOB, "kind": "invented_kind", "status": "succeeded", "created_at": NOW - timedelta(hours=2),
            "started_at": NOW - timedelta(hours=1) if started else None,
            "finished_at": NOW - timedelta(minutes=30) if started else None, "cost_usd": 0.5,
            "tour_id": None, "parent_job_id": None}


def _llm(at, cost, total_n=2, total_cost=0.3):
    return {"at": at, "stage": "invented_stage", "role": "writer", "model": "model-a", "provider": "bedrock-satellite",
            "account": "llm-main", "tokens_in": 10, "tokens_out": 20, "cache_read_tokens": None,
            "cache_write_tokens": None, "cost_usd": cost, "stop_reason": "end_turn", "fallback_used": False,
            "pricing_mode": "catalog", "shadow": False, "total_n": total_n, "total_cost": total_cost}


def _trace_pool(job, fail=()):
    t0 = NOW - timedelta(minutes=50)
    return FakePool({
        "FROM shared.job": job,
        "FROM shared.llm_call_log": [_llm(t0, 0.1), _llm(t0 + timedelta(minutes=5), 0.2)],
        "FROM shared.dfs_call_log": [{"at": t0 + timedelta(minutes=1), "endpoint": "search_volume", "keyword": "kw",
                                      "location_code": 1, "keyword_count": 3, "cost_usd": 0.05, "cache_hit": True,
                                      "fetched_live": False, "tour_id": None, "total_n": 1, "total_cost": 0.05}],
        "FROM geo.geocode_call": [],
        "FROM shared.decision_log": [{"at": t0 + timedelta(minutes=2), "stage": "invented_stage",
                                      "question_key": "q1", "subject_key": "s1", "mode": "shadow", "zone": "grey",
                                      "model": "jev", "latency_ms": 420, "cost_usd": 0.001, "error": "x" * 400,
                                      "outcome": None, "cached": False, "total_n": 1, "total_cost": 0.001}],
    }, fail=fail)


def test_trace_merges_the_ledgers_in_time_order():
    pool = _trace_pool(_job())
    out = asyncio.run(gw.trace_job(pool, JOB, now=NOW))
    assert [e["source"] for e in out["events"]] == ["llm", "dfs", "jev", "llm"]
    jev = out["events"][2]
    assert jev["outcome"] == "error" and len(jev["detail"]["error"]) == gw.ERROR_CHARS and jev["latency_ms"] == 420
    assert out["events"][1]["outcome"] == "cache"
    assert out["sources"]["llm"] == {"events": 2, "shown": 2, "cost_usd": 0.3, "truncated": False, "read": True,
                                     "error": None}
    assert out["sources"]["geocoder"]["events"] == 0 and out["sources"]["geocoder"]["read"] is True
    assert out["totals"] == {"events": 4, "cost_usd": 0.351}
    assert out["notes"] == ["refused_calls_not_logged"]
    # the two ledgers without a job index are bounded by the run window
    window = (NOW - timedelta(hours=1), NOW - timedelta(minutes=29))
    assert pool.ran("FROM shared.decision_log") == [(JOB, *window, gw.TRACE_LIMIT)]
    assert pool.ran("FROM geo.geocode_call") == [(JOB, *window, gw.TRACE_LIMIT)]
    assert out["window"] == {"from": window[0].isoformat(), "to": window[1].isoformat()}


def test_trace_counts_a_jev_call_once():
    """decide() logs one Jev call twice under the job: a ``typesafe`` llm_call_log row with the whole cost and one
    decision_log row per question with its share. The LLM read leaves the typesafe rows out, so the trace lists Jev
    from decision_log only and the total holds the Jev cost once."""
    sql = " ".join(gw.TRACE_LLM_SQL.split())
    assert "WHERE job_id = $1::uuid AND coalesce(provider, '') <> 'typesafe'" in sql
    t0 = NOW - timedelta(minutes=50)

    def jev(at, q):
        return {"at": at, "stage": "invented_stage", "question_key": q, "subject_key": "s1", "mode": "enforce",
                "zone": "accept", "model": "jev", "latency_ms": 300, "cost_usd": 0.002, "error": None,
                "outcome": None, "cached": False, "total_n": 2, "total_cost": 0.004}

    out = gw.build_trace(_job(), (t0, NOW), {
        "llm": ([_llm(t0, 0.1, total_n=1, total_cost=0.1)], None),
        "jev": ([jev(t0 + timedelta(minutes=1), "q1"), jev(t0 + timedelta(minutes=1), "q2")], None),
    }, gw.TRACE_LIMIT)
    assert [e["source"] for e in out["events"]] == ["llm", "jev", "jev"]
    assert all(e["provider"] != "typesafe" for e in out["events"] if e["source"] == "llm")
    assert out["sources"]["jev"]["cost_usd"] == 0.004 and out["sources"]["llm"]["cost_usd"] == 0.1
    assert out["totals"] == {"events": 3, "cost_usd": 0.104}


def test_trace_flags_truncation():
    out = gw.build_trace(_job(), None, {"llm": ([_llm(NOW, 0.1, total_n=900, total_cost=9.0)], None)}, 1)
    assert out["sources"]["llm"]["truncated"] is True and out["sources"]["llm"]["events"] == 900
    assert "truncated" in out["notes"] and "job_not_started" in out["notes"]


def test_trace_of_a_job_that_never_started_skips_the_unindexed_ledgers():
    pool = _trace_pool(_job(started=False))
    out = asyncio.run(gw.trace_job(pool, JOB, now=NOW))
    assert pool.ran("FROM shared.decision_log") == [] and pool.ran("FROM geo.geocode_call") == []
    assert out["sources"]["jev"]["read"] is False and out["window"] is None
    assert "job_not_started" in out["notes"]


def test_trace_source_failure_is_contained():
    out = asyncio.run(gw.trace_job(_trace_pool(_job(), fail=("FROM shared.dfs_call_log",)), JOB, now=NOW))
    dfs = out["sources"]["dfs"]
    assert dfs["read"] is False and "DataForSEO call log read failed" in dfs["error"]
    assert out["sources"]["llm"]["events"] == 2


def test_trace_of_an_unknown_job_is_none():
    assert asyncio.run(gw.trace_job(FakePool({"FROM shared.job": None}), JOB, now=NOW)) is None


# ── router ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def _request(pool):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))


def test_trace_route_answers_422_and_404():
    with pytest.raises(HTTPException) as e:
        asyncio.run(router_mod.get_trace(_request(FakePool({})), job_id="not-a-uuid"))
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        asyncio.run(router_mod.get_trace(_request(FakePool({"FROM shared.job": None})), job_id=JOB))
    assert e.value.status_code == 404
    out = asyncio.run(router_mod.get_trace(_request(_trace_pool(_job())), job_id=JOB))
    assert out["job"]["id"] == JOB
