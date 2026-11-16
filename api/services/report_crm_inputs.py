"""AA-1003 R6 — what the partner and agency reports read from the partner CRM's contract ``crm.ops.v1``.

The CRM accepts only a staff member's own WORKFORCE token on ``/crm/ops/v1`` (root ADR 0006b decision 6) and no AA role
reads schema ``crm`` (root ADR 0008a decision 1: no cross-schema grant, everything over HTTP contracts), so the report
worker cannot read the CRM by itself. When an administrator presses Generate now on Platform › Reports
(``POST /admin/reports/{id}/run``), the API reads the parts the report needs here, with that administrator's token
(``platform_apps.relay_authorization``), and puts them into the report_generate job's payload (``inputs``); the worker
renders them (services/reports/partners.py). A scheduled run has no staff session: its CRM part reads "not read".

    capture(report_id, first, last, authorization) -> None | {"contract", "read_at", "parts": {name: dict}}

Parts (each a dict, or ``{"error": ...}`` when it could not be read; never raises):
  coverage   GET /data-coverage: agencies per lifecycle status (synthetic counted apart), members per status. Counts
             only; the one read the CRM writes to no agency's audit log (crm.ops.v1 1.4).
  agencies   GET /tenants (every page): per agency its id, state, synthetic flag, whether it has an active owner, its
             creation time and lock source. The agency name is dropped here (outputs show an id prefix only). One
             ``ops_read`` event per agency in its own access log.
  usage      GET /tenants/{id}/usage per agency that is not offboarded: seats used / allowed, members active in 30
             days, AA's AI cost and budget this calendar month, failed jobs in 24 h, last activity. One ``ops_read``
             event per agency.
  llm_usage  GET /llm/usage for the period's UTC days: the CRM gateway's daily rows per stage and model, summed over
             every agency (calls, failed calls, tokens, cost).
  llm_calls  GET /llm/calls for the period's UTC days (every page), reduced here to numbers per agency and in total
             (calls, refused, errors, cost, latency p95); shadow rows are left out (the gateway rule). One ``ops_read``
             event per agency whose rows were returned. The rows themselves are never stored.

No business figure (trip, pipeline, revenue) exists in crm.ops.v1, so none reaches a report from here.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from statistics import quantiles
from typing import Any, Optional

import httpx
import structlog

from api.services import crm_console, platform_apps

logger = structlog.get_logger()

CONTRACT = "crm.ops.v1"
# Which parts each report reads (the reports of services/reports/partners.py).
PARTS: dict[str, tuple[str, ...]] = {
    "EX-03": ("coverage", "agencies", "usage"),
    "EX-07": ("agencies",),
    "CRM-04": ("agencies", "usage", "llm_usage", "llm_calls"),
}
TENANT_PAGE_SIZE = 200   # technical-constant: the crm.ops.v1 Limit maximum of GET /tenants (every page is read)
CALLS_PAGE_SIZE = 500    # technical-constant: the crm.ops.v1 maximum of GET /llm/calls (every page is read)
AGENCY_FIELDS = ("tenant_id", "state", "is_synthetic", "has_owner", "created_at", "lock_source")
USAGE_FIELDS = ("seats_used", "seats_allowed", "active_members_30d", "ai_cost_usd_month", "ai_budget_usd_month",
                "failed_jobs_24h", "last_activity_at", "period_start")


def needs_inputs(report_id: str) -> bool:
    return report_id in PARTS


def _problem(status: int, body: dict) -> dict:
    detail = body.get("detail") if isinstance(body, dict) else None
    return {"error": f"crm.ops.v1 answered {status}" + (f" ({detail})" if isinstance(detail, str) else "")}


async def coverage(authorization: Optional[str]) -> dict:
    status, body = await crm_console.crm_ops_call("GET", "/data-coverage", authorization)
    if status != 200:
        return _problem(status, body)
    return {"as_of": body.get("as_of"), "agencies": body.get("agencies") or {}, "members": body.get("members") or {}}


async def agencies(authorization: Optional[str]) -> dict:
    """Every agency, page by page (cursor = the last tenant id); never cut at a fixed count."""
    rows: list[dict] = []
    cursor: Optional[str] = None
    seen: set[str] = set()
    while True:
        status, body = await crm_console.crm_ops_call(
            "GET", "/tenants", authorization, params={"limit": TENANT_PAGE_SIZE, "cursor": cursor})
        if status != 200:
            return _problem(status, body)
        for item in body.get("items") or []:
            if isinstance(item, dict) and item.get("tenant_id"):
                rows.append({k: item.get(k) for k in AGENCY_FIELDS})
        cursor = body.get("next_cursor")
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    return {"rows": rows}


async def usage(authorization: Optional[str], agency_rows: list[dict]) -> dict:
    """The usage meters of every agency that is not offboarded; one failed agency is noted, the rest still count."""
    out: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for a in agency_rows:
        tid = str(a["tenant_id"])
        if a.get("state") == "offboarded":
            continue
        status, body = await crm_console.crm_ops_call("GET", f"/tenants/{tid}/usage", authorization)
        if status != 200:
            errors[tid] = _problem(status, body)["error"]
            continue
        out[tid] = {k: body.get(k) for k in USAGE_FIELDS}
    if errors and not out:
        return {"error": next(iter(errors.values()))}
    return {"by_agency": out, "unreadable": errors}


async def llm_usage(authorization: Optional[str], first: date, last: date,
                    client: Optional[httpx.AsyncClient] = None) -> dict:
    body, problem = await platform_apps.crm_llm_usage_rows(authorization, first, last, client)
    if body is None:
        return {"error": f"crm.ops.v1 /llm/usage: {problem.get('status')} ({problem.get('reason')})"}
    rows = [r for r in body.get("rows") or [] if isinstance(r, dict)]
    return {"rows": [{k: r.get(k) for k in ("day", "stage", "model", "calls", "failed_calls", "cost_usd")}
                     for r in rows]}


def _p95(values: list[int]) -> Optional[float]:
    vals = sorted(values)
    if not vals:
        return None
    if len(vals) == 1:
        return float(vals[0])
    return round(quantiles(vals, n=20, method="inclusive")[18], 1)


def reduce_calls(rows: list[dict]) -> dict:
    """crm.ops.v1 LlmCallRow rows -> numbers per agency and in total (shadow rows left out). Pure."""
    per: dict[str, dict] = {}
    lat: dict[str, list[int]] = {}
    total = {"calls": 0, "refused": 0, "errors": 0, "unusable": 0, "cost_usd": 0.0, "shadow_excluded": 0}
    all_lat: list[int] = []
    by_stage: dict[str, dict] = {}
    for r in rows:
        if r.get("is_shadow"):
            total["shadow_excluded"] += 1
            continue
        tid = str(r.get("tenant_id"))
        p = per.setdefault(tid, {"calls": 0, "refused": 0, "errors": 0, "cost_usd": 0.0})
        try:
            cost = float(r.get("cost_usd") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        status = r.get("status")
        refused = status == "refused" or bool(r.get("refusal_code"))
        error = status == "error"
        for bucket in (p, total):
            bucket["calls"] += 1
            bucket["refused"] += int(refused)
            bucket["errors"] += int(error)
            bucket["cost_usd"] = round(bucket["cost_usd"] + cost, 6)
        total["unusable"] += int(status in ("billed_unusable", "truncated"))
        st = by_stage.setdefault(str(r.get("stage")), {"calls": 0, "refused": 0})
        st["calls"] += 1
        st["refused"] += int(refused)
        if isinstance(r.get("latency_ms"), int):
            lat.setdefault(tid, []).append(r["latency_ms"])
            all_lat.append(r["latency_ms"])
    for tid, p in per.items():
        p["latency_p95_ms"] = _p95(lat.get(tid, []))
    total["latency_p95_ms"] = _p95(all_lat)
    return {"by_agency": per, "total": total, "by_stage": by_stage}


async def llm_calls(authorization: Optional[str], first: date, last: date,
                    client: Optional[httpx.AsyncClient] = None) -> dict:
    rows: list[dict] = []
    cursor: Optional[str] = None
    seen: set[str] = set()
    while True:
        params = {"from": first.isoformat(), "to": last.isoformat(), "limit": CALLS_PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        body, problem = await platform_apps.crm_llm_calls(authorization, params, client)
        if body is None:      # a missing page makes every total wrong: the part is unreadable, never partial
            return {"error": f"crm.ops.v1 /llm/calls: {problem.get('status')} ({problem.get('reason')})"}
        rows += [r for r in body.get("rows") or [] if isinstance(r, dict)]
        cursor = body.get("next_cursor")
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    return reduce_calls(rows)


async def _safe(name: str, coro) -> dict:
    try:
        return await coro
    except Exception as e:      # noqa: BLE001 — one unreadable part must not stop the run
        logger.warning("report_crm_input_failed", part=name, error=type(e).__name__)
        return {"error": f"{type(e).__name__}"}


async def capture(report_id: str, first: date, last: date, authorization: Optional[str]) -> Optional[dict]:
    """The crm.ops.v1 parts `report_id` reads, with the caller's own token; None for any other report. Never
    raises; a part that could not be read is ``{"error"}``."""
    names = PARTS.get(report_id)
    if not names:
        return None
    parts: dict[str, Any] = {}
    if "coverage" in names:
        parts["coverage"] = await _safe("coverage", coverage(authorization))
    if "agencies" in names:
        parts["agencies"] = await _safe("agencies", agencies(authorization))
    if "usage" in names:
        rows = parts.get("agencies", {}).get("rows")
        parts["usage"] = (await _safe("usage", usage(authorization, rows)) if isinstance(rows, list)
                          else {"error": "the agency list could not be read"})
    if "llm_usage" in names or "llm_calls" in names:
        async with httpx.AsyncClient(timeout=platform_apps.HTTP_TIMEOUT_S,
                                     transport=crm_console._transport) as client:
            if "llm_usage" in names:
                parts["llm_usage"] = await _safe("llm_usage", llm_usage(authorization, first, last, client))
            if "llm_calls" in names:
                parts["llm_calls"] = await _safe("llm_calls", llm_calls(authorization, first, last, client))
    logger.info("report_crm_inputs", report_id=report_id, parts=sorted(parts),
                unreadable=sorted(k for k, v in parts.items() if "error" in v))
    return {"contract": CONTRACT, "read_at": datetime.now(timezone.utc).isoformat(), "parts": parts}
