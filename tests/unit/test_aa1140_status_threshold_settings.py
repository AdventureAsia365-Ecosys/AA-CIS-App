"""AA-1140 (S289, the Ecosystem status page; CIS 476) — the twenty-one status page limits are compiled with the S285
advisory values as defaults, a plain label and a complete EN + VI guide, maker-checker, and the accessors of
shared/ops_status_thresholds.py return them in the units the page uses, a critical value never on the wrong side of its
warn value. No DB."""
from unittest.mock import patch

from shared import ops_status_thresholds as t
from shared import ops_thresholds
from shared import settings

DEFAULTS = {
    t.ERROR_RATE_WARN_PCT: 1, t.ERROR_RATE_CRIT_PCT: 5, t.CLIENT_ERROR_WARN_PCT: 10,
    t.P95_WARN_MS: 1500, t.P95_CRIT_MS: 3000, t.LAMBDA_P95_LIMIT_MS: 6000, t.MIN_REQUESTS: 50,
    t.QUEUE_WAIT_WARN_MIN: 15, t.QUEUE_WAIT_CRIT_MIN: 60, t.RDS_CPU_WARN_PCT: 70, t.RDS_CPU_CRIT_PCT: 90,
    t.RDS_CONNECTIONS_WARN_PCT: 80, t.RDS_CONNECTIONS_CRIT_PCT: 95, t.ROLE_CONNECTIONS_CRIT_PCT: 95,
    t.RDS_FREE_STORAGE_WARN_GIB: 15, t.RDS_FREE_STORAGE_CRIT_GIB: 10, t.XID_AGE_CRIT: 1_500_000_000,
    t.UPTIME_SLO_PCT: 99.5, t.STALE_FACTOR: 3, t.HISTORY_DAYS: 14, t.GUARD_STALE_HOURS: 30,
}


def test_every_key_is_compiled_with_its_default_and_a_guide():
    assert set(t.ALL) == set(DEFAULTS) and len(t.ALL) == len(set(t.ALL))
    for key, default in DEFAULTS.items():
        d = settings.definition(key)
        assert (d.app, d.area, d.scope_kind, d.enforced, d.approval) == ("cis", "ops", "platform", True,
                                                                         "maker_checker"), key
        assert d.default_value == default and d.label, key
        assert d.min_value is not None and d.max_value is not None and d.min_value <= default <= d.max_value, key
        guide = settings.guide(key)
        for lang in ("en", "vi"):
            assert all(guide[lang][part] for part in ("what", "raise", "lower", "when", "example")), (key, lang)


def test_pairs_and_units():
    assert t.error_rate() == {"warn": 1.0, "crit": 5.0, "warn_key": t.ERROR_RATE_WARN_PCT,
                              "crit_key": t.ERROR_RATE_CRIT_PCT, "dir": "above"}
    assert (t.p95_ms()["warn"], t.p95_ms()["crit"]) == (1500.0, 3000.0)
    assert (t.queue_wait_min()["warn"], t.queue_wait_min()["crit"]) == (15.0, 60.0)
    free = t.rds_free_storage_gib()
    assert (free["warn"], free["crit"], free["dir"]) == (15.0, 10.0, "below")
    role = t.role_connections_pct()
    assert (role["warn"], role["crit"], role["warn_key"]) == (80.0, 95.0, ops_thresholds.ROLE_CONNECTIONS_PCT)
    xid = t.xid_age()
    assert (xid["warn"], xid["crit"]) == (1e9, 1.5e9)
    assert t.min_requests() == 50 and isinstance(t.min_requests(), int)
    assert t.history_days() == 14 and t.uptime_slo_pct() == 99.5 and t.stale_factor() == 3.0
    assert t.guard_stale_hours() == 30.0 and t.lambda_p95_limit_ms() == 6000.0 and t.client_error_warn_pct() == 10.0


def test_a_critical_value_on_the_wrong_side_falls_back_to_the_warn_value():
    values = {t.ERROR_RATE_WARN_PCT: 4, t.ERROR_RATE_CRIT_PCT: 2,
              t.RDS_FREE_STORAGE_WARN_GIB: 15, t.RDS_FREE_STORAGE_CRIT_GIB: 20}
    real = settings.get
    with patch.object(settings, "get", side_effect=lambda k, *a, **kw: values.get(k, real(k, *a, **kw))):
        assert t.error_rate()["crit"] == 4.0
        assert t.rds_free_storage_gib()["crit"] == 15.0


def test_snapshot_names_only_known_keys():
    snap = t.snapshot()
    keys = set()
    for v in snap.values():
        keys |= {v[k] for k in ("key", "warn_key", "crit_key") if k in v}
    allowed = set(t.ALL) | {ops_thresholds.ROLE_CONNECTIONS_PCT, ops_thresholds.XID_AGE_LIMIT,
                            ops_thresholds.APP_SPEND_NEAR_PCT}
    assert keys <= allowed and set(t.ALL) <= keys
