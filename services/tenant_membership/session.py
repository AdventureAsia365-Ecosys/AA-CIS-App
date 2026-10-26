"""Content studio sign-in, invitation and activation on the CIS membership directory (AA-979 P2b, root ADR 0010a).

Three writers / readers of ``shared.tenant_members`` (CIS 276), all on the main pool (cis_app, the only role CIS 276
grants the directory):

- :func:`identity_memberships` — the ACTIVE memberships of one partner-pool identity on agencies that are active
  (``shared.tenants.is_active`` and not offboarded), with the agency name and plan: what ``/auth/tenant-session`` and
  ``/auth/tenant-memberships`` answer from. Keyed by iss AND sub (ADR 0006 decision 9).
- :func:`invite` — a staff invitation (Tenant 360 Members): one ``invited`` row with a fresh ``invitation_id`` and the
  Content studio role CIS decides. CIS stores no e-mail (AA-813 addendum): the address goes once, by the staff relay,
  to the CRM, which sends the AA system mail and owns the claim (``crm.studio_invitation``).
- :func:`activate` — the CRM's M2M call after the person accepted: binds ``(iss, sub)`` to the invited row and makes it
  ``active`` in one transaction. The CIS 276 trigger writes ``membership.activated`` in the same transaction.
  Idempotent for the same identity; another identity, a removed or an unknown invitation are refused.

Every write is one transaction with an ``acp_shared.audit_log`` row next to it (the AA-978 writer's rule); the audit
details never carry the identity (only its 12-hex fingerprint) or an e-mail.
"""

from __future__ import annotations

from typing import Any, Optional

import asyncpg

from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log
from services.tenant_membership.writer import ROLES, fingerprint, staff_row

ACTIVE_FOR_IDENTITY_SQL = """
    SELECT m.membership_id, m.tenant_id, m.content_studio_role, t.name, t.plan_tier
      FROM shared.tenant_members m
      JOIN shared.tenants t ON t.tenant_id = m.tenant_id
     WHERE m.cognito_iss = $1 AND m.cognito_sub = $2 AND m.status = 'active'
       AND t.is_active AND t.cancelled_at IS NULL
     ORDER BY t.name, m.tenant_id
"""
TENANT_ACTIVE_SQL = "SELECT is_active, cancelled_at FROM shared.tenants WHERE tenant_id = $1::uuid"
INVITE_SQL = """
    INSERT INTO shared.tenant_members (tenant_id, content_studio_role, status, invitation_id, created_by)
    VALUES ($1::uuid, $2, 'invited', gen_random_uuid(), $3)
 RETURNING membership_id, cognito_iss, cognito_sub, content_studio_role, status, invitation_id, created_at,
           created_by, updated_at, removed_at, removed_by
"""
INVITATION_LOCK_SQL = """
    SELECT membership_id, tenant_id, cognito_iss, cognito_sub, content_studio_role, status, invitation_id, created_at,
           created_by, updated_at, removed_at, removed_by
      FROM shared.tenant_members
     WHERE invitation_id = $1::uuid
       FOR UPDATE
"""
ACTIVATE_SQL = """
    UPDATE shared.tenant_members
       SET status = 'active', cognito_iss = $2, cognito_sub = $3, updated_at = now()
     WHERE membership_id = $1::uuid AND status = 'invited'
 RETURNING membership_id, tenant_id, cognito_iss, cognito_sub, content_studio_role, status, invitation_id, created_at,
           created_by, updated_at, removed_at, removed_by
"""
MEMBERSHIP_OF_INVITATION_SQL = """
    SELECT membership_id, tenant_id, invitation_id FROM shared.tenant_members
     WHERE tenant_id = $1::uuid AND membership_id = $2::uuid
"""


class InvitationNotFound(Exception):  # noqa: N818 - reads as an outcome
    """No membership carries this invitation id (404)."""


class InvitationClosed(Exception):  # noqa: N818 - reads as an outcome
    """The invitation's membership is removed, or active for another identity (409). ``code`` says which."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TenantInactive(Exception):  # noqa: N818 - reads as an outcome
    """The agency is locked or offboarded: no invitation is created for it (409)."""


def membership_choice(row: Any) -> dict:
    """One entry of the chooser (``GET /auth/tenant-memberships``): ids, name and role, never the identity."""
    return {
        "tenant_id": str(row["tenant_id"]),
        "tenant_name": row["name"],
        "content_studio_role": row["content_studio_role"],
        "membership_id": str(row["membership_id"]),
    }


async def identity_memberships(conn: Any, iss: str, sub: str) -> list[Any]:
    """Active memberships of the identity on active agencies (rows with tenant name and plan tier)."""
    return list(await conn.fetch(ACTIVE_FOR_IDENTITY_SQL, iss, sub))


async def invite(conn: Any, tenant_id: str, role: str, actor: str) -> dict:
    """A new ``invited`` row (the staff row shape of the AA-978 writer, which includes ``invitation_id``)."""
    if role not in ROLES:
        raise ValueError(f"content_studio_role must be one of {', '.join(ROLES)}")
    async with conn.transaction():
        tenant = await conn.fetchrow(TENANT_ACTIVE_SQL, tenant_id)
        if tenant is None or not tenant["is_active"] or tenant["cancelled_at"] is not None:
            raise TenantInactive(tenant_id)
        row = await conn.fetchrow(INVITE_SQL, tenant_id, role, actor[:64])
        await write_audit_log(
            conn, tenant_id=tenant_id, actor=actor, action=AdminTenantAuditAction.MEMBER_INVITED,
            resource_type="tenant_membership", resource_id=str(row["membership_id"]),
            details={"after": {"status": "invited", "content_studio_role": role,
                               "invitation_id": str(row["invitation_id"])}},
        )
    return staff_row(row)


async def invitation_of(conn: Any, tenant_id: str, membership_id: str) -> Optional[Any]:
    """``(membership_id, tenant_id, invitation_id)`` of one membership of the tenant, or None."""
    return await conn.fetchrow(MEMBERSHIP_OF_INVITATION_SQL, tenant_id, membership_id)


async def activate(conn: Any, invitation_id: str, iss: str, sub: str, actor: str) -> tuple[Any, bool]:
    """Bind ``(iss, sub)`` to the invited membership of ``invitation_id``. Returns (row, changed).

    - invited -> active with that identity (changed = True), audit ``tenant.member_activated``;
    - already active with the SAME identity -> (row, False): the CRM's retry after a lost answer is a no-op;
    - active with another identity -> :class:`InvitationClosed` ``identity_mismatch``;
    - removed -> :class:`InvitationClosed` ``membership_removed``;
    - no row -> :class:`InvitationNotFound`.
    A second live membership of the same identity in the same agency (unique index ``tenant_members_live_identity_key``)
    is :class:`InvitationClosed` ``already_member``.
    """
    async with conn.transaction():
        row = await conn.fetchrow(INVITATION_LOCK_SQL, invitation_id)
        if row is None:
            raise InvitationNotFound(invitation_id)
        if row["status"] == "removed":
            raise InvitationClosed("membership_removed")
        if row["status"] == "active":
            if (row["cognito_iss"], row["cognito_sub"]) == (iss, sub):
                return row, False
            raise InvitationClosed("identity_mismatch")
        try:
            after = await conn.fetchrow(ACTIVATE_SQL, row["membership_id"], iss, sub)
        except asyncpg.UniqueViolationError as exc:
            raise InvitationClosed("already_member") from exc
        await write_audit_log(
            conn, tenant_id=str(row["tenant_id"]), actor=actor, action=AdminTenantAuditAction.MEMBER_ACTIVATED,
            resource_type="tenant_membership", resource_id=str(row["membership_id"]),
            details={"before": {"status": "invited"},
                     "after": {"status": "active", "identity_fingerprint": fingerprint(iss, sub)}},
        )
    return after, True
