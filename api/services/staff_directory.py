"""AA-816 step C6 — the staff directory writer: shared.admin_users first, then the WORKFORCE Cognito pool.

Root ADR 0006 §3: the directory (``shared.admin_users`` + ``shared.staff_app_grant``) is the source of truth for who
is staff and the CIS admin UI is the ONLY writer of WORKFORCE users and group memberships. Every operation here
commits the DB change (and its ``acp_shared.audit_log`` row) first, then calls Cognito. A Cognito failure never
rolls the DB back: the caller answers 502 ``{"status": "saved_cognito_pending"}`` and the owner retries with
:func:`sync_staff` (``POST /admin/platform/staff/{id}/sync``), which makes the pool match the row again. Every
Cognito step is idempotent, so a retry (or a re-POST of the same create) is always safe.

Cognito calls (the CIS ECS task role's allow-list on the WORKFORCE pool, ADR 0006 §3): ``AdminCreateUser``,
``AdminAddUserToGroup``, ``AdminRemoveUserFromGroup``, ``AdminDisableUser``, ``AdminEnableUser``,
``AdminUserGlobalSignOut`` (``ListUsers`` / ``ListUsersInGroup`` are the reconcile job's, C7). Nothing else is ever
called: :data:`COGNITO_OPERATIONS` is enforced by :class:`CognitoDirectory`, so ``AdminGetUser`` (counts as MAU),
``AdminSetUserPassword`` or ``AdminDeleteUser`` cannot slip in.

Operations
----------
- create: ``@adventure.asia`` only (no exception: the temporary E2E test address of migration 250 was removed by
  migration 252, root ADR 0006a A10); username derived from the address's local part (unique); ``password_hash``
  NULL (Cognito-only member); optional Booking grant. Cognito: ``AdminCreateUser`` with ``MessageAction=SUPPRESS``
  (always: without it Cognito mails a temporary password and the person could set a permanent password through
  managed login, bypassing email-code-only sign-in) and ``email`` + ``email_verified=true``. Then
  ``AdminAddUserToGroup`` ``cis-<role>`` and, with a grant, ``booking-<role>``. An existing user
  (``UsernameExistsException``: left over from the bootstrap tool, an old offboarding, or the first attempt of a
  re-POST) counts as created, but its state is unknown (the writer may not read it), so the create then also runs
  ``AdminEnableUser`` and removes every other CIS / Booking staff group, exactly like an active-row sync: a stale
  ``cis-admin`` must never outrank the role the directory shows.
- roles: the CIS role (``admin_users.role``) and/or the Booking grant (set or removed). Cognito: add the new group,
  then remove the old one (a failure in between leaves the person with MORE access for a moment, never locked out
  of both; the retry finishes it).
- disable: ``is_active = false``, then ``AdminDisableUser``, then ``AdminUserGlobalSignOut`` (ADR 0006 §7: a disabled
  user cannot re-authenticate even with the managed-login cookie; the worst case is one live 15-minute access
  token). ``AdminDisableUser`` already revokes the user's tokens, so a ``NotAuthorizedException`` from the sign-out
  that follows a successful disable counts as done (``global_sign_out(..., user_disabled=True)``).
- enable: ``is_active = true``, then ``AdminEnableUser``.
- sign-out ("reset", ADR 0006 amendment): the account is re-enabled when it was disabled (``is_active = true``),
  then ``AdminEnableUser`` (always, a no-op on an enabled user) BEFORE ``AdminUserGlobalSignOut``: sign-out on a
  disabled Cognito user may be refused, and a failed sign-out must not leave the row active and the pool disabled.
- sync (the retry): make the pool match the row (active: create if missing, enable, add the wanted groups, remove
  the other staff groups; inactive: disable + sign out).
- invitation (S242, api/services/staff_invite.py): because AdminCreateUser is SUPPRESS, Cognito tells nobody. The
  create (``send_invite``) and ``resend_invite`` queue one ``staff_invite_email`` job in the same transaction; the
  worker sends the branded e-mail through SES. No Cognito call.

Guards (checked in the same transaction, on rows locked ``FOR UPDATE``)
--------------------------------------------------------------------
- The actor is re-read: an active row with ``is_owner`` AND ``role = 'admin'`` (the route policy already required
  an owner on a Cognito session, from a row cached up to 60 s, and took the admin role from the access token's
  groups, which lag a demotion by up to 15 minutes; the re-read closes both windows).
- Nobody demotes (CIS role other than admin) or disables themself.
- The last active owner (``is_owner AND is_active AND role = 'admin'``; an owner without the admin role cannot
  open the staff page) is never demoted or disabled. Every owner row is locked first, so two owners cannot remove
  each other concurrently.
- The break-glass row (username ``admin`` / a break-glass address, :func:`is_breakglass`) is never changed here: it
  is enabled and disabled only by the break-glass runbook.

Ordering: the Cognito half runs after the row locks are released, so two opposite writes on the same row a moment
apart (disable in one tab, enable in another) can reach Cognito in reverse order. Accepted: the DB stays the source
of truth, ``sync`` repairs it, and the C7 reconcile job reports the mismatch.

Time budget: one attempt per Cognito call (connect 2 s, read 4 s) and a 20 s deadline on the step list, so the
answer (ok or pending) always beats the API Gateway 29 s integration timeout; a step skipped by the deadline is
reported as pending (``error_code = "DeadlineExceeded"``) and the retry finishes it.

Rows without an email (legacy, unlinked) have no Cognito user: role / active changes touch the DB only and report
``cognito = "not_applicable"``; sync refuses them.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import asyncpg
import structlog
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from api.core import identity_config, staff_identity
from api.services import staff_invite
from services.acp_shared.audit_log import write_audit_log

logger = structlog.get_logger()

STAFF_EMAIL_DOMAIN = "adventure.asia"
# Same local-part rule as the bootstrap tool (AA-CIS-Infra tools/identity/workforce_bootstrap.py EMAIL_LOCAL_RE).
EMAIL_LOCAL_RE = re.compile(r"^[a-z0-9._%+-]+$")
MAX_EMAIL_LEN = 254
MAX_DISPLAY_NAME_LEN = 120
MAX_USERNAME_LEN = 100            # shared.admin_users.username varchar(100)
_USERNAME_SUFFIX_TRIES = 50

# shared.admin_role minus 'viewer' (AA-878, migration 249): the read-only E2E role is never assigned from here.
CIS_ROLES: tuple[str, ...] = ("admin", "reviewer", "content")
BOOKING_ROLES: tuple[str, ...] = ("admin", "sales", "contracting", "content_creator")  # staff_app_grant_role_chk
APP_BOOKING = "booking"

# The break-glass account (ADR 0006 decision 11; S242 plan D1). Never written from the UI.
BREAKGLASS_USERNAMES = frozenset({"admin"})
BREAKGLASS_EMAILS = frozenset({"admin@adventure.asia", "breakglass@adventure.asia"})

# The only Cognito operations this module may call (the writer half of the task role's allow-list).
COGNITO_OPERATIONS = frozenset({
    "AdminCreateUser", "AdminAddUserToGroup", "AdminRemoveUserFromGroup",
    "AdminDisableUser", "AdminEnableUser", "AdminUserGlobalSignOut",
})

STATUS_OK = "ok"
STATUS_PENDING = "saved_cognito_pending"
COGNITO_DONE = "done"
COGNITO_PENDING = "pending"
COGNITO_NOT_APPLICABLE = "not_applicable"

# WriteResult.invite (S242): what happened to the invitation e-mail of a create / resend.
INVITE_QUEUED = "queued"                    # a staff_invite_email job was committed with the change
INVITE_NOT_REQUESTED = "not_requested"      # the owner unticked "Send invitation email"
INVITE_NOT_CONFIGURED = "not_configured"    # SES_FROM_ADDRESS unset on this task: nothing queued
INVITE_EXISTING = "existing_account"        # re-POST of an existing create: no new invite (use Resend)


class StaffAudit:
    """``acp_shared.audit_log.action`` values of the staff directory (resource_type ``admin_user``)."""
    CREATED = "staff.created"
    ROLES_CHANGED = "staff.roles_changed"
    DISABLED = "staff.disabled"
    ENABLED = "staff.enabled"
    SIGNED_OUT = "staff.signed_out"
    COGNITO_SYNCED = "staff.cognito_synced"
    COGNITO_PENDING = "staff.cognito_pending"
    # S242 invitation e-mail (api/services/staff_invite.py; sent / not_sent are written by the worker)
    INVITE_REQUESTED = staff_invite.InviteAudit.REQUESTED
    INVITE_SENT = staff_invite.InviteAudit.SENT
    INVITE_NOT_SENT = staff_invite.InviteAudit.NOT_SENT


RESOURCE_TYPE = "admin_user"


def cis_group(role: str) -> str:
    return f"cis-{role}"


def booking_group(role: str) -> str:
    return f"booking-{role}"


CIS_GROUPS: tuple[str, ...] = tuple(cis_group(r) for r in CIS_ROLES)
BOOKING_GROUPS: tuple[str, ...] = tuple(booking_group(r) for r in BOOKING_ROLES)


class StaffDirectoryError(Exception):
    """A refused write. ``status`` is the HTTP status, ``detail`` the message shown to the owner."""

    def __init__(self, status: int, detail: str, code: str, headers: Optional[dict[str, str]] = None) -> None:
        super().__init__(status, detail, code, headers)
        self.status = status
        self.detail = detail
        self.code = code
        self.headers = headers


@dataclass
class WriteResult:
    """What a write did. ``status`` is ``ok`` or ``saved_cognito_pending`` (DB saved, a Cognito step failed)."""
    staff_id: str
    operation: str
    status: str = STATUS_OK
    cognito: str = COGNITO_DONE
    steps: list[str] = field(default_factory=list)       # Cognito steps that succeeded, in order
    failed_step: Optional[str] = None
    error_code: Optional[str] = None
    created: bool = False                                # create only: a new row was inserted
    invite: Optional[str] = None                         # create / invite: INVITE_* (None for other writes)
    invite_job_id: Optional[str] = None

    @property
    def pending(self) -> bool:
        return self.status == STATUS_PENDING


# ── Validation ───────────────────────────────────────────────────────────────────────────────────

def normalise_email(email: Any) -> str:
    return (email or "").strip().lower() if isinstance(email, str) or email is None else ""


def require_staff_email(email: Any) -> str:
    """The lower-cased ``@adventure.asia`` address, or 400. Mirrors the DB CHECK admin_users_email_chk and the
    WORKFORCE pre sign-up trigger (which refuses other domains too). There is no exception (root ADR 0006a A10)."""
    value = normalise_email(email)
    local, sep, domain = value.rpartition("@")
    if not sep or not local or domain != STAFF_EMAIL_DOMAIN:
        raise StaffDirectoryError(400, f"Only @{STAFF_EMAIL_DOMAIN} addresses can be staff accounts",
                                  "email_domain")
    if len(value) > MAX_EMAIL_LEN or not EMAIL_LOCAL_RE.match(local):
        raise StaffDirectoryError(400, "Unsupported character in the email address (allowed: a-z 0-9 . _ % + -)",
                                  "email_invalid")
    return value


def require_display_name(name: Any) -> str:
    value = " ".join((name or "").split()) if isinstance(name, str) else ""
    if not value:
        raise StaffDirectoryError(400, "A display name is required", "display_name_missing")
    if len(value) > MAX_DISPLAY_NAME_LEN:
        raise StaffDirectoryError(400, f"Display name is longer than {MAX_DISPLAY_NAME_LEN} characters",
                                  "display_name_too_long")
    return value


def require_cis_role(role: Any) -> str:
    if role not in CIS_ROLES:
        raise StaffDirectoryError(400, f"CIS role must be one of {', '.join(CIS_ROLES)}", "cis_role")
    return role


def require_booking_role(role: Any) -> Optional[str]:
    if role is None:
        return None
    if role not in BOOKING_ROLES:
        raise StaffDirectoryError(400, f"Booking role must be one of {', '.join(BOOKING_ROLES)}", "booking_role")
    return role


def username_base(email: str) -> str:
    """The username stem from the address's local part: a-z 0-9 . _ - only (anything else becomes '-')."""
    local = email.rpartition("@")[0]
    base = re.sub(r"[^a-z0-9._-]+", "-", local).strip("-.")[: MAX_USERNAME_LEN - 4]
    if not base:
        raise StaffDirectoryError(400, "Cannot derive a username from this address", "username_invalid")
    return base


def is_breakglass(username: Optional[str], email: Optional[str]) -> bool:
    return (username or "").strip().lower() in BREAKGLASS_USERNAMES or normalise_email(email) in BREAKGLASS_EMAILS


# ── Cognito ──────────────────────────────────────────────────────────────────────────────────────

# One attempt per call (the owner has a Retry button; a botocore retry would only eat the API Gateway budget).
_BOTO_CONFIG = BotoConfig(connect_timeout=2, read_timeout=4, retries={"max_attempts": 1, "mode": "standard"})
# Wall-clock budget for one write's Cognito steps: no step STARTS after it (worst case = budget + one call's 6 s,
# still under the ~29 s API Gateway integration timeout).
STEPS_DEADLINE_S = 20.0
_monotonic = time.monotonic   # module-level so tests can drive the deadline without touching asyncio's clock


def _snake(operation: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", operation).lower()


class CognitoDirectory:
    """The WORKFORCE pool as the directory writer sees it. Sync boto3 (callers run it through
    ``asyncio.to_thread``); the client is built on first use, once, on its own Session (the default Session is
    not thread-safe, AA-859). Pool id and region come from ``api/core/identity_config.py``."""

    def __init__(self, client: Any = None, pool_id: Optional[str] = None) -> None:
        self._client = client
        self._pool_id = pool_id
        self._lock = threading.Lock()

    @property
    def pool_id(self) -> str:
        return self._pool_id or identity_config.workforce_pool_id()

    def client(self) -> Any:
        if self._client is None:
            with self._lock:
                if self._client is None:
                    import boto3

                    self._client = boto3.session.Session().client(
                        "cognito-idp", region_name=identity_config.workforce_region(), config=_BOTO_CONFIG)
        return self._client

    def _call(self, operation: str, **params: Any) -> Any:
        if operation not in COGNITO_OPERATIONS:
            raise RuntimeError(f"Cognito operation {operation} is not allowed for the staff directory")
        return getattr(self.client(), _snake(operation))(UserPoolId=self.pool_id, **params)

    def create_user(self, email: str) -> bool:
        """AdminCreateUser, always SUPPRESS + email_verified=true. False when the user already existed."""
        try:
            self._call(
                "AdminCreateUser",
                Username=email,
                UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}],
                MessageAction="SUPPRESS",
            )
            return True
        except ClientError as exc:
            if _error_code(exc) == "UsernameExistsException":
                return False
            raise

    def add_to_group(self, email: str, group: str) -> None:
        self._call("AdminAddUserToGroup", Username=email, GroupName=group)

    def remove_from_group(self, email: str, group: str) -> None:
        try:
            self._call("AdminRemoveUserFromGroup", Username=email, GroupName=group)
        except ClientError as exc:
            if _error_code(exc) != "UserNotFoundException":   # no user: it is in no group either
                raise

    def disable(self, email: str) -> None:
        try:
            self._call("AdminDisableUser", Username=email)
        except ClientError as exc:
            if _error_code(exc) != "UserNotFoundException":   # no user: nobody can sign in as it
                raise

    def enable(self, email: str) -> None:
        self._call("AdminEnableUser", Username=email)

    def global_sign_out(self, email: str, *, user_disabled: bool = False) -> None:
        """AdminUserGlobalSignOut. ``user_disabled=True`` only right after a successful AdminDisableUser in the
        same step list: Cognito may then refuse with ``NotAuthorizedException`` ("User is disabled"), which counts
        as done because AdminDisableUser already revoked the user's tokens."""
        try:
            self._call("AdminUserGlobalSignOut", Username=email)
        except ClientError as exc:
            code = _error_code(exc)
            if code == "UserNotFoundException":   # no user: no session to end
                return
            if user_disabled and code == "NotAuthorizedException":
                logger.info("staff_directory_sign_out_on_disabled_user", note="tokens already revoked by disable")
                return
            raise


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or "ClientError")
    return type(exc).__name__


_cognito: Optional[CognitoDirectory] = None
_cognito_lock = threading.Lock()


def get_cognito() -> CognitoDirectory:
    """The process-wide writer (FastAPI dependency; tests override it). Building it makes no AWS call."""
    global _cognito
    if _cognito is None:
        with _cognito_lock:
            if _cognito is None:
                _cognito = CognitoDirectory()
    return _cognito


Step = tuple[str, Callable[[], Any]]


def _mark_pending(result: WriteResult, step: str, error_code: str) -> WriteResult:
    result.status = STATUS_PENDING
    result.cognito = COGNITO_PENDING
    result.failed_step = step
    result.error_code = error_code
    logger.warning("staff_directory_cognito_failed", staff_id=result.staff_id,
                   operation=result.operation, step=step, error=error_code)
    return result


async def _run_steps(result: WriteResult, steps: list[Step], deadline_s: Optional[float] = None) -> WriteResult:
    """Run the Cognito steps in order; stop at the first failure (or when the deadline has passed before a step
    starts) and mark the result pending. A step may append further steps to ``steps`` (create does, when the user
    already existed); they run after the ones already listed."""
    started = _monotonic()
    budget = STEPS_DEADLINE_S if deadline_s is None else deadline_s
    for name, call in steps:
        if _monotonic() - started > budget:
            return _mark_pending(result, name, "DeadlineExceeded")
        try:
            await asyncio.to_thread(call)
        except (ClientError, BotoCoreError, Exception) as exc:  # noqa: BLE001 - any failure = pending, never a 500
            return _mark_pending(result, name, _error_code(exc))
        result.steps.append(name)
    result.cognito = COGNITO_DONE
    return result


# ── SQL (shared.admin_users / shared.staff_app_grant, migration 247) ─────────────────────────────

SQL_ACTOR = (
    "SELECT id::text AS id, is_active, is_owner, role::text AS role FROM shared.admin_users WHERE id = $1::uuid"
)
SQL_TARGET = (
    "SELECT id::text AS id, username, role::text AS role, email, display_name, is_active, is_owner "
    "FROM shared.admin_users WHERE id = $1::uuid FOR UPDATE"
)
SQL_TARGET_BY_EMAIL = (
    "SELECT id::text AS id, username, role::text AS role, email, display_name, is_active, is_owner "
    "FROM shared.admin_users WHERE lower(email) = $1 FOR UPDATE"
)
SQL_GRANT = (
    "SELECT role FROM shared.staff_app_grant WHERE admin_user_id = $1::uuid AND app = $2 FOR UPDATE"
)
SQL_LOCK_OWNERS = "SELECT id FROM shared.admin_users WHERE is_owner ORDER BY id FOR UPDATE"
SQL_OTHER_ACTIVE_OWNERS = (
    "SELECT count(*) FROM shared.admin_users "
    "WHERE is_owner AND is_active AND role = 'admin' AND id <> $1::uuid"
)
SQL_USERNAME_TAKEN = "SELECT 1 FROM shared.admin_users WHERE username = $1"
SQL_INSERT = (
    "INSERT INTO shared.admin_users (username, password_hash, role, is_active, email, display_name, is_owner) "
    "VALUES ($1, NULL, $2::shared.admin_role, true, $3, $4, false) RETURNING id::text AS id"
)
SQL_SET_ROLE = (
    "UPDATE shared.admin_users SET role = $2::shared.admin_role, updated_at = now() WHERE id = $1::uuid"
)
SQL_SET_ACTIVE = "UPDATE shared.admin_users SET is_active = $2, updated_at = now() WHERE id = $1::uuid"
SQL_UPSERT_GRANT = (
    "INSERT INTO shared.staff_app_grant (admin_user_id, app, role) VALUES ($1::uuid, $2, $3) "
    "ON CONFLICT (admin_user_id, app) DO UPDATE SET role = EXCLUDED.role, updated_at = now()"
)
SQL_DELETE_GRANT = "DELETE FROM shared.staff_app_grant WHERE admin_user_id = $1::uuid AND app = $2"


def _actor_tag(actor_id: str) -> str:
    return f"admin:{actor_id}"


async def _require_owner(conn: Any, actor_id: str) -> None:
    """Lock every owner row (always first, in id order, so concurrent writes never deadlock and two owners can not
    remove each other at the same time), then re-read the actor: active, owner and CIS admin, or 403. The role is
    re-read because the route policy took it from the access token's groups, which keep ``cis-admin`` for up to
    15 minutes after another owner demoted this one."""
    await conn.fetch(SQL_LOCK_OWNERS)
    row = await conn.fetchrow(SQL_ACTOR, actor_id)
    if row is None or not row["is_active"] or not row["is_owner"] or row["role"] != "admin":
        raise StaffDirectoryError(403, "Only a platform owner can change staff accounts", "not_owner")


async def _load_target(conn: Any, staff_id: str) -> Any:
    row = await conn.fetchrow(SQL_TARGET, staff_id)
    if row is None:
        raise StaffDirectoryError(404, "Staff account not found", "not_found")
    if is_breakglass(row["username"], row["email"]):
        raise StaffDirectoryError(403, "The break-glass account is managed only by its runbook, not here",
                                  "breakglass")
    return row


async def _guard_last_owner(conn: Any, target: Any) -> None:
    """409 when ``target`` is the last active owner (call before demoting or disabling it; the owner rows are
    already locked by :func:`_require_owner`)."""
    if target["is_owner"] and target["is_active"] and target["role"] == "admin":
        others = await conn.fetchval(SQL_OTHER_ACTIVE_OWNERS, target["id"])
        if not others:
            raise StaffDirectoryError(409, "This is the last active owner; it cannot be demoted or disabled",
                                      "last_owner")


async def _audit(conn: Any, actor_id: str, action: str, staff_id: str, details: dict[str, Any]) -> None:
    await write_audit_log(conn, tenant_id=None, actor=_actor_tag(actor_id), action=action,
                          resource_type=RESOURCE_TYPE, resource_id=staff_id, details=details)


async def _audit_after(pool: Any, actor_id: str, result: WriteResult) -> None:
    """After the Cognito steps: a pending row (what failed, so the retry is traceable) or, for a sync, the synced
    row. Best effort: the DB change and its own audit row are already committed."""
    if result.pending:
        action = StaffAudit.COGNITO_PENDING
    elif result.operation == "sync":
        action = StaffAudit.COGNITO_SYNCED
    else:
        return
    try:
        await _audit(pool, actor_id, action, result.staff_id, {
            "operation": result.operation, "steps_done": result.steps,
            "failed_step": result.failed_step, "error_code": result.error_code,
        })
    except Exception as exc:  # noqa: BLE001 - never turn a saved write into a 500 over the follow-up row
        logger.error("staff_directory_audit_failed", staff_id=result.staff_id, action=action,
                     error=type(exc).__name__)


async def _finish(pool: Any, actor_id: str, result: WriteResult, steps: list[Step],
                  has_email: bool = True) -> WriteResult:
    staff_identity.clear_cache()   # this task's verified-row cache; other tasks catch up within its 60 s TTL
    if not has_email:
        result.cognito = COGNITO_NOT_APPLICABLE
        return result
    await _run_steps(result, steps)
    await _audit_after(pool, actor_id, result)
    return result


# ── Operations ───────────────────────────────────────────────────────────────────────────────────

async def create_staff(pool: Any, cognito: CognitoDirectory, *, actor_id: str, email: Any, display_name: Any,
                       role: Any, booking_role: Any = None, send_invite: bool = False) -> WriteResult:
    """Insert the row (and Booking grant), then create the Cognito user (SUPPRESS) and add its groups.

    ``send_invite`` (S242): queue the invitation e-mail (one ``staff_invite_email`` job + its
    ``staff.invite_requested`` row) in the same transaction as the INSERT, so a rolled-back create never mails.
    Only a NEW row gets one; SES not configured -> the account is still created, ``invite = not_configured``.

    Re-POSTing the same address with the same roles is a retry: no new row, no new invitation, the Cognito steps
    run again. The same address with different roles is 409 (use the roles route)."""
    email = require_staff_email(email)
    display_name = require_display_name(display_name)
    role = require_cis_role(role)
    booking_role = require_booking_role(booking_role)
    if email in BREAKGLASS_EMAILS:
        raise StaffDirectoryError(403, "The break-glass address cannot be a directory account", "breakglass")

    created = False
    invite = INVITE_NOT_REQUESTED
    invite_job_id: Optional[str] = None
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_owner(conn, actor_id)
            existing = await conn.fetchrow(SQL_TARGET_BY_EMAIL, email)
            if existing is not None:
                if is_breakglass(existing["username"], existing["email"]):
                    raise StaffDirectoryError(403, "The break-glass account is managed only by its runbook",
                                              "breakglass")
                grant = await conn.fetchval(SQL_GRANT, existing["id"], APP_BOOKING)
                if not existing["is_active"] or existing["role"] != role or grant != booking_role:
                    raise StaffDirectoryError(
                        409, "A staff account with this address already exists; change it from its row",
                        "exists")
                staff_id = existing["id"]
                if send_invite:
                    invite = INVITE_EXISTING
            else:
                username = await _free_username(conn, username_base(email))
                try:
                    staff_id = await conn.fetchval(SQL_INSERT, username, role, email, display_name)
                except asyncpg.UniqueViolationError:
                    raise StaffDirectoryError(409, "That address or username was just taken; try again",
                                              "conflict") from None
                if booking_role is not None:
                    await conn.execute(SQL_UPSERT_GRANT, staff_id, APP_BOOKING, booking_role)
                await _audit(conn, actor_id, StaffAudit.CREATED, staff_id, {
                    "email": email, "username": username, "display_name": display_name,
                    "role": role, "booking_role": booking_role, "send_invite": bool(send_invite),
                })
                created = True
                if send_invite:
                    if staff_invite.mail_configured():
                        invite_job_id = await staff_invite.enqueue_invite(
                            conn, staff_id=staff_id, actor_id=actor_id, reason=staff_invite.REASON_CREATED)
                        invite = INVITE_QUEUED
                    else:
                        invite = INVITE_NOT_CONFIGURED
                        logger.warning("staff_invite_not_configured", staff_id=staff_id,
                                       hint="set SES_FROM_ADDRESS on the task definition")

    result = WriteResult(staff_id=staff_id, operation="create", created=created, invite=invite,
                         invite_job_id=invite_job_id)
    wanted = [cis_group(role)] + ([booking_group(booking_role)] if booking_role is not None else [])
    steps: list[Step] = []

    def _create_user() -> None:
        if not cognito.create_user(email):
            # The user already existed in the pool, in an unknown state: make it match the row like an
            # active-row sync (enable, then drop every staff group the row does not grant; the wanted groups
            # are added by the steps already listed, so add-before-remove holds).
            steps.extend(_existing_user_steps(cognito, email, wanted))

    steps.append(("create_user", _create_user))
    steps += _add_group_steps(cognito, email, wanted)
    return await _finish(pool, actor_id, result, steps)


def _add_group_steps(cognito: CognitoDirectory, email: str, groups: list[str]) -> list[Step]:
    return [(f"add_group:{g}", (lambda g=g: cognito.add_to_group(email, g))) for g in groups]


def _remove_other_groups_steps(cognito: CognitoDirectory, email: str, wanted: list[str]) -> list[Step]:
    return [(f"remove_group:{g}", (lambda g=g: cognito.remove_from_group(email, g)))
            for g in CIS_GROUPS + BOOKING_GROUPS if g not in wanted]


def _existing_user_steps(cognito: CognitoDirectory, email: str, wanted: list[str]) -> list[Step]:
    return [("enable_user", lambda: cognito.enable(email))] + _remove_other_groups_steps(cognito, email, wanted)


async def _free_username(conn: Any, base: str) -> str:
    for i in range(1, _USERNAME_SUFFIX_TRIES + 1):
        candidate = base if i == 1 else f"{base}-{i}"
        if candidate in BREAKGLASS_USERNAMES:
            continue
        if not await conn.fetchval(SQL_USERNAME_TAKEN, candidate):
            return candidate
    raise StaffDirectoryError(409, "Could not find a free username for this address", "username_taken")


_UNSET: Any = object()


async def change_roles(pool: Any, cognito: CognitoDirectory, *, actor_id: str, staff_id: str,
                       role: Any = _UNSET, booking_role: Any = _UNSET) -> WriteResult:
    """Set the CIS role and/or the Booking grant (``booking_role=None`` removes it). Cognito: add the new group,
    then remove the old one."""
    if role is _UNSET and booking_role is _UNSET:
        raise StaffDirectoryError(400, "Nothing to change: give a CIS role and/or a Booking role", "empty")
    new_role = require_cis_role(role) if role is not _UNSET else None
    new_booking = require_booking_role(booking_role) if booking_role is not _UNSET else None

    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_owner(conn, actor_id)
            target = await _load_target(conn, staff_id)
            staff_id = target["id"]
            old_role = target["role"]
            old_booking = await conn.fetchval(SQL_GRANT, staff_id, APP_BOOKING)
            changes: dict[str, Any] = {}
            if new_role is not None and new_role != old_role:
                if new_role != "admin":
                    if staff_id == actor_id:
                        raise StaffDirectoryError(400, "You cannot demote yourself", "self_demote")
                    await _guard_last_owner(conn, target)
                await conn.execute(SQL_SET_ROLE, staff_id, new_role)
                changes["role"] = {"before": old_role, "after": new_role}
            if booking_role is not _UNSET and new_booking != old_booking:
                if new_booking is None:
                    await conn.execute(SQL_DELETE_GRANT, staff_id, APP_BOOKING)
                else:
                    await conn.execute(SQL_UPSERT_GRANT, staff_id, APP_BOOKING, new_booking)
                changes["booking_role"] = {"before": old_booking, "after": new_booking}
            if changes:
                await _audit(conn, actor_id, StaffAudit.ROLES_CHANGED, staff_id, changes)

    email = target["email"]
    steps: list[Step] = []
    if new_role is not None:
        steps.append((f"add_group:{cis_group(new_role)}", lambda: cognito.add_to_group(email, cis_group(new_role))))
        if old_role != new_role and old_role in CIS_ROLES:   # AA-878: 'viewer' has no Cognito group
            steps.append((f"remove_group:{cis_group(old_role)}",
                          lambda: cognito.remove_from_group(email, cis_group(old_role))))
    if booking_role is not _UNSET:
        if new_booking is not None:
            steps.append((f"add_group:{booking_group(new_booking)}",
                          lambda: cognito.add_to_group(email, booking_group(new_booking))))
        if old_booking is not None and old_booking != new_booking:
            steps.append((f"remove_group:{booking_group(old_booking)}",
                          lambda: cognito.remove_from_group(email, booking_group(old_booking))))
    result = WriteResult(staff_id=staff_id, operation="roles")
    return await _finish(pool, actor_id, result, steps, has_email=bool(email))


async def disable_staff(pool: Any, cognito: CognitoDirectory, *, actor_id: str, staff_id: str) -> WriteResult:
    """``is_active = false``, then AdminDisableUser, then AdminUserGlobalSignOut (in that order, ADR 0006 §7)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_owner(conn, actor_id)
            target = await _load_target(conn, staff_id)
            staff_id = target["id"]
            if staff_id == actor_id:
                raise StaffDirectoryError(400, "You cannot disable yourself", "self_disable")
            await _guard_last_owner(conn, target)
            if target["is_active"]:
                await conn.execute(SQL_SET_ACTIVE, staff_id, False)
                await _audit(conn, actor_id, StaffAudit.DISABLED, staff_id, {"is_active": {"before": True,
                                                                                            "after": False}})
    email = target["email"]
    steps: list[Step] = _disable_steps(cognito, email)
    return await _finish(pool, actor_id, WriteResult(staff_id=staff_id, operation="disable"), steps,
                         has_email=bool(email))


def _disable_steps(cognito: CognitoDirectory, email: str) -> list[Step]:
    """AdminDisableUser, then AdminUserGlobalSignOut; the sign-out only runs after the disable succeeded (the step
    list stops at the first failure), so it may treat "User is disabled" as done."""
    return [("disable_user", lambda: cognito.disable(email)),
            ("global_sign_out", lambda: cognito.global_sign_out(email, user_disabled=True))]


async def enable_staff(pool: Any, cognito: CognitoDirectory, *, actor_id: str, staff_id: str) -> WriteResult:
    """``is_active = true``, then AdminEnableUser."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_owner(conn, actor_id)
            target = await _load_target(conn, staff_id)
            staff_id = target["id"]
            if not target["is_active"]:
                await conn.execute(SQL_SET_ACTIVE, staff_id, True)
                await _audit(conn, actor_id, StaffAudit.ENABLED, staff_id, {"is_active": {"before": False,
                                                                                           "after": True}})
    email = target["email"]
    steps: list[Step] = [("enable_user", lambda: cognito.enable(email))]
    return await _finish(pool, actor_id, WriteResult(staff_id=staff_id, operation="enable"), steps,
                         has_email=bool(email))


async def sign_out_staff(pool: Any, cognito: CognitoDirectory, *, actor_id: str, staff_id: str) -> WriteResult:
    """"Reset": re-enable the account when it was disabled, then end every session (AdminUserGlobalSignOut).

    AdminEnableUser always runs, and BEFORE the sign-out: the writer may not read the pool's state (AdminGetUser is
    not allowed and counts as MAU), enabling an enabled user is a no-op, and a sign-out on a still-disabled Cognito
    user may be refused, which must not leave the row active and the pool disabled."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_owner(conn, actor_id)
            target = await _load_target(conn, staff_id)
            staff_id = target["id"]
            reenabled = not target["is_active"]
            if reenabled:
                await conn.execute(SQL_SET_ACTIVE, staff_id, True)
            await _audit(conn, actor_id, StaffAudit.SIGNED_OUT, staff_id, {"reenabled": reenabled})
    email = target["email"]
    steps: list[Step] = [("enable_user", lambda: cognito.enable(email)),
                         ("global_sign_out", lambda: cognito.global_sign_out(email))]
    return await _finish(pool, actor_id, WriteResult(staff_id=staff_id, operation="sign_out"), steps,
                         has_email=bool(email))


async def resend_invite(pool: Any, *, actor_id: str, staff_id: str) -> WriteResult:
    """Queue another invitation e-mail for an active account (S242 "Resend invite"). No Cognito call.

    Same guards as the other writes (owner re-read, break-glass 403, 404), plus: the account must be active
    (409) and have a work email (409), the SES sender must be configured (503), and the per-account rate limit
    (api/services/staff_invite.py: 1 per 10 minutes, 5 per 24 h) answers 429 with ``Retry-After``. The rate
    check and the enqueue run in one transaction on the locked row, so two clicks at once queue one job."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_owner(conn, actor_id)
            target = await _load_target(conn, staff_id)
            staff_id = target["id"]
            if not target["is_active"]:
                raise StaffDirectoryError(409, "This account is disabled; enable it before sending an invitation",
                                          "inactive")
            if not target["email"]:
                raise StaffDirectoryError(409, "This account has no work email, so there is nobody to invite",
                                          "no_email")
            if not staff_invite.mail_configured():
                raise StaffDirectoryError(503, "Invitation e-mail is not configured on this server "
                                               "(SES_FROM_ADDRESS is unset); nothing was sent", "not_configured")
            limited = await staff_invite.check_rate_limit(conn, staff_id)
            if limited is not None:
                raise StaffDirectoryError(429, limited.message, "rate_limited",
                                          headers={"Retry-After": str(limited.retry_after_s)})
            job_id = await staff_invite.enqueue_invite(conn, staff_id=staff_id, actor_id=actor_id,
                                                       reason=staff_invite.REASON_RESEND)
    logger.info("staff_invite_queued", staff_id=staff_id, job_id=job_id, reason=staff_invite.REASON_RESEND)
    return WriteResult(staff_id=staff_id, operation="invite", cognito=COGNITO_NOT_APPLICABLE,
                       invite=INVITE_QUEUED, invite_job_id=job_id)


async def sync_staff(pool: Any, cognito: CognitoDirectory, *, actor_id: str, staff_id: str) -> WriteResult:
    """The retry for ``saved_cognito_pending``: make the pool match the row. Idempotent.

    Active row: AdminCreateUser (SUPPRESS; an existing user is fine), AdminEnableUser, add ``cis-<role>`` (and
    ``booking-<role>``), remove every other CIS / Booking staff group. Inactive row: AdminDisableUser, then
    AdminUserGlobalSignOut (no user is created for an inactive row)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            await _require_owner(conn, actor_id)
            target = await _load_target(conn, staff_id)
            staff_id = target["id"]
            booking = await conn.fetchval(SQL_GRANT, staff_id, APP_BOOKING)
    email = target["email"]
    if not email:
        raise StaffDirectoryError(400, "This account has no work email, so it has no Cognito user to sync",
                                  "no_email")
    if not target["is_active"]:
        steps: list[Step] = _disable_steps(cognito, email)
    else:
        wanted = [cis_group(target["role"])] + ([booking_group(booking)] if booking else [])
        steps = [("create_user", lambda: cognito.create_user(email)),
                 ("enable_user", lambda: cognito.enable(email))]
        steps += _add_group_steps(cognito, email, wanted)
        steps += _remove_other_groups_steps(cognito, email, wanted)
    return await _finish(pool, actor_id, WriteResult(staff_id=staff_id, operation="sync"), steps)
