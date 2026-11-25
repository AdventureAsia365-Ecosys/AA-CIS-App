"""AA-1142 (S292, P2) — the per-agency drill-down of the Ecosystem status page (S285 review idea 12, Honeycomb style):
which agency an AI spend, a failed job or an AI error comes from, over the page's time range.

``GET /admin/overview/status/tenants?range=&from=&to=&tenant=`` (ops.read):

- without ``tenant``: three rankings of at most ``cis.ops.status_tenant_top_n`` agencies (CIS 486, default 10): AI
  spend (USD) and AI calls, failed jobs, failed AI calls, API server errors (S293); plus the platform's own share
  (calls without an agency);
- with ``tenant`` (an agency's id): that agency only, its AI spend and failures by app x stage, its failed jobs by
  kind (count and last time; no error text: a job error may quote tenant content), its failed AI calls by error class,
  its API calls and server errors with the endpoints that answered 5xx (route templates, at most the top-N setting).

Sources (free reads, no new grant): ``shared.ai_usage_hourly`` (the quarter-hour rollup, live traffic, ``tenant_id``)
for spend and AI failures, ``shared.job`` (``payload ->> 'tenant_id'``, status failed, finished in the range) for failed
jobs, ``shared.tenant_api_usage`` (S293: the metered ``/v1/*`` call log, one row per call of an agency's own systems,
``actor_type = 'tenant'``, kept ``cis.retention.tenant_api_usage_days``) for server errors (HTTP 5xx) per agency,
``shared.tenants`` for names.

Not measured, said in ``not_measured``: the partner CRM's jobs per agency (the crm.ops.v1 contract has no tenant
breakdown). Partly measured, said in ``server_errors_scope``: the server-error count covers the agencies' metered API
calls only, not their portal sessions (not metered, AA-760: only in the request log lines), not a request whose route
crashed before answering (Starlette answers that 500 outside the metering middleware, so it has no usage row), and not
the Booking or CRM APIs (no tenant dimension at API Gateway or the load balancer).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from api.services.ops_status.ranges import TimeRange
from shared import ops_status_thresholds as th

AI_SQL = """
    SELECT tenant_id::text AS tenant_id, sum(cost_usd)::float8 AS cost_usd, sum(requests)::bigint AS requests,
           coalesce(sum(requests) FILTER (WHERE status <> 'ok'), 0)::bigint AS failed
      FROM shared.ai_usage_hourly
     WHERE hour >= $1 AND hour < $2 AND traffic = 'live'
     GROUP BY tenant_id
"""
JOBS_SQL = """
    SELECT payload ->> 'tenant_id' AS tenant_id, count(*)::int AS failed, max(coalesce(finished_at, updated_at)) AS last
      FROM shared.job
     WHERE status = 'failed' AND coalesce(finished_at, updated_at) >= $1 AND coalesce(finished_at, updated_at) < $2
       AND payload ? 'tenant_id'
     GROUP BY 1
"""
NAMES_SQL = "SELECT tenant_id::text AS tenant_id, name, slug, is_active FROM shared.tenants"
ONE_AI_SQL = """
    SELECT app, coalesce(stage, '') AS stage, sum(cost_usd)::float8 AS cost_usd, sum(requests)::bigint AS requests,
           coalesce(sum(requests) FILTER (WHERE status <> 'ok'), 0)::bigint AS failed
      FROM shared.ai_usage_hourly
     WHERE hour >= $1 AND hour < $2 AND traffic = 'live' AND tenant_id = $3::uuid
     GROUP BY 1, 2
     ORDER BY 3 DESC
"""
ONE_ERRORS_SQL = """
    SELECT coalesce(error_class, status) AS error_class, sum(requests)::bigint AS requests
      FROM shared.ai_usage_hourly
     WHERE hour >= $1 AND hour < $2 AND traffic = 'live' AND tenant_id = $3::uuid AND status <> 'ok'
     GROUP BY 1
     ORDER BY 2 DESC
"""
ONE_JOBS_SQL = """
    SELECT kind, count(*)::int AS failed, max(coalesce(finished_at, updated_at)) AS last
      FROM shared.job
     WHERE status = 'failed' AND coalesce(finished_at, updated_at) >= $1 AND coalesce(finished_at, updated_at) < $2
       AND payload ->> 'tenant_id' = $3
     GROUP BY kind
     ORDER BY 2 DESC
"""
API_SQL = """
    SELECT tenant_id::text AS tenant_id, count(*)::bigint AS calls,
           count(*) FILTER (WHERE status_code >= 500)::bigint AS server_errors,
           max(called_at) FILTER (WHERE status_code >= 500) AS last
      FROM shared.tenant_api_usage
     WHERE called_at >= $1 AND called_at < $2 AND actor_type = 'tenant' AND tenant_id IS NOT NULL
     GROUP BY tenant_id
"""
ONE_API_SQL = """
    SELECT method, endpoint, count(*)::bigint AS server_errors, max(called_at) AS last
      FROM shared.tenant_api_usage
     WHERE called_at >= $1 AND called_at < $2 AND actor_type = 'tenant' AND tenant_id = $3::uuid
       AND status_code >= 500
     GROUP BY method, endpoint
     ORDER BY 3 DESC, 2
     LIMIT $4
"""
ONE_API_TOTAL_SQL = """
    SELECT count(*)::bigint AS calls, count(*) FILTER (WHERE status_code >= 500)::bigint AS server_errors
      FROM shared.tenant_api_usage
     WHERE called_at >= $1 AND called_at < $2 AND actor_type = 'tenant' AND tenant_id = $3::uuid
"""
NOT_MEASURED = ("crm_jobs_per_agency",)
# What the server-error figure leaves out (see the module docstring); the page says it next to the ranking.
SERVER_ERRORS_SCOPE = ("metered_api_calls_only", "portal_sessions", "crashed_requests", "booking_crm_apis")


class TenantError(ValueError):
    pass


def check_tenant(value: Optional[str]) -> Optional[str]:
    """A tenant filter is an agency id (UUID); TenantError otherwise. Pure."""
    if value is None or value == "":
        return None
    try:
        return str(UUID(value))
    except ValueError as e:
        raise TenantError("tenant must be an agency id") from e


def hour_window(rng: TimeRange) -> tuple[datetime, datetime]:
    """The rollup hours that overlap the range (the rollup is hourly). Pure."""
    start = rng.start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    end = rng.end.astimezone(timezone.utc)
    end_h = end.replace(minute=0, second=0, microsecond=0)
    return start, end_h if end_h == end else end_h + timedelta(hours=1)


def _label(tid: Optional[str], names: dict[str, dict]) -> dict:
    n = names.get(tid or "") or {}
    return {"tenant_id": tid, "name": n.get("name"), "slug": n.get("slug"), "active": n.get("is_active")}


def _pct(part: int, whole: int) -> Optional[float]:
    return round(100.0 * part / whole, 2) if whole else None


def rankings(ai: list[dict], jobs: list[dict], names: dict[str, dict], top_n: int,
             api: Optional[list[dict]] = None) -> dict:
    """Top agencies by AI spend, failed jobs, failed AI calls and API server errors; the platform's own AI share (no
    agency). Pure."""
    api = api or []
    server = sorted((r for r in api if r.get("tenant_id") and r.get("server_errors")),
                    key=lambda r: (-r["server_errors"], -(r.get("calls") or 0)))
    agencies = [r for r in ai if r.get("tenant_id")]
    platform = next((r for r in ai if not r.get("tenant_id")), None)
    spend = sorted((r for r in agencies if (r.get("cost_usd") or 0) > 0 or r.get("requests")),
                   key=lambda r: (-(r.get("cost_usd") or 0), -(r.get("requests") or 0)))
    failed_ai = sorted((r for r in agencies if r.get("failed")), key=lambda r: -r["failed"])
    failed_jobs = sorted((r for r in jobs if r.get("tenant_id") and r.get("failed")), key=lambda r: -r["failed"])
    return {
        "ai_spend": [{**_label(r["tenant_id"], names), "cost_usd": round(r.get("cost_usd") or 0.0, 4),
                      "requests": int(r.get("requests") or 0)} for r in spend[:top_n]],
        "failed_jobs": [{**_label(r["tenant_id"], names), "failed": r["failed"],
                         "last": r["last"].isoformat() if r.get("last") else None} for r in failed_jobs[:top_n]],
        "ai_errors": [{**_label(r["tenant_id"], names), "failed": int(r["failed"]),
                       "requests": int(r.get("requests") or 0),
                       "failed_pct": round(100.0 * r["failed"] / r["requests"], 2) if r.get("requests") else None}
                      for r in failed_ai[:top_n]],
        "server_errors": [{**_label(r["tenant_id"], names), "server_errors": int(r["server_errors"]),
                           "calls": int(r.get("calls") or 0),
                           "error_pct": _pct(int(r["server_errors"]), int(r.get("calls") or 0)),
                           "last": r["last"].isoformat() if r.get("last") else None} for r in server[:top_n]],
        "api_calls": sum(int(r.get("calls") or 0) for r in api if r.get("tenant_id")),
        "platform": {"cost_usd": round((platform or {}).get("cost_usd") or 0.0, 4),
                     "requests": int((platform or {}).get("requests") or 0),
                     "failed": int((platform or {}).get("failed") or 0)},
        "agencies_seen": len({r["tenant_id"] for r in agencies} | {r["tenant_id"] for r in jobs if r.get("tenant_id")}
                             | {r["tenant_id"] for r in api if r.get("tenant_id")}),
    }


def options(names: dict[str, dict]) -> list[dict]:
    """The tenant filter's choices: every agency by name. Pure."""
    return sorted(({"tenant_id": k, "name": v.get("name"), "slug": v.get("slug"), "active": v.get("is_active")}
                   for k, v in names.items()), key=lambda r: ((r["name"] or "").lower(), r["tenant_id"]))


async def collect(pool: Any, rng: TimeRange, tenant: Optional[str], now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    h0, h1 = hour_window(rng)
    names = {r["tenant_id"]: dict(r) for r in await pool.fetch(NAMES_SQL)}
    out: dict[str, Any] = {"range": rng.as_dict(), "generated_at": now.isoformat(), "tenant": tenant,
                           "top_n": th.tenant_top_n(), "top_n_key": th.TENANT_TOP_N, "options": options(names),
                           "not_measured": list(NOT_MEASURED), "server_errors_scope": list(SERVER_ERRORS_SCOPE)}
    if tenant is None:
        ai, jobs, api = await asyncio.gather(pool.fetch(AI_SQL, h0, h1), pool.fetch(JOBS_SQL, rng.start, rng.end),
                                             pool.fetch(API_SQL, rng.start, rng.end))
        return {**out, **rankings([dict(r) for r in ai], [dict(r) for r in jobs], names, th.tenant_top_n(),
                                  [dict(r) for r in api])}
    ai, errors, jobs, api, api_total = await asyncio.gather(
        pool.fetch(ONE_AI_SQL, h0, h1, tenant), pool.fetch(ONE_ERRORS_SQL, h0, h1, tenant),
        pool.fetch(ONE_JOBS_SQL, rng.start, rng.end, tenant), pool.fetch(ONE_API_SQL, rng.start, rng.end, tenant, th.tenant_top_n()),
        pool.fetch(ONE_API_TOTAL_SQL, rng.start, rng.end, tenant))
    total = dict(api_total[0]) if api_total else {}
    calls, server_errors = int(total.get("calls") or 0), int(total.get("server_errors") or 0)
    by_stage = [{"app": r["app"], "stage": r["stage"] or None, "cost_usd": round(r["cost_usd"] or 0.0, 4),
                 "requests": int(r["requests"] or 0), "failed": int(r["failed"] or 0)} for r in ai]
    return {**out, "agency": {**_label(tenant, names), "known": tenant in names,
                              "cost_usd": round(sum(r["cost_usd"] for r in by_stage), 4),
                              "requests": sum(r["requests"] for r in by_stage),
                              "ai_failed": sum(r["failed"] for r in by_stage),
                              "jobs_failed": sum(int(r["failed"]) for r in jobs),
                              "api_calls": calls, "server_errors": server_errors,
                              "server_error_pct": _pct(server_errors, calls),
                              "server_error_endpoints": [
                                  {"method": r["method"], "endpoint": r["endpoint"],
                                   "server_errors": int(r["server_errors"]),
                                   "last": r["last"].isoformat() if r.get("last") else None} for r in api],
                              "by_stage": by_stage,
                              "ai_errors": [{"error_class": r["error_class"], "requests": int(r["requests"])}
                                            for r in errors],
                              "failed_jobs": [{"kind": r["kind"], "failed": r["failed"],
                                               "last": r["last"].isoformat() if r.get("last") else None}
                                              for r in jobs]}}
