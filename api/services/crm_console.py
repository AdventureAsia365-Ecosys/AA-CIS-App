"""AA-941 (card CRM-13a, S-01) — the relay behind Platform › Tenants › Agencies (CRM): the AA admin L1 console of the
partner CRM.

    crm_ops_call(method, path, authorization, json=None, params=None) -> (status_code, body: dict)

Every call goes to the CRM's ``crm.ops.v1`` (``APPS["crm"].extra["ops_base"]``) with the signed-in staff member's OWN
WORKFORCE access token (``platform_apps._cognito_bearer``; StaffBearerMiddleware keeps it in the scope state,
``platform_apps.relay_authorization``). No new secret, no M2M client: the CRM's route class OPS checks the issuer, the
client and the group itself (cis-admin or crm-platform-admin may read, only crm-platform-admin may write).

Rules
- ``ALLOWED`` is the whole reach of this relay: one (method, regex) per L1 route of the CRM (ids are UUIDs). Anything
  else is refused before any network call (404 ``crm_path_not_allowed``), so this relay cannot be used to reach other
  CRM paths.
- The CRM's status code and JSON object body are passed through unchanged (``{detail, message, ...}`` on errors, so
  codes such as ``has_travellers`` reach the page). CIS does not re-filter keys: the CRM's ops serializer allow-list
  and the root contract CI (crm.ops.v1) are the enforcement points for "no trip, pipeline or revenue keys".
- A legacy password session (no Cognito token) -> 401 ``cognito_session_required``; a network error or timeout -> 503
  ``crm_unavailable``; a body that is not a JSON object -> 502 ``crm_bad_response``.
- Request and response bodies are never logged (the owner invite carries an e-mail address): one
  ``crm_console_call`` line with method, route pattern, status and duration only. Sentry drops request bodies
  (shared/sentry_init.py: max_request_body_size "never", before_send pops request.data); frame locals, which hold the
  token and the parsed body on an unhandled error, are dropped by fix/sentry-frame-locals (S249, merged with this);
  the staff audit middleware stores no body (api/middleware/staff_audit.py).
- Timeouts: 2 s for reads, 5 s for the agency list and the data coverage counts (one CRM transaction per agency),
  10 s for writes (the owner invite sends its SES mail inside the CRM transaction).
"""
from __future__ import annotations

import json as _json
import re
import time
from typing import Any, Optional

import httpx
import structlog

from api.core.app_registry import APPS
from api.services.platform_apps import _cognito_bearer

logger = structlog.get_logger()

READ_TIMEOUT_S = 2.0
LIST_TIMEOUT_S = 5.0
WRITE_TIMEOUT_S = 10.0

_ID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_T = rf"/tenants/{_ID}"

# (method, full-match pattern) — the L1 routes of crm.ops.v1 this console uses (AA-941 spec "partner_api").
ALLOWED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (m, re.compile(p)) for m, p in (
        ("GET", r"/console"),
        ("GET", r"/data-coverage"),   # AA-992: crm.ops.v1 1.4 counts for /admin/data-coverage
        ("GET", r"/tenants"),
        ("POST", r"/tenants"),
        ("GET", _T),
        ("GET", rf"{_T}/(usage|entitlements|data-requests|members|jobs)"),
        ("POST", rf"{_T}/(lock|unlock|owner-invite|data-requests|owner-recovery)"),
        ("POST", rf"{_T}/owner-recovery/{_ID}/complete"),
        ("POST", rf"{_T}/members/{_ID}/deactivate"),
        ("PUT", rf"{_T}/(entitlements-allowed|insights-opt-out)"),
        ("POST", rf"{_T}/jobs/{_ID}/retry"),
        # AA-979 P2b — Content studio invitation delivery (Tenant 360 Members invite / send again), relayed by
        # api/routers/admin_tenant360.py: the CRM sends the AA system mail and owns the claim (crm.studio_invitation).
        ("POST", rf"{_T}/studio-invitations"),
    )
)

# Tests swap this for an httpx.MockTransport (no network).
_transport: Optional[httpx.AsyncBaseTransport] = None


def is_allowed(method: str, path: str) -> bool:
    return any(m == method and p.fullmatch(path) for m, p in ALLOWED)


def _pattern(method: str, path: str) -> str:
    """The path with its UUIDs replaced, for the log line (ids are not personal data, but one shape per route reads
    better in a log search)."""
    return re.sub(_ID, "{id}", path)


def _timeout(method: str, path: str) -> float:
    if method != "GET":
        return WRITE_TIMEOUT_S
    # /tenants and /data-coverage open one CRM transaction per agency.
    return LIST_TIMEOUT_S if path in ("/tenants", "/data-coverage") else READ_TIMEOUT_S


def _problem(detail: str, message: str) -> dict[str, Any]:
    return {"detail": detail, "message": message}


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"non-JSON constant {name}")


async def crm_ops_call(method: str, path: str, authorization: Optional[str], *, json: Any = None,
                       params: Optional[dict[str, Any]] = None) -> tuple[int, dict[str, Any]]:
    """Relay one L1 call. Never raises; returns the status code and JSON object body to send back."""
    method = method.upper()
    if not is_allowed(method, path):
        logger.warning("crm_console_path_refused", method=method, path=_pattern(method, path))
        return 404, _problem("crm_path_not_allowed", "This CRM path is not part of the L1 console.")
    token = _cognito_bearer(authorization)
    if token is None:
        return 401, _problem("cognito_session_required",
                             "Sign in with your staff account (Cognito) to use the CRM console.")
    url = f"{APPS['crm'].extra['ops_base']}{path}"
    clean_params = {k: v for k, v in (params or {}).items() if v is not None} or None
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_timeout(method, path), transport=_transport) as client:
            r = await client.request(method, url, params=clean_params, json=json,
                                     headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    except Exception as e:  # timeout, DNS, TLS, connection refused
        logger.warning("crm_console_call_failed", method=method, path=_pattern(method, path),
                       error=type(e).__name__)
        return 503, _problem("crm_unavailable", "The partner CRM did not answer. Try again in a minute.")
    ms = round((time.monotonic() - started) * 1000)
    try:
        # NaN / Infinity are not JSON objects a response can carry back (JSONResponse refuses them): treat them like
        # any malformed answer instead of a 500 (AA-941 security review).
        body = _json.loads(r.content, parse_constant=_refuse_constant)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        logger.warning("crm_console_bad_response", method=method, path=_pattern(method, path),
                       status=r.status_code, ms=ms)
        return 502, _problem("crm_bad_response", f"The partner CRM answered {r.status_code} without a JSON object.")
    logger.info("crm_console_call", method=method, path=_pattern(method, path), status=r.status_code, ms=ms)
    return r.status_code, body
