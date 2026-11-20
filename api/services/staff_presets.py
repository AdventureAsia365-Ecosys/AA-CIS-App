"""The Staff page's role presets (AA-1031, AA-1091, AA-1150): what each one sets, as data and pure functions.

``api/services/staff_directory.py`` (the only writer of staff roles) applies them inside its guarded transaction and
re-exports every name here, so callers keep using ``staff_directory.PRESETS`` and friends.

- ``platform_owner`` (AA-1031), shown as **"Administrator"** since AA-1150: CIS admin + Booking admin, and CRM
  ``platform_admin`` added to the CRM roles held.
- ``business_owner`` (AA-1150): CIS admin + Booking admin; the CRM roles are left as they are (no read-only CRM staff
  role exists yet).
- ``content_ops`` (AA-1150), **"Content + Product & operations"**: CIS ``content_lead``, NO Booking role and no CRM role
  (tech lead decision Q1, S285: no interim Booking grant until custom roles exist). It replaces roles, so the
  directory's self-demote and last-owner guards apply.
- ``pricing_team`` (AA-1091, S278): what the customer-service team needs to enter prices in CIS (Reference prices:
  ``reference_price.write``, held by admin / content_lead / customer_success) and in Booking (supplier rates, pricing
  rules, trip plan components and departures: Booking ``pricing.write`` / ``supply.write`` / ``trip_plan.write``, held
  by Booking admin / contracting). Additive only: a role that already has those rights is kept, an empty slot (CIS
  viewer, no Booking grant, a retired Booking grant) is filled, and any other role (CIS finance, content, tenant_qa,
  support, ai_ops; Booking sales, finance) is NEVER replaced: one person holds one role per app, so the preset is
  refused with 409 ``preset_would_replace`` and the owner sets the roles by hand. CRM roles are untouched.

No preset ever sets the owner flag (``is_owner``).
"""
from __future__ import annotations

from typing import Any, Optional

# AA-1150: Booking roles the directory no longer grants (S285 T13). A grant row that still holds one is retired by Sync
# (deleted in the same audited transaction) and its WORKFORCE group is removed like any other unwanted group.
RETIRED_BOOKING_ROLES: tuple[str, ...] = ("content_creator",)

PRESET_PLATFORM_OWNER = "platform_owner"
PRESET_PRICING_TEAM = "pricing_team"
PRESET_BUSINESS_OWNER = "business_owner"
PRESET_CONTENT_OPS = "content_ops"
PRESETS: tuple[str, ...] = (PRESET_PLATFORM_OWNER, PRESET_PRICING_TEAM, PRESET_BUSINESS_OWNER, PRESET_CONTENT_OPS)

PRESET_CIS_ROLE = "admin"
PRESET_BOOKING_ROLE = "admin"
PRESET_CRM_ROLE = "platform_admin"
CONTENT_OPS_CIS_ROLE = "content_lead"

PRICING_CIS_ROLE = "customer_success"
PRICING_KEEP_CIS_ROLES = frozenset({"admin", "content_lead", "customer_success"})
PRICING_FILL_CIS_ROLES = frozenset({"viewer"})          # the read-only role: nothing is lost by raising it
PRICING_BOOKING_ROLE = "contracting"
PRICING_KEEP_BOOKING_ROLES = frozenset({"admin", "contracting"})


def preset_fields(preset: str) -> dict[str, Any]:
    """The fields a preset sets directly (``role``, ``booking_role``, ``crm_roles``); a field left out is kept.
    ``pricing_team`` sets nothing here: it is planned from the roles read in the transaction (:func:`pricing_team`).
    ``platform_owner`` adds its CRM role to those held, which the directory does in the transaction too."""
    if preset in (PRESET_PLATFORM_OWNER, PRESET_BUSINESS_OWNER):
        return {"role": PRESET_CIS_ROLE, "booking_role": PRESET_BOOKING_ROLE}
    if preset == PRESET_CONTENT_OPS:
        return {"role": CONTENT_OPS_CIS_ROLE, "booking_role": None, "crm_roles": []}
    return {}


def pricing_team(old_role: Optional[str], old_booking: Optional[str]) -> tuple[Optional[str], Optional[str], list]:
    """AA-1091: ``(new_role, new_booking, lost)`` of the Pricing team preset for these current roles: ``new_role`` /
    ``new_booking`` None = kept; ``lost`` names each role the preset would have to replace (then it is refused)."""
    new_role, new_booking, lost = None, None, []
    if old_role not in PRICING_KEEP_CIS_ROLES:
        if old_role is None or old_role in PRICING_FILL_CIS_ROLES:
            new_role = PRICING_CIS_ROLE
        else:
            lost.append(f"CIS {old_role}")
    if old_booking not in PRICING_KEEP_BOOKING_ROLES:
        if old_booking is None or old_booking in RETIRED_BOOKING_ROLES:   # AA-1150: a retired grant gives nothing
            new_booking = PRICING_BOOKING_ROLE
        else:
            lost.append(f"Booking {old_booking}")
    return new_role, new_booking, lost
