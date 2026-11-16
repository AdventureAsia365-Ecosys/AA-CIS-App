"""AA-1120 (CIS 434) — the settings registry keys of the DataForSEO research cache (research_cache.py, dfs_queue.py).

The two freshness windows of acp_contract.search_demand and the recovery window of paid queued tasks are platform
settings on Platform › Settings (tech lead S279: a TTL or retention window is a setting, never a code literal): readers
name a key through these constants and read the value in force with ``shared.settings``, never a copied number. The
keys equal the CIS 434 seed (tests/integration/test_aa1120_migration_434.py and the settings-lint reader check,
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
