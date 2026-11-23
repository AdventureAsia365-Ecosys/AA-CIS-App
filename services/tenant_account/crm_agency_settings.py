"""AA-1160 (CIS 475) — the setting key of the CRM agency marker (``shared.tenant_crm_agency``).

A CRM sighting of a tenant's agency counts only while it is younger than this many hours (Platform › Settings, area
tenant, maker-checker; tech lead S279: every window is data an administrator edits). The reader is
``services/tenant_account/crm_agency.py``. The key equals the CIS 475 seed (the settings-lint reader check,
tests/unit/test_s279_settings_lint.py).
"""
from __future__ import annotations

CRM_AGENCY_SEEN_HOURS = "cis.tenant.crm_agency_seen_hours"
