"""AA-1003 R2 — DATA-01 probe verdicts (CloudWatch Logs) and TP-03 inquiry handoff (contract views). No DB, no AWS.

- probes: the verdict line parser, one run per log stream, the last run of a probe decides, a probe with no run is
  `missing` and a run without a verdict `no_verdict`; refusals and old days raise LogsUnavailable.
- handoff: TP-03 is retired (R0 hotfix, root ADR 0019 B2B-only model): the sampler no longer queues the hourly check,
  a stale queued check is a no-op, no KPI computer reads the inquiry views, no renderer, the register says retired.
- KPI computers: values written, Unavailable recorded without failing the snapshot job.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.jobs import report_jobs, report_ops_jobs
from services.reports import b2b_only, handoff, kpis, periods, probes, registry

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


LIVENESS_FILE = "run-script: file=db/scripts/s248_tour_deletion_probe.py sha256=abcdef012345"
FRESHNESS_FILE = "run-script: file=db/scripts/aa991_freshness_probe.py sha256=abcdef012345"


class FakeLogs:
    """FilterLogEvents over a fixed event list: honours startTime / endTime, pages `per_page` events at a time and
    advances a fake clock by `seconds_per_call` on every call."""

    def __init__(self, events, per_page=100, seconds_per_call=0.0, can_list=False):
        self.events, self.per_page, self.seconds_per_call = events, per_page, seconds_per_call
        self.calls: list[dict] = []
        self.list_calls: list[dict] = []
        self.can_list = can_list
        self.now = 0.0

    def describe_log_streams(self, **kw):
        # S260: the stream-targeted read lists streams first; these slice tests run without that permission
        self.list_calls.append(kw)
        if not self.can_list:
            raise _Denied("no")
        return {"logStreams": []}

    def clock(self) -> float:
        return self.now

    def filter_log_events(self, **kw):
        self.calls.append(kw)
        self.now += self.seconds_per_call
        hits = [e for e in self.events if kw["startTime"] <= e["timestamp"] <= kw["endTime"]]
        offset = int(kw.get("nextToken") or 0)
        page = hits[offset:offset + self.per_page]
        out = {"events": page}
        if offset + self.per_page < len(hits):
            out["nextToken"] = str(offset + self.per_page)
        return out

    def spans(self) -> list[tuple[datetime, datetime]]:
        return [(datetime.fromtimestamp(c["startTime"] / 1000, UTC), datetime.fromtimestamp((c["endTime"] + 1) / 1000,
                                                                                             UTC)) for c in self.calls]


NIGHT = [  # a normal night: both probes ran a few minutes after their cron
    _ev("migrate/migrate/t1", "2026-10-29T04:46:00", LIVENESS_FILE),
    _ev("migrate/migrate/t1", "2026-10-29T04:49:00", "SCRIPT verdict violations=0 errors=0 list=- pending=0"),
    _ev("migrate/migrate/t2", "2026-10-29T05:16:00", FRESHNESS_FILE),
    _ev("migrate/migrate/t2", "2026-10-29T05:19:00", "SCRIPT verdict stale=1 stale_aged=0 errors=0 list=-"),
]


def test_probe_slices_follow_the_cron_and_stay_inside_the_ict_day():
    liveness = probes.PROBES[0]
    slices = probes.probe_slices(liveness, date(2026, 10, 29))
    assert slices[0] == (datetime(2026, 10, 29, 4, 33, tzinfo=UTC), datetime(2026, 10, 29, 5, 33, tzinfo=UTC))
    assert slices[-1][1] == datetime(2026, 10, 29, 16, 43, tzinfo=UTC)  # S260: + 12 h (GitHub runs crons late)
    assert all(b - a <= timedelta(hours=6) for a, b in slices)      # never the whole day
    # a day still running: slices after `now` are dropped, the current one is cut at now
    cut = probes.probe_slices(liveness, date(2026, 10, 29), now=datetime(2026, 10, 29, 6, 0, tzinfo=UTC))
    assert cut[-1][1] == datetime(2026, 10, 29, 6, 0, tzinfo=UTC) and len(cut) == 2


def test_a_normal_night_is_read_with_one_narrow_call():
    logs = FakeLogs(NIGHT)
    events, searched = probes.search_day(logs, "/ecs/aa-cis-dev", date(2026, 10, 29), clock=logs.clock)
    assert len(logs.calls) == 1 and searched["calls"] == 1                # freshness found in liveness's slice
    (a, b), = logs.spans()
    assert b - a == timedelta(hours=1)
    results = {r.probe.key: r for r in probes.summarize(probes.group_runs(events))}
    assert results["liveness"].status == "passed" and results["freshness"].status == "passed"
    first = logs.calls[0]
    assert first["logStreamNamePrefix"] == "migrate/migrate/" and "SCRIPT verdict" in first["filterPattern"]


def test_a_late_run_is_found_in_a_later_slice_without_searching_a_window_twice():
    late = [_ev("migrate/migrate/t1", "2026-10-29T06:10:00", LIVENESS_FILE),          # GitHub ran it 1 h 27 late
            _ev("migrate/migrate/t1", "2026-10-29T06:14:00", "SCRIPT verdict violations=2 errors=0 list=x pending=0"),
            _ev("migrate/migrate/t2", "2026-10-29T05:16:00", FRESHNESS_FILE),
            _ev("migrate/migrate/t2", "2026-10-29T05:19:00", "SCRIPT verdict stale=0 stale_aged=0 errors=0 list=-")]
    logs = FakeLogs(late)
    events, searched = probes.search_day(logs, "/ecs/aa-cis-dev", date(2026, 10, 29), clock=logs.clock)
    spans = logs.spans()
    assert len(spans) == 2 and spans[0][1] == spans[1][0]                  # contiguous, no overlap
    results = {r.probe.key: r for r in probes.summarize(probes.group_runs(events))}
    assert results["liveness"].status == "failed" and results["liveness"].value == 2
    assert results["freshness"].status == "passed"
    assert searched["slices"][0][0] == "2026-10-29T04:33:00+00:00"


def test_a_probe_that_did_not_run_is_missing_after_its_last_slice():
    logs = FakeLogs(NIGHT[2:])                                             # only freshness ran
    events, searched = probes.search_day(logs, "/ecs/aa-cis-dev", date(2026, 10, 29), clock=logs.clock)
    results = {r.probe.key: r for r in probes.summarize(probes.group_runs(events))}
    assert results["liveness"].status == "missing" and results["freshness"].status == "passed"
    covered = sorted(logs.spans())
    assert covered[0][0] == datetime(2026, 10, 29, 4, 33, tzinfo=UTC)
    assert covered[-1][1] == datetime(2026, 10, 29, 16, 43, tzinfo=UTC)     # liveness's last slice, then stop


def test_the_day_search_finishes_within_its_budget_even_when_calls_are_slow():
    logs = FakeLogs(NIGHT[2:], seconds_per_call=7.0)                     # slow calls (Dev 29/10: ~5 s a page)
    with pytest.raises(probes.LogsUnavailable) as e:
        probes.search_day(logs, "/ecs/aa-cis-dev", date(2026, 10, 29), clock=logs.clock)
    assert e.value.reason == "incomplete"
    assert logs.now < 20                                                   # the snapshot part's 20 s ceiling
    assert logs.now <= probes.FETCH_BUDGET_S + 7.0                         # at most one call past the budget


def test_read_events_reports_the_slices_it_searched():
    logs = FakeLogs(NIGHT)
    events, window = probes.read_events(date(2026, 10, 29), logs, datetime(2026, 10, 30, 2, tzinfo=UTC))
    assert len(events) == 4 and window["calls"] == 1 and len(window["slices"]) == 1
    assert window["mode"] == "slices" and "DescribeLogStreams" in window["why"]
    assert window["log_group"].startswith("/ecs/aa-cis-")
    start, end = periods.day_bounds(date(2026, 10, 29))
    assert window["start"] == start.isoformat() and window["end"] == end.isoformat()


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


# ── handoff (retired) ───────────────────────────────────────────────────────────────────────────────────────────

D = date(2026, 10, 29)


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


def test_a_stale_hourly_check_is_a_no_op_that_reads_nothing():
    pool = MagicMock()
    pool.acquire = MagicMock(side_effect=AssertionError("the retired check must not touch the database"))
    out = asyncio.run(report_ops_jobs.run_handoff_check(_ctx({"hour": "2026-11-03T05"}, pool)))
    assert out["status"] == "retired" and out["hour"] == "2026-11-03T05"
    assert handoff.CHECK_KIND in b2b_only.RETIRED_JOB_KINDS


def test_the_sampler_no_longer_queues_the_hourly_check(monkeypatch):
    from api.core import ops_metrics_sampler as sampler
    from shared.jobs.registry import kinds

    enqueue = AsyncMock(return_value=("job-h", True))
    monkeypatch.setattr(report_ops_jobs, "enqueue", enqueue)
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)
    out: dict = {}
    ts = datetime(2026, 11, 3, 3, 5, tzinfo=UTC)
    asyncio.run(sampler._reports_part(pool, {handoff.CHECK_KIND: kinds()[handoff.CHECK_KIND]}, ts, out))
    assert out == {} and enqueue.await_count == 0
    assert not hasattr(report_ops_jobs, "enqueue_hourly_check")


def test_no_kpi_computer_or_renderer_reads_the_inquiry_hand_off():
    from services.reports import generate

    assert "inquiry_handoff" not in kpis.COMPUTERS
    assert not {"K-TP-HANDOFFGAP", "K-BK-PARKED", "K-BK-INQ2CASE"} & kpis.computed_kpi_ids()
    reg = registry.load()
    for kid in ("K-TP-HANDOFFGAP", "K-BK-PARKED", "K-BK-INQ2CASE"):
        assert not reg.kpi(kid).snapshotted and reg.kpi(kid).alert is None, kid
    tp03 = reg.report("TP-03")
    assert tp03.retired and not tp03.runnable and tp03.retired_on == b2b_only.B2C_OFF_DAY
    assert "TP-03" not in generate.RENDERERS and tp03 not in reg.runnable()


# ── KPI computers ───────────────────────────────────────────────────────────────────────────────────────────────

def _day_ctx(day=D):
    start, end = periods.day_bounds(day)
    return kpis.DayContext(day=day, start=start, end=end, latest=True)


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


# ── S260: stream-targeted read (DescribeLogStreams, then FilterLogEvents by stream name) ──────────────────────────

class StreamLogs(FakeLogs):
    """FakeLogs that can list its streams: one stream per distinct logStreamName, plus `noise` api / worker streams,
    newest last event first, `page` streams a page. FilterLogEvents honours logStreamNames."""

    def __init__(self, events, noise=(), page=50, **kw):
        super().__init__(events, can_list=True, **kw)
        self.page = page
        streams: dict[str, list[int]] = {}
        for e in list(events) + list(noise):
            streams.setdefault(e["logStreamName"], []).append(e["timestamp"])
        self.streams = sorted(({"logStreamName": n, "firstEventTimestamp": min(t), "lastEventTimestamp": max(t)}
                               for n, t in streams.items()), key=lambda s: -s["lastEventTimestamp"])

    def describe_log_streams(self, **kw):
        self.list_calls.append(kw)
        self.now += self.seconds_per_call
        assert kw["orderBy"] == "LastEventTime" and kw["descending"] is True and "logStreamNamePrefix" not in kw
        offset = int(kw.get("nextToken") or 0)
        out = {"logStreams": self.streams[offset:offset + self.page]}
        if offset + self.page < len(self.streams):
            out["nextToken"] = str(offset + self.page)
        return out

    def filter_log_events(self, **kw):
        names = kw.get("logStreamNames")
        if names is not None:
            assert "logStreamNamePrefix" not in kw and len(names) <= probes.MAX_STREAM_NAMES
            kept = self.events
            self.events = [e for e in kept if e["logStreamName"] in names]
            try:
                return super().filter_log_events(**kw)
            finally:
                self.events = kept
        return super().filter_log_events(**kw)


# Dev 29/10: GitHub ran the 04:43 gate at 11:06 and the 05:13 probe at 11:29 UTC (outside the S259 6 h slices)
LATE_NIGHT = [
    _ev("migrate/migrate/a1", "2026-10-29T11:06:00", LIVENESS_FILE),
    _ev("migrate/migrate/a1", "2026-10-29T11:06:30", "SCRIPT verdict violations=0 errors=0 list=- pending=0"),
    _ev("migrate/migrate/b2", "2026-10-29T11:29:00", FRESHNESS_FILE),
    _ev("migrate/migrate/b2", "2026-10-29T11:29:40", "SCRIPT verdict stale=1 stale_aged=111 errors=0 list=x"),
    _ev("migrate/migrate/c3", "2026-10-29T03:22:00", "run-script: file=db/scripts/aa838_capacity_probe.py sha256=a"),
]
NOISE = [_ev("api/api/x1", "2026-10-30T01:50:00", "GET /health 200"),
         _ev("worker/worker/w1", "2026-10-30T01:40:00", "job claimed"),
         _ev("migrate/migrate/old", "2026-10-27T05:00:00", LIVENESS_FILE),     # an earlier day: not searched
         _ev("migrate/migrate/next", "2026-10-29T18:00:00", LIVENESS_FILE)]    # the next ICT day (UTC 18:00 > 17:00)
NEXT_MORNING = datetime(2026, 10, 30, 0, 30, tzinfo=UTC)                     # the snapshot of 29/10


def test_streams_read_finds_runs_hours_after_the_cron_in_few_calls():
    logs = StreamLogs(LATE_NIGHT, NOISE)
    events, window = probes.read_events(date(2026, 10, 29), logs, NEXT_MORNING)
    assert window["mode"] == "streams" and window["streams"] == 3        # a1, b2, c3 (not old, next, api, worker)
    assert window["list_pages"] == 1 and window["calls"] == 2
    (f,) = logs.calls
    assert sorted(f["logStreamNames"]) == ["migrate/migrate/a1", "migrate/migrate/b2", "migrate/migrate/c3"]
    start, end = periods.day_bounds(date(2026, 10, 29))
    assert f["startTime"] == int(start.timestamp() * 1000) and f["endTime"] == int(end.timestamp() * 1000) - 1
    results = {r.probe.key: r for r in probes.summarize(probes.group_runs(events))}
    assert results["liveness"].status == "passed"
    assert results["freshness"].status == "failed" and results["freshness"].value == 111


def test_streams_read_stops_listing_at_the_day_start_and_pages_names_by_100():
    many = [_ev(f"migrate/migrate/t{i:03d}", f"2026-10-29T{10 + i // 60:02d}:{i % 60:02d}:00", "x")
            for i in range(130)] + LATE_NIGHT
    older = [_ev(f"migrate/migrate/o{i:03d}", "2026-10-20T05:00:00", "x") for i in range(500)]
    logs = StreamLogs(many, older, page=50, per_page=1000)
    events, window = probes.read_events(date(2026, 10, 29), logs, NEXT_MORNING)
    assert window["streams"] == 133 and window["list_pages"] == 3           # 133 streams of the day, 50 a page
    assert [len(c["logStreamNames"]) for c in logs.calls] == [100, 33]
    assert {r.probe.key: r.status for r in probes.summarize(probes.group_runs(events))}["liveness"] == "passed"


def test_a_lagging_last_event_time_is_still_listed():
    lag = StreamLogs(LATE_NIGHT)
    # a stream of the day whose lastEventTimestamp lags (shown 30 min before the day starts, AWS eventual consistency)
    start, _ = periods.day_bounds(date(2026, 10, 29))
    lag.streams.append({"logStreamName": "migrate/migrate/lag",
                        "firstEventTimestamp": int(start.timestamp() * 1000) - 1,
                        "lastEventTimestamp": int((start - timedelta(minutes=30)).timestamp() * 1000)})
    _, window = probes.read_events(date(2026, 10, 29), lag, NEXT_MORNING)
    assert window["streams"] == 4


def test_no_migrator_stream_in_the_day_means_every_probe_missing_without_a_search():
    logs = StreamLogs([], NOISE)
    events, window = probes.read_events(date(2026, 10, 29), logs, NEXT_MORNING)
    assert window["streams"] == 0 and logs.calls == [] and events == []
    assert {r.status for r in probes.summarize(probes.group_runs(events))} == {"missing"}


def test_a_day_too_far_back_falls_back_to_the_slices():
    filler = [_ev(f"migrate/migrate/f{i:04d}", "2026-10-29T20:00:00", "x") for i in range(probes.MAX_STREAM_PAGES * 50)]
    logs = StreamLogs(NIGHT, filler)
    events, window = probes.read_events(date(2026, 10, 29), logs, NEXT_MORNING)
    assert window["mode"] == "slices" and "log streams back" in window["why"]
    assert len(logs.list_calls) == probes.MAX_STREAM_PAGES and "logStreamNamePrefix" in logs.calls[0]
    assert {r.probe.key: r.status for r in probes.summarize(probes.group_runs(events))}["liveness"] == "passed"


def test_streams_read_is_bounded_by_the_budget():
    logs = StreamLogs(LATE_NIGHT, [_ev(f"api/api/n{i}", "2026-10-30T01:00:00", "x") for i in range(400)], page=50,
                      seconds_per_call=4.0)
    with pytest.raises(probes.LogsUnavailable) as e:
        probes.read_streams_day(logs, "/ecs/aa-cis-dev", date(2026, 10, 29), NEXT_MORNING, clock=logs.clock)
    assert e.value.reason == "incomplete" and logs.now <= probes.FETCH_BUDGET_S + 4.0


def test_a_refused_search_after_the_list_is_not_permitted_not_a_fallback():
    logs = StreamLogs(LATE_NIGHT)
    logs.filter_log_events = MagicMock(side_effect=_Denied("no"))
    with pytest.raises(probes.LogsUnavailable) as e:
        probes.read_events(date(2026, 10, 29), logs, NEXT_MORNING)
    assert e.value.reason == "not_permitted"


def test_a_day_still_running_is_searched_up_to_now():
    logs = StreamLogs(LATE_NIGHT)
    now = datetime(2026, 10, 29, 11, 10, tzinfo=UTC)
    _, window = probes.read_events(date(2026, 10, 29), logs, now)
    assert logs.calls[0]["endTime"] == int(now.timestamp() * 1000) - 1
    assert window["streams"] == 2                                          # b2 (11:29) starts after now
