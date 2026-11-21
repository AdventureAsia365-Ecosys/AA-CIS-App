"""AA-1171 (S287 alerts catalogue P0, tech lead decision D3) — CIS 469: the eight in-app check thresholds are compiled
with today's values as defaults, a plain label and a complete EN + VI guide, maker-checker, and the accessors of
shared/ops_thresholds.py return them in the units the checks use. No DB."""
from datetime import timedelta

from shared import ops_thresholds as t
from shared import settings

DEFAULTS = {
    t.JOBS_STARVED_AFTER_S: 900,
    t.JOBS_OVERDUE_FACTOR: 2,
    t.ROLE_CONNECTIONS_PCT: 80,
    t.XID_AGE_LIMIT: 1_000_000_000,
    t.RECOMPUTE_LAG_LIMIT_S: 1800,
    t.RESEARCH_STALE_SHARE_PCT: 20,
    t.APP_SPEND_NEAR_PCT: 80,
    t.BELL_EVERY_HOURS: 6,
}


def test_every_key_is_compiled_with_todays_value_and_a_guide():
    assert set(t.ALL) == set(DEFAULTS)
    for key, default in DEFAULTS.items():
        d = settings.definition(key)
        assert (d.app, d.area, d.scope_kind, d.enforced, d.approval) == ("cis", "ops", "platform", True,
                                                                         "maker_checker"), key
        assert d.default_value == default and d.label, key
        assert d.min_value is not None and d.max_value is not None and d.min_value <= default <= d.max_value, key
        guide = settings.guide(key)
        for lang in ("en", "vi"):
            assert all(guide[lang][part] for part in ("what", "raise", "lower", "when", "example")), (key, lang)


def test_accessors_return_the_units_the_checks_use():
    assert t.jobs_starved_after_s() == 900.0
    assert t.jobs_overdue_factor() == 2.0
    assert t.role_connections_pct() == 80.0
    assert t.xid_age_limit() == 1_000_000_000
    assert t.recompute_lag_limit_s() == 1800.0
    assert t.research_stale_share() == 0.2
    assert t.app_spend_near_ratio() == 0.8
    assert t.bell_every() == timedelta(hours=6)


def test_by_check_names_only_known_keys():
    for check, keys in t.BY_CHECK.items():
        assert keys and all(k in t.ALL for k in keys), check
