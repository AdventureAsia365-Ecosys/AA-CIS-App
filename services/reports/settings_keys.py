"""AA-1003 R6 (CIS 430) — the settings registry keys of the partner and agency reports (services/reports/partners.py).

Every decision level of EX-03 and EX-07 is a platform setting on Platform › Settings (tech lead S279: a threshold is a
setting, never a code literal): readers name a key through these constants and read the value in force with
``shared.settings.aget``, never a copied number. The keys equal the CIS 430 seed (tests/integration/
test_aa1003_migration_430.py and the settings-lint reader check, tests/unit/test_s279_settings_lint.py).
"""
from __future__ import annotations

# K-PART-ACT: a Content studio tenant is activated by a first publish within this many days of its creation.
ACTIVATION_DAYS = "cis.reports.partner_activation_days"
# K-ONB-STUCK: a partner not activated yet is stuck after this many days on one onboarding step.
STUCK_DAYS = "cis.reports.partner_stuck_days"
# K-PART-QUOTA: upsell candidate at or above this share of its own quota; churn risk under the low-use share for the
# low-use months in a row (ending with the report's month).
UPSELL_QUOTA_PCT = "cis.reports.partner_upsell_quota_pct"
LOW_USE_QUOTA_PCT = "cis.reports.partner_low_use_quota_pct"
LOW_USE_MONTHS = "cis.reports.partner_low_use_months"

ALL = (ACTIVATION_DAYS, STUCK_DAYS, UPSELL_QUOTA_PCT, LOW_USE_QUOTA_PCT, LOW_USE_MONTHS)
