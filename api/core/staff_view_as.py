"""AA-1157 (S288, root ADR 0019 d5 as amended) — read-only "view as a lower staff role" for an owner.

An owner (``shared.admin_users.is_owner`` on a verified Cognito session, holding the admin role: the ``OWNER_PERMS``
rule of :mod:`api.core.staff_authz`) sees CIS as a lower staff role sees it, without a second account and without
touching the sign-in: the Cognito session stays exactly as it was (no new token, no cookie of the sign-in rewritten).

How it works
------------
1. ``POST /admin/view-as/roles`` (api/routers/admin_view_as.py; capability ``staff_authz.STAFF_VIEW_AS_ROLE``, owner
   only) checks the target role (:func:`target_role`: a built-in CIS role, or an active CIS custom role) and that it is
   downward (:func:`downward`, tech lead Q6: the target's read rights are a subset of the viewer's), ends the caller's
   other open view-as sessions (either kind: one view at a time), writes one ``shared.staff_view_as_session`` row
   (``tenant_id`` NULL, ``view_role`` set; CIS 470) and one ``staff.view_as_started`` audit row whose details hold the
   target role's FULL capability list, and answers a short-lived signed token (:func:`mint_token`) to the staff BFF only
   (header ``x-view-as-client: bff``). The BFF keeps it in an httpOnly cookie and never hands it to the page.
2. The staff BFF forwards the token on every staff API call in header :data:`HEADER`. The staff bearer middleware
   (api/middleware/staff_bearer.py), after it verified the Cognito token, calls :func:`resolve`: the token must be
   valid and the viewer's own, the session row open, the viewer still an owner and admin, the target role still
   usable and still downward (a custom role that cannot be read right now answers 503 and leaves the session open).
   Then the request runs as the target role: ``x-admin-role`` = the view role,
   capabilities = target ∩ viewer, ``is_owner`` False, ``view_as`` set on the identity. Otherwise the session is ended
   for good (``staff.view_as_ended`` row, cause) and the request answers 409 :data:`ENDED_DETAIL`.
3. Every non-GET/HEAD/OPTIONS request of such a session is refused 403 ``view_as_read_only``. In the normal browser
   flow the staff BFF refuses it first and never forwards it (lib/auth-server.ts; its trace is one structured log
   line ``staff_view_as_write_refused`` in the admin web's log, no API row). A write that reaches the API with the
   header anyway is refused before any route code (api/middleware/staff_view_as.py, inside the staff audit middleware,
   so it is logged as a ``staff.request`` row with status 403 and ``details.view_as``), and again by
   ``staff_authz.denial_reason`` (defence in depth).
4. The relays that send the staff member's own token to another app (crm.ops.v1) never run during a view: the bearer
   middleware does not keep the token for such a request (api/services/platform_apps.relay_authorization answers None,
   the relay answers "unavailable").
5. Ending: ``POST /admin/view-as/sessions/{id}/end`` (the BFF's Back button, sent WITHOUT the view-as header), a new
   start, sign-out / staff disable (:func:`api.core.view_as.close_open_sessions`), the per-request check, or the
   owner's next read of the session once it expired (``GET /admin/view-as/sessions/{id}``: the banner's check at 0
   minutes, through the BFF whose token cookie outlives the view by a short grace). Every end writes one
   ``staff.view_as_ended`` row for a role session (``tenant.view_as_ended`` for a tenant one). Starts take a
   per-member advisory lock, so two tabs never leave two open sessions.

The token is not a tenant token: its ``aud`` (:data:`AUDIENCE`) makes ``api/routers/auth.verify_jwt`` (which decodes
without an audience) refuse it, and it carries no ``role`` claim.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import jwt
import structlog

from api.core import staff_roles, view_as

logger = structlog.get_logger()

HEADER = "x-staff-view-as"
HEADER_BYTES = HEADER.encode()
TOKEN_TYPE = "staff_view_as"
AUDIENCE = "cis-staff-view-as"
SESSION_CLAIM, STAFF_CLAIM, ROLE_CLAIM, CAPS_CLAIM = "svs", "stf", "vrl", "vcp"

READ_ONLY_DETAIL = view_as.READ_ONLY_DETAIL          # "view_as_read_only": same answer as the tenant view
ENDED_DETAIL = "staff_view_as_ended"
NEEDS_COGNITO_DETAIL = "staff_view_as_needs_cognito"
NOT_OWN_DETAIL = "staff_view_as_not_own"
NOT_DOWNWARD_DETAIL = "view_as_not_downward"
UNKNOWN_ROLE_DETAIL = "view_as_unknown_role"
OWNER_ONLY_MESSAGE = "Only a platform owner can view as another role"

AUDIT_RESOURCE = view_as.AUDIT_RESOURCE              # "staff_view_as_session"
GATE_ACTOR = "system:staff_view_as"
# details.cause of a staff.view_as_ended row written by resolve()
RESOLVE_END_CAUSES = frozenset({"expired", "owner_lost", "role_unavailable", "not_downward"})

READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Read capabilities checked inside handlers (not POLICY entries) that a stronger read implies: holding the key implies
# every value. Used by :func:`read_surface` so a viewer who reads every tenant text is not told it cannot see the
# narrower "own book" read of the target.
IMPLIED_READS: dict[str, frozenset[str]] = {
    "tenant_content.read": frozenset({"lab.tenant_text.read", "tenant_content.book.read", "lab.read"}),
    "tenant.read": frozenset({"tenant.book.read", "tenant.book.account.read"}),
}
# Capabilities that open data without being named "*.read" (each one is a read power a target may hold).
EXTRA_READ_CAPS = frozenset({"tenant.view_as", "learn.preview"})


@dataclass(frozen=True)
class StaffViewAs:
    """The open view-as session of one request (on the swapped identity and the scope state)."""
    session_id: str
    view_role: str
    label: str
    viewer_role: str
    expires_at: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class StaffViewAsRefused(Exception):
    """A view-as header that may not be used: ``status`` / ``detail`` to answer, ``reason`` for the log."""

    def __init__(self, status: int, detail: str, reason: str) -> None:
        super().__init__(status, detail, reason)
        self.status, self.detail, self.reason = status, detail, reason


def session_of(identity: Any, role: Optional[str] = None) -> Optional[str]:
    """The session id of the view-as-a-role session the verified Cognito ``identity`` runs in (the identity the staff
    bearer middleware swapped; ``role``, when given, must be its role), else None (staff_authz, staff_roles)."""
    view = getattr(identity, "view_as", None) if identity is not None else None
    if view is None or getattr(identity, "auth", None) != "cognito":
        return None
    if role is not None and getattr(identity, "role", None) != role:
        return None
    sid = getattr(view, "session_id", None)
    return sid if isinstance(sid, str) and sid else None


def effective_perms(identity: Any) -> frozenset[str]:
    """The capabilities of a view-as request: the swapped identity's (target ∩ viewer), built-in role or custom."""
    return frozenset(getattr(identity, "capabilities", None) or ())


# ── Downward (tech lead Q6) ──────────────────────────────────────────────────────────────────────────────────────
def is_read_cap(capability: str) -> bool:
    return capability.endswith(".read") or capability in EXTRA_READ_CAPS


def _expanded(perms: Iterable[str]) -> frozenset[str]:
    out = set(perms)
    for key, implied in IMPLIED_READS.items():
        if key in out:
            out |= implied
    return frozenset(out)


def read_surface(perms: Iterable[str]) -> frozenset[str]:
    """What ``perms`` may READ in CIS: every GET / HEAD route of ``staff_authz.POLICY`` they pass (owner-only entries
    left out), the unlisted GETs (``OPEN_READ``) when they hold it, and every read capability a handler checks itself
    (``cap:<name>``, with :data:`IMPLIED_READS`). Writes are not part of it: a view-as session never writes."""
    from api.core.staff_authz import OPEN_READ, OWNER_PERMS, POLICY, policy_capabilities

    held = _expanded(perms)
    surface: set[str] = set()
    for (method, route), entry in POLICY.items():
        if method not in ("GET", "HEAD"):
            continue
        caps = policy_capabilities(entry)
        if any(c in OWNER_PERMS for c in caps):
            continue
        if any(c in held for c in caps):
            surface.add(f"{method} {route}")
    if OPEN_READ in held:
        surface.add("GET *")
    surface |= {f"cap:{c}" for c in held if is_read_cap(c)}
    return frozenset(surface)


def downward(viewer_perms: Iterable[str], target_perms: Iterable[str]) -> tuple[bool, list[str]]:
    """(ok, missing): the target's read surface is a subset of the viewer's. ``missing`` is for the log only (the
    refusal never names a capability)."""
    missing = sorted(read_surface(target_perms) - read_surface(viewer_perms))
    return not missing, missing


# ── Target role ──────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TargetRole:
    code: str
    label: str
    capabilities: frozenset[str]
    custom: bool


UNAVAILABLE_DETAIL = "Staff roles are unavailable right now; try again in a moment"


async def target_role(conn: Any, code: Any) -> Optional[TargetRole]:
    """A built-in CIS role (``ROLE_PERMS``, the viewer included) or an active CIS custom role (state ok: known, not
    archived, with capabilities); None otherwise. A custom role that cannot be READ (a transient database error:
    ``staff_roles.STATE_UNREADABLE``) is neither: :class:`StaffViewAsRefused` 503, so the caller refuses this request
    without ending the session or recording a revocation that never happened."""
    from api.core.staff_authz import ROLE_PERMS

    if not isinstance(code, str):
        return None
    if code in ROLE_PERMS:
        return TargetRole(code, staff_roles.CIS_ROLE_LABELS.get(code, code), frozenset(ROLE_PERMS[code]), False)
    if not staff_roles.is_custom_code(code):
        return None
    grant = await staff_roles.load_cis_grant(conn, code)
    if grant.state == staff_roles.STATE_UNREADABLE:
        raise StaffViewAsRefused(503, UNAVAILABLE_DETAIL, "role_unreadable")
    if not grant.ok or grant.role is None:
        return None
    return TargetRole(code, grant.role.label, frozenset(grant.role.capabilities), True)


VIEW_FROM_ROLE = "admin"   # technical-constant: the one built-in role a view starts from (BFF lib/staffViewAs.ts)


def may_start(identity: Any) -> bool:
    """An owner on a verified Cognito session holding the built-in admin role (and staff.read), never a Preview or E2E
    session and never inside a view already (is_owner is False there). A custom role never qualifies, even one that
    holds staff.read: the BFF honours the view cookie only on the admin role (lib/staffViewAs.ts VIEW_FROM_ROLE), so
    both sides use the same rule."""
    from api.core.staff_authz import STAFF_READ

    return bool(identity is not None and getattr(identity, "auth", None) == "cognito"
                and getattr(identity, "is_owner", False) and not getattr(identity, "preview", False)
                and not getattr(identity, "e2e_read_only", False) and getattr(identity, "view_as", None) is None
                and getattr(identity, "role", None) == VIEW_FROM_ROLE
                and STAFF_READ in (getattr(identity, "capabilities", None) or ()))


# ── Token ────────────────────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TokenClaims:
    session_id: str
    staff_id: str
    view_role: str
    capabilities: tuple[str, ...]


def mint_token(*, session_id: str, staff_id: str, view_role: str, capabilities: Iterable[str],
               expires_at: datetime, now: Optional[datetime] = None) -> str:
    """The signed token of one session (same secret as the tenant tokens, its own ``aud`` and ``typ``). ``vcp`` (the
    effective capabilities) is a hint for the BFF's page gate only; the API recomputes them on every request."""
    from api.routers.auth import JWT_ALG, JWT_SECRET

    payload = {
        "typ": TOKEN_TYPE, "aud": AUDIENCE,
        SESSION_CLAIM: session_id, STAFF_CLAIM: staff_id, ROLE_CLAIM: view_role,
        CAPS_CLAIM: sorted(set(capabilities)),
        "iat": now or datetime.now(timezone.utc), "exp": expires_at,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def verify_token(token: Any) -> Optional[TokenClaims]:
    """The claims of a valid, unexpired token of this kind, or None."""
    from api.routers.auth import JWT_ALG, JWT_SECRET

    if not isinstance(token, str) or not token or len(token) > 8192:
        return None
    try:
        p = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG], audience=AUDIENCE,
                       options={"require": ["exp", "aud"]})
    except jwt.PyJWTError:
        return None
    if p.get("typ") != TOKEN_TYPE:
        return None
    sid, stf, vrl = view_as._uuid(p.get(SESSION_CLAIM)), view_as._uuid(p.get(STAFF_CLAIM)), p.get(ROLE_CLAIM)
    caps = p.get(CAPS_CLAIM)
    if sid is None or stf is None or not isinstance(vrl, str) or not isinstance(caps, list):
        return None
    return TokenClaims(sid, stf, vrl, tuple(c for c in caps if isinstance(c, str)))


# ── Session state (per request) ──────────────────────────────────────────────────────────────────────────────────
# $1 session, $2 staff, $3 view role: a role session (tenant_id NULL) of this staff member for this role.
STATE_SQL = (
    "SELECT ended_at IS NULL AND expires_at > now() AS open, ended_at IS NULL AS not_ended, expires_at "
    "FROM shared.staff_view_as_session "
    "WHERE session_id = $1::uuid AND admin_user_id = $2::uuid AND view_role = $3 AND tenant_id IS NULL"
)
CLOSE_SQL = (
    "UPDATE shared.staff_view_as_session SET ended_at = greatest(now(), started_at), "
    "end_reason = CASE WHEN expires_at <= now() THEN 'expired' ELSE $2 END "
    "WHERE session_id = $1::uuid AND ended_at IS NULL RETURNING end_reason, view_role"
)


async def close(conn: Any, *, session_id: str, staff_id: str, view_role: str, cause: str,
                actor: str = GATE_ACTOR) -> Optional[str]:
    """End one role session for ``cause`` and write its staff.view_as_ended row (one transaction, audit row last).
    The end_reason, or None when it was already closed. Best effort: a failure is logged, the caller refuses anyway."""
    from services.acp_shared.audit_log import write_audit_log

    try:
        async with conn.transaction():
            row = await conn.fetchrow(CLOSE_SQL, session_id, "expired" if cause == "expired" else "revoked")
            if row is None:
                return None
            await write_audit_log(conn, **view_as.ended_audit(
                tenant_id=None, session_id=session_id, actor=actor, end_reason=row["end_reason"], cause=cause,
                session_staff=staff_id, view_role=view_role))
    except Exception as e:  # noqa: BLE001 - the refusal stands; the next request retries the close
        logger.warning("staff_view_as_close_failed", error=repr(e)[:300], session_id=session_id, cause=cause)
        return None
    return row["end_reason"]


def _refuse_ended(reason: str) -> StaffViewAsRefused:
    return StaffViewAsRefused(409, ENDED_DETAIL, reason)


async def resolve(pool: Any, identity: Any, header_value: str) -> tuple[Any, StaffViewAs]:
    """The identity a request with the view-as header runs as, and its session; or :class:`StaffViewAsRefused`.

    ``identity`` is the viewer's verified Cognito StaffIdentity. The token must verify and be the viewer's own (403
    otherwise, nothing closed: a foreign token never ends another member's session); the row open (else it is closed
    as expired / was ended: 409); the viewer still an owner and admin, the target role still usable and still
    downward (else the session ends 'revoked': 409)."""
    claims = verify_token(header_value)
    if claims is None:
        raise _refuse_ended("invalid_token")
    if claims.staff_id != getattr(identity, "admin_id", None):
        raise StaffViewAsRefused(403, NOT_OWN_DETAIL, "not_own")
    if pool is None:
        raise StaffViewAsRefused(503, "Staff directory is unavailable", "no_pool")
    viewer_caps = effective_perms(identity)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(STATE_SQL, claims.session_id, claims.staff_id, claims.view_role)
        if row is None:
            raise _refuse_ended("unknown_session")
        if not row["open"]:
            if row["not_ended"]:
                await close(conn, session_id=claims.session_id, staff_id=claims.staff_id,
                            view_role=claims.view_role, cause="expired")
                raise _refuse_ended("expired")
            raise _refuse_ended("ended")
        target, cause = await _still_allowed(conn, identity, claims.view_role, viewer_caps)
        if target is None:
            await close(conn, session_id=claims.session_id, staff_id=claims.staff_id, view_role=claims.view_role,
                        cause=cause)
            raise _refuse_ended(cause)
    expires = row["expires_at"]
    session = StaffViewAs(
        session_id=claims.session_id, view_role=target.code, label=target.label,
        viewer_role=getattr(identity, "role", "") or "",
        expires_at=expires.isoformat() if isinstance(expires, datetime) else None)
    swapped = dataclasses.replace(
        identity, role=target.code, capabilities=frozenset(target.capabilities & viewer_caps), is_owner=False,
        custom_label=target.label if target.custom else None, view_as=session)
    return swapped, session


async def _still_allowed(conn: Any, identity: Any, view_role: str,
                         viewer_caps: frozenset[str]) -> tuple[Optional[TargetRole], str]:
    """(target, "") while the viewer is still an owner and admin and the role still usable and downward; else (None,
    the cause the session ends with). A role that cannot be read right now raises :class:`StaffViewAsRefused` 503
    (:func:`target_role`): the session stays open and the next request checks again."""
    if not may_start(identity):
        return None, "owner_lost"
    target = await target_role(conn, view_role)
    if target is None:
        return None, "role_unavailable"
    if not downward(viewer_caps, target.capabilities)[0]:
        return None, "not_downward"
    return target, ""


def started_details(*, session_id: str, viewer_role: str, target: TargetRole, minutes: int,
                    expires_at: Any, ended_sessions: list[str]) -> dict[str, Any]:
    """details of the staff.view_as_started row: the viewer's role, the target role and its FULL capability list (the
    issue's Done-when), the window. Never the reason text (free text, stays in the session row)."""
    return {
        "session_id": session_id, "viewer_role": viewer_role, "view_role": target.code, "view_label": target.label,
        "capabilities": sorted(target.capabilities), "minutes": minutes,
        "expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at,
        "ended_sessions": ended_sessions,
    }
