"""AA-1003 R2 — DATA-01 probe verdicts (CloudWatch Logs) and TP-03 inquiry handoff (contract views). No DB, no AWS.

- probes: the verdict line parser, one run per log stream, the last run of a probe decides, a probe with no run is
  `missing` and a run without a verdict `no_verdict`; refusals and old days raise LogsUnavailable.
- handoff: per UTC day counts from the two views, the gap, the "persists at the next hourly check" alert rule, the
  bell throttle, the hourly job and the TP-03 report.
- KPI computers: values written, Unavailable recorded without failing the snapshot job.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from services.jobs import report_jobs, report_ops_jobs
from services.reports import handoff, kpis, periods, probes, registry

UTC = timezone.utc


def _ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


def _ev(stream: str, at: str, msg: str) -> dict:
    return {"logStreamName": stream, "timestamp": _ms(at), "message": msg}


# ── probes ──────────────────────────────────────────────────────────────────────────────────────────────────────

def test_parse_verdict_reads_key_values_as_ints():
    v = probes.parse_verdict("SCRIPT verdict violations=0 errors=2 list=a.b,c.d pending=1")
    assert v == {"violations": 0, "errors": 2, "list": "a.b,c.d", "pending": 1}
    assert probes.parse_verdict("SCRIPT metric name=x n=1") is None


def test_runs_are_grouped_per_stream_and_the_last_run_decides():
    events = [
        _ev("migrate/migrate/t1", "2026-10-29T04:44:00", "run-script: file=db/scripts/s248_tour_deletion_probe.py "
                                                         "sha256=abcdef012345"),
        _ev("migrate/migrate/t1", "2026-10-29T04:46:00", "SCRIPT verdict violations=3 errors=0 list=x pending=0"),
        # a manual re-run later the same day passes: it decides
        _ev("migrate/migrate/t2", "2026-10-29T09:00:00", "run-script: file=db/scripts/s248_tour_deletion_probe.py "
                                                         "sha256=abcdef012345"),
        _ev("migrate/migrate/t2", "2026-10-29T09:02:00", "SCRIPT verdict violations=0 errors=0 list= pending=0"),
        # the freshness task died before its verdict
        _ev("migrate/migrate/t3", "2026-10-29T05:14:00", "run-script: file=db/scripts/aa991_freshness_probe.py "
                                                         "sha256=abcdef012345"),
        # another run-script (not a probe) is ignored
        _ev("migrate/migrate/t4", "2026-10-29T06:00:00", "run-script: file=db/scripts/aa838_capacity_probe.py "
                                                         "sha256=abcdef012345"),
    ]
    results = {r.probe.key: r for r in probes.summarize(probes.group_runs(events))}
    assert results["liveness"].status == "passed" and results["liveness"].value == 0 and results["liveness"].runs == 2
    assert results["freshness"].status == "no_verdict" and results["freshness"].value is None


def test_failed_and_missing_probes():
    events = [
        _ev("migrate/migrate/t9", "2026-10-29T05:14:00", "run-script: file=db/scripts/aa991_freshness_probe.py "
                                                         "sha256=abcdef012345"),
        _ev("migrate/migrate/t9", "2026-10-29T05:20:00", "SCRIPT verdict stale=2 stale_aged=7 errors=0 list=a"),
    ]
    results = {r.probe.key: r for r in probes.summarize(probes.group_runs(events))}
    assert results["freshness"].status == "failed" and results["freshness"].value == 7
    assert results["liveness"].status == "missing"
    # errors alone make a probe red, as the Infra gate decides
    assert probes.judge(probes.PROBES[0], {"violations": 0, "errors": 1})[0] == "failed"
    assert probes.judge(probes.PROBES[0], {"violations": "x", "errors": 0})[0] == "failed"


class _Denied(Exception):
    response = {"Error": {"Code": "AccessDeniedException"}}


def test_read_events_refuses_and_explains():
    client = MagicMock()
    client.filter_log_events.side_effect = _Denied("no")
    now = datetime(2026, 10, 30, 2, 0, tzinfo=UTC)
    with pytest.raises(probes.LogsUnavailable) as e:
        probes.read_events(date(2026, 10, 29), client, now)
    assert e.value.reason == "not_permitted"
    with pytest.raises(probes.LogsUnavailable) as e:
        probes.read_events(date(2026, 9, 1), client, now)          # older than the 30-day retention
    assert e.value.reason == "too_old"


def test_read_events_pages_through_the_ict_day():
    client = MagicMock()
    client.filter_log_events.side_effect = [{"events": [{"m": 1}], "nextToken": "n"}, {"events": [{"m": 2}]}]
    events, window = probes.read_events(date(2026, 10, 29), client, datetime(2026, 10, 30, 2, tzinfo=UTC))
    assert events == [{"m": 1}, {"m": 2}]
    first = client.filter_log_events.call_args_list[0].kwargs
    start, end = periods.day_bounds(date(2026, 10, 29))
    assert first["startTime"] == int(start.timestamp() * 1000) and first["endTime"] == int(end.timestamp() * 1000) - 1
    assert first["logStreamNamePrefix"] == "migrate/migrate/" and "SCRIPT verdict" in first["filterPattern"]
    assert client.filter_log_events.call_args_list[1].kwargs["nextToken"] == "n"
    assert window["log_group"].startswith("/ecs/aa-cis-")


def test_fetch_events_never_returns_a_partial_search():
    client = MagicMock()
    client.filter_log_events.return_value = {"events": [{"m": 1}], "nextToken": "more"}
    start, end = periods.day_bounds(date(2026, 10, 29))
    with pytest.raises(probes.LogsUnavailable) as e:                     # page cap
        probes.fetch_events(client, "/ecs/aa-cis-dev", start, end)
    assert e.value.reason == "incomplete" and client.filter_log_events.call_count == probes.MAX_PAGES
    ticks = iter([0.0, 5.0, 25.0])                                       # start, after page 1, after page 2
    client.filter_log_events.reset_mock()
    with pytest.raises(probes.LogsUnavailable) as e:                     # time budget
        probes.fetch_events(client, "/ecs/aa-cis-dev", start, end, budget_s=20, clock=lambda: next(ticks))
    assert e.value.reason == "incomplete" and client.filter_log_events.call_count == 2
    assert "20 s" in str(e.value)
    # read_events passes the refusal through unchanged (not re-labelled aws_error)
    client.filter_log_events.reset_mock()
    with pytest.raises(probes.LogsUnavailable) as e:
        probes.read_events(date(2026, 10, 29), client, datetime(2026, 10, 30, 2, tzinfo=UTC))
    assert e.value.reason == "incomplete"


def test_logs_client_has_short_timeouts_and_bounded_retries():
    cfg = probes._client().meta.config
    assert (cfg.connect_timeout, cfg.read_timeout) == (2, 5)
    assert cfg.retries == {"total_max_attempts": 3, "mode": "standard"}     # max_attempts=2 retries + 1 try


@pytest.mark.parametrize("env,group", [("test", "/ecs/aa-cis-dev"), ("", "/ecs/aa-cis-dev"),
                                       ("prod", "/ecs/aa-cis-prod")])
def test_log_group_follows_the_ops_overview_environment(monkeypatch, env, group):
    monkeypatch.setenv("ENVIRONMENT", env)
    assert probes.log_group() == group


# ── handoff ─────────────────────────────────────────────────────────────────────────────────────────────────────

D = date(2026, 10, 29)


def _rows(day, sent=0, received=0, parked=0, failed=0, cases=0, cis_received=0):
    sent_rows = [{"day": day, "n": sent}] if sent else []
    funnel = [{"day": day, "metric": "inquiry_received", "source_system": "tripplanner", "n": received},
              {"day": day, "metric": "inquiry_received", "source_system": "cis", "n": cis_received},
              {"day": day, "metric": "inquiry_parked", "source_system": "tripplanner", "n": parked},
              {"day": day, "metric": "inquiry_failed", "source_system": "cis", "n": failed},
              {"day": day, "metric": "trip_case_created", "source_system": "tripplanner", "n": cases},
              {"day": day, "metric": "trip_case_created", "source_system": "manual", "n": 9}]
    return sent_rows, funnel


def test_build_days_fills_every_day_and_counts_tripplanner_only_where_it_should():
    s1, f1 = _rows(D, sent=3, received=2, parked=1, failed=1, cases=2, cis_received=5)
    days = handoff.build_days(D - timedelta(days=2), D, s1, f1)
    assert [d.day for d in days] == [D - timedelta(days=2), D - timedelta(days=1), D]
    assert days[0].sent == 0 and days[0].gap == 0
    last = days[-1]
    assert (last.sent, last.received, last.parked, last.failed, last.cases, last.gap) == (3, 2, 1, 1, 2, 1)
    assert handoff.inq_to_case(last) == 1.0 and handoff.inq_to_case(days[0]) is None
    # several rows of one day and metric are summed, never the last one wins
    two = handoff.build_days(D, D, [{"day": D, "n": 3}, {"day": D, "n": 1}], f1)
    assert two[0].sent == 4 and two[0].gap == 2


def test_evaluate_alerts_only_when_the_gap_persists():
    s, f = _rows(D, sent=2, received=1)
    days = handoff.build_days(D - timedelta(days=6), D, s, f)
    first = handoff.evaluate(days, None)
    assert first["gap"] == 1 and first["persistent"] is False and first["mismatch_days"][0]["day"] == D.isoformat()
    assert handoff.evaluate(days, {"gap": 1})["persistent"] is True
    assert handoff.evaluate(days, {"gap": 0})["persistent"] is False
    s, f = _rows(D, sent=2, received=2)
    assert handoff.evaluate(handoff.build_days(D, D, s, f), {"gap": 3})["persistent"] is False


def test_check_key_is_the_utc_hour():
    h = handoff.hour_of(datetime(2026, 10, 30, 3, 47, 12, tzinfo=UTC))
    assert handoff.check_key(h) == "inquiry_handoff_check:2026-10-30T03"


class BellConn:
    def __init__(self, last_gap=None):
        self.last_gap, self.inserted = last_gap, []

    def transaction(self):
        @asynccontextmanager
        async def tx():
            yield
        return tx()

    async def execute(self, sql, *args):
        if "INSERT INTO shared.notifications" in sql:
            self.inserted.append(args)

    async def fetchrow(self, sql, *args):
        return None if self.last_gap is None else {"gap": self.last_gap}


@pytest.mark.parametrize("last_gap,gap,raised", [(None, 1, True), (1, 1, False), (1, 2, True), (3, 2, False)])
def test_gap_bell_throttle(last_gap, gap, raised):
    conn = BellConn(last_gap)
    result = {"gap": gap, "sent": 5, "received": 5 - gap, "window": {"first": "a", "last": "b"}}
    assert asyncio.run(handoff.maybe_raise_gap_bell(conn, result)) is raised
    if raised:
        _tenant, event, payload, roles = conn.inserted[0]
        assert event == handoff.GAP_EVENT and json.loads(payload)["gap"] == gap and roles == ["admin"]


class CheckConn:
    def __init__(self, sent, received, previous_gap):
        self.sent, self.received, self.previous_gap = sent, received, previous_gap

    async def fetch(self, sql, *args):
        if "v_ops_traffic_daily" in sql:
            return [{"day": args[1], "n": self.sent}]
        return [{"day": args[1], "metric": "inquiry_received", "source_system": "tripplanner", "n": self.received}]

    async def fetchrow(self, sql, *args):
        assert args[0] == "inquiry_handoff_check:2026-10-30T02"          # the previous hour
        return None if self.previous_gap is None else {"result": json.dumps({"gap": self.previous_gap})}


def _pool(conn):
    pool = MagicMock()

    @asynccontextmanager
    async def ctx():
        yield conn
    pool.acquire = MagicMock(side_effect=lambda: ctx())
    return pool


def _ctx(payload, pool):
    ctx = MagicMock()
    ctx.payload, ctx.pool, ctx.job_id = payload, pool, "11111111-1111-1111-1111-111111111111"
    ctx.set_result = MagicMock()
    return ctx


@pytest.mark.parametrize("previous_gap,alerted", [(None, False), (1, True)])
def test_hourly_check_alerts_on_a_persistent_gap(monkeypatch, previous_gap, alerted):
    monkeypatch.delenv(handoff.ALERTS_ENV, raising=False)
    alert = AsyncMock(return_value={"bell": True, "email_job": "j"})
    monkeypatch.setattr(report_ops_jobs, "_alert", alert)
    out = asyncio.run(report_ops_jobs.run_handoff_check(
        _ctx({"hour": "2026-10-30T03"}, _pool(CheckConn(sent=2, received=1, previous_gap=previous_gap)))))
    assert out["gap"] == 1 and out["window"]["days"] == handoff.WINDOW_DAYS
    assert out["window"]["last"] == "2026-10-30"
    assert alert.await_count == (1 if alerted else 0) and ("alert" in out) is alerted


def test_alert_kill_switch_keeps_the_check_and_drops_bell_and_email(monkeypatch):
    monkeypatch.setenv(handoff.ALERTS_ENV, "off")
    alert = AsyncMock()
    monkeypatch.setattr(report_ops_jobs, "_alert", alert)
    out = asyncio.run(report_ops_jobs.run_handoff_check(
        _ctx({"hour": "2026-10-30T03"}, _pool(CheckConn(sent=2, received=1, previous_gap=1)))))
    assert out["persistent"] is True and out["gap"] == 1
    assert alert.await_count == 0 and out["alert"] == {"disabled": "INQUIRY_HANDOFF_ALERTS is off"}


@pytest.mark.parametrize("value,enabled", [(None, True), ("", True), ("on", True), ("1", True), ("off", False),
                                           (" FALSE ", False), ("0", False), ("no", False)])
def test_alerts_enabled_reads_the_toggle(value, enabled):
    env = {} if value is None else {handoff.ALERTS_ENV: value}
    assert handoff.alerts_enabled(env) is enabled


def test_hourly_check_refuses_a_bad_hour():
    with pytest.raises(report_ops_jobs.NonRetryable):
        asyncio.run(report_ops_jobs.run_handoff_check(_ctx({"hour": "yesterday"}, _pool(None))))


def test_alert_parts_are_best_effort(monkeypatch):
    monkeypatch.setattr(handoff, "maybe_raise_gap_bell", AsyncMock(side_effect=RuntimeError("db")))
    email = AsyncMock(return_value="job-9")
    monkeypatch.setattr(report_ops_jobs.ops_alerts, "enqueue_inquiry_handoff_email", email)
    result = {"gap": 2, "sent": 3, "received": 1, "parked": 0, "failed": 0, "window": {}}
    out = asyncio.run(report_ops_jobs._alert(_pool(None), result, datetime(2026, 10, 30, 3, tzinfo=UTC)))
    assert "bell_error" in out and out["email_job"] == "job-9"
    assert email.await_args.kwargs["details"]["gap"] == 2


def test_hourly_check_kind_is_queued_once_per_hour():
    from api.core import ops_metrics_sampler as sampler
    from shared.jobs.registry import kinds

    enqueue = AsyncMock(return_value=("job-h", True))
    orig = report_ops_jobs.enqueue
    report_ops_jobs.enqueue = enqueue
    try:
        pool = MagicMock()
        pool.fetchval = AsyncMock(return_value=False)
        out: dict = {}
        ts = datetime(2026, 10, 30, 3, 5, tzinfo=UTC)
        asyncio.run(sampler._reports_part(pool, {handoff.CHECK_KIND: kinds()[handoff.CHECK_KIND]}, ts, out))
        assert out["inquiry_handoff_check"]["job_id"] == "job-h"
        assert enqueue.await_args.kwargs["idempotency_key"] == "inquiry_handoff_check:2026-10-30T03"
        pool.fetchval = AsyncMock(return_value=True)                      # already queued this hour
        out = {}
        asyncio.run(sampler._reports_part(pool, {handoff.CHECK_KIND: object()}, ts, out))
        assert out == {} and enqueue.await_count == 1
    finally:
        report_ops_jobs.enqueue = orig


def test_tp03_report_renders_days_checks_and_what_is_not_measured():
    s, f = _rows(D, sent=3, received=2, parked=1)
    days = handoff.build_days(D - timedelta(days=6), D, s, f)
    checks = [{"created_at": datetime(2026, 10, 30, 1, tzinfo=UTC), "status": "succeeded",
               "result": {"gap": 1, "persistent": True}},
              {"created_at": datetime(2026, 10, 30, 0, tzinfo=UTC), "status": "succeeded", "result": {"gap": 0}}]
    tp03 = registry.load().report("TP-03")
    out = handoff.render_report(tp03, D, days, [], checks, datetime(2026, 10, 30, 0, 31, tzinfo=UTC))
    assert out.output["status"] == "gap" and out.output["day"]["gap"] == 1
    assert out.output["hourly_checks"] == {"count": 2, "with_gap": 1, "persistent": 1, "failed": 0}
    assert "| 2026-10-29 | 3 | 2 | 1 | 1 | 0 | 0 |" in out.markdown and "Not measured yet" in out.markdown
    ok = handoff.render_report(tp03, D, handoff.build_days(D, D, *_rows(D, sent=2, received=2)), [], [],
                               datetime(2026, 10, 30, tzinfo=UTC))
    assert ok.output["status"] == "ok" and ok.summary.endswith("all 2 inquiries received")


# ── KPI computers ───────────────────────────────────────────────────────────────────────────────────────────────

def _day_ctx(day=D):
    start, end = periods.day_bounds(day)
    return kpis.DayContext(day=day, start=start, end=end, latest=True)


def test_inquiry_handoff_kpis():
    class Conn:
        async def fetch(self, sql, *args):
            if "v_ops_traffic_daily" in sql:
                return [{"day": D, "n": 4}]
            return _rows(D, received=3, parked=1, failed=1, cases=3)[1]
    out = {v.kpi_id: v for v in asyncio.run(kpis.inquiry_handoff(Conn(), _day_ctx()))}
    assert out["K-TP-HANDOFFGAP"].value == Decimal(1)
    assert out["K-BK-PARKED"].value == Decimal(2)
    assert out["K-BK-INQ2CASE"].value == Decimal("1.0")


@pytest.mark.parametrize("err", [asyncpg.InsufficientPrivilegeError, asyncpg.UndefinedTableError,
                                 asyncpg.UndefinedColumnError])
def test_an_unreadable_contract_view_is_unavailable_not_a_failure(err):
    class Conn:
        async def fetch(self, sql, *args):
            raise err("permission denied for view v_ops_traffic_daily")
    with pytest.raises(kpis.Unavailable, match="contract_view"):
        asyncio.run(kpis.inquiry_handoff(Conn(), _day_ctx()))


def test_nightly_probes_kpis(monkeypatch):
    lv, fr = probes.PROBES
    at = datetime(2026, 10, 29, 4, 46, tzinfo=UTC)
    monkeypatch.setattr(probes, "read_day", AsyncMock(return_value={
        "window": {"log_group": "/ecs/aa-cis-test"}, "passed": 1, "expected": 2,
        "probes": [probes.ProbeResult(lv, "passed", value=0, errors=0, ran_at=at),
                   probes.ProbeResult(fr, "missing")]}))
    out = {v.kpi_id: v for v in asyncio.run(kpis.nightly_probes(None, _day_ctx()))}
    assert out["K-DATA-PROBES"].value == Decimal("0.5000")
    assert out["K-DATA-LIVENESS"].value == Decimal(0) and out["K-DATA-LIVENESS"].data_as_of == at
    assert "K-DATA-STALE" not in out                                     # no run: no value, never a fake 0
    monkeypatch.setattr(probes, "read_day", AsyncMock(side_effect=probes.LogsUnavailable("not_permitted", "nope")))
    with pytest.raises(kpis.Unavailable, match="not_permitted"):
        asyncio.run(kpis.nightly_probes(None, _day_ctx()))


class SnapConn:
    def __init__(self):
        self.written = []

    async def executemany(self, sql, rows):
        self.written += rows

    def transaction(self):
        @asynccontextmanager
        async def tx():
            yield
        return tx()


def test_an_unavailable_source_does_not_fail_the_snapshot(monkeypatch):
    async def ok(conn, ctx):
        return [kpis.KpiValue("K-JOB-FAIL", "app:cis", Decimal(0), "shared.job")]

    async def unavailable(conn, ctx):
        raise kpis.Unavailable("not_permitted: pending")

    monkeypatch.setattr(report_jobs, "COMPUTERS", {"job_fail": (ok, ("K-JOB-FAIL",)),
                                                   "nightly_probes": (unavailable, ("K-DATA-PROBES",))})
    chain = AsyncMock(return_value=[])
    monkeypatch.setattr(report_jobs, "chain_reports", chain)
    conn = SnapConn()
    ctx = _ctx({"trigger": "schedule", "day": periods.latest_closed_day().isoformat()}, _pool(conn))
    ctx.progress = AsyncMock()
    result = asyncio.run(report_jobs.run_snapshot(ctx))
    assert result["errors"] == {} and result["unavailable"] == {"nightly_probes": "not_permitted: pending"}
    assert chain.await_count == 1 and [r[0] for r in conn.written] == ["K-JOB-FAIL"]
