"""AA-1174 (S288) — evidence source ``crm_ops``: consent capture and campaign sends to opted-in contacts only, read from
the partner CRM's read-only route ``GET /crm/ops/v1/privacy-evidence`` (crm.ops.v1 1.18; counts across agencies, no
tenant name, no contact) with the signed-in staff member's own Cognito token (api/services/crm_console.py relay).

    collect(authorization, window_days, now) -> (evidence rows, info), never raises

Checks (ids stable for AA-1173):

  campaign_consent_enforced   fact ``violations`` = marketing messages sent in the window to a contact whose latest
                              marketing consent at send time was not a yes, or who was suppressed (CIS grades it
                              against cis.compliance.campaign_consent_violations_max); detail adds the sends counted
                              (``sent`` includes the sends held send_uncertain, which the provider may have delivered;
                              ``uncertain`` counts them apart). No marketing send in the window -> a ``manual`` row:
                              0 violations out of 0 sends is no evidence either way, so the obligation never reads met
                              on it (a manual result is not met in phase 1).
  consent_capture_present     fact ``last_date`` = the newest consent decision the CRM recorded (CIS grades its age
                              against cis.compliance.consent_capture_max_age_days); none at all -> error.

Who may write evidence (S288 lane contract rule 3; the CRM's evidence must not be poisoned by a viewer): a 401 / 403
answer or a request without a Cognito session (``cognito_session_required``) writes NO row: ``info`` reports it and the
newest real row keeps counting. Any other failure (the route missing, a timeout, a bad body) is an ``error`` row: the
CRM evidence is really unavailable. Only known numeric / date keys of the body are kept.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional

from api.services import crm_console

SOURCE = "crm_ops"
COLLECTOR = "cis api (compliance_crm)"
PATH = "/privacy-evidence"
LINK = "https://github.com/AdventureAsia365-Ecosys/AA-Ecosys/blob/master/docs/legal/consent-capture-query.md"
CHECKS = ("campaign_consent_enforced", "consent_capture_present")
NO_EVIDENCE = (401, 403)
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


def _int(v: Any) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _day(v: Any) -> Optional[str]:
    if not isinstance(v, str):
        return None
    try:
        return date.fromisoformat(v[:10]).isoformat()
    except ValueError:
        return None


def _version() -> Optional[str]:
    from api.services.compliance_db import _version as v   # noqa: PLC0415 - same SHA rule

    return v()


def _row(check_id: str, status: str, summary: str, detail: dict, now: datetime) -> dict:
    return {"source": SOURCE, "check_id": check_id, "status": status, "collected_at": now, "collector": COLLECTOR,
            "collector_version": _version(), "run_ref": None, "summary": summary[:300], "detail": detail, "link": LINK}


def evaluate(body: dict, now: datetime) -> list[dict]:
    """The two evidence rows from a 200 body of the CRM route (pure)."""
    window = _int(body.get("window_days"))
    sent, violations = _int(body.get("marketing_sent")), _int(body.get("violations"))
    uncertain = _int(body.get("marketing_uncertain"))
    tenants, excluded = _int(body.get("tenants")), _int(body.get("excluded_erased"))
    recorded, last = _int(body.get("consents_recorded")), _day(body.get("last_consent_at"))
    if sent is None or violations is None:
        campaign = _row("campaign_consent_enforced", "error", "the CRM answer has no send counts", {}, now)
    else:
        detail = {"violations": violations, "sent": sent, "uncertain": uncertain, "window_days": window,
                  "tenants": tenants, "excluded_erased": excluded}
        if sent == 0:
            campaign = _row("campaign_consent_enforced", "manual",
                            f"no marketing send in {window} days: nothing to check yet, not evidence of consent",
                            detail, now)
        else:
            held = f" ({uncertain} possibly delivered after a stopped run)" if uncertain else ""
            campaign = _row("campaign_consent_enforced", "pass",
                            f"{violations} of {sent} marketing sends{held} in {window} days went to a contact "
                            f"without a yes", detail, now)
    if last is None:
        capture = _row("consent_capture_present", "error", "the CRM has recorded no consent decision yet",
                       {"recorded_in_window": recorded, "window_days": window}, now)
    else:
        capture = _row("consent_capture_present", "pass", f"newest consent decision {last}; {recorded} in "
                       f"{window} days", {"last_date": last, "recorded_in_window": recorded, "window_days": window},
                       now)
    return [campaign, capture]


async def collect(authorization: Optional[str], window_days: int, now: datetime) -> tuple[list[dict], dict]:
    status, body = await crm_console.crm_ops_call("GET", PATH, authorization, params={"days": int(window_days)})
    detail = str(body.get("detail") or status) if isinstance(body, dict) else str(status)
    if status in NO_EVIDENCE:
        return [], {"reason": detail[:80], "http": status}
    if status != 200 or not isinstance(body, dict):
        summary = f"the partner CRM evidence route answered {status} ({detail[:60]})"
        return [_row(cid, "error", summary, {"http": status}, now) for cid in CHECKS], {"http": status}
    return evaluate(body, now), {"http": 200}
