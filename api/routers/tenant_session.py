"""Content studio sign-in with the partner pool (AA-979 P2b, root ADR 0010a; card CRM-P2 R / E).

  POST /auth/tenant-session      Bearer = the person's partner-pool ACCESS token (partner-web client)
                                 body {"tenant_id": <uuid> | null}
                                 -> 200 {token, tenant_id, tenant_name, plan_tier, content_studio_role, membership_id,
                                         expires_at, expires_in}
  GET  /auth/tenant-memberships  same Bearer -> 200 {memberships: [{tenant_id, tenant_name, content_studio_role,
                                         membership_id}]}  (the chooser; no identity in the body)

Who may: ONLY a partner-pool access token of the ``partner-web`` client (``api/core/partner_identity.py``, its own
verifier, never part of the staff issuers). A WORKFORCE (``cis-admin``, ``breakglass``, ``cis-admin-preview``),
EXTERNAL (``cis-portal``) or M2M token -> 403 ``forbidden`` (card R: a staff token gets no portal session); no token,
a bad, expired or ID token -> 401 ``unauthorized``; key set or configuration unavailable -> 503 ``unavailable``.

What it answers from: the ACTIVE ``shared.tenant_members`` rows of the token's (iss, sub) on active agencies
(``services/tenant_membership/session.py``). None -> 403 ``no_membership``; ``tenant_id`` given but not one of them ->
403 ``tenant_not_member``; several and none chosen -> 409 ``choose_tenant`` (the portal shows its chooser from GET
``/auth/tenant-memberships``); exactly one, or the chosen one -> the tenant JWT of
``api.routers.auth._create_partner_session_jwt`` (``client=portal``, ``amr=partner``, ``mid``, ``csr``; exp =
min(now + 15 min, the access token's exp)). The Content studio role comes from CIS only: the CRM role of the person
plays no part (ADR 0010 §7, "no CRM event grants a CIS role").

Throttle (critic S2): per verified identity in Redis (:data:`EXCHANGE_LIMIT_PER_MIN` per minute, key = the identity's
12-hex fingerprint), never per IP: on Fargate every portal call leaves through one NAT address, so a per-IP bucket
would be one bucket for every tenant. Fail open when Redis is down (the token was verified; the API Gateway throttle
still caps the rate).

Logs: one ``tenant_session_issued`` / ``tenant_session_refused`` line with the outcome, the tenant id and the identity
fingerprint only (never the token, the sub or an e-mail). A successful exchange also writes the AA-559 ``tenant.login``
audit row (best effort, like ``/auth/tenant-login``) with ``login_method=partner``.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from api.core import partner_identity
from api.core.identity_verifier import AuthUnavailable, InvalidToken, Principal
from api.core.m2m_auth import bearer_token
from api.routers.auth import PORTAL_CLIENT, _create_partner_session_jwt
from api.schemas.public_api import PartnerTenantMemberships, PartnerTenantSession  # doc only (AA-899)
from services.acp_shared.audit_log import TenantAuditAction, write_audit_log
from services.tenant_membership import session as studio
from services.tenant_membership.writer import fingerprint

logger = structlog.get_logger()
router = APIRouter(prefix="/auth", tags=["auth"])

EXCHANGE_LIMIT_PER_MIN = 30
ACQUIRE_TIMEOUT_S = 5.0
NO_STORE = {"Cache-Control": "no-store"}
UNAUTHORIZED_HEADERS = {"WWW-Authenticate": 'Bearer realm="tenant-session"', **NO_STORE}


class TenantSessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: Optional[uuid.UUID] = None


def _problem(status: int, detail: str, message: str, headers: Optional[dict[str, str]] = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "message": message},
                        headers={**NO_STORE, **(headers or {})})


async def _verified(request: Request) -> tuple[Optional[Principal], Optional[JSONResponse]]:
    token = bearer_token(request)
    if token is None:
        return None, _problem(401, "unauthorized", "A partner sign-in token is required.", UNAUTHORIZED_HEADERS)
    try:
        principal = await partner_identity.verify_partner_access_token(token)
    except partner_identity.PartnerCallerRefused as exc:
        logger.info("tenant_session_refused", reason="foreign_token", detail=str(exc))
        return None, _problem(403, "forbidden", "This sign-in cannot open the Content studio.")
    except InvalidToken as exc:
        logger.info("tenant_session_refused", reason="invalid_token", detail=str(exc))
        return None, _problem(401, "unauthorized", "The sign-in token is not valid.", UNAUTHORIZED_HEADERS)
    except AuthUnavailable:
        logger.error("tenant_session_jwks_unavailable")
        return None, _problem(503, "unavailable", "Sign-in verification is unavailable; retry later.",
                              {"Retry-After": "5"})
    except ValueError as exc:
        logger.error("tenant_session_config_invalid", error=str(exc))
        return None, _problem(503, "unavailable", "Sign-in verification is not configured.")
    return principal, None


async def _throttled(request: Request, principal: Principal) -> bool:
    """True when the identity used more than EXCHANGE_LIMIT_PER_MIN exchanges this minute. Fail open."""
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return False
    key = f"tenant_session:{fingerprint(principal.iss, principal.sub)}:{int(time.time() // 60)}"
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 120)
    except Exception as exc:  # noqa: BLE001 - fail open on any Redis problem
        logger.warning("tenant_session_throttle_redis_error", error=type(exc).__name__)
        return False
    return isinstance(count, int) and count > EXCHANGE_LIMIT_PER_MIN


async def _memberships(request: Request, principal: Principal) -> tuple[Optional[list[Any]], Optional[JSONResponse]]:
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        return None, _problem(503, "unavailable", "Memberships are unavailable; retry later.", {"Retry-After": "5"})
    try:
        async with pool.acquire(timeout=ACQUIRE_TIMEOUT_S) as conn:
            rows = await studio.identity_memberships(conn, principal.iss, principal.sub)
    except (asyncpg.UndefinedTableError, asyncpg.PostgresConnectionError, asyncpg.InterfaceError,
            asyncpg.TooManyConnectionsError, OSError) as exc:
        logger.error("tenant_session_db_unavailable", error=type(exc).__name__)
        return None, _problem(503, "unavailable", "Memberships are unavailable; retry later.", {"Retry-After": "5"})
    return rows, None


def _access_token_expiry(principal_token: str) -> Optional[datetime]:
    exp = partner_identity._unverified_claims(principal_token).get("exp")  # verified just before; read exp only
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    return None


@router.post("/tenant-session", responses={200: {"model": PartnerTenantSession}},
             summary="AA-979 — partner-pool access token -> short Content studio tenant JWT")
async def tenant_session(request: Request) -> JSONResponse:
    principal, refused = await _verified(request)
    if refused is not None:
        return refused
    assert principal is not None  # noqa: S101 - set whenever refused is None
    fp = fingerprint(principal.iss, principal.sub)
    try:
        raw = await request.body()
        body = TenantSessionIn.model_validate_json(raw) if raw.strip() else TenantSessionIn()
    except (ValidationError, ValueError):
        return _problem(400, "invalid", "The body must be {\"tenant_id\": <uuid> | null}.")
    if await _throttled(request, principal):
        logger.info("tenant_session_refused", reason="throttled", identity=fp)
        return _problem(429, "throttled", "Too many sign-in attempts. Try again in a minute.", {"Retry-After": "60"})
    rows, unavailable = await _memberships(request, principal)
    if unavailable is not None:
        return unavailable
    assert rows is not None  # noqa: S101
    if not rows:
        logger.info("tenant_session_refused", reason="no_membership", identity=fp)
        return _problem(403, "no_membership", "This account has no Content studio access.")
    if body.tenant_id is not None:
        chosen = [r for r in rows if str(r["tenant_id"]) == str(body.tenant_id)]
        if not chosen:
            logger.info("tenant_session_refused", reason="tenant_not_member", identity=fp)
            return _problem(403, "tenant_not_member", "This account has no Content studio access to that agency.")
        row = chosen[0]
    elif len(rows) == 1:
        row = rows[0]
    else:
        logger.info("tenant_session_refused", reason="choose_tenant", identity=fp, memberships=len(rows))
        return _problem(409, "choose_tenant", "Choose the agency to open.")

    tenant_id = str(row["tenant_id"])
    token, exp = _create_partner_session_jwt(
        tenant_id, row["name"], row["plan_tier"], membership_id=str(row["membership_id"]),
        content_studio_role=row["content_studio_role"], not_after=_access_token_expiry(bearer_token(request) or ""),
    )
    try:
        await write_audit_log(
            request.app.state.pool, tenant_id=tenant_id, actor=f"tenant:{tenant_id}",
            action=TenantAuditAction.TENANT_LOGIN, resource_type="tenant", resource_id=tenant_id,
            details={"login_method": "partner", "client": PORTAL_CLIENT, "identity_fingerprint": fp},
        )
    except Exception as exc:  # noqa: BLE001 - the AA-559 rule: an audit hiccup never locks a tenant out
        logger.error("tenant_session_audit_log_failed", tenant_id=tenant_id, error=type(exc).__name__)
    logger.info("tenant_session_issued", tenant_id=tenant_id, identity=fp, role=row["content_studio_role"])
    expires_in = max(0, int((exp - datetime.now(timezone.utc)).total_seconds()))
    return JSONResponse(
        content={
            "token": token,
            "tenant_id": tenant_id,
            "tenant_name": row["name"],
            "plan_tier": row["plan_tier"],
            "content_studio_role": row["content_studio_role"],
            "membership_id": str(row["membership_id"]),
            "expires_at": exp.isoformat().replace("+00:00", "Z"),
            "expires_in": expires_in,
        },
        headers=NO_STORE,
    )


@router.get("/tenant-memberships", responses={200: {"model": PartnerTenantMemberships}},
            summary="AA-979 — the agencies a partner-pool person may open in the studio")
async def tenant_memberships(request: Request) -> JSONResponse:
    principal, refused = await _verified(request)
    if refused is not None:
        return refused
    assert principal is not None  # noqa: S101
    if await _throttled(request, principal):
        return _problem(429, "throttled", "Too many requests. Try again in a minute.", {"Retry-After": "60"})
    rows, unavailable = await _memberships(request, principal)
    if unavailable is not None:
        return unavailable
    assert rows is not None  # noqa: S101
    logger.info("tenant_memberships_listed", identity=fingerprint(principal.iss, principal.sub), count=len(rows))
    return JSONResponse(content={"memberships": [studio.membership_choice(r) for r in rows]}, headers=NO_STORE)
