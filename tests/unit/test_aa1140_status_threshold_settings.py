"""AA-1140 (S289, the Ecosystem status page; CIS 475, 477; AA-1141 CIS 483; AA-1142 CIS 486) — the forty-five status
page limits are compiled with the S285 advisory values as defaults, a plain label and a complete EN + VI guide,
maker-checker, and the accessors of shared/ops_status_thresholds.py return them in the units the page uses, a
critical value never on the wrong side of its warn value. No DB."""
from unittest.mock import patch

from shared import ops_status_thresholds as t
from shared import ops_thresholds
from shared import settings

DEFAULTS = {
    t.ERROR_RATE_WARN_PCT: 1, t.ERROR_RATE_CRIT_PCT: 5, t.CLIENT_ERROR_WARN_PCT: 10,
    t.P95_WARN_MS: 1500, t.P95_CRIT_MS: 3000, t.LAMBDA_P95_LIMIT_MS: 6000, t.MIN_REQUESTS: 50,
    t.QUEUE_WAIT_WARN_MIN: 15, t.QUEUE_WAIT_CRIT_MIN: 60, t.RDS_CPU_WARN_PCT: 70, t.RDS_CPU_CRIT_PCT: 90,
    t.RDS_CONNECTIONS_WARN_PCT: 80, t.RDS_CONNECTIONS_CRIT_PCT: 95, t.ROLE_CONNECTIONS_CRIT_PCT: 95,
    t.RDS_FREE_STORAGE_WARN_GIB: 15, t.RDS_FREE_STORAGE_CRIT_GIB: 10, t.RDS_FREE_WARN_PCT: 20, t.RDS_FREE_CRIT_PCT: 10,
    t.XID_AGE_CRIT: 1_500_000_000,
    t.UPTIME_SLO_PCT: 99.5, t.STALE_FACTOR: 3, t.HISTORY_DAYS: 14, t.GUARD_STALE_HOURS: 30,
    # AA-1141 (CIS 483): ack / snooze / mute, the cost anomaly banner, the capacity forecast
    t.ACK_REASON_MIN_CHARS: 20, t.ACK_HOURS: 24, t.MUTE_MAX_DAYS: 7, t.SNOOZE_TOMORROW_HOUR: 8,
    t.SNOOZE_CHOICES_HOURS: [1, 4],
    t.COST_ANOMALY_FACTOR: 3, t.COST_ANOMALY_BASELINE_DAYS: 7, t.COST_ANOMALY_FLOOR_USD_H: 0.1,
    t.FORECAST_WINDOW_DAYS: 7, t.FORECAST_WARN_DAYS: 30, t.FORECAST_CRIT_DAYS: 7,
    # AA-1142 (CIS 486): the availability objective and burn rates, certificates, incidents, agencies
    t.SLO_TARGET_PCT: 99.5, t.SLO_WINDOW_DAYS: 30, t.SLO_BUDGET_WARN_PCT: 25, t.BURN_FAST_WINDOW_MIN: 60, t.BURN_FAST_FACTOR: 14.4,
    t.BURN_SLOW_WINDOW_HOURS: 6, t.BURN_SLOW_FACTOR: 6, t.CERT_WARN_DAYS: 21, t.CERT_CRIT_DAYS: 7,
    t.INCIDENT_LOOKBACK_MIN: 60, t.TENANT_TOP_N: 10,
}


def test_every_key_is_compiled_with_its_default_and_a_guide():
    assert set(t.ALL) == set(DEFAULTS) and len(t.ALL) == len(set(t.ALL))
    for key, default in DEFAULTS.items():
        d = settings.definition(key)
        assert (d.app, d.area, d.scope_kind, d.enforced, d.approval) == ("cis", "ops", "platform", True,
                                                                         "maker_checker"), key
        assert d.default_value == default and d.label, key
        if d.value_type == "list":   # AA-1141: the snooze lengths, each bounded by the allowed values
            assert d.allowed_values and set(default) <= set(d.allowed_values), key
        else:
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
    assert t.snooze_choices_hours() == [1, 4] and t.acks()["snooze_hours"] == [1, 4]


def test_snooze_choices_are_sorted_whole_hours_each_once():
    real = settings.get
    values = {t.SNOOZE_CHOICES_HOURS: [4, 1, 4, 12]}
    with patch.object(settings, "get", side_effect=lambda k, *a, **kw: values.get(k, real(k, *a, **kw))):
        assert t.snooze_choices_hours() == [1, 4, 12]


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
        keys |= {v[k] for k in v if k == "key" or k.endswith("_key")}   # AA-1141: acks / anomaly / forecast blocks
    allowed = set(t.ALL) | {ops_thresholds.ROLE_CONNECTIONS_PCT, ops_thresholds.XID_AGE_LIMIT,
                            ops_thresholds.APP_SPEND_NEAR_PCT}
    assert keys <= allowed and set(t.ALL) <= keys


def test_free_storage_is_a_share_of_the_allocated_storage_with_the_gib_pair_as_fallback():
    # CIS 477: 20 GiB allocated, 20 % / 10 % -> 4 / 2 GiB; a share follows autoscaling (100 GiB -> 20 / 10 GiB)
    small = t.rds_free_storage(20)
    assert (small["warn"], small["crit"], small["warn_pct"], small["crit_pct"]) == (4.0, 2.0, 20.0, 10.0)
    assert (small["basis"], small["dir"], small["warn_key"], small["allocated_gib"]) == (
        "pct", "below", t.RDS_FREE_WARN_PCT, 20.0)
    assert (t.rds_free_storage(100)["warn"], t.rds_free_storage(100)["crit"]) == (20.0, 10.0)
    for unknown in (None, 0):
        fb = t.rds_free_storage(unknown)
        assert (fb["warn"], fb["crit"], fb["basis"], fb["warn_key"]) == (15.0, 10.0, "gib", t.RDS_FREE_STORAGE_WARN_GIB)
    assert t.snapshot()["rds_free_storage_pct"]["warn"] == 20.0


def test_a_free_storage_share_on_the_wrong_side_falls_back_to_the_warn_share():
    values = {t.RDS_FREE_WARN_PCT: 15, t.RDS_FREE_CRIT_PCT: 25}
    real = settings.get
    with patch.object(settings, "get", side_effect=lambda k, *a, **kw: values.get(k, real(k, *a, **kw))):
        assert t.rds_free_storage(40)["crit"] == t.rds_free_storage(40)["warn"] == 6.0


def test_aa1142_slo_and_certificate_accessors():
    s = t.slo()
    assert (s["target_pct"], s["window_days"], s["fast"]["window_min"], s["fast"]["factor"]) == (99.5, 30, 60, 14.4)
    assert (s["slow"]["window_hours"], s["slow"]["factor"], s["min_requests"]) == (6, 6.0, 50)
    assert (s["budget_warn_pct"], s["budget_warn_key"]) == (25.0, t.SLO_BUDGET_WARN_PCT)
    assert t.cert_days() == {"warn": 21.0, "crit": 7.0, "warn_key": t.CERT_WARN_DAYS, "crit_key": t.CERT_CRIT_DAYS,
                             "dir": "below"}
    assert t.incident_lookback_min() == 60 and t.tenant_top_n() == 10
    snap = t.snapshot()
    assert {"slo", "cert_days", "incident_lookback_min", "tenant_top_n"} <= set(snap)


def test_aa1142_cert_critical_above_warn_is_lowered():
    real = settings.get
    with patch.object(settings, "get", lambda k: 40 if k == t.CERT_CRIT_DAYS else real(k)):
        assert t.cert_days()["crit"] == 21.0
