"""AA-1003 R1 — job kind kpi_snapshot_daily, the KPI computers, the ICT periods and the sampler hook (no DB).

- ICT day bounds and the day the daily chain snapshots (00:30 UTC rule).
- Each computer binds the ICT bounds and turns rows into kpi_daily values (zero vs no value).
- The job writes every value it could compute, then raises when a part failed (the runner retries; never a silent
  success); a scheduled run that wrote everything chains report_generate for EX-04 and EX-06 once per day.
- The sampler part queues one snapshot per day, only while the kind is registered.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.jobs import report_jobs
from services.reports import kpis, periods
from shared.jobs.registry import NonRetryable, kinds

UTC = timezone.utc


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# ── periods ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_ict_day_bounds_are_17_utc_to_17_utc():
    start, end = periods.day_bounds(date(2026, 10, 28))
    assert start.astimezone(UTC) == _at("2026-10-27T17:00:00")
    assert end.astimezone(UTC) == _at("2026-10-28T17:00:00")


@pytest.mark.parametrize("now,day", [
    ("2026-10-29T00:29:00", date(2026, 10, 27)),     # before 00:30 UTC: still the day before
    ("2026-10-29T00:30:00", date(2026, 10, 28)),     # 07:30 ICT: yesterday (ICT) is snapshotted
    ("2026-10-29T18:00:00", date(2026, 10, 28)),     # 29/10 ICT has ended at 17:00 UTC but has not settled yet
])
def test_snapshot_day_follows_the_0030_utc_rule(now, day):
    assert periods.snapshot_day(_at(now)) == day


def test_parse_day_needs_a_real_date():
    assert periods.parse_day("2026-10-28") == date(2026, 10, 28)
    with pytest.raises(ValueError):
        periods.parse_day("28/10/2026")


def test_resolve_day_refuses_open_and_too_old_days():
    now = _at("2026-10-29T03:00:00")
    assert report_jobs.resolve_day({}, now) == date(2026, 10, 28)
    with pytest.raises(NonRetryable):
        report_jobs.resolve_day({"day": "2026-10-29"}, now)                 # ICT 29/10 has not ended
    with pytest.raises(NonRetryable):
        report_jobs.resolve_day({"day": "2026-07-01"}, now)                 # > 90 days
    with pytest.raises(NonRetryable):
        report_jobs.resolve_day({"day": "yesterday"}, now)


# ── computers ───────────────────────────────────────────────────────────────────────────────────────────────────

def _ctx(day=date(2026, 10, 28), latest=True):
    start, end = periods.day_bounds(day)
    return kpis.DayContext(day=day, start=start, end=end, latest=latest)


def test_job_fail_counts_the_ict_day():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=3)
    ctx = _ctx()
    out = asyncio.run(kpis.job_fail(conn, ctx))
    assert out == [kpis.KpiValue("K-JOB-FAIL", "app:cis", Decimal(3), "shared.job")]
    sql, start, end = conn.fetchval.call_args.args
    assert "status = 'failed'" in sql and "shared.job" in sql and (start, end) == (ctx.start, ctx.end)


def test_queue_age_has_no_value_on_a_day_without_jobs():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=None)
    assert asyncio.run(kpis.job_queue_age(conn, _ctx())) == []
    conn.fetchval = AsyncMock(return_value=42.26)
    assert asyncio.run(kpis.job_queue_age(conn, _ctx()))[0].value == Decimal("42.3")


def test_llm_day_totals_per_app_and_provider_and_fallback_ratio():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[
        {"app": "cis", "provider": "bedrock", "calls": 8, "fallback_calls": 1, "cost_usd": Decimal("1.50")},
        {"app": "tripplanner", "provider": "bedrock", "calls": 2, "fallback_calls": 0, "cost_usd": Decimal("0.25")},
        {"app": "cis", "provider": None, "calls": 0, "fallback_calls": 0, "cost_usd": None},
    ])
    out = {(v.kpi_id, v.scope): v.value for v in asyncio.run(kpis.llm_day(conn, _ctx()))}
    assert out[("K-LLM-COST-DAY", "all")] == Decimal("1.75")
    assert out[("K-LLM-COST-DAY", "app:cis")] == Decimal("1.50")
    assert out[("K-LLM-COST-DAY", "app:tripplanner")] == Decimal("0.25")
    assert out[("K-LLM-COST-DAY", "provider:bedrock")] == Decimal("1.75")
    assert out[("K-LLM-COST-DAY", "provider:unknown")] == Decimal(0)
    assert out[("K-LLM-FALLBACK-24H", "all")] == Decimal("0.1000")


def test_llm_day_without_calls_writes_zero_cost_and_no_fallback_ratio():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[])
    out = asyncio.run(kpis.llm_day(conn, _ctx()))
    assert [(v.kpi_id, v.scope, v.value) for v in out] == [("K-LLM-COST-DAY", "all", Decimal(0))]


def test_scope_values_fit_the_check():
    assert kpis.scope("provider", "bedrock/acc3 eu") == "provider:bedrock_acc3_eu"
    assert kpis.scope("app", "") == "app:unknown"


def test_tours_live_is_point_in_time_only():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=812)
    assert asyncio.run(kpis.tours_live(conn, _ctx(latest=False))) == []
    conn.fetchval.assert_not_awaited()
    v = asyncio.run(kpis.tours_live(conn, _ctx(latest=True)))[0]
    assert (v.kpi_id, v.value, v.data_as_of is not None) == ("K-CAT-LIVE", Decimal(812), True)
    assert "v_tour_liveness" in conn.fetchval.call_args.args[0]
    # the view's states are served / paused / retired (CIS 272); there is no 'live' state
    assert "state = 'served'" in conn.fetchval.call_args.args[0]


def test_reports_failed_covers_seven_days():
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=0)
    ctx = _ctx()
    asyncio.run(kpis.reports_failed(conn, ctx))
    _, start, end = conn.fetchval.call_args.args
    assert end - start == timedelta(days=7)


# ── the job ─────────────────────────────────────────────────────────────────────────────────────────────────────

class FakeConn:
    def __init__(self):
        self.written = []

    async def executemany(self, sql, rows):
        assert "shared.kpi_daily" in sql and "ON CONFLICT (kpi_id, day, scope)" in sql
        self.written += rows

    def transaction(self):
        @asynccontextmanager
        async def tx():
            yield
        return tx()


class FakePool:
    def __init__(self):
        self.conn = FakeConn()

    def acquire(self):
        conn = self.conn

        @asynccontextmanager
        async def ctx():
            yield conn
        return ctx()


def _job_ctx(payload, pool):
    ctx = MagicMock()
    ctx.payload, ctx.pool, ctx.job_id = payload, pool, "11111111-1111-1111-1111-111111111111"
    ctx.progress = AsyncMock()
    ctx.set_result = MagicMock()
    return ctx


def _fake_computers(monkeypatch, fail=None):
    async def ok(conn, ctx):
        return [kpis.KpiValue("K-JOB-FAIL", "app:cis", Decimal(1), "shared.job")]

    async def broken(conn, ctx):
        raise RuntimeError("relation does not exist")

    comps = {"job_fail": (ok, ("K-JOB-FAIL",))}
    if fail:
        comps["llm_day"] = (broken, ("K-LLM-COST-DAY",))
    monkeypatch.setattr(report_jobs, "COMPUTERS", comps)


def test_snapshot_writes_values_then_raises_when_a_part_failed(monkeypatch):
    _fake_computers(monkeypatch, fail=True)
    chain = AsyncMock()
    monkeypatch.setattr(report_jobs, "chain_reports", chain)
    pool = FakePool()
    ctx = _job_ctx({"trigger": "schedule", "day": periods.latest_closed_day().isoformat()}, pool)
    with pytest.raises(RuntimeError, match="llm_day"):
        asyncio.run(report_jobs.run_snapshot(ctx))
    assert [r[0] for r in pool.conn.written] == ["K-JOB-FAIL"]        # what could be computed is kept
    assert pool.conn.written[0][5] == "count"                         # the registry unit
    chain.assert_not_awaited()                                        # no report on a partial snapshot
    assert ctx.set_result.call_args.args[0]["errors"]


def test_scheduled_snapshot_chains_the_daily_reports(monkeypatch):
    _fake_computers(monkeypatch)
    chain = AsyncMock(return_value=[{"report_id": "EX-04", "run_id": "r", "created": True}])
    monkeypatch.setattr(report_jobs, "chain_reports", chain)
    day = periods.latest_closed_day()
    result = asyncio.run(report_jobs.run_snapshot(_job_ctx({"trigger": "schedule", "day": day.isoformat()},
                                                           FakePool())))
    assert result["rows_written"] == 1 and result["chained"][0]["report_id"] == "EX-04"
    assert chain.await_args.args[1] == day


def test_manual_snapshot_does_not_chain(monkeypatch):
    _fake_computers(monkeypatch)
    chain = AsyncMock()
    monkeypatch.setattr(report_jobs, "chain_reports", chain)
    asyncio.run(report_jobs.run_snapshot(_job_ctx({"day": periods.latest_closed_day().isoformat()}, FakePool())))
    chain.assert_not_awaited()


def test_chain_targets_the_daily_and_live_cis_reports(monkeypatch):
    seen = []

    async def fake_enqueue(pool, report, first, last, **kw):
        seen.append((report.id, first, last, kw["trigger"], kw["triggered_by"]))
        return {"run_id": "r"}
    monkeypatch.setattr(report_jobs, "enqueue_report_run", fake_enqueue)
    day = date(2026, 10, 28)
    asyncio.run(report_jobs.chain_reports(MagicMock(), day, "job"))
    assert sorted(s[0] for s in seen) == ["EX-04", "EX-06"]
    assert all(s[1:] == (day, day, "schedule", report_jobs.SCHEDULER) for s in seen)


def test_kinds_are_registered_background_and_cheap():
    k = kinds()
    for name in (report_jobs.SNAPSHOT_KIND, report_jobs.REPORT_KIND):
        assert name in k and k[name].background and not k[name].deploy_blocking
    assert k[report_jobs.REPORT_KIND].on_terminal is not None


# ── report_generate ─────────────────────────────────────────────────────────────────────────────────────────────

def _run_row(report_id="EX-04", status="queued"):
    return {"id": "22222222-2222-2222-2222-222222222222", "report_id": report_id, "status": status,
            "period_start": date(2026, 10, 28), "period_end": date(2026, 10, 28)}


def test_report_generate_renders_into_its_run(monkeypatch):
    calls = []
    monkeypatch.setattr(report_jobs.store, "get_run", AsyncMock(return_value=_run_row()))
    monkeypatch.setattr(report_jobs.store, "mark_running", AsyncMock(side_effect=lambda *a: calls.append("running")))
    monkeypatch.setattr(report_jobs.store, "finish_ok", AsyncMock(side_effect=lambda *a, **k: calls.append("ok")))
    monkeypatch.setattr(report_jobs.generate, "render", AsyncMock(
        return_value=report_jobs.generate.Rendered("s", {"x": 1}, "# md")))
    out = asyncio.run(report_jobs.run_report(_job_ctx({"run_id": _run_row()["id"]}, FakePool())))
    assert calls == ["running", "ok"] and out["report_id"] == "EX-04"


class TxConn:
    """Records the transaction outcome around enqueue_report_run."""

    def __init__(self):
        self.tx = []

    def transaction(self):
        conn = self

        @asynccontextmanager
        async def tx():
            try:
                yield
            except BaseException:
                conn.tx.append("rollback")
                raise
            conn.tx.append("commit")
        return tx()


def _tx_pool(conn):
    pool = MagicMock()

    @asynccontextmanager
    async def ctx():
        yield conn
    pool.acquire = MagicMock(side_effect=lambda: ctx())
    return pool


def test_enqueue_report_run_commits_row_and_job_together(monkeypatch):
    conn = TxConn()
    monkeypatch.setattr(report_jobs.store, "create_run", AsyncMock(return_value="run-1"))
    monkeypatch.setattr(report_jobs.store, "set_job", AsyncMock())
    monkeypatch.setattr(report_jobs, "enqueue", AsyncMock(return_value=("job-1", True)))
    ex04 = report_jobs.load().report("EX-04")
    out = asyncio.run(report_jobs.enqueue_report_run(_tx_pool(conn), ex04, date(2026, 10, 28), date(2026, 10, 28),
                                                     trigger="schedule", triggered_by=report_jobs.SCHEDULER))
    assert out["run_id"] == "run-1" and out["job_id"] == "job-1" and conn.tx == ["commit"]


def test_enqueue_failure_rolls_back_the_scheduled_row(monkeypatch):
    """A failed enqueue must not leave a scheduled row: the unique index would stop the retry from chaining again."""
    conn = TxConn()
    finished = AsyncMock()
    monkeypatch.setattr(report_jobs.store, "create_run", AsyncMock(return_value="run-1"))
    monkeypatch.setattr(report_jobs.store, "finish_failed", finished)
    monkeypatch.setattr(report_jobs, "enqueue", AsyncMock(side_effect=RuntimeError("db gone")))
    ex04 = report_jobs.load().report("EX-04")
    with pytest.raises(RuntimeError):
        asyncio.run(report_jobs.enqueue_report_run(_tx_pool(conn), ex04, date(2026, 10, 28), date(2026, 10, 28),
                                                   trigger="schedule", triggered_by=report_jobs.SCHEDULER))
    assert conn.tx == ["rollback"] and finished.await_count == 0


def test_report_generate_marks_the_run_failed_and_raises(monkeypatch):
    failed = AsyncMock()
    monkeypatch.setattr(report_jobs.store, "get_run", AsyncMock(return_value=_run_row()))
    monkeypatch.setattr(report_jobs.store, "mark_running", AsyncMock())
    monkeypatch.setattr(report_jobs.store, "finish_failed", failed)
    monkeypatch.setattr(report_jobs.generate, "render", AsyncMock(side_effect=RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        asyncio.run(report_jobs.run_report(_job_ctx({"run_id": _run_row()["id"]}, FakePool())))
    assert "boom" in failed.await_args.args[2]


def test_report_generate_refuses_a_report_without_generator(monkeypatch):
    monkeypatch.setattr(report_jobs.store, "get_run", AsyncMock(return_value=_run_row("FI-01")))
    monkeypatch.setattr(report_jobs.store, "finish_failed", AsyncMock())
    with pytest.raises(NonRetryable):
        asyncio.run(report_jobs.run_report(_job_ctx({"run_id": _run_row()["id"]}, FakePool())))


def test_terminal_hook_fails_a_run_left_running(monkeypatch):
    failed = AsyncMock()
    monkeypatch.setattr(report_jobs.store, "get_run", AsyncMock(return_value=_run_row(status="running")))
    monkeypatch.setattr(report_jobs.store, "finish_failed", failed)
    asyncio.run(report_jobs._report_terminal(FakePool(), {"payload": '{"run_id": "r"}', "status": "cancelled",
                                                          "error": None}))
    assert failed.await_args.args[2] == "job cancelled"


# ── sampler hook ────────────────────────────────────────────────────────────────────────────────────────────────

def test_sampler_part_queues_one_snapshot_per_day(monkeypatch):
    from api.core import ops_metrics_sampler as sampler

    enqueue = AsyncMock(return_value=("job-1", True))
    monkeypatch.setattr(report_jobs, "enqueue", enqueue)
    out: dict = {}
    ts = _at("2026-10-29T00:35:00")
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)                      # not queued yet today
    asyncio.run(sampler._reports_part(pool, {report_jobs.SNAPSHOT_KIND: object()}, ts, out))
    assert out["kpi_snapshot"] == {"job_id": "job-1", "day": "2026-10-28"}
    assert enqueue.await_args.kwargs["idempotency_key"] == "kpi_snapshot_daily:2026-10-28"
    out = {}
    asyncio.run(sampler._reports_part(pool, {}, ts, out))             # kind not registered: nothing
    assert out == {} and enqueue.await_count == 1
    pool.fetchval = AsyncMock(return_value=True)                       # queued earlier today: no INSERT
    asyncio.run(sampler._reports_part(pool, {report_jobs.SNAPSHOT_KIND: object()}, ts, out))
    assert out == {} and enqueue.await_count == 1
