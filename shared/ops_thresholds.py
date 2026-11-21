"""AA-1171 (S287 alerts catalogue P0, tech lead decision D3; CIS 469) — the thresholds of the CIS in-app ops checks on
Platform › Settings (area "ops").

The checks (shared/ops_checks.py, run by the api's ops metrics sampler; shared/jobs/backlog.py for the overdue factor;
services/recompute/lifecycle.py and services/acp_contract/research_coverage.py for their limits;
shared/llm_client/route_watch.py and services/quality/funnel.py for the reply cut share and the quality funnel rules)
and the bell window of each check family (``Check.bell_key``) read the values in force through these accessors,
never a module constant (tech lead S279: a threshold is a setting, maker-checker and audited, with today's value as
the seeded default). The keys equal the CIS 469 seed
(tests/integration/test_aa1171_migration_469.py and the settings-lint reader check,
tests/unit/test_s279_settings_lint.py).
``shared.settings.get`` serves the last snapshot (the api and the worker refresh it in the background) and the
compiled default before the first refresh, so a read never blocks a sampler tick and never fails.

The alerts catalogue (Platform › Alerts › Catalogue) lists each key next to the check it drives (:data:`BY_CHECK`).
"""
from __future__ import annotations

from datetime import timedelta

from shared import settings

JOBS_STARVED_AFTER_S = "cis.ops.jobs_starved_after_s"
JOBS_OVERDUE_FACTOR = "cis.ops.jobs_overdue_factor"
ROLE_CONNECTIONS_PCT = "cis.ops.role_connections_pct"
XID_AGE_LIMIT = "cis.ops.xid_age_limit"
RECOMPUTE_LAG_LIMIT_S = "cis.ops.recompute_lag_limit_s"
RESEARCH_STALE_SHARE_PCT = "cis.ops.research_stale_share_pct"
APP_SPEND_NEAR_PCT = "cis.ops.app_spend_near_pct"
BELL_EVERY_HOURS = "cis.ops.bell_every_hours"
REPLY_CUT_ALERT_PCT = "cis.ops.reply_cut_alert_pct"
QF_MIN_REQUESTS = "cis.ops.qf_min_requests"
QF_DRIFT_ALERT_PCT = "cis.ops.qf_drift_alert_pct"
QF_DRIFT_NEW_MODEL_PCT = "cis.ops.qf_drift_new_model_pct"
BELL_EVERY_ROUTE_HOURS = "cis.ops.bell_every_route_hours"
BELL_EVERY_DAILY_HOURS = "cis.ops.bell_every_daily_hours"
BELL_EVERY_NIGHTLY_HOURS = "cis.ops.bell_every_nightly_hours"

ALL = (JOBS_STARVED_AFTER_S, JOBS_OVERDUE_FACTOR, ROLE_CONNECTIONS_PCT, XID_AGE_LIMIT, RECOMPUTE_LAG_LIMIT_S,
       RESEARCH_STALE_SHARE_PCT, APP_SPEND_NEAR_PCT, BELL_EVERY_HOURS, REPLY_CUT_ALERT_PCT, QF_MIN_REQUESTS,
       QF_DRIFT_ALERT_PCT, QF_DRIFT_NEW_MODEL_PCT, BELL_EVERY_ROUTE_HOURS, BELL_EVERY_DAILY_HOURS,
       BELL_EVERY_NIGHTLY_HOURS)
# The bell window keys (hours); a check names one in Check.bell_key, BELL_EVERY_HOURS when it names none.
BELL_KEYS = (BELL_EVERY_HOURS, BELL_EVERY_ROUTE_HOURS, BELL_EVERY_DAILY_HOURS, BELL_EVERY_NIGHTLY_HOURS)

# The check (shared/ops_checks.py name or prefix) -> the keys that set its threshold (the bell window keys are on the
# Check itself, Check.bell_key). qf_fb. also reads the route policy's fallback_alert_pct (AI gateway › Trace).
BY_CHECK: dict[str, tuple[str, ...]] = {
    "jobs_starved": (JOBS_STARVED_AFTER_S,),
    "jobs_overdue": (JOBS_OVERDUE_FACTOR,),
    "rds_role_connections": (ROLE_CONNECTIONS_PCT,),
    "rds_xid_age": (XID_AGE_LIMIT,),
    "recompute_lifecycle_lag": (RECOMPUTE_LAG_LIMIT_S,),
    "research_stale_share": (RESEARCH_STALE_SHARE_PCT,),
    "llm_spend_alert.tripplanner.near": (APP_SPEND_NEAR_PCT,),
    "route_cut.": (REPLY_CUT_ALERT_PCT,),
    "qf_cut.": (REPLY_CUT_ALERT_PCT, QF_MIN_REQUESTS),
    "qf_fb.": (QF_MIN_REQUESTS,),
    "qf_drift.": (QF_DRIFT_ALERT_PCT, QF_DRIFT_NEW_MODEL_PCT, QF_MIN_REQUESTS),
}


def jobs_starved_after_s() -> float:
    return float(settings.get(JOBS_STARVED_AFTER_S))


def jobs_overdue_factor() -> float:
    return float(settings.get(JOBS_OVERDUE_FACTOR))


def role_connections_pct() -> float:
    return float(settings.get(ROLE_CONNECTIONS_PCT))


def xid_age_limit() -> int:
    return int(settings.get(XID_AGE_LIMIT))


def recompute_lag_limit_s() -> float:
    return float(settings.get(RECOMPUTE_LAG_LIMIT_S))


def research_stale_share() -> float:
    """The stale share limit as a fraction (20 % -> 0.2)."""
    return float(settings.get(RESEARCH_STALE_SHARE_PCT)) / 100.0


def app_spend_near_ratio() -> float:
    """The near-cap warning share as a fraction (80 % -> 0.8)."""
    return float(settings.get(APP_SPEND_NEAR_PCT)) / 100.0


def reply_cut_alert_pct() -> float:
    return float(settings.get(REPLY_CUT_ALERT_PCT))


def qf_min_requests() -> int:
    return int(settings.get(QF_MIN_REQUESTS))


def qf_drift_alert_pct() -> float:
    return float(settings.get(QF_DRIFT_ALERT_PCT))


def qf_drift_new_model_pct() -> float:
    return float(settings.get(QF_DRIFT_NEW_MODEL_PCT))


def bell_every(key: str = BELL_EVERY_HOURS) -> timedelta:
    """The bell window in force of one bell window key (:data:`BELL_KEYS`; the default window without one)."""
    if key not in BELL_KEYS:
        raise KeyError(key)
    return timedelta(hours=int(settings.get(key)))
