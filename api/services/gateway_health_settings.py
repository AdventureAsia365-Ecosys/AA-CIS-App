"""AA-1127 (C1, S283 UI audit; CIS 448) — the AI gateway's health setting on Platform › Settings (area "ai").

``cis.llm.idle_after_hours``: how long after its newest answered call a connection with no failure since stops reading
"ok" and reads idle ("No recent calls", status ``unknown`` with no reason) on AI gateway › Providers
(api/services/llm_availability.py). The "Need a look" tile leaves an idle row out and the row badge says why, so the
tile and the badge follow one rule and one value (tech lead S279: a threshold is a setting, never a code literal). The
key equals the CIS 448 seed (tests/integration/test_aa1127_migration_448.py and the settings-lint reader check,
tests/unit/test_s279_settings_lint.py); the reader reads the value in force through ``shared.settings``.
"""
from __future__ import annotations

IDLE_AFTER_HOURS = "cis.llm.idle_after_hours"

ALL = (IDLE_AFTER_HOURS,)
