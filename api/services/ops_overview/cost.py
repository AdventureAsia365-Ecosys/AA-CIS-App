"""AA-858 — the "Cost per app" card of /admin/overview, built ONLY on AA-884's cost module.

``api.services.platform_cost.cost_by_app`` is the one place that computes spend in the admin console (S243 critic
finding 5); this module calls it for this month and last month and reshapes its rows into plain business labels.
No Cost Explorer call, no SQL on the cost snapshot here: ``/admin/overview`` and ``/admin/platform/cost`` therefore
always show the same numbers. The lazy CE refresh (> 24 h old or partial, Redis SET NX lock, one attempt per window
per day, ~$0.01) is the module's own; without Redis it never calls CE and returns the stored data.

Columns per row: ``aws_usd`` (acc2 bill by App tag), ``ai_outside_aws_usd`` (LLM spend not already on the acc2 bill:
acc1/acc3 Bedrock, OpenAI, Jev — gateway estimates), ``total_usd`` = the two added (= ``cost_by_app`` total_usd).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import structlog

from api.services import platform_cost

logger = structlog.get_logger()

TOP_SERVICES = 5

LABELS = {
    "cis": "CIS (content + catalog)",
    "tripplanner": "TripPlanner",
    "booking": "Booking",
    "tag:shared": "Shared platform",
    "tag:identity": "Staff sign-in (identity)",
    "tag:platform": "Platform tooling",
    "untagged": "Not assigned to an app",
}
HINTS = {
    "tag:shared": "AWS resources several apps use together (App tag 'shared').",
    "tag:identity": "The staff login service (App tag 'identity').",
    "tag:platform": "Build and deploy tooling (App tag 'platform').",
    "untagged": "AWS charges with no App tag: tax, support, data transfer and resources tagged before 16/10/2026. "
                "Per-app numbers are reliable from 16/10/2026.",
}
NOTES = [
    "AWS numbers come from the AWS bill and lag about one day.",
    "AI usage is an estimate from our own call log; most AI runs on other AWS accounts and providers, so it is not "
    "part of this AWS bill.",
]


def _prev_month(today: date) -> str:
    return (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")


def _row(r: dict) -> dict:
    key = r["app"]
    ai_outside = round(float(r.get("ai_usd") or 0.0) - float(r.get("ai_in_aws_acc2_usd") or 0.0), 4)
    out = {
        "key": key,
        "label": LABELS.get(key) or (f"AI for {key[4:]}" if key.startswith("llm:") else r.get("label") or key),
        "kind": r["kind"],
        "hint": HINTS.get(key),
        "aws_usd": round(float(r.get("aws_usd") or 0.0), 2),
        "ai_outside_aws_usd": round(ai_outside, 2),
        "total_usd": round(float(r.get("total_usd") or 0.0), 2),
        "prev_total_usd": None,
        "top_services": [],
    }
    if r["kind"] in ("untagged", "shared_tag", "other_tag"):
        out["top_services"] = [{"service": s["service"], "amount_usd": round(float(s["amount_usd"]), 2)}
                               for s in (r.get("services") or [])[:TOP_SERVICES]]
    return out


async def cost_card(pool: Any, redis: Any = None, now: Optional[datetime] = None) -> dict:
    """This month (to date) and last month, per app."""
    now = now or datetime.now(timezone.utc)
    cur = await platform_cost.cost_by_app(pool, None, redis=redis, refresh_if_stale=True, now=now)
    prev_month = _prev_month(now.date())
    try:
        prev = await platform_cost.cost_by_app(pool, prev_month, redis=redis, refresh_if_stale=True, now=now)
    except Exception as e:  # noqa: BLE001 — last month is a comparison only
        logger.warning("ops_overview_cost_prev_failed", error=repr(e)[:300])
        prev = None

    rows = [_row(r) for r in cur["apps"]]
    prev_totals = {r["app"]: round(float(r.get("total_usd") or 0.0), 2) for r in (prev or {}).get("apps", [])}
    for row in rows:
        row["prev_total_usd"] = prev_totals.get(row["key"], 0.0) if prev else None
    # Rows with nothing this month and nothing last month add noise (e.g. an app with no AI and no tag yet).
    rows = [r for r in rows if r["kind"] == "app" or r["total_usd"] or (r["prev_total_usd"] or 0)]

    aws = cur.get("aws") or {}
    refresh = cur.get("refresh") or {}
    return {
        "status": "ok",
        "month": cur["month"],
        "prev_month": prev_month,
        "period_start": cur["period_start"],
        "period_end_exclusive": cur["period_end_exclusive"],
        "has_aws_data": bool(aws.get("has_data")),
        "aws_fetched_at": aws.get("fetched_at"),
        "aws_covered_to": aws.get("covered_to"),
        "rows": rows,
        "totals": {
            "aws_usd": round(sum(r["aws_usd"] for r in rows), 2),
            "ai_outside_aws_usd": round(sum(r["ai_outside_aws_usd"] for r in rows), 2),
            "total_usd": round(sum(r["total_usd"] for r in rows), 2),
            "prev_total_usd": round(sum(prev_totals.values()), 2) if prev else None,
        },
        "refresh": {k: refresh.get(k) for k in ("attempted", "ok", "stale", "reason")},
        "notes": NOTES,
    }
