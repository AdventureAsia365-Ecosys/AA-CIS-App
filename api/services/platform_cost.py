"""AA-884 (S241 GĐ10) — cost per app: acc2 AWS spend by the App cost-allocation tag plus LLM spend by app.

The one cost module of the admin console (S243 critic finding 5): /admin/platform/cost (AA-884) and the cost card of
/admin/overview (AA-858) both call ``cost_by_app`` and never compute spend another way, so the two pages agree.

Public API (AA-858 imports these, read-only; do not fork the logic):

    async def cost_by_app(pool, month: str | None = None, *, redis=None, refresh_if_stale: bool = True,
                          now: datetime | None = None) -> dict
    async def refresh_app_snapshot(pool, start: date, end_exclusive: date) -> dict
    def month_window(month: str | None, today: date) -> tuple[date, date]
    async def ai_spend_by_provider(pool, month: str | None = None, now=None) -> dict

Sources, per app row:
- ``aws_usd``: acc2 Cost Explorer UnblendedCost grouped by TAG App (+ SERVICE), stored in
  ``shared.cost_explorer_app_snapshot`` (CIS 248), newest fetch per (tag, service, day). Matches the CE console for
  the same window. Untagged spend is its own row (``app = "untagged"``) with a per-service breakdown (AA-828 tags it).
  Tags shared by several apps (shared / identity / platform) are their own rows, never split.
- ``ai_usd``: LLM spend from ``shared.llm_call_log`` (days not rolled up yet) and ``shared.llm_call_daily`` (rolled
  days, AA-825), by ``app`` = ``quality_signal->>'app'`` (default ``cis``; TripPlanner writes ``tripplanner``). These
  are the gateway's price-table estimates, incl. OpenAI and Jev, which are not on any AWS bill.
- ``ai_in_aws_acc2_usd``: the part of ``ai_usd`` billed on acc2 itself (Bedrock calls with account acc2 or none),
  already inside ``aws_usd`` of some tag (usually untagged: Bedrock usage carries no App tag). ``total_usd`` =
  ``aws_usd`` + (``ai_usd`` - ``ai_in_aws_acc2_usd``), so acc2 Bedrock is not counted twice.
- ``llm_satellites``: the acc1 / acc3 Bedrock line from the SERVICE snapshot (``shared.cost_explorer_snapshot``, the
  External Spend page's data) next to the llm_call_log estimate for the same accounts — a cross-check, not added to
  any app (those calls are already in ``ai_usd`` by app).

One presentation (S243 UI verify, finding 4): row labels and hints come from ``ROW_LABELS`` / ``ROW_HINTS`` here (the
plain wording of the overview card), every row and the totals carry ``ai_outside_aws_usd`` (= ``ai_usd`` -
``ai_in_aws_acc2_usd``), so both pages show AWS + AI outside the AWS bill = Total, and the KPI strip can show the
deduction. ``ai_spend_by_provider`` gives the same AI spend per provider family for the legacy overview cost table.

Lazy refresh (critic resolution, S243): with ``refresh_if_stale`` and a Redis client, a window whose stored data is
missing, covers only part of the window (``POST /admin/cost-explorer/check`` stores its own, by default 7-day,
window), or is older than ``STALE_AFTER`` (24 h) while it may still change (fetched before the window's end + 3 days),
is fetched again — one CE request (~$0.01, ``CE_REQUEST_COST_USD``) — behind a Redis ``SET NX`` lock so concurrent
page views make one call. Every attempt, whatever its outcome (rows, no rows, AWS error), leaves a per-window Redis
marker (``ATTEMPT_MARKER_TTL_S`` = 24 h after a success, ``ATTEMPT_MARKER_ERROR_TTL_S`` = 6 h after an error); no CE
call is made for that window while it is set, so an empty CE answer (early on the 1st, a month outside CE retention)
is not re-fetched on every page view. Without Redis, or when the lock is taken, the stored data is returned as is.
Worst case: one call per viewed month window per day (the current month's window end moves daily) — about
$0.30/month for the current month — and at most 4 a day per window while CE keeps failing.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from api.core.app_registry import APPS, SHARED_COST_TAGS, app_for_cost_tag
from shared.aws_client import cost_explorer as ce

logger = structlog.get_logger()

STALE_AFTER = timedelta(hours=24)
FINAL_AFTER = timedelta(days=3)          # CE stops revising a day ~48 h after it ends
REFRESH_LOCK_KEY = "platform_cost:refresh_lock:v1"
REFRESH_LOCK_TTL_S = 120
ATTEMPT_MARKER_PREFIX = "platform_cost:attempted:v1"
ATTEMPT_MARKER_TTL_S = int(STALE_AFTER.total_seconds())   # after a success (rows or none): once a day per window
ATTEMPT_MARKER_ERROR_TTL_S = 6 * 3600                       # after an AWS error: retry in 6 h
CE_LAG = timedelta(days=2)               # CE may lack the last ~2 days of a window: not a coverage gap
CE_REQUEST_COST_USD = ce.CE_REQUEST_COST_USD
DEFAULT_LLM_APP = "cis"

# Rolled-up days (llm_call_daily, AA-825) + raw days not rolled up yet; a day is either rolled or raw, never both
# (the rollup writes a day once, before its raw rows are deleted). $1/$2 = UTC dates [start, end).
# billed_in_acc2: Bedrock on acc2 (or an older row without account) — already on acc2's AWS bill.
AI_SPEND_SQL = """
    WITH rolled AS (
        SELECT d.app, d.provider, d.account, sum(d.calls)::bigint AS calls, sum(d.cost_usd)::float AS cost_usd
          FROM shared.llm_call_daily d
         WHERE d.day >= $1 AND d.day < $2
         GROUP BY 1, 2, 3
    ), raw AS (
        SELECT coalesce(l.quality_signal ->> 'app', 'cis') AS app, l.provider, l.account,
               count(*)::bigint AS calls, coalesce(sum(l.cost_usd), 0)::float AS cost_usd
          FROM shared.llm_call_log l
         WHERE l.created_at >= ($1::date::timestamp AT TIME ZONE 'UTC')
           AND l.created_at < ($2::date::timestamp AT TIME ZONE 'UTC')
           AND NOT EXISTS (SELECT 1 FROM shared.llm_call_daily d
                            WHERE d.day = (l.created_at AT TIME ZONE 'UTC')::date)
         GROUP BY 1, 2, 3
    )
    SELECT app, provider, account, sum(calls)::bigint AS calls, sum(cost_usd)::float AS cost_usd,
           (coalesce(provider, '') LIKE 'bedrock%' AND coalesce(account, 'acc2') = 'acc2') AS billed_in_acc2
      FROM (SELECT * FROM rolled UNION ALL SELECT * FROM raw) u
     GROUP BY app, provider, account
     ORDER BY app, cost_usd DESC
"""


# One wording for every cost row on every page (the overview card and /admin/platform/cost both read these).
ROW_LABELS = {
    "untagged": "Not assigned to an app",
    "tag:shared": "Shared platform",
    "tag:identity": "Staff sign-in (identity)",
    "tag:platform": "Platform tooling",
}
ROW_HINTS = {
    "tag:shared": "AWS resources several apps use together (App tag 'shared').",
    "tag:identity": "The staff login service (App tag 'identity').",
    "tag:platform": "Build and deploy tooling (App tag 'platform').",
    "untagged": "AWS charges with no App tag: tax, support, data transfer and resources tagged before 16/10/2026. "
                "Per-app numbers are reliable from 16/10/2026.",
}


# Provider families of the AI spend (the provider column of llm_call_log / llm_call_daily -> the spend_budget
# provider the legacy overview cost table shows). Anything else is "other".
def provider_family(provider: Optional[str]) -> str:
    p = (provider or "").lower()
    if p.startswith("bedrock"):
        return "bedrock"
    if p in ("openai", "jev"):
        return p
    return "other"


def row_label(key: str, kind: str, default: str) -> str:
    if key in ROW_LABELS:
        return ROW_LABELS[key]
    if kind == "llm_app":
        return f"AI for {key[4:]}"
    if kind == "other_tag":
        return f"Other App tag: {key[4:]}"
    return default


def month_window(month: Optional[str], today: date) -> tuple[date, date]:
    """'YYYY-MM' (None = the current UTC month) -> [first day, end) with end = min(first of next month, tomorrow).
    ValueError on a malformed month or a month after the current one."""
    if month is None:
        start = today.replace(day=1)
    else:
        try:
            start = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            raise ValueError("month must be YYYY-MM")
    if start > today:
        raise ValueError("month is in the future")
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, min(next_month, today + timedelta(days=1))


def _is_stale(stored: Optional[dict], start: date, end_exclusive: date, now: datetime) -> tuple[bool, str]:
    """Per day of [start, end_exclusive): stale when a day before the CE lag (the window's last ``CE_LAG``) has no
    stored row — the stored data covers only part of the window, e.g. after ``POST /admin/cost-explorer/check``
    stored its own 7-day window — or when a stored day that CE may still revise (fetched less than
    ``FINAL_AFTER`` after the day ended) was fetched more than ``STALE_AFTER`` ago."""
    if not stored:
        return True, "no stored data for this window"
    day_fetched = {date.fromisoformat(d): datetime.fromisoformat(f)
                   for d, f in (stored.get("day_fetched_at") or {}).items()}
    must_have_until = end_exclusive - CE_LAG
    missing = [start + timedelta(days=i) for i in range((must_have_until - start).days)
               if start + timedelta(days=i) not in day_fetched]
    if missing:
        return True, (f"stored data covers only part of the window: {len(missing)} day(s) missing "
                      f"({missing[0].isoformat()}..{missing[-1].isoformat()})")
    old = [d for d, f in day_fetched.items()
           if f < datetime.combine(d + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) + FINAL_AFTER
           and now - f > STALE_AFTER]
    if old:
        return True, f"older than {int(STALE_AFTER.total_seconds() // 3600)} h ({len(old)} day(s) not final yet)"
    newest = datetime.fromisoformat(stored["fetched_at"])
    window_end = datetime.combine(end_exclusive, datetime.min.time(), tzinfo=timezone.utc)
    if newest >= window_end + FINAL_AFTER:
        return False, "final (fetched after the window closed)"
    return False, "fresh"


def _attempt_key(start: date, end_exclusive: date) -> str:
    return f"{ATTEMPT_MARKER_PREFIX}:{start.isoformat()}:{end_exclusive.isoformat()}"


async def refresh_app_snapshot(pool: Any, start: date, end_exclusive: date) -> dict:
    """One live CE fetch (acc2, by App tag) for [start, end_exclusive), stored. ~$0.01 per CE request.
    Raises ce.CostExplorerUnavailable on an AWS error."""
    result = await asyncio.to_thread(ce.fetch_acc2_cost_by_app, start.isoformat(), end_exclusive.isoformat())
    written = await ce.record_app_snapshot(pool, result["rows"])
    logger.info("platform_cost_app_snapshot_refreshed", start=start.isoformat(), end=end_exclusive.isoformat(),
                rows=written, ce_requests=result["requests"])
    return {"row_count": written, "ce_requests": result["requests"],
            "est_cost_usd": round(result["requests"] * CE_REQUEST_COST_USD, 2)}


async def _lazy_refresh(pool: Any, redis: Any, start: date, end_exclusive: date, now: datetime) -> dict:
    marker_key = _attempt_key(start, end_exclusive)
    try:
        marker = await redis.get(marker_key)
    except Exception as e:
        logger.warning("platform_cost_marker_read_failed", error=repr(e)[:200])
        return {"attempted": False, "ok": None, "reason": "attempt marker unavailable (Redis error), not refreshed"}
    if marker:
        if isinstance(marker, bytes):
            marker = marker.decode("utf-8", "replace")
        return {"attempted": False, "ok": None, "skipped": "recent_attempt",
                "reason": f"this window was already fetched from Cost Explorer recently ({marker}); "
                          "no new CE call until the marker expires"}
    try:
        got = await redis.set(REFRESH_LOCK_KEY, "1", nx=True, ex=REFRESH_LOCK_TTL_S)
    except Exception as e:
        logger.warning("platform_cost_lock_failed", error=repr(e)[:200])
        return {"attempted": False, "ok": None, "reason": "lock unavailable (Redis error)"}
    if not got:
        return {"attempted": False, "ok": None, "reason": "another refresh is running"}
    try:
        out = await refresh_app_snapshot(pool, start, end_exclusive)
        result = {"attempted": True, "ok": True, **out}
        marker_value, marker_ttl = f"{now.isoformat()} ok, {out['row_count']} rows", ATTEMPT_MARKER_TTL_S
    except Exception as e:
        logger.warning("platform_cost_lazy_refresh_failed", error=repr(e)[:300])
        result = {"attempted": True, "ok": False, "error": str(e)[:300]}
        marker_value, marker_ttl = f"{now.isoformat()} error", ATTEMPT_MARKER_ERROR_TTL_S
    finally:
        try:
            await redis.delete(REFRESH_LOCK_KEY)
        except Exception:
            pass
    try:
        await redis.set(marker_key, marker_value, ex=marker_ttl)
    except Exception as e:
        logger.warning("platform_cost_marker_write_failed", error=repr(e)[:200])
    return result


def _row(app: str, kind: str, label: str) -> dict:
    return {"app": app, "kind": kind, "label": row_label(app, kind, label), "hint": ROW_HINTS.get(app),
            "aws_usd": 0.0, "aws_bedrock_usd": 0.0, "services": [], "aws_tags": [], "ai_usd": 0.0,
            "ai_in_aws_acc2_usd": 0.0, "ai_outside_aws_usd": 0.0, "ai_calls": 0, "ai_by_source": []}


async def cost_by_app(pool: Any, month: Optional[str] = None, *, redis: Any = None, refresh_if_stale: bool = True,
                      now: Optional[datetime] = None) -> dict:
    """Cost per app for one UTC month (month-to-date for the current one). See the module docstring."""
    now = now or datetime.now(timezone.utc)
    start, end = month_window(month, now.date())
    stored = await ce.read_app_cost_range(pool, start, end)
    stale, why = _is_stale(stored, start, end, now)
    refresh: dict[str, Any] = {"attempted": False, "ok": None, "reason": why, "stale": stale}
    if stale and refresh_if_stale:
        if redis is None:
            refresh["reason"] = f"{why}; no Redis lock, not refreshed"
        else:
            refresh.update(await _lazy_refresh(pool, redis, start, end, now))
            refresh["stale_reason"] = why
            if refresh.get("ok"):
                stored = await ce.read_app_cost_range(pool, start, end)

    rows: dict[str, dict] = {a.id: _row(a.id, "app", a.label) for a in APPS.values()}
    for tag, t in ((stored or {}).get("tags") or {}).items():
        owner = app_for_cost_tag(tag)
        if owner is not None:
            key, kind, label = owner, "app", APPS[owner].label
        elif tag == "":
            key, kind, label = "untagged", "untagged", "Untagged (no App tag)"
        elif tag in SHARED_COST_TAGS:
            key, kind, label = f"tag:{tag}", "shared_tag", f"Shared: {tag}"
        else:
            key, kind, label = f"tag:{tag}", "other_tag", f"Tag: {tag}"
        r = rows.setdefault(key, _row(key, kind, label))
        r["aws_usd"] += t["total_usd"]
        r["aws_bedrock_usd"] += t["bedrock_usd"]
        r["aws_tags"].append(tag)
        r["services"].extend(t["services"])

    ai_rows = await pool.fetch(AI_SPEND_SQL, start, end)
    sat_estimate: dict[str, float] = {}
    for a in ai_rows:
        app = a["app"] or DEFAULT_LLM_APP
        key = app if app in rows else f"llm:{app}"
        r = rows.setdefault(key, _row(key, "llm_app", f"LLM app: {app}"))
        cost = float(a["cost_usd"] or 0.0)
        r["ai_usd"] += cost
        r["ai_calls"] += int(a["calls"] or 0)
        if a["billed_in_acc2"]:
            r["ai_in_aws_acc2_usd"] += cost
        r["ai_by_source"].append({"provider": a["provider"], "account": a["account"], "calls": int(a["calls"] or 0),
                                  "cost_usd": round(cost, 4), "billed_in_aws_acc2": bool(a["billed_in_acc2"])})
        if a["account"] in ("acc1", "acc3") and (a["provider"] or "").startswith("bedrock"):
            sat_estimate[a["account"]] = sat_estimate.get(a["account"], 0.0) + cost

    out_rows = []
    for r in rows.values():
        for k in ("aws_usd", "aws_bedrock_usd", "ai_usd", "ai_in_aws_acc2_usd"):
            r[k] = round(r[k], 4)
        r["ai_outside_aws_usd"] = round(r["ai_usd"] - r["ai_in_aws_acc2_usd"], 4)
        r["total_usd"] = round(r["aws_usd"] + r["ai_outside_aws_usd"], 4)
        r["services"].sort(key=lambda s: -s["amount_usd"])
        out_rows.append(r)
    order = {"app": 0, "llm_app": 1, "shared_tag": 2, "other_tag": 3, "untagged": 4}
    out_rows.sort(key=lambda r: (order.get(r["kind"], 9), -r["total_usd"]))

    satellites = await _satellites(pool, start, end, sat_estimate)
    aws_total = stored["total_usd"] if stored else None
    return {
        "month": start.strftime("%Y-%m"),
        "period_start": start.isoformat(), "period_end_exclusive": end.isoformat(),
        "aws": {"account_id": ce.ACC2_ACCOUNT_ID, "has_data": bool(stored), "total_usd": aws_total,
                "covered_from": (stored or {}).get("covered_from"), "covered_to": (stored or {}).get("covered_to"),
                "fetched_at": (stored or {}).get("fetched_at"),
                "untagged_usd": next((r["aws_usd"] for r in out_rows if r["kind"] == "untagged"), 0.0)},
        "apps": out_rows,
        "totals": {"aws_usd": aws_total, "ai_usd": round(sum(r["ai_usd"] for r in out_rows), 4),
                   "ai_in_aws_acc2_usd": round(sum(r["ai_in_aws_acc2_usd"] for r in out_rows), 4),
                   "ai_outside_aws_usd": round(sum(r["ai_outside_aws_usd"] for r in out_rows), 4),
                   "total_usd": round(sum(r["total_usd"] for r in out_rows), 4)},
        "llm_satellites": satellites,
        "refresh": refresh,
        "sources": {
            "aws_usd": "AWS Cost Explorer, acc2, UnblendedCost grouped by tag App (shared.cost_explorer_app_snapshot)",
            "ai_usd": "LLM gateway estimates by app (shared.llm_call_log / shared.llm_call_daily)",
            "ai_outside_aws_usd": "ai_usd - ai_in_aws_acc2_usd (Bedrock on acc2 is already in aws_usd)",
            "total_usd": "aws_usd + ai_outside_aws_usd",
            "llm_satellites": "acc1/acc3 Bedrock from Cost Explorer (shared.cost_explorer_snapshot) vs the estimate",
        },
    }


async def ai_spend_by_provider(pool: Any, month: Optional[str] = None, now: Optional[datetime] = None) -> dict:
    """This month's AI spend per provider family ({bedrock, openai, jev, other}) with the cost module's own query
    (raw + rolled-up days, every app), so its total equals ``cost_by_app(...)["totals"]["ai_usd"]``. DB only."""
    now = now or datetime.now(timezone.utc)
    start, end = month_window(month, now.date())
    rows = await pool.fetch(AI_SPEND_SQL, start, end)
    by_family: dict[str, float] = {}
    breakdown = []
    for r in rows:
        cost = float(r["cost_usd"] or 0.0)
        fam = provider_family(r["provider"])
        by_family[fam] = by_family.get(fam, 0.0) + cost
        breakdown.append({"app": r["app"], "provider": r["provider"], "account": r["account"],
                          "calls": int(r["calls"] or 0), "cost_usd": round(cost, 4)})
    return {"month": start.strftime("%Y-%m"),
            "by_provider": {k: round(v, 4) for k, v in by_family.items()},
            "total_usd": round(sum(by_family.values()), 4),
            "breakdown": breakdown}


async def _satellites(pool: Any, start: date, end: date, estimate: dict[str, float]) -> dict:
    """acc1 / acc3 Bedrock actual (CE SERVICE snapshot, External Spend data) vs the llm_call_log estimate."""
    try:
        data = await ce.read_cost_range(pool, start, end)
    except Exception as e:
        logger.warning("platform_cost_satellites_failed", error=repr(e)[:200])
        data = None
    by_id = {a["account_id"]: a for a in (data or {}).get("accounts", [])}
    out = {}
    for name, account_id in (("acc1", ce.ACC1_ACCOUNT_ID), ("acc3", ce.ACC3_ACCOUNT_ID)):
        acc = by_id.get(account_id)
        out[name] = {"account_id": account_id,
                     "ce_bedrock_usd": round(acc["bedrock_usd"], 4) if acc else None,
                     "llm_log_estimate_usd": round(estimate.get(name, 0.0), 4)}
    out["ce_fetched_at"] = (data or {}).get("fetched_at")
    return out
