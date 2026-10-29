"""AA-1003 R2 (DATA-01) — the nightly integrity probes' verdicts, read from CloudWatch Logs. No DB, no paid call.

The nightly probes of AA-CIS-Infra that run as a one-off ECS task (DB Migrate mode run-script: the migrator task
definition, log group /ecs/aa-cis-<env>, log streams migrate/migrate/<task id>) print one machine-readable verdict
line; the Infra gates (db/tour_liveness_gate.py, db/freshness_gate.py) read the same line from the same log:

  run-script: file=db/scripts/<name>.py sha256=<12 hex>        (the runner, first: which script this stream runs)
  SCRIPT verdict violations=<n> errors=<n> list=<...> pending=<n>            (s248_tour_deletion_probe, liveness gate)
  SCRIPT verdict stale=<n> stale_aged=<n> errors=<n> list=<...>              (aa991_freshness_probe, freshness probe)

CIS reads those two lines for an ICT day with one FilterLogEvents query (paged) and keeps the last run of each probe.
The other nightly checks (migration and contract copy guards, connection budget, CRM key policy diff) run on GitHub
only, and the schema dump and the isolation probe print no verdict line this parser knows: NOT_VISIBLE names them, so
the digest says what it cannot see instead of counting them green.

Permission: logs:FilterLogEvents on the log group (AA-CIS-Infra, ECS task role; the request is recorded in
docs/implementation-notes/AA-1003.md, "R2 follow-up requests"). Without it, or when CloudWatch does not answer,
read_day raises LogsUnavailable: the KPI computer then writes no value and says why in the job result, and the OPS-12
digest prints the reason. A day older than the log retention (30 days, AA-CIS-Infra modules/ecs) has no events left,
so it is refused the same way rather than reported as "every probe missing".

Bounded: both callers hold a pool connection while this runs (kpi_snapshot_daily, report_generate), so the client
is the ops overview one (api/services/ops_overview/cloudwatch.py: connect 2 s, read 5 s, standard retries, at most 3
tries a call) and the paging loop has a time budget (FETCH_BUDGET_S, checked between pages) and a page cap
(MAX_PAGES). A search that does not finish within either raises LogsUnavailable("incomplete"): a partial read could
call a probe that ran "missing", so no partial verdict is ever counted.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from services.reports import periods

STREAM_PREFIX = "migrate/migrate/"
# Either line (CloudWatch filter syntax: ?term ?term = OR). Quoted terms match the exact phrase.
FILTER_PATTERN = '?"run-script: file=" ?"SCRIPT verdict"'
LOG_RETENTION_DAYS = 30          # AA-CIS-Infra modules/ecs/main.tf aws_cloudwatch_log_group.ecs retention_in_days
MAX_PAGES = 20                   # 1 MB / 10,000 events a page; a night has a handful of matching lines
FETCH_BUDGET_S = 20.0            # the whole paged search, checked between pages; one call <= ~3 x (2 s + 5 s)

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
                 budget_s: float = FETCH_BUDGET_S, clock: Callable[[], float] = time.monotonic) -> list[dict]:
    """Every matching event of [start, end) (sync: run it in a thread). Raises LogsUnavailable("incomplete") when the
    search still has a next page after MAX_PAGES pages or after `budget_s` seconds."""
    kwargs = {"logGroupName": group, "logStreamNamePrefix": STREAM_PREFIX, "filterPattern": FILTER_PATTERN,
              "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000) - 1}
    events: list[dict] = []
    started = clock()
    for page_no in range(1, MAX_PAGES + 1):
        page = client.filter_log_events(**kwargs)
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


def read_events(day: date, client: Any = None, now: Optional[datetime] = None) -> tuple[list[dict], dict]:
    """The matching events of an ICT day and the window read. Raises LogsUnavailable."""
    if day < periods.today_ict(now) - timedelta(days=LOG_RETENTION_DAYS - 1):
        raise LogsUnavailable("too_old", f"{day.isoformat()} is older than the {LOG_RETENTION_DAYS}-day log retention")
    start, end = periods.day_bounds(day)
    group = log_group()
    try:
        events = fetch_events(client or _client(), group, start, end)
    except LogsUnavailable:
        raise
    except Exception as e:  # noqa: BLE001 — AWS / network: the caller degrades and says why
        from api.services.ops_overview import cloudwatch

        code = _error_code(e)
        if cloudwatch.is_denied(e):
            raise LogsUnavailable("not_permitted", "the CIS task role may not read the probe log yet "
                                                   "(logs:FilterLogEvents on the log group is pending)") from e
        raise LogsUnavailable("aws_error", f"CloudWatch Logs did not answer ({code})") from e
    return events, {"log_group": group, "start": start.isoformat(), "end": end.isoformat()}


async def read_day(day: date, client: Any = None, now: Optional[datetime] = None) -> dict:
    """{"window": {...}, "probes": [ProbeResult...], "passed": n, "expected": n}. Raises LogsUnavailable."""
    events, window = await asyncio.to_thread(read_events, day, client, now)
    results = summarize(group_runs(events))
    return {"window": window, "probes": results, "passed": sum(1 for r in results if r.status == "passed"),
            "expected": len(PROBES)}
