"""AA-1155 T14: the four people-role presets CIS 465 seeds, as the unit tests' in-memory ``shared.staff_role_preset``
(test_aa1155_staff_presets.py checks them against the migration's INSERT, so a fake never drifts from the seed)."""
from __future__ import annotations

from api.services.staff_presets import Preset

SEED_PRESETS: tuple[Preset, ...] = (
    Preset(code="platform_owner", label="Administrator", cis_role="admin", booking_mode="set", booking_role="admin",
           crm_mode="add", crm_roles=("platform_admin",), sort_order=10),
    Preset(code="business_owner", label="Business owner", cis_role="admin", booking_mode="set", booking_role="admin",
           sort_order=20),
    Preset(code="content_ops", label="Content + Product & operations", cis_role="content_lead", booking_mode="clear",
           crm_mode="replace", sort_order=30),
    Preset(code="pricing_team", label="Pricing team", cis_role="customer_success", booking_mode="set",
           booking_role="contracting", additive_only=True, cis_keep_roles=("admin", "content_lead", "customer_success"),
           booking_keep_roles=("admin", "contracting"), sort_order=40),
)


def seed_records() -> dict[str, dict]:
    """The seed as the rows ``SELECT … FROM shared.staff_role_preset`` returns, keyed by code."""
    return {p.code: {**p.as_dict(), "crm_roles": list(p.crm_roles)} for p in SEED_PRESETS}
