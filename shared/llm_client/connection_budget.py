"""AA-1002 (gateway slice S10) — the monthly budget of each LLM connection, on the call path.

``stops(connection)`` answers "leave this connection out of the route now?" for ``vendor_route.deployment_plan`` (and
the S2 ``vendor_deployment_for``). ``check(connection_key, what)`` is the same rule for the gateway's direct paths, the
ones that call a connection without a plan: the hard-coded legacy chain (``haiku`` / ``sonnet`` on acc3 / acc2, the
``gpt-4.1`` tier and the T3 last resort on openai-direct), and the catalog path with deployment routing off or no
registry plan (Converse on the satellite account, ``openai_chat`` on openai-direct). It raises the budget stop before
anything is sent. The rules and the SQL live in ``shared/cost_guard.py`` (``ConnectionBudget``); this
module only caches them for the sync gateway:

- A connection without ``monthly_budget_usd`` is never read or enforced (every connection today: the seed rows have no
  budget), so a route over such connections makes no database call here at all.
- The state of the connections that have a budget is loaded in one query pair (policies + month-to-date spend) and
  kept ``CACHE_TTL_SECONDS``; a spend that crosses the cap is seen within that window (calls already running finish).
- Loading also raises the soft warning: ``cost_guard.maybe_alert_connection`` (one admin bell + ops e-mail per
  connection per 24 h) for each connection at or above its ``alert_pct``.
- Never raises. When the state cannot be read (DB down, migration 317 not applied and the spend query failing too) the
  budget is NOT enforced (logged once per failure window): the same DB outage also stops the cost log, and failing
  closed would take every budgeted connection out of service on a database blip. A missing 317 table alone is not a
  failure (the policy defaults apply: alert 80 %, hard stop).
- ``LLM_CONNECTION_BUDGETS=off`` (ECS env, read per call) switches enforcement and alerts off.

AA-1126 G1 (S283): what a reached cap does is the environment's setting ``cis.llm.connection_cap_mode``
(``caps_settings``). ``hard_stop`` (the default, Dev) is the rule above. ``failover_continue`` (prod) keeps leaving an
over-cap connection out while another rung can answer; when the request still fails and a connection cap took part
(``budget_keys``), ``run_with_cap_mode`` raises one alert (bell + ops e-mail + log, once per 24 h) and runs the request
again with the caps lifted for it (``caps_lifted``: ``stops`` answers None), so the call goes to the first healthy rung
and a customer is never refused because of a connection cap.
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator, Optional, TypeVar

import structlog

from shared import cost_guard

from . import caps_settings, connections

logger = structlog.get_logger()

SWITCH_ENV = "LLM_CONNECTION_BUDGETS"
CACHE_TTL_SECONDS = 60.0  # technical-constant: in-process cache refresh of a read
_FAILURE_TTL_SECONDS = 60.0  # technical-constant: back-off after a failed cache read

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
    """The budget state when ``connection`` must be left out of routes now (hard stop reached), else None. None as well
    while the caps are lifted for this request (AA-1126 G1, ``failover_continue``)."""
    if connection.monthly_budget_usd is None or _LIFTED.get():
        return None
    b = budget_of(connection.connection_key)
    return b if b is not None and b.stops else None


def check(connection_key: str, what: str) -> None:
    """Raise ConnectionBudgetExceeded when the registry row of ``connection_key`` has a hard monthly budget it reached,
    before a direct path (no deployment plan) calls it; ``what`` names the model key for the error. A connection with no
    row (registry unreadable, no such row: the built-in mirror has no budget) or no budget is never read. Raises
    nothing else."""
    try:
        conn = connections.get_connection_sync(connection_key)
    except Exception as e:  # noqa: BLE001 — get_connection_sync never raises; belt and braces on the call path
        logger.warning("llm_connection_budget_registry_failed", error=str(e)[:200])
        return
    if conn is None:
        return
    over = stops(conn)
    if over is not None:
        logger.info("llm_direct_call_skip", model=what, connection=connection_key, reason="monthly budget reached")
        raise budget_error(what, [(connection_key, over)])


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


# ── AA-1126 G1: failover_continue (the connection cap never refuses the call) ────────────────────────────────────────

_LIFTED: ContextVar[bool] = ContextVar("llm_connection_caps_lifted", default=False)
CONTINUE_SCOPE = "connection_continue"
T = TypeVar("T")


@contextmanager
def caps_lifted() -> Iterator[None]:
    """While bound, no connection is left out for its budget (``stops`` answers None, ``check`` passes)."""
    token = _LIFTED.set(True)
    try:
        yield
    finally:
        _LIFTED.reset(token)


def budget_keys(exc: BaseException) -> tuple:
    """The connections a failed request left out for their monthly budget: the keys of a ConnectionBudgetExceeded in
    ``exc``'s cause chain or among its route failures (``route_failures``, a mixed failure). () when none. Pure."""
    keys: list = []
    cur, seen = exc, 0
    while cur is not None and seen < 16:
        found = [cur] + list(getattr(cur, "route_failures", None) or ())
        keys += [k for f in found if isinstance(f, ConnectionBudgetExceeded) for k in f.connection_keys]
        cur, seen = cur.__cause__ or cur.__context__, seen + 1
    return tuple(dict.fromkeys(keys))


def run_with_cap_mode(call: Callable[[], T], stage: Optional[str]) -> T:
    """Run ``call`` (one gateway request: its whole route). In ``failover_continue`` mode a failure in which a
    connection cap took part is alerted and ``call`` runs once more with the caps lifted; otherwise the failure is
    raised as it is (``hard_stop``: today's budget stop)."""
    try:
        return call()
    except Exception as e:
        keys = budget_keys(e)
        if not keys or _LIFTED.get() or caps_settings.connection_cap_mode() != caps_settings.FAILOVER_CONTINUE:
            raise
    logger.warning("llm_connection_caps_lifted", stage=stage, connections=list(keys),
                   hint="every rung was over its connection cap or failed: the call goes on (failover_continue)")
    alert_continue(keys, stage)
    with caps_lifted():
        return call()


def continue_message(keys: tuple, stage: Optional[str]) -> str:
    return (f"LLM connection caps reached ({', '.join(keys)}) and no other way in answered for stage "
            f"{stage or 'unstaged'}: the call went on with the caps lifted (connection cap mode failover_continue). "
            "Raise the connection budgets or check the spend.")


async def _alert_continue(keys: tuple, stage: Optional[str]) -> None:
    conn = await connections._connect()
    try:
        summary = {"provider": "llm", "scope": CONTINUE_SCOPE, "connection_keys": list(keys), "stage": stage,
                   "hard_stop": False, "hard_stop_day": False}
        await cost_guard.maybe_alert_central(conn, summary, alert_pct=100, stopped=False,
                                             message=continue_message(keys, stage))
    finally:
        await conn.close()


def send_continue(keys: tuple, stage: Optional[str]) -> None:
    """Write the bell (tests replace it; no database in unit tests)."""
    connections.run_sync(lambda: _alert_continue(keys, stage))


def alert_continue(keys: tuple, stage: Optional[str]) -> None:
    """One admin bell + ops e-mail per 24 h (scope llm:connection_continue). Best effort: never raises."""
    try:
        send_continue(keys, stage)
    except Exception as e:  # noqa: BLE001 — the call goes on whatever the bell does
        logger.warning("llm_connection_continue_alert_failed", error=str(e)[:200])
