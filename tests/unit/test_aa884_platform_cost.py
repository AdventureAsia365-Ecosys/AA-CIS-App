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


def _stored_days(start, end_exclusive, fetched):
    """A read_app_cost_range-shaped dict with every day of [start, end_exclusive) fetched at ``fetched``."""
    days = {(start + timedelta(days=i)).isoformat(): fetched.isoformat()
            for i in range((end_exclusive - start).days)}
    return {"fetched_at": fetched.isoformat(), "day_fetched_at": days}


def test_staleness_rules():
    start, end = date(2026, 10, 1), date(2026, 10, 20)
    assert pc._is_stale(None, start, end, NOW)[0] is True
    assert pc._is_stale(_stored_days(start, end, NOW - timedelta(hours=2)), start, end, NOW) == (False, "fresh")
    stale, why = pc._is_stale(_stored_days(start, end, NOW - timedelta(hours=30)), start, end, NOW)
    assert stale is True and "older than 24 h" in why
    # The last CE_LAG days of the window may be missing (CE lag): not a coverage gap.
    assert pc._is_stale(_stored_days(start, date(2026, 10, 18), NOW - timedelta(hours=2)), start, end, NOW)[0] is False
    # A closed month fetched 3+ days after it ended is final, however old the fetch.
    sep = _stored_days(date(2026, 9, 1), date(2026, 10, 1), datetime(2026, 10, 5, tzinfo=timezone.utc))
    final = (False, "final (fetched after the window closed)")
    assert pc._is_stale(sep, date(2026, 9, 1), date(2026, 10, 1), NOW) == final


def test_partial_coverage_is_stale_even_when_freshly_fetched():
    # Review finding (S243): POST /admin/cost-explorer/check stores its own 7-day window. Right after 248 (empty
    # table) those rows are all the month has and they are fresh — the month total must still count as stale.
    start, end = date(2026, 10, 1), date(2026, 10, 20)
    last7 = _stored_days(date(2026, 10, 12), date(2026, 10, 19), NOW - timedelta(minutes=5))
    stale, why = pc._is_stale(last7, start, end, NOW)
    assert stale is True and "11 day(s) missing (2026-10-01..2026-10-11)" in why
    # A gap in the middle (month fetched on the 5th, a 7-day check on the 19th) is caught too.
    gap = _stored_days(start, date(2026, 10, 6), NOW - timedelta(hours=1))
    gap["day_fetched_at"].update(last7["day_fetched_at"])
    stale, why = pc._is_stale(gap, start, end, NOW)
    assert stale is True and "6 day(s) missing (2026-10-06..2026-10-11)" in why
    # A 7-day check across a month boundary leaves the closed month partial as well.
    sep_tail = _stored_days(date(2026, 9, 26), date(2026, 10, 1), NOW - timedelta(hours=1))
    assert pc._is_stale(sep_tail, date(2026, 9, 1), date(2026, 10, 1), NOW)[0] is True


def test_only_not_final_days_age():
    # Days CE no longer revises keep their old fetch; only the recent, revisable days must be < 24 h old.
    start, end = date(2026, 10, 1), date(2026, 10, 20)
    stored = _stored_days(start, end, NOW - timedelta(hours=2))
    stored["day_fetched_at"]["2026-10-01"] = datetime(2026, 10, 6, tzinfo=timezone.utc).isoformat()   # final
    assert pc._is_stale(stored, start, end, NOW)[0] is False
    stored["day_fetched_at"]["2026-10-15"] = datetime(2026, 10, 16, tzinfo=timezone.utc).isoformat()  # not final
    stale, why = pc._is_stale(stored, start, end, NOW)
    assert stale is True and "1 day(s) not final" in why


class FakeRedis:
    def __init__(self, locked=False):
        self.locked = locked
        self.deleted = []
        self.data = {}
        self.ttl = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if key == pc.REFRESH_LOCK_KEY:
            return not self.locked
        self.data[key], self.ttl[key] = value, ex
        return True

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
    "day_fetched_at": _stored_days(date(2026, 10, 1), date(2026, 10, 20), NOW - timedelta(hours=1))["day_fetched_at"],
}


def _aged(hours):
    return {**STORED, **_stored_days(date(2026, 10, 1), date(2026, 10, 20), NOW - timedelta(hours=hours))}

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
    # S243 finding 4: one wording and one netting rule for every page.
    assert rows["untagged"]["label"] == "Not assigned to an app" and rows["untagged"]["hint"]
    assert rows["tag:shared"]["label"] == "Shared platform" and rows["tag:weird"]["label"] == "Other App tag: weird"
    assert rows["cis"]["label"] == "CIS (content + catalog)" and rows["cis"]["hint"] is None
    assert rows["cis"]["ai_outside_aws_usd"] == round(4.7 - 0.5, 4)
    t = out["totals"]
    assert t["ai_in_aws_acc2_usd"] == 0.5 and t["ai_outside_aws_usd"] == round(t["ai_usd"] - 0.5, 4)
    assert t["total_usd"] == round(101.06 + t["ai_outside_aws_usd"], 4)


@pytest.mark.asyncio
async def test_ai_spend_by_provider_groups_families_and_matches_the_ai_total():
    pool = _pool()
    out = await pc.ai_spend_by_provider(pool, now=NOW)
    assert out["by_provider"] == {"bedrock": 4.8, "other": 0.2}
    assert out["total_usd"] == 5.0 and out["month"] == "2026-10" and len(out["breakdown"]) == 4
    sql, start, end = pool.fetch.await_args.args
    assert sql == pc.AI_SPEND_SQL and (start, end) == (date(2026, 10, 1), date(2026, 10, 20))


def test_provider_family():
    assert [pc.provider_family(p) for p in ("bedrock", "bedrock-satellite", "openai", "jev", None, "typesafe")] == \
        ["bedrock", "bedrock", "openai", "jev", "other", "other"]


@pytest.mark.asyncio
async def test_stale_month_refreshes_once_behind_the_lock(monkeypatch):
    stale = _aged(30)
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
    marker = pc._attempt_key(date(2026, 10, 1), date(2026, 10, 20))
    assert redis.ttl[marker] == pc.ATTEMPT_MARKER_TTL_S and "40 rows" in redis.data[marker]


@pytest.mark.asyncio
async def test_empty_refresh_is_remembered_and_not_repeated(monkeypatch):
    # Review finding (S243): CE answers with no rows (early on the 1st, a month outside CE retention). Nothing gets
    # stored, so the next view is "no stored data" again — the attempt marker must stop a CE call on every view.
    monkeypatch.setattr(ce, "read_app_cost_range", AsyncMock(return_value=None))
    monkeypatch.setattr(ce, "read_cost_range", AsyncMock(return_value=None))
    refresh = AsyncMock(return_value={"row_count": 0, "ce_requests": 1, "est_cost_usd": 0.01})
    monkeypatch.setattr(pc, "refresh_app_snapshot", refresh)
    redis = FakeRedis()
    first = await pc.cost_by_app(_pool([]), "2026-09", redis=redis, now=NOW)
    assert first["refresh"]["attempted"] is True and first["refresh"]["row_count"] == 0
    for _ in range(3):
        again = await pc.cost_by_app(_pool([]), "2026-09", redis=redis, now=NOW)
        assert again["refresh"]["attempted"] is False and again["refresh"]["skipped"] == "recent_attempt"
        assert "already fetched" in again["refresh"]["reason"] and again["refresh"]["stale"] is True
    assert refresh.await_count == 1
    # Another window has its own marker.
    await pc.cost_by_app(_pool([]), "2026-08", redis=redis, now=NOW)
    assert refresh.await_count == 2


@pytest.mark.asyncio
async def test_partial_window_after_a_check_triggers_one_full_month_refresh(monkeypatch):
    last7 = {**STORED, **_stored_days(date(2026, 10, 12), date(2026, 10, 19), NOW - timedelta(minutes=5))}
    reads = AsyncMock(side_effect=[last7, STORED])
    monkeypatch.setattr(ce, "read_app_cost_range", reads)
    monkeypatch.setattr(ce, "read_cost_range", AsyncMock(return_value=None))
    refresh = AsyncMock(return_value={"row_count": 90, "ce_requests": 1, "est_cost_usd": 0.01})
    monkeypatch.setattr(pc, "refresh_app_snapshot", refresh)
    out = await pc.cost_by_app(_pool([]), None, redis=FakeRedis(), now=NOW)
    assert out["refresh"]["ok"] is True and "covers only part" in out["refresh"]["stale_reason"]
    assert refresh.await_args.args[1:] == (date(2026, 10, 1), date(2026, 10, 20))


@pytest.mark.asyncio
async def test_lock_taken_or_no_redis_or_disabled_means_no_ce_call(monkeypatch):
    stale = _aged(30)
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
    stale = _aged(30)
    monkeypatch.setattr(ce, "read_app_cost_range", AsyncMock(return_value=stale))
    monkeypatch.setattr(ce, "read_cost_range", AsyncMock(return_value=None))
    monkeypatch.setattr(pc, "refresh_app_snapshot", AsyncMock(side_effect=ce.CostExplorerUnavailable("throttled")))
    redis = FakeRedis()
    out = await pc.cost_by_app(_pool([]), None, redis=redis, now=NOW)
    assert out["refresh"]["ok"] is False and "throttled" in out["refresh"]["error"]
    assert out["aws"]["total_usd"] == 101.06 and redis.deleted == [pc.REFRESH_LOCK_KEY]
    marker = pc._attempt_key(date(2026, 10, 1), date(2026, 10, 20))
    assert redis.ttl[marker] == pc.ATTEMPT_MARKER_ERROR_TTL_S and "error" in redis.data[marker]


@pytest.mark.asyncio
async def test_no_data_at_all(monkeypatch):
    monkeypatch.setattr(ce, "read_app_cost_range", AsyncMock(return_value=None))
    monkeypatch.setattr(ce, "read_cost_range", AsyncMock(return_value=None))
    out = await pc.cost_by_app(_pool([]), "2026-09", redis=None, now=NOW)
    assert out["aws"]["has_data"] is False and out["aws"]["total_usd"] is None
    assert {r["app"] for r in out["apps"]} == {"cis", "booking", "tripplanner"}
    assert all(r["total_usd"] == 0.0 for r in out["apps"])
