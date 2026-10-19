"""AA-858 — GET /admin/overview/ops: CloudWatch health, contract-view activity, the cost card on AA-884's module,
the per-section cache (no AWS call without Redis, SET NX lock) and the route policy."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import boto3
import pytest
from botocore.stub import Stubber

from api.services import ops_overview
from api.services.ops_overview import activity, cache, cloudwatch, cost

NOW = datetime(2026, 10, 19, 12, 34, tzinfo=timezone.utc)
ENV = "dev"


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.deleted: list[str] = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttl[key] = ex
        return True

    async def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)


class BrokenRedis(FakeRedis):
    async def get(self, key):
        raise ConnectionError("redis down")


def _cw_client():
    return boto3.client("cloudwatch", region_name="us-west-1", aws_access_key_id="x", aws_secret_access_key="y",
                        endpoint_url="https://monitoring.us-west-1.amazonaws.com")


# ── CloudWatch ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_metric_queries_shape():
    qs = cloudwatch.metric_queries(cloudwatch.app_sources(ENV), ENV)
    assert len(qs) == 16
    ids = [q["Id"] for q in qs]
    assert len(set(ids)) == 16 and all(re.fullmatch(r"[a-z][a-zA-Z0-9_]*", i) for i in ids)
    by_id = {q["Id"]: q["MetricStat"] for q in qs}
    cis_req = by_id["q_cis_0_requests"]
    assert cis_req["Metric"]["Namespace"] == "AWS/ApiGateway" and cis_req["Metric"]["MetricName"] == "Count"
    assert cis_req["Metric"]["Dimensions"] == [{"Name": "ApiName", "Value": "aa-cis-dev-api"},
                                               {"Name": "Stage", "Value": "dev"}]
    assert cis_req["Period"] == 3600 and cis_req["Stat"] == "Sum"
    assert by_id["q_booking_0_p95"]["Metric"]["MetricName"] == "Latency"
    assert by_id["q_booking_0_p95"]["Period"] == 86400 and by_id["q_booking_0_p95"]["Stat"] == "p95"
    tp = by_id["q_tripplanner_1_throttles"]["Metric"]
    assert tp["Namespace"] == "AWS/Lambda" and tp["Dimensions"] == [
        {"Name": "FunctionName", "Value": "aa-tripplanner-dev-assembly"}]
    assert by_id["q_tripplanner_0_p95"]["Metric"]["MetricName"] == "Duration"


def test_window_is_hour_aligned_and_includes_the_current_hour():
    start, end = cloudwatch.window(NOW)
    assert end == datetime(2026, 10, 19, 13, 0, tzinfo=timezone.utc)
    assert start == end - timedelta(hours=24)


def _res(i, values, hours, status="Complete"):
    start, _ = cloudwatch.window(NOW)
    return {"Id": i, "StatusCode": status, "Timestamps": [start + timedelta(hours=h) for h in hours],
            "Values": values}


def test_parse_health_totals_hourly_and_tripplanner_sum():
    start, _ = cloudwatch.window(NOW)
    results = [
        _res("q_cis_0_requests", [100.0, 50.0], [0, 23]),
        _res("q_cis_0_errors", [2.0], [23]),
        _res("q_cis_0_client_errors", [7.0], [5]),
        _res("q_cis_0_p95", [310.0, 420.5], [0, 1]),
        _res("q_tripplanner_0_requests", [10.0], [3]),
        _res("q_tripplanner_1_requests", [5.0], [3]),
        _res("q_tripplanner_1_errors", [1.0], [3]),
        _res("q_tripplanner_1_throttles", [2.0], [4]),
        _res("q_tripplanner_0_p95", [800.0], [0]),
        _res("q_tripplanner_1_p95", [1200.0], [0]),
    ]
    apps = {a["app"]: a for a in cloudwatch.parse_health(results, cloudwatch.app_sources(ENV), start)}
    cis = apps["cis"]
    assert cis["requests"] == 150 and cis["errors"] == 2 and cis["client_errors"] == 7
    assert cis["error_rate_pct"] == 1.33 and cis["p95_ms"] == 420.5 and cis["throttles"] is None
    assert len(cis["hourly"]) == 24
    assert cis["hourly"][0] == {"hour": start.isoformat(), "requests": 100, "errors": 0}
    assert cis["hourly"][23]["requests"] == 50 and cis["hourly"][23]["errors"] == 2
    tp = apps["tripplanner"]
    assert tp["requests"] == 15 and tp["errors"] == 1 and tp["throttles"] == 2 and tp["p95_ms"] == 1200.0
    assert tp["hourly"][3] == {"hour": (start + timedelta(hours=3)).isoformat(), "requests": 15, "errors": 1}
    bk = apps["booking"]
    assert bk["has_data"] is False and bk["requests"] == 0 and bk["error_rate_pct"] is None


def test_get_metric_data_paginates_and_merges_by_id():
    client = _cw_client()
    start, end = cloudwatch.window(NOW)
    with Stubber(client) as stub:
        stub.add_response("get_metric_data", {
            "MetricDataResults": [{"Id": "q_cis_0_requests", "StatusCode": "PartialData",
                                   "Timestamps": [start], "Values": [3.0]}], "NextToken": "t2"})
        stub.add_response("get_metric_data", {
            "MetricDataResults": [{"Id": "q_cis_0_requests", "StatusCode": "Complete",
                                   "Timestamps": [start + timedelta(hours=1)], "Values": [4.0]}]},
            expected_params={"MetricDataQueries": [], "StartTime": start, "EndTime": end,
                             "ScanBy": "TimestampAscending", "NextToken": "t2"})
        results, calls = cloudwatch.get_metric_data(client, [], start, end)
    assert calls == 2
    assert len(results) == 1 and results[0]["Values"] == [3.0, 4.0]


def _alarm(name, state, ts=None):
    return {"AlarmName": name, "StateValue": state, "AlarmDescription": f"desc {name}",
            "StateUpdatedTimestamp": ts or NOW}


def test_fetch_alarms_maps_prefixes_skips_target_tracking_and_paginates():
    client = _cw_client()
    with Stubber(client) as stub:
        stub.add_response("describe_alarms", {"MetricAlarms": [
            _alarm("aa-cis-dev-api-5xx", "ALARM"),
            _alarm("aa-cis-dev-rds-cpu-high", "OK"),
            _alarm("TargetTracking-service/aa-cis-dev-cluster/aa-cis-dev-api-AlarmLow-x", "ALARM"),
            _alarm("aa-booking-dev-no-healthy-task", "INSUFFICIENT_DATA"),
        ], "NextToken": "n2"})
        stub.add_response("describe_alarms", {"MetricAlarms": [
            _alarm("aa-tripplanner-dev-browse-errors", "OK"),
            _alarm("aa-identity-dev-breakglass-token-issued", "ALARM"),
            _alarm("someone-elses-alarm", "ALARM"),
        ], "CompositeAlarms": []})
        out, calls = cloudwatch.fetch_alarms(client, ENV)
    assert calls == 2
    assert out["cis"]["total"] == 2 and [f["name"] for f in out["cis"]["firing"]] == ["aa-cis-dev-api-5xx"]
    assert out["cis"]["firing"][0]["since"] == NOW.isoformat()
    assert out["booking"] == {"total": 1, "no_data": 1, "firing": []}
    assert out["tripplanner"]["total"] == 1 and not out["tripplanner"]["firing"]
    assert out["shared"]["total"] == 1 and out["shared"]["firing"][0]["name"].startswith("aa-identity-dev-")


@pytest.mark.parametrize("app,alarms,expected", [
    ({"has_data": True, "requests": 10, "errors": 0}, {"firing": [{"name": "a"}, {"name": "b"}]}, "alarm"),
    ({"has_data": False}, None, "unknown"),
    ({"has_data": True, "requests": 100, "errors": 3, "error_rate_pct": 3.0}, {"firing": []}, "warn"),
    ({"has_data": True, "requests": 1000, "errors": 1, "error_rate_pct": 0.1}, None, "ok"),
    ({"has_data": True, "requests": 10, "errors": 0, "throttles": 2}, None, "warn"),
    ({"has_data": True, "requests": 0, "errors": 0}, None, "ok"),
])
def test_app_state(app, alarms, expected):
    state, reason = cloudwatch.app_state(app, alarms)
    assert state == expected and reason


def test_collect_health_access_denied_is_not_permitted():
    client = _cw_client()
    with Stubber(client) as stub:
        stub.add_client_error("get_metric_data", service_error_code="AccessDenied", http_status_code=403)
        out = cloudwatch.collect_health(NOW, ENV, client)
    assert out["status"] == "unavailable" and out["reason"] == "not_permitted" and out["message"]


def test_collect_health_alarms_denied_keeps_metrics():
    client = _cw_client()
    with Stubber(client) as stub:
        stub.add_response("get_metric_data", {"MetricDataResults": [
            {"Id": "q_cis_0_requests", "StatusCode": "Complete", "Timestamps": [NOW], "Values": [9.0]}]})
        stub.add_client_error("describe_alarms", service_error_code="AccessDenied", http_status_code=403)
        out = cloudwatch.collect_health(NOW, ENV, client)
    assert out["status"] == "ok" and out["alarms_error"] == "not_permitted"
    cis = next(a for a in out["apps"] if a["app"] == "cis")
    assert cis["requests"] == 9 and cis["state"] == "ok" and cis["alarms"] is None
    assert [a["app"] for a in out["apps"]] == ["cis", "tripplanner", "booking"]


def test_collect_health_firing_alarm_sets_state():
    client = _cw_client()
    with Stubber(client) as stub:
        stub.add_response("get_metric_data", {"MetricDataResults": []})
        stub.add_response("describe_alarms", {"MetricAlarms": [_alarm("aa-booking-dev-api-5xx", "ALARM")]})
        out = cloudwatch.collect_health(NOW, ENV, client)
    bk = next(a for a in out["apps"] if a["app"] == "booking")
    assert bk["state"] == "alarm" and bk["state_reason"] == "1 alarm firing"


def test_environment_defaults(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    assert cloudwatch.environment() == "dev"
    monkeypatch.setenv("ENVIRONMENT", "prod")
    assert cloudwatch.environment() == "prod"


# ── cache ───────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_redis_means_no_aws_call():
    compute = AsyncMock(return_value={"status": "ok"})
    out = await cache.cached_section(None, "health", 300, compute, needs_cache=True)
    assert out["status"] == "unavailable" and out["reason"] == "cache_offline"
    compute.assert_not_awaited()
    out = await cache.cached_section(BrokenRedis(), "health", 300, compute, needs_cache=True)
    assert out["reason"] == "cache_offline"
    compute.assert_not_awaited()


@pytest.mark.asyncio
async def test_db_section_without_redis_is_computed():
    compute = AsyncMock(return_value={"status": "ok", "x": 1})
    out = await cache.cached_section(None, "booking", 300, compute, needs_cache=False)
    assert out == {"status": "ok", "x": 1, "_cached": False}
    out = await cache.cached_section(BrokenRedis(), "booking", 300, compute, needs_cache=False)
    assert out["x"] == 1 and compute.await_count == 2


@pytest.mark.asyncio
async def test_miss_then_hit_and_lock_released():
    redis = FakeRedis()
    compute = AsyncMock(return_value={"status": "ok", "n": 5})
    first = await cache.cached_section(redis, "health", 300, compute, needs_cache=True)
    second = await cache.cached_section(redis, "health", 300, compute, needs_cache=True)
    assert first["_cached"] is False and second == {"status": "ok", "n": 5, "_cached": True}
    assert compute.await_count == 1
    key = cache.section_key("health")
    assert redis.ttl[key] == 300 and redis.ttl[f"{key}:last"] == cache.LAST_GOOD_TTL_S
    assert redis.deleted == [f"{key}:lock"]


@pytest.mark.asyncio
async def test_failure_is_cached_briefly_and_never_raises():
    redis = FakeRedis()
    out = await cache.cached_section(redis, "health", 300, AsyncMock(side_effect=RuntimeError("boom")),
                                     needs_cache=True)
    assert out["status"] == "unavailable" and out["reason"] == "error"
    key = cache.section_key("health")
    assert redis.ttl[key] == cache.ERROR_TTL_S and f"{key}:last" not in redis.store


@pytest.mark.asyncio
async def test_lock_taken_serves_last_good_or_refreshing():
    redis = FakeRedis()
    key = cache.section_key("health")
    redis.store[f"{key}:lock"] = "1"
    compute = AsyncMock(return_value={"status": "ok"})
    out = await cache.cached_section(redis, "health", 300, compute, needs_cache=True)
    assert out["reason"] == "refreshing"
    redis.store[f"{key}:last"] = json.dumps({"status": "ok", "n": 1})
    out = await cache.cached_section(redis, "health", 300, compute, needs_cache=True)
    assert out == {"status": "ok", "n": 1, "_cached": True, "_stale": True}
    compute.assert_not_awaited()


# ── activity (contract views) ───────────────────────────────────────────────────────────────────────────────────────

def _pool(*fetch_results):
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=list(fetch_results))
    return pool


@pytest.mark.asyncio
async def test_tripplanner_activity_series_totals_and_change():
    today = NOW.date()
    rows = [
        {"day": today, "metric": "planner_session_started", "n": 4},
        {"day": today - timedelta(days=1), "metric": "trip_started", "n": 2},
        {"day": today - timedelta(days=8), "metric": "planner_session_started", "n": 2},
        {"day": today, "metric": "a_future_metric", "n": 99},
    ]
    searches = [{"day": today, "n": 3}]
    pool = _pool(rows, searches)
    out = await activity.tripplanner_activity(pool, NOW)
    assert out["status"] == "ok" and len(out["days"]) == 14
    assert out["days"][-1] == {"day": today.isoformat(), "planner_session_started": 4, "trip_started": 0,
                               "trip_edited": 0, "inquiry_sent": 0, "ai_search": 3}
    assert out["totals_7d"]["planner_session_started"] == 4 and out["prev_7d"]["planner_session_started"] == 2
    assert out["change_pct"]["planner_session_started"] == 100.0 and out["change_pct"]["trip_started"] is None
    sql, start, end = pool.fetch.await_args_list[0].args
    assert "tripplanner.v_ops_traffic_daily" in sql and (start, end) == (today - timedelta(days=13), today)
    assert pool.fetch.await_args_list[1].args[3] == "tp_search_embed"


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.InsufficientPrivilegeError,
                                 asyncpg.exceptions.InvalidSchemaNameError])
async def test_missing_contract_view_is_unavailable(exc):
    out = await activity.tripplanner_activity(_pool(exc("nope")), NOW)
    assert out["status"] == "unavailable" and out["reason"] == "contract_view_missing"
    assert "TripPlanner" in out["message"]
    out = await activity.booking_funnel(_pool(exc("nope")), NOW)
    assert out["reason"] == "contract_view_missing" and "Booking" in out["message"]


@pytest.mark.asyncio
async def test_booking_funnel_weeks_conversion_status_and_attention():
    monday = NOW.date() - timedelta(days=NOW.date().weekday())
    weekly = [
        {"week": monday, "metric": "inquiry_received", "n": 4},
        {"week": monday, "metric": "trip_case_created", "n": 4},
        {"week": monday - timedelta(weeks=1), "metric": "proposal_sent", "n": 2},
        {"week": monday - timedelta(weeks=1), "metric": "confirmed", "n": 1},
        {"week": monday - timedelta(weeks=20), "metric": "confirmed", "n": 50},   # outside the 8 weeks
    ]
    attention = [{"metric": "inquiry_parked", "n": 1}]
    statuses = [{"status": "new_inquiry", "n": 3}, {"status": "completed", "n": 5}, {"status": "cancelled", "n": 1}]
    pool = _pool(weekly, attention, statuses)
    out = await activity.booking_funnel(pool, NOW)
    assert out["status"] == "ok" and len(out["weeks"]) == 8
    assert out["weeks"][-1]["week_start"] == monday.isoformat() and out["weeks"][-1]["inquiry_received"] == 4
    assert out["totals"] == {"inquiry_received": 4, "trip_case_created": 4, "proposal_sent": 2, "confirmed": 1,
                             "cancelled": 0}
    assert out["conversion_pct"] == {"inquiry_to_case": 100.0, "case_to_proposal": 50.0,
                                     "proposal_to_confirmed": 50.0}
    assert out["open_cases"] == 3 and out["inbox_attention"] == {"parked": 1, "failed": 0}
    statuses_out = {s["status"]: s for s in out["cases_by_status"]}
    assert statuses_out["completed"]["closed"] is True and statuses_out["new_inquiry"]["n"] == 3
    assert pool.fetch.await_args_list[0].args[1] == monday - timedelta(weeks=7)


# ── cost card (AA-884 module) ───────────────────────────────────────────────────────────────────────────────────────

def _cost(month, rows, fetched="2026-10-19T03:00:00+00:00"):
    return {"month": month, "period_start": f"{month}-01", "period_end_exclusive": "2026-10-20",
            "aws": {"has_data": True, "fetched_at": fetched, "covered_to": "2026-10-18"},
            "apps": rows, "refresh": {"attempted": False, "ok": None, "stale": False, "reason": "fresh", "x": 1}}


def _app(app, kind, aws=0.0, ai=0.0, ai_acc2=0.0, services=()):
    return {"app": app, "kind": kind, "label": app, "aws_usd": aws, "ai_usd": ai, "ai_in_aws_acc2_usd": ai_acc2,
            "total_usd": round(aws + ai - ai_acc2, 4), "services": [{"service": s, "amount_usd": v}
                                                                   for s, v in services]}


@pytest.mark.asyncio
async def test_cost_card_is_built_on_platform_cost(monkeypatch):
    cur = _cost("2026-10", [
        _app("cis", "app", aws=6.57, ai=4.7, ai_acc2=0.5),
        _app("tripplanner", "app", ai=0.3),
        _app("booking", "app", aws=1.08),
        _app("tag:shared", "shared_tag", aws=9.68, services=[("Amazon VPC", 9.0)]),
        _app("tag:platform", "shared_tag"),
        _app("untagged", "untagged", aws=83.72, services=[(f"S{i}", 10.0 - i) for i in range(7)]),
    ])
    prev = _cost("2026-09", [_app("cis", "app", aws=20.0), _app("tag:platform", "shared_tag", aws=0.01)])
    calls = []

    async def fake(pool, month=None, *, redis=None, refresh_if_stale=True, now=None):
        calls.append((month, redis, refresh_if_stale))
        return cur if month is None else prev

    monkeypatch.setattr(cost.platform_cost, "cost_by_app", fake)
    redis = FakeRedis()
    out = await cost.cost_card(MagicMock(), redis, NOW)
    assert calls == [(None, redis, True), ("2026-09", redis, True)]
    rows = {r["key"]: r for r in out["rows"]}
    assert rows["cis"]["label"] == "CIS (content + catalog)"
    assert rows["cis"]["ai_outside_aws_usd"] == 4.2 and rows["cis"]["total_usd"] == 10.77
    assert rows["cis"]["prev_total_usd"] == 20.0 and rows["booking"]["prev_total_usd"] == 0.0
    assert rows["untagged"]["label"] == "Not assigned to an app" and len(rows["untagged"]["top_services"]) == 5
    assert rows["untagged"]["hint"] and rows["tag:shared"]["top_services"][0]["service"] == "Amazon VPC"
    assert rows["tag:platform"]["total_usd"] == 0 and rows["tag:platform"]["prev_total_usd"] == 0.01
    assert out["totals"]["total_usd"] == round(10.77 + 0.3 + 1.08 + 9.68 + 83.72, 2)
    assert out["totals"]["prev_total_usd"] == 20.01
    assert out["refresh"] == {"attempted": False, "ok": None, "stale": False, "reason": "fresh"}
    assert out["month"] == "2026-10" and out["prev_month"] == "2026-09"


@pytest.mark.asyncio
async def test_cost_card_drops_empty_non_app_rows_and_survives_prev_failure(monkeypatch):
    cur = _cost("2026-10", [_app("booking", "app"), _app("tag:identity", "shared_tag")])

    async def fake(pool, month=None, **kw):
        if month is not None:
            raise RuntimeError("db")
        return cur

    monkeypatch.setattr(cost.platform_cost, "cost_by_app", fake)
    out = await cost.cost_card(MagicMock(), None, NOW)
    assert [r["key"] for r in out["rows"]] == ["booking"]
    assert out["rows"][0]["prev_total_usd"] is None and out["totals"]["prev_total_usd"] is None


def test_prev_month_across_the_year():
    assert cost._prev_month(date(2026, 1, 5)) == "2025-12"


# ── the whole endpoint ──────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_isolates_a_failing_section(monkeypatch):
    monkeypatch.setattr(activity, "tripplanner_activity", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(activity, "booking_funnel", AsyncMock(return_value={"status": "ok", "weeks": []}))
    monkeypatch.setattr(cost, "cost_card", AsyncMock(return_value={"status": "ok", "rows": []}))
    health = MagicMock(side_effect=AssertionError("no AWS call without Redis"))
    monkeypatch.setattr(cloudwatch, "collect_health", health)
    out = await ops_overview.build_ops_overview(MagicMock(), None, NOW)
    assert out["tripplanner"]["status"] == "unavailable" and out["tripplanner"]["reason"] == "error"
    assert out["booking"]["status"] == "ok" and out["cost"]["status"] == "ok"
    assert out["health"]["reason"] == "cache_offline"
    assert set(out["sources"]) == {"health", "tripplanner", "booking", "cost"} and out["_cached"] is False
    health.assert_not_called()


_SECRET = "aa858-test-admin-secret"


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_overview

    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(app.state, "pool", MagicMock(), raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    monkeypatch.setattr(admin_overview, "build_ops_overview",
                        AsyncMock(return_value={"generated_at": NOW.isoformat(), "_cached": False}))
    return TestClient(app)


@pytest.mark.parametrize("role,status", [("admin", 200), ("reviewer", 403), ("content", 403), (None, 403)])
def test_route_policy(client, role, status):
    headers = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "aa858"}
    if role:
        headers["x-admin-role"] = role
    resp = client.get("/admin/overview/ops", headers=headers)
    assert resp.status_code == status, resp.text
    if status == 200:
        assert resp.json()["generated_at"] == NOW.isoformat()


def test_route_needs_the_secret(client):
    assert client.get("/admin/overview/ops", headers={"x-admin-role": "admin"}).status_code in (401, 403)


def test_policy_entry_is_ops_read_and_not_a_machine_route():
    from api.core.staff_authz import MACHINE_ROUTES, OPS_READ, POLICY

    assert POLICY[("GET", "/admin/overview/ops")] == OPS_READ
    assert ("GET", "/admin/overview/ops") not in MACHINE_ROUTES
