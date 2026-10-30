"""AA-1003 R2 (DATA-01) — the nightly integrity probes' verdicts, read from CloudWatch Logs. No DB, no paid call.

The nightly probes of AA-CIS-Infra that run as a one-off ECS task (DB Migrate mode run-script: the migrator task
definition, log group /ecs/aa-cis-<env>, log streams migrate/migrate/<task id>) print one machine-readable verdict
line; the Infra gates (db/tour_liveness_gate.py, db/freshness_gate.py) read the same line from the same log:

  run-script: file=db/scripts/<name>.py sha256=<12 hex>        (the runner, first: which script this stream runs)
  SCRIPT verdict violations=<n> errors=<n> list=<...> pending=<n>            (s248_tour_deletion_probe, liveness gate)
  SCRIPT verdict stale=<n> stale_aged=<n> errors=<n> list=<...>              (aa991_freshness_probe, freshness probe)

CIS reads those two lines for an ICT day and keeps the last run of each probe it found.

Stream-targeted read (S260, the default): a search with only a stream prefix scans every api / worker line of the
group (Dev 30/10: 18 s for 16 hours, 0 matches), and GitHub starts the nightly crons hours late (Dev 25-29/10: the
04:43 liveness gate ran at 09:25-11:06 UTC, the 05:13 freshness probe at 10:29-14:01 UTC, past the S259 slices that
ended 6 h after the cron, so a probe that ran read "missing"). So CIS first lists the log streams of the group newest
last event first (DescribeLogStreams, 50 a page) back to the start of the day, keeps the migrator streams
(migrate/migrate/<task id>) that have events in the day, and runs FilterLogEvents on those streams by name only (at most
100 names a call) over the whole ICT day. On Dev 29/10 that was 3 list pages and 36 streams, 6 matching lines in about
6 s, a whole day, every run of the day (manual re-runs included).

Fallback, narrow slices (S259): when the stream list is not permitted (logs:DescribeLogStreams, AA-CIS-Infra
FilterOwnLogGroup) or the day is further back than MAX_STREAM_PAGES pages of streams, each probe is searched only
around its nightly cron time, in growing slices ([cron - 10 min, cron + 50 min), then up to + 2 h, + 6 h, + 12 h), and
the search of a probe stops at the first slice where its run has a verdict line. A slice already searched for another
probe is not searched again. The read says which mode answered (`window.mode`: streams | slices, and why).
The other nightly checks (migration and contract copy guards, connection budget, CRM key policy diff) run on GitHub
only, and the schema dump and the isolation probe print no verdict line this parser knows: NOT_VISIBLE names them, so
the digest says what it cannot see instead of counting them green.

Permission: logs:FilterLogEvents and logs:DescribeLogStreams on the log group (AA-CIS-Infra, ECS task role statement
FilterOwnLogGroup; requests recorded in docs/implementation-notes/AA-1003.md, "R2 follow-up requests" and "R4 part
2"). Without FilterLogEvents, or when CloudWatch does not answer, read_day raises LogsUnavailable: the KPI computer
then writes no value and says why in the job result, and the OPS-12 digest prints the reason. A day older than the
log retention (30 days, AA-CIS-Infra modules/ecs) has no events left, so it is refused the same way rather than
reported as "every probe missing".

Bounded: both callers hold a pool connection while this runs (kpi_snapshot_daily, report_generate), so the client
is the ops overview one (api/services/ops_overview/cloudwatch.py: connect 2 s, read 5 s, standard retries, at most 3
tries a call) and every search of a day shares one time budget (FETCH_BUDGET_S, checked before each call) and a page
cap per slice (MAX_PAGES). A search that does not finish within either raises LogsUnavailable("incomplete"): a
partial read could call a probe that ran "missing", so no partial verdict is ever counted.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from services.reports import periods

STREAM_PREFIX = "migrate/migrate/"
# Either line (CloudWatch filter syntax: ?term ?term = OR). Quoted terms match the exact phrase.
FILTER_PATTERN = '?"run-script: file=" ?"SCRIPT verdict"'
LOG_RETENTION_DAYS = 30          # AA-CIS-Infra modules/ecs/main.tf aws_cloudwatch_log_group.ecs retention_in_days
MAX_PAGES = 20                   # per slice; 1 MB / 10,000 events a page; a night has a handful of matching lines
# Every search of one day together, checked before each call (S259: the 20 s whole-day search was cut off). One call
# normally answers in < 2 s; the client bounds a stuck one (2 s connect + 5 s read, 3 tries).
FETCH_BUDGET_S = 12.0
SLICE_BEFORE_MIN = 10            # a probe's first slice starts this long before its cron time ...
SLICE_ENDS_MIN = (50, 120, 360, 720)  # ... and the slices end this long after it (GitHub runs the crons hours late)
# Stream-targeted read (the default). DescribeLogStreams returns at most 50 streams a page; Dev writes ~60 log streams
# a day (most of them migrator tasks), so the snapshot of yesterday (00:30 UTC) walks 2-3 pages. 12 pages reach about
# 10 days back; an older day falls back to the slices.
STREAM_PAGE_SIZE = 50
MAX_STREAM_PAGES = 12
MAX_STREAM_NAMES = 100           # FilterLogEvents logStreamNames: at most 100 names a call
# lastEventTimestamp is eventually consistent (AWS: "typically updates in less than an hour"): keep walking an hour
# past the day's start so a stream whose value lags is still listed.
STREAM_LAG = timedelta(hours=1)

FILE_RE = re.compile(r"run-script: file=db/scripts/([a-z0-9_]+)\.py\b")
VERDICT_RE = re.compile(r"SCRIPT verdict (.*)$")
_KV_RE = re.compile(r"([a-z_]+)=(\S*)")


@dataclass(frozen=True)
class Probe:
    key: str          # stable name used in the digest and the job result
    label: str
    script: str       # db/scripts/<script>.py of AA-CIS-Infra
    value_field: str  # the verdict field that is the probe's KPI value
    kpi_id: str
    cron_utc: str


# The probes CIS can read. Order = display order (the nightly order).
PROBES: tuple[Probe, ...] = (
    Probe("liveness", "Tour liveness gate", "s248_tour_deletion_probe", "violations", "K-DATA-LIVENESS", "04:43"),
    Probe("freshness", "Downstream freshness probe", "aa991_freshness_probe", "stale_aged", "K-DATA-STALE", "05:13"),
)
BY_SCRIPT = {p.script: p for p in PROBES}

# The nightly checks DATA-01 lists that CIS cannot see (name, UTC time, why).
NOT_VISIBLE: tuple[tuple[str, str, str], ...] = (
    ("Migration copy guard", "02:17", "GitHub only"),
    ("Contract copy guard", "02:29", "GitHub only"),
    ("Connection budget", "02:43", "GitHub only"),
    ("Schema dump and drift", "03:43", "ECS task without a verdict line"),
    ("Isolation probe", "04:13", "ECS task without a verdict line"),
    ("CRM key policy diff", "04:29", "GitHub only"),
)


class LogsUnavailable(Exception):
    """The probe log cannot be read: `reason` is not_permitted, aws_error, incomplete or too_old (plain words in
    str())."""

    def __init__(self, reason: str, message: str):
        super().__init__(reason, message)
        self.reason = reason
        self.message = message

    def __str__(self) -> str:
        return self.message


@dataclass
class ProbeRun:
    stream: str
    script: Optional[str] = None
    first_at: Optional[datetime] = None
    verdict_at: Optional[datetime] = None
    verdict: Optional[dict] = None


@dataclass
class ProbeResult:
    probe: Probe
    status: str                       # passed | failed | no_verdict | missing
    value: Optional[int] = None       # the probe's KPI value (violations / stale_aged)
    errors: Optional[int] = None
    ran_at: Optional[datetime] = None
    runs: int = 0
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"probe": self.probe.key, "label": self.probe.label, "script": self.probe.script,
                "cron_utc": self.probe.cron_utc, "status": self.status, "value": self.value,
                "value_field": self.probe.value_field, "errors": self.errors,
                "ran_at": self.ran_at.isoformat() if self.ran_at else None, "runs": self.runs, "detail": self.detail}


# ── pure parsing ────────────────────────────────────────────────────────────────────────────────────────────────


def _int(v: Any) -> Any:
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def parse_verdict(line: str) -> Optional[dict]:
    """`SCRIPT verdict a=1 b=x` -> {"a": 1, "b": "x"}; None when the line is not a verdict."""
    m = VERDICT_RE.search(line or "")
    if not m:
        return None
    return {k: _int(v) for k, v in _KV_RE.findall(m.group(1))}


def _ts(ms: Any) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def group_runs(events: Iterable[dict]) -> list[ProbeRun]:
    """FilterLogEvents events -> one ProbeRun per log stream (a stream is one task, one script)."""
    runs: dict[str, ProbeRun] = {}
    for e in sorted(events, key=lambda e: (e.get("logStreamName") or "", e.get("timestamp") or 0)):
        stream = e.get("logStreamName") or ""
        run = runs.setdefault(stream, ProbeRun(stream=stream))
        at = _ts(e.get("timestamp"))
        msg = e.get("message") or ""
        if run.first_at is None or (at and at < run.first_at):
            run.first_at = at
        m = FILE_RE.search(msg)
        if m:
            run.script = m.group(1)
            continue
        verdict = parse_verdict(msg)
        if verdict is not None:
            run.verdict, run.verdict_at = verdict, at
    return list(runs.values())


def judge(probe: Probe, verdict: dict) -> tuple[str, Optional[int], Optional[int]]:
    """(passed | failed, value, errors) the way the Infra gate decides: red on errors > 0 or on the value field > 0."""
    value = verdict.get(probe.value_field)
    errors = verdict.get("errors")
    if not isinstance(value, int) or not isinstance(errors, int):
        return "failed", value if isinstance(value, int) else None, errors if isinstance(errors, int) else None
    return ("passed" if value == 0 and errors == 0 else "failed"), value, errors


def summarize(runs: Iterable[ProbeRun]) -> list[ProbeResult]:
    """The last run of each known probe decides its status; a probe with no run in the window is `missing`, a run
    whose log has no verdict line (the task died, the script raised) is `no_verdict`."""
    by_probe: dict[str, list[ProbeRun]] = {}
    for r in runs:
        if r.script in BY_SCRIPT:
            by_probe.setdefault(r.script, []).append(r)
    out = []
    for p in PROBES:
        mine = sorted(by_probe.get(p.script, []), key=lambda r: r.first_at or datetime.min.replace(tzinfo=timezone.utc))
        if not mine:
            out.append(ProbeResult(p, "missing"))
            continue
        last = mine[-1]
        if last.verdict is None:
            out.append(ProbeResult(p, "no_verdict", ran_at=last.first_at, runs=len(mine)))
            continue
        status, value, errors = judge(p, last.verdict)
        detail = {k: v for k, v in last.verdict.items() if k != "list"}
        if last.verdict.get("list"):
            detail["list"] = str(last.verdict["list"])[:300]
        out.append(ProbeResult(p, status, value=value, errors=errors, ran_at=last.verdict_at or last.first_at,
                               runs=len(mine), detail=detail))
    return out


# ── AWS ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def environment() -> str:
    """The deployment environment the log group is named after; test / local map to dev like the ops overview's."""
    from api.services.ops_overview import cloudwatch

    return cloudwatch.environment()


def log_group(env: Optional[str] = None) -> str:
    return f"/ecs/aa-cis-{env or environment()}"


def _client() -> Any:
    from api.services.ops_overview import cloudwatch  # lazy: boto3 only on the worker path that reads the log

    return cloudwatch.aws_client("logs")


def _error_code(e: Exception) -> str:
    response = getattr(e, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if isinstance(code, str):
            return code
    return type(e).__name__


def fetch_events(client: Any, group: str, start: datetime, end: datetime, *,
                 budget_s: float = FETCH_BUDGET_S, clock: Callable[[], float] = time.monotonic,
                 stats: Optional[dict] = None, streams: Optional[list[str]] = None) -> list[dict]:
    """Every matching event of [start, end) (sync: run it in a thread). Raises LogsUnavailable("incomplete") when the
    search still has a next page after MAX_PAGES pages or after `budget_s` seconds. `stats` (optional) counts the
    calls made: {"calls": n}. `streams`: search only these log streams (at most MAX_STREAM_NAMES) instead of every
    stream under the migrator prefix."""
    kwargs = {"logGroupName": group, "filterPattern": FILTER_PATTERN,
              "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000) - 1}
    if streams is not None:
        kwargs["logStreamNames"] = list(streams)
    else:
        kwargs["logStreamNamePrefix"] = STREAM_PREFIX
    events: list[dict] = []
    started = clock()
    for page_no in range(1, MAX_PAGES + 1):
        page = client.filter_log_events(**kwargs)
        if stats is not None:
            stats["calls"] = stats.get("calls", 0) + 1
        events += page.get("events") or []
        token = page.get("nextToken")
        if not token:
            return events
        kwargs["nextToken"] = token
        if clock() - started >= budget_s:
            raise LogsUnavailable("incomplete", f"CloudWatch Logs did not finish the search in {budget_s:g} s "
                                                f"({page_no} pages, {len(events)} lines); probe results not counted")
    raise LogsUnavailable("incomplete", f"CloudWatch Logs search still had more pages after {MAX_PAGES} pages "
                                        f"({len(events)} lines); probe results not counted")


def probe_slices(probe: Probe, day: date, now: Optional[datetime] = None) -> list[tuple[datetime, datetime]]:
    """The UTC windows searched for a probe's nightly run on ICT day `day`, in order, clipped to the ICT day and to
    `now` (a slice that has not started yet is dropped)."""
    day_start, day_end = periods.day_bounds(day)
    h, m = (int(x) for x in probe.cron_utc.split(":"))
    cron = datetime.combine(day, dtime(h, m), tzinfo=timezone.utc)
    if cron >= day_end:                 # a cron at or after 17:00 UTC belongs to the ICT day that starts then
        cron -= timedelta(days=1)
    edges = [cron - timedelta(minutes=SLICE_BEFORE_MIN)] + [cron + timedelta(minutes=x) for x in SLICE_ENDS_MIN]
    top = min(day_end, now) if now is not None else day_end
    out = []
    for a, b in zip(edges, edges[1:]):
        a, b = max(a, day_start), min(b, top)
        if a < b:
            out.append((a, b))
    return out


def _uncovered(a: datetime, b: datetime, done: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """The parts of [a, b) not inside any searched window of `done`."""
    pieces = [(a, b)]
    for s, e in sorted(done):
        nxt = []
        for x, y in pieces:
            if e <= x or s >= y:
                nxt.append((x, y))
                continue
            if x < s:
                nxt.append((x, s))
            if e < y:
                nxt.append((e, y))
        pieces = nxt
    return pieces


def _has_verdict(events: Iterable[dict], probe: Probe) -> bool:
    return any(r.script == probe.script and r.verdict is not None for r in group_runs(events))


def search_day(client: Any, group: str, day: date, now: Optional[datetime] = None, *,
               budget_s: float = FETCH_BUDGET_S, clock: Callable[[], float] = time.monotonic) -> tuple[list, dict]:
    """The matching events of every probe's slices on ICT day `day` (see the module doc) and what was searched:
    {"slices": [[start, end], ...], "calls": n, "seconds": s}. Raises LogsUnavailable("incomplete") when the budget
    runs out before a probe's search has finished."""
    started = clock()
    stats: dict = {"calls": 0}
    done: list[tuple[datetime, datetime]] = []
    seen: dict[tuple, dict] = {}
    for probe in PROBES:
        for a, b in probe_slices(probe, day, now):
            if _has_verdict(seen.values(), probe):
                break
            for x, y in _uncovered(a, b, done):
                left = budget_s - (clock() - started)
                if left <= 0:
                    raise LogsUnavailable("incomplete", f"CloudWatch Logs did not finish the probe searches in "
                                                        f"{budget_s:g} s ({stats['calls']} calls, {len(seen)} "
                                                        f"lines); probe results not counted")
                for e in fetch_events(client, group, x, y, budget_s=left, clock=clock, stats=stats):
                    seen[(e.get("logStreamName"), e.get("timestamp"), e.get("eventId"), e.get("message"))] = e
                done.append((x, y))
    searched = {"slices": [[x.isoformat(), y.isoformat()] for x, y in sorted(done)], "calls": stats["calls"],
                "seconds": round(clock() - started, 2)}
    return list(seen.values()), searched


class _UseSlices(Exception):
    """The stream-targeted read cannot answer for this day (list not permitted, day too far back): use the slices."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


def list_day_streams(client: Any, group: str, start: datetime, end: datetime, *, budget_s: float = FETCH_BUDGET_S,
                     clock: Callable[[], float] = time.monotonic, stats: Optional[dict] = None) -> list[str]:
    """The migrator log streams (STREAM_PREFIX) with events in [start, end), from DescribeLogStreams newest last event
    first, stopping at the first stream whose last event is older than start - STREAM_LAG. Raises _UseSlices when the
    list is not permitted or the walk needs more than MAX_STREAM_PAGES pages, LogsUnavailable("incomplete") when the
    budget runs out."""
    floor = int((start - STREAM_LAG).timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    names: list[str] = []
    kwargs: dict[str, Any] = {"logGroupName": group, "orderBy": "LastEventTime", "descending": True,
                              "limit": STREAM_PAGE_SIZE}
    started = clock()
    for _ in range(MAX_STREAM_PAGES):
        if clock() - started >= budget_s:
            raise LogsUnavailable("incomplete", f"CloudWatch Logs did not list the day's log streams in {budget_s:g} "
                                                f"s; probe results not counted")
        try:
            page = client.describe_log_streams(**kwargs)
        except Exception as e:  # noqa: BLE001 — a refusal falls back to the slices; anything else is the caller's
            from api.services.ops_overview import cloudwatch

            if cloudwatch.is_denied(e):
                raise _UseSlices("listing the log streams is not permitted (logs:DescribeLogStreams)") from e
            raise
        if stats is not None:
            stats["calls"] = stats.get("calls", 0) + 1
            stats["list_pages"] = stats.get("list_pages", 0) + 1
        for s in page.get("logStreams") or []:
            last = s.get("lastEventTimestamp") or s.get("lastIngestionTime") or 0
            if last < floor:
                return names
            first = s.get("firstEventTimestamp")
            name = s.get("logStreamName") or ""
            if name.startswith(STREAM_PREFIX) and first is not None and first < end_ms:
                names.append(name)
        token = page.get("nextToken")
        if not token:
            return names
        kwargs["nextToken"] = token
    raise _UseSlices(f"the day is more than {MAX_STREAM_PAGES * STREAM_PAGE_SIZE} log streams back")


def read_streams_day(client: Any, group: str, day: date, now: Optional[datetime] = None, *,
                     budget_s: float = FETCH_BUDGET_S,
                     clock: Callable[[], float] = time.monotonic) -> tuple[list[dict], dict]:
    """Every matching event of ICT day `day` (up to `now`) in the day's migrator streams, searched by name. Raises
    _UseSlices or LogsUnavailable (see list_day_streams / fetch_events)."""
    started = clock()
    start, end = periods.day_bounds(day)
    if now is not None:
        end = max(start, min(end, now))
    stats: dict = {"calls": 0, "list_pages": 0}
    names = list_day_streams(client, group, start, end, budget_s=budget_s, clock=clock, stats=stats)
    events: list[dict] = []
    for i in range(0, len(names), MAX_STREAM_NAMES):
        left = budget_s - (clock() - started)
        if left <= 0:
            raise LogsUnavailable("incomplete", f"CloudWatch Logs did not finish the probe searches in {budget_s:g} s "
                                                f"({stats['calls']} calls, {len(events)} lines); probe results not "
                                                f"counted")
        events += fetch_events(client, group, start, end, budget_s=left, clock=clock, stats=stats,
                               streams=names[i:i + MAX_STREAM_NAMES])
    return events, {"mode": "streams", "streams": len(names), "list_pages": stats["list_pages"],
                    "calls": stats["calls"], "seconds": round(clock() - started, 2)}


def read_events(day: date, client: Any = None, now: Optional[datetime] = None) -> tuple[list[dict], dict]:
    """The matching events of an ICT day and the window read: the stream-targeted read, else the slices (module doc).
    Raises LogsUnavailable."""
    if day < periods.today_ict(now) - timedelta(days=LOG_RETENTION_DAYS - 1):
        raise LogsUnavailable("too_old", f"{day.isoformat()} is older than the {LOG_RETENTION_DAYS}-day log retention")
    start, end = periods.day_bounds(day)
    group = log_group()
    client = client or _client()
    try:
        try:
            events, searched = read_streams_day(client, group, day, now)
        except _UseSlices as why:
            events, searched = search_day(client, group, day, now)
            searched = {"mode": "slices", "why": why.why, **searched}
    except LogsUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 — AWS / network: the caller degrades and says why
        from api.services.ops_overview import cloudwatch

        code = _error_code(e)
        if cloudwatch.is_denied(e):
            raise LogsUnavailable("not_permitted", "the CIS task role may not read the probe log yet "
                                                   "(logs:FilterLogEvents on the log group is pending)") from e
        raise LogsUnavailable("aws_error", f"CloudWatch Logs did not answer ({code})") from e
    return events, {"log_group": group, "start": start.isoformat(), "end": end.isoformat(), **searched}


async def read_day(day: date, client: Any = None, now: Optional[datetime] = None) -> dict:
    """{"window": {...}, "probes": [ProbeResult...], "passed": n, "expected": n}. Raises LogsUnavailable."""
    events, window = await asyncio.to_thread(read_events, day, client, now)
    results = summarize(group_runs(events))
    return {"window": window, "probes": results, "passed": sum(1 for r in results if r.status == "passed"),
            "expected": len(PROBES)}
