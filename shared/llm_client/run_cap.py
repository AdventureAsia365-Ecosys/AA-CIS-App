"""AA-1126 G4 (S283) — the hard USD cap of one internal batch run: an S1 rewrite job (one tour of a rerun wave), an eval
run and an ad-hoc run. Tech lead policy S283 point 5: internal batch runs keep a hard cap per run in every environment,
like the W5 writer proof cap (services/writer_proof/spend.py).

The run of a call (``run_of``), in this order:

- ``eval``: an eval label is bound (``call_log.bind_eval_stage``) or the stage is ``writing_memory_eval``. The run is
  the bound job when there is one (its ledger rows, every attempt), else the eval run id (rows of the eval stage whose
  ``quality_signal.eval_run`` is that id, since this process first saw the run);
- ``adhoc``: the stage (or the eval label) starts with ``adhoc_``. The run is the bound job, else the stage's rows since
  this process first called it (one script run = one process);
- ``s1_rewrite``: the bound job's kind is ``s1_rewrite`` (``capacity.current_kind``, bound by the worker). The run is
  the job.

Any other call is not a capped run: nothing is read. The cap: the job payload's ``cap_usd`` when it carries one (the
worker binds it, ``bind_payload``; new ``s1_rewrite`` jobs carry the setting's value from when they were queued,
``stamp``), else the class's setting (``caps_settings.run_cap_usd``: ``cis.spend.run_cap_*_usd`` on Platform ›
Settings, area Spend). When the run's ledger spend has reached its cap the gateway refuses the next call before any
attempt: ``RunCapReached`` (a ``BudgetExceeded``: the job ends ``stopped_budget``, nothing is retried; the gateway
writes the 0 USD refused / budget ledger row and remembers the stop for the job, ``note_stop``, so a pipeline that
turns a failed call into a failed draft still ends its job ``stopped_budget``: the S1 rewrite job reads ``job_stop``)
and one admin bell + ops e-mail per class and 24 h (scope ``llm:run:<class>``).

Spend is read from ``shared.llm_call_log`` (one indexed query: ``job_id``, or ``stage`` + ``created_at``), cached
``SPEND_TTL_SECONDS`` per run in this process, so a run passes its cap by at most the calls already running plus one
cache window. Never fails closed (the gateway's rule for every budget read): an unreadable spend is logged and the call
goes on. No env switch: a run cap is changed on Platform › Settings only (tech lead S283, one source of truth).
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

import structlog

from shared import cost_guard

from . import caps_settings, connections

logger = structlog.get_logger()

SPEND_TTL_SECONDS = 5.0  # technical-constant: in-process cache refresh of a read
FAILURE_TTL_SECONDS = 60.0  # technical-constant: back-off after a failed cache read
EVAL_STAGE = "writing_memory_eval"
ADHOC_PREFIX = "adhoc_"
S1_KIND = "s1_rewrite"
STAMPED_KINDS = (S1_KIND,)                      # the job kinds whose payload gets cap_usd at enqueue
SCOPE_PREFIX = "run:"

JOB_SPEND_SQL = "SELECT coalesce(sum(cost_usd), 0)::float AS usd FROM shared.llm_call_log WHERE job_id = $1::uuid"
EVAL_SPEND_SQL = """
    SELECT coalesce(sum(cost_usd), 0)::float AS usd
      FROM shared.llm_call_log
     WHERE stage = $1 AND created_at >= $2 AND quality_signal ->> 'eval_run' = $3
"""
STAGE_SPEND_SQL = """
    SELECT coalesce(sum(cost_usd), 0)::float AS usd
      FROM shared.llm_call_log
     WHERE stage = $1 AND created_at >= $2
"""


class RunCapReached(cost_guard.BudgetExceeded, RuntimeError):
    """The run's ledger spend reached its hard cap: the gateway refused the call before any provider attempt. A
    ``BudgetExceeded`` (the job ends ``stopped_budget``) and a ``RuntimeError`` (callers that catch the gateway's
    failures still do). ``run_class`` / ``cap_usd`` / ``spent_usd`` name the cap."""

    run_class: str = ""
    cap_usd: float = 0.0
    spent_usd: float = 0.0


@dataclass(frozen=True)
class Run:
    run_class: str               # s1_rewrite | eval | adhoc
    kind: str                    # job | eval_run | stage: what the spend query counts
    ref: str                     # the job id, the eval run id or the stage
    stage: Optional[str] = None  # the ledger stage of an eval_run / stage run
    since: Optional[datetime] = None


# ── the job payload's cap (bound by the worker) ─────────────────────────────────────────────────────────────────────

_payload_cap: ContextVar[Optional[float]] = ContextVar("llm_run_cap_payload", default=None)


def cap_of_payload(payload: Any) -> Optional[float]:
    """The payload's ``cap_usd`` when it is a positive finite number, else None. Pure."""
    v = payload.get("cap_usd") if isinstance(payload, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v <= 0 or v == float("inf"):
        return None
    return float(v)


@contextmanager
def bind_payload(payload: Any) -> Iterator[None]:
    """The worker binds the running job's payload cap around its handler (None when the payload has none)."""
    token = _payload_cap.set(cap_of_payload(payload))
    try:
        yield
    finally:
        _payload_cap.reset(token)


def stamp(kind: str, payload: dict) -> dict:
    """``payload`` with ``cap_usd`` = the run class's setting now, for a kind in ``STAMPED_KINDS`` whose payload has no
    cap yet (the queue keeps the value the job was queued with). Any other payload is returned as is. Never raises."""
    if kind not in STAMPED_KINDS or not isinstance(payload, dict) or cap_of_payload(payload) is not None:
        return payload
    cap = caps_settings.run_cap_usd(kind)
    return payload if cap is None else {**payload, "cap_usd": cap}


# ── which run a call belongs to ─────────────────────────────────────────────────────────────────────────────────────

_first_seen: dict[tuple, datetime] = {}


def _since(key: tuple, now: datetime) -> datetime:
    at = _first_seen.get(key)
    if at is None:
        if len(_first_seen) > 1000:
            _first_seen.clear()
        at = _first_seen[key] = now
    return at


def run_of(stage: Optional[str], *, job_id: Optional[str], job_kind: Optional[str],
           eval_label: Optional[tuple], now: Optional[datetime] = None) -> Optional[Run]:
    """The capped run of a call, or None (module docstring). ``eval_label`` = (eval stage, run id) or None."""
    now = now or datetime.now(timezone.utc)
    label_stage = eval_label[0] if eval_label else None
    if eval_label or stage == EVAL_STAGE:
        cls = "eval"
    elif (stage or "").startswith(ADHOC_PREFIX) or (label_stage or "").startswith(ADHOC_PREFIX):
        cls = "adhoc"
    elif job_id and job_kind == S1_KIND:
        cls = S1_KIND
    else:
        return None
    if job_id:
        return Run(cls, "job", str(job_id))
    if eval_label:
        return Run(cls, "eval_run", str(eval_label[1]), stage=label_stage,
                   since=_since(("eval", label_stage, eval_label[1]), now))
    if not stage:
        return None
    return Run(cls, "stage", stage, stage=stage, since=_since(("stage", stage), now))


def cap_for(run: Run) -> Optional[float]:
    """The bound payload's cap (a job run only), else the class's setting. None = no cap (setting unreadable)."""
    if run.kind == "job":
        bound = _payload_cap.get()
        if bound is not None:
            return bound
    return caps_settings.run_cap_usd(run.run_class)


# ── spend (asyncpg) ─────────────────────────────────────────────────────────────────────────────────────────────────

async def read_spend(conn, run: Run) -> float:
    if run.kind == "job":
        return float(await conn.fetchval(JOB_SPEND_SQL, run.ref) or 0.0)
    if run.kind == "eval_run":
        return float(await conn.fetchval(EVAL_SPEND_SQL, run.stage, run.since, run.ref) or 0.0)
    return float(await conn.fetchval(STAGE_SPEND_SQL, run.stage, run.since) or 0.0)


async def _load(run: Run) -> float:
    conn = await connections._connect()
    try:
        return await read_spend(conn, run)
    finally:
        await conn.close()


def loader(run: Run) -> float:
    """The sync spend loader (tests replace it; no database in unit tests)."""
    return connections.run_sync(lambda: _load(run))


_spend: dict[tuple, tuple[float, float]] = {}
_failed_at: dict[tuple, float] = {}
_lock = threading.Lock()


def spend_of(run: Run) -> Optional[float]:
    """The run's cached ledger spend (None: unreadable). Never raises."""
    key = (run.kind, run.ref, run.stage)
    now = time.monotonic()
    hit = _spend.get(key)
    if hit is not None and now - hit[1] < SPEND_TTL_SECONDS:
        return hit[0]
    failed = _failed_at.get(key)
    if failed and now - failed < FAILURE_TTL_SECONDS:
        return None
    with _lock:
        try:
            usd = loader(run)
        except Exception as e:  # noqa: BLE001 — fail open, see the module docstring
            _failed_at[key] = time.monotonic()
            logger.warning("llm_run_cap_spend_read_failed", run_class=run.run_class, run=run.ref[:64],
                           error=str(e)[:300])
            return None
        if len(_spend) > 1000:
            _spend.clear()
        _spend[key] = (usd, time.monotonic())
        _failed_at.pop(key, None)
        return usd


# ── the gateway's check ─────────────────────────────────────────────────────────────────────────────────────────────

def message(run: Run, cap: float, spent: float) -> str:
    what = {"job": "job", "eval_run": "eval run", "stage": "ad-hoc run of stage"}[run.kind]
    return (f"{run.run_class} run cap reached: {what} {run.ref} has spent ${spent:.4f} of its ${cap:.2f} hard cap "
            "(job payload cap_usd, else Platform › Settings › Spend); this call was not sent")


def refusal(run: Run, cap: float, spent: float) -> RunCapReached:
    exc = RunCapReached(message(run, cap, spent), "llm", "per_run")
    exc.run_class, exc.cap_usd, exc.spent_usd = run.run_class, cap, spent
    return exc


async def _alert(run: Run, cap: float, spent: float, text: str) -> None:
    conn = await connections._connect()
    try:
        summary = {"provider": "llm", "scope": f"{SCOPE_PREFIX}{run.run_class}", "per_run_usd": cap,
                   "run_spent_usd": round(spent, 4), "hard_stop": True, "hard_stop_day": False,
                   "used_pct": round(100.0 * spent / cap, 1) if cap > 0 else None}
        await cost_guard.maybe_alert_central(conn, summary, alert_pct=100, stopped=True, message=text)
    finally:
        await conn.close()


def send(run: Run, cap: float, spent: float, text: str) -> None:
    """Write the bell (tests replace it; no database in unit tests)."""
    connections.run_sync(lambda: _alert(run, cap, spent, text))


def alert(run: Run, cap: float, spent: float, text: str) -> None:
    """One admin bell + ops e-mail per run class and 24 h. Best effort: never raises."""
    try:
        send(run, cap, spent, text)
    except Exception as e:  # noqa: BLE001 — the refusal stands whatever the bell does
        logger.warning("llm_run_cap_alert_failed", run_class=run.run_class, error=str(e)[:200])


def check(stage: Optional[str]) -> None:
    """Before any attempt: raise ``RunCapReached`` when the call's run reached its hard cap. Raises nothing else."""
    try:
        from . import capacity
        from .call_log import current_job_id, eval_label
        run = run_of(stage, job_id=current_job_id(), job_kind=capacity.current_kind(), eval_label=eval_label())
        if run is None:
            return
        cap = cap_for(run)
        if cap is None:
            return
        spent = spend_of(run)
        if spent is None or spent < cap:
            return
    except Exception as e:  # noqa: BLE001 — never on the call path
        logger.warning("llm_run_cap_check_failed", stage=stage, error=str(e)[:200])
        return
    exc = refusal(run, cap, spent)
    logger.warning("llm_run_cap_refused", stage=stage, run_class=run.run_class, run=run.ref[:64], cap_usd=cap,
                   spent_usd=round(spent, 6))
    alert(run, cap, spent, str(exc))
    raise exc


def refusal_of(stage: Optional[str]) -> Optional[RunCapReached]:
    """``check`` returning its refusal instead of raising it (the gateway writes the ledger row and raises it)."""
    try:
        check(stage)
    except RunCapReached as e:
        return e
    return None


# ── a spend stop inside a job (review S283: the S1 rewrite job must end stopped_budget, not failed) ──────────────────

def spend_stop_in(exc: Optional[BaseException]) -> Optional[cost_guard.BudgetExceeded]:
    """The ``RunCapReached`` or ``AnomalyBreakerTripped`` in ``exc`` or its explicit cause chain (``raise ... from``),
    else None. A pipeline that turns the gateway's failures into a failed result re-raises these two (no retry can pass
    them: the run spent its cap, the app's breaker is tripped); the worker reads the chain too. Pure."""
    from .anomaly_breaker import AnomalyBreakerTripped
    from .errors import _chain

    if exc is None:
        return None
    for link in _chain(exc, implicit=False):
        if isinstance(link, (RunCapReached, AnomalyBreakerTripped)):
            return link
    return None


_job_stops: dict[str, cost_guard.BudgetExceeded] = {}


def note_stop(exc: BaseException) -> None:
    """The gateway refused a call of the bound job with a spend stop (``spend_stop_in``): remember it for the job, so
    a handler whose pipeline swallowed the refusal (the S1 graph turns a failed call into a failed draft) still ends
    the job ``stopped_budget`` (``job_stop``). No job bound, or another refusal: nothing. Never raises."""
    try:
        stop = spend_stop_in(exc)
        if stop is None:
            return
        from .call_log import current_job_id
        job_id = current_job_id()
        if not job_id:
            return
        if len(_job_stops) > 1000:
            _job_stops.clear()
        _job_stops[str(job_id)] = stop
    except Exception as e:  # noqa: BLE001 — the refusal stands whatever this note does
        logger.warning("llm_run_cap_note_stop_failed", error=str(e)[:200])


def job_stop(job_id: Optional[str], *, pop: bool = False) -> Optional[cost_guard.BudgetExceeded]:
    """The spend stop the gateway raised for ``job_id`` in this process (``note_stop``), else None; ``pop`` forgets it
    (the job handler, once it has ended the job with it)."""
    if not job_id:
        return None
    key = str(job_id)
    return _job_stops.pop(key, None) if pop else _job_stops.get(key)


def invalidate() -> None:
    with _lock:
        _spend.clear()
        _failed_at.clear()
        _first_seen.clear()
        _job_stops.clear()


__all__ = ["RunCapReached", "Run", "bind_payload", "cap_for", "cap_of_payload", "check", "invalidate", "job_stop",
           "note_stop", "refusal_of", "run_of", "spend_of", "spend_stop_in", "stamp"]
