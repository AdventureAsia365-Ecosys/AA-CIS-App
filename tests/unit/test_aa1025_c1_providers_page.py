"""AA-1025 slice C1 (S263 gateway plan) — the Providers page: latency / error rate per connection, the CRM AI and
Bedrock Guardrails rows (crm.ops.v1 with the caller's token), the static Mapbox row, the real switch of Jev /
DataForSEO / Geoapify (api/services/ai_gateway.py, api/routers/admin_ai_gateway.py, api/services/platform_apps.py).

No database and no network: the builders are pure, the CRM reads are fakes. The stats SQL runs on PostgreSQL in
tests/integration/test_aa804_ai_gateway_sql.py. Names below are invented test values.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api.routers import admin_ai_gateway as router_mod
from api.services import ai_gateway as gw
from api.services import platform_apps
from shared.llm_client import connections

NOW = datetime(2026, 3, 14, 15, 30, tzinfo=timezone.utc)
TODAY = datetime(2026, 3, 14, tzinfo=timezone.utc)


# ── stats ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_stats_sql_counts_refused_apart_and_reads_both_windows():
    for sql in (gw.LLM_STATS_SQL, gw.DFS_STATS_SQL, gw.GEO_STATS_SQL):
        for w in ("24h", "7d"):
            for col in ("calls", "errors", "refused", "p50", "p95"):
                assert f"AS {col}_{w}" in sql, (col, w)
    # the 7 d range is the scan; 24 h is a FILTER inside it
    assert "created_at >= $2 AND created_at < $3" in gw.LLM_STATS_SQL
    assert "status IS NOT NULL" in gw.LLM_STATS_SQL           # ledger v2 rows only
    assert "status IN ('error', 'billed_unusable', 'truncated')" in gw.LLM_STATS_SQL
    assert "percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FILTER (WHERE created_at >= $1 AND " \
           "status <> 'refused')" in gw.LLM_STATS_SQL
    assert "NOT cache_hit" in gw.DFS_STATS_SQL and "failed_requests" in gw.GEO_STATS_SQL


def test_window_stats_and_with_stats():
    r = {"calls_24h": 8, "errors_24h": 2, "refused_24h": 1, "p50_24h": 812.4, "p95_24h": 2400.6,
         "calls_7d": 0, "errors_7d": 0, "refused_7d": 3, "p50_7d": None, "p95_7d": None}
    row = gw.with_stats({"key": "llm:x"}, r, sampled_7d=True)
    assert row["stats"]["24h"] == {"calls": 8, "errors": 2, "refused": 1, "error_pct": 25.0, "latency_p50_ms": 812,
                                   "latency_p95_ms": 2401}
    assert row["stats"]["7d"] == {"calls": 0, "errors": 0, "refused": 3, "error_pct": None, "latency_p50_ms": None,
                                  "latency_p95_ms": None, "sampled": True}
    assert (row["error_pct"], row["latency_p50_ms"], row["latency_p95_ms"]) == (25.0, 812, 2401)
    empty = gw.with_stats({"key": "llm:y"}, None)
    assert empty["stats"]["24h"]["calls"] == 0 and empty["error_pct"] is None
    gone = gw.stats_unreadable({"key": "llm:z"})
    assert gone["stats"] is None and gone["latency_p95_ms"] is None


@pytest.mark.parametrize("values,q,expected", [
    ([], 0.5, None), ([7.0], 0.95, 7.0), ([100.0, 200.0, 900.0], 0.5, 200.0), ([100.0, 200.0, 900.0], 0.95, 830.0),
    ([4.0, 1.0, 3.0, 2.0], 0.5, 2.5)])
def test_percentile_matches_percentile_cont(values, q, expected):
    got = gw.percentile(values, q)
    assert got == (None if expected is None else pytest.approx(expected))


# ── CRM rows ───────────────────────────────────────────────────────────────────────────────────────────────────────

USAGE = [
    {"day": "2026-03-14", "stage": "crm_draft", "model": "probe-model", "calls": 3, "failed_calls": 1,
     "cost_usd": "0.0300"},
    {"day": "2026-03-02", "stage": "crm_draft", "model": "probe-model", "calls": 5, "failed_calls": 0,
     "cost_usd": "0.0500"},
    {"day": "2026-03-14", "stage": "crm_scrub", "model": "apply_guardrail", "calls": 4, "failed_calls": 0,
     "cost_usd": "0.0004"},
    {"day": "2026-03-14", "stage": "crm_draft", "model": "probe-model", "calls": "x", "cost_usd": "bad"},
    "not a row",
]


def _call(minutes_ago: float, stage: str, status: str, ms: int) -> dict:
    return {"at": (NOW - timedelta(minutes=minutes_ago)).isoformat(), "stage": stage, "status": status,
            "latency_ms": ms}


CALLS = [
    _call(10, "crm_draft", "ok", 1000), _call(20, "crm_draft", "error", 3000), _call(30, "crm_draft", "refused", 0),
    _call(60 * 30, "crm_draft", "truncated", 2000),            # 30 h ago: 7 d only
    _call(15, "crm_scrub", "ok", 80), _call(16, "crm_scrub", "ok", 120),
    _call(60 * 24 * 9, "crm_draft", "error", 10),               # older than 7 d: never counted
    {"at": "not a time", "stage": "crm_draft", "status": "ok"},
]


def test_crm_usage_spend_splits_the_guardrail_stage():
    spend = gw.crm_usage_spend(USAGE, TODAY)
    assert spend["llm"]["month"] == pytest.approx(0.08) and spend["llm"]["today"] == pytest.approx(0.03)
    assert spend["llm"]["calls"] == 8
    assert spend["guard"] == {"today": pytest.approx(0.0004), "month": pytest.approx(0.0004), "calls": 4,
                              "last": None}
    assert gw.crm_usage_spend("not a list", TODAY)["llm"]["calls"] == 0


def test_crm_call_stats_follow_the_ledger_rules():
    st = gw.crm_call_stats(CALLS, NOW)
    llm, guard = st["llm"], st["guard"]
    assert (llm["calls_24h"], llm["errors_24h"], llm["refused_24h"]) == (2, 1, 1)
    assert (llm["calls_7d"], llm["errors_7d"], llm["refused_7d"]) == (3, 2, 1)
    assert llm["p50_24h"] == pytest.approx(2000.0) and llm["p95_7d"] == pytest.approx(2900.0)
    assert llm["last"] == NOW - timedelta(minutes=10)
    assert guard["calls_24h"] == 2 and guard["errors_24h"] == 0 and guard["p50_24h"] == pytest.approx(100.0)


@pytest.mark.parametrize("problem,reason", [
    ({"status": "unavailable", "reason": "cognito_session_required"}, "crm_sign_in_needed"),
    ({"status": "refused", "http_status": 403}, "crm_refused"),
    ({"status": "unavailable", "reason": "ConnectError"}, "crm_unreachable"),
    ({"status": "error", "http_status": 500}, "crm_unreachable")])
def test_crm_problem_reason(problem, reason):
    assert gw.crm_problem_reason(problem) == reason


def test_crm_status():
    assert gw.crm_status({"calls": 4, "errors": 3}, None) == ("degraded", "most_calls_failed")
    assert gw.crm_status({"calls": 4, "errors": 2}, None) == ("ok", None)
    assert gw.crm_status({"calls": 0, "errors": 0}, None) == ("unknown", None)       # "No recent calls"
    assert gw.crm_status({}, {"status": "refused"}) == ("unknown", "crm_refused")


def test_crm_rows_from_a_read_and_from_a_problem():
    guard_reg = connections.BUILTIN_PROVIDERS["bedrock-guardrails"]             # seeded off in the registry
    crm = {"usage": USAGE, "calls": CALLS, "problem": None, "sampled": True}
    llm, guard = gw.crm_rows(crm, NOW, guard_reg)
    assert llm["key"] == "crm:llm" and llm["group"] == "llm" and llm["app"] == "crm"
    assert llm["status"] == "ok" and llm["switch"] == "crm_settings" and llm["switchable"] is False
    assert llm["spend"]["month_usd"] == pytest.approx(0.08) and llm["spend"]["month_calls"] == 8
    assert llm["spend"]["last_call_at"] == (NOW - timedelta(minutes=10)).isoformat()
    assert llm["error_pct"] == 50.0 and llm["stats"]["7d"]["sampled"] is True
    assert guard["key"] == "guardrails" and guard["group"] == "guard" and guard["provider"] == "bedrock_guardrails"
    # the registry facts, but never its switch: the CRM's client reads its own settings
    assert guard["enabled"] is True and guard["switchable"] is False and guard["from_registry"] is True
    assert guard["failure_domain"] == "bedrock_guardrails:bedrock-guardrails:aws_only"
    assert guard["connection_key"] == "bedrock-guardrails" and guard["status"] == "ok"
    assert guard["spend"]["today_usd"] == pytest.approx(0.0004)

    down = gw.crm_rows({"problem": {"status": "unavailable", "reason": "cognito_session_required"}}, NOW, None)
    for row in down:
        assert row["status"] == "unknown" and row["status_reason"] == "crm_sign_in_needed"
        assert row["spend"] is None and row["stats"] is None
    assert down[1]["from_registry"] is False


def test_read_crm_asks_both_reads_and_flags_a_sample():
    asked = {}

    async def usage(start, end):
        asked["usage"] = (start, end)
        return {"rows": USAGE}, {}

    async def calls(params):
        asked["calls"] = params
        return {"rows": CALLS, "next_cursor": "2026-03-07T00:00:00+00:00~abc"}, {}

    out = asyncio.run(gw._read_crm(NOW, usage, calls))
    assert asked["usage"] == (date(2026, 3, 1), date(2026, 3, 14))
    assert asked["calls"] == {"from": "2026-03-07", "to": "2026-03-14", "limit": gw.CRM_CALLS_PAGE}
    assert out["problem"] is None and out["sampled"] is True and out["usage"] == USAGE

    async def refused(*_a):
        return None, {"status": "refused", "http_status": 403}

    assert asyncio.run(gw._read_crm(NOW, usage, refused))["problem"]["status"] == "refused"
    assert asyncio.run(gw._read_crm(NOW, None, None))["problem"]["reason"] == "not_asked"


def test_mapbox_row_is_static_and_never_needs_a_look():
    row = gw.mapbox_row()
    assert row["status"] == "untracked" and row["spend"] is None and row["switchable"] is False
    assert row["usage_url"].startswith("https://") and row["app"] == "tripplanner" and row["stats"] is None


# ── the switch of Jev / DataForSEO / Geoapify ─────────────────────────────────────────────────────────────────────

def test_registry_switch_wins_over_the_row_status():
    jev = connections.BUILTIN_PROVIDERS["typesafe"]
    off = connections.ProviderConnection(**{**jev.__dict__, "enabled": False, "access_state": "ok",
                                            "last_probe_at": "2026-03-14T01:00:00+00:00"})
    low = {"key": "jev", "enabled": True, "status": "low", "status_reason": "below_threshold"}
    out = gw._with_registry(low, off)
    assert (out["status"], out["status_reason"]) == ("disabled", "switched_off_in_registry")
    assert out["switchable"] is True and out["last_probe_at"] == "2026-03-14T01:00:00+00:00"
    assert gw._with_registry(low, jev)["status"] == "low"                         # on: the row's own status
    unread = {"key": "jev", "enabled": True, "status": "unknown", "status_reason": "source_unreadable"}
    assert gw._with_registry(unread, off)["status_reason"] == "source_unreadable"
    assert gw._with_registry(low, None)["switchable"] is False                     # no registry row: no switch
    assert gw.registry_view(connections.BUILTIN_PROVIDERS["bedrock-guardrails"])["switchable"] is False


def test_switched_off_geoapify_says_so():
    geo = gw.geo_row(True, {"credits_today": 10}, 2500, None, {})
    off = connections.ProviderConnection(**{**connections.BUILTIN_PROVIDERS["geoapify"].__dict__, "enabled": False})
    out = gw._with_registry(geo, off)
    assert out["enabled"] is False and out["configured"] is True
    assert (out["status"], out["status_reason"]) == ("disabled", "switched_off_in_registry")


# ── route + CRM relay ──────────────────────────────────────────────────────────────────────────────────────────────

def test_providers_route_relays_the_callers_token_to_both_crm_reads(monkeypatch):
    seen = {}

    async def fake_read(pool, *, crm_usage_rows, crm_calls):
        seen["usage"] = await crm_usage_rows(date(2026, 3, 1), date(2026, 3, 14))
        seen["calls"] = await crm_calls({"limit": 1})
        return {"providers": []}

    async def usage_rows(authorization, start, end):
        return {"auth": authorization, "range": (start, end)}, {}

    async def llm_calls(authorization, params):
        return {"auth": authorization, "params": params}, {}

    monkeypatch.setattr(gw, "read_providers", fake_read)
    monkeypatch.setattr(platform_apps, "relay_authorization", lambda request: "Bearer probe-token")
    monkeypatch.setattr(platform_apps, "crm_llm_usage_rows", usage_rows)
    monkeypatch.setattr(platform_apps, "crm_llm_calls", llm_calls)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=object())))
    assert asyncio.run(router_mod.get_providers(request)) == {"providers": []}
    assert seen["usage"][0] == {"auth": "Bearer probe-token", "range": (date(2026, 3, 1), date(2026, 3, 14))}
    assert seen["calls"][0] == {"auth": "Bearer probe-token", "params": {"limit": 1}}


def test_crm_llm_usage_rows_sends_the_whole_day_range(monkeypatch):
    asked = {}

    async def ops_get(authorization, path, params=None, client=None):
        asked.update(path=path, params=params, authorization=authorization)
        return {"rows": []}, {}

    monkeypatch.setattr(platform_apps, "_crm_ops_get", ops_get)
    body, problem = asyncio.run(platform_apps.crm_llm_usage_rows("Bearer t", date(2026, 3, 1), date(2026, 3, 14)))
    assert body == {"rows": []} and problem == {}
    assert asked == {"path": "/llm/usage", "params": {"from": "2026-03-01", "to": "2026-03-14"},
                     "authorization": "Bearer t"}
