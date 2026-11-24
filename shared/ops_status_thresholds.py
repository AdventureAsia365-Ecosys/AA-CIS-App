"""AA-1140 (S289, the Ecosystem status page; CIS 475) — the limits the status page draws and judges against, on
Platform › Settings (area "ops").

The page (``/admin/overview``, ``GET /admin/overview/status``, ``api/services/ops_status/``) colours every app, chart,
tile and queue from these values and draws them as its warn / critical lines (tech lead S279: a threshold is a setting,
maker-checker and audited, with the S285 advisory value as the seeded default). Two warn values are the in-app check
settings of CIS 469 (``cis.ops.role_connections_pct``, ``cis.ops.xid_age_limit``, :mod:`shared.ops_thresholds`), so a
tile and its alert never disagree; the critical value next to them is new here. A signal with a CloudWatch alarm keeps
the alarm's own limit as the authority (S287 D2); these values only colour the page.

``shared.settings.get`` serves the last snapshot and the compiled default before the first refresh, so a read never
blocks the endpoint and never fails. The keys equal the CIS 475 seed (tests/integration/test_aa1140_migration_475.py and
the settings-lint reader check, tests/unit/test_s279_settings_lint.py).
"""
from __future__ import annotations

from shared import ops_thresholds, settings

ERROR_RATE_WARN_PCT = "cis.ops.status_error_rate_warn_pct"
ERROR_RATE_CRIT_PCT = "cis.ops.status_error_rate_crit_pct"
CLIENT_ERROR_WARN_PCT = "cis.ops.status_client_error_warn_pct"
P95_WARN_MS = "cis.ops.status_p95_warn_ms"
P95_CRIT_MS = "cis.ops.status_p95_crit_ms"
LAMBDA_P95_LIMIT_MS = "cis.ops.status_lambda_p95_limit_ms"
MIN_REQUESTS = "cis.ops.status_min_requests"
QUEUE_WAIT_WARN_MIN = "cis.ops.status_queue_wait_warn_min"
QUEUE_WAIT_CRIT_MIN = "cis.ops.status_queue_wait_crit_min"
RDS_CPU_WARN_PCT = "cis.ops.status_rds_cpu_warn_pct"
RDS_CPU_CRIT_PCT = "cis.ops.status_rds_cpu_crit_pct"
RDS_CONNECTIONS_WARN_PCT = "cis.ops.status_rds_connections_warn_pct"
RDS_CONNECTIONS_CRIT_PCT = "cis.ops.status_rds_connections_crit_pct"
ROLE_CONNECTIONS_CRIT_PCT = "cis.ops.status_role_connections_crit_pct"
RDS_FREE_STORAGE_WARN_GIB = "cis.ops.status_rds_free_storage_warn_gib"
RDS_FREE_STORAGE_CRIT_GIB = "cis.ops.status_rds_free_storage_crit_gib"
# CIS 477: free storage as a share of the allocated storage; the GiB pair above is the fallback (size unknown)
RDS_FREE_WARN_PCT = "cis.ops.status_rds_free_warn_pct"
RDS_FREE_CRIT_PCT = "cis.ops.status_rds_free_crit_pct"
XID_AGE_CRIT = "cis.ops.status_xid_age_crit"
UPTIME_SLO_PCT = "cis.ops.status_uptime_slo_pct"
STALE_FACTOR = "cis.ops.status_stale_factor"
HISTORY_DAYS = "cis.ops.status_history_days"
GUARD_STALE_HOURS = "cis.ops.status_guard_stale_hours"

ALL = (ERROR_RATE_WARN_PCT, ERROR_RATE_CRIT_PCT, CLIENT_ERROR_WARN_PCT, P95_WARN_MS, P95_CRIT_MS, LAMBDA_P95_LIMIT_MS,
       MIN_REQUESTS, QUEUE_WAIT_WARN_MIN, QUEUE_WAIT_CRIT_MIN, RDS_CPU_WARN_PCT, RDS_CPU_CRIT_PCT,
       RDS_CONNECTIONS_WARN_PCT, RDS_CONNECTIONS_CRIT_PCT, ROLE_CONNECTIONS_CRIT_PCT, RDS_FREE_STORAGE_WARN_GIB,
       RDS_FREE_STORAGE_CRIT_GIB, RDS_FREE_WARN_PCT, RDS_FREE_CRIT_PCT, XID_AGE_CRIT, UPTIME_SLO_PCT, STALE_FACTOR,
       HISTORY_DAYS, GUARD_STALE_HOURS)


def _num(key: str) -> float:
    return float(settings.get(key))


def _pair(warn_key: str, crit_key: str, *, below: bool = False) -> dict:
    """{warn, crit, warn_key, crit_key, dir}: a critical value on the wrong side of its warn is moved to the warn
    value (the stricter of the two wins), so a pair edited out of order never hides the warn line."""
    warn, crit = _num(warn_key), _num(crit_key)
    crit = min(crit, warn) if below else max(crit, warn)
    return {"warn": warn, "crit": crit, "warn_key": warn_key, "crit_key": crit_key,
            "dir": "below" if below else "above"}


def error_rate() -> dict:
    return _pair(ERROR_RATE_WARN_PCT, ERROR_RATE_CRIT_PCT)


def p95_ms() -> dict:
    return _pair(P95_WARN_MS, P95_CRIT_MS)


def queue_wait_min() -> dict:
    return _pair(QUEUE_WAIT_WARN_MIN, QUEUE_WAIT_CRIT_MIN)


def rds_cpu_pct() -> dict:
    return _pair(RDS_CPU_WARN_PCT, RDS_CPU_CRIT_PCT)


def rds_connections_pct() -> dict:
    return _pair(RDS_CONNECTIONS_WARN_PCT, RDS_CONNECTIONS_CRIT_PCT)


def rds_free_storage_gib() -> dict:
    """The deprecated fallback (CIS 475): fixed GiB limits, used only while the allocated storage is unknown."""
    return _pair(RDS_FREE_STORAGE_WARN_GIB, RDS_FREE_STORAGE_CRIT_GIB, below=True)


def rds_free_storage_pct() -> dict:
    """Free storage limits as a share (%) of the allocated storage (CIS 477)."""
    return _pair(RDS_FREE_WARN_PCT, RDS_FREE_CRIT_PCT, below=True)


def rds_free_storage(allocated_gib: float | None) -> dict:
    """The free storage limits in GiB for a volume of ``allocated_gib``: the CIS 477 share of it (``basis`` pct, with
    ``warn_pct`` / ``crit_pct`` and ``allocated_gib`` so the page can say "20 % = 4 GiB"), or the CIS 475 GiB pair
    (``basis`` gib) while the size is unknown. A share follows the volume as storage autoscaling grows it."""
    if not allocated_gib or allocated_gib <= 0:
        return {**rds_free_storage_gib(), "basis": "gib"}
    pct = rds_free_storage_pct()
    size = float(allocated_gib)
    return {"warn": round(size * pct["warn"] / 100.0, 2), "crit": round(size * pct["crit"] / 100.0, 2),
            "warn_pct": pct["warn"], "crit_pct": pct["crit"], "allocated_gib": size,
            "warn_key": RDS_FREE_WARN_PCT, "crit_key": RDS_FREE_CRIT_PCT, "dir": "below", "basis": "pct"}


def role_connections_pct() -> dict:
    """Warn = the in-app check value (cis.ops.role_connections_pct, CIS 469), critical = this page's value."""
    warn = ops_thresholds.role_connections_pct()
    crit = max(_num(ROLE_CONNECTIONS_CRIT_PCT), warn)
    return {"warn": warn, "crit": crit, "warn_key": ops_thresholds.ROLE_CONNECTIONS_PCT,
            "crit_key": ROLE_CONNECTIONS_CRIT_PCT, "dir": "above"}


def xid_age() -> dict:
    """Warn = the in-app check value (cis.ops.xid_age_limit, CIS 469), critical = this page's value."""
    warn = float(ops_thresholds.xid_age_limit())
    crit = max(_num(XID_AGE_CRIT), warn)
    return {"warn": warn, "crit": crit, "warn_key": ops_thresholds.XID_AGE_LIMIT, "crit_key": XID_AGE_CRIT,
            "dir": "above"}


def client_error_warn_pct() -> float:
    return _num(CLIENT_ERROR_WARN_PCT)


def lambda_p95_limit_ms() -> float:
    return _num(LAMBDA_P95_LIMIT_MS)


def min_requests() -> int:
    return int(settings.get(MIN_REQUESTS))


def uptime_slo_pct() -> float:
    return _num(UPTIME_SLO_PCT)


def stale_factor() -> float:
    return _num(STALE_FACTOR)


def history_days() -> int:
    return int(settings.get(HISTORY_DAYS))


def guard_stale_hours() -> float:
    return _num(GUARD_STALE_HOURS)


def snapshot() -> dict:
    """Every limit the page draws, in one dict (the endpoint's ``thresholds`` block; the page links each key to
    Platform › Settings)."""
    return {
        "error_rate_pct": error_rate(),
        "client_error_warn_pct": {"warn": client_error_warn_pct(), "warn_key": CLIENT_ERROR_WARN_PCT, "dir": "above"},
        "p95_ms": p95_ms(),
        "lambda_p95_limit_ms": {"warn": lambda_p95_limit_ms(), "warn_key": LAMBDA_P95_LIMIT_MS, "dir": "above"},
        "min_requests": {"value": min_requests(), "key": MIN_REQUESTS},
        "queue_wait_min": queue_wait_min(),
        "rds_cpu_pct": rds_cpu_pct(),
        "rds_connections_pct": rds_connections_pct(),
        "role_connections_pct": role_connections_pct(),
        "rds_free_storage_gib": rds_free_storage_gib(),
        "rds_free_storage_pct": rds_free_storage_pct(),
        "xid_age": xid_age(),
        "uptime_slo_pct": {"value": uptime_slo_pct(), "key": UPTIME_SLO_PCT},
        "stale_factor": {"value": stale_factor(), "key": STALE_FACTOR},
        "history_days": {"value": history_days(), "key": HISTORY_DAYS},
        "guard_stale_hours": {"value": guard_stale_hours(), "key": GUARD_STALE_HOURS},
        "app_spend_near_pct": {"warn": ops_thresholds.app_spend_near_ratio() * 100.0,
                               "warn_key": ops_thresholds.APP_SPEND_NEAR_PCT, "dir": "above"},
    }
