"""AA-884 — cost per app: the CE App-tag parser, the month window, staleness, lazy refresh and the aggregation."""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services import platform_cost as pc
from shared.aws_client import cost_explorer as ce

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


def test_strip_app_tag():
    assert ce.strip_app_tag("App$cis") == "cis"
    assert ce.strip_app_tag("App$") == ""
    assert ce.strip_app_tag("cis") == "cis"


class StubCE:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_cost_and_usage(self, **kw):
        self.calls.append(kw)
        return self.pages.pop(0)


def _group(tag, service, amount):
    return {"Keys": [tag, service], "Metrics": {"UnblendedCost": {"Amount": str(amount), "Unit": "USD"}}}


def test_fetch_by_app_parses_paginates_and_strips(monkeypatch):
    stub = StubCE([
        {"ResultsByTime": [{"TimePeriod": {"Start": "2026-10-01", "End": "2026-10-02"},
                            "Groups": [_group("App$cis", "Amazon ECS", 1.25), _group("App$", "Amazon RDS", 2.5)]}],
         "NextPageToken": "p2"},
        {"ResultsByTime": [{"TimePeriod": {"Start": "2026-10-02", "End": "2026-10-03"},
                            "Groups": [_group("App$booking", "AWS Lambda", 0.1)]}]},
    ])
    monkeypatch.setattr(ce, "_get_ce_client", lambda account: stub)
    out = ce.fetch_acc2_cost_by_app("2026-10-01", "2026-10-03")
    assert out["requests"] == 2
    assert [(r["app_tag"], r["service"], r["amount_usd"]) for r in out["rows"]] == [
        ("cis", "Amazon ECS", 1.25), ("", "Amazon RDS", 2.5), ("booking", "AWS Lambda", 0.1)]
    first = stub.calls[0]
    assert first["GroupBy"] == [{"Type": "TAG", "Key": "App"}, {"Type": "DIMENSION", "Key": "SERVICE"}]
    assert first["Granularity"] == "DAILY" and stub.calls[1]["NextPageToken"] == "p2"
    assert all(r["account_id"] == ce.ACC2_ACCOUNT_ID for r in out["rows"])


def test_fetch_by_app_wraps_aws_errors(monkeypatch):
    from botocore.exceptions import ClientError

    class Failing:
        def get_cost_and_usage(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetCostAndUsage")

    monkeypatch.setattr(ce, "_get_ce_client", lambda account: Failing())
    with pytest.raises(ce.CostExplorerUnavailable, match="AccessDenied"):
        ce.fetch_acc2_cost_by_app("2026-10-01", "2026-10-02")


def test_month_window():
    today = date(2026, 10, 19)
    assert pc.month_window(None, today) == (date(2026, 10, 1), date(2026, 10, 20))
    assert pc.month_window("2026-09", today) == (date(2026, 9, 1), date(2026, 10, 1))
    assert pc.month_window("2025-12", today) == (date(2025, 12, 1), date(2026, 1, 1))
    with pytest.raises(ValueError):
        pc.month_window("2026-11", today)
    with pytest.raises(ValueError):
        pc.month_window("2026/10", today)


def test_staleness_rules():
    end = date(2026, 10, 20)
    assert pc._is_stale(None, end, NOW)[0] is True
    fresh = {"fetched_at": (NOW - timedelta(hours=2)).isoformat()}
    assert pc._is_stale(fresh, end, NOW)[0] is False
    old = {"fetched_at": (NOW - timedelta(hours=30)).isoformat()}
    assert pc._is_stale(old, end, NOW)[0] is True
    # A closed month fetched 3+ days after it ended is final, however old the fetch.
    final = {"fetched_at": datetime(2026, 9, 5, tzinfo=timezone.utc).isoformat()}
    assert pc._is_stale(final, date(2026, 9, 1), NOW)[0] is False


class FakeRedis:
    def __init__(self, locked=False):
        self.locked = locked
        self.deleted = []

    async def set(self, key, value, nx=False, ex=None):
        return not self.locked

    async def delete(self, key):
        self.deleted.append(key)


STORED = {
    "tags": {
        "cis": {"total_usd": 6.57, "bedrock_usd": 0.0, "services": [{"service": "Amazon ECS", "amount_usd": 6.57,
                                                                      "is_bedrock": False}]},
        "": {"total_usd": 83.72, "bedrock_usd": 1.0,
             "services": [{"service": "Amazon RDS", "amount_usd": 82.72, "is_bedrock": False},
                          {"service": "Amazon Bedrock", "amount_usd": 1.0, "is_bedrock": True}]},
        "shared": {"total_usd": 9.68, "bedrock_usd": 0.0, "services": []},
        "booking": {"total_usd": 1.08, "bedrock_usd": 0.0, "services": []},
        "weird": {"total_usd": 0.01, "bedrock_usd": 0.0, "services": []},
    },
    "total_usd": 101.06, "covered_from": "2026-10-01", "covered_to": "2026-10-19",
    "fetched_at": (NOW - timedelta(hours=1)).isoformat(), "oldest_fetched_at": (NOW - timedelta(hours=1)).isoformat(),
}

AI_ROWS = [
    {"app": "cis", "provider": "bedrock-satellite", "account": "acc3", "calls": 100, "cost_usd": 4.0,
     "billed_in_acc2": False},
    {"app": "cis", "provider": "bedrock", "account": "acc2", "calls": 50, "cost_usd": 0.5, "billed_in_acc2": True},
    {"app": "cis", "provider": "typesafe", "account": None, "calls": 10, "cost_usd": 0.2, "billed_in_acc2": False},
    {"app": "tripplanner", "provider": "bedrock-satellite", "account": "acc1", "calls": 5, "cost_usd": 0.3,
     "billed_in_acc2": False},
]


def _pool(ai_rows=AI_ROWS):
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=ai_rows)
    return pool


@pytest.mark.asyncio
async def test_cost_by_app_aggregates_without_double_counting(monkeypatch):
    monkeypatch.setattr(ce, "read_app_cost_range", AsyncMock(return_value=STORED))
    monkeypatch.setattr(ce, "read_cost_range", AsyncMock(return_value={
        "accounts": [{"account_id": ce.ACC3_ACCOUNT_ID, "bedrock_usd": 4.2}], "fetched_at": "x"}))
    pool = _pool()
    out = await pc.cost_by_app(pool, None, redis=None, now=NOW)
    rows = {r["app"]: r for r in out["apps"]}
    assert out["month"] == "2026-10" and out["period_end_exclusive"] == "2026-10-20"
    assert rows["cis"]["aws_usd"] == 6.57 and rows["cis"]["ai_usd"] == 4.7
    assert rows["cis"]["ai_in_aws_acc2_usd"] == 0.5
    assert rows["cis"]["total_usd"] == round(6.57 + 4.7 - 0.5, 4)
    assert rows["tripplanner"]["ai_usd"] == 0.3 and rows["tripplanner"]["aws_usd"] == 0.0
    assert rows["booking"]["aws_usd"] == 1.08
    assert rows["untagged"]["aws_usd"] == 83.72 and rows["untagged"]["kind"] == "untagged"
    assert rows["untagged"]["services"][0]["service"] == "Amazon RDS"
    assert rows["tag:shared"]["kind"] == "shared_tag" and rows["tag:weird"]["kind"] == "other_tag"
    assert out["aws"]["total_usd"] == 101.06 and out["aws"]["untagged_usd"] == 83.72
    assert out["llm_satellites"]["acc3"] == {"account_id": ce.ACC3_ACCOUNT_ID, "ce_bedrock_usd": 4.2,
                                             "llm_log_estimate_usd": 4.0}
    assert out["llm_satellites"]["acc1"]["ce_bedrock_usd"] is None
    assert out["refresh"]["attempted"] is False and out["refresh"]["stale"] is False
    # AI spend reads [month start, end) as dates; rolled days from llm_call_daily, the rest raw.
    sql, start, end = pool.fetch.await_args.args
    assert (start, end) == (date(2026, 10, 1), date(2026, 10, 20))
    assert "shared.llm_call_daily" in sql and "NOT EXISTS" in sql and "quality_signal ->> 'app'" in sql


@pytest.mark.asyncio
async def test_stale_month_refreshes_once_behind_the_lock(monkeypatch):
    stale = {**STORED, "fetched_at": (NOW - timedelta(hours=30)).isoformat()}
    reads = AsyncMock(side_effect=[stale, STORED])
    monkeypatch.setattr(ce, "read_app_cost_range", reads)
    monkeypatch.setattr(ce, "read_cost_range", AsyncMock(return_value=None))
    refresh = AsyncMock(return_value={"row_count": 40, "ce_requests": 1, "est_cost_usd": 0.01})
    monkeypatch.setattr(pc, "refresh_app_snapshot", refresh)
    redis = FakeRedis()
    out = await pc.cost_by_app(_pool([]), None, redis=redis, now=NOW)
    assert out["refresh"]["attempted"] is True and out["refresh"]["ok"] is True
    assert out["refresh"]["est_cost_usd"] == 0.01 and reads.await_count == 2
    assert refresh.await_args.args[1:] == (date(2026, 10, 1), date(2026, 10, 20))
    assert redis.deleted == [pc.REFRESH_LOCK_KEY]


@pytest.mark.asyncio
async def test_lock_taken_or_no_redis_or_disabled_means_no_ce_call(monkeypatch):
    stale = {**STORED, "fetched_at": (NOW - timedelta(hours=30)).isoformat()}
    monkeypatch.setattr(ce, "read_app_cost_range", AsyncMock(return_value=stale))
    monkeypatch.setattr(ce, "read_cost_range", AsyncMock(return_value=None))
    refresh = AsyncMock()
    monkeypatch.setattr(pc, "refresh_app_snapshot", refresh)
    out = await pc.cost_by_app(_pool([]), None, redis=FakeRedis(locked=True), now=NOW)
    assert out["refresh"]["attempted"] is False and "another refresh" in out["refresh"]["reason"]
    out = await pc.cost_by_app(_pool([]), None, redis=None, now=NOW)
    assert "no Redis" in out["refresh"]["reason"]
    out = await pc.cost_by_app(_pool([]), None, redis=FakeRedis(), refresh_if_stale=False, now=NOW)
    assert out["refresh"]["attempted"] is False and out["refresh"]["stale"] is True
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_refresh_keeps_stored_data(monkeypatch):
    stale = {**STORED, "fetched_at": (NOW - timedelta(hours=30)).isoformat()}
    monkeypatch.setattr(ce, "read_app_cost_range", AsyncMock(return_value=stale))
    monkeypatch.setattr(ce, "read_cost_range", AsyncMock(return_value=None))
    monkeypatch.setattr(pc, "refresh_app_snapshot", AsyncMock(side_effect=ce.CostExplorerUnavailable("throttled")))
    redis = FakeRedis()
    out = await pc.cost_by_app(_pool([]), None, redis=redis, now=NOW)
    assert out["refresh"]["ok"] is False and "throttled" in out["refresh"]["error"]
    assert out["aws"]["total_usd"] == 101.06 and redis.deleted == [pc.REFRESH_LOCK_KEY]


@pytest.mark.asyncio
async def test_no_data_at_all(monkeypatch):
    monkeypatch.setattr(ce, "read_app_cost_range", AsyncMock(return_value=None))
    monkeypatch.setattr(ce, "read_cost_range", AsyncMock(return_value=None))
    out = await pc.cost_by_app(_pool([]), "2026-09", redis=None, now=NOW)
    assert out["aws"]["has_data"] is False and out["aws"]["total_usd"] is None
    assert {r["app"] for r in out["apps"]} == {"cis", "booking", "tripplanner"}
    assert all(r["total_usd"] == 0.0 for r in out["apps"])
