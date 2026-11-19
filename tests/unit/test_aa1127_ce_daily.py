"""AA-1127 (C2 "spend truth"; tech lead decision S285 E2; CIS 461) — the daily Cost Explorer refresh: its settings,
window, the job and its schedule, the request count (the cost line), and the "stale" freshness block of the Spend page.
No DB, no AWS: Cost Explorer is a fake client; invented account ids only."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core import ops_metrics_sampler
from api.services import cost_explorer_daily as daily
from api.services import platform_cost
from services.jobs import cost_explorer_refresh_job as job
from shared import settings
from shared.aws_client import cost_explorer as ce
from shared.jobs.registry import NonRetryable, get_kind

TODAY = date(2026, 11, 19)
NOW = datetime(2026, 11, 19, 9, 30, tzinfo=timezone.utc)


# ── settings ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_readers_serve_the_seeded_defaults():
    """The compiled defaults once CIS 461 is in (tests/unit/test_aa1127_ce_daily_settings.py), the same fallback values
    before: behaviour never depends on whether the registry knows the keys yet."""
    assert daily.ALL == (daily.ENABLED, daily.RUN_HOUR_UTC, daily.STALE_AFTER_HOURS)
    assert daily.schedule_enabled() is True and daily.run_hour_utc() == 1 and daily.stale_after_hours() == 36.0


def test_the_readers_fall_back_on_a_broken_or_odd_value(monkeypatch):
    def boom(key, tenant_id=None):
        raise RuntimeError("registry down")

    monkeypatch.setattr(settings, "get", boom)
    assert daily.schedule_enabled() is True and daily.run_hour_utc() == 1 and daily.stale_after_hours() == 36.0
    odd = {daily.ENABLED: "yes", daily.RUN_HOUR_UTC: 24, daily.STALE_AFTER_HOURS: 0}
    monkeypatch.setattr(settings, "get", lambda key, tenant_id=None: odd[key])
    assert daily.schedule_enabled() is True and daily.run_hour_utc() == 1 and daily.stale_after_hours() == 36.0
    chosen = {daily.ENABLED: False, daily.RUN_HOUR_UTC: 6, daily.STALE_AFTER_HOURS: 48}
    monkeypatch.setattr(settings, "get", lambda key, tenant_id=None: chosen[key])
    assert daily.schedule_enabled() is False and daily.run_hour_utc() == 6 and daily.stale_after_hours() == 48.0


# ── window ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_window_is_the_month_to_date_mid_month():
    assert daily.refresh_window(TODAY) == (date(2026, 11, 1), date(2026, 11, 20))


def test_the_window_reaches_back_into_the_last_month_early_in_a_month():
    # the last days of October were still revised by Cost Explorer at the runs of 1-3 November: fetched once more
    start, end = daily.refresh_window(date(2026, 11, 2))
    assert end == date(2026, 11, 3)
    assert start == date(2026, 11, 2) - (platform_cost.FINAL_AFTER + platform_cost.CE_LAG) == date(2026, 10, 28)


def test_a_daily_run_leaves_no_day_stale_for_the_lazy_refresh():
    """Every day of the month gets a fetch after it became final, so By app never refetches on its own."""
    fetched: dict[str, str] = {}
    day = date(2026, 10, 25)
    while day <= TODAY:
        at = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=1, minutes=5)
        start, end = daily.refresh_window(day)
        for i in range((end - start).days):
            fetched[(start + timedelta(days=i)).isoformat()] = at.isoformat()
        day += timedelta(days=1)
    start, end = platform_cost.month_window(None, TODAY)
    stored = {"fetched_at": max(fetched.values()),
              "day_fetched_at": {d: f for d, f in fetched.items() if start.isoformat() <= d < end.isoformat()}}
    stale, why = platform_cost._is_stale(stored, start, end, NOW)
    assert stale is False, why


# ── refresh: both reads, the request count, partial failures ─────────────────────────────────────────────────────

class _Client:
    """A fake Cost Explorer client: ``pages`` answers per call, then the last one again."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    def get_cost_and_usage(self, **kwargs):
        self.calls += 1
        return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]


def _page(day, groups, token=None):
    out = {"ResultsByTime": [{"TimePeriod": {"Start": day, "End": day}, "Groups": groups}]}
    if token:
        out["NextPageToken"] = token
    return out


def test_fetch_all_accounts_cost_counts_every_request_pages_included(monkeypatch):
    group = {"Keys": ["Amazon Example Service", "Usage"],
             "Metrics": {"UnblendedCost": {"Amount": "1.5", "Unit": "USD"}}}
    clients = {"acc2": _Client([_page("2026-11-18", [group], token="t1"), _page("2026-11-18", [group])]),
               "acc3": _Client([_page("2026-11-18", [group])])}
    monkeypatch.setattr(ce, "_get_ce_client", lambda account: clients[account])
    monkeypatch.setattr(ce, "acc2_account_id", lambda: "1" * 12)
    monkeypatch.setitem(ce._SATELLITE_ACCOUNTS["acc3"], "account_id", "3" * 12)
    stats: dict = {}
    out = ce.fetch_all_accounts_cost("2026-11-18", "2026-11-19", stats=stats)
    assert stats == {"requests": 3} and out["errors"] == {}
    assert sorted(r["account_id"] for r in out["rows"]) == ["1" * 12, "3" * 12]
    # without stats the call is the one it always was
    clients["acc2"] = _Client([_page("2026-11-18", [group])])
    assert ce.fetch_all_accounts_cost("2026-11-18", "2026-11-19")["errors"] == {}


def _patch_reads(monkeypatch, *, service=None, service_error=None, app=None, app_error=None):
    def fetch_all(start, end, *, stats=None):
        stats["requests"] = stats.get("requests", 0) + 2
        if service_error:
            raise service_error
        return service

    monkeypatch.setattr(ce, "fetch_all_accounts_cost", fetch_all)
    monkeypatch.setattr(ce, "record_snapshot", AsyncMock(side_effect=lambda pool, rows: len(rows)))

    async def app_read(pool, start, end):
        if app_error:
            raise app_error
        return app

    monkeypatch.setattr(platform_cost, "refresh_app_snapshot", app_read)


def test_refresh_stores_both_and_reports_the_cost_line(monkeypatch):
    _patch_reads(monkeypatch, service={"rows": [{}, {}, {}], "errors": {}},
                 app={"row_count": 5, "ce_requests": 1, "est_cost_usd": 0.01})
    out = asyncio.run(daily.refresh(MagicMock(), date(2026, 11, 1), date(2026, 11, 20)))
    assert out == {"period_start": "2026-11-01", "period_end_exclusive": "2026-11-20", "row_count": 3,
                   "app_tag_row_count": 5, "ce_requests": 3, "est_cost_usd": 0.03, "errors": {}}


def test_a_failed_account_is_listed_by_key_and_class_never_by_its_text(monkeypatch):
    secret_text = "AssumeRole refused for an example role in account ACCOUNT-ID-SENTINEL"
    _patch_reads(monkeypatch, service={"rows": [{}], "errors": {"acc3": secret_text}},
                 app={"row_count": 2, "ce_requests": 1, "est_cost_usd": 0.01})
    out = asyncio.run(daily.refresh(MagicMock(), date(2026, 11, 1), date(2026, 11, 20)))
    assert out["errors"] == {"acc3": "CostExplorerUnavailable"} and out["row_count"] == 1
    assert "ACCOUNT-ID-SENTINEL" not in repr(out)


def test_one_read_failing_keeps_the_other(monkeypatch):
    _patch_reads(monkeypatch, service_error=ce.CostExplorerUnavailable("all accounts down"),
                 app={"row_count": 4, "ce_requests": 1, "est_cost_usd": 0.01})
    out = asyncio.run(daily.refresh(MagicMock(), date(2026, 11, 1), date(2026, 11, 20)))
    assert out["errors"] == {"service": "CostExplorerUnavailable"} and out["app_tag_row_count"] == 4
    assert out["ce_requests"] == 3   # the failed accounts were asked too
    _patch_reads(monkeypatch, service={"rows": [{}], "errors": {}}, app_error=ce.CostExplorerUnavailable("x"))
    out = asyncio.run(daily.refresh(MagicMock(), date(2026, 11, 1), date(2026, 11, 20)))
    assert out["errors"] == {"acc2_app_tag": "CostExplorerUnavailable"} and out["app_tag_row_count"] is None


def test_both_reads_failing_raises(monkeypatch):
    _patch_reads(monkeypatch, service_error=ce.CostExplorerUnavailable("down"), app_error=RuntimeError("down"))
    with pytest.raises(ce.CostExplorerUnavailable):
        asyncio.run(daily.refresh(MagicMock(), date(2026, 11, 1), date(2026, 11, 20)))


# ── the job ───────────────────────────────────────────────────────────────────────────────────────────────────────

class _Ctx:
    def __init__(self, payload):
        self.payload = payload
        self.pool = MagicMock()
        self.cost_usd = 0.0
        self.result = None

    def add_cost(self, usd):
        self.cost_usd += float(usd or 0.0)

    def set_result(self, result):
        self.result = result


def test_the_kind_is_a_single_attempt_background_kind():
    k = get_kind(job.KIND)
    assert k is not None and k.background and k.concurrency == 1 and k.max_attempts == 1
    assert not k.deploy_gate_exempt and not k.deploy_blocking


def test_a_run_reports_its_cost_and_result(monkeypatch):
    seen = {}

    async def refresh(pool, start, end):
        seen["window"] = (start, end)
        return {"row_count": 3, "app_tag_row_count": 5, "ce_requests": 3, "est_cost_usd": 0.03, "errors": {}}

    monkeypatch.setattr(daily, "refresh", refresh)
    ctx = _Ctx({"trigger": "schedule", "start": "2026-11-01", "end_exclusive": "2026-11-20"})
    out = asyncio.run(job.run(ctx))
    assert seen["window"] == (date(2026, 11, 1), date(2026, 11, 20))
    assert ctx.cost_usd == pytest.approx(0.03) and ctx.result == out
    assert out["kind"] == job.KIND and out["trigger"] == "schedule" and out["ce_requests"] == 3


def test_a_run_without_dates_uses_the_daily_window(monkeypatch):
    seen = {}

    async def refresh(pool, start, end):
        seen["window"] = (start, end)
        return {"ce_requests": 0, "est_cost_usd": 0.0, "errors": {}}

    monkeypatch.setattr(daily, "refresh", refresh)
    asyncio.run(job.run(_Ctx({})))
    assert seen["window"] == daily.refresh_window(datetime.now(timezone.utc).date())


@pytest.mark.parametrize("payload", [{"start": "2026-11-01"}, {"start": "first", "end_exclusive": "2026-11-20"},
                                     {"start": "2026-11-20", "end_exclusive": "2026-11-01"}])
def test_bad_payloads_are_non_retryable(payload):
    with pytest.raises(NonRetryable):
        job._window(payload, TODAY)


def test_both_reads_failing_fails_the_job_without_a_retry(monkeypatch):
    monkeypatch.setattr(daily, "refresh", AsyncMock(side_effect=ce.CostExplorerUnavailable("both down")))
    with pytest.raises(NonRetryable):
        asyncio.run(job.run(_Ctx({})))


# ── the schedule ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_daily_run_is_queued_once_a_day_from_the_setting_hour(monkeypatch):
    enqueue = AsyncMock(return_value=("j-1", True))
    monkeypatch.setattr(job, "enqueue", enqueue)
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)
    early = datetime(2026, 11, 19, 0, 59, tzinfo=timezone.utc)
    assert asyncio.run(job.enqueue_daily(pool, now=early))[1] is False
    pool.fetchval.assert_not_awaited()
    late = datetime(2026, 11, 19, 1, 5, tzinfo=timezone.utc)
    assert asyncio.run(job.enqueue_daily(pool, now=late)) == ("j-1", True, TODAY)
    assert enqueue.await_args.kwargs["idempotency_key"] == "cost_explorer_refresh:2026-11-19"
    assert enqueue.await_args.kwargs["created_by"] == "scheduler:aa-1127"
    assert enqueue.await_args.args[2] == {"trigger": "schedule", "start": "2026-11-01", "end_exclusive": "2026-11-20"}
    pool.fetchval = AsyncMock(return_value=True)
    assert asyncio.run(job.enqueue_daily(pool, now=late))[1] is False


def test_the_hour_and_the_switch_come_from_the_settings(monkeypatch):
    enqueue = AsyncMock(return_value=("j-2", True))
    monkeypatch.setattr(job, "enqueue", enqueue)
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)
    values = {daily.ENABLED: True, daily.RUN_HOUR_UTC: 6, daily.STALE_AFTER_HOURS: 36}
    monkeypatch.setattr(settings, "get", lambda key, tenant_id=None: values[key])
    assert asyncio.run(job.enqueue_daily(pool, now=datetime(2026, 11, 19, 5, 59, tzinfo=timezone.utc)))[1] is False
    assert asyncio.run(job.enqueue_daily(pool, now=datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)))[1] is True
    values[daily.ENABLED] = False
    assert asyncio.run(job.enqueue_daily(pool, now=datetime(2026, 11, 19, 23, tzinfo=timezone.utc)))[1] is False
    assert enqueue.await_count == 1


def test_the_sampler_tick_runs_the_part_under_its_own_budget():
    import inspect
    src = inspect.getsource(ops_metrics_sampler.sample_once)
    assert "from services.jobs.cost_explorer_refresh_job import sampler_part" in src
    assert '"cost_explorer_refresh_error", timeout=5.0)' in src


def test_the_sampler_part_queues_only_while_the_kind_is_registered(monkeypatch):
    called = AsyncMock(return_value=("j-1", True, TODAY))
    monkeypatch.setattr(job, "enqueue_daily", called)
    out: dict = {}
    asyncio.run(job.sampler_part(MagicMock(), {job.KIND: object()}, NOW, out))
    assert out == {"cost_explorer_refresh": {"job_id": "j-1", "day": "2026-11-19"}}
    out = {}
    asyncio.run(job.sampler_part(MagicMock(), {}, NOW, out))
    assert out == {} and called.await_count == 1
    called.return_value = (None, False, TODAY)
    asyncio.run(job.sampler_part(MagicMock(), {job.KIND: object()}, NOW, out))
    assert out == {}


# ── freshness (the Spend page's "stale" badge) ───────────────────────────────────────────────────────────────────

def test_fresh_when_both_newest_fetches_are_inside_the_window():
    out = daily.freshness_of(NOW - timedelta(hours=8), NOW - timedelta(hours=2), NOW, 36.0)
    assert out["stale"] is False and out["age_hours"] == 8.0
    assert out["oldest_newest_at"] == (NOW - timedelta(hours=8)).isoformat()
    assert out["daily_refresh"] == {"enabled": True, "hour_utc": 1}


def test_stale_when_one_snapshot_lags_or_is_empty():
    assert daily.freshness_of(NOW - timedelta(hours=40), NOW - timedelta(hours=1), NOW, 36.0)["stale"] is True
    empty = daily.freshness_of(None, NOW - timedelta(hours=1), NOW, 36.0)
    assert empty["stale"] is True and empty["age_hours"] is None and empty["service_fetched_at"] is None


def test_the_stale_window_is_the_setting(monkeypatch):
    pool = MagicMock()
    pool.fetchrow = AsyncMock(return_value={"service_fetched_at": NOW - timedelta(hours=30),
                                            "app_fetched_at": NOW - timedelta(hours=30)})
    assert asyncio.run(daily.freshness(pool, now=NOW))["stale"] is False
    monkeypatch.setattr(settings, "get", lambda key, tenant_id=None: {daily.ENABLED: True, daily.RUN_HOUR_UTC: 1,
                                                                      daily.STALE_AFTER_HOURS: 24}[key])
    out = asyncio.run(daily.freshness(pool, now=NOW))
    assert out["stale"] is True and out["stale_after_hours"] == 24.0
    assert pool.fetchrow.await_args.args[0] == daily.FRESHNESS_SQL


def test_a_failed_freshness_read_never_breaks_the_page():
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))
    out = asyncio.run(daily.freshness(pool, now=NOW))
    assert out["stale"] is None and out["error"] == "freshness unavailable"


def test_the_two_spend_reads_carry_the_freshness_block(monkeypatch):
    from api.routers import admin_llm_ops, admin_platform_ops
    from api.services import cost_ops

    block = {"stale": False, "age_hours": 3.0}
    monkeypatch.setattr(daily, "freshness", AsyncMock(return_value=block))
    monkeypatch.setattr(cost_ops, "cost_forecast", AsyncMock(return_value={"month": "2026-11"}))
    monkeypatch.setattr(platform_cost, "cost_by_app", AsyncMock(return_value={"month": "2026-11"}))
    monkeypatch.setattr(admin_platform_ops.platform_apps, "relay_authorization", lambda request: None)
    request = MagicMock()
    assert asyncio.run(admin_llm_ops.get_cost_ops_forecast(request, None)) == {"month": "2026-11",
                                                                              "ce_freshness": block}
    out = asyncio.run(admin_platform_ops.get_cost(request, None, True))
    assert out == {"month": "2026-11", "ce_freshness": block}
