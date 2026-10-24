"""Staff writes on the CIS membership directory (AA-978, Tenant 360 Members): list, change the Content studio role,
remove.

CIS can change only the Content studio role (owner / editor / viewer) and remove a membership; no route or column
carries a CRM role (the CRM owns those). Invitation and activation of an identity arrive with AA-979 (partner-pool
re-enrolment). Each write is one transaction on the main pool (cis_app): the row is locked, changed, and an
``acp_shared.audit_log`` row is written next to it. The CIS 276 trigger writes the feed event in the same
transaction, so the CRM sees the change (a removal disables the member and signs them out of the CRM within a
minute) exactly when it commits.

The staff list never returns the raw identity: (iss, sub) is shown as a short fingerprint (sha256, 12 hex), enough
to tell two members apart and to match a support case, not to sign in as anyone.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from services.acp_shared.audit_log import AdminTenantAuditAction, write_audit_log
from services.tenant_membership.reader import iso_utc

ROLES = ("owner", "editor", "viewer")

LIST_SQL = """
    SELECT membership_id, cognito_iss, cognito_sub, content_studio_role, status, invitation_id, created_at,
           created_by, updated_at, removed_at, removed_by
      FROM shared.tenant_members
     WHERE tenant_id = $1::uuid
     ORDER BY (status = 'removed'), created_at, membership_id
"""
LOCK_SQL = """
    SELECT membership_id, cognito_iss, cognito_sub, content_studio_role, status, invitation_id, created_at,
           created_by, updated_at, removed_at, removed_by
      FROM shared.tenant_members
     WHERE tenant_id = $1::uuid AND membership_id = $2::uuid
       FOR UPDATE
"""
ROLE_SQL = """
    UPDATE shared.tenant_members SET content_studio_role = $3, updated_at = now()
     WHERE tenant_id = $1::uuid AND membership_id = $2::uuid
 RETURNING membership_id, cognito_iss, cognito_sub, content_studio_role, status, invitation_id, created_at,
           created_by, updated_at, removed_at, removed_by
"""
REMOVE_SQL = """
    UPDATE shared.tenant_members SET status = 'removed', removed_at = now(), removed_by = $3, updated_at = now()
     WHERE tenant_id = $1::uuid AND membership_id = $2::uuid
 RETURNING membership_id, cognito_iss, cognito_sub, content_studio_role, status, invitation_id, created_at,
           created_by, updated_at, removed_at, removed_by
"""


class MemberNotFound(Exception):  # noqa: N818 - reads as an outcome
    """No membership with this id in this tenant (404)."""


class MemberRemoved(Exception):  # noqa: N818 - reads as an outcome
    """The membership is removed; a removed membership never changes again (409)."""


def fingerprint(iss: Optional[str], sub: Optional[str]) -> Optional[str]:
    if iss is None or sub is None:
        return None
    return hashlib.sha256(f"{iss}|{sub}".encode("utf-8")).hexdigest()[:12]


def staff_row(row: Any) -> dict:
    return {
        "membership_id": str(row["membership_id"]),
        "identity_fingerprint": fingerprint(row["cognito_iss"], row["cognito_sub"]),
        "content_studio_role": row["content_studio_role"],
        "status": row["status"],
        "invitation_id": str(row["invitation_id"]) if row["invitation_id"] is not None else None,
        "created_at": iso_utc(row["created_at"]),
        "created_by": row["created_by"],
        "updated_at": iso_utc(row["updated_at"]),
        "removed_at": iso_utc(row["removed_at"]) if row["removed_at"] is not None else None,
        "removed_by": row["removed_by"],
    }


async def list_members(conn: Any, tenant_id: str) -> list[dict]:
    """Every membership of the tenant, removed ones last."""
    return [staff_row(r) for r in await conn.fetch(LIST_SQL, tenant_id)]


async def _locked(conn: Any, tenant_id: str, membership_id: str) -> Any:
    row = await conn.fetchrow(LOCK_SQL, tenant_id, membership_id)
    if row is None:
        raise MemberNotFound(membership_id)
    if row["status"] == "removed":
        raise MemberRemoved(membership_id)
    return row


async def change_role(conn: Any, tenant_id: str, membership_id: str, role: str, actor: str) -> tuple[dict, bool]:
    """Set the Content studio role. Returns (row, changed); the same role is a no-op (no audit row, no event)."""
    if role not in ROLES:
        raise ValueError(f"content_studio_role must be one of {', '.join(ROLES)}")
    async with conn.transaction():
        before = await _locked(conn, tenant_id, membership_id)
        if before["content_studio_role"] == role:
            return staff_row(before), False
        after = await conn.fetchrow(ROLE_SQL, tenant_id, membership_id, role)
        await write_audit_log(
            conn, tenant_id=tenant_id, actor=actor, action=AdminTenantAuditAction.MEMBER_ROLE_CHANGED,
            resource_type="tenant_membership", resource_id=membership_id,
            details={"before": {"content_studio_role": before["content_studio_role"]},
                     "after": {"content_studio_role": role}},
        )
    return staff_row(after), True


async def remove(conn: Any, tenant_id: str, membership_id: str, actor: str) -> tuple[dict, bool]:
    """Remove the membership (final). Returns (row, changed); removing a removed membership is a no-op."""
    async with conn.transaction():
        row = await conn.fetchrow(LOCK_SQL, tenant_id, membership_id)
        if row is None:
            raise MemberNotFound(membership_id)
        if row["status"] == "removed":
            return staff_row(row), False
        after = await conn.fetchrow(REMOVE_SQL, tenant_id, membership_id, actor[:64])
        await write_audit_log(
            conn, tenant_id=tenant_id, actor=actor, action=AdminTenantAuditAction.MEMBER_REMOVED,
            resource_type="tenant_membership", resource_id=membership_id,
            details={"before": {"status": row["status"], "content_studio_role": row["content_studio_role"]}},
        )
    return staff_row(after), True
