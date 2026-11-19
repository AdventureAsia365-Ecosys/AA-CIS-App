"""AA-1127 (C2 "spend truth", S283 UI audit; tech lead decision S285 E2; CIS 461) — the daily Cost Explorer check's
settings on Platform › Settings (area "spend").

``cis.spend.ce_daily_refresh_enabled``: whether the api's ops metrics sampler queues job kind ``cost_explorer_refresh``
once per UTC day (about 0.03 USD a run). ``cis.spend.ce_daily_refresh_hour_utc``: the UTC hour from which it does.
``cis.spend.ce_stale_after_hours``: how old the newest stored Cost Explorer fetch may be before AI & spend › Spend marks
the AWS figures stale. The keys equal the CIS 461 seed (tests/integration/test_aa1127_migration_461.py and the
settings-lint reader check, tests/unit/test_s279_settings_lint.py); the reader (api/services/cost_explorer_daily.py)
reads the values in force through ``shared.settings``.
"""
from __future__ import annotations

ENABLED = "cis.spend.ce_daily_refresh_enabled"
RUN_HOUR_UTC = "cis.spend.ce_daily_refresh_hour_utc"
STALE_AFTER_HOURS = "cis.spend.ce_stale_after_hours"

ALL = (ENABLED, RUN_HOUR_UTC, STALE_AFTER_HOURS)
