"""The tenant's own view of the CIS membership directory (AA-978 follow-up): the portal's Account › Members page.

``GET /v1/account/members`` (``api/routers/v1_account_members.py``) lists the agency's current memberships of the
Content studio: invited and active rows of ``shared.tenant_members`` (CIS 276). Removed rows are history the staff
Tenant 360 page shows; the tenant sees who belongs now.

What a tenant sees of a row: the membership id, a short identity reference (the same 12-hex fingerprint the staff list
shows, never the raw ``(iss, sub)``: enough to tell two members apart, not to sign in as anyone; null while invited,
the identity binds at activation), the Content studio role, the status and two dates. Never ``created_by`` /
``removed_by`` (staff actor ids), the invitation id or a CRM role (the table has no column for one). The table has no
e-mail or name column (CIS 276, AA-813 addendum), so the page cannot show either.

The read runs on the main pool (role cis_app): CIS 276 grants these tables to cis_app only (no RLS, nothing for
``aa_app_user``, the AA-859 invariant), the ``/v1/billing`` pattern. The tenant id comes only from the verified tenant
token and is bound in the one query; there is no tenant parameter to tamper with.
"""

from __future__ import annotations

from typing import Any

from services.tenant_membership.reader import iso_utc
from services.tenant_membership.writer import fingerprint

TENANT_MEMBERS_SQL = """
    SELECT membership_id, cognito_iss, cognito_sub, content_studio_role, status, created_at, updated_at
      FROM shared.tenant_members
     WHERE tenant_id = $1::uuid AND status <> 'removed'
     ORDER BY (status = 'invited'), CASE content_studio_role WHEN 'owner' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END,
              created_at, membership_id
"""


def tenant_row(row: Any) -> dict:
    return {
        "membership_id": str(row["membership_id"]),
        "member_ref": fingerprint(row["cognito_iss"], row["cognito_sub"]),
        "role": row["content_studio_role"],
        "status": row["status"],
        "added_at": iso_utc(row["created_at"]),
        "updated_at": iso_utc(row["updated_at"]),
    }


async def list_for_tenant(conn: Any, tenant_id: str) -> dict:
    """The tenant's invited and active memberships, active first (owners, editors, viewers), then invitations."""
    rows = [tenant_row(r) for r in await conn.fetch(TENANT_MEMBERS_SQL, tenant_id)]
    return {
        "data": rows,
        "total": len(rows),
        "active": sum(1 for r in rows if r["status"] == "active"),
        "invited": sum(1 for r in rows if r["status"] == "invited"),
    }
