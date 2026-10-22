# api/routers/auth.py
# S7 Step 2 — Real API key authentication for tenant portal
#
# Flow:
#   POST /auth/tenant-login  { api_key }
#   → SHA256 hash key
#   → lookup shared.tenants WHERE api_key_hash = hash AND is_active = true
#   → if found: return signed JWT { tenant_id, name, plan_tier, exp }
#   → frontend stores JWT in cookie → middleware verifies JWT on each request
#
# JWT secret: JWT_SECRET env var (required, no fallback — AA-506)
# JWT expiry: 24h

import hashlib
import os
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import asyncpg
import bcrypt
import jwt  # PyJWT
from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from pydantic.json_schema import SkipJsonSchema
from api.core.secret_compare import secrets_equal

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
        "aa-cis/dev/jwt-secret (AA-506); no hardcoded fallback is permitted."
    )
JWT_ALG      = "HS256"
JWT_EXPIRY_H = 24

# AA-760 — the tenant portal's own sign-in (portal/app/api/auth/tenant-login) asks for a token with
# client="portal". The claim marks a portal SESSION: api/middleware/rate_limit.py still rate-limits it but
# does not meter it as an API call (shared.tenant_api_usage = the "API calls" on the tenant's Billing page).
# A token minted without it (any machine caller of POST /auth/tenant-login, and every token minted before
# AA-760) is metered, as before. Same API key either way: the claim is signed, so it cannot be added to an
# existing token, but a caller holding the key could ask for it. API calls carry no charge (overage is per
# rewrite, v_tenant_monthly_usage) and the RPM limit applies to both, so that only lowers a displayed count.
PORTAL_CLIENT = "portal"


# ── Models ────────────────────────────────────────────────────────────────────

class TenantLoginRequest(BaseModel):
    api_key: str
    # AA-760 — set only by the portal's sign-in BFF (see PORTAL_CLIENT); left out of the public schema.
    client: SkipJsonSchema[Optional[Literal["portal"]]] = None


class TenantLoginResponse(BaseModel):
    token: str
    tenant_id: str
    tenant_name: str
    plan_tier: str


# ── Helper ────────────────────────────────────────────────────────────────────

def _hash_api_key(raw_key: str) -> str:
    """SHA256 hex digest — matches migration 007 encode(sha256(...),'hex')."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _create_jwt(tenant_id: str, name: str, plan_tier: str, client: Optional[str] = None) -> str:
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

@router.post("/tenant-login", response_model=TenantLoginResponse, deprecated=True)
async def tenant_login(
    body: TenantLoginRequest,
    pool=Depends(lambda: None),  # replaced by app-level pool injection below
):
    """
    DEPRECATED (AA-181): ACP tenant routes now authenticate with a single
    X-API-Key header (see verify_tenant_api_key) instead of a JWT obtained here.
    Kept for now for any remaining non-ACP callers; do not add new dependents.

    Verify tenant API key against shared.tenants.api_key_hash.
    Returns signed JWT on success.
    """
    if not body.api_key or len(body.api_key) < 10:
        raise HTTPException(status_code=400, detail="Invalid API key format")

    key_hash = _hash_api_key(body.api_key)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT tenant_id::text, name, plan_tier
            FROM shared.tenants
            WHERE api_key_hash = $1
              AND is_active = true
            """,
            key_hash,
        )

    if not row:
        # Consistent timing to prevent enumeration
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    token = _create_jwt(
        tenant_id=row["tenant_id"],
        name=row["name"],
        plan_tier=row["plan_tier"],
    )

    return TenantLoginResponse(
        token=token,
        tenant_id=row["tenant_id"],
        tenant_name=row["name"],
        plan_tier=row["plan_tier"],
    )


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


# ── verify_tenant_api_key — AA-181 single-header ACP tenant auth ──────────────
#
# Replaces the per-router HTTPBearer(JWT) + X-Admin-Secret combo on all
# /acp/* tenant routes. Accepts EXACTLY ONE of:
#   - X-Admin-Secret: <ADMIN_SECRET>  -> AA internal staff (reviewer_type=aa_internal)
#   - X-API-Key: <raw cis_... key>    -> tenant partner (reviewer_type=tenant_self),
#     hashed with sha256 and matched against shared.tenants.api_key_hash
#
# AA_INTERNAL_ADMIN_SUB matches the long-standing admin sentinel used by the
# old _get_tenant()/_get_admin() dependencies (see AA-22 tech debt note).
AA_INTERNAL_ADMIN_SUB = "00000000-0000-0000-0000-000000000001"

# auto_error=False: these only register the headers in the OpenAPI schema
# (Swagger "Authorize" + per-route docs). The actual lookup/validation reads
# request.headers directly below, so a request can omit one and still pass
# via the other.
_api_key_header_scheme = APIKeyHeader(
    name="X-API-Key",
    scheme_name="TenantApiKey",
    auto_error=False,
    description="Raw tenant API key (cis_...), hashed and matched against shared.tenants.api_key_hash.",
)
_admin_secret_header_scheme = APIKeyHeader(
    name="X-Admin-Secret",
    scheme_name="AdminSecret",
    auto_error=False,
    description="AA internal admin secret (ADMIN_SECRET env var) — for AA staff/internal callers.",
)


async def verify_tenant_api_key(
    request: Request,
    _api_key: str = Security(_api_key_header_scheme),
    _admin_secret: str = Security(_admin_secret_header_scheme),
) -> dict:
    """
    Single-header ACP tenant auth dependency (AA-181).

    Send EXACTLY ONE of:
      - X-API-Key: <raw cis_... key>   (tenant partner)
      - X-Admin-Secret: <ADMIN_SECRET> (AA internal staff)

    Returns a dict with: sub, tenant_id, role, actor, reviewer_type, name, plan_tier.
    Raises 401 if neither X-Admin-Secret nor a valid X-API-Key is present.
    """
    admin_secret = os.environ.get("ADMIN_SECRET", "")
    x_admin = request.headers.get("X-Admin-Secret", "")
    if admin_secret and secrets_equal(x_admin, admin_secret):
        return {
            "sub": AA_INTERNAL_ADMIN_SUB,
            "tenant_id": AA_INTERNAL_ADMIN_SUB,
            "role": "admin",
            "actor": "aa_internal_admin",
            "reviewer_type": "aa_internal",
            "name": "AA Internal",
            "plan_tier": "internal",
        }

    raw_key = request.headers.get("X-API-Key", "")
    if raw_key:
        pool: asyncpg.Pool = request.app.state.pool
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT tenant_id::text, name, plan_tier
                FROM shared.tenants
                WHERE api_key_hash = $1
                  AND is_active = true
                """,
                _hash_api_key(raw_key),
            )
        if row:
            return {
                "sub": row["tenant_id"],
                "tenant_id": row["tenant_id"],
                "role": "tenant",
                "actor": row["name"] or row["tenant_id"],
                "reviewer_type": "tenant_self",
                "name": row["name"],
                "plan_tier": row["plan_tier"],
            }

    raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


# ── AA-232 — Per-user admin JWT auth (helpers + models) ──────────────────────
#
# The live endpoints (POST /auth/admin-login, POST /auth/verify-admin) are
# declared in api/main.py, mirroring tenant_login/verify_tenant there — the
# /auth `router` object in this module is NOT mounted (see main.py: it imports
# these helpers/models and re-declares the routes on `app` with Depends(get_pool)).
# This module owns the reusable pieces only: bcrypt password verify, admin JWT
# mint, and the request/response models. Admin JWTs share JWT_SECRET/HS256/
# verify_jwt() with tenant tokens but carry sub = admin_users.id (UUID) and
# role = 'admin'|'reviewer'|'content'|'viewer' (vs tenant's role = 'tenant').

# AA-656: every staff role in shared.admin_role (migration 218 added 'content', 249 the read-only 'viewer',
# AA-878). Mirrors api/core/staff_authz.py ROLE_PERMS and the frontend matrix in frontend/lib/rbac.ts.
STAFF_ROLES = ("admin", "reviewer", "content", "viewer")


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


class BookingTokenResponse(BaseModel):
    """AA-680 — POST /auth/booking-token: an RS256 JWT (aud=booking) for AA-Booking."""
    access_token: str
    token_type: str
    expires_in: int


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
