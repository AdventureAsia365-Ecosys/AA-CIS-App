"""AA-1002 (gateway slice S10) — the monthly budget of each LLM connection, on the call path.

``stops(connection)`` answers "leave this connection out of the route now?" for ``vendor_route.deployment_plan`` (and
the S2 ``vendor_deployment_for``). The rules and the SQL live in ``shared/cost_guard.py`` (``ConnectionBudget``); this
module only caches them for the sync gateway:

- A connection without ``monthly_budget_usd`` is never read or enforced (every connection today: the seed rows have no
  budget), so a route over such connections makes no database call here at all.
- The state of the connections that have a budget is loaded in one query pair (policies + month-to-date spend) and
  kept ``CACHE_TTL_SECONDS``; a spend that crosses the cap is seen within that window (calls already running finish).
- Loading also raises the soft warning: ``cost_guard.maybe_alert_connection`` (one admin bell + ops e-mail per
  connection per 24 h) for each connection at or above its ``alert_pct``.
- Never raises. When the state cannot be read (DB down, migration 316 not applied and the spend query failing too) the
  budget is NOT enforced (logged once per failure window): the same DB outage also stops the cost log, and failing
  closed would take every budgeted connection out of service on a database blip. A missing 316 table alone is not a
  failure (the policy defaults apply: alert 80 %, hard stop).
- ``LLM_CONNECTION_BUDGETS=off`` (ECS env, read per call) switches enforcement and alerts off.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

import structlog

from shared import cost_guard

from . import connections

logger = structlog.get_logger()

SWITCH_ENV = "LLM_CONNECTION_BUDGETS"
CACHE_TTL_SECONDS = 60.0
_FAILURE_TTL_SECONDS = 60.0

_state: dict[str, cost_guard.ConnectionBudget] = {}
_loaded_for: tuple = ()
_loaded_at = 0.0
_failed_at = 0.0
_lock = threading.Lock()


def enabled() -> bool:
    return (os.environ.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


def _capped(rows: dict) -> tuple:
    """((connection_key, monthly_budget_usd), ...) of the registry rows that have a budget, sorted."""
    return tuple(sorted((k, float(c.monthly_budget_usd)) for k, c in rows.items()
                        if c.monthly_budget_usd is not None))


async def _load(capped: tuple) -> dict:
    conn = await connections._connect()
    try:
        budgets = await cost_guard.connection_budgets(conn, dict(capped))
        for b in budgets.values():
            if b.alerting:
                await cost_guard.maybe_alert_connection(conn, b)
        return budgets
    finally:
        await conn.close()


def _fresh(capped: tuple, now: float) -> bool:
    if capped != _loaded_for:
        return False
    return bool((_loaded_at and now - _loaded_at < CACHE_TTL_SECONDS)
                or (_failed_at and now - _failed_at < _FAILURE_TTL_SECONDS))


def budget_of(connection_key: str) -> Optional[cost_guard.ConnectionBudget]:
    """The cached budget state of one connection (None: no budget, switched off, or unreadable). Never raises."""
    global _state, _loaded_for, _loaded_at, _failed_at
    if not enabled():
        return None
    try:
        capped = _capped(connections.registry_rows_sync())
    except Exception as e:  # noqa: BLE001 — registry_rows_sync never raises; belt and braces on the call path
        logger.warning("llm_connection_budget_registry_failed", error=str(e)[:200])
        return None
    if not capped or connection_key not in dict(capped):
        return None
    now = time.monotonic()
    if not _fresh(capped, now):
        with _lock:
            now = time.monotonic()
            if not _fresh(capped, now):
                try:
                    _state = connections.run_sync(lambda: _load(capped))
                    _loaded_for, _loaded_at, _failed_at = capped, now, 0.0
                except Exception as e:  # noqa: BLE001 — fail open, see the module docstring
                    _state, _loaded_for, _loaded_at, _failed_at = {}, capped, 0.0, now
                    logger.warning("llm_connection_budget_read_failed", error=str(e)[:300],
                                   hint="connection budgets are not enforced until the state can be read")
    return _state.get(connection_key)


def stops(connection: connections.ProviderConnection) -> Optional[cost_guard.ConnectionBudget]:
    """The budget state when ``connection`` must be left out of routes now (hard stop reached), else None."""
    if connection.monthly_budget_usd is None:
        return None
    b = budget_of(connection.connection_key)
    return b if b is not None and b.stops else None


def invalidate() -> None:
    global _loaded_at, _failed_at, _loaded_for
    _loaded_at = 0.0
    _failed_at = 0.0
    _loaded_for = ()


class ConnectionBudgetExceeded(cost_guard.BudgetExceeded, RuntimeError):
    """Every deployment a model (or a whole route) could use was left out because its connection reached its hard
    monthly budget. A ``BudgetExceeded``, so the job runner stops the job cleanly (status ``stopped_budget``) instead of
    retrying; a ``RuntimeError``, so callers that catch the gateway's failures as before still do. Built by
    ``exceeded`` (``connection_keys``: the connections that were left out)."""

    connection_keys: tuple = ()


def exceeded(message: str, connection_keys: tuple = ()) -> ConnectionBudgetExceeded:
    exc = ConnectionBudgetExceeded(message, "llm_connection", "per_month")
    exc.connection_keys = tuple(connection_keys)
    return exc


def budget_error(model_key: str, skips: list) -> ConnectionBudgetExceeded:
    keys = tuple(k for k, _ in skips)
    detail = "; ".join(f"{k}: ${b.month_spent_usd:.2f} of ${b.monthly_budget_usd:.2f}" for k, b in skips)
    return exceeded(f"{model_key!r} not called: every usable connection reached its monthly budget ({detail})", keys)
