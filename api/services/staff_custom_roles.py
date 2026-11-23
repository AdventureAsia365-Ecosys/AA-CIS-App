"""AA-1155 (spec task T9, root ADR 0006a A14) — the staff directory's custom roles and the business-owner marker.

The writes stay in :mod:`api.services.staff_directory` (DB first, then Cognito, owners only, audited); this module holds
what custom roles add to them (api/core/staff_roles.py reads the roles):

- The CIS role may be a CIS custom role code (``custom_<name>``, an active ``shared.staff_role`` row with
  capabilities): the row then holds ``custom_role`` = the code and ``role`` = the role's ``based_on`` (never admin);
  its group is ``cis-custom``. A built-in role clears ``custom_role``. Unknown, archived or empty: 400 ``custom_role``.
- The Booking role may be a Booking custom role code (an active role of ``booking.v_staff_role_capability``, bk-062):
  ``staff_app_grant.role`` = the code, group ``booking-custom``. Unknown or archived: 400 ``booking_role``; the view
  unreadable: 503 ``booking_roles_unavailable``.
- ``cis-custom`` / ``booking-custom`` are staff groups too (:data:`CIS_EXTRA`, :data:`BK_EXTRA`): Sync and
  create-over-an-existing-user put a custom holder in them and remove them from everyone else; two custom roles share
  the group, so a change between them never removes it (:func:`drops_group`).
- A10g (``e2e-custom-maker@``, Dev only): its CIS role is a custom role based on content_lead (:func:`e2e_role_ok`).
- The business-owner marker (``admin_users.is_business_owner``, CIS 462, one person): :func:`set_business_owner`,
  owners only (the directory's owner re-read), audited before and after (``staff.business_owner_changed``); never an
  E2E address, never an inactive account, never set or cleared by the A10h E2E owner (403 ``e2e_owner_scope``); a
  second holder is 409 ``business_owner_taken`` (clear the first). No
  Cognito step: Booking reads the marker from the contract view shared.v_staff_booking_grant.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

import asyncpg

from api.core import e2e_staff, staff_identity, staff_roles

CIS_EXTRA: tuple[str, ...] = (staff_roles.CIS_CUSTOM_GROUP,)
BK_EXTRA: tuple[str, ...] = (staff_roles.BOOKING_CUSTOM_GROUP,)
# The CIS role an E2E address may hold when it is "a custom role" (A10g).
E2E_ANY_CUSTOM_ROLE = "custom_*"
BUSINESS_OWNER_CHANGED = "staff.business_owner_changed"

SQL_BUSINESS_OWNER_HOLDER = "SELECT id::text AS id FROM shared.admin_users WHERE is_business_owner AND id <> $1::uuid"
SQL_SET_BUSINESS_OWNER = "UPDATE shared.admin_users SET is_business_owner = $2, updated_at = now() WHERE id = $1::uuid"


def _refused(status: int, detail: str, code: str) -> Exception:
    from api.services.staff_directory import StaffDirectoryError   # noqa: PLC0415 - staff_directory imports this module

    return StaffDirectoryError(status, detail, code)


def cis_code(row: Any) -> str:
    """The CIS role a directory row shows and grants: its custom role when it has one, else ``role``."""
    custom = row.get("custom_role") if hasattr(row, "get") else None
    return custom or row["role"]


async def resolve_cis_custom(conn: Any, role: str) -> tuple[str, Optional[str]]:
    """``(admin_users.role, admin_users.custom_role)`` for a CIS role code: a built-in role is itself and no custom
    role; a custom code must be an active role with capabilities (400 ``custom_role``) and keeps its ``based_on``."""
    if not staff_roles.is_custom_code(role):
        return role, None
    grant = await staff_roles.load_cis_grant(conn, role)
    if not grant.ok or grant.role is None:
        raise _refused(400, "This custom CIS role does not exist, is archived or has no permission", "custom_role")
    return grant.role.based_on, role


async def check_booking_custom(conn: Any, role: Optional[str]) -> None:
    """A Booking custom role code must be an active role of booking.v_staff_role_capability (400), and the view must
    be readable (503 ``booking_roles_unavailable``). No-op for a built-in Booking role or none."""
    if role is None or not staff_roles.is_custom_code(role):
        return
    grant = await staff_roles.load_booking_role(conn, role)
    if grant.state == staff_roles.STATE_UNREADABLE:
        raise _refused(503, "Booking custom roles cannot be read here (the Booking role view bk-062 is not readable); "
                            "nothing was changed", "booking_roles_unavailable")
    if not grant.ok:
        raise _refused(400, "This custom Booking role does not exist or is archived", "booking_role")


async def check_new_roles(conn: Any, new_role: Optional[str], old_role: Optional[str], new_booking: Optional[str],
                          old_booking: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """For a write: ``(admin_users.role, custom_role, based_on of a custom role)`` of the new CIS role (all None when
    the CIS role does not change), after checking that a changed CIS or Booking custom code exists and is active."""
    db_role = custom = based_on = None
    if new_role is not None and new_role != old_role:
        db_role, custom = await resolve_cis_custom(conn, new_role)
        based_on = db_role if custom else None
    if new_booking != old_booking:
        await check_booking_custom(conn, new_booking)
    return db_role, custom, based_on


def drops_group(old: Optional[str], new: Optional[str], group_of: Callable[[str], str],
                known: Optional[Iterable[str]] = None) -> bool:
    """A role change must remove the old role's group: the role changed, the old role has a group (a built-in role in
    ``known`` when given, or any custom code) and the new role does not share it (two custom roles share one)."""
    if old is None or old == new:
        return False
    if known is not None and old not in known and not staff_roles.is_custom_code(old):
        return False   # AA-878: the viewer has no group
    return new is None or group_of(old) != group_of(new)


def e2e_role_ok(allowed: str, role: Optional[str], based_on: Optional[str]) -> tuple[bool, str]:
    """``(ok, the allowed role in words)`` for an E2E address's CIS role: the one role it may hold, or for A10g a
    custom role based on content_lead (``based_on`` known when the role is set here)."""
    if allowed != E2E_ANY_CUSTOM_ROLE:
        return role == allowed, allowed
    ok = staff_roles.is_custom_code(role) and based_on in (None, e2e_staff.E2E_CUSTOM_MAKER_TEST_STAFF_ROLE)
    return ok, "a custom role based on content_lead"


async def set_business_owner(pool: Any, *, actor_id: str, staff_id: str, value: bool) -> Any:
    """Set or clear the business-owner marker of one account (R3.3); returns the directory's WriteResult."""
    from api.services import staff_directory as sd   # noqa: PLC0415 - staff_directory imports this module

    value = bool(value)
    async with pool.acquire() as conn:
        async with conn.transaction():
            actor = await sd._require_owner(conn, actor_id)
            if sd.e2e_owner.is_actor(actor):
                # AA-1176 (A10h, D5): the Dev-only E2E owner never sets or clears the business-owner marker.
                raise _refused(403, "The E2E owner account may not change the business owner",
                               e2e_staff.E2E_OWNER_SCOPE)
            target = await sd._load_target(conn, staff_id)
            staff_id = target["id"]
            before = bool(target["is_business_owner"])
            if before == value:
                return sd.WriteResult(staff_id=staff_id, operation="business_owner", cognito=sd.COGNITO_NOT_APPLICABLE)
            if value:
                await _may_hold_marker(conn, target, staff_id)
            try:
                await conn.execute(SQL_SET_BUSINESS_OWNER, staff_id, value)
            except asyncpg.UniqueViolationError:
                raise _refused(409, "Another staff member just became the business owner",
                               "business_owner_taken") from None
            await sd._audit(conn, actor_id, BUSINESS_OWNER_CHANGED, staff_id,
                            {"is_business_owner": {"before": before, "after": value}})
    staff_identity.clear_cache()
    return sd.WriteResult(staff_id=staff_id, operation="business_owner", cognito=sd.COGNITO_NOT_APPLICABLE)


async def _may_hold_marker(conn: Any, target: Any, staff_id: str) -> None:
    from api.services.staff_directory import is_e2e_test_email   # noqa: PLC0415

    if is_e2e_test_email(target["email"]):
        raise _refused(400, "An E2E test account can never be the business owner", "e2e_test_scope")
    if not target["is_active"]:
        raise _refused(409, "This account is disabled; enable it first", "inactive")
    if await conn.fetchval(SQL_BUSINESS_OWNER_HOLDER, staff_id) is not None:
        raise _refused(409, "Another staff member is the business owner; clear that marker first",
                       "business_owner_taken")
