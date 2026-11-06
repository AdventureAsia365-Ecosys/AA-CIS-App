# api/routers/auth.py
# Tenant JWT helpers (the partner-pool studio session, AA-979 P2b) and the staff password-session helpers.
#
# AA-1036 R3 slice 3 (root ADR 0019 d6): tenant API keys are gone. POST /auth/tenant-login answers 410 Gone
# (tenant_login_gone below); people sign in to the Content studio with their partner account
# (POST /auth/tenant-session, api/routers/tenant_session.py). No code path issues or verifies a tenant key.
#
# JWT secret: JWT_SECRET env var (required, no fallback — AA-506)
# JWT expiry: 24h

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt  # PyJWT
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/auth", tags=["auth"])

# AA-506: no hardcoded fallback. The old default ("cis-dev-jwt-secret-change-in-prod")
# was committed to this repo's own history, so anyone who ever cloned it could forge a
# valid tenant OR admin JWT against any deployment that forgot to set the real env var.
# Fail fast at import time instead — a missing secret must stop the app from starting,
# not silently sign tokens with a publicly-known string.
JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET env var is not set. Refusing to start — see Secrets Manager "
        "aa-cis/dev/jwt-secret; no hardcoded fallback is permitted."
    )
JWT_ALG      = "HS256"
JWT_EXPIRY_H = 24

# AA-760 — the claim client="portal" marks a studio SESSION: api/middleware/rate_limit.py still rate-limits it but
# does not meter it as an API call (shared.tenant_api_usage = the "API calls" on the tenant's Billing page). Every
# partner-pool session carries it (_create_partner_session_jwt). AA-1036: the API-key tokens that went without it
# are no longer minted (POST /auth/tenant-login is 410).
PORTAL_CLIENT = "portal"


# ── POST /auth/tenant-login: 410 Gone (AA-1036 R3 slice 3) ──────────────────────

TENANT_LOGIN_GONE = {
    "detail": "tenant_api_key_removed",
    "message": ("Tenant API keys were removed. Sign in to the Content studio with your partner account "
                "(the agency web sign-in)."),
    "sign_in": "partner",
}


def tenant_login_gone() -> JSONResponse:
    """The answer of ``POST /auth/tenant-login`` for any body (root ADR 0019 d6): 410 Gone, a short JSON body that
    names the partner sign-in. The body is never read, so a missing, malformed or valid-looking key answers the same."""
    return JSONResponse(status_code=status.HTTP_410_GONE, content=dict(TENANT_LOGIN_GONE))


# ── Tenant JWT helpers ────────────────────────────────────────────────────────

def _create_jwt(tenant_id: str, name: str, plan_tier: str, client: Optional[str] = None) -> str:
    """A tenant JWT without ``amr`` / ``mid`` / ``csr`` (the shape the retired API-key sign-in minted). AA-1036: no
    route mints it any more; the unit tests use it to check how the token gates treat such a token (owner
    behaviour, metered unless ``client=portal``). ``tests/unit/test_aa1036_no_tenant_api_keys.py`` keeps it out of
    production code."""
    payload = {
        "sub":       tenant_id,
        "name":      name,
        "plan_tier": plan_tier,
        "role":      "tenant",
        "iat":       datetime.now(timezone.utc),
        "exp":       datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_H),
    }
    # AA-760 — only the one known value is ever written; anything else mints a plain (metered) token.
    if client == PORTAL_CLIENT:
        payload["client"] = PORTAL_CLIENT
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


PARTNER_SESSION_MAX_S = 15 * 60
PARTNER_AMR = "partner"
STUDIO_ROLES = ("owner", "editor", "viewer")


def _create_partner_session_jwt(tenant_id: str, name: str, plan_tier: str, *, membership_id: str,
                                content_studio_role: str, not_after: Optional[datetime] = None) -> tuple[str, datetime]:
    """AA-979 P2b — the tenant JWT of a partner-pool sign-in (``POST /auth/tenant-session``).

    Same shape and secret as the legacy tenant token (``_create_jwt``: ``role=tenant``, ``sub`` = tenant id), so
    every ``/v1/*`` route and ``/auth/verify-tenant`` accept it unchanged, plus: ``client=portal`` (an unmetered
    portal session, AA-760), ``amr=partner`` (how the session was opened), ``mid`` (the membership) and ``csr`` (the
    Content studio role the tenant role middleware enforces). Lives at most :data:`PARTNER_SESSION_MAX_S` and never
    past ``not_after`` (the partner access token's own expiry), so a removed member or a disabled account loses the
    studio within 15 min."""
    if content_studio_role not in STUDIO_ROLES:
        raise ValueError("unknown Content studio role")
    now = datetime.now(timezone.utc)
    exp = now + timedelta(seconds=PARTNER_SESSION_MAX_S)
    if not_after is not None and not_after < exp:
        exp = not_after
    payload = {
        "sub":       tenant_id,
        "name":      name,
        "plan_tier": plan_tier,
        "role":      "tenant",
        "client":    PORTAL_CLIENT,
        "amr":       PARTNER_AMR,
        "mid":       membership_id,
        "csr":       content_studio_role,
        "iat":       now,
        "exp":       exp,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG), exp


def studio_role_of(payload: dict) -> Optional[str]:
    """AA-979 — the Content studio role a verified tenant JWT carries (``csr``), or None for a token without it (the
    legacy shape, owner behaviour). An unknown value is treated as the most restrictive role (viewer)."""
    if "csr" not in payload:
        return None
    role = payload.get("csr")
    return role if role in STUDIO_ROLES else "viewer"


def is_portal_session(payload: dict) -> bool:
    """AA-760 — True for a verified tenant JWT minted by the portal's sign-in (claim client="portal")."""
    return payload.get("client") == PORTAL_CLIENT


def verify_jwt(token: str) -> dict:
    """Verify JWT and return payload. Raises HTTPException on failure."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def verify_tenant_jwt(token: str) -> dict:
    """A TENANT JWT only. Staff JWTs share JWT_SECRET (AA-656) and carry sub = admin_users.id, so a
    plain verify_jwt() let a staff token act as a "tenant" whose id is a staff user's — reads
    came back empty and writes (brand rules, rewrites) landed on a tenant that does not exist
    (AA-741 pre-audit). Tenant tokens always carry role='tenant' (_create_jwt)."""
    payload = verify_jwt(token)
    if payload.get("role") != "tenant":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Tenant token required")
    return payload


# ── Endpoint ──────────────────────────────────────────────────────────────────
# This /auth router is not mounted (api/main.py declares the live routes on the app); its tenant-login answers the
# same 410 so the two handlers can never disagree again (AA-559 found the live one in main.py the hard way).

@router.post("/tenant-login", deprecated=True, status_code=status.HTTP_410_GONE)
async def tenant_login():
    """Removed (AA-1036 R3 slice 3, root ADR 0019 d6): 410 Gone for any body; sign in with the partner pool."""
    return tenant_login_gone()


# ── /auth/verify — middleware helper ──────────────────────────────────────────

class VerifyResponse(BaseModel):
    tenant_id: str
    name: str
    plan_tier: str
    valid: bool


@router.post("/verify-tenant", response_model=VerifyResponse)
async def verify_tenant_token(token: str):
    """
    Verify a tenant JWT. Used by Next.js middleware (server-side).
    Returns 401 if invalid/expired.
    """
    payload = verify_jwt(token)
    return VerifyResponse(
        tenant_id=payload["sub"],
        name=payload["name"],
        plan_tier=payload["plan_tier"],
        valid=True,
    )


# ── AA-232 — Per-user admin JWT auth (helpers + models) ──────────────────────
#
# The live endpoints (POST /auth/admin-login, POST /auth/verify-admin) are
# declared in api/main.py, mirroring verify_tenant there — the
# /auth `router` object in this module is NOT mounted (see main.py: it imports
# these helpers/models and re-declares the routes on `app` with Depends(get_pool)).
# This module owns the reusable pieces only: bcrypt password verify, admin JWT
# mint, and the request/response models. Admin JWTs share JWT_SECRET/HS256/
# verify_jwt() with tenant tokens but carry sub = admin_users.id (UUID) and
# role = a staff role of STAFF_ROLES below (vs tenant's role = 'tenant').

# AA-656: every staff role in shared.admin_role (migration 218 added 'content', 249 the read-only 'viewer',
# AA-878). Mirrors api/core/staff_authz.py ROLE_PERMS and the frontend matrix in frontend/lib/rbac.ts.
STAFF_ROLES = ("admin", "content", "viewer",
               # AA-1035 R2 (CIS 362, root ADR 0019 d3)
               "content_lead", "tenant_qa", "customer_success", "support", "finance", "ai_ops")


class AdminLoginRequest(BaseModel):
    username: str
    password: str


class AdminLoginResponse(BaseModel):
    token: str
    admin_id: str
    username: str
    role: str


class VerifyAdminResponse(BaseModel):
    admin_id: str
    username: str
    role: str
    valid: bool
    # AA-816 C3: directory fields (migration 247; NULL until the row is linked) and which session kind was
    # verified: "cognito" (WORKFORCE access token) or "legacy" (HS256 password session).
    email: Optional[str] = None
    display_name: Optional[str] = None
    auth: str = "legacy"


# A real bcrypt hash (cost 12, like the legacy staff password hashes) of a random value that was thrown
# away: comparing against it costs the same time as a real compare and never matches. Used when there is
# no hash to compare with — unknown username, or a Cognito-only staff row whose password_hash is NULL
# (migration 247) — so those cases look like a wrong password, in shape and in timing.
_DUMMY_BCRYPT_HASH = b"$2b$12$SZNlHnnffvkqKqziIuXG6OfiTEgZ6j1PT8AM3vK5IzwcL3vcb4HSC"


def _verify_password(raw_password: str, password_hash: Optional[str]) -> bool:
    """bcrypt compare. Returns False (not raise) on any malformed-hash error —
    a corrupt/legacy hash should fail closed, not 500.

    AA-816: a NULL hash (Cognito-only staff member, 247) fails closed too: the
    compare runs against _DUMMY_BCRYPT_HASH and the result is always False."""
    if not isinstance(password_hash, str) or not password_hash:
        try:
            bcrypt.checkpw((raw_password or "").encode(), _DUMMY_BCRYPT_HASH)
        except (ValueError, TypeError):
            pass
        return False
    try:
        return bcrypt.checkpw(raw_password.encode(), password_hash.encode())
    except (ValueError, TypeError, AttributeError):
        return False


def _create_admin_jwt(admin_id: str, username: str, role: str) -> str:
    payload = {
        "sub":      admin_id,
        "username": username,
        "role":     role,
        "iat":      datetime.now(timezone.utc),
        "exp":      datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_H),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)
