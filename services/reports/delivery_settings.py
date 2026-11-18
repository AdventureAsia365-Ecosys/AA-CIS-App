"""AA-1108 (CIS 433), AA-1125 (CIS 445) and AA-1135 (CIS 447) — the settings registry keys of Platform › Delivery and
of the DORA keys of DEV-01.

Every colour threshold of the delivery metrics is a platform setting on Platform › Settings (area "delivery"; tech lead
S279: a threshold is a setting, never a code literal; S282 decision 4: the starting values are the DORA "high" band).
Readers (api/services/delivery_metrics.py, services/reports/delivery.py) name a key through these constants and read
the value in force with ``shared.settings.aget``, never a copied number. The keys equal the CIS 433 and CIS 445 seeds
(tests/integration/test_aa1108_migration_433.py, tests/integration/test_aa1125_migration_445.py and the settings-lint
reader check, tests/unit/test_s279_settings_lint.py).

Each banded metric has a green and an amber value; ``HIGHER_IS_BETTER`` says which way the bands run.
"""
from __future__ import annotations

DEPLOYS_PER_WEEK_GREEN = "cis.delivery.deploys_per_week_green"
DEPLOYS_PER_WEEK_AMBER = "cis.delivery.deploys_per_week_amber"
LEAD_TIME_GREEN_HOURS = "cis.delivery.lead_time_green_hours"
LEAD_TIME_AMBER_HOURS = "cis.delivery.lead_time_amber_hours"
CFR_GREEN_PCT = "cis.delivery.cfr_green_pct"
CFR_AMBER_PCT = "cis.delivery.cfr_amber_pct"
RECOVERY_GREEN_HOURS = "cis.delivery.recovery_green_hours"
RECOVERY_AMBER_HOURS = "cis.delivery.recovery_amber_hours"
REWORK_GREEN_PCT = "cis.delivery.rework_green_pct"
REWORK_AMBER_PCT = "cis.delivery.rework_amber_pct"
# The default branch: red when its newest CI or deploy run has failed for longer than this many hours, else amber.
MAIN_RED_HOURS = "cis.delivery.main_red_hours"
CI_P95_GREEN_MINUTES = "cis.delivery.ci_time_green_minutes"
CI_P95_AMBER_MINUTES = "cis.delivery.ci_time_amber_minutes"
# The smallest n a metric needs before it gets a colour (Dev has few events).
MIN_SAMPLE = "cis.delivery.min_sample"

# metric -> (green key, amber key, higher is better)
BANDS: dict[str, tuple[str, str, bool]] = {
    "deployment_frequency": (DEPLOYS_PER_WEEK_GREEN, DEPLOYS_PER_WEEK_AMBER, True),
    "lead_time": (LEAD_TIME_GREEN_HOURS, LEAD_TIME_AMBER_HOURS, False),
    "change_failure_rate": (CFR_GREEN_PCT, CFR_AMBER_PCT, False),
    "recovery_time": (RECOVERY_GREEN_HOURS, RECOVERY_AMBER_HOURS, False),
    "rework_rate": (REWORK_GREEN_PCT, REWORK_AMBER_PCT, False),
    "ci_p95": (CI_P95_GREEN_MINUTES, CI_P95_AMBER_MINUTES, False),
}

ALL = (DEPLOYS_PER_WEEK_GREEN, DEPLOYS_PER_WEEK_AMBER, LEAD_TIME_GREEN_HOURS, LEAD_TIME_AMBER_HOURS, CFR_GREEN_PCT,
       CFR_AMBER_PCT, RECOVERY_GREEN_HOURS, RECOVERY_AMBER_HOURS, REWORK_GREEN_PCT, REWORK_AMBER_PCT, MAIN_RED_HOURS,
       CI_P95_GREEN_MINUTES, CI_P95_AMBER_MINUTES, MIN_SAMPLE)

# AA-1125 (CIS 445): the Merge queue tab. Wait = p95 hours from entering the merge queue to the merge; ejection and
# flaky rates in percent of the entries that merged or were ejected (the summary's `queue` section). MIN_SAMPLE applies.
QUEUE_WAIT_GREEN_HOURS = "cis.delivery.queue_wait_green_hours"
QUEUE_WAIT_AMBER_HOURS = "cis.delivery.queue_wait_amber_hours"
QUEUE_EJECTION_GREEN_PCT = "cis.delivery.queue_ejection_green_pct"
QUEUE_EJECTION_AMBER_PCT = "cis.delivery.queue_ejection_amber_pct"
QUEUE_FLAKY_GREEN_PCT = "cis.delivery.queue_flaky_green_pct"
QUEUE_FLAKY_AMBER_PCT = "cis.delivery.queue_flaky_amber_pct"

# queue metric -> (green key, amber key, higher is better)
QUEUE_BANDS: dict[str, tuple[str, str, bool]] = {
    "queue_wait": (QUEUE_WAIT_GREEN_HOURS, QUEUE_WAIT_AMBER_HOURS, False),
    "ejection_rate": (QUEUE_EJECTION_GREEN_PCT, QUEUE_EJECTION_AMBER_PCT, False),
    "flaky_rate": (QUEUE_FLAKY_GREEN_PCT, QUEUE_FLAKY_AMBER_PCT, False),
}

QUEUE_ALL = (QUEUE_WAIT_GREEN_HOURS, QUEUE_WAIT_AMBER_HOURS, QUEUE_EJECTION_GREEN_PCT, QUEUE_EJECTION_AMBER_PCT,
             QUEUE_FLAKY_GREEN_PCT, QUEUE_FLAKY_AMBER_PCT)

# AA-1135 (CIS 447): the custom date range of Platform › Delivery (api/services/delivery_range.py). The longest range
# (days, from and to included) it computes, and how many computed ranges it keeps until the next collection.
CUSTOM_RANGE_MAX_DAYS = "cis.delivery.custom_range_max_days"
CUSTOM_RANGE_CACHE_ENTRIES = "cis.delivery.custom_range_cache_entries"

RANGE_ALL = (CUSTOM_RANGE_MAX_DAYS, CUSTOM_RANGE_CACHE_ENTRIES)
