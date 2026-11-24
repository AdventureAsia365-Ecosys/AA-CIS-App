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

AA-1174 (S288, CIS 471) adds the privacy evidence: two sources the API collects itself (``cis_db``: the retention runs
of the job runner; ``crm_ops``: consent capture and campaign sends to opted-in contacts, counts from the partner CRM),
the legal document checks of the root collector (graded on pages past their own review cycle), the erasure drill age,
and ``APPLICABILITY_REVIEW_MAX_AGE_DAYS`` (display only: when a verdict of the applicability matrix shows "Review due").
``CAMPAIGN_CONSENT_WINDOW_DAYS`` is the window the CRM counts sends over (a query parameter the CRM caps).

AA-1173 (S290, CIS 481) adds the phase 2-3 keys: the validity of the two Infra sources sealed in the Object Lock
evidence bucket (``aws_nightly``: the nightly AWS read-only checks; ``prowler_weekly``: the weekly Prowler scan), the
failing critical / high Prowler findings the scan check tolerates, the longest an exception may run (checked at
proposal and at approval) and the age of the newest monthly posture review the review check accepts.
"""
from __future__ import annotations

# source -> key: how many hours a result of that collector counts (phase 1: the root daily collector only)
VALIDITY_HOURS = {"root_daily": "cis.compliance.validity_hours.root_daily",
                  "cis_db": "cis.compliance.validity_hours.cis_db",      # AA-1174 (471)
                  "crm_ops": "cis.compliance.validity_hours.crm_ops",    # AA-1174 (471)
                  "aws_nightly": "cis.compliance.validity_hours.aws_nightly",        # AA-1173 (481)
                  "prowler_weekly": "cis.compliance.validity_hours.prowler_weekly"}  # AA-1173 (481)
HEARTBEAT_HOURS = "cis.compliance.collector_heartbeat_hours"
PARTIAL_WEIGHT = "cis.compliance.partial_weight"
DRILL_MAX_AGE_DAYS = "cis.compliance.drill_max_age_days"
ACCESS_REVIEW_MAX_AGE_DAYS = "cis.compliance.access_review_max_age_days"
DOCS_OVERDUE_MAX = "cis.compliance.docs_overdue_max"
CI_SCAN_MAX_AGE_DAYS = "cis.compliance.ci_scan_max_age_days"
REFERENCE_SCORES = "cis.compliance.reference_scores"
# AA-1174 (CIS 471)
LEGAL_DOCS_OVERDUE_MAX = "cis.compliance.legal_docs_overdue_max"
ERASURE_DRILL_MAX_AGE_DAYS = "cis.compliance.erasure_drill_max_age_days"
RETENTION_RUN_MAX_AGE_DAYS = "cis.compliance.retention_run_max_age_days"
RETENTION_LIVE_MAX_AGE_DAYS = "cis.compliance.retention_live_max_age_days"
CONSENT_CAPTURE_MAX_AGE_DAYS = "cis.compliance.consent_capture_max_age_days"
CAMPAIGN_CONSENT_VIOLATIONS_MAX = "cis.compliance.campaign_consent_violations_max"
CAMPAIGN_CONSENT_WINDOW_DAYS = "cis.compliance.campaign_consent_window_days"
APPLICABILITY_REVIEW_MAX_AGE_DAYS = "cis.compliance.applicability_review_max_age_days"
# AA-1173 (CIS 481)
PROWLER_CRITICAL_HIGH_MAX = "cis.compliance.prowler_critical_high_max"
EXCEPTION_MAX_DAYS = "cis.compliance.exception_max_days"
REVIEW_MAX_AGE_DAYS = "cis.compliance.review_max_age_days"

# AA-1174: the legal document checks of the root collector (tools/compliance/checks.json "legal_docs"); each publishes
# `overdue` = its pages past their own last_reviewed + review_every_days.
LEGAL_DOC_CHECKS = ("legal_ropa_reviewed", "legal_dpa_reviewed", "legal_subprocessors_reviewed",
                    "legal_transfer_dossier_vn", "legal_dsar_runbook_reviewed", "legal_consent_capture_reviewed")

# check_id -> (fact the collector publishes, key, kind): "age_days" = the fact is an ISO date that must be at most the
# setting's days old at read time; "max" = the fact is a count that must be at most the setting.
FACT_LIMITS = {
    "last_restore_drill": ("last_date", DRILL_MAX_AGE_DAYS, "age_days"),
    "last_access_review": ("last_date", ACCESS_REVIEW_MAX_AGE_DAYS, "age_days"),
    "docs_review_overdue": ("overdue", DOCS_OVERDUE_MAX, "max"),
    "ci_image_scan_sbom": ("last_date", CI_SCAN_MAX_AGE_DAYS, "age_days"),
    # AA-1174 (471)
    **{cid: ("overdue", LEGAL_DOCS_OVERDUE_MAX, "max") for cid in LEGAL_DOC_CHECKS},
    "last_erasure_drill": ("last_date", ERASURE_DRILL_MAX_AGE_DAYS, "age_days"),
    "retention_runs_recent": ("last_date", RETENTION_RUN_MAX_AGE_DAYS, "age_days"),
    "retention_live_enabled": ("live_last_date", RETENTION_LIVE_MAX_AGE_DAYS, "age_days"),
    "consent_capture_present": ("last_date", CONSENT_CAPTURE_MAX_AGE_DAYS, "age_days"),
    "campaign_consent_enforced": ("violations", CAMPAIGN_CONSENT_VIOLATIONS_MAX, "max"),
    # AA-1173 (481)
    "prowler_critical_high": ("critical_high", PROWLER_CRITICAL_HIGH_MAX, "max"),
    "monthly_review_recent": ("last_date", REVIEW_MAX_AGE_DAYS, "age_days"),
}

ALL_466 = ("cis.compliance.validity_hours.root_daily", HEARTBEAT_HOURS, PARTIAL_WEIGHT, DRILL_MAX_AGE_DAYS,
           ACCESS_REVIEW_MAX_AGE_DAYS, DOCS_OVERDUE_MAX, CI_SCAN_MAX_AGE_DAYS, REFERENCE_SCORES)
ALL_471 = (VALIDITY_HOURS["cis_db"], VALIDITY_HOURS["crm_ops"], LEGAL_DOCS_OVERDUE_MAX, ERASURE_DRILL_MAX_AGE_DAYS,
           RETENTION_RUN_MAX_AGE_DAYS, RETENTION_LIVE_MAX_AGE_DAYS, CONSENT_CAPTURE_MAX_AGE_DAYS,
           CAMPAIGN_CONSENT_VIOLATIONS_MAX, CAMPAIGN_CONSENT_WINDOW_DAYS, APPLICABILITY_REVIEW_MAX_AGE_DAYS)
ALL_481 = (VALIDITY_HOURS["aws_nightly"], VALIDITY_HOURS["prowler_weekly"], PROWLER_CRITICAL_HIGH_MAX,
           EXCEPTION_MAX_DAYS, REVIEW_MAX_AGE_DAYS)
ALL = ALL_466 + ALL_471 + ALL_481
