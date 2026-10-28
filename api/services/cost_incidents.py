"""AA-665 — recorded cost incidents: the findings of a spend investigation, shown next to the spend they explain.

    incidents_overlapping(since, until, provider=None) -> list[CostIncident]
    async incident_view(pool, incident) -> dict

Spend made outside a job (a request handler, an ad-hoc script, or any row logged before job tracking) cannot be
attributed from the logs alone: such ``shared.dfs_call_log`` / ``shared.llm_call_log`` rows carry no job id and, for
platform-wide research, no tenant either. Once an incident has been investigated, its findings are recorded here and
Cost Operations › DataForSEO reads them:

  * the record (trigger, scope, failure point, cause, what changed since) is reviewed text, like any post-mortem;
  * its numbers are NOT typed in: ``incident_view`` reads them live from the logs over the incident window (calls, live
    calls, cost and keywords per endpoint, first and last paid call, the LLM stages the incident names), so the page
    and the logs can never disagree;
  * DataForSEO rows without a job inside an incident window are attributed to it (``attribution = "incident"``) by
    ``cost_ops._infer_unlinked``.

Records are code, not a table: incidents are rare, each one is investigated and reviewed anyway, and a code record
needs no migration and no edit screen. Add one with the investigation that backs it (a read-only script over the logs,
kept with the session audit). The text is user-facing: English, roles not personal names, no issue IDs.

LLM numbers: ``llm_call_log`` keeps raw rows for 180 days (retention_llm_call_log), then only the per-day rollup
``llm_call_daily``. When the raw rows of the window are gone, the view reads the rollup for the window's days and says
so (``llm_source = "daily"``: whole days, not the incident's hours).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

UTC = timezone.utc


@dataclass(frozen=True)
class CostIncident:
    id: str
    provider: str                    # "dfs" today; the spend family whose rows the incident explains
    title: str
    started_at: datetime             # incident window [started_at, ended_at), UTC
    ended_at: datetime
    trigger: str                     # what started the spend, and why no job id carries it
    scope: str                       # what the run covered
    failure_point: str               # where and how it stopped
    cause: str                       # why it could happen
    since_fixed: tuple[str, ...] = field(default_factory=tuple)   # what changed afterwards (shipped)
    llm_stages: tuple[str, ...] = field(default_factory=tuple)    # llm_call_log stages the same run wrote
    investigated: str = ""           # how the findings were established


INCIDENTS: tuple[CostIncident, ...] = (
    CostIncident(
        id="2026-09-25-dfs-research-sweep",
        provider="dfs",
        title="DataForSEO research sweep after a tenant rewrite (balance exhausted)",
        started_at=datetime(2026, 9, 25, 5, 0, tzinfo=UTC),
        ended_at=datetime(2026, 9, 25, 11, 0, tzinfo=UTC),
        trigger=(
            "A tour rewrite by the test tenant WanderLux in the tenant portal finished at 05:04:21 UTC; 35 seconds "
            "later the rewrite's background step started search-demand research, and a second wave ran from about "
            "08:00 UTC while the same tenant's rewrites were being tested. The research ran as a background task of "
            "the API request, not as a job, so no logged call carries a job id or a tenant."
        ),
        scope=(
            "Every place behind the platform-wide Segments that had no fresh research (2,215 of 2,222 places) "
            "times the triggering tenant's three buyer markets (AU, US, UK): keyword volumes in small bulk tasks "
            "(678 tasks, 4.5 keywords each on average) plus one keyword-ideas task per place, with Haiku choosing "
            "each step (stage a3_search_demand)."
        ),
        failure_point=(
            "The DataForSEO balance ran out during the second wave: the last paid call was at 08:51:02 UTC. "
            "Nothing stopped the run on the payment error, so from 09:00 to 10:59 UTC the failed lookups were saved "
            "as empty volumes and 1,795 places were marked researched although nothing was bought for them."
        ),
        cause=(
            "Search-demand research fired after every tenant rewrite and read every Segment on the platform with no "
            "filter (Segments had become platform-wide), with no spend budget, no balance check before the run and "
            "no stop on a payment error."
        ),
        since_fixed=(
            "Tenant actions never buy DataForSEO: research is an admin job with an explicit scope, markets, a "
            "pre-run estimate and a per-run budget.",
            "Spend budgets are checked before every paid DataForSEO and LLM call of a guarded job; a payment or "
            "auth error stops the whole run.",
            "Failed lookups are never cached and a place is marked researched only when its purchases succeeded; "
            "the 25/09 data was repaired (16,311 empty volume rows and 5,859 research-log rows removed).",
            "Volumes are bought in real bulk tasks (up to 1,000 keywords per task), and every DataForSEO call now "
            "logs the job that made it.",
        ),
        llm_stages=("a3_search_demand",),
        investigated=(
            "Investigated on 28/09/2026 from the DataForSEO and LLM call logs, the tenant rewrite records and the "
            "research cache. The numbers on this card are read live from the call logs over the incident window."
        ),
    ),
)


def incidents_overlapping(since: datetime, until: datetime, provider: Optional[str] = None,
                          incidents: tuple[CostIncident, ...] = INCIDENTS) -> list[CostIncident]:
    """Incidents whose window overlaps [since, until), oldest first."""
    out = [i for i in incidents
           if i.started_at < until and i.ended_at > since and (provider is None or i.provider == provider)]
    return sorted(out, key=lambda i: i.started_at)


INCIDENT_DFS_SQL = """
    SELECT endpoint,
           count(*)                                       AS calls,
           count(*) FILTER (WHERE fetched_live)           AS live_calls,
           coalesce(sum(cost_usd), 0)::float              AS cost_usd,
           coalesce(sum(keyword_count), 0)::bigint        AS keywords,
           count(*) FILTER (WHERE job_id IS NOT NULL)     AS linked_calls,
           min(created_at) FILTER (WHERE cost_usd > 0)    AS first_paid_at,
           max(created_at) FILTER (WHERE cost_usd > 0)    AS last_paid_at
      FROM shared.dfs_call_log
     WHERE created_at >= $1 AND created_at < $2
     GROUP BY endpoint
     ORDER BY cost_usd DESC, endpoint
"""

INCIDENT_LLM_SQL = """
    SELECT stage, count(*) AS calls, coalesce(sum(cost_usd), 0)::float AS cost_usd
      FROM shared.llm_call_log
     WHERE created_at >= $1 AND created_at < $2 AND stage = ANY($3::text[])
     GROUP BY stage
     ORDER BY stage
"""

INCIDENT_LLM_DAILY_SQL = """
    SELECT stage, coalesce(sum(calls), 0)::bigint AS calls, coalesce(sum(cost_usd), 0)::float AS cost_usd
      FROM shared.llm_call_daily
     WHERE day >= $1::date AND day <= $2::date AND stage = ANY($3::text[]) AND NOT is_shadow
     GROUP BY stage
     ORDER BY stage
"""


def _iso(v: Any) -> Optional[str]:
    return v.isoformat() if v is not None else None


async def incident_view(pool: Any, incident: CostIncident) -> dict[str, Any]:
    """The record plus its numbers read live from the logs over the incident window."""
    a, b = incident.started_at, incident.ended_at
    rows = await pool.fetch(INCIDENT_DFS_SQL, a, b)
    endpoints = [{
        "endpoint": r["endpoint"], "calls": int(r["calls"]), "live_calls": int(r["live_calls"]),
        "cost_usd": round(float(r["cost_usd"] or 0.0), 6), "keywords": int(r["keywords"] or 0),
    } for r in rows]
    paid_first = [r["first_paid_at"] for r in rows if r["first_paid_at"] is not None]
    paid_last = [r["last_paid_at"] for r in rows if r["last_paid_at"] is not None]

    llm: list[dict[str, Any]] = []
    llm_source = None
    if incident.llm_stages:
        stages = list(incident.llm_stages)
        llm_rows = await pool.fetch(INCIDENT_LLM_SQL, a, b, stages)
        llm_source = "raw"
        if not llm_rows:
            llm_rows = await pool.fetch(INCIDENT_LLM_DAILY_SQL, a.date(), b.date(), stages)
            llm_source = "daily" if llm_rows else None
        llm = [{"stage": r["stage"], "calls": int(r["calls"]), "cost_usd": round(float(r["cost_usd"] or 0.0), 6)}
               for r in llm_rows]

    return {
        "id": incident.id, "provider": incident.provider, "title": incident.title,
        "started_at": _iso(a), "ended_at": _iso(b),
        "trigger": incident.trigger, "scope": incident.scope, "failure_point": incident.failure_point,
        "cause": incident.cause, "since_fixed": list(incident.since_fixed), "investigated": incident.investigated,
        "dfs": {
            "calls": sum(e["calls"] for e in endpoints),
            "live_calls": sum(e["live_calls"] for e in endpoints),
            "cost_usd": round(sum(e["cost_usd"] for e in endpoints), 6),
            "linked_calls": sum(int(r["linked_calls"] or 0) for r in rows),
            "first_paid_at": _iso(min(paid_first)) if paid_first else None,
            "last_paid_at": _iso(max(paid_last)) if paid_last else None,
            "endpoints": endpoints,
        },
        "llm": {
            "source": llm_source,
            "calls": sum(s["calls"] for s in llm),
            "cost_usd": round(sum(s["cost_usd"] for s in llm), 6),
            "stages": llm,
        },
    }
