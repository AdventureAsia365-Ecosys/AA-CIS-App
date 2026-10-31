"""AA-1002 (gateway slice S4) — a circuit breaker per deployment and a limiter per connection.

Breaker. One circuit per (connection, catalog model) — a deployment — so a model the LLM account may not call does not
stop the models it may. The state machine is the Jev credit breaker's (``decide.jev_breaker_open``: trip on a failure
a retry cannot fix, skip the provider for a cool-down, close on the next success), with three changes the blueprint
asks for:

- the state lives in ``shared.llm_provider_health`` (migration 309), so every api / worker task sees a circuit another
  task opened, instead of each process learning it again with its own failed calls;
- what opens a circuit depends on the error class (``errors.classify``, plus ``credential`` for a connection whose key
  cannot be read): an account-level refusal or a missing credential opens at once, throttling after a few in a row,
  anything else (outage, timeout) after more (``RULES``); a refusal of one request's own content (input too long,
  malformed, content filter: ``is_request_error``) is not counted at all;
- after the open period the circuit is half-open: exactly ONE call across all tasks may try it (a probe lease taken
  in the DB row); its success closes the circuit, its failure opens it again for twice as long (capped). A breaker
  that opened by mistake therefore costs one short open period and one probe, never a stuck route.

Transitions are pure functions (``after_failure``, ``after_success``, ``claimed``) on a ``Health`` row. A write locks
the row (``SELECT … FOR UPDATE``), applies the function and writes the result, so concurrent tasks count failures
correctly. Reads are a cached snapshot of the whole table (``READ_TTL_S``); a success is written at most once a
minute per deployment and process unless it closes something. When the table cannot be read or written (migration 309
not applied yet, DB down) the same transitions run on the in-process copy: the breaker degrades to a per-process
breaker, never to an error on the call path. Every public function here never raises.

Where it plugs in: ``vendor_route.deployment_plan`` leaves out a deployment whose circuit is open (``is_open``, the S3
hook ``vendor_route.circuit_open``); ``LLMClient._call_target`` and ``embed`` wrap each provider call in ``guarded``
(half-open probe claim, limiter, outcome). ``LLM_CIRCUIT_BREAKER=off`` (ECS env, read per call) makes ``is_open``
False and ``guarded`` a plain call (the limiter still applies), i.e. the S3 behaviour.

Limiter. Per connection, in process (each ECS task enforces its own share): ``max_concurrency`` calls at once (a
semaphore) and ``rpm_limit`` calls per rolling minute, both from the connection row (NULL = no limit). A call waits
for a slot up to ``LIMIT_WAIT_S``; past that it fails with ``LimiterSaturated`` (class ``throttled``), which is not a
provider failure (no breaker count) and lets the route move to its next deployment or model.

AA-1018 (reliability part 1) — the connection-level circuit. An account-wide refusal (Bedrock "Error 002" on the LLM
account, a key that cannot be read, a provider spend cap) refuses every model of the connection alike, yet the
per-deployment circuits above learn it one model at a time, each with a failed real call. ``connection_health`` derives
one circuit per connection (shown with ``model_key "*"``, ``CONNECTION_MODEL``) from the deployment rows every task
already shares: it trips on account-wide signals only — a non-closed deployment circuit of class ``credential`` or
``quota_exhausted``, or ``access_denied`` on at least ``CONNECTION_DENIED_MODELS`` distinct models (one model's refusal,
e.g. acc2 refusing one Claude model, never stops acc2's embeddings) — newer than the connection's last answered call
(any success on the connection closes it). While it is OPEN (until the newest open period of its tripping rows ends)
``connection_open`` makes ``vendor_route.deployment_plan`` and ``admit`` (every ``guarded`` call) skip every deployment
of the connection; afterwards the per-deployment circuits decide alone (a failed probe reopens its row for twice as
long, which reopens the connection). It is derived, not stored: ``llm_provider_health.model_key`` references the model
catalog, so no ``"*"`` row can be written (a later migration may persist it). On the transition to OPEN,
``record_failure`` calls ``on_connection_open``: one admin bell (``shared.notifications``, ``llm_circuit_open``, at
most one unread per connection per 24 h) and the ops alert e-mail (``ops_alerts.enqueue_llm_circuit_email``).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Iterator, Optional, TypeVar

import structlog

from . import connections
from .errors import (ACCESS_DENIED, OTHER, QUOTA_EXHAUSTED, THROTTLED, TRUNCATED, _chain, account_level_class,
                     classify, redact_ids)

logger = structlog.get_logger()

T = TypeVar("T")

SWITCH_ENV = "LLM_CIRCUIT_BREAKER"
_OFF = ("0", "false", "off", "no")

# The breaker's own class for a connection whose credential cannot be resolved (connections.ConnectionCredentialError):
# not a provider answer, but every call on that connection fails the same way until someone fixes the secret.
CREDENTIAL = "credential"
CLASSES = (ACCESS_DENIED, QUOTA_EXHAUSTED, THROTTLED, CREDENTIAL, OTHER)
# Not a circuit class: a refusal of one request's content (input too long, malformed, content filter). It is never
# counted (``record_failure`` ignores it), so one bad payload retried by a job cannot open the circuit for everyone.
REQUEST = "request"
_REQUEST_WORDING = re.compile(
    r"validationexception"
    r"|\binvalid_request_error\b"
    r"|\brequest_too_large\b"
    r"|badrequesterror"
    r"|context_length_exceeded"
    r"|maximum context length"
    r"|input is too long"
    r"|prompt is too long"
    r"|too many (input )?tokens"
    r"|malformed input request"
    r"|content_filter"
    r"|content_policy_violation",
    re.IGNORECASE)
_DEPLOYMENT_WORDING = re.compile(
    r"model identifier is invalid"
    r"|on-demand throughput"
    r"|inference profile"
    r"|end of (its )?life"
    r"|model (is )?not (found|supported)"
    r"|unsupported model",
    re.IGNORECASE)

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


@dataclass(frozen=True)
class Rule:
    threshold: int        # consecutive failures that open a closed circuit
    open_s: float         # the first open period; doubled on each failed probe
    max_open_s: float     # cap of the doubling


# Short open periods on purpose (blueprint S4 risk: "a breaker opened by mistake → half-open 1 request, short open").
RULES: dict[str, Rule] = {
    ACCESS_DENIED: Rule(threshold=1, open_s=300, max_open_s=1800),     # Error 002 / IAM: a retry minutes later fails
    QUOTA_EXHAUSTED: Rule(threshold=1, open_s=300, max_open_s=1800),   # no credit left
    CREDENTIAL: Rule(threshold=1, open_s=120, max_open_s=900),          # the secret is missing / unreadable
    THROTTLED: Rule(threshold=3, open_s=30, max_open_s=300),            # rate limit: back off, then probe
    OTHER: Rule(threshold=5, open_s=60, max_open_s=600),                # outage, timeout, 5xx
}
PROBE_LEASE_S = 180.0       # how long a half-open probe holds its lease (a probe that hangs longer frees it)
ERROR_CHARS = 300
READ_TTL_S = 10.0           # the snapshot of the table every task re-reads
OK_WRITE_INTERVAL_S = 60.0  # a success on a healthy deployment is written at most this often per process
DB_RETRY_S = 30.0           # after a DB error, the breaker runs in process for this long before trying the DB again
DB_MISSING_RETRY_S = 300.0  # the table does not exist (migration 309 not applied)
LIMIT_WAIT_S = 30.0


# ── the state machine (pure) ─────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Health:
    connection_key: str
    model_key: str
    last_ok_at: Optional[datetime] = None
    last_error_at: Optional[datetime] = None
    last_error_class: Optional[str] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    circuit_open_until: Optional[datetime] = None
    open_streak: int = 0
    opened_at: Optional[datetime] = None
    probe_until: Optional[datetime] = None


def state_of(h: Optional[Health], now: datetime) -> str:
    """closed (no open period on record), open (inside it) or half_open (it ran out, no success since)."""
    if h is None or h.circuit_open_until is None:
        return CLOSED
    return OPEN if now < h.circuit_open_until else HALF_OPEN


def open_seconds(error_class: str, streak: int) -> float:
    rule = RULES.get(error_class, RULES[OTHER])
    return min(rule.open_s * (2 ** max(streak - 1, 0)), rule.max_open_s)


def after_failure(h: Health, error_class: str, message: str, now: datetime) -> Health:
    """A failed call. Open while open: nothing moves (a call that started before the circuit opened). Half-open: the
    probe failed, open again for twice as long. Closed: open once the class's threshold of failures in a row is met."""
    cls = error_class if error_class in RULES else OTHER
    before = state_of(h, now)
    base = replace(h, last_error_at=now, last_error_class=cls, last_error=(message or "")[:ERROR_CHARS],
                   consecutive_failures=h.consecutive_failures + 1, probe_until=None)
    if before == OPEN:
        return base
    if before == HALF_OPEN:
        streak = h.open_streak + 1
    elif base.consecutive_failures >= RULES[cls].threshold:
        streak = 1
    else:
        return base
    return replace(base, circuit_open_until=now + timedelta(seconds=open_seconds(cls, streak)), open_streak=streak,
                   opened_at=now)


def after_success(h: Health, now: datetime) -> Health:
    """A call answered: the circuit closes and the counters start again."""
    return replace(h, last_ok_at=now, consecutive_failures=0, circuit_open_until=None, open_streak=0, opened_at=None,
                   probe_until=None)


def claimed(h: Health, now: datetime) -> Optional[Health]:
    """The row with this caller's probe lease, or None when the circuit is not half-open or another call holds it."""
    if state_of(h, now) != HALF_OPEN or (h.probe_until is not None and h.probe_until > now):
        return None
    return replace(h, probe_until=now + timedelta(seconds=PROBE_LEASE_S))


# ── AA-1018: the connection-level circuit, derived from the deployment rows (pure) ───────────────────────────────

CONNECTION_MODEL = "*"                 # how the connection circuit is named next to the deployment rows
# Classes that trip the connection from ONE deployment: the key cannot be read, or the account has no money left.
CONNECTION_SOLO_CLASSES = (CREDENTIAL, QUOTA_EXHAUSTED)
# access_denied trips it only from this many distinct models: one model the account may not call is not an account
# block (acc2 refusing a Claude model must not stop acc2's embeddings).
CONNECTION_DENIED_MODELS = 2
CONNECTION_CLASSES = (CREDENTIAL, QUOTA_EXHAUSTED, ACCESS_DENIED)    # also the order that names the circuit's class


def _tripping(rows: Iterable[Health], connection_key: str, now: datetime) -> tuple[list[Health], Optional[datetime]]:
    """(the deployment rows that trip the connection circuit, the connection's last answered call)."""
    mine = [h for h in rows if h.connection_key == connection_key and h.model_key != CONNECTION_MODEL]
    last_ok = max((h.last_ok_at for h in mine if h.last_ok_at is not None), default=None)
    signals = [h for h in mine
               if h.last_error_class in CONNECTION_CLASSES and state_of(h, now) != CLOSED
               and h.last_error_at is not None and (last_ok is None or h.last_error_at > last_ok)]
    solo = [h for h in signals if h.last_error_class in CONNECTION_SOLO_CLASSES]
    denied = [h for h in signals if h.last_error_class == ACCESS_DENIED]
    if len({h.model_key for h in denied}) < CONNECTION_DENIED_MODELS:
        denied = []
    return solo + denied, last_ok


def connection_health(rows: Iterable[Health], connection_key: str, now: datetime) -> Optional[Health]:
    """The connection-level circuit of ``connection_key`` from its deployment rows, or None when nothing account-wide
    is on record. Tripping rows: a non-closed deployment circuit of a ``CONNECTION_CLASSES`` class whose failure is
    newer than the connection's last answered call (any deployment). Trips on one ``CONNECTION_SOLO_CLASSES`` row, or
    on ``access_denied`` rows of at least ``CONNECTION_DENIED_MODELS`` distinct models. The result (``model_key "*"``,
    ``consecutive_failures`` = the number of tripping rows) is open until the newest open period of its tripping rows
    ends (``state_of``), then half-open: not blocking, the deployment circuits decide (module docstring)."""
    members, last_ok = _tripping(list(rows), connection_key, now)
    if not members:
        return None
    cls = next(c for c in CONNECTION_CLASSES if any(h.last_error_class == c for h in members))
    newest = max(members, key=lambda h: h.last_error_at)
    return Health(connection_key=connection_key, model_key=CONNECTION_MODEL, last_ok_at=last_ok,
                  last_error_at=newest.last_error_at, last_error_class=cls, last_error=newest.last_error,
                  consecutive_failures=len(members),
                  circuit_open_until=max(h.circuit_open_until for h in members),
                  open_streak=max(h.open_streak for h in members),
                  opened_at=min((h.opened_at or h.last_error_at) for h in members))


def connection_members(rows: Iterable[Health], connection_key: str, now: datetime) -> list[str]:
    """The model keys whose rows trip the connection circuit (empty when it is not tripped). For display."""
    return sorted({h.model_key for h in _tripping(list(rows), connection_key, now)[0]})


def connection_refusing(h: Optional[Health], now: datetime) -> bool:
    """Whether a connection circuit (``connection_health``) still means "calls are refused": it is tripped at all, open
    or half-open of any class. ``connection_health`` already drops every refusal older than the connection's last
    answered call, so a half-open circuit here means nothing has answered since the refusals: the account is presumed
    still blocked until a call proves otherwise (a real call, the admin's Test connection, the probe). Used to hold the
    bulk re-queue (shared/jobs/queue.py: a still-blocked account must not get hundreds of jobs back that fail again or
    spill onto a paid fallback), to count a connection as unfunded (invariant.py) and for the admin card / digest;
    never to skip a call (``connection_open`` does that, and lets a half-open circuit probe). ``now`` is kept for the
    callers' signature."""
    return h is not None


def connection_state(rows: Iterable[Health], connection_key: str, now: datetime) -> str:
    """closed | open | half_open of the connection circuit (``connection_health``)."""
    return state_of(connection_health(rows, connection_key, now), now)


# ── storage: shared.llm_provider_health, with an in-process copy ────────────────────────────────────────────────

_COLUMNS = ("connection_key", "model_key", "last_ok_at", "last_error_at", "last_error_class", "last_error",
            "consecutive_failures", "circuit_open_until", "open_streak", "opened_at", "probe_until")
SELECT_SQL = f"SELECT {', '.join(_COLUMNS)} FROM shared.llm_provider_health"
_ENSURE_SQL = ("INSERT INTO shared.llm_provider_health (connection_key, model_key) VALUES ($1, $2) "
               "ON CONFLICT (connection_key, model_key) DO NOTHING")
_LOCK_SQL = SELECT_SQL + " WHERE connection_key = $1 AND model_key = $2 FOR UPDATE"
_UPDATE_SQL = ("UPDATE shared.llm_provider_health SET last_ok_at = $3, last_error_at = $4, last_error_class = $5, "
               "last_error = $6, consecutive_failures = $7, circuit_open_until = $8, open_streak = $9, "
               "opened_at = $10, probe_until = $11, updated_at = now() WHERE connection_key = $1 AND model_key = $2")

Key = tuple[str, str]

_lock = threading.Lock()
_load_lock = threading.Lock()
_rows: dict[Key, Health] = {}
_loaded_at = 0.0
_db_down_until = 0.0
_ok_written: dict[Key, float] = {}


def enabled() -> bool:
    """``LLM_CIRCUIT_BREAKER`` (ECS env, default on), read per call."""
    return (os.environ.get(SWITCH_ENV) or "").strip().lower() not in _OFF


def _now() -> datetime:
    return datetime.now(timezone.utc)


def row_to_health(row) -> Health:
    d = {k: row[k] for k in _COLUMNS}
    d["consecutive_failures"] = int(d["consecutive_failures"] or 0)
    d["open_streak"] = int(d["open_streak"] or 0)
    return Health(**d)


def _args(h: Health) -> tuple:
    return (h.connection_key, h.model_key, h.last_ok_at, h.last_error_at, h.last_error_class, h.last_error,
            h.consecutive_failures, h.circuit_open_until, h.open_streak, h.opened_at, h.probe_until)


async def fetch_health(conn) -> list[Health]:
    return [row_to_health(r) for r in await conn.fetch(SELECT_SQL + " ORDER BY connection_key, model_key")]


async def apply_transition(conn, key: Key, fn: Callable[[Health], Optional[Health]]) -> tuple[Health, Optional[Health]]:
    """(row before, row written or None when ``fn`` declined) under the row lock, in one transaction."""
    async with conn.transaction():
        await conn.execute(_ENSURE_SQL, *key)
        cur = row_to_health(await conn.fetchrow(_LOCK_SQL, *key))
        new = fn(cur)
        if new is not None:
            await conn.execute(_UPDATE_SQL, *_args(new))
        return cur, new


async def _with_conn(fn):
    conn = await connections._connect()
    try:
        return await fn(conn)
    finally:
        await conn.close()


def _db(fn):
    """Run ``fn(conn)`` on a fresh connection, unless the DB failed recently (then raise at once, no connect)."""
    global _db_down_until
    if time.monotonic() < _db_down_until:
        raise RuntimeError("llm_provider_health unavailable (recent DB error)")
    try:
        return connections.run_sync(lambda: _with_conn(fn))
    except Exception as e:
        missing = type(e).__name__ == "UndefinedTableError"
        _db_down_until = time.monotonic() + (DB_MISSING_RETRY_S if missing else DB_RETRY_S)
        logger.warning("llm_breaker_db_unavailable", error=str(e)[:200],
                       hint="migration 309 not applied: the breaker runs per process" if missing else
                       "the breaker runs per process until the DB answers again")
        raise


def _ensure_loaded() -> None:
    global _rows, _loaded_at
    if time.monotonic() - _loaded_at < READ_TTL_S:
        return
    if not _load_lock.acquire(blocking=False):
        return                      # another thread is reloading: use the current copy meanwhile
    try:
        if time.monotonic() - _loaded_at < READ_TTL_S:
            return
        try:
            rows = _db(fetch_health)
            if not isinstance(rows, list) or not all(isinstance(r, Health) for r in rows):
                raise TypeError("unexpected health rows")
            with _lock:
                _rows = {(r.connection_key, r.model_key): r for r in rows}
        except Exception:  # noqa: BLE001 — keep the in-process copy (see the module docstring)
            pass
        _loaded_at = time.monotonic()
    finally:
        _load_lock.release()


def _get(key: Key) -> Optional[Health]:
    with _lock:
        return _rows.get(key)


def _transition(key: Key, fn: Callable[[Health], Optional[Health]]) -> tuple[Optional[Health], Optional[Health]]:
    """Apply ``fn`` in the DB (row lock) and mirror the result in process; in process only when the DB fails.
    Returns (before, after-or-None)."""
    try:
        before, after = _db(lambda conn: apply_transition(conn, key, fn))
        if not isinstance(before, Health):
            raise TypeError("unexpected transition result")
    except Exception:  # noqa: BLE001 — the per-process breaker (module docstring)
        with _lock:
            before = _rows.get(key) or Health(*key)
            after = fn(before)
            if after is not None:
                _rows[key] = after
        return before, after
    with _lock:
        _rows[key] = after if after is not None else before
    return before, after


# ── the breaker's public side ────────────────────────────────────────────────────────────────────────────────────

class CircuitOpen(RuntimeError):
    """A deployment was not called because its circuit is open (or another call holds its half-open probe).
    ``llm_error_class`` is the class that opened it, so a route whose every model is refused at account level still
    ends in LLMAccessDenied (the job fails at once) although no provider call was made."""

    def __init__(self, message: str, error_class: str = OTHER):
        super().__init__(message, error_class)
        self.llm_error_class = error_class

    def __str__(self) -> str:
        return str(self.args[0])


def is_open(connection_key: str, model_key: str) -> bool:
    """True while the deployment's circuit is open, or half-open with its one probe already taken. The
    ``vendor_route.circuit_open`` hook. Never raises."""
    if not enabled():
        return False
    try:
        _ensure_loaded()
        h = _get((connection_key, model_key))
        now = _now()
        st = state_of(h, now)
        if st == OPEN:
            return True
        return st == HALF_OPEN and h.probe_until is not None and h.probe_until > now
    except Exception as e:  # noqa: BLE001 — a breaker problem never stops a call
        logger.warning("llm_breaker_read_failed", connection=connection_key, model=model_key, error=str(e)[:200])
        return False


def open_class(connection_key: str, model_key: str) -> Optional[str]:
    """The class that opened the deployment's circuit, else (AA-1018) the class of an open connection circuit (None
    when both are closed or unknown)."""
    h = _get((connection_key, model_key))
    if h is not None and state_of(h, _now()) != CLOSED:
        return h.last_error_class
    return connection_open(connection_key)


def _rows_of(connection_key: str) -> list[Health]:
    with _lock:
        return [h for k, h in _rows.items() if k[0] == connection_key]


def connection_open(connection_key: str) -> Optional[str]:
    """AA-1018: the class of the connection-level circuit while it is OPEN (every deployment of the connection is
    skipped), else None. The ``vendor_route.connection_circuit_open`` hook; ``LLM_CIRCUIT_BREAKER=off`` answers None.
    Never raises."""
    if not enabled():
        return None
    try:
        _ensure_loaded()
        now = _now()
        h = connection_health(_rows_of(connection_key), connection_key, now)
        return h.last_error_class if h is not None and state_of(h, now) == OPEN else None
    except Exception as e:  # noqa: BLE001 — a breaker problem never stops a call
        logger.warning("llm_breaker_read_failed", connection=connection_key, model=CONNECTION_MODEL,
                       error=str(e)[:200])
        return None


def connection_snapshot(connection_key: str) -> Optional[Health]:
    """AA-1018: the connection circuit from the in-process copy (None = nothing account-wide on record)."""
    return connection_health(_rows_of(connection_key), connection_key, _now())


def admit(connection_key: str, model_key: str) -> bool:
    """Whether a call may go to the deployment now: closed yes, open no, half-open only for the one call that takes
    the probe lease (atomically, in the DB row). AA-1018: no while the connection circuit is open. Never raises."""
    if not enabled():
        return True
    try:
        if connection_open(connection_key):
            return False
        key = (connection_key, model_key)
        st = state_of(_get(key), _now())
        if st == CLOSED:
            return True
        if st == OPEN:
            return False
        before, got = _transition(key, lambda cur: claimed(cur, _now()))
        if got is not None:
            logger.info("llm_circuit_probe", connection=connection_key, model=model_key)
            return True
        # The local copy (up to READ_TTL_S old) said half-open, but the row under the lock is already closed: a probe
        # in another task closed it meanwhile. The call goes through (it is not a probe; nothing to claim).
        return state_of(before, _now()) == CLOSED
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_breaker_admit_failed", connection=connection_key, model=model_key, error=str(e)[:200])
        return True


def is_request_error(exc: BaseException) -> bool:
    """True when the provider refused THIS request's content (input too long, context exceeded, malformed body,
    content filter): Bedrock ``ValidationException``, Anthropic ``invalid_request_error`` / 400 / 413, OpenAI
    ``BadRequestError`` / ``context_length_exceeded``. Not when the wording points at the deployment itself (an invalid
    model identifier, no on-demand throughput, a retired model): that fails every request alike and keeps counting.
    Read only for an ``errors.classify`` result of ``other`` (a credit-balance 400 stays quota_exhausted)."""
    for link in _chain(exc):
        text = f"{type(link).__name__}: {link}"
        if _DEPLOYMENT_WORDING.search(text):
            return False
        if getattr(link, "status_code", None) in (400, 413) or _REQUEST_WORDING.search(text):
            return True
    return False


def failure_class(exc: BaseException) -> str:
    """The breaker class of a failed call: ``credential`` for an unreadable key, else ``errors.classify``, with its
    ``other`` narrowed to ``request`` for a refusal of the request's own content (``is_request_error``)."""
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, connections.ConnectionCredentialError):
            return CREDENTIAL
        cur = cur.__cause__ or cur.__context__
    cls = classify(exc)
    if cls == TRUNCATED:
        # AA-1018 part 2: the provider refused a reply cut at its token limit (OpenAI 400): the request asked for too
        # few tokens, the deployment is healthy. Not counted, like any other refusal of the request's own content.
        return REQUEST
    return REQUEST if cls == OTHER and is_request_error(exc) else cls


def _lease_released(cur: Health) -> Optional[Health]:
    return replace(cur, probe_until=None) if cur.probe_until is not None else None


def record_failure(connection_key: str, model_key: str, exc: BaseException) -> None:
    """Count a failed provider call on the deployment (it may open the circuit). Never raises."""
    if not enabled() or isinstance(exc, (CircuitOpen, LimiterSaturated)):
        return
    try:
        cls = failure_class(exc)
        if cls == REQUEST:
            # Says nothing about the deployment's health: no count, the circuit stays as it is. A half-open probe that
            # met a bad request frees its lease so the next call probes (no doubled open period, no 3-minute wait).
            h = _get((connection_key, model_key))
            if h is not None and h.probe_until is not None:
                _transition((connection_key, model_key), _lease_released)
            logger.info("llm_breaker_request_error_not_counted", connection=connection_key, model=model_key,
                        error=str(exc)[:200])
            return
        message = f"{type(exc).__name__}: {exc}"
        now_fn = _now
        before, after = _transition((connection_key, model_key),
                                    lambda cur: after_failure(cur, cls, message, now_fn()))
        opened = after is not None and after.opened_at is not None
        if opened and (before is None or before.opened_at != after.opened_at):
            logger.warning("llm_circuit_opened", connection=connection_key, model=model_key, error_class=cls,
                           until=after.circuit_open_until.isoformat(), streak=after.open_streak,
                           consecutive_failures=after.consecutive_failures)
        _check_connection_opened(connection_key, model_key, before)
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_breaker_record_failed", connection=connection_key, model=model_key, error=str(e)[:200])


def _check_connection_opened(connection_key: str, model_key: str, before: Optional[Health]) -> None:
    """AA-1018: after a failure was written (the in-process copy holds the new row), compare the connection circuit
    with and without it; on closed / half-open -> OPEN call ``on_connection_open``. Every task that sees the transition
    calls it; the bell throttle and the e-mail idempotency key keep that to one bell and one e-mail."""
    now = _now()
    rows = _rows_of(connection_key)
    after_c = connection_health(rows, connection_key, now)
    if after_c is None or state_of(after_c, now) != OPEN:
        return
    # AA-1018 part 2: the lasting access state follows every OPEN connection circuit (written only on a change).
    note_access_state(connection_key, access_state_of(after_c.last_error_class))
    prior = [h for h in rows if h.model_key != model_key] + ([before] if before is not None else [])
    before_c = connection_health(prior, connection_key, now)
    if before_c is not None and state_of(before_c, now) == OPEN:
        return
    members = connection_members(rows, connection_key, now)
    logger.warning("llm_connection_circuit_opened", connection=connection_key, error_class=after_c.last_error_class,
                   until=after_c.circuit_open_until.isoformat(), models=members)
    try:
        on_connection_open(after_c, members)
    except Exception as e:  # noqa: BLE001 — an alert problem never stops a call
        logger.warning("llm_connection_alert_failed", connection=connection_key, error=str(e)[:200])


# ── AA-1018: the alert when a connection circuit opens (admin bell + ops e-mail) ─────────────────────────────────

CIRCUIT_ALERT_EVENT = "llm_circuit_open"
_PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"
_RECENT_ALERT_SQL = ("SELECT 1 FROM shared.notifications WHERE event_type = $1 AND entity_id = $2 AND is_read = FALSE "
                     "AND created_at >= now() - interval '24 hours' LIMIT 1")
_INSERT_ALERT_SQL = ("INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, "
                     "payload, target_roles) VALUES ($1::uuid, 'system', $2, 'llm_connection', $3, $4::jsonb, "
                     "ARRAY['admin'])")
_CLASS_WORDS = {CREDENTIAL: "its key cannot be read", QUOTA_EXHAUSTED: "the provider refuses for lack of credit",
                ACCESS_DENIED: "the account is refused on several models"}


def circuit_alert_details(h: Health, models: list[str]) -> dict:
    """The bell payload / e-mail details of an opened connection circuit: keys, classes, times and the last provider
    answer with its ARNs and account IDs masked (``errors.redact_ids``: an IAM / STS refusal names both accounts and
    their roles; the alert goes to every recipient and stays in shared.notifications)."""
    words = _CLASS_WORDS.get(h.last_error_class or "", "account-wide refusals")
    until = h.circuit_open_until.isoformat() if h.circuit_open_until else None
    return {"connection_key": h.connection_key, "error_class": h.last_error_class, "models": list(models),
            "open_until": until, "opened_at": h.opened_at.isoformat() if h.opened_at else None,
            "last_error": redact_ids((h.last_error or "")[:ERROR_CHARS]) or None,
            "message": (f"LLM connection {h.connection_key} paused: {words} ({h.last_error_class}). Calls skip it "
                        f"until {until or 'the next probe'}; routes fall back where they can.")}


async def alert_connection_open(conn, h: Health, models: list[str]) -> bool:
    """One admin bell for an opened connection circuit, at most one unread per connection per 24 h, and the ops
    e-mail when a bell row was written. True when a bell was written. ``conn`` is an asyncpg connection."""
    entity = f"llm_connection:{h.connection_key}"
    if await conn.fetchval(_RECENT_ALERT_SQL, CIRCUIT_ALERT_EVENT, entity):
        return False
    details = circuit_alert_details(h, models)
    await conn.execute(_INSERT_ALERT_SQL, _PLATFORM_TENANT_ID, CIRCUIT_ALERT_EVENT, entity, json.dumps(details))
    try:
        from shared import ops_alerts  # lazy: keeps the LLM client import-light

        await ops_alerts.enqueue_llm_circuit_email(conn, details=details)
    except Exception as e:  # noqa: BLE001 — the bell row stays
        logger.warning("llm_circuit_email_enqueue_failed", connection=h.connection_key, error=str(e)[:200])
    return True


def _alert_in_db(h: Health, models: list[str]) -> None:
    """The default ``on_connection_open``: ``alert_connection_open`` on its own connection. Never touches the breaker's
    DB back-off (a failed alert write must not send the breaker to its per-process fallback). Never raises."""
    try:
        connections.run_sync(lambda: _with_conn(lambda conn: alert_connection_open(conn, h, models)))
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_connection_alert_failed", connection=h.connection_key, error=str(e)[:200])


# Tests replace it (conftest: no database in unit tests).
on_connection_open: Callable[[Health, list[str]], None] = _alert_in_db


# ── AA-1018 part 2: the connection's lasting access state (llm_provider_connection.access_state, migration 322) ──
#
# The connection circuit above is derived and short-lived (minutes). The access state is the lasting answer the admin
# and the probe read: 'ok' after any answered call, else the account-wide class that opened the connection circuit
# ('access_denied', 'quota_exhausted'; a key that cannot be read = 'unconfigured'). Written once per change and process
# (a per-process memo; the UPDATE is a no-op when the row already holds the value), on its own DB connection, best
# effort: a failed write never touches the breaker's DB back-off and never raises. cis_app has column UPDATE on
# access_state (322).

ACCESS_STATE_OF_CLASS = {CREDENTIAL: "unconfigured", ACCESS_DENIED: "access_denied",
                         QUOTA_EXHAUSTED: "quota_exhausted"}
ACCESS_STATES = ("unknown", "ok", "access_denied", "quota_exhausted", "unconfigured")
_SET_ACCESS_STATE_SQL = ("UPDATE shared.llm_provider_connection SET access_state = $2 "
                         "WHERE connection_key = $1 AND access_state IS DISTINCT FROM $2")
_access_written: dict[str, str] = {}


def access_state_of(error_class: Optional[str]) -> Optional[str]:
    """The access_state a connection circuit opened by ``error_class`` means (None: not a lasting account state)."""
    return ACCESS_STATE_OF_CLASS.get(error_class or "")


async def _write_access_state(conn, connection_key: str, state: str) -> str:
    return await conn.execute(_SET_ACCESS_STATE_SQL, connection_key, state)


def note_access_state(connection_key: str, state: Optional[str]) -> None:
    """Persist ``state`` as the connection's access_state unless this process already wrote it (the UPDATE itself
    changes nothing when the row already holds it). Never raises. ``LLM_CIRCUIT_BREAKER=off`` writes nothing."""
    if not enabled() or state not in ACCESS_STATES:
        return
    if _access_written.get(connection_key) == state:
        return
    try:
        set_access_state(connection_key, state)
        _access_written[connection_key] = state
        logger.info("llm_connection_access_state", connection=connection_key, access_state=state)
    except Exception as e:  # noqa: BLE001 — before migration 322 (no column) or DB down: try again next time
        logger.warning("llm_connection_access_state_write_failed", connection=connection_key, error=str(e)[:200])


def _set_access_state_in_db(connection_key: str, state: str) -> None:
    connections.run_sync(lambda: _with_conn(lambda conn: _write_access_state(conn, connection_key, state)))


# Tests replace it (conftest: no database in unit tests).
set_access_state: Callable[[str, str], None] = _set_access_state_in_db


def record_success(connection_key: str, model_key: str) -> None:
    """A provider call answered: close the circuit. A healthy deployment's success is written at most once a minute
    per process (it only refreshes ``last_ok_at``). AA-1018 part 2: the connection's access_state becomes 'ok' (written
    only when it changes). Never raises."""
    if not enabled():
        return
    note_access_state(connection_key, "ok")
    key = (connection_key, model_key)
    try:
        h = _get(key)
        healthy = (h is not None and not h.consecutive_failures and h.circuit_open_until is None
                   and h.probe_until is None)
        if healthy and time.monotonic() - _ok_written.get(key, float("-inf")) < OK_WRITE_INTERVAL_S:
            return
        before, _ = _transition(key, lambda cur: after_success(cur, _now()))
        _ok_written[key] = time.monotonic()
        if before is not None and before.circuit_open_until is not None:
            logger.info("llm_circuit_closed", connection=connection_key, model=model_key,
                        was=before.last_error_class)
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_breaker_record_failed", connection=connection_key, model=model_key, error=str(e)[:200])


def circuit_error(model_key: str, skips: list[tuple[str, Optional[str]]]) -> CircuitOpen:
    """The error for a model none of whose deployments could be called because their circuits are open
    (``skips``: (connection, class that opened it)). Account-level when every skipped circuit was opened by an
    account-level refusal, so the route / job ends exactly as if the calls had been made and refused."""
    classes = [c if c in RULES else OTHER for _, c in skips] or [OTHER]
    cls = account_level_class(classes) or (classes[0] if len(set(classes)) == 1 else OTHER)
    names = ", ".join(f"{k} ({c or OTHER})" for k, c in skips)
    return CircuitOpen(f"Model {model_key!r} not called: circuit open on {names}", cls)


# ── limiter ──────────────────────────────────────────────────────────────────────────────────────────────────────

class LimiterSaturated(RuntimeError):
    """The connection's own limit (max_concurrency / rpm_limit) had no slot within LIMIT_WAIT_S. Not a provider
    failure: no breaker count; classified throttled so the route moves on and a job retries with backoff."""

    llm_error_class = THROTTLED


_sleep = time.sleep


class _Limiter:
    def __init__(self, max_concurrency: Optional[int], rpm: Optional[int]):
        self.config = (max_concurrency, rpm)
        self.sem = threading.BoundedSemaphore(max_concurrency) if max_concurrency else None
        self.rpm = rpm
        self.calls: deque = deque()
        self.lock = threading.Lock()

    def _take_rate_slot(self, deadline: float, key: str) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.calls and now - self.calls[0] >= 60.0:
                    self.calls.popleft()
                if len(self.calls) < self.rpm:
                    self.calls.append(now)
                    return
                wait = 60.0 - (now - self.calls[0])
            if now + wait > deadline:
                raise LimiterSaturated(f"connection {key!r}: rpm_limit {self.rpm} reached (no slot within "
                                       f"{LIMIT_WAIT_S:.0f} s)")
            _sleep(max(wait, 0.05))

    @contextmanager
    def slot(self, key: str) -> Iterator[None]:
        deadline = time.monotonic() + LIMIT_WAIT_S
        sem = self.sem
        if sem is not None and not sem.acquire(timeout=LIMIT_WAIT_S):
            raise LimiterSaturated(f"connection {key!r}: max_concurrency {self.config[0]} reached (no slot within "
                                   f"{LIMIT_WAIT_S:.0f} s)")
        try:
            if self.rpm:
                self._take_rate_slot(deadline, key)
            yield
        finally:
            if sem is not None:
                sem.release()


_limiters: dict[str, _Limiter] = {}
_limiters_lock = threading.Lock()


def _limiter_for(conn: connections.ProviderConnection) -> Optional[_Limiter]:
    config = (conn.max_concurrency or None, conn.rpm_limit or None)
    if config == (None, None):
        return None
    with _limiters_lock:
        lim = _limiters.get(conn.connection_key)
        if lim is None or lim.config != config:     # a PATCHed limit takes effect once the registry reloads
            lim = _Limiter(*config)
            _limiters[conn.connection_key] = lim
        return lim


@contextmanager
def limit(conn: connections.ProviderConnection) -> Iterator[None]:
    """Hold one of the connection's call slots for the duration of a provider call (no-op without limits)."""
    lim = _limiter_for(conn)
    if lim is None:
        yield
        return
    with lim.slot(conn.connection_key):
        yield


def guarded(conn: connections.ProviderConnection, model_key: str, call: Callable[[], T], *,
            admit_call: bool = True, answered: tuple = ()) -> T:
    """One provider call on one deployment: the half-open admission (``admit_call``; the admin's connection test
    passes False — it IS the probe), a limiter slot, the call, and its outcome on the breaker. An exception of a type
    in ``answered`` (an unusable reply: the provider answered and billed) counts as a success. Raises the call's own
    exception unchanged, or CircuitOpen / LimiterSaturated without calling."""
    key = conn.connection_key
    if admit_call and not admit(key, model_key):
        raise circuit_error(model_key, [(key, open_class(key, model_key))])
    with limit(conn):
        try:
            result = call()
        except Exception as e:
            if answered and isinstance(e, answered):
                record_success(key, model_key)
            else:
                record_failure(key, model_key, e)
            raise
    record_success(key, model_key)
    return result


def snapshot() -> list[Health]:
    """The in-process copy (tests, debugging)."""
    with _lock:
        return list(_rows.values())


def reset() -> None:
    """Forget everything in process (tests)."""
    global _rows, _loaded_at, _db_down_until
    with _lock:
        _rows = {}
    _loaded_at = 0.0
    _db_down_until = 0.0
    _ok_written.clear()
    _access_written.clear()
    with _limiters_lock:
        _limiters.clear()
