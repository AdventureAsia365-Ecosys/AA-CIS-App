"""AA-1120 (CIS 434) / AA-1122 (CIS 442) — the settings registry keys of DataForSEO research.

The freshness windows of acp_contract.search_demand, the recovery window of paid queued tasks, and (AA-1122) the
automatic research tick, its caps, its alerts and the tour People Also Ask backfill are platform settings on
Platform › Settings (tech lead S279: a threshold, limit, cap, schedule or window is a setting, never a code literal):
readers name a key through these constants and read the value in force with ``shared.settings``, never a copied
number. The keys equal the CIS 434 / 442 seeds (tests/integration/test_aa1120_migration_434.py,
tests/integration/test_aa1122_migrations_440_442.py and the settings-lint reader check,
tests/unit/test_s279_settings_lint.py).
"""
from __future__ import annotations

# A (keyword, market) volume is reused for this many days; a place's volume tier is fresh for as long.
VOLUME_FRESH_DAYS = "cis.seo.research_volume_fresh_days"
# A (keyword, market) SERP (PAA, related searches, domains) is reused for this many days; the SERP tier likewise.
SERP_FRESH_DAYS = "cis.seo.research_serp_fresh_days"
# A queued task paid for but never collected is collected free when it was posted within this many days (0 = off).
QUEUE_RECOVER_DAYS = "cis.seo.dfs_queue_recover_days"

ALL = (VOLUME_FRESH_DAYS, SERP_FRESH_DAYS, QUEUE_RECOVER_DAYS)

# ── AA-1122 (CIS 442): automatic research (services/acp_contract/research_auto.py) ───────────────────────────────────
AUTO_ENABLED = "cis.seo.research_auto_enabled"
AUTO_WINDOW_START_UTC = "cis.seo.research_auto_window_start_utc"
AUTO_WINDOW_END_UTC = "cis.seo.research_auto_window_end_utc"
AUTO_NEW_MIN_PLACES = "cis.seo.research_auto_new_min_places"
AUTO_NEW_MAX_WAIT_DAYS = "cis.seo.research_auto_new_max_wait_days"
AUTO_REFRESH_EVERY_DAYS = "cis.seo.research_auto_refresh_every_days"
REFRESH_AHEAD_DAYS = "cis.seo.research_refresh_ahead_days"
REFRESH_MAX_SHARE_PCT = "cis.seo.research_refresh_max_share_pct"
AUTO_DFS_QUEUE = "cis.seo.research_auto_dfs_queue"
AUTO_MAX_CONSECUTIVE_FAILURES = "cis.seo.research_auto_max_consecutive_failures"
REQUALIFY_MAX_PLACES = "cis.seo.research_requalify_max_places"
MARKET_TENANT_STATUSES = "cis.seo.research_market_tenant_statuses"
AUTO_ACTIVATION_STALE_PCT = "cis.seo.research_auto_activation_stale_pct"
NEW_WAIT_ALERT_DAYS = "cis.seo.research_new_wait_alert_days"
MONTH_WARN_PCT = "cis.seo.research_month_warn_pct"
AUTO_MAX_USD_PER_RUN = "cis.spend.research_auto_max_usd_per_run"
MAX_USD_PER_MONTH = "cis.spend.research_max_usd_per_month"
AUTO_MIN_RUN_USD = "cis.spend.research_auto_min_run_usd"
JEV_ASK_ESTIMATE_USD = "cis.spend.research_jev_ask_estimate_usd"

# ── AA-1122 (CIS 442): the tour People Also Ask backfill (services/seo_intelligence/tour_paa.py) ─────────────────────
TOUR_PAA_MAX = "cis.seo.tour_paa_max"
TOUR_PAA_PER_KEYWORD = "cis.seo.tour_paa_per_keyword"
TOUR_PAA_MAX_USD_PER_RUN = "cis.spend.tour_paa_backfill_max_usd_per_run"

AUTO = (AUTO_ENABLED, AUTO_WINDOW_START_UTC, AUTO_WINDOW_END_UTC, AUTO_NEW_MIN_PLACES, AUTO_NEW_MAX_WAIT_DAYS,
        AUTO_REFRESH_EVERY_DAYS, REFRESH_AHEAD_DAYS, REFRESH_MAX_SHARE_PCT, AUTO_DFS_QUEUE,
        AUTO_MAX_CONSECUTIVE_FAILURES, REQUALIFY_MAX_PLACES, MARKET_TENANT_STATUSES, AUTO_ACTIVATION_STALE_PCT,
        NEW_WAIT_ALERT_DAYS, MONTH_WARN_PCT, AUTO_MAX_USD_PER_RUN, MAX_USD_PER_MONTH, AUTO_MIN_RUN_USD,
        JEV_ASK_ESTIMATE_USD)
TOUR_PAA = (TOUR_PAA_MAX, TOUR_PAA_PER_KEYWORD, TOUR_PAA_MAX_USD_PER_RUN)
