"""AA-816 (S242 plan step C7) — staff directory reconcile: WORKFORCE pool vs shared.admin_users.

Report only. It reads the WORKFORCE pool with the two calls the CIS task role may make for reading
(ListUsers, ListUsersInGroup; AA-CIS-Infra identity_iam.tf, plan step I3) and the directory
(shared.admin_users + shared.staff_app_grant), and returns what disagrees. It never writes Cognito or the
DB: no Admin* call is made here, and the SQL is two SELECTs.

What it reports (the `result` of a `staff_directory_reconcile` job, services/jobs/staff_reconcile_job.py):

  (a) cognito_without_row      Cognito users that no ACTIVE directory row stands for (except break-glass):
                               no row at all, an inactive row while Cognito still has the user enabled, or a
                               row with the same email that is bound to a DIFFERENT sub (a user re-created in
                               Cognito: sign-in links by email only while cognito_sub is NULL, so that person
                               is locked out until the row is fixed).
  (b) rows_unknown_to_cognito  active rows whose (cognito_iss, cognito_sub) Cognito does not know: the sub is
                               not in the pool, or the issuer is another pool's.
  (c) group_drift              for each active row matched to a Cognito user: the CIS role vs the highest
                               cis-* group (the C3 sign-in rule, staff_identity.role_from_groups), the Booking
                               grant (staff_app_grant app='booking') vs the booking-* groups, and an active row
                               whose Cognito user is disabled.
  (d) status_findings          Cognito users (except break-glass) whose UserStatus is not CONFIRMED.
                               FORCE_CHANGE_PASSWORD = created, never signed in (the directory's create flow
                               leaves it there until the first email-code sign-in); RESET_REQUIRED = an admin
                               forced a password reset, which no staff flow does: a residual-risk signal
                               (plan I3: the task role cannot set passwords, a console admin can).
  (e) readiness                cutover readiness (ADR 0006 cutover precondition): active rows missing an
                               email or a cognito_sub, count + usernames. `ready` only when both are empty.
  breakglass                   the break-glass user: present, CONFIRMED, disabled at rest, in cis-admin and
                               booking-admin. Each deviation is an issue.

ListUsers cannot tell whether a user has a password (no attribute says so, and AdminGetUser is denied), so
(d) is status based; `notes` in the result says so.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from api.core.identity_config import workforce_issuer
from api.core.staff_identity import GROUP_ROLES, role_from_groups

# ── configuration ────────────────────────────────────────────────────────────────────────────────

# D1 (S242): the break-glass account is the `admin` row, admin@adventure.asia. Code constant with an env
# override, like the issuer / client ids (api/core/identity_config.py), so no task-definition variable.
DEV_BREAKGLASS_EMAIL = "admin@adventure.asia"
ENV_BREAKGLASS_EMAIL = "COGNITO_BREAKGLASS_EMAIL"
# ADR 0006 "Break-glass": the account is in cis-admin and booking-admin.
BREAKGLASS_GROUPS: tuple[str, ...] = ("cis-admin", "booking-admin")

CIS_GROUPS: tuple[str, ...] = tuple(g for g, _ in GROUP_ROLES)
BOOKING_GROUP_ROLES: tuple[tuple[str, str], ...] = (
    ("booking-admin", "admin"),
    ("booking-sales", "sales"),
    ("booking-contracting", "contracting"),
    ("booking-content_creator", "content_creator"),
)
BOOKING_GROUPS: tuple[str, ...] = tuple(g for g, _ in BOOKING_GROUP_ROLES)
STAFF_GROUPS: tuple[str, ...] = CIS_GROUPS + BOOKING_GROUPS

PAGE_LIMIT = 60          # ListUsers / ListUsersInGroup maximum page size
MAX_PAGES = 100          # 6 000 users per listing: far above a staff pool; stops a token loop
CONFIRMED = "CONFIRMED"

ROWS_SQL = """
    SELECT id::text AS id, username, role::text AS role, is_active, email, cognito_iss, cognito_sub
      FROM shared.admin_users
     ORDER BY lower(username)
"""
GRANTS_SQL = "SELECT admin_user_id::text AS admin_user_id, app, role FROM shared.staff_app_grant"

NOTES = (
    "ListUsers does not say whether a user has a password; (d) flags any status other than CONFIRMED.",
    "Report only: nothing in Cognito or shared.admin_users was changed.",
)


def breakglass_email() -> str:
    value = (os.environ.get(ENV_BREAKGLASS_EMAIL) or "").strip().lower()
    return value or DEV_BREAKGLASS_EMAIL


def pool_from_issuer(issuer: str) -> tuple[str, str]:
    """(user pool id, region) from a Cognito issuer URL https://cognito-idp.<region>.amazonaws.com/<pool id>."""
    pool_id = issuer.rstrip("/").rsplit("/", 1)[-1]
    region, sep, _ = pool_id.partition("_")
    if not sep or not region:
        raise ValueError(f"not a Cognito issuer: {issuer!r}")
    return pool_id, region


# ── Cognito reads (sync boto3; the job runs them in a thread) ────────────────────────────────────

class CognitoReadError(Exception):
    """A listing failed in a way a retry can not fix (access denied, pool missing, bad request)."""


_PERMANENT_CODES = frozenset({
    "AccessDeniedException", "NotAuthorizedException", "ResourceNotFoundException",
    "InvalidParameterException", "UnrecognizedClientException",
})


def _error_code(exc: Exception) -> Optional[str]:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return (response.get("Error") or {}).get("Code")
    return None


def cognito_client(region: str):
    import boto3
    from botocore.config import Config

    return boto3.client("cognito-idp", region_name=region, config=Config(
        connect_timeout=5, read_timeout=10, retries={"max_attempts": 3, "mode": "standard"}))


def list_users(client, pool_id: str) -> list[dict]:
    """Every user of the pool (ListUsers, paginated by PaginationToken)."""
    users: list[dict] = []
    token: Optional[str] = None
    for _ in range(MAX_PAGES):
        kwargs: dict[str, Any] = {"UserPoolId": pool_id, "Limit": PAGE_LIMIT}
        if token:
            kwargs["PaginationToken"] = token
        page = client.list_users(**kwargs)
        users.extend(page.get("Users") or [])
        token = page.get("PaginationToken")
        if not token:
            return users
    raise CognitoReadError(f"ListUsers did not end after {MAX_PAGES} pages")


def list_group(client, pool_id: str, group: str) -> Optional[list[dict]]:
    """The members of one group (ListUsersInGroup, paginated by NextToken); None when the group is missing."""
    users: list[dict] = []
    token: Optional[str] = None
    for _ in range(MAX_PAGES):
        kwargs: dict[str, Any] = {"UserPoolId": pool_id, "GroupName": group, "Limit": PAGE_LIMIT}
        if token:
            kwargs["NextToken"] = token
        try:
            page = client.list_users_in_group(**kwargs)
        except Exception as exc:  # botocore ClientError; a missing group is a finding, not a failure
            if _error_code(exc) == "ResourceNotFoundException":
                return None
            raise
        users.extend(page.get("Users") or [])
        token = page.get("NextToken")
        if not token:
            return users
    raise CognitoReadError(f"ListUsersInGroup {group} did not end after {MAX_PAGES} pages")


def fetch_pool(client, pool_id: str, groups: Iterable[str] = STAFF_GROUPS) -> dict:
    """{"users": [...], "groups": {group: [users] | None}}. Permanent errors become CognitoReadError."""
    try:
        users = list_users(client, pool_id)
        members = {g: list_group(client, pool_id, g) for g in groups}
    except CognitoReadError:
        raise
    except Exception as exc:
        code = _error_code(exc)
        if code in _PERMANENT_CODES:
            raise CognitoReadError(f"{code}: {str(exc)[:300]}") from exc
        raise
    return {"users": users, "groups": members}


# ── comparison (pure) ────────────────────────────────────────────────────────────────────────────

@dataclass
class CognitoUser:
    username: str
    sub: Optional[str]
    email: Optional[str]
    status: str
    enabled: bool
    groups: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return self.sub or self.username


def _attrs(user: dict) -> dict[str, str]:
    return {a.get("Name"): a.get("Value") for a in (user.get("Attributes") or []) if a.get("Name")}


def _norm_email(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip().lower()
    return value or None


def parse_users(raw_users: list[dict], raw_groups: dict[str, Optional[list[dict]]]) -> dict[str, CognitoUser]:
    """Cognito users keyed by sub (Username when a user has no sub attribute), each with its staff groups."""
    users: dict[str, CognitoUser] = {}
    by_username: dict[str, CognitoUser] = {}
    for raw in raw_users:
        attrs = _attrs(raw)
        username = raw.get("Username") or ""
        email = _norm_email(attrs.get("email")) or (_norm_email(username) if "@" in username else None)
        u = CognitoUser(username=username, sub=attrs.get("sub"), email=email,
                        status=raw.get("UserStatus") or "UNKNOWN", enabled=bool(raw.get("Enabled", True)))
        users[u.key] = u
        by_username[username] = u
    for group, members in raw_groups.items():
        for raw in members or ():
            sub = _attrs(raw).get("sub")
            u = users.get(sub) if sub else None
            if u is None:
                u = by_username.get(raw.get("Username") or "")
            if u is not None:   # a member ListUsers did not return (created in between): next run sees it
                u.groups.add(group)
    return users


def _booking_role_from_groups(groups: Iterable[str]) -> list[str]:
    held = set(groups)
    return [role for group, role in BOOKING_GROUP_ROLES if group in held]


def _sorted_groups(groups: Iterable[str]) -> list[str]:
    order = {g: i for i, g in enumerate(STAFF_GROUPS)}
    return sorted(groups, key=lambda g: (order.get(g, len(order)), g))


def reconcile(raw_users: list[dict], raw_groups: dict[str, Optional[list[dict]]], rows: list[dict],
              grants: list[dict], *, issuer: str, breakglass: str) -> dict:
    """The report. Pure: no I/O, so every category is unit-tested with plain dicts."""
    users = parse_users(raw_users, raw_groups)
    breakglass = breakglass.lower()
    booking_grant = {g["admin_user_id"]: g["role"] for g in grants if g.get("app") == "booking"}

    by_identity = {(r.get("cognito_iss"), r.get("cognito_sub")): r for r in rows if r.get("cognito_sub")}
    by_email = {_norm_email(r.get("email")): r for r in rows if _norm_email(r.get("email"))}
    active_rows = [r for r in rows if r.get("is_active")]

    def is_breakglass(u: CognitoUser) -> bool:
        return u.email == breakglass or u.username.lower() == breakglass

    without_row: list[dict] = []
    drift: list[dict] = []
    status_findings: list[dict] = []

    for u in sorted(users.values(), key=lambda x: (x.email or x.username)):
        if is_breakglass(u):
            continue
        if u.status != CONFIRMED:
            status_findings.append({"email": u.email, "username": u.username, "status": u.status,
                                    "enabled": u.enabled})
        row = by_identity.get((issuer, u.sub)) if u.sub else None
        reason = None
        if row is None:
            candidate = by_email.get(u.email) if u.email else None
            if candidate is None:
                reason = "no_row"
            elif candidate.get("cognito_sub"):
                # Same email, bound to another identity: the row was linked to an earlier Cognito user.
                row, reason = candidate, "row_bound_to_other_sub"
            else:
                row = candidate   # not linked yet: links itself on the first sign-in (readiness reports it)
        if reason is None and row is not None and not row.get("is_active"):
            if u.enabled:
                reason = "row_inactive"
            else:
                continue          # disabled in Cognito and inactive in the directory: consistent
        if reason is not None:
            without_row.append({"email": u.email, "username": u.username, "enabled": u.enabled,
                                "status": u.status, "groups": _sorted_groups(u.groups), "reason": reason,
                                "row_username": row.get("username") if row else None})
            continue
        drift.extend(_row_drift(row, u, booking_grant.get(row["id"])))

    unknown: list[dict] = []
    for r in active_rows:
        sub, iss = r.get("cognito_sub"), r.get("cognito_iss")
        if not sub:
            continue
        if iss != issuer:
            unknown.append({"username": r["username"], "email": r.get("email"), "reason": "other_issuer",
                            "cognito_iss": iss})
        elif sub not in users:
            unknown.append({"username": r["username"], "email": r.get("email"), "reason": "unknown_sub"})

    missing_email = sorted(r["username"] for r in active_rows if not _norm_email(r.get("email")))
    missing_sub = sorted(r["username"] for r in active_rows if not r.get("cognito_sub"))
    readiness = {
        "ready": not missing_email and not missing_sub,
        "active_rows": len(active_rows),
        "missing_email_count": len(missing_email),
        "missing_email": missing_email,
        "missing_cognito_sub_count": len(missing_sub),
        "missing_cognito_sub": missing_sub,
    }

    bg = _breakglass_block(users, breakglass, by_email.get(breakglass), issuer)
    groups_missing = sorted(g for g, members in raw_groups.items() if members is None)
    counts = {
        "cognito_users": len(users),
        "rows": len(rows),
        "active_rows": len(active_rows),
        "cognito_without_row": len(without_row),
        "rows_unknown_to_cognito": len(unknown),
        "group_drift": len(drift),
        "status_findings": len(status_findings),
        "missing_email": len(missing_email),
        "missing_cognito_sub": len(missing_sub),
        "breakglass_issues": len(bg["issues"]),
        "groups_missing": len(groups_missing),
    }
    counts["drift_total"] = (counts["cognito_without_row"] + counts["rows_unknown_to_cognito"]
                             + counts["group_drift"] + counts["status_findings"] + counts["breakglass_issues"]
                             + counts["groups_missing"])
    return {
        "issuer": issuer,
        "counts": counts,
        "drift_total": counts["drift_total"],
        "cognito_without_row": without_row,
        "rows_unknown_to_cognito": unknown,
        "group_drift": drift,
        "status_findings": status_findings,
        "readiness": readiness,
        "breakglass": bg,
        "groups": {g: (None if members is None else len(members)) for g, members in raw_groups.items()},
        "groups_missing": groups_missing,
        "notes": list(NOTES),
    }


def _row_drift(row: dict, u: CognitoUser, grant_role: Optional[str]) -> list[dict]:
    out: list[dict] = []
    base = {"username": row["username"], "email": u.email or row.get("email")}
    cis_groups = [g for g in _sorted_groups(u.groups) if g in CIS_GROUPS]
    group_role = role_from_groups(u.groups)
    if group_role != row.get("role") or len(cis_groups) > 1:
        out.append({**base, "kind": "cis_role", "directory": row.get("role"), "cognito": group_role,
                    "cognito_groups": cis_groups})
    booking_groups = [g for g in _sorted_groups(u.groups) if g in BOOKING_GROUPS]
    booking_roles = _booking_role_from_groups(u.groups)
    if booking_roles != ([grant_role] if grant_role else []):
        out.append({**base, "kind": "booking_role", "directory": grant_role,
                    "cognito": booking_roles[0] if len(booking_roles) == 1 else (booking_roles or None),
                    "cognito_groups": booking_groups})
    if not u.enabled:
        out.append({**base, "kind": "cognito_disabled", "directory": "active", "cognito": "disabled",
                    "cognito_groups": _sorted_groups(u.groups)})
    return out


def _breakglass_block(users: dict[str, CognitoUser], email: str, row: Optional[dict], issuer: str) -> dict:
    u = next((x for x in users.values() if x.email == email or x.username.lower() == email), None)
    block: dict[str, Any] = {"email": email, "present": u is not None, "has_row": row is not None,
                             "row_username": row.get("username") if row else None, "issues": []}
    if u is None:
        block["issues"].append("break-glass user not found in the WORKFORCE pool")
        return block
    missing = [g for g in BREAKGLASS_GROUPS if g not in u.groups]
    block.update(status=u.status, enabled=u.enabled, groups=_sorted_groups(u.groups), missing_groups=missing,
                 linked=bool(row and row.get("cognito_iss") == issuer and row.get("cognito_sub") == u.sub))
    if u.status != CONFIRMED:
        block["issues"].append(f"status is {u.status}, not CONFIRMED (password + TOTP not set up)")
    if u.enabled:
        block["issues"].append("enabled: break-glass should stay disabled at rest")
    if missing:
        block["issues"].append(f"missing group(s): {', '.join(missing)}")
    return block


def summary_fields(report: dict) -> dict:
    """The fields of the `staff_reconcile_summary` log line: counts only, no email or username."""
    return {**report["counts"], "ready": report["readiness"]["ready"]}


async def load_directory(pool) -> tuple[list[dict], list[dict]]:
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(ROWS_SQL)]
        grants = [dict(r) for r in await conn.fetch(GRANTS_SQL)]
    return rows, grants


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
