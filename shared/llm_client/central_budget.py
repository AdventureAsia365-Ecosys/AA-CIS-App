"""AA-1025 A7 — the central LLM budgets, on the gateway's call path.

The caps are ``shared.spend_budget`` rows of provider ``llm`` (CIS migration 340): one cap counts every LLM provider
together (the spend of ``shared.llm_call_log``), whatever connection answers. Scopes:

- ``app:<app>``    the app's spend (``llm_call_log.app``; a NULL app is a CIS row from before ledger v2). Every call of
                   this gateway is in ``app:cis`` (``call_log.APP``).
- ``stage:<stage>`` the spend of one stage (the request's ``stage``).
- ``job:<kind>``   the spend of the jobs of one kind (rows whose ``job_id`` is a job of that kind); its ``per_run_usd``
                   is the spend of the running job (``call_log.current_job_id``, every attempt and retry of it).
- ``eval``         the eval runs: a call under an eval label (``call_log.bind_eval_stage``) or of the stage
                   ``writing_memory_eval``; spend = the rows of ``EVAL_STAGES`` (the labels eval rows are written
                   under).
- ``adhoc``        the ad-hoc scripts: a call whose stage (or eval label) starts with ``adhoc_``; spend = those rows.

Each row may cap the UTC day (``per_day_usd``), the UTC calendar month (``per_month_usd``) and, for ``job:<kind>``,
the run (``per_run_usd``). ``check(request_stage, estimate_usd)`` runs in ``LLMClient._generate_with`` after the stage
switch and the tenant budget, before any attempt:

- No ``llm`` row (every database before migration 340), or no row for the call's scopes: nothing more is read and
  nothing changes. The rows are one cheap query per ``ROWS_TTL_SECONDS`` per process.
- For each row that applies, the spend of its scope is read (one query, cached ``SPEND_TTL_SECONDS``; a spend that
  crosses the cap is seen within that window, calls already running finish). A row with ``hard_stop`` refuses the
  call when spent + ``estimate_usd`` (the worst case of the call: the route's first model, the prompt, the whole
  ``max_tokens``) would pass a cap: ``CentralBudgetExceeded`` (a ``BudgetExceeded``: the job ends ``stopped_budget``,
  nothing is sent; the gateway writes the 0 USD ledger row, status refused, class budget). A row without
  ``hard_stop`` never refuses (tech lead decision T-8: Dev caps are alert-only).
- At or above ``alert_pct`` of a cap (or a refusal): one admin bell + the ops e-mail per scope per 24 h
  (``cost_guard.maybe_alert_central``). Best effort.
- Never fails closed: when the rows or a spend cannot be read, the cap is not enforced (logged once per failure window)
  — the same DB outage also stops the cost log, and failing closed would stop every LLM call on a database blip.
- ``LLM_CENTRAL_BUDGETS=off`` (ECS env, read per call) switches the check and its alerts off.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog

from shared import cost_guard

from . import connections

logger = structlog.get_logger()

PROVIDER = "llm"
SWITCH_ENV = "LLM_CENTRAL_BUDGETS"
ROWS_TTL_SECONDS = 60.0  # technical-constant: in-process cache refresh of a read
SPEND_TTL_SECONDS = 30.0  # technical-constant: in-process cache refresh of a read
FAILURE_TTL_SECONDS = 60.0  # technical-constant: back-off after a failed cache read
JOB_KIND_TTL_SECONDS = 600.0  # technical-constant: in-process cache refresh of a read
# The llm_call_log labels eval rows are written under (services/writing_memory/config.py EVAL_LOG_STAGES + SHADOW_STAGE;
# a unit test keeps the two in step: shared/ must not import services/).
EVAL_STAGES = ("writing_memory_eval", "adhoc_aa900", "adhoc_aa1051", "writing_memory_shadow")
ADHOC_PREFIX = "adhoc_"
# The scopes an llm row may have (= migration 340's spend_budget_scope_check for provider 'llm').
SCOPE_RE = re.compile(r"^(?:(app|stage|job):[a-z0-9][a-z0-9_.-]{0,99}|eval|adhoc)$")
SCOPE_KINDS = ("app", "stage", "job", "eval", "adhoc")

_ROWS_SQL = """
    SELECT to_jsonb(b) AS row
      FROM shared.spend_budget b
     WHERE b.provider = 'llm'
"""
_JOB_KIND_SQL = "SELECT kind FROM shared.job WHERE id = $1::uuid"
# $1 = the window start (the UTC month when the row has a month cap, else the UTC day), $2 = the UTC day start.
_SPEND_HEAD = """
    SELECT coalesce(sum(l.cost_usd) FILTER (WHERE l.created_at >= $2), 0)::float AS day,
           coalesce(sum(l.cost_usd), 0)::float AS win
      FROM shared.llm_call_log l
"""
_SPEND_FILTER = {
    "app": "WHERE l.created_at >= $1 AND coalesce(l.app, 'cis') = $3",
    "stage": "WHERE l.created_at >= $1 AND l.stage = $3",
    "job": "JOIN shared.job j ON j.id = l.job_id WHERE l.created_at >= $1 AND j.kind = $3",
    "eval": "WHERE l.created_at >= $1 AND l.stage = ANY($3::text[])",
    "adhoc": "WHERE l.created_at >= $1 AND l.stage LIKE 'adhoc\\_%'",
}
_RUN_SPEND_SQL = "SELECT coalesce(sum(cost_usd), 0)::float FROM shared.llm_call_log WHERE job_id = $1::uuid"


def spend_sql(kind: str) -> str:
    return _SPEND_HEAD + _SPEND_FILTER[kind]


@dataclass(frozen=True)
class CentralBudget:
    """One ``llm`` row of shared.spend_budget."""
    scope: str
    per_run_usd: Optional[float] = None
    per_day_usd: Optional[float] = None
    per_month_usd: Optional[float] = None
    hard_stop: bool = False
    alert_pct: int = 80

    @property
    def kind(self) -> str:
        return self.scope.split(":", 1)[0]

    @property
    def target(self) -> Optional[str]:
        """The app / stage / job kind of the scope (None for eval and adhoc)."""
        return self.scope.split(":", 1)[1] if ":" in self.scope else None

    @property
    def capped(self) -> bool:
        return any(v is not None for v in (self.per_run_usd, self.per_day_usd, self.per_month_usd))


@dataclass(frozen=True)
class Spend:
    day_usd: float = 0.0
    month_usd: Optional[float] = None       # None: not read (the row has no month cap)
    run_usd: Optional[float] = None         # None: not read (no run cap, or no job bound)


def row_of(raw: dict) -> Optional[CentralBudget]:
    """A CentralBudget from a ``to_jsonb`` row (a column the database does not have yet is absent), or None when the
    scope is not a central one. Pure."""
    scope = str(raw.get("scope") or "")
    if not SCOPE_RE.match(scope):
        return None

    def num(key):
        v = raw.get(key)
        return None if v is None else float(v)

    return CentralBudget(scope=scope, per_run_usd=num("per_run_usd"), per_day_usd=num("per_day_usd"),
                         per_month_usd=num("per_month_usd"), hard_stop=bool(raw.get("hard_stop", False)),
                         alert_pct=int(raw.get("alert_pct") or 80))


def scopes_for(stage: Optional[str], *, app: str, job_kind: Optional[str] = None,
               eval_stage: Optional[str] = None) -> list[str]:
    """The central scopes a call belongs to, broadest first. ``eval_stage``: the bound eval label's stage. Pure."""
    out = [f"app:{app}"]
    if stage:
        out.append(f"stage:{stage}")
    if job_kind:
        out.append(f"job:{job_kind}")
    if eval_stage or stage == EVAL_STAGES[0]:
        out.append("eval")
    if (stage or "").startswith(ADHOC_PREFIX) or (eval_stage or "").startswith(ADHOC_PREFIX):
        out.append("adhoc")
    return out


def utc_windows(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """(UTC midnight, UTC first of the month) of ``now``. Pure."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day, day.replace(day=1)


@dataclass(frozen=True)
class Breach:
    axis: str                   # per_run | per_day | per_month
    cap: float
    spent: float


def breaches(row: CentralBudget, spend: Spend, estimate_usd: float) -> list[Breach]:
    """The caps of ``row`` that ``estimate_usd`` more would pass (hard or not). Pure."""
    out = []
    for axis, cap, spent in (("per_run", row.per_run_usd, spend.run_usd),
                             ("per_day", row.per_day_usd, spend.day_usd),
                             ("per_month", row.per_month_usd, spend.month_usd)):
        if cap is not None and spent is not None and spent + estimate_usd > cap:
            out.append(Breach(axis, cap, spent))
    return out


def used_pct(row: CentralBudget, spend: Spend) -> Optional[float]:
    """The highest share of a cap already spent (100 when a $0 cap has any spend), None without a cap. Pure."""
    pcts = []
    for cap, spent in ((row.per_run_usd, spend.run_usd), (row.per_day_usd, spend.day_usd),
                       (row.per_month_usd, spend.month_usd)):
        if cap is None or spent is None:
            continue
        pcts.append((100.0 if spent > 0 else 0.0) if cap <= 0 else 100.0 * spent / cap)
    return max(pcts) if pcts else None


def summary(row: CentralBudget, spend: Spend) -> dict:
    """The bell / e-mail payload (the keys ops_alerts.render_spend_budget reads, plus the month)."""
    return {
        "provider": PROVIDER, "scope": row.scope, "per_run_usd": row.per_run_usd, "per_day_usd": row.per_day_usd,
        "per_month_usd": row.per_month_usd, "hard_stop": row.hard_stop, "hard_stop_day": row.hard_stop,
        "alert_pct": row.alert_pct, "day_spent_usd": round(spend.day_usd, 4),
        "month_spent_usd": None if spend.month_usd is None else round(spend.month_usd, 4),
        "run_spent_usd": None if spend.run_usd is None else round(spend.run_usd, 4),
        "used_pct": None if used_pct(row, spend) is None else round(used_pct(row, spend), 1),
    }


def message(row: CentralBudget, spend: Spend, found: list[Breach], estimate_usd: float) -> str:
    b = found[0]
    window = {"per_run": "this job", "per_day": "today (UTC)", "per_month": "this month (UTC)"}[b.axis]
    return (f"central LLM budget llm/{row.scope}: {window} ${b.spent:.4f} spent of the ${b.cap:.2f} "
            f"{b.axis.replace('_', ' ')} cap; this call (up to ~${estimate_usd:.4f}) was not sent")


class CentralBudgetExceeded(cost_guard.BudgetExceeded, RuntimeError):
    """A call refused by a hard central LLM cap before any attempt. A ``BudgetExceeded``, so the job runner ends the
    job ``stopped_budget`` (no retry); a ``RuntimeError``, so callers that catch the gateway's failures as before still
    do. ``scope`` / ``axis`` name the cap."""

    scope: str = ""
    axis: str = ""


def exceeded(row: CentralBudget, spend: Spend, found: list[Breach], estimate_usd: float) -> CentralBudgetExceeded:
    exc = CentralBudgetExceeded(message(row, spend, found, estimate_usd), PROVIDER, found[0].axis)
    exc.scope = row.scope
    exc.axis = found[0].axis
    return exc


def enabled() -> bool:
    return (os.environ.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


# ── loaders (tests replace them; conftest: no database in unit tests) ───────────────────────────────────────────────

async def _load_rows() -> dict[str, CentralBudget]:
    conn = await connections._connect()
    try:
        rows = await conn.fetch(_ROWS_SQL)
    finally:
        await conn.close()
    out = {}
    for r in rows:
        raw = r["row"]
        if isinstance(raw, str):
            import json
            raw = json.loads(raw)
        row = row_of(raw)
        if row is not None and row.capped:
            out[row.scope] = row
    return out


async def read_spend(conn, row: CentralBudget, *, job_id: Optional[str] = None,
                     now: Optional[datetime] = None) -> Spend:
    """The spend of ``row``'s scope today (and this month when it has a month cap, and the running job's when it has a
    run cap and a job is bound). One query, plus one for the run. ``conn``: an asyncpg connection."""
    day, month = utc_windows(now)
    start = month if row.per_month_usd is not None else day
    kind = row.kind
    args: list = [start, day]
    if kind == "app" or kind == "stage" or kind == "job":
        args.append(row.target)
    elif kind == "eval":
        args.append(list(EVAL_STAGES))
    rec = await conn.fetchrow(spend_sql(kind), *args)
    day_usd, window_usd = float(rec["day"] or 0.0), float(rec["win"] or 0.0)
    run_usd = None
    if row.per_run_usd is not None and kind == "job" and job_id:
        run_usd = float(await conn.fetchval(_RUN_SPEND_SQL, str(job_id)) or 0.0)
    return Spend(day_usd=day_usd, month_usd=window_usd if row.per_month_usd is not None else None, run_usd=run_usd)


async def _load_spend(row: CentralBudget, job_id: Optional[str]) -> Spend:
    conn = await connections._connect()
    try:
        spend = await read_spend(conn, row, job_id=job_id)
        pct = used_pct(row, spend)
        if pct is not None and pct >= row.alert_pct:
            tail = "" if row.hard_stop else " (alert only: calls go on)"
            await cost_guard.maybe_alert_central(conn, summary(row, spend), alert_pct=row.alert_pct, stopped=False,
                                                 message=f"central LLM budget llm/{row.scope} is at {pct:.0f}% of a "
                                                         f"cap{tail}")
        return spend
    finally:
        await conn.close()


async def _load_job_kind(job_id: str) -> Optional[str]:
    conn = await connections._connect()
    try:
        return await conn.fetchval(_JOB_KIND_SQL, str(job_id))
    finally:
        await conn.close()


async def _alert_stop(row: CentralBudget, spend: Spend, text: str) -> None:
    conn = await connections._connect()
    try:
        await cost_guard.maybe_alert_central(conn, summary(row, spend), alert_pct=row.alert_pct, stopped=True,
                                             message=text)
    finally:
        await conn.close()


def loader_rows() -> dict[str, CentralBudget]:
    return connections.run_sync(_load_rows)


def loader_spend(row: CentralBudget, job_id: Optional[str]) -> Spend:
    return connections.run_sync(lambda: _load_spend(row, job_id))


def loader_job_kind(job_id: str) -> Optional[str]:
    return connections.run_sync(lambda: _load_job_kind(job_id))


def alert_stop(row: CentralBudget, spend: Spend, text: str) -> None:
    connections.run_sync(lambda: _alert_stop(row, spend, text))


# ── caches ──────────────────────────────────────────────────────────────────────────────────────────────────────────

_rows: dict[str, CentralBudget] = {}
_rows_at = 0.0
_rows_failed_at = 0.0
_spend: dict[tuple, tuple[Spend, float]] = {}
_spend_failed_at: dict[tuple, float] = {}
_job_kinds: dict[str, tuple[Optional[str], float]] = {}
_lock = threading.Lock()


def rows() -> dict[str, CentralBudget]:
    """The cached ``llm`` rows by scope ({} = none, or unreadable). Never raises."""
    global _rows, _rows_at, _rows_failed_at
    now = time.monotonic()
    if (_rows_at and now - _rows_at < ROWS_TTL_SECONDS) or (_rows_failed_at and now - _rows_failed_at
                                                            < FAILURE_TTL_SECONDS):
        return _rows
    with _lock:
        now = time.monotonic()
        if (_rows_at and now - _rows_at < ROWS_TTL_SECONDS) or (_rows_failed_at and now - _rows_failed_at
                                                                < FAILURE_TTL_SECONDS):
            return _rows
        try:
            _rows = loader_rows()
            _rows_at, _rows_failed_at = time.monotonic(), 0.0
        except Exception as e:  # noqa: BLE001 — fail open, see the module docstring
            _rows, _rows_at, _rows_failed_at = {}, 0.0, time.monotonic()
            logger.warning("llm_central_budget_read_failed", error=str(e)[:300],
                           hint="central LLM budgets are not enforced until the rows can be read")
    return _rows


def _job_kind(job_id: Optional[str]) -> Optional[str]:
    if not job_id:
        return None
    now = time.monotonic()
    cached = _job_kinds.get(job_id)
    if cached is not None and now - cached[1] < JOB_KIND_TTL_SECONDS:
        return cached[0]
    try:
        kind = loader_job_kind(job_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_central_budget_job_kind_failed", job_id=str(job_id)[:64], error=str(e)[:200])
        return None
    if len(_job_kinds) > 1000:
        _job_kinds.clear()
    _job_kinds[job_id] = (kind, now)
    return kind


def spend_of(row: CentralBudget, job_id: Optional[str]) -> Optional[Spend]:
    """The cached spend of ``row``'s scope (None: unreadable). Never raises."""
    key = (row, job_id if (row.per_run_usd is not None and row.kind == "job") else None)
    now = time.monotonic()
    cached = _spend.get(key)
    if cached is not None and now - cached[1] < SPEND_TTL_SECONDS:
        return cached[0]
    failed = _spend_failed_at.get(key)
    if failed and now - failed < FAILURE_TTL_SECONDS:
        return None
    try:
        spend = loader_spend(row, key[1])
    except Exception as e:  # noqa: BLE001 — fail open
        _spend_failed_at[key] = time.monotonic()
        logger.warning("llm_central_budget_spend_read_failed", scope=row.scope, error=str(e)[:300])
        return None
    if len(_spend) > 1000:
        _spend.clear()
    _spend[key] = (spend, time.monotonic())
    _spend_failed_at.pop(key, None)
    return spend


def active() -> bool:
    """True when the check is on and at least one central cap exists (the cached rows; never raises). The gateway
    computes the call's estimate only then."""
    try:
        return enabled() and bool(rows())
    except Exception:  # noqa: BLE001
        return False


def check(stage: Optional[str], estimate_usd: float = 0.0) -> None:
    """The gateway's check before any attempt (module docstring). Raises ``CentralBudgetExceeded`` when a hard cap of
    one of the call's scopes would be passed by ``estimate_usd`` more; nothing else."""
    if not enabled():
        return
    try:
        table = rows()
        if not table:
            return
        from .call_log import APP, current_job_id, eval_label

        job_id = current_job_id()
        kind = _job_kind(job_id) if job_id and any(s.startswith("job:") for s in table) else None
        label = eval_label()
        applicable = [table[s] for s in scopes_for(stage, app=APP, job_kind=kind,
                                                   eval_stage=label[0] if label else None) if s in table]
        estimate = max(float(estimate_usd or 0.0), 0.0)
        stop = None
        for row in applicable:
            spend = spend_of(row, job_id)
            if spend is None:
                continue
            found = breaches(row, spend, estimate)
            if found and row.hard_stop:
                stop = (row, spend, found)
                break
            if found:
                logger.info("llm_central_budget_over_alert_only", stage=stage, **summary(row, spend))
    except Exception as e:  # noqa: BLE001 — never on the call path
        logger.warning("llm_central_budget_check_failed", stage=stage, error=str(e)[:200])
        return
    if stop is None:
        return
    row, spend, found = stop
    exc = exceeded(row, spend, found, estimate)
    logger.warning("llm_central_budget_refused", stage=stage, axis=exc.axis, estimate_usd=round(estimate, 6),
                   **summary(row, spend))
    try:
        alert_stop(row, spend, str(exc))
    except Exception as e:  # noqa: BLE001 — the bell is best effort, the refusal stands
        logger.warning("llm_central_budget_alert_failed", scope=row.scope, error=str(e)[:200])
    raise exc


def invalidate() -> None:
    global _rows, _rows_at, _rows_failed_at
    _rows, _rows_at, _rows_failed_at = {}, 0.0, 0.0
    _spend.clear()
    _spend_failed_at.clear()
    _job_kinds.clear()
