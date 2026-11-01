"""AA-1016 — the monthly AI budget of a Content studio tenant, on the gateway's call path.

``check(stage)`` runs in ``LLMClient._generate_with`` before any attempt. It reads the tenant bound to the call
(``call_log.current_tenant_id``: the job's tenant, AA-741) and the tenant's row in ``shared.tenant_ai_budget``
(migration 321; the rules and the SQL are ``shared.cost_guard.TenantAIBudget`` / ``tenant_budget_check``):

- No tenant bound, or a tenant without a budget row (every tenant today): nothing else is read and nothing changes.
  The set of budgeted tenants is one cheap query per ``TENANTS_TTL_SECONDS`` per process; before migration 321 (no
  table) it is empty.
- At or above ``alert_pct``: one admin bell + the ops e-mail (``cost_guard.maybe_alert_tenant``, at most one unread
  bell per tenant per 24 h). Calls go on.
- At or above ``approval_pct`` with ``hard_stop`` on, for a **T-stage** call (the Content studio pipeline T0-T11:
  ``t<digit>…`` stage keys): ``TenantBudgetApprovalRequired``. It is a ``BudgetExceeded``, so the job runner ends the
  job ``stopped_budget`` with an error that starts with ``APPROVAL_REASON`` (no retry, nothing sent). That is the wait:
  staff approve by raising the tenant's budget or switching its hard stop off, then re-queue the held jobs
  (``POST /admin/job-runner/jobs/retry-llm-denied`` with ``reason: tenant_ai_budget_approval``, or the one-job Retry);
  the bulk re-queue keeps a job held while its tenant still needs approval (``shared/jobs/queue.py``). No silent
  cut-off: the stop always comes with the bell naming the tenant and its spend. Platform (A-stage) calls are never
  stopped by a tenant budget.
- The state of a budgeted tenant is cached ``CACHE_TTL_SECONDS``. AA-642 A8 (gap CIS-3): when the tenant is known to
  have a budget (the budgeted-tenant set was read) but its spend counter cannot be read, a **T-stage** call fails
  closed for ``FAILURE_TTL_SECONDS``: ``TenantBudgetUnavailable`` (a 0 USD ledger row, status refused, class budget;
  a plain RuntimeError, so the job retries with its normal backoff, never ``stopped_budget``). The T2 graph, the T2
  rewrite and the T9 write re-raise it like the approval stop (``counter_unavailable_in``), so it reaches the job
  runner as a retryable failure instead of a failed draft or piece (review S263). A T-stage call re-reads a failed
  counter after ``UNREADABLE_RECHECK_SECONDS`` (shorter than the job runner's first retry backoff), so the retry
  attempt reads it again instead of failing on the cached failure; an A-stage call keeps the ``FAILURE_TTL_SECONDS``
  cache. Exception: the last state read for that tenant had ``hard_stop`` off (the budget only rings a bell): then it
  fails open, logged. When the budgeted-tenant set itself was never readable (no database at all), nothing is known
  and the check fails open as before. Platform (A-stage) calls always fail open. Nothing else is raised.
- ``LLM_TENANT_BUDGETS=off`` (ECS env, read per call) switches the check off.
"""
from __future__ import annotations

import os
import re
import threading
import time
import uuid
from typing import Optional

import structlog

from shared import cost_guard

from . import connections

logger = structlog.get_logger()

SWITCH_ENV = "LLM_TENANT_BUDGETS"
APPROVAL_REASON = "tenant_ai_budget_approval"
TENANTS_TTL_SECONDS = 60.0
CACHE_TTL_SECONDS = 60.0
FAILURE_TTL_SECONDS = 60.0
# AA-642 A8 (review S263): a T-stage call re-reads a failed counter after this many seconds. Must stay below the job
# runner's first retry backoff (shared.jobs.queue.BACKOFF_BASE_SECONDS, 30 s; a unit test pins it), so a retried job
# reads the counter again instead of burning its last attempt on the cached failure.
UNREADABLE_RECHECK_SECONDS = 15.0
MISSING_TABLE_TTL_SECONDS = 300.0
# The Content studio stages (T0-T11, ADR 0006): t2_generate, t5_atomize, t9_write, t10_judge ...
T_STAGE = re.compile(r"^t\d+_")

_tenants: Optional[frozenset] = None
_tenants_at = 0.0
_tenants_failed_at = 0.0
_state: dict[str, tuple[Optional[cost_guard.TenantAIBudget], float]] = {}
_failed_at: dict[str, float] = {}
_lock = threading.Lock()


class TenantBudgetApprovalRequired(cost_guard.BudgetExceeded, RuntimeError):
    """A T-stage call of a tenant at or past its approval threshold (hard stop on). A BudgetExceeded: the job ends
    ``stopped_budget`` (no retry); the message starts with APPROVAL_REASON and names the tenant, so the bulk re-queue
    finds it. A RuntimeError, so callers that catch the gateway's failures as before still do. Built by
    ``approval_required``."""

    tenant_id: Optional[str] = None
    budget: Optional[cost_guard.TenantAIBudget] = None


UNREADABLE_REASON = "tenant_ai_budget_unreadable"


class TenantBudgetUnavailable(RuntimeError):
    """AA-642 A8: a T-stage call of a budgeted tenant whose spend counter cannot be read (fail closed, module
    docstring). Not a BudgetExceeded: the job retries with its normal backoff instead of waiting for staff approval.
    The message starts with UNREADABLE_REASON and names the tenant."""

    tenant_id: Optional[str] = None


def counter_unavailable(tenant_id: str) -> TenantBudgetUnavailable:
    exc = TenantBudgetUnavailable(f"{UNREADABLE_REASON}: Tenant {tenant_id} has an AI budget but its spend this month "
                                  "could not be read; T-stage AI calls wait until it can (the job retries)")
    exc.tenant_id = tenant_id
    return exc


def counter_unavailable_in(exc: Optional[BaseException]) -> Optional[TenantBudgetUnavailable]:
    """AA-642 A8 (review S263): the ``TenantBudgetUnavailable`` in ``exc`` or its explicit cause chain, else None. The
    T-stage pipelines re-raise it (as they do ``approval_stop_in``'s stop) so the job runner retries the job instead of
    the pipeline turning the refusal into a failed draft / piece."""
    from .errors import _chain  # pure, import-light

    if exc is None:
        return None
    for link in _chain(exc, implicit=False):
        if isinstance(link, TenantBudgetUnavailable):
            return link
    return None


def approval_required(budget: cost_guard.TenantAIBudget) -> TenantBudgetApprovalRequired:
    exc = TenantBudgetApprovalRequired(f"{APPROVAL_REASON}: {cost_guard.tenant_budget_message(budget)}",
                                       "tenant_ai", "approval")
    exc.tenant_id = budget.tenant_id
    exc.budget = budget
    return exc


def enabled() -> bool:
    return (os.environ.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


def is_t_stage(stage: Optional[str]) -> bool:
    return bool(stage) and T_STAGE.match(stage) is not None


# What the tenant's live progress view says while a job waits for staff approval (plain words: no budget figures,
# no stage names; the tenant sees content, the bell tells staff why).
ON_HOLD_MESSAGE = "Writing is on hold and will continue automatically. No need to try again."


def approval_stop_in(exc: Optional[BaseException]) -> Optional[TenantBudgetApprovalRequired]:
    """The ``TenantBudgetApprovalRequired`` in ``exc`` or its explicit cause chain (``raise ... from``), else None.
    Pipelines that turn the gateway's failures into a failed result re-raise this one (review S262: the T2 graph and
    the T9 write swallowed it, so the job never ended ``stopped_budget``); the worker reads the chain too."""
    from .errors import _chain  # pure, import-light

    if exc is None:
        return None
    for link in _chain(exc, implicit=False):
        if isinstance(link, TenantBudgetApprovalRequired):
            return link
    return None


def is_approval_stop(job_row: Optional[dict]) -> bool:
    """True for a job that waits for staff approval: ended ``stopped_budget`` by ``TenantBudgetApprovalRequired``.
    The T-stage ``on_terminal`` hooks leave their piece / version running for such a job, so the approval re-queue
    (``queue.retry_by_reason``) finds it still to do; an admin Cancel of the job (decline) ends it ``cancelled`` and
    the hook then marks it failed as for any other stop."""
    if not job_row:
        return False
    return job_row.get("status") == "stopped_budget" and (job_row.get("error") or "").startswith(APPROVAL_REASON)


def tenant_of_error(error: Optional[str]) -> Optional[str]:
    """The tenant UUID named in a stored approval-stop job error (``TenantBudgetApprovalRequired``), else None."""
    if not error or not error.startswith(APPROVAL_REASON):
        return None
    m = re.search(r"Tenant ([0-9a-fA-F-]{36})", error)
    return m.group(1).lower() if m else None


def _bound_tenant() -> Optional[str]:
    from .call_log import current_tenant_id
    tid = current_tenant_id()
    if not tid:
        return None
    try:
        return str(uuid.UUID(str(tid)))
    except ValueError:
        return None


async def _load_tenants() -> Optional[frozenset]:
    conn = await connections._connect()
    try:
        return await cost_guard.budgeted_tenants(conn)
    finally:
        await conn.close()


async def _load_state(tenant_id: str) -> Optional[cost_guard.TenantAIBudget]:
    conn = await connections._connect()
    try:
        budget = await cost_guard.tenant_budget_check(conn, tenant_id)
        if budget is not None and (budget.alerting or budget.needs_approval):
            await cost_guard.maybe_alert_tenant(conn, budget)
        return budget
    finally:
        await conn.close()


def _budgeted(tenant_id: str) -> bool:
    """Whether ``tenant_id`` has a budget row (the cached set; one query per TENANTS_TTL_SECONDS)."""
    global _tenants, _tenants_at, _tenants_failed_at
    now = time.monotonic()
    fresh = _tenants_at and now - _tenants_at < (TENANTS_TTL_SECONDS if _tenants is not None
                                                  else MISSING_TABLE_TTL_SECONDS)
    if not fresh and not (_tenants_failed_at and now - _tenants_failed_at < FAILURE_TTL_SECONDS):
        with _lock:
            try:
                _tenants = loader_tenants()
                _tenants_at, _tenants_failed_at = time.monotonic(), 0.0
            except Exception as e:  # noqa: BLE001 — fail open, see the module docstring
                _tenants_failed_at = time.monotonic()
                logger.warning("tenant_ai_budget_read_failed", error=str(e)[:300],
                               hint="tenant AI budgets are not enforced until the table can be read")
    return bool(_tenants) and tenant_id in _tenants


def budget_of(tenant_id: str, *, recheck_after: float = FAILURE_TTL_SECONDS) -> Optional[cost_guard.TenantAIBudget]:
    """The cached budget state of a budgeted tenant (None: no budget, switched off, or unreadable). Never raises. A
    failed read is not retried for ``recheck_after`` seconds (``check`` passes UNREADABLE_RECHECK_SECONDS for a
    T-stage call)."""
    if not enabled() or not _budgeted(tenant_id):
        return None
    now = time.monotonic()
    cached = _state.get(tenant_id)
    if cached is not None and now - cached[1] < CACHE_TTL_SECONDS:
        return cached[0]
    failed = _failed_at.get(tenant_id)
    if failed and now - failed < min(recheck_after, FAILURE_TTL_SECONDS):
        return None
    try:
        budget = loader_state(tenant_id)
        _state[tenant_id] = (budget, time.monotonic())
        _failed_at.pop(tenant_id, None)
        return budget
    except Exception as e:  # noqa: BLE001 — fail open
        _failed_at[tenant_id] = time.monotonic()
        logger.warning("tenant_ai_budget_read_failed", tenant_id=tenant_id, error=str(e)[:300])
        return None


def counter_unreadable(tenant_id: str) -> bool:
    """AA-642 A8: True when ``tenant_id`` is known to have a budget and its last state read failed less than
    FAILURE_TTL_SECONDS ago, unless the last state read before that said its hard stop is off (or it has no amount).
    Reads nothing (the caches only)."""
    if not enabled() or not (_tenants and tenant_id in _tenants):
        return False
    failed = _failed_at.get(tenant_id)
    if not failed or time.monotonic() - failed >= FAILURE_TTL_SECONDS:
        return False
    last = _state.get(tenant_id)
    if last is not None and (last[0] is None or not last[0].hard_stop):
        return False
    return True


def check(stage: Optional[str]) -> Optional[cost_guard.TenantAIBudget]:
    """The gateway's check before any attempt (module docstring). Returns the tenant's budget state (None without a
    budget); raises TenantBudgetApprovalRequired for a T-stage call of a tenant waiting for approval, and
    TenantBudgetUnavailable for a T-stage call of a budgeted tenant whose spend cannot be read (AA-642 A8)."""
    unreadable = False
    try:
        tenant_id = _bound_tenant()
        if tenant_id is None:
            return None
        t_stage = is_t_stage(stage)
        budget = budget_of(tenant_id,
                           recheck_after=UNREADABLE_RECHECK_SECONDS if t_stage else FAILURE_TTL_SECONDS)
        unreadable = budget is None and counter_unreadable(tenant_id)
    except Exception as e:  # noqa: BLE001 — never on the call path
        logger.warning("tenant_ai_budget_check_failed", stage=stage, error=str(e)[:200])
        return None
    if unreadable and is_t_stage(stage):
        logger.warning("tenant_ai_budget_unreadable_fail_closed", stage=stage, tenant_id=tenant_id)
        raise counter_unavailable(tenant_id)
    if budget is not None and budget.needs_approval and is_t_stage(stage):
        logger.warning("tenant_ai_budget_approval_wait", stage=stage, **budget.summary())
        raise approval_required(budget)
    return budget


def _load_tenants_sync() -> Optional[frozenset]:
    return connections.run_sync(_load_tenants)


def _load_state_sync(tenant_id: str) -> Optional[cost_guard.TenantAIBudget]:
    return connections.run_sync(lambda: _load_state(tenant_id))


# Tests replace them (conftest: no database in unit tests).
loader_tenants = _load_tenants_sync
loader_state = _load_state_sync


def invalidate() -> None:
    global _tenants, _tenants_at, _tenants_failed_at
    _tenants, _tenants_at, _tenants_failed_at = None, 0.0, 0.0
    _state.clear()
    _failed_at.clear()
