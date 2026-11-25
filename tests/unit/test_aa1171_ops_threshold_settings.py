"""AA-1171 (S287 alerts catalogue P0, tech lead decision D3) — CIS 469: the fifteen in-app check thresholds and bell
windows (plus CIS 488, AA-1141: the role connections samples in a row) are compiled with today's values as defaults, a
plain label and a complete EN + VI guide, maker-checker, and the accessors of shared/ops_thresholds.py return them in
the units the checks use. No DB."""
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
    t.REPLY_CUT_ALERT_PCT: 5,
    t.QF_MIN_REQUESTS: 20,
    t.QF_DRIFT_ALERT_PCT: 10,
    t.QF_DRIFT_NEW_MODEL_PCT: 1,
    t.BELL_EVERY_ROUTE_HOURS: 1,
    t.BELL_EVERY_DAILY_HOURS: 24,
    t.BELL_EVERY_NIGHTLY_HOURS: 20,
    t.ROLE_CONNECTIONS_SAMPLES: 3,   # AA-1141 (S292, CIS 488)
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
    assert t.role_connections_samples() == 3 and isinstance(t.role_connections_samples(), int)
    assert t.xid_age_limit() == 1_000_000_000
    assert t.recompute_lag_limit_s() == 1800.0
    assert t.research_stale_share() == 0.2
    assert t.app_spend_near_ratio() == 0.8
    assert t.bell_every() == timedelta(hours=6)
    assert t.reply_cut_alert_pct() == 5.0 and t.qf_drift_alert_pct() == 10.0 and t.qf_drift_new_model_pct() == 1.0
    assert t.qf_min_requests() == 20 and isinstance(t.qf_min_requests(), int)
    assert [t.bell_every(k) for k in t.BELL_KEYS] == [timedelta(hours=h) for h in (6, 1, 24, 20)]


def test_bell_every_refuses_a_key_that_is_no_bell_window():
    import pytest

    with pytest.raises(KeyError):
        t.bell_every(t.QF_MIN_REQUESTS)


def test_the_nightly_window_stays_under_a_day():
    d = settings.definition(t.BELL_EVERY_NIGHTLY_HOURS)
    assert d.max_value < 24, "a once-a-night check must bell on every night even when its run starts earlier"


def test_by_check_names_only_known_keys():
    for check, keys in t.BY_CHECK.items():
        assert keys and all(k in t.ALL for k in keys), check
