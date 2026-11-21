"""AA-1170 (S287, compliance readiness page; CIS 466) — the settings of Platform › Compliance (area "compliance").

Every value that moves the Evidenced % is a setting on Platform › Settings (tech lead S279: a threshold, window or
weight is a setting, never a code literal or an env var alone). The scorer (api/services/compliance_score.py) and the
page service (api/services/compliance.py) name a key through these constants and read the value in force with
``shared.settings.aget``. The keys equal the CIS 466 seed (tests/integration/test_aa1170_migration_466.py and the
settings-lint reader check, tests/unit/test_s279_settings_lint.py).

- ``VALIDITY_HOURS``: per evidence source, how many hours a published result counts; older is stale.
- ``HEARTBEAT_HOURS``: a collector silent longer turns every check it feeds to error.
- ``PARTIAL_WEIGHT``: the weight of partial coverage in the weighted score (S286: 0.5).
- ``AGE_LIMITS``: per check whose result is a date, the oldest date that still passes (days), or the most items a
  count check tolerates. ``CI_SCAN_MAX_AGE_DAYS`` grades the date of the oldest newest image scan, so a CI scan that
  stopped running fails instead of passing on an old run.
- ``REFERENCE_SCORES``: the S286 hand-assessed score per framework, the reference line (display only).
"""
from __future__ import annotations

# source -> key: how many hours a result of that collector counts (phase 1: the root daily collector only)
VALIDITY_HOURS = {"root_daily": "cis.compliance.validity_hours.root_daily"}
HEARTBEAT_HOURS = "cis.compliance.collector_heartbeat_hours"
PARTIAL_WEIGHT = "cis.compliance.partial_weight"
DRILL_MAX_AGE_DAYS = "cis.compliance.drill_max_age_days"
ACCESS_REVIEW_MAX_AGE_DAYS = "cis.compliance.access_review_max_age_days"
DOCS_OVERDUE_MAX = "cis.compliance.docs_overdue_max"
CI_SCAN_MAX_AGE_DAYS = "cis.compliance.ci_scan_max_age_days"
REFERENCE_SCORES = "cis.compliance.reference_scores"

# check_id -> (fact the collector publishes, key, kind): "age_days" = the fact is an ISO date that must be at most the
# setting's days old at read time; "max" = the fact is a count that must be at most the setting.
FACT_LIMITS = {
    "last_restore_drill": ("last_date", DRILL_MAX_AGE_DAYS, "age_days"),
    "last_access_review": ("last_date", ACCESS_REVIEW_MAX_AGE_DAYS, "age_days"),
    "docs_review_overdue": ("overdue", DOCS_OVERDUE_MAX, "max"),
    "ci_image_scan_sbom": ("last_date", CI_SCAN_MAX_AGE_DAYS, "age_days"),
}

ALL = (*VALIDITY_HOURS.values(), HEARTBEAT_HOURS, PARTIAL_WEIGHT, DRILL_MAX_AGE_DAYS, ACCESS_REVIEW_MAX_AGE_DAYS,
       DOCS_OVERDUE_MAX, CI_SCAN_MAX_AGE_DAYS, REFERENCE_SCORES)
