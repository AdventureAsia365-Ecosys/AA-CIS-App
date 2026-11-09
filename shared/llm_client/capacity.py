"""AA-1025 H2 (capacity S12; root ADR 0009 decisions 4-5, approved capacity plan S261 §2.2-§2.3, §5.1) — request
lines, a token-per-minute limiter per connection / model, and the backpressure the job worker applies.

Lines. Every LLM call belongs to one line:

- ``prod``: what a customer or a tenant waits for (tenant T-stages, Content studio drafts, admin actions, any call
  outside a job);
- ``bulk``: catalogue work that may wait (rerun waves: S1 rewrite, atomize, revalidate; place, embedding and
  writing-memory backfills);
- ``dev``: ad-hoc scripts (stage ``adhoc_*``), evaluation runs and the probes.

A job's line is its kind's (``KIND_LINES``, every registered kind listed; ``None`` = the kind makes no LLM call). The
worker binds the kind (``bind_kind``); ``LLMClient`` binds each request's line and token estimate (``bind_request``).

TPM limiter. ``shared.llm_capacity_limit`` (migration 348) holds a ``tpm_limit`` per connection (``model_key '*'``)
or per model on a connection, the unit AWS meters. Before a provider call ``breaker.guarded`` reserves the request's
tokens — the prompt at ~4 characters per token plus ``max_tokens``, the capacity plan's "TPM giữ chỗ" — in a rolling
60 s window of every limit that covers the deployment (``reserve``). A call that does not fit **waits** (prod and dev
up to ``WAIT_S``, bulk longer) until older reservations leave the window; only past that wait it raises
``LimiterSaturated`` (class throttled: not a provider failure, the route moves on, a job retries with backoff). The
bulk line may hold at most ``bulk_pct`` % of the limit and the dev line ``dev_pct`` %; both also wait while a prod call
waits on the same limit (bulk yields to prod). Per process, like ``rpm_limit``: each ECS task enforces the limit on
its own calls, so a row is set to one task's share of the quota (capacity plan §2.2 (a)). A request without a bound
estimate (embeddings, the admin's connection test) reserves nothing: embedding models are metered by requests
(``rpm_limit``), and a probe must never wait.

Backpressure (``claim_hold`` + ``held_kinds``). Before each claim the worker leaves out the job kinds whose line is
held, so their jobs **wait in the queue** instead of failing:

- a prod guard circuit is OPEN — a ``prod_guard`` row names a connection (``*``: its connection circuit,
  ``breaker.connection_health``) or one deployment (its circuit) — : every LLM kind of the prod and bulk lines waits.
  Once the circuit is half-open the prod kinds are claimed again, so a real call probes it;
- a prod guard circuit still refuses (open or half-open, nothing answered since), the app profile's ``bulk_paused``
  is on (``shared.llm_app_profile``, 345), or a person paused an open wave: the bulk kinds wait;
- this process has calls of a line waiting on a TPM limit (or one did within ``PRESSURE_HOLD_S``): that line's kinds
  wait (prod pressure holds bulk too).

Wave pause (``sync_waves``). While a prod guard circuit refuses or the bulk pause is on, the worker marks every open
wave paused (``shared.rerun_wave.paused_at``, ``paused_by 'capacity'``, migration 349; the status stays ``open``) with
one admin bell per wave, and resumes the waves it paused once neither holds. A wave a person paused stays paused until
a person resumes it.

Fail open everywhere: no table (before 348 / 349), an unreadable row or a DB error = no limit, no hold, nothing
paused (logged). ``LLM_CAPACITY=off`` (ECS env, read per call) turns the limiter and the holds off. The DB-derived hold
is cached ``HOLD_TTL_S`` per process.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping, Optional

import structlog

from . import connections

logger = structlog.get_logger()

PROD, BULK, DEV = "prod", "bulk", "dev"
LINES = (PROD, BULK, DEV)
SWITCH_ENV = "LLM_CAPACITY"
ANY_MODEL = "*"

WINDOW_S = 60.0                     # the rolling window of a TPM limit
WAIT_S = {PROD: 65.0, BULK: 180.0, DEV: 65.0}   # one window + margin: request N+1 waits, it does not fail
WAKE_S = 1.0                        # a waiter re-checks at least this often (a prod waiter may have left)
PRESSURE_HOLD_S = 30.0              # after a call of a line waited, new claims of that line's kinds wait this long
CACHE_TTL_SECONDS = 20.0            # the limit rows (the stage switch's 20 s)
FAILURE_TTL_SECONDS = 60.0
HOLD_TTL_S = 10.0                   # the worker's claim hold (the breaker's snapshot is 10 s too)
TPM_MAX = 100_000_000
NOTES_MAX = 500
WAVE_PAUSED_BY = "capacity"
WAVE_PAUSED_EVENT = "platform.rerun_wave.paused"
_PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"

# Every registered job kind and its line (None = it makes no LLM call; never held). A unit test fails when a kind is
# registered without an entry here, so a new LLM kind cannot slip past the backpressure.
KIND_LINES: dict[str, Optional[str]] = {
    # bulk: rerun waves and catalogue backfills
    "s1_rewrite": BULK, "a3_atomize": BULK, "revalidate": BULK, "s1_batch_ingest": BULK,
    "segment_research": BULK, "place_extract": BULK, "place_dedupe": BULK, "place_cards_build": BULK,
    "embedding_reembed": BULK, "writing_memory_mine": BULK, "writing_memory_r0": BULK, "writing_memory_r2": BULK,
    "writing_memory_shadow": BULK, "writing_signal_backfill": BULK, "writing_signal_fact_filter": BULK,
    # prod: tenant work someone waits for
    "t2_rewrite": PROD, "t7_slate": PROD, "t8_angles": PROD, "t9_write": PROD, "supply_content_draft": PROD,
    # dev: the probes (never held: a held probe could not close a circuit)
    "llm_connection_probe": DEV, "llm_contract_smoke": DEV, "llm_model_lifecycle_sync": DEV,
    # no LLM call
    "ai_usage_rollup": None,     # AA-1045: the hourly usage rollup of the paid-call ledgers (database only)
    "data_erasure": None, "inquiry_handoff_check": None, "kpi_snapshot_daily": None, "llm_cost_reconcile": None,
    "llm_route_watch": None,     # AA-1042: route alerts and digest from the ledger (database reads only)
    "ops_alert_email": None, "photo_destination_match": None, "photo_medium_backfill": None, "photo_sync": None,
    "place_geocode": None, "place_relocate": None, "place_shape": None,
    "quality_funnel_watch": None,  # AA-900: the nightly quality digest from a stored CIS-08 run (database reads only)
    "recompute": None, "report_email": None,
    "report_generate": None, "retention_cost_explorer_snapshot": None, "retention_decision_log": None,
    "retention_job": None, "retention_llm_call_log": None, "retention_tenant_api_usage": None,
    "s1_seo_prefetch": None, "staff_directory_reconcile": None, "staff_invite_email": None, "supply_media_purge": None,
    "tenant_account_tick": None, "tenant_billing_tick": None, "tenant_provision": None,
    "tour_retirement_fanout": None, "retired_page_draft": None,
    "tour_notice_email": None,
    "tour_graph_build": None, "tripplanner_purge": None, "writing_memory_kpi_weekly": None,
    "writing_memory_post_activation": None,
}


def enabled() -> bool:
    """``LLM_CAPACITY`` (ECS env, default on), read per call."""
    return (os.environ.get(SWITCH_ENV) or "on").strip().lower() not in ("off", "false", "0", "no")


# ── lines ────────────────────────────────────────────────────────────────────────────────────────────────────────────

_kind: ContextVar[Optional[str]] = ContextVar("llm_capacity_job_kind", default=None)
_request: ContextVar[Optional[tuple[int, str]]] = ContextVar("llm_capacity_request", default=None)


@contextmanager
def bind_kind(kind: Optional[str]) -> Iterator[None]:
    """The job kind whose handler runs in this context (the worker binds it for each job)."""
    token = _kind.set(kind)
    try:
        yield
    finally:
        _kind.reset(token)


def line_of_kind(kind: Optional[str]) -> Optional[str]:
    return KIND_LINES.get(kind or "")


def line_for(stage: Optional[str] = None) -> str:
    """The line of a call made now for ``stage``: the bound job kind's line, else dev for an ad-hoc stage or an
    evaluation run, else prod."""
    line = line_of_kind(_kind.get())
    if line:
        return line
    if (stage or "").startswith("adhoc_"):
        return DEV
    try:
        from .call_log import eval_label
        if eval_label() is not None:
            return DEV
    except Exception:  # noqa: BLE001 — a line is a hint, never an error
        pass
    return PROD


def estimate_tokens(request: Any) -> int:
    """The tokens a request reserves: its prompt at ~4 characters per token plus its whole ``max_tokens`` (the same
    estimate as the central budget's ``client.estimate_call_usd``). Never raises (0)."""
    try:
        chars = len(getattr(request, "system_prompt", None) or "") + len(getattr(request, "user_prompt", None) or "")
        chars += sum(len(str(s.get("content", ""))) for s in (getattr(request, "few_shots", None) or [])
                     if isinstance(s, dict))
        return chars // 4 + 1 + max(int(getattr(request, "max_tokens", 0) or 0), 0)
    except Exception:  # noqa: BLE001
        return 0


@contextmanager
def bind_request(tokens: int, line: str) -> Iterator[None]:
    """The token estimate and line of the request being dispatched (``LLMClient`` binds it around the route)."""
    token = _request.set((max(int(tokens or 0), 0), line if line in LINES else PROD))
    try:
        yield
    finally:
        _request.reset(token)


def set_request(tokens: int, line: str):
    """``bind_request`` for code that keeps a try / finally of its own: returns the token for ``reset_request``."""
    return _request.set((max(int(tokens or 0), 0), line if line in LINES else PROD))


def reset_request(token) -> None:
    _request.reset(token)


def bound_request() -> Optional[tuple[int, str]]:
    return _request.get()


# ── limit rows (shared.llm_capacity_limit, 348) ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Limit:
    connection_key: str
    model_key: str = ANY_MODEL
    tpm_limit: Optional[int] = None
    bulk_pct: int = 50
    dev_pct: Optional[int] = None
    prod_guard: bool = False
    notes: Optional[str] = None
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None

    def share(self, line: str) -> Optional[int]:
        """The tokens ``line`` may hold in one window (None = the whole limit)."""
        if not self.tpm_limit:
            return None
        pct = self.bulk_pct if line == BULK else self.dev_pct if line == DEV else None
        return None if pct is None else max(int(self.tpm_limit * pct / 100), 1)

    def as_dict(self) -> dict:
        return asdict(self)


def limit_of(raw: Any) -> Optional[Limit]:
    """A row (``to_jsonb`` text or dict) as a Limit; None when it is unusable."""
    try:
        d = json.loads(raw) if isinstance(raw, str) else dict(raw)
        tpm = d.get("tpm_limit")
        dev = d.get("dev_pct")
        return Limit(connection_key=str(d["connection_key"]), model_key=str(d.get("model_key") or ANY_MODEL),
                     tpm_limit=int(tpm) if tpm is not None else None, bulk_pct=int(d.get("bulk_pct") or 50),
                     dev_pct=int(dev) if dev is not None else None, prod_guard=bool(d.get("prod_guard")),
                     notes=d.get("notes"), updated_at=str(d["updated_at"]) if d.get("updated_at") else None,
                     updated_by=d.get("updated_by"))
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_capacity_row_unusable", error=str(e)[:200])
        return None


SELECT_SQL = ("SELECT to_jsonb(c) AS row FROM shared.llm_capacity_limit c "
              "ORDER BY c.connection_key, c.model_key")


class CapacityUnavailable(RuntimeError):
    """The limit table is not there (migration 348 not applied): the admin routes answer 503."""


async def read_all(conn) -> list[Limit]:
    """Every limit row. Raises ``CapacityUnavailable`` when the table is missing. ``conn``: asyncpg."""
    import asyncpg
    try:
        rows = await conn.fetch(SELECT_SQL)
    except asyncpg.exceptions.UndefinedTableError as e:
        raise CapacityUnavailable("the capacity limits need migration 348") from e
    return [lim for lim in (limit_of(r["row"]) for r in rows) if lim is not None]


async def _load() -> list[Limit]:
    conn = await connections._connect()
    try:
        return await read_all(conn)
    finally:
        await conn.close()


def loader() -> list[Limit]:
    """The sync loader of the cache (tests replace it; conftest: no database in unit tests)."""
    return connections.run_sync(_load)


async def loader_async(db) -> list[Limit]:
    """The async loader through ``db`` (a pool; tests replace it)."""
    async with db.acquire() as conn:
        return await read_all(conn)


_limits: list[Limit] = []
_loaded_at = 0.0
_failed_at = 0.0
_load_lock = threading.Lock()


def _fresh(now: float) -> bool:
    return bool((_loaded_at and now - _loaded_at < CACHE_TTL_SECONDS)
                or (_failed_at and now - _failed_at < FAILURE_TTL_SECONDS))


def _store(rows: Optional[list[Limit]], error: Optional[BaseException] = None) -> None:
    global _limits, _loaded_at, _failed_at
    if rows is None:
        _limits, _loaded_at, _failed_at = [], 0.0, time.monotonic()
        logger.warning("llm_capacity_read_failed", error=str(error)[:300] if error else None,
                       hint="no token limit and no prod guard until the limits can be read")
    else:
        _limits, _loaded_at, _failed_at = list(rows), time.monotonic(), 0.0


def limits() -> list[Limit]:
    """The cached limit rows ([] = none, or unreadable). Never raises."""
    if _fresh(time.monotonic()):
        return _limits
    with _load_lock:
        if _fresh(time.monotonic()):
            return _limits
        try:
            _store(loader())
        except Exception as e:  # noqa: BLE001 — fail open (module docstring)
            _store(None, e)
    return _limits


async def limits_async(db) -> list[Limit]:
    """``limits()`` for async code holding a pool: the same cache, refreshed through ``db``. Never raises."""
    if _fresh(time.monotonic()):
        return _limits
    try:
        rows = await loader_async(db)
    except Exception as e:  # noqa: BLE001
        with _load_lock:
            _store(None, e)
        return _limits
    with _load_lock:
        _store(rows)
    return _limits


def invalidate() -> None:
    """Forget the cached rows (the admin's writes, tests)."""
    global _loaded_at, _failed_at, _hold, _hold_at
    with _load_lock:
        _loaded_at = _failed_at = 0.0
    _hold, _hold_at = None, 0.0


def covering(rows: Iterable[Limit], connection_key: str, model_key: str) -> list[Limit]:
    """The rows with a token limit that cover a call on (connection, model): the connection's ``*`` row and the
    model's own row. Pure."""
    return [r for r in rows if r.tpm_limit and r.connection_key == connection_key
            and r.model_key in (ANY_MODEL, model_key)]


# ── the TPM limiter (per process) ───────────────────────────────────────────────────────────────────────────────────

class _Window:
    """The reservations of one limit in the rolling window: (time, tokens, line)."""

    def __init__(self, limit: Limit):
        self.limit = limit
        self.config = (limit.tpm_limit, limit.bulk_pct, limit.dev_pct)
        self.held: deque = deque()
        self.waiting = {PROD: 0, BULK: 0, DEV: 0}

    def purge(self, now: float) -> None:
        while self.held and now - self.held[0][0] >= WINDOW_S:
            self.held.popleft()

    def used(self, line: Optional[str] = None) -> int:
        return sum(t for _, t, ln in self.held if line is None or ln == line)

    def wait_for(self, tokens: int, line: str, now: float) -> Optional[float]:
        """None when ``tokens`` of ``line`` fit now, else the seconds until the oldest reservation that blocks it
        leaves the window (at least WAKE_S / 10). A request bigger than the whole limit fits only into an empty
        window (it would never fit otherwise); bulk and dev also wait while a prod call waits here."""
        self.purge(now)
        cap = int(self.limit.tpm_limit or 0)
        total = self.used()
        blocked = bool(self.held) and total + tokens > cap
        share = self.limit.share(line)
        if share is not None:
            mine = self.used(line)
            blocked = blocked or (mine > 0 and mine + tokens > share)
        if line != PROD and self.waiting[PROD]:
            blocked = True
        if not blocked:
            return None
        if not self.held:
            return WAKE_S
        return max(WINDOW_S - (now - self.held[0][0]), WAKE_S / 10)

    def add(self, now: float, tokens: int, line: str) -> None:
        self.held.append((now, tokens, line))

    def snapshot(self, now: float) -> dict:
        self.purge(now)
        return {"connection_key": self.limit.connection_key, "model_key": self.limit.model_key,
                "tpm_limit": self.limit.tpm_limit, "used": self.used(),
                "used_by_line": {ln: self.used(ln) for ln in LINES}, "waiting": dict(self.waiting)}


_cond = threading.Condition()
_windows: dict[tuple[str, str], _Window] = {}
_pressure_until: dict[str, float] = {}
_clock = time.monotonic


def _wait(cond: threading.Condition, timeout: float) -> None:
    cond.wait(timeout)


def _window(limit: Limit) -> _Window:
    key = (limit.connection_key, limit.model_key)
    w = _windows.get(key)
    if w is None or w.config != (limit.tpm_limit, limit.bulk_pct, limit.dev_pct):
        old = w
        w = _Window(limit)
        if old is not None:              # an edited limit keeps what this process already sent in the window
            w.held = old.held
            w.waiting = old.waiting
        _windows[key] = w
    return w


class TokenLimitSaturated(RuntimeError):
    """Raised inside ``reserve``; ``breaker`` turns it into its LimiterSaturated (class throttled)."""


def reserve(connection_key: str, model_key: str, tokens: Optional[int] = None, line: Optional[str] = None) -> float:
    """Reserve the bound request's tokens (or ``tokens`` / ``line``) in every TPM window that covers the deployment,
    waiting while they do not fit. Returns the seconds waited (0.0 without a limit, an estimate, or when it fit at
    once). Raises ``TokenLimitSaturated`` past the line's ``WAIT_S``. A problem reading the limits never stops a
    call."""
    if not enabled():
        return 0.0
    bound = bound_request()
    if tokens is None:
        if bound is None:
            return 0.0
        tokens, line = bound[0], line or bound[1]
    line = line if line in LINES else PROD
    if not tokens:
        return 0.0
    try:
        rows = covering(limits(), connection_key, model_key)
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_capacity_limits_failed", error=str(e)[:200])
        return 0.0
    if not rows:
        return 0.0
    started = _clock()
    deadline = started + WAIT_S.get(line, WAIT_S[PROD])
    with _cond:
        windows = [_window(r) for r in rows]
        for w in windows:
            w.waiting[line] += 1
        try:
            logged = False
            did_wait = False                 # only a real sleep counts: two clock reads always differ a little
            while True:
                now = _clock()
                waits = [w.wait_for(tokens, line, now) for w in windows]
                if all(x is None for x in waits):
                    for w in windows:
                        w.add(now, tokens, line)
                    if not did_wait:
                        return 0.0
                    waited = now - started
                    _pressure_until[line] = now + PRESSURE_HOLD_S
                    logger.info("llm_tpm_waited", connection=connection_key, model=model_key, line=line,
                                tokens=tokens, waited_s=round(waited, 2))
                    return waited
                _pressure_until[line] = now + PRESSURE_HOLD_S
                left = deadline - now
                if left <= 0:
                    logger.warning("llm_tpm_saturated", connection=connection_key, model=model_key, line=line,
                                   tokens=tokens, waited_s=round(now - started, 2))
                    raise TokenLimitSaturated(
                        f"connection {connection_key!r} model {model_key!r}: token limit reached for the {line} line "
                        f"(no room for {tokens} tokens within {WAIT_S.get(line, WAIT_S[PROD]):.0f} s)")
                if not logged:
                    logger.info("llm_tpm_waiting", connection=connection_key, model=model_key, line=line,
                                tokens=tokens)
                    logged = True
                did_wait = True
                _wait(_cond, min(max(x for x in waits if x is not None), left, WAKE_S))
        finally:
            for w in windows:
                w.waiting[line] -= 1
            _cond.notify_all()


def pressure(now: Optional[float] = None) -> frozenset:
    """The lines with a call waiting on a TPM limit in this process now, or one that waited within
    ``PRESSURE_HOLD_S``."""
    now = _clock() if now is None else now
    with _cond:
        waiting = {ln for w in _windows.values() for ln, n in w.waiting.items() if n > 0}
        recent = {ln for ln, until in _pressure_until.items() if until > now}
    return frozenset(waiting | recent)


def windows_snapshot() -> list[dict]:
    """This process's windows (the admin page; tests)."""
    now = _clock()
    with _cond:
        return [w.snapshot(now) for w in _windows.values()]


def reset() -> None:
    """Forget every window, the pressure and the caches (tests)."""
    with _cond:
        _windows.clear()
        _pressure_until.clear()
    invalidate()


# ── backpressure: what the worker holds ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Hold:
    prod_open: tuple = ()           # prod guard circuits OPEN now ("conn" or "conn/model", with the class)
    prod_refusing: tuple = ()       # prod guard circuits not closed (open or half-open, nothing answered since)
    bulk_paused: bool = False       # the app profile's bulk pause
    waves_paused_by_staff: int = 0  # open waves a person paused
    pressure: frozenset = field(default_factory=frozenset)

    def lines(self) -> frozenset:
        """The lines whose job kinds wait in the queue now. Never dev (the probes)."""
        out = set()
        if self.prod_open or PROD in self.pressure:
            out |= {PROD, BULK}
        if self.prod_refusing or self.bulk_paused or self.waves_paused_by_staff or BULK in self.pressure:
            out.add(BULK)
        return frozenset(out)

    def wave_pause_reason(self) -> Optional[str]:
        """Why the worker pauses the open waves now (None = it resumes the waves it paused)."""
        if self.prod_refusing:
            return "prod circuit refusing: " + ", ".join(self.prod_refusing)
        if self.bulk_paused:
            return "bulk work paused in the app profile"
        return None

    def as_dict(self) -> dict:
        return {"prod_open": list(self.prod_open), "prod_refusing": list(self.prod_refusing),
                "bulk_paused": self.bulk_paused, "waves_paused_by_staff": self.waves_paused_by_staff,
                "pressure": sorted(self.pressure), "held_lines": sorted(self.lines()),
                "wave_pause_reason": self.wave_pause_reason()}


def hold_of(rows: Iterable[Limit], health: Iterable[Any], *, bulk_paused: bool = False, waves_paused_by_staff: int = 0,
            pressure_lines: Iterable[str] = (), now: Optional[datetime] = None) -> Hold:
    """The hold from the limit rows, the breaker's health rows (``breaker.Health``), the bulk pause and this process's
    TPM pressure. Pure."""
    from . import breaker
    now = now or datetime.now(timezone.utc)
    health = list(health)
    opened, refusing = [], []
    for r in rows:
        if not r.prod_guard:
            continue
        if r.model_key == ANY_MODEL:
            h = breaker.connection_health(health, r.connection_key, now)
            name = r.connection_key
        else:
            h = next((x for x in health if x.connection_key == r.connection_key and x.model_key == r.model_key),
                     None)
            name = f"{r.connection_key}/{r.model_key}"
        state = breaker.state_of(h, now)
        if state == breaker.CLOSED:
            continue
        label = f"{name} ({h.last_error_class or 'other'})"
        refusing.append(label)
        if state == breaker.OPEN:
            opened.append(label)
    return Hold(prod_open=tuple(opened), prod_refusing=tuple(refusing), bulk_paused=bool(bulk_paused),
                waves_paused_by_staff=int(waves_paused_by_staff or 0), pressure=frozenset(pressure_lines))


def held_kinds(lines: Iterable[str], kinds: Iterable[str]) -> frozenset:
    """The kinds (of ``kinds``) whose line is in ``lines``. Pure."""
    lines = frozenset(lines)
    return frozenset(k for k in kinds if KIND_LINES.get(k) in lines)


_STAFF_PAUSED_SQL = ("SELECT count(*)::int AS n FROM shared.rerun_wave "
                     "WHERE status = 'open' AND paused_at IS NOT NULL AND paused_by <> $1")
_hold: Optional[Hold] = None
_hold_at = 0.0


async def _read_hold(pool) -> Hold:
    import asyncpg

    from . import app_profile, breaker
    from .call_log import APP
    rows = await limits_async(pool)
    health = []
    if any(r.prod_guard for r in rows):
        try:
            health = [breaker.row_to_health(r) for r in await pool.fetch(breaker.SELECT_SQL)]
        except asyncpg.exceptions.UndefinedTableError:
            health = []
    paused = app_profile.profile(APP, await app_profile.profiles_async(pool)).bulk_paused
    staff = 0
    try:
        staff = int(await pool.fetchval(_STAFF_PAUSED_SQL, WAVE_PAUSED_BY) or 0)
    except (asyncpg.exceptions.UndefinedColumnError, asyncpg.exceptions.UndefinedTableError):
        staff = 0                       # before migration 349: no wave can be paused
    return hold_of(rows, health, bulk_paused=paused, waves_paused_by_staff=staff)


async def claim_hold(pool, *, with_pressure: bool = True) -> Hold:
    """The hold of this process now: the DB part (cached ``HOLD_TTL_S``) plus this process's TPM pressure. Never
    raises (an empty hold when anything fails)."""
    global _hold, _hold_at
    if not enabled():
        return Hold()
    now = time.monotonic()
    if _hold is None or now - _hold_at >= HOLD_TTL_S:
        try:
            _hold = await _read_hold(pool)
        except Exception as e:  # noqa: BLE001 — fail open: nothing is held
            logger.warning("llm_capacity_hold_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
            _hold = Hold()
        _hold_at = now
    return _hold if not with_pressure else with_pressure_of(_hold)


def with_pressure_of(hold: Hold) -> Hold:
    """``hold`` with this process's TPM pressure now."""
    return Hold(prod_open=hold.prod_open, prod_refusing=hold.prod_refusing, bulk_paused=hold.bulk_paused,
                waves_paused_by_staff=hold.waves_paused_by_staff, pressure=pressure())


def without_held(caps: Mapping[str, int], lines: Iterable[str]) -> dict[str, int]:
    """``caps`` minus the kinds whose line is held: those jobs stay queued. Pure."""
    held = held_kinds(lines, caps)
    return {k: v for k, v in caps.items() if k not in held}


# ── wave pause (shared.rerun_wave, 349) ─────────────────────────────────────────────────────────────────────────────

_PAUSE_SQL = """
    UPDATE shared.rerun_wave SET paused_at = now(), paused_by = $1, pause_reason = left($2, 500)
     WHERE status = 'open' AND paused_at IS NULL
 RETURNING id::text AS id, name
"""
_RESUME_SQL = """
    UPDATE shared.rerun_wave SET paused_at = NULL, paused_by = NULL, pause_reason = NULL
     WHERE status = 'open' AND paused_by = $1
 RETURNING id::text AS id, name
"""
_BELL_SQL = ("INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, payload, "
             "target_roles) VALUES ($1::uuid, 'system', $2, 'rerun_wave', $3, $4::jsonb, ARRAY['admin'])")


async def sync_waves(pool, hold: Hold) -> dict:
    """Pause every open, unpaused wave while ``hold.wave_pause_reason()`` says so (one admin bell per wave paused);
    resume the waves this rule paused once it does not. Returns {"paused": [...], "resumed": [...]} (ids). Never
    raises: before migration 349 (no columns) or on a DB error nothing changes."""
    import asyncpg
    out: dict = {"paused": [], "resumed": []}
    if not enabled():
        return out
    reason = hold.wave_pause_reason()
    try:
        async with pool.acquire() as conn:
            if reason:
                async with conn.transaction():
                    rows = await conn.fetch(_PAUSE_SQL, WAVE_PAUSED_BY, reason)
                    for r in rows:
                        await conn.execute(_BELL_SQL, _PLATFORM_TENANT_ID, WAVE_PAUSED_EVENT, r["id"], json.dumps(
                            {"message": f"Rerun wave '{r['name']}' paused: {reason}. Its jobs wait in the queue and "
                                        "the wave resumes by itself when this clears.", "reason": reason}))
                out["paused"] = [r["id"] for r in rows]
            else:
                out["resumed"] = [r["id"] for r in await conn.fetch(_RESUME_SQL, WAVE_PAUSED_BY)]
    except (asyncpg.exceptions.UndefinedColumnError, asyncpg.exceptions.UndefinedTableError):
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("llm_capacity_wave_sync_failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        return out
    if out["paused"]:
        logger.warning("rerun_wave_paused", waves=out["paused"], reason=reason)
    if out["resumed"]:
        logger.info("rerun_wave_resumed", waves=out["resumed"])
    return out


# ── admin writes (PUT / DELETE /admin/ai-gateway/capacity) ──────────────────────────────────────────────────────────

_UPSERT_SQL = """
    INSERT INTO shared.llm_capacity_limit (connection_key, model_key, tpm_limit, bulk_pct, dev_pct, prod_guard, notes,
                                           updated_at, updated_by)
    VALUES ($1, $2, $3, $4, $5, $6, $7, now(), $8)
    ON CONFLICT (connection_key, model_key) DO UPDATE
       SET tpm_limit = EXCLUDED.tpm_limit, bulk_pct = EXCLUDED.bulk_pct, dev_pct = EXCLUDED.dev_pct,
           prod_guard = EXCLUDED.prod_guard, notes = EXCLUDED.notes, updated_at = now(),
           updated_by = EXCLUDED.updated_by
 RETURNING to_jsonb(llm_capacity_limit) AS row
"""
_BEFORE_SQL = ("SELECT to_jsonb(c) AS row FROM shared.llm_capacity_limit c "
               "WHERE c.connection_key = $1 AND c.model_key = $2 FOR UPDATE")
_DELETE_SQL = ("DELETE FROM shared.llm_capacity_limit WHERE connection_key = $1 AND model_key = $2 "
               "RETURNING to_jsonb(llm_capacity_limit) AS row")
_CONNECTION_SQL = "SELECT kind FROM shared.llm_provider_connection WHERE connection_key = $1"
_DEPLOYMENT_SQL = ("SELECT 1 AS ok FROM shared.llm_model_deployment WHERE connection_key = $1 AND model_key = $2")
_AUDIT_SQL = ("INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, details) "
              "VALUES ($1, $2, 'llm_capacity_limit', $3, $4::jsonb)")


def validate(fields: Mapping[str, Any]) -> dict:
    """The row fields of a PUT, checked as migration 348 does. Raises ValueError with the reason."""
    tpm = fields.get("tpm_limit")
    if tpm is not None and not (isinstance(tpm, int) and not isinstance(tpm, bool) and 1 <= tpm <= TPM_MAX):
        raise ValueError(f"tpm_limit must be an integer from 1 to {TPM_MAX}, or null")
    bulk = fields.get("bulk_pct", 50)
    if not (isinstance(bulk, int) and not isinstance(bulk, bool) and 1 <= bulk <= 100):
        raise ValueError("bulk_pct must be an integer from 1 to 100")
    dev = fields.get("dev_pct")
    if dev is not None and not (isinstance(dev, int) and not isinstance(dev, bool) and 1 <= dev <= 100):
        raise ValueError("dev_pct must be an integer from 1 to 100, or null")
    guard = bool(fields.get("prod_guard", False))
    if tpm is None and not guard:
        raise ValueError("set tpm_limit, prod_guard or both (delete the row to remove the limit)")
    notes = fields.get("notes")
    if notes is not None and len(str(notes)) > NOTES_MAX:
        raise ValueError(f"notes must be at most {NOTES_MAX} characters")
    return {"tpm_limit": tpm, "bulk_pct": bulk, "dev_pct": dev, "prod_guard": guard,
            "notes": (str(notes).strip() or None) if notes is not None else None}


async def put(pool, connection_key: str, model_key: str, fields: Mapping[str, Any], *, actor: str) -> dict:
    """Create or replace one limit row, audited (``llm_capacity_limit_set``). LookupError: no such connection, or the
    model is not deployed on it. ValueError: a bad value. CapacityUnavailable: no table (348)."""
    import asyncpg
    row = validate(fields)
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                kind = await conn.fetchval(_CONNECTION_SQL, connection_key)
                if kind is None or kind not in connections.LLM_KINDS:
                    raise LookupError("no such LLM connection")
                if model_key != ANY_MODEL and not await conn.fetchval(_DEPLOYMENT_SQL, connection_key, model_key):
                    raise LookupError("the model is not deployed on this connection")
                before = await conn.fetchrow(_BEFORE_SQL, connection_key, model_key)
                after = await conn.fetchrow(_UPSERT_SQL, connection_key, model_key, row["tpm_limit"], row["bulk_pct"],
                                            row["dev_pct"], row["prod_guard"], row["notes"], actor[:128])
                await conn.execute(_AUDIT_SQL, actor, "llm_capacity_limit_set", f"{connection_key}/{model_key}",
                                   json.dumps({"before": _plain(before), "after": _plain(after)}, default=str))
        except asyncpg.exceptions.UndefinedTableError as e:
            raise CapacityUnavailable("the capacity limits need migration 348") from e
    invalidate()
    out = limit_of(after["row"])
    return out.as_dict() if out else {}


async def delete(pool, connection_key: str, model_key: str, *, actor: str) -> bool:
    """Remove one limit row (back to no limit), audited (``llm_capacity_limit_cleared``). False when there was none."""
    import asyncpg
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                gone = await conn.fetchrow(_DELETE_SQL, connection_key, model_key)
                if gone is not None:
                    await conn.execute(_AUDIT_SQL, actor, "llm_capacity_limit_cleared",
                                       f"{connection_key}/{model_key}",
                                       json.dumps({"before": _plain(gone)}, default=str))
        except asyncpg.exceptions.UndefinedTableError as e:
            raise CapacityUnavailable("the capacity limits need migration 348") from e
    invalidate()
    return gone is not None


def _plain(row) -> Optional[dict]:
    if row is None:
        return None
    raw = row["row"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)
