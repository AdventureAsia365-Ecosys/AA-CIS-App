"""AA-1126 (S283) — the settings of the gateway's spend controls (CIS migration 446, Platform › Settings, area Spend).

Tech lead policy S283: Dev keeps hard caps on the AI connections; prod is pay per use, where a connection cap only
alerts and fails over, only an anomaly breaker may stop calls, and internal batch runs keep a hard cap per run. One
code path for every environment: each environment's own settings registry (its own database) picks the behaviour, so
going to prod is a settings change, not new code. The defaults are today's Dev behaviour.

- ``cis.llm.connection_cap_mode`` (G1): ``hard_stop`` (default: a connection over its monthly budget is left out of
  every route and a model / route with no other way in is a budget stop) or ``failover_continue`` (it is still left out
  while another rung can answer; when every rung is over its cap the call goes on, caps lifted, with an alert).
  A connection whose own cost policy says alert only (``llm_connection_cost_policy.hard_stop`` false) stays alert only.
- ``cis.llm.anomaly_breaker_mode`` / ``_multiple`` / ``_floor_usd`` / ``_baseline_days`` (G2): the anomaly breaker
  (``anomaly_breaker.py``). ``off``, ``alert`` (default: one bell + e-mail, nothing refused) or ``enforce`` (the app's
  new calls are refused while it is tripped).
- ``cis.spend.run_cap_s1_rewrite_usd`` / ``_eval_usd`` / ``_adhoc_usd`` (G4): the hard cap of one run when the job
  payload carries no ``cap_usd`` (``run_cap.py``).

Every read goes through ``shared.settings`` (the snapshot the API and worker refresh; else the compiled defaults). A key
the compiled defaults do not have yet (the code shipped before migration 446's PR) reads as "not seeded": the control
keeps today's behaviour (hard stop, breaker off, no run cap). Never raises.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import structlog

logger = structlog.get_logger()

CONNECTION_CAP_MODE = "cis.llm.connection_cap_mode"
ANOMALY_MODE = "cis.llm.anomaly_breaker_mode"
ANOMALY_MULTIPLE = "cis.llm.anomaly_multiple"
ANOMALY_FLOOR_USD = "cis.llm.anomaly_floor_usd"
ANOMALY_BASELINE_DAYS = "cis.llm.anomaly_baseline_days"
RUN_CAP_S1_REWRITE_USD = "cis.spend.run_cap_s1_rewrite_usd"
RUN_CAP_EVAL_USD = "cis.spend.run_cap_eval_usd"
RUN_CAP_ADHOC_USD = "cis.spend.run_cap_adhoc_usd"

HARD_STOP = "hard_stop"
FAILOVER_CONTINUE = "failover_continue"
CAP_MODES = (HARD_STOP, FAILOVER_CONTINUE)

OFF = "off"
ALERT = "alert"
ENFORCE = "enforce"
ANOMALY_MODES = (OFF, ALERT, ENFORCE)

# The run classes with a hard cap per run (G4) and the setting of each one's default cap.
RUN_CAP_KEYS = {"s1_rewrite": RUN_CAP_S1_REWRITE_USD, "eval": RUN_CAP_EVAL_USD, "adhoc": RUN_CAP_ADHOC_USD}

ALL = (CONNECTION_CAP_MODE, ANOMALY_MODE, ANOMALY_MULTIPLE, ANOMALY_FLOOR_USD, ANOMALY_BASELINE_DAYS,
       RUN_CAP_S1_REWRITE_USD, RUN_CAP_EVAL_USD, RUN_CAP_ADHOC_USD)


def _get(key: str) -> Any:
    """The value in force of ``key``, or None when the key is not seeded yet or cannot be read. Never raises."""
    try:
        from shared import settings
        return settings.get(key)
    except KeyError:
        return None
    except Exception as e:  # noqa: BLE001 — a settings problem never breaks the call path
        logger.warning("llm_caps_setting_read_failed", key=key, error=str(e)[:200])
        return None


def _number(key: str) -> Optional[float]:
    v = _get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def connection_cap_mode() -> str:
    """G1: ``hard_stop`` (default, and whenever the value cannot be read) or ``failover_continue``."""
    v = _get(CONNECTION_CAP_MODE)
    return v if v in CAP_MODES else HARD_STOP


def anomaly_mode() -> str:
    """G2: ``off`` (also when not seeded or unreadable), ``alert`` or ``enforce``."""
    v = _get(ANOMALY_MODE)
    return v if v in ANOMALY_MODES else OFF


@dataclass(frozen=True)
class AnomalyPolicy:
    multiple: float
    floor_usd: float
    baseline_days: int


def anomaly_policy() -> Optional[AnomalyPolicy]:
    """G2: the trip rule in force, or None when one of its settings cannot be read (the breaker then does nothing)."""
    multiple, floor, days = _number(ANOMALY_MULTIPLE), _number(ANOMALY_FLOOR_USD), _number(ANOMALY_BASELINE_DAYS)
    if multiple is None or floor is None or days is None:
        return None
    return AnomalyPolicy(multiple=multiple, floor_usd=floor, baseline_days=int(days))


def run_cap_usd(run_class: Optional[str]) -> Optional[float]:
    """G4: the default hard cap of one run of ``run_class`` (``s1_rewrite`` / ``eval`` / ``adhoc``), or None."""
    key = RUN_CAP_KEYS.get(run_class or "")
    return _number(key) if key else None


__all__ = ["ALERT", "ALL", "ANOMALY_BASELINE_DAYS", "ANOMALY_FLOOR_USD", "ANOMALY_MODE", "ANOMALY_MODES",
           "ANOMALY_MULTIPLE", "AnomalyPolicy", "CAP_MODES", "CONNECTION_CAP_MODE", "ENFORCE", "FAILOVER_CONTINUE",
           "HARD_STOP", "OFF", "RUN_CAP_ADHOC_USD", "RUN_CAP_EVAL_USD", "RUN_CAP_KEYS", "RUN_CAP_S1_REWRITE_USD",
           "anomaly_mode", "anomaly_policy", "connection_cap_mode", "run_cap_usd"]
