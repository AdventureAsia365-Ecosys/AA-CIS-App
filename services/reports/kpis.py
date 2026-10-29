"""AA-1003 R1 — the KPIs job kind kpi_snapshot_daily computes, from CIS tables only (no LLM, no paid API, no AWS call).

Each computer reads stored rows for one ICT day and returns KpiValue rows; the job writes them to shared.kpi_daily.
The set of computers must equal the KPIs marked `snapshot: {job: kpi_snapshot_daily}` in the registry (unit test).
Table and column names: root docs/architecture/db-schema-reference.md (shared.job, shared.llm_call_log,
gold_aa_internal.v_tour_liveness, shared.report_run).

Point-in-time KPIs (tours live) describe "now", so they are written only for the latest closed day, never back-filled.

AA-1003 R2 adds two computers that read outside the CIS tables:
- `inquiry_handoff` (TP-03): the ops.overview.v1 contract views of TripPlanner and Booking (cis_app has SELECT),
  services/reports/handoff.py. Those views count per UTC day, so these KPIs describe UTC day D (closed at 17:00 ICT of
  D, long before the 00:30 UTC snapshot). The views belong to other apps: a revoked grant or a view the owner
  dropped or reshaped (handoff.CONTRACT_ERRORS) raises Unavailable like the probe log below.
- `nightly_probes` (DATA-01): the probe verdict lines in CloudWatch Logs (services/reports/probes.py). When the log
  cannot be read (permission pending, AWS error, older than the log retention) it raises Unavailable: no value is
  written, the reason lands in the job result under `unavailable`, and the job still succeeds (an outside source
  that is down must not hold back the daily reports).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Awaitable, Callable, Optional

from services.reports import handoff, periods, probes

SCOPE_ALL = "all"
_SCOPE_VALUE = re.compile(r"[^A-Za-z0-9_.-]+")


def scope(kind: str, value: Optional[str]) -> str:
    """'app:cis', 'provider:bedrock'; a value outside the scope CHECK alphabet is normalised, empty -> unknown."""
    v = _SCOPE_VALUE.sub("_", (value or "").strip())[:80] or "unknown"
    return f"{kind}:{v}"


@dataclass(frozen=True)
class KpiValue:
    kpi_id: str
    scope: str
    value: Decimal
    source: str
    data_as_of: Optional[datetime] = None


@dataclass(frozen=True)
class DayContext:
    day: date
    start: datetime          # ICT day start (aware)
    end: datetime            # ICT day end, exclusive
    latest: bool             # day is the newest day there is (point-in-time KPIs run only then)

    @classmethod
    def for_day(cls, day: date, now: Optional[datetime] = None) -> "DayContext":
        start, end = periods.day_bounds(day)
        return cls(day=day, start=start, end=end,
                   latest=day in (periods.latest_closed_day(now), periods.snapshot_day(now)))


class Unavailable(Exception):
    """A computer whose outside source cannot be read now: no value, the reason is kept in the job result."""


def _dec(v) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


JOB_FAILED_SQL = """
    SELECT count(*) FROM shared.job
     WHERE status = 'failed' AND finished_at >= $1 AND finished_at < $2
"""

# started_at keeps the first attempt's start; a retried row's run_after can be later than it, hence greatest(0, ...)
JOB_QUEUE_AGE_P95_SQL = """
    SELECT percentile_cont(0.95) WITHIN GROUP (
               ORDER BY greatest(0, extract(epoch FROM started_at - greatest(created_at, run_after))))
      FROM shared.job
     WHERE started_at >= $1 AND started_at < $2
"""

LLM_DAY_SQL = """
    SELECT coalesce(l.quality_signal ->> 'app', 'cis') AS app, l.provider,
           count(*)::bigint AS calls,
           count(*) FILTER (WHERE l.fallback_used)::bigint AS fallback_calls,
           coalesce(sum(l.cost_usd), 0) AS cost_usd
      FROM shared.llm_call_log l
     WHERE l.created_at >= $1 AND l.created_at < $2
     GROUP BY 1, 2
"""

TOURS_LIVE_SQL = "SELECT count(*) FROM gold_aa_internal.v_tour_liveness WHERE state = 'served'"

REPORT_FAILED_7D_SQL = """
    SELECT count(*) FROM shared.report_run
     WHERE status = 'failed' AND finished_at >= $1 AND finished_at < $2
"""


async def job_fail(conn, ctx: DayContext) -> list[KpiValue]:
    n = await conn.fetchval(JOB_FAILED_SQL, ctx.start, ctx.end)
    return [KpiValue("K-JOB-FAIL", scope("app", "cis"), _dec(n or 0), "shared.job")]


async def job_queue_age(conn, ctx: DayContext) -> list[KpiValue]:
    p95 = await conn.fetchval(JOB_QUEUE_AGE_P95_SQL, ctx.start, ctx.end)
    if p95 is None:             # no job started that day: no value (not zero)
        return []
    return [KpiValue("K-JOB-QUEUEAGE", scope("app", "cis"), _dec(round(float(p95), 1)), "shared.job")]


async def llm_day(conn, ctx: DayContext) -> list[KpiValue]:
    """K-LLM-COST-DAY (all, per app, per provider) and K-LLM-FALLBACK-24H (all) from one grouped read."""
    rows = await conn.fetch(LLM_DAY_SQL, ctx.start, ctx.end)
    total = Decimal(0)
    calls = fallback = 0
    by_app: dict[str, Decimal] = {}
    by_provider: dict[str, Decimal] = {}
    for r in rows:
        cost = _dec(r["cost_usd"] or 0)
        total += cost
        calls += int(r["calls"] or 0)
        fallback += int(r["fallback_calls"] or 0)
        a, p = scope("app", r["app"]), scope("provider", r["provider"])
        by_app[a] = by_app.get(a, Decimal(0)) + cost
        by_provider[p] = by_provider.get(p, Decimal(0)) + cost
    src = "shared.llm_call_log"
    out = [KpiValue("K-LLM-COST-DAY", SCOPE_ALL, total, src)]
    out += [KpiValue("K-LLM-COST-DAY", s, v, src) for s, v in sorted(by_app.items())]
    out += [KpiValue("K-LLM-COST-DAY", s, v, src) for s, v in sorted(by_provider.items())]
    if calls:
        out.append(KpiValue("K-LLM-FALLBACK-24H", SCOPE_ALL, (Decimal(fallback) / Decimal(calls)).quantize(
            Decimal("0.0001")), src))
    return out


async def tours_live(conn, ctx: DayContext) -> list[KpiValue]:
    if not ctx.latest:
        return []
    n = await conn.fetchval(TOURS_LIVE_SQL)
    return [KpiValue("K-CAT-LIVE", SCOPE_ALL, _dec(n or 0), "gold_aa_internal.v_tour_liveness",
                     data_as_of=periods.now_utc())]


async def reports_failed(conn, ctx: DayContext) -> list[KpiValue]:
    n = await conn.fetchval(REPORT_FAILED_7D_SQL, ctx.start - timedelta(days=6), ctx.end)
    return [KpiValue("K-RPT-FAILED", SCOPE_ALL, _dec(n or 0), "shared.report_run")]


async def inquiry_handoff(conn, ctx: DayContext) -> list[KpiValue]:
    """K-TP-HANDOFFGAP, K-BK-PARKED, K-BK-INQ2CASE of UTC day ctx.day (TP-03)."""
    try:
        (d,) = await handoff.read_days(conn, ctx.day, ctx.day)
    except handoff.CONTRACT_ERRORS as e:
        raise Unavailable(f"contract_view: {type(e).__name__}: {str(e)[:200]}") from e
    src = "booking.v_ops_funnel_daily"
    out = [KpiValue("K-TP-HANDOFFGAP", SCOPE_ALL, _dec(d.gap), "tripplanner.v_ops_traffic_daily + " + src),
           KpiValue("K-BK-PARKED", SCOPE_ALL, _dec(d.parked + d.failed), src)]
    ratio = handoff.inq_to_case(d)
    if ratio is not None:
        out.append(KpiValue("K-BK-INQ2CASE", SCOPE_ALL, _dec(ratio), src))
    return out


async def nightly_probes(conn, ctx: DayContext) -> list[KpiValue]:
    """K-DATA-PROBES, K-DATA-LIVENESS, K-DATA-STALE from the probe runs of ICT day ctx.day (DATA-01)."""
    try:
        day = await probes.read_day(ctx.day)
    except probes.LogsUnavailable as e:
        raise Unavailable(f"{e.reason}: {e}") from e
    src = f"cloudwatch:{day['window']['log_group']}"
    out = [KpiValue("K-DATA-PROBES", SCOPE_ALL, (Decimal(day["passed"]) / Decimal(day["expected"])).quantize(
        Decimal("0.0001")), src)]
    for r in day["probes"]:
        if r.value is not None:
            out.append(KpiValue(r.probe.kpi_id, SCOPE_ALL, _dec(r.value), src, data_as_of=r.ran_at))
    return out


Computer = Callable[[object, DayContext], Awaitable[list[KpiValue]]]

# name -> (computer, the KPI IDs it writes). Order = run order.
COMPUTERS: dict[str, tuple[Computer, tuple[str, ...]]] = {
    "job_fail": (job_fail, ("K-JOB-FAIL",)),
    "job_queue_age": (job_queue_age, ("K-JOB-QUEUEAGE",)),
    "llm_day": (llm_day, ("K-LLM-COST-DAY", "K-LLM-FALLBACK-24H")),
    "tours_live": (tours_live, ("K-CAT-LIVE",)),
    "reports_failed": (reports_failed, ("K-RPT-FAILED",)),
    "inquiry_handoff": (inquiry_handoff, ("K-TP-HANDOFFGAP", "K-BK-PARKED", "K-BK-INQ2CASE")),
    "nightly_probes": (nightly_probes, ("K-DATA-PROBES", "K-DATA-LIVENESS", "K-DATA-STALE")),
}


def computed_kpi_ids() -> set[str]:
    return {k for _, ids in COMPUTERS.values() for k in ids}
