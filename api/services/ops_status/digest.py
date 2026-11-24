"""AA-1141 (P1 feature 6) — the Ecosystem status part of the daily OPS-12 digest: the SAME verdict the status page shows
(``verdict.decide`` through ``build_status``, range 24 h, at digest time), so the e-mail and the page never disagree,
with a link to the right view of the page (S285 review idea 6, Better Stack / Opsgenie style).

The digest is the existing report OPS-12 (``services/reports/digest.py``): a ``report_generate`` job on ``shared.job``
that stores its run in ``shared.report_run``, shows it on Reports and e-mails the OPS-12 opt-in list. ``run_report``
builds this part on the job's pool and Redis (the page's own cache: the CloudWatch signals cost nothing more when the
page was opened in the last 5 minutes) and hands it to the renderer as an input; a failure leaves the part out with
its reason, never the digest. Push for SEV1 / SEV2 not acknowledged after N minutes is P2 (not built): the digest is
daily, the SNS alarms still page in real time. :func:`compact` and :func:`markdown` are pure.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from urllib.parse import urlencode

ITEMS_MAX = 15  # technical-constant: rows of the digest list (the page has them all)
PAGE = "/admin/overview"


def view_link(base: str, **params: str) -> str:
    q = urlencode({k: v for k, v in params.items() if v})
    return f"{base}{PAGE}" + (f"?{q}" if q else "")


def compact(status: dict, base: str) -> dict:
    """{status, open, set_aside, unknown, link, items}: the page's verdict, the open items first, each with a link to
    the page filtered on its app and severity. Pure."""
    v = status.get("verdict") or {}
    items = v.get("attention") or []
    urgent = sorted({i["severity"] for i in items if i.get("status") == "open" and i.get("severity") in ("sev1",
                                                                                                         "sev2")})
    link = view_link(base, range="24h", sev=",".join(urgent))
    rows = [{"id": i["id"], "severity": i.get("severity"), "app": i.get("app"), "title": i.get("title"),
             "status": i.get("status"), "since": i.get("since"), "ack": i.get("ack"),
             "link": view_link(base, range="24h", apps=i.get("app") if i.get("app") != "shared" else "",
                               sev=i.get("severity") or "")} for i in items[:ITEMS_MAX]]
    return {"status": v.get("status"), "open": v.get("open", 0), "set_aside": v.get("set_aside", 0),
            "unknown": v.get("unknown") or [], "link": link, "items": rows, "more": max(0, len(items) - ITEMS_MAX),
            "generated_at": status.get("generated_at")}


HEADLINE = {"ok": "All systems normal", "attention": "{n} open on the status page",
            "partial": "No problems found in the sources that answered", "unknown": "Status unknown"}


def markdown(part: Optional[dict]) -> list[str]:
    """The digest's "Ecosystem status" section (Markdown lines). Pure."""
    md = ["## Ecosystem status (the status page verdict, last 24 h)", ""]
    if not part:
        return md + ["- Not built for this digest.", ""]
    if part.get("error"):
        return md + [f"- Not available: {part['error']}.", ""]
    head = HEADLINE.get(part.get("status") or "", "Status unknown").format(n=part.get("open", 0))
    md.append(f"**{head}**" + (f"; {part['set_aside']} set aside (acknowledged, snoozed or muted)"
                               if part.get("set_aside") else "") + f". [Open the status page]({part['link']})")
    if part.get("unknown"):
        md.append(f"Not counted (no answer): {', '.join(part['unknown'])}.")
    md.append("")
    for i in part.get("items") or []:
        state = "" if i.get("status") == "open" else f" ({i['status']})"
        md.append(f"- {str(i.get('severity') or '').upper()} {i.get('title')}{state}: [view]({i['link']})")
    if part.get("more"):
        md.append(f"- and {part['more']} more on the page.")
    return md + [""]


async def build(pool: Any, redis: Any, now: Optional[datetime] = None) -> dict:
    """The digest part from the page's own build (range 24 h). Never raises: {"error": ...} instead."""
    from api.services import ops_status
    from shared.ops_alerts import admin_base_url

    try:
        status = await ops_status.build_status(pool, redis, range_key="24h", now=now)
    except Exception as e:  # noqa: BLE001 — the digest is always written
        return {"error": f"{type(e).__name__}"}
    return compact(status, admin_base_url())
