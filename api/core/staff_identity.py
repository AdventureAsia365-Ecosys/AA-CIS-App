"""Cognito staff identity for the CIS API (AA-816 / AA-854 step C3, root ADR 0006 §3, §8, §9).

Turns a verified WORKFORCE access token into the CIS staff member behind it. Used by two callers:

- ``POST /auth/verify-admin`` (``api/main.py``) when the token carries an ``iss`` (legacy HS256 staff tokens
  have none), with ``touch_login=True`` so ``last_login_at`` is kept;
- ``api/middleware/staff_bearer.py`` for a Bearer token on the staff paths (``STAFF_PATH_PREFIXES``).

Rules
-----
1. **Path by the unverified ``iss``** (:func:`token_kind`): a JWT whose ``iss`` is the configured WORKFORCE
   issuer is a Cognito staff token and goes through the C2 verifier (``api/core/identity_config.py``:
   WORKFORCE x {cis-admin, breakglass}); a JWT without ``iss`` is a legacy HS256 token (staff or tenant) and
   is left to the existing checks; a JWT with any OTHER ``iss`` (a future EXTERNAL ``cis-portal`` token, the
   CIS-minted Booking RS256 token) is "other": the bearer middleware lets it through to the route's own
   guard, and ``/auth/verify-admin`` refuses it (401). When the verifier can not be built (bad env override),
   every ``iss``-bearing token is treated as staff so the caller answers 503 (fail closed).
2. **A ``cis-*`` group is required.** The pre token trigger is not client-aware, so a user with only
   ``booking-*`` groups still gets a cis-admin token: no CIS group -> 403.
3. **Role = the highest ``cis-*`` group** (:data:`GROUP_ROLES`, same order as the WORKFORCE group precedence
   10/20/30). The DB role is not used for authorisation (ADR 0006 phase 1: "staff routes authorise from
   ``cognito:groups``"); a difference is logged as ``staff_role_drift`` (the C7 reconcile job reports it).
4. **Row by ``(cognito_iss, cognito_sub)``** (ADR 0006 §9). When no row is bound yet, the first sign-in binds
   the token identity to the ACTIVE row whose ``lower(email)`` equals the token's ``email`` and whose identity
   is still empty, once (``UPDATE … WHERE cognito_sub IS NULL``; a row bound to another identity is never
   re-bound). The email claim is trusted here because the WORKFORCE pre token trigger only issues tokens to
   ``@adventure.asia`` addresses with ``email_verified`` and app clients cannot write ``email``. The trigger
   adds ``email`` but not ``email_verified`` to the access token (ADR 0006 §6 asks APIs to check it), so as
   defence in depth the link is refused when the token says ``email_verified`` is false; absent = trusted
   on the trigger's check.
5. Unknown identity, an inactive row, or a row bound to another identity -> 401.
6. Rows are cached for :data:`STAFF_STATE_TTL_S` keyed by ``(iss, sub)`` (misses too), like the legacy
   verify-admin cache, so a deactivation takes effect within that TTL (plus the BFF's own verify cache).

Errors are :class:`StaffAuthError` with the HTTP status to answer: 401 bad token / unknown or inactive user,
403 no CIS group, 503 verifier, JWKS or directory unavailable (never a 500).

``last_login_at`` is bookkeeping: a failed write is logged (``staff_identity_touch_failed``) and never turns a
successful sign-in into an error.

``LEGACY_ADMIN_LOGIN_ENABLED`` (default true, :func:`legacy_login_enabled`) gates only the HS256 path of
``/auth/verify-admin`` (decision D3); ``/auth/admin-login`` is still served (AA-816 S2 gates or removes it; the
Booking bridge that used it and ``/auth/booking-token`` are gone, AA-911).
Only an explicit ``0``/``false``/``no``/``off`` switches it off, so a typo or empty value keeps the rollback
path open.
"""
from __future__ import annotations

import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import asyncpg
import jwt
import structlog

from api.core import identity_config
from api.core.identity_verifier import MAX_TOKEN_CHARS, AuthUnavailable, InvalidToken, Principal

logger = structlog.get_logger()

AUTH_COGNITO = "cognito"
AUTH_LEGACY = "legacy"
AUTH_OTHER = "other"          # a JWT with an iss that is not the WORKFORCE issuer (token_kind only)
AUTH_MALFORMED = "malformed"

_LEGACY_OFF_VALUES = frozenset({"0", "false", "no", "off"})

# WORKFORCE group -> shared.admin_role, highest first (identity.tf group precedence cis-admin 10,
# cis-reviewer 20, cis-content 30; ADR 0006 §3).
GROUP_ROLES: tuple[tuple[str, str], ...] = (
    ("cis-admin", "admin"),
    ("cis-reviewer", "reviewer"),
    ("cis-content", "content"),
)

STAFF_STATE_TTL_S = 60.0
STAFF_STATE_MAX = 2048

# The staff member of the current request when a Cognito Bearer was verified by the staff bearer middleware
# (None otherwise: legacy HS256 Bearer, secret-only, or not a staff path). Read by later steps (C9).
current_staff: ContextVar[Optional["StaffIdentity"]] = ContextVar("aa816_current_staff", default=None)


class StaffAuthError(Exception):
    """A refused staff token. ``status`` is the HTTP status, ``detail`` the response text, ``reason`` the log
    reason (never shown to the caller)."""

    def __init__(self, status: int, detail: str, reason: str) -> None:
        super().__init__(status, detail, reason)
        self.status = status
        self.detail = detail
        self.reason = reason


@dataclass(frozen=True)
class StaffRow:
    """The directory row bound to a Cognito identity (only the columns the API needs)."""

    admin_id: str
    username: str
    db_role: str
    email: Optional[str]
    display_name: Optional[str]
    is_owner: bool = False


@dataclass(frozen=True)
class StaffIdentity:
    """A verified Cognito staff member: who (the directory row) and the role the token's groups grant."""

    admin_id: str
    username: str
    role: str
    db_role: str
    email: Optional[str]
    display_name: Optional[str]
    iss: str
    sub: str
    client_id: str
    auth: str = AUTH_COGNITO
    # shared.admin_users.is_owner (C6: staff.write is owner-only). Cached with the row for STAFF_STATE_TTL_S; the
    # staff directory writer re-reads it from the DB inside its own transaction before every write.
    is_owner: bool = False


def legacy_login_enabled() -> bool:
    """D3: the legacy CIS password session (HS256 path of /auth/verify-admin; the UI login reads the same
    name in the BFF and must parse it the same way). Default true; only an explicit 0/false/no/off turns it
    off, so unset, empty or a typo keeps the rollback path. Read per call so a task-definition change needs
    no code change."""
    value = os.environ.get("LEGACY_ADMIN_LOGIN_ENABLED", "").strip().lower()
    return value not in _LEGACY_OFF_VALUES


def token_kind(token: Any) -> str:
    """Which verifier runs; nothing is verified here.

    - ``"cognito"``: a JWT whose ``iss`` is one of the CIS staff verifier's issuers (WORKFORCE), or any
      ``iss``-bearing JWT when that verifier can not be built (so the caller answers 503, fail closed);
    - ``"legacy"``: a JWT without ``iss`` (the HS256 staff and tenant tokens, ``api/routers/auth.py``);
    - ``"other"``: a JWT with another ``iss`` (not a CIS staff token; left to the route's own guard);
    - ``"malformed"``: anything else."""
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
        return AUTH_MALFORMED
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return AUTH_MALFORMED
    if not isinstance(claims, dict):
        return AUTH_MALFORMED
    if "iss" not in claims:
        return AUTH_LEGACY
    try:
        staff_issuers = {cfg.issuer for cfg in identity_config.get_cis_staff_verifier().issuers}
    except Exception:  # noqa: BLE001 - bad env override: verify_staff_token answers 503
        return AUTH_COGNITO
    return AUTH_COGNITO if claims["iss"] in staff_issuers else AUTH_OTHER


def role_from_groups(groups: Iterable[str]) -> Optional[str]:
    """The highest CIS role among the token's groups, or None when it holds no ``cis-*`` staff group."""
    held = set(groups or ())
    for group, role in GROUP_ROLES:
        if group in held:
            return role
    return None


async def verify_staff_token(token: str) -> Principal:
    """The C2 verifier for the CIS staff surface: 401 on an unacceptable token, 503 when the verifier can not
    be built (bad env override) or no JWKS was ever loaded for the issuer."""
    try:
        verifier = identity_config.get_cis_staff_verifier()
    except Exception as exc:  # noqa: BLE001 - a bad issuer/client override must answer 503, not 500
        logger.error("staff_identity_verifier_unavailable", error=type(exc).__name__)
        raise StaffAuthError(503, "Staff sign-in is unavailable", "verifier_build_failed") from exc
    try:
        return await verifier.verify(token)
    except InvalidToken as exc:
        raise StaffAuthError(401, "Invalid staff token", f"invalid_token:{exc}") from exc
    except AuthUnavailable as exc:
        logger.error("staff_identity_jwks_unavailable")
        raise StaffAuthError(503, "Staff sign-in is unavailable", "jwks_unavailable") from exc


# ── Directory lookup (shared.admin_users, migration 247) ──────────────────────────────────────────

_ROW_COLUMNS = ("id::text AS id, username, role::text AS role, email, display_name, is_active, is_owner")

SQL_BY_IDENTITY = (
    f"SELECT {_ROW_COLUMNS} FROM shared.admin_users WHERE cognito_iss = $1 AND cognito_sub = $2"
)
# First sign-in: bind the identity to the active, still unbound row with that address, exactly once.
SQL_FIRST_LINK = (
    "UPDATE shared.admin_users SET cognito_iss = $1, cognito_sub = $2, updated_at = now() "
    "WHERE lower(email) = $3 AND cognito_iss IS NULL AND cognito_sub IS NULL AND is_active "
    f"RETURNING {_ROW_COLUMNS}"
)
# Only for the refusal log: why the first link did not happen.
SQL_BY_EMAIL = (
    "SELECT id::text AS id, is_active, cognito_sub IS NOT NULL AS bound FROM shared.admin_users "
    "WHERE lower(email) = $1"
)
SQL_TOUCH_LOGIN = "UPDATE shared.admin_users SET last_login_at = now() WHERE id = $1::uuid"

# (iss, sub) -> (expires_at, row or None, last_login_at written for this entry)
_cache: dict[tuple[str, str], tuple[float, Optional[StaffRow], bool]] = {}


def clear_cache() -> None:
    _cache.clear()


def _row(record: Any) -> StaffRow:
    return StaffRow(
        admin_id=record["id"], username=record["username"], db_role=record["role"],
        email=record["email"], display_name=record["display_name"],
        is_owner=bool(record.get("is_owner") or False),
    )


async def _load(conn: Any, principal: Principal) -> Optional[StaffRow]:
    """The ACTIVE row bound to (iss, sub), binding it on the first sign-in; None when refused (logged)."""
    record = await conn.fetchrow(SQL_BY_IDENTITY, principal.iss, principal.sub)
    if record is None:
        email = (principal.email or "").strip().lower()
        if not email:
            logger.warning("staff_identity_refused", reason="unknown_identity_no_email", sub=principal.sub)
            return None
        if principal.email_verified is False:
            # ADR 0006 §6: link by email only on a verified address (defence in depth; the trigger checks too).
            logger.warning("staff_identity_refused", reason="email_not_verified", sub=principal.sub)
            return None
        try:
            record = await conn.fetchrow(SQL_FIRST_LINK, principal.iss, principal.sub, email)
        except asyncpg.UniqueViolationError:
            record = None  # a concurrent first sign-in bound it a moment ago; read it below
        if record is not None:
            logger.info("staff_identity_linked", admin_id=record["id"], sub=principal.sub,
                        client_id=principal.client_id)
        else:
            record = await conn.fetchrow(SQL_BY_IDENTITY, principal.iss, principal.sub)
            if record is None:
                other = await conn.fetchrow(SQL_BY_EMAIL, email)
                reason = ("unknown_email" if other is None
                          else "inactive" if not other["is_active"]
                          else "bound_to_other_identity" if other["bound"]
                          else "not_linked")
                logger.warning("staff_identity_refused", reason=reason, sub=principal.sub,
                               admin_id=other["id"] if other is not None else None)
                return None
    if not record["is_active"]:
        logger.warning("staff_identity_refused", reason="inactive", admin_id=record["id"], sub=principal.sub)
        return None
    return _row(record)


async def _cached_row(db: Any, principal: Principal, *, touch_login: bool) -> tuple[Optional[StaffRow], bool]:
    """(row or None, loaded_now). Cached for STAFF_STATE_TTL_S by (iss, sub), misses included.

    ``touch_login`` writes ``last_login_at`` at most once per cache entry, also when the entry was loaded by
    the bearer middleware (which never touches it), so the column stays at most one TTL behind."""
    key = (principal.iss, principal.sub)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        expires_at, row, touched = hit
        if touch_login and row is not None and not touched and await _touch(db, row):
            _cache[key] = (expires_at, row, True)
        return row, False
    try:
        async with db.acquire() as conn:
            row = await _load(conn, principal)
    except Exception as exc:  # noqa: BLE001 - a directory outage is 503, never a 500 or an open door
        logger.error("staff_identity_directory_unavailable", error=type(exc).__name__)
        raise StaffAuthError(503, "Staff directory is unavailable", "directory_unavailable") from exc
    touched = bool(touch_login and row is not None and await _touch(db, row))
    if len(_cache) >= STAFF_STATE_MAX:
        _cache.clear()
    _cache[key] = (now + STAFF_STATE_TTL_S, row, touched)
    return row, True


async def _touch(db: Any, row: StaffRow) -> bool:
    """Best effort ``last_login_at = now()``: bookkeeping, not authentication. A failure is logged and the
    sign-in still succeeds (the next verify-admin call retries)."""
    try:
        async with db.acquire() as conn:
            await conn.execute(SQL_TOUCH_LOGIN, row.admin_id)
        return True
    except Exception as exc:  # noqa: BLE001 - never fail a verified sign-in over bookkeeping
        logger.warning("staff_identity_touch_failed", admin_id=row.admin_id, error=type(exc).__name__)
        return False


async def resolve_staff(db: Any, principal: Principal, *, touch_login: bool = False) -> StaffIdentity:
    """The CIS staff member behind a verified WORKFORCE token, or StaffAuthError (403 no CIS group,
    401 unknown / inactive / bound to another identity, 503 directory unavailable)."""
    role = role_from_groups(principal.groups)
    if role is None:
        logger.warning("staff_identity_refused", reason="no_cis_group", sub=principal.sub,
                       client_id=principal.client_id, groups=list(principal.groups))
        raise StaffAuthError(403, "No CIS staff group", "no_cis_group")
    if db is None:
        raise StaffAuthError(503, "Staff directory is unavailable", "no_pool")
    row, loaded_now = await _cached_row(db, principal, touch_login=touch_login)
    if row is None:
        raise StaffAuthError(401, "Staff user is inactive or unknown", "inactive_or_unknown")
    if loaded_now and row.db_role != role:
        # The group decides (ADR 0006); the reconcile job (C7) reports the difference.
        logger.warning("staff_role_drift", admin_id=row.admin_id, group_role=role, db_role=row.db_role,
                       sub=principal.sub)
    return StaffIdentity(
        admin_id=row.admin_id, username=row.username, role=role, db_role=row.db_role,
        email=row.email or principal.email, display_name=row.display_name,
        iss=principal.iss, sub=principal.sub, client_id=principal.client_id, is_owner=row.is_owner,
    )


async def authenticate(db: Any, token: str, *, touch_login: bool = False) -> StaffIdentity:
    """verify_staff_token + resolve_staff."""
    principal = await verify_staff_token(token)
    return await resolve_staff(db, principal, touch_login=touch_login)
