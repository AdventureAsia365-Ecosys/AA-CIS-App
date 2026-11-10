"""shared/provider_circuit.py — AA-1025 B3 (S263 gateway plan): one shared circuit per non-LLM provider connection.

Jev (TypeSafe, ``typesafe``), DataForSEO (``dataforseo``) and Geoapify (``geoapify``) are called from both ECS tasks
(api and worker). Before this module only Jev had a breaker, in one process and only for a 402
(decide.jev_breaker_open); an outage or a run of 5xx cost every task its own timeouts, and DataForSEO / Geoapify had
none. Here each connection has ONE circuit whose state lives in ``shared.provider_health`` (migration 342), so a
circuit one task opens is skipped by every task.

State machine. The LLM breaker's (shared/llm_client/breaker.py: ``after_failure`` / ``after_success`` / ``claimed``
on a ``Health`` row, ``PROVIDER_RULES``: the LLM ``RULES`` with the access_denied cap kept at 30 min, AA-900): a
failure class opens the circuit after its threshold of failures in a row
(access_denied / quota_exhausted at once, throttled after 3, other after 5), for a short period that doubles on each
failed half-open probe (capped); after the period exactly ONE call across all tasks may try (a lease in the row); its
success closes the circuit. A caller records one outcome per logical call (after the client's own retries), so "5
failures" means 5 calls that each failed every retry.

What counts (``failure_class``): HTTP 401 / 403 access_denied, 402 quota_exhausted, 429 throttled, 404 / 405 /
408 / 410 / 5xx and a timeout or transport error other. Any other 4xx is a refusal of the request's own content (a bad
keyword, a malformed body): not counted, the provider answered. DataForSEO's five-digit body codes are read by their
first three digits (40100 -> 401): 401 / 402 / 403 and 5xxxx count, the other 4xxxx (a bad field) do not.

The calls. ``admit(key)`` before a call: None = go ahead (closed, or this call took the half-open probe lease), else
the refusal text (the circuit is open; nothing is sent; the caller writes its 0 USD refused ledger row and fails like
on an outage). ``record_success(key)`` / ``record_failure(key, error_class, message)`` after it. Every function here
never raises.

Alert. When a write opens the circuit (closed or half-open -> open) ``on_open`` runs once in the task that wrote it
(the row lock makes that one task): one admin bell (``shared.notifications`` event ``provider_circuit_open``, entity
``provider_connection:<key>``, at most one unread per connection per 24 h, under an advisory lock) and, when the bell
was written, one ``ops_alert_email`` job (``shared.ops_alerts.enqueue_provider_circuit_email``: at most one per
connection, class and UTC day). A Jev 402 already has its own exhausted bell + e-mail (AA-720 / AA-845), so a
``typesafe`` circuit opened by ``quota_exhausted`` raises no second one.

Storage. Reads are a cached snapshot of the table (``READ_TTL_S``); a success on a healthy connection is written at
most once a minute per process. Writes lock the row (``SELECT … FOR UPDATE``) and apply the pure transition. When the
table cannot be read or written (342 not applied, DB down) the same transitions run on the in-process copy: the
circuit degrades to a per-process one, never to an error on the call path. ``PROVIDER_CIRCUIT_BREAKER=off`` (ECS env,
read per call) turns it all off: ``admit`` always says go, nothing is written.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import asyncpg
import structlog

from shared.llm_client.breaker import (CLOSED, OPEN, PROVIDER_RULES, RULES, Health, after_failure, after_success,
                                       claimed, state_of)
from shared.llm_client.errors import ACCESS_DENIED, OTHER, QUOTA_EXHAUSTED, THROTTLED, redact_ids

logger = structlog.get_logger()

SWITCH_ENV = "PROVIDER_CIRCUIT_BREAKER"
_OFF = ("0", "false", "off", "no")

JEV = "typesafe"
DATAFORSEO = "dataforseo"
GEOAPIFY = "geoapify"
PROVIDERS = (JEV, DATAFORSEO, GEOAPIFY)
LABELS = {JEV: "Jev (TypeSafe)", DATAFORSEO: "DataForSEO", GEOAPIFY: "Geoapify"}
CLASSES = (ACCESS_DENIED, QUOTA_EXHAUSTED, THROTTLED, OTHER)
ROW_MODEL = "*"                 # the breaker's Health needs a model key; a provider circuit has none

ERROR_CHARS = 300
READ_TTL_S = 10.0
OK_WRITE_INTERVAL_S = 60.0
DB_RETRY_S = 30.0
DB_MISSING_RETRY_S = 300.0
_CONNECT_TIMEOUT_S = 5.0

_COLUMNS = ("connection_key", "last_ok_at", "last_error_at", "last_error_class", "last_error",
            "consecutive_failures", "circuit_open_until", "open_streak", "opened_at", "probe_until")
SELECT_SQL = f"SELECT {', '.join(_COLUMNS)} FROM shared.provider_health"
_ENSURE_SQL = ("INSERT INTO shared.provider_health (connection_key) VALUES ($1) "
               "ON CONFLICT (connection_key) DO NOTHING")
_LOCK_SQL = SELECT_SQL + " WHERE connection_key = $1 FOR UPDATE"
_UPDATE_SQL = ("UPDATE shared.provider_health SET last_ok_at = $2, last_error_at = $3, last_error_class = $4, "
               "last_error = $5, consecutive_failures = $6, circuit_open_until = $7, open_streak = $8, "
               "opened_at = $9, probe_until = $10, updated_at = now() WHERE connection_key = $1")

_lock = threading.Lock()
_rows: dict[str, Health] = {}
_loaded_at = float("-inf")
_loading = False
_db_down_until = 0.0
_ok_written: dict[str, float] = {}


# ── classification (pure) ────────────────────────────────────────────────────────────────────────────────────────

def failure_class(status: Optional[int]) -> Optional[str]:
    """The circuit class of a failed call from its HTTP status (None = a timeout / transport error = ``other``;
    404 / 405 / 410 = the endpoint is wrong or gone, every request fails alike), or
    None when the failure says nothing about the provider (a refusal of the request's own content: 400, 409, 413, 422
    and the other 4xx). A DataForSEO body code (5 digits) is read by its first three digits."""
    if status is None:
        return OTHER
    try:
        code = int(status)
    except (TypeError, ValueError):
        return OTHER
    if code >= 10000:              # a DataForSEO body code: only auth, payment and server failures say anything
        head = int(str(code)[:3])
        return {401: ACCESS_DENIED, 403: ACCESS_DENIED, 402: QUOTA_EXHAUSTED}.get(
            head, OTHER if head >= 500 else None)
    if code in (401, 403):
        return ACCESS_DENIED
    if code == 402:
        return QUOTA_EXHAUSTED
    if code == 429:
        return THROTTLED
    if code in (404, 405, 408, 410) or code >= 500:     # the endpoint itself is wrong or gone, a timeout, 5xx
        return OTHER
    if 400 <= code < 500:
        return None
    return OTHER


def enabled() -> bool:
    """``PROVIDER_CIRCUIT_BREAKER`` (ECS env, default on), read per call."""
    return (os.environ.get(SWITCH_ENV) or "").strip().lower() not in _OFF


def _now() -> datetime:
    return datetime.now(timezone.utc)


# S275: the marker of a ledger row refused because a provider circuit was open (nothing was sent). The row keeps the
# class that opened the circuit (llm_call_log's error_class CHECK has no circuit class; adding one needs a migration),
# and quality_signal.refusal = circuit_open says why it was refused. Readers count such a row as a refusal, never as a
# provider error: the failures that opened the circuit are rows of their own (the root tool tools/llm/gateway_status.py
# keeps the same rule; rows written before the marker it matches by ``refusal_text``'s "circuit open (" ... "nothing
# was sent to the provider").
REFUSAL_SIGNAL_KEY = "refusal"
REFUSAL_CIRCUIT_OPEN = "circuit_open"


def refusal_signal() -> dict:
    """The quality_signal keys of a circuit-open refused ledger row."""
    return {REFUSAL_SIGNAL_KEY: REFUSAL_CIRCUIT_OPEN}


def refusal_text(h: Health) -> str:
    """The refusal of a call while the circuit is open (ledger ``error``, job results)."""
    until = h.circuit_open_until.isoformat() if h.circuit_open_until else "the next probe"
    return (f"connection {h.connection_key!r} circuit open ({h.last_error_class or OTHER}) until {until}: "
            "nothing was sent to the provider")


# ── storage ──────────────────────────────────────────────────────────────────────────────────────────────────────

def row_to_health(row) -> Health:
    d = {k: row[k] for k in _COLUMNS}
    d["consecutive_failures"] = int(d["consecutive_failures"] or 0)
    d["open_streak"] = int(d["open_streak"] or 0)
    return Health(model_key=ROW_MODEL, **d)


def _args(h: Health) -> tuple:
    return (h.connection_key, h.last_ok_at, h.last_error_at, h.last_error_class, h.last_error,
            h.consecutive_failures, h.circuit_open_until, h.open_streak, h.opened_at, h.probe_until)


async def fetch_health(conn) -> list[Health]:
    """Every row of shared.provider_health (``conn``: an asyncpg connection or pool)."""
    return [row_to_health(r) for r in await conn.fetch(SELECT_SQL + " ORDER BY connection_key")]


async def apply_transition(conn, key: str,
                           fn: Callable[[Health], Optional[Health]]) -> tuple[Health, Optional[Health]]:
    """(row before, row written or None when ``fn`` declined) under the row lock, in one transaction."""
    async with conn.transaction():
        await conn.execute(_ENSURE_SQL, key)
        cur = row_to_health(await conn.fetchrow(_LOCK_SQL, key))
        new = fn(cur)
        if new is not None:
            await conn.execute(_UPDATE_SQL, *_args(new))
        return cur, new


async def _connect():
    from shared.secrets import get_database_url

    return await asyncpg.connect(get_database_url(), ssl="require", timeout=_CONNECT_TIMEOUT_S)


async def _run_in_db(fn):
    conn = await _connect()
    try:
        return await fn(conn)
    finally:
        await conn.close()


# Tests replace it (conftest: no database in unit tests; the integration tests pass a local connection).
db_runner: Callable[[Callable], Awaitable] = _run_in_db


async def _db(fn):
    """``fn(conn)`` on its own connection, unless the DB failed recently (then raise at once, no connect)."""
    global _db_down_until
    if time.monotonic() < _db_down_until:
        raise RuntimeError("provider_health unavailable (recent DB error)")
    try:
        return await db_runner(fn)
    except Exception as e:
        missing = type(e).__name__ == "UndefinedTableError"
        _db_down_until = time.monotonic() + (DB_MISSING_RETRY_S if missing else DB_RETRY_S)
        logger.warning("provider_circuit_db_unavailable", error=str(e)[:200],
                       hint="migration 342 not applied: the circuit runs per process" if missing else
                       "the circuit runs per process until the DB answers again")
        raise


async def _ensure_loaded() -> None:
    global _rows, _loaded_at, _loading
    with _lock:
        if _loading or time.monotonic() - _loaded_at < READ_TTL_S:
            return                  # fresh, or another coroutine / thread is reloading: use the current copy
        _loading = True
    try:
        rows = await _db(fetch_health)
        if not isinstance(rows, list) or not all(isinstance(r, Health) for r in rows):
            raise TypeError("unexpected provider health rows")
        with _lock:
            _rows = {r.connection_key: r for r in rows}
    except Exception:  # noqa: BLE001 — keep the in-process copy (module docstring)
        pass
    finally:
        with _lock:
            _loaded_at = time.monotonic()
            _loading = False


def _get(key: str) -> Optional[Health]:
    with _lock:
        return _rows.get(key)


async def _transition(key: str, fn: Callable[[Health], Optional[Health]]) -> tuple[Health, Optional[Health]]:
    """Apply ``fn`` in the DB (row lock) and mirror the result in process; in process only when the DB fails."""
    try:
        before, after = await _db(lambda conn: apply_transition(conn, key, fn))
        if not isinstance(before, Health):
            raise TypeError("unexpected transition result")
    except Exception:  # noqa: BLE001 — the per-process circuit (module docstring)
        with _lock:
            before = _rows.get(key) or Health(connection_key=key, model_key=ROW_MODEL)
            after = fn(before)
            if after is not None:
                _rows[key] = after
        return before, after
    with _lock:
        _rows[key] = after if after is not None else before
    return before, after


# ── the public side ──────────────────────────────────────────────────────────────────────────────────────────────

async def admit(key: str) -> Optional[str]:
    """None when a call to the provider may go now (closed; or half-open and this call took the one probe lease),
    else the refusal text (open, or half-open with the probe taken by another call). Never raises."""
    if not enabled():
        return None
    try:
        await _ensure_loaded()
        h = _get(key)
        now = _now()
        st = state_of(h, now)
        if st == CLOSED:
            return None
        if st == OPEN:
            return refusal_text(h)
        before, got = await _transition(key, lambda cur: claimed(cur, _now()))
        if got is not None:
            logger.info("provider_circuit_probe", connection=key)
            return None
        if state_of(before, _now()) == CLOSED:       # a probe in another task closed it meanwhile
            return None
        return refusal_text(before)
    except Exception as e:  # noqa: BLE001 — a circuit problem never stops a call
        logger.warning("provider_circuit_admit_failed", connection=key, error=str(e)[:200])
        return None


def open_class(key: str) -> Optional[str]:
    """The class that opened the provider's circuit (None when closed or unknown), from the in-process copy."""
    h = _get(key)
    return h.last_error_class if h is not None and state_of(h, _now()) != CLOSED else None


async def record_success(key: str) -> None:
    """The provider answered: close the circuit. A healthy connection's success is written at most once a minute per
    process. Never raises."""
    if not enabled():
        return
    try:
        h = _get(key)
        healthy = (h is not None and not h.consecutive_failures and h.circuit_open_until is None
                   and h.probe_until is None)
        if healthy and time.monotonic() - _ok_written.get(key, float("-inf")) < OK_WRITE_INTERVAL_S:
            return
        before, _ = await _transition(key, lambda cur: after_success(cur, _now()))
        _ok_written[key] = time.monotonic()
        if before is not None and before.circuit_open_until is not None:
            logger.info("provider_circuit_closed", connection=key, was=before.last_error_class)
    except Exception as e:  # noqa: BLE001
        logger.warning("provider_circuit_record_failed", connection=key, error=str(e)[:200])


def _lease_released(cur: Health) -> Optional[Health]:
    return replace(cur, probe_until=None) if cur.probe_until is not None else None


async def record_failure(key: str, error_class: Optional[str], message: str) -> None:
    """Count a failed call (``error_class`` from ``failure_class``; None = the request's own fault: not counted, a
    held probe lease is released). Opening the circuit raises the alert (``on_open``). Never raises."""
    if not enabled():
        return
    try:
        if error_class is None:
            h = _get(key)
            if h is not None and h.probe_until is not None:
                await _transition(key, _lease_released)
            return
        cls = error_class if error_class in RULES else OTHER
        text = redact_ids((message or "")[:ERROR_CHARS])
        before, after = await _transition(key, lambda cur: after_failure(cur, cls, text, _now(),
                                                                         rules=PROVIDER_RULES))
        if after is None or after.opened_at is None or (before is not None and before.opened_at == after.opened_at):
            return                  # still closed, or already open (a call that started before it opened)
        logger.warning("provider_circuit_opened", connection=key, error_class=cls,
                       until=after.circuit_open_until.isoformat() if after.circuit_open_until else None,
                       streak=after.open_streak, consecutive_failures=after.consecutive_failures)
        try:
            await on_open(after)
        except Exception as e:  # noqa: BLE001 — an alert problem never stops a call
            logger.warning("provider_circuit_alert_failed", connection=key, error=str(e)[:200])
    except Exception as e:  # noqa: BLE001
        logger.warning("provider_circuit_record_failed", connection=key, error=str(e)[:200])


# ── the alert when a circuit opens (admin bell + ops e-mail) ─────────────────────────────────────────────────────

ALERT_EVENT = "provider_circuit_open"
ENTITY_TYPE = "provider_connection"
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"
# The class a provider already alerts on by itself (Jev 402: AA-720 exhausted bell + AA-845 e-mail).
OWN_ALERT = {JEV: (QUOTA_EXHAUSTED,)}
_ALERT_LOCK_KEY = 1_025_000_342       # serialises the check-then-insert across tasks (the AA-756 race)
_RECENT_ALERT_SQL = ("SELECT 1 FROM shared.notifications WHERE event_type = $1 AND entity_id = $2 AND is_read = FALSE "
                     "AND created_at >= now() - interval '24 hours' LIMIT 1")
_INSERT_ALERT_SQL = ("INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, "
                     "payload, target_roles) VALUES ($1::uuid, 'system', $2, $3, $4, $5::jsonb, ARRAY['admin'])")
_CLASS_WORDS = {ACCESS_DENIED: "the provider refuses the key (HTTP 401 / 403)",
                QUOTA_EXHAUSTED: "the provider refuses for lack of credit (HTTP 402)",
                THROTTLED: "the provider keeps answering 429 (rate limit)",
                OTHER: "the provider is failing (5xx, timeouts or errors in a row)"}


def entity_id(key: str) -> str:
    return f"{ENTITY_TYPE}:{key}"


def alert_details(h: Health) -> dict:
    """The bell payload / e-mail details of an opened provider circuit: key, label, class, times, the last error with
    ARNs and account IDs masked. Never a key or a request body."""
    cls = h.last_error_class or OTHER
    until = h.circuit_open_until.isoformat() if h.circuit_open_until else None
    label = LABELS.get(h.connection_key, h.connection_key)
    return {"connection_key": h.connection_key, "label": label, "error_class": cls,
            "consecutive_failures": h.consecutive_failures, "open_streak": h.open_streak,
            "opened_at": h.opened_at.isoformat() if h.opened_at else None, "open_until": until,
            "last_error": redact_ids((h.last_error or "")[:ERROR_CHARS]) or None,
            "message": (f"{label} paused: {_CLASS_WORDS.get(cls, _CLASS_WORDS[OTHER])}. Every api and worker task "
                        f"skips it until {until or 'the next probe'}, then one call probes it; the calling stages "
                        "keep their own fallback.")}


async def alert_open(conn, h: Health) -> tuple[bool, Optional[str]]:
    """One admin bell for an opened provider circuit (at most one unread per connection per 24 h) and, when the bell
    was written, the ops e-mail job. (bell written, e-mail job id). ``conn`` is an asyncpg connection."""
    if h.last_error_class in OWN_ALERT.get(h.connection_key, ()):
        return False, None
    details = alert_details(h)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock($1)", _ALERT_LOCK_KEY)
        if await conn.fetchval(_RECENT_ALERT_SQL, ALERT_EVENT, entity_id(h.connection_key)):
            return False, None
        await conn.execute(_INSERT_ALERT_SQL, PLATFORM_TENANT_ID, ALERT_EVENT, ENTITY_TYPE,
                           entity_id(h.connection_key), json.dumps(details))
    job_id = None
    try:
        from shared import ops_alerts  # lazy: ops_alerts -> ses_mailer

        job_id = await ops_alerts.enqueue_provider_circuit_email(conn, details=details)
    except Exception as e:  # noqa: BLE001 — the bell row stays
        logger.warning("provider_circuit_email_enqueue_failed", connection=h.connection_key, error=str(e)[:200])
    logger.warning("provider_circuit_alert", connection=h.connection_key, error_class=h.last_error_class,
                   email_job_id=job_id)
    return True, job_id


async def _alert_in_db(h: Health) -> None:
    """The default ``on_open``: ``alert_open`` on its own connection, without the circuit's DB back-off (a failed alert
    write must not send the circuit to its per-process fallback). Never raises."""
    try:
        await db_runner(lambda conn: alert_open(conn, h))
    except Exception as e:  # noqa: BLE001
        logger.warning("provider_circuit_alert_failed", connection=h.connection_key, error=str(e)[:200])


# Tests replace it (conftest: no database in unit tests).
on_open: Callable[[Health], Awaitable[None]] = _alert_in_db


# ── reads for the admin page ─────────────────────────────────────────────────────────────────────────────────────

def view(h: Optional[Health], now: Optional[datetime] = None) -> Optional[dict]:
    """The circuit of one provider for the admin page (None = no row = closed and never failed). Pure."""
    if h is None:
        return None
    now = now or _now()
    iso = (lambda v: v.isoformat() if v is not None else None)
    return {"state": state_of(h, now), "error_class": h.last_error_class, "last_error": h.last_error,
            "consecutive_failures": h.consecutive_failures, "open_streak": h.open_streak,
            "opened_at": iso(h.opened_at), "open_until": iso(h.circuit_open_until),
            "last_ok_at": iso(h.last_ok_at), "last_error_at": iso(h.last_error_at)}


async def read_views(db, now: Optional[datetime] = None) -> dict[str, dict]:
    """{connection_key: view} from the table (``db``: asyncpg connection or pool). Raises on a failed read (the admin
    read guards it)."""
    now = now or _now()
    return {h.connection_key: view(h, now) for h in await fetch_health(db)}


def snapshot() -> list[Health]:
    """The in-process copy (tests, debugging)."""
    with _lock:
        return list(_rows.values())


def reset() -> None:
    """Forget everything in process (tests)."""
    global _rows, _loaded_at, _db_down_until, _loading
    with _lock:
        _rows = {}
        _loaded_at = float("-inf")
        _loading = False
    _db_down_until = 0.0
    _ok_written.clear()
