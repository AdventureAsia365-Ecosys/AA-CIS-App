"""AA-900 (S271, root ADR 0020 "Gateway amendments" d8) — the cassette recorder: the raw reply of every provider
attempt, with the request and prompt shas, for replay.

Record-only (wave W0). ``LLMClient._attempt`` calls ``record_attempt`` once per provider attempt that RETURNED a
reply (every role and stage, shadows, the admin probe and the gateway's truncation retry included), and (S272)
``record_unusable_attempt`` once per attempt that ended billed but unusable (the provider answered and billed, the
gateway refused the reply: empty, not JSON, schema mismatch, a refusal stop): that row holds the reply the
BilledUnusableReply carried (``reply_facts``; empty when it carried none) and is marked ``stop_reason =
'billed_unusable'`` or ``'billed_unusable:<provider stop reason>'`` (``UNUSABLE_MARK``; the 385 CHECK lets
``reply_withheld`` hold only 'pii_stage'). The row goes to
``shared.llm_cassette`` (CIS migration 385) on a background write, best-effort like the ledger's gateway rows: a
failure is logged and counted (``stats()``), never raised, and the call goes on. Nothing on a live path reads it yet;
``get_by_request_sha`` / ``get_by_call_log_id`` are the replay helpers.

What a row holds (no prompt text, DM-6.4 / DM-10.4: the prompt is kept as its sha only):
- ``request_sha256`` = ``request_sha(request, model_key=..., effort=...)``: sha256 of the canonical JSON of the
  messages (system, few-shots, user) and the params (model key, max_tokens, temperature, seed, json_schema, effort).
  A replay harness computes it from the request it is about to send and looks the reply up.
- the raw reply, capped at ``REPLY_CAP_BYTES`` of UTF-8 (``reply_truncated``), with the sha and size of the whole
  reply; a stage of data class ``pii`` stores no text (``reply_withheld = 'pii_stage'``).
- the answered model (provider-reported id), wire id, connection, provider request id, rung (route position),
  attempt number, effort, max_tokens, stop reason, shadow / truncation-retry marks.
- the links: ``route_trace`` + ``attempt_no`` (the same two facts the answered ``llm_call_log`` row carries in
  ``quality_signal.route_trace`` and ``attempt_no``; the ledger row id is resolved through them, the ledger insert is
  not changed), the bound job and tenant (call_log's context), the bound eval run, and what a caller binds with
  ``bind_owner(content_piece_id=..., tour_id=..., eval_item=...)``.

The ledger link rule (S272, test_aa900_s272_cassette_gaps.py): one cassette row per provider attempt that got a reply
(answered, cut, or billed unusable), joined to its ``llm_call_log`` row on (route_trace, attempt_no), never on the
model name. The ledger's ``model`` is the reply's label (``haiku-4-5`` for both the legacy key ``haiku`` and the
catalog key ``haiku-4-5`` on acc3), the cassette's ``model_key`` is the key the gateway called (``haiku`` /
``haiku-4-5``): counted by model name two probes of one model look like 2 ledger rows : 1 cassette row. A
truncation retry shares the cut attempt's number (``is_truncation_retry`` tells it apart).

Not recorded, by design: embeddings (``embed.py``: the reply is a vector, not text; the caller stores it in its own
vector column and a re-embed is deterministic, so there is nothing to replay against a changed rule), Jev
``decide()`` (its own client, a decision log), and an attempt that failed with nothing billed (no reply).
A legacy-key structured request (``LLMClient._legacy_structured``) is checked after its attempt returned: its reply is
recorded as a normal row and the later unusable error is not marked here (that path warns on use).

Switch: ``LLM_CASSETTE`` = ``record`` (default) | ``off`` (``0``, ``false``, ``no`` too): off writes nothing.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

import asyncpg
import structlog

from shared.secrets import get_database_url

logger = structlog.get_logger()

SWITCH_ENV = "LLM_CASSETTE"
MODE_RECORD = "record"
MODE_OFF = "off"
_OFF = frozenset({"off", "0", "false", "no"})
# 256 KiB of UTF-8 (CHECK llm_cassette_reply_cap_check of CIS 385).
REPLY_CAP_BYTES = 262144
WITHHELD_PII = "pii_stage"
# The request-sha layout version: bump it when the canonical request changes, so old and new shas never collide.
REQUEST_SHA_VERSION = 1
# S272: the stop_reason mark of a billed but unusable reply (record_unusable_attempt); replay never serves it.
UNUSABLE_MARK = "billed_unusable"
_RECORDED_ATTR = "_aa900_cassette_recorded"
# Before CIS 385 is applied the table is missing: a write is skipped (and counted) for this long, then tried again.
_MISSING_RETRY_SECONDS = 300.0
_missing_at = 0.0

ROLES = ("writer", "judge", "validate", "embed", "decide", "data", "guard")

COLUMNS = (
    "app", "stage", "role", "request_sha256", "prompt_sha256", "prompt_version", "model_key", "answered_model",
    "deployment_wire_id", "connection_key", "provider", "provider_request_id", "route_trace", "rung", "attempt_no",
    "effort", "max_tokens", "stop_reason", "is_shadow", "is_truncation_retry", "reply_text", "reply_sha256",
    "reply_bytes", "reply_truncated", "reply_withheld", "tenant_id", "job_id", "content_piece_id", "tour_id",
    "eval_run", "eval_item",
)
_UUID_COLUMNS = frozenset({"route_trace", "tenant_id", "job_id", "content_piece_id", "tour_id"})
_INSERT_SQL = (
    "INSERT INTO shared.llm_cassette (" + ", ".join(COLUMNS) + ") VALUES ("
    + ", ".join(f"${i}::uuid" if c in _UUID_COLUMNS else f"${i}" for i, c in enumerate(COLUMNS, 1)) + ")"
)
# Text caps of the CHECKs of 385 (a longer value is cut, never refused: the row is evidence, not a contract).
_TEXT_CAPS = {"stage": 128, "model_key": 200, "prompt_version": 128, "answered_model": 200, "deployment_wire_id": 300,
              "connection_key": 128, "provider": 64, "provider_request_id": 256, "effort": 32, "stop_reason": 64,
              "eval_run": 128, "eval_item": 256}

# ── counters (a write never raises; these say what happened) ─────────────────────────────────────────────────────

_COUNTS = {"recorded": 0, "written": 0, "write_failed": 0, "missing_table": 0, "build_failed": 0, "unusable": 0}
_COUNTS_LOCK = threading.Lock()


def _count(key: str) -> None:
    with _COUNTS_LOCK:
        _COUNTS[key] = _COUNTS.get(key, 0) + 1


def stats() -> dict:
    """Counts since the process started (or ``reset_stats``): recorded (handed to a background write), written,
    write_failed, missing_table (skipped before 385), build_failed (a row could not be built), unusable (rows of a
    billed but unusable reply built, S272; also counted in recorded when handed over)."""
    with _COUNTS_LOCK:
        return dict(_COUNTS)


def reset_stats() -> None:
    global _missing_at
    with _COUNTS_LOCK:
        for k in _COUNTS:
            _COUNTS[k] = 0
    _missing_at = 0.0


def mode() -> str:
    """``record`` unless LLM_CASSETTE says off."""
    return MODE_OFF if (os.environ.get(SWITCH_ENV) or "").strip().lower() in _OFF else MODE_RECORD


def enabled() -> bool:
    return mode() == MODE_RECORD


# ── the owning object a caller may bind ──────────────────────────────────────────────────────────────────────────

_OWNER_KEYS = ("content_piece_id", "tour_id", "eval_item")
_owner: ContextVar[Optional[dict]] = ContextVar("llm_cassette_owner", default=None)


def current_owner() -> dict:
    return dict(_owner.get() or {})


@contextmanager
def bind_owner(*, content_piece_id=None, tour_id=None, eval_item=None) -> Iterator[None]:
    """Bind the object the calls below produce (same propagation as call_log.bind_job: tasks, to_thread and executors
    copy the context). A key left None keeps the outer binding's value."""
    merged = current_owner()
    for key, value in (("content_piece_id", content_piece_id), ("tour_id", tour_id), ("eval_item", eval_item)):
        if value is not None and str(value) not in ("", "None"):
            merged[key] = str(value)
    token = _owner.set(merged)
    try:
        yield
    finally:
        _owner.reset(token)


# ── the row ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def request_sha(request, *, model_key: Optional[str], effort: Optional[str] = None) -> str:
    """sha256 hex of the canonical request: messages (system, few-shots, user) and params (model key, max_tokens,
    temperature, seed, json_schema, effort). Read defensively (a test double gives None for what it lacks)."""
    few_shots = getattr(request, "few_shots", None)
    messages = [{"role": "system", "content": getattr(request, "system_prompt", None)},
                *(few_shots if isinstance(few_shots, list) else []),
                {"role": "user", "content": getattr(request, "user_prompt", None)}]
    params = {"model": model_key, "max_tokens": getattr(request, "max_tokens", None),
              "temperature": getattr(request, "temperature", None), "seed": getattr(request, "seed", None),
              "json_schema": getattr(request, "json_schema", None), "effort": effort}
    body = {"v": REQUEST_SHA_VERSION, "messages": messages, "params": params}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def capped_reply(text: Optional[str]) -> tuple[str, str, int, bool]:
    """(stored text, sha256 of the whole reply, whole size in bytes, truncated?). The cut falls on a character
    boundary at most REPLY_CAP_BYTES bytes in."""
    raw = (text if isinstance(text, str) else "").encode("utf-8", errors="replace")
    sha = hashlib.sha256(raw).hexdigest()
    if len(raw) <= REPLY_CAP_BYTES:
        return raw.decode("utf-8"), sha, len(raw), False
    return raw[:REPLY_CAP_BYTES].decode("utf-8", errors="ignore"), sha, len(raw), True


def _uuid_or_none(value) -> Optional[str]:
    if value is None:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _str_or_none(value, cap: Optional[int] = None) -> Optional[str]:
    if not isinstance(value, str) or not value:
        return None
    return value[:cap] if cap else value


def _int_or_none(value, minimum: int = 0) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and value >= minimum:
        return value
    return None


def _sha_or_none(value) -> Optional[str]:
    if isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value):
        return value
    return None


def build_row(request, resp, *, model: str, connection_key: Optional[str] = None, wire_id: Optional[str] = None,
              provider: Optional[str] = None, scope=None, truncation_retry: bool = False,
              pii_class: Optional[str] = None) -> dict:
    """The cassette row of one returned reply (a dict keyed by COLUMNS). ``scope`` is the gateway's attempt scope
    (role, position, trace_id, prompt_hash, prompt_version, extra) or None outside a gateway request."""
    from . import call_log            # lazy: call_log is the ledger module, it never imports this one

    effort = _str_or_none(getattr(resp, "effort", None))
    model_key = _str_or_none(model) or _str_or_none(getattr(resp, "model_used", None)) or "unknown"
    stored, reply_sha, reply_bytes, truncated = capped_reply(getattr(resp, "content", None))
    withheld = WITHHELD_PII if pii_class == "pii" else None
    role = getattr(scope, "role", None) or getattr(request, "role", None) or "writer"
    extra = getattr(scope, "extra", None)
    label = call_log.eval_label()
    owner = current_owner()
    row = {
        "app": call_log.APP,
        "stage": _str_or_none(getattr(request, "stage", None)) or "unstaged",
        "role": role if role in ROLES else "writer",
        "request_sha256": request_sha(request, model_key=model_key, effort=effort),
        "prompt_sha256": _sha_or_none(getattr(resp, "prompt_hash", None) or getattr(scope, "prompt_hash", None)),
        "prompt_version": _str_or_none(getattr(resp, "prompt_version", None)
                                       or getattr(scope, "prompt_version", None)),
        "model_key": model_key,
        "answered_model": _str_or_none(getattr(resp, "provider_model", None)),
        "deployment_wire_id": _str_or_none(getattr(resp, "deployment_wire_id", None)) or _str_or_none(wire_id),
        "connection_key": (_str_or_none(getattr(resp, "connection_key", None)) or _str_or_none(connection_key)
                           or _str_or_none(getattr(resp, "satellite_account", None))),
        "provider": _str_or_none(getattr(resp, "provider", None)) or _str_or_none(provider),
        "provider_request_id": _str_or_none(getattr(resp, "request_id", None)),
        "route_trace": _uuid_or_none(getattr(scope, "trace_id", None) or getattr(resp, "route_trace", None)),
        "rung": _int_or_none(getattr(scope, "position", None) if scope is not None
                             else getattr(resp, "route_position", None)),
        "attempt_no": _int_or_none(getattr(resp, "attempt_no", None), minimum=1),
        "effort": effort,
        "max_tokens": _int_or_none(getattr(request, "max_tokens", None)),
        "stop_reason": _str_or_none(getattr(resp, "stop_reason", None)),
        "is_shadow": bool(isinstance(extra, dict) and extra.get("shadow")),
        "is_truncation_retry": bool(truncation_retry),
        "reply_text": None if withheld else stored,
        "reply_sha256": reply_sha,
        "reply_bytes": reply_bytes,
        "reply_truncated": truncated and not withheld,
        "reply_withheld": withheld,
        "tenant_id": _uuid_or_none(call_log.current_tenant_id()),
        "job_id": _uuid_or_none(call_log.current_job_id()),
        "content_piece_id": _uuid_or_none(owner.get("content_piece_id")),
        "tour_id": _uuid_or_none(owner.get("tour_id")),
        "eval_run": label[1] if label else None,
        "eval_item": owner.get("eval_item"),
    }
    for key, cap in _TEXT_CAPS.items():
        if isinstance(row.get(key), str):
            row[key] = row[key][:cap]
    return row


def unusable_stop_reason(stop_reason) -> str:
    """The stop_reason of a billed but unusable reply's row: ``billed_unusable`` or
    ``billed_unusable:<provider stop reason>`` (cut to the 64-character CHECK by build_row)."""
    if isinstance(stop_reason, str) and stop_reason:
        return f"{UNUSABLE_MARK}:{stop_reason}"
    return UNUSABLE_MARK


def is_unusable(row) -> bool:
    """True for the row of a billed but unusable reply (its stop_reason carries ``UNUSABLE_MARK``)."""
    reason = (row or {}).get("stop_reason") if isinstance(row, dict) else getattr(row, "stop_reason", None)
    return isinstance(reason, str) and (reason == UNUSABLE_MARK or reason.startswith(UNUSABLE_MARK + ":"))


class _UnusableReply:
    """The reply-shaped view of a BilledUnusableReply that ``build_row`` reads (what it lacks reads as None)."""

    def __init__(self, exc, *, attempt_no: Optional[int], wire_id: Optional[str],
                 connection_key: Optional[str]) -> None:
        billed = list(getattr(exc, "billed_usage", None) or ())
        first = billed[0] if billed and isinstance(billed[0], dict) else {}
        facts = getattr(exc, "reply_facts", None)
        facts = facts if isinstance(facts, dict) else {}
        content = facts.get("content")
        self.content = content if isinstance(content, str) else ""
        self.model_used = first.get("model")
        self.provider_model = facts.get("provider_model")
        self.request_id = facts.get("request_id")
        self.effort = facts.get("effort")
        self.stop_reason = unusable_stop_reason(first.get("stop_reason"))
        self.attempt_no = attempt_no
        self.deployment_wire_id = first.get("deployment_wire_id") or wire_id
        self.connection_key = first.get("connection_key") or connection_key
        self.prompt_hash = first.get("prompt_hash")
        self.prompt_version = first.get("prompt_version")
        self.route_trace = first.get("route_trace")
        self.route_position = first.get("route_position")


def record_attempt(request, resp, *, model: str, connection_key: Optional[str] = None,
                   wire_id: Optional[str] = None, provider: Optional[str] = None, scope=None,
                   truncation_retry: bool = False) -> None:
    """Record one returned reply (``LLMClient._attempt``). Never raises: off, a row that cannot be built and a failed
    write are logged and counted; the reply is returned to its caller either way."""
    if not enabled():
        return
    try:
        from .client import stage_pii_class      # lazy: client imports this module
        row = build_row(request, resp, model=model, connection_key=connection_key, wire_id=wire_id,
                        provider=provider, scope=scope, truncation_retry=truncation_retry,
                        pii_class=stage_pii_class(request))
    except Exception as e:  # noqa: BLE001 — a recorder never breaks the call it records
        _count("build_failed")
        logger.warning("llm_cassette_build_failed", stage=getattr(request, "stage", None), error=str(e)[:200])
        return
    _hand_off(row)


def record_unusable_attempt(request, exc, *, model: str, connection_key: Optional[str] = None,
                            wire_id: Optional[str] = None, provider: Optional[str] = None, scope=None,
                            truncation_retry: bool = False, attempt_no: Optional[int] = None) -> None:
    """S272: record one attempt that ended billed but unusable (``LLMClient._attempt``'s except path, ``exc`` = the
    BilledUnusableReply in the chain). The row holds the reply ``exc.reply_facts`` carries (empty when none) and is
    marked with ``UNUSABLE_MARK`` in stop_reason. One row per exception object: an error seen again by an outer
    attempt (a wrapped chain) is not recorded twice. Never raises (same counters as ``record_attempt``)."""
    if not enabled():
        return
    try:
        if exc is not None:
            if getattr(exc, _RECORDED_ATTR, False):
                return
            setattr(exc, _RECORDED_ATTR, True)
    except Exception:  # noqa: BLE001 — an exception that refuses attributes is still recorded
        pass
    try:
        from .client import stage_pii_class      # lazy: client imports this module
        view = _UnusableReply(exc, attempt_no=attempt_no, wire_id=wire_id, connection_key=connection_key)
        row = build_row(request, view, model=model, connection_key=connection_key, wire_id=wire_id,
                        provider=provider, scope=scope, truncation_retry=truncation_retry,
                        pii_class=stage_pii_class(request))
    except Exception as e:  # noqa: BLE001 — a recorder never breaks the call it records
        _count("build_failed")
        logger.warning("llm_cassette_build_failed", stage=getattr(request, "stage", None), unusable=True,
                       error=str(e)[:200])
        return
    _count("unusable")
    _hand_off(row)


def _hand_off(row: dict) -> None:
    """Hand a built row to the background write (skipped while 385 is known missing). Never raises."""
    if _missing_at and time.monotonic() - _missing_at < _MISSING_RETRY_SECONDS:
        _count("missing_table")
        return
    from .call_log import _in_background
    try:
        _count("recorded")
        _in_background(write_row, {"row": row})
    except Exception as e:  # noqa: BLE001 — e.g. no thread could be started
        _count("write_failed")
        logger.warning("llm_cassette_write_failed", stage=row.get("stage"), error=str(e)[:200])


async def _connect():
    """The cassette's own short-lived connection (as call_log.record_call). Tests replace it."""
    return await asyncpg.connect(get_database_url(), ssl="require", timeout=10)


async def write_row(row: dict, conn=None) -> bool:
    """INSERT one row, on ``conn`` when given, else on a short-lived connection. True when written. Never raises."""
    global _missing_at
    own = conn is None
    try:
        if own:
            conn = await _connect()
        try:
            await conn.execute(_INSERT_SQL, *[row.get(c) for c in COLUMNS])
        finally:
            if own:
                await conn.close()
    except asyncpg.exceptions.UndefinedTableError:
        _missing_at = time.monotonic()
        _count("missing_table")
        logger.warning("llm_cassette_table_missing", hint="CIS migration 385 not applied: replies are not recorded")
        return False
    except Exception as e:  # noqa: BLE001 — a recorder never breaks the call it records
        _count("write_failed")
        logger.warning("llm_cassette_write_failed", stage=row.get("stage"), role=row.get("role"),
                       error=f"{type(e).__name__}: {str(e)[:200]}")
        return False
    _count("written")
    return True


# ── replay reads ─────────────────────────────────────────────────────────────────────────────────────────────────

_SELECT = "SELECT c.id, c.created_at, " + ", ".join(f"c.{col}" for col in COLUMNS) + " FROM shared.llm_cassette c"

# S272: a billed but unusable reply (stop_reason UNUSABLE_MARK[:...]) is never served as a replay answer.
_BY_REQUEST_SQL = (_SELECT + " WHERE c.request_sha256 = $1 AND ($2::text IS NULL OR c.model_key = $2)"
                   " AND c.reply_text IS NOT NULL AND NOT c.reply_truncated"
                   f" AND (c.stop_reason IS NULL OR (c.stop_reason <> '{UNUSABLE_MARK}'"
                   f" AND c.stop_reason NOT LIKE '{UNUSABLE_MARK}:%'))"
                   " ORDER BY c.created_at DESC, c.id DESC LIMIT 1")

# The ledger row's route_trace is a quality_signal text (64 characters at most, call_log.route_signal); only a uuid
# can match. Among the replies of one (trace, attempt) (a truncation retry reports the cut reply's attempt number),
# the one whose provider request id, then whose retry mark, matches the ledger row wins.
_BY_CALL_LOG_SQL = (
    "SELECT c.id, c.created_at, " + ", ".join(f"c.{col}" for col in COLUMNS)
    + ", l.id AS call_log_id, l.content_piece_id AS ledger_content_piece_id, l.tour_id AS ledger_tour_id,"
      " l.job_id AS ledger_job_id, l.status AS ledger_status"
      " FROM shared.llm_call_log l"
      " JOIN shared.llm_cassette c"
      "   ON c.route_trace = CASE WHEN (l.quality_signal->>'route_trace') ~ "
      "'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'"
      "                          THEN (l.quality_signal->>'route_trace')::uuid END"
      "  AND c.attempt_no IS NOT DISTINCT FROM l.attempt_no"
      " WHERE l.id = $1"
      " ORDER BY (l.request_id IS NOT NULL AND c.provider_request_id = l.request_id) DESC,"
      "          (c.is_truncation_retry = ((l.quality_signal->>'truncation_retry') IS NOT DISTINCT FROM 'true')) DESC,"
      "          c.created_at DESC, c.id DESC"
      " LIMIT 1"
)


def _as_dict(record) -> Optional[dict]:
    if record is None:
        return None
    out = dict(record)
    for key, value in out.items():
        if isinstance(value, uuid.UUID):
            out[key] = str(value)
    return out


async def get_by_request_sha(conn, request_sha256: str, *, model_key: Optional[str] = None) -> Optional[dict]:
    """The newest whole reply (stored, not cut, not withheld, not a billed unusable one) recorded for this request
    sha (and model key, when given), else None. ``conn`` is an asyncpg connection (or pool) that may read
    shared.llm_cassette."""
    return _as_dict(await conn.fetchrow(_BY_REQUEST_SQL, request_sha256, model_key))


async def get_by_call_log_id(conn, call_log_id: str) -> Optional[dict]:
    """The reply behind one llm_call_log row (matched on route_trace + attempt_no), with the ledger row's own links
    (``call_log_id``, ``ledger_content_piece_id``, ``ledger_tour_id``, ``ledger_job_id``, ``ledger_status``), else
    None (a row written outside a gateway request, a failed attempt, or a reply recorded before 385). A
    ``billed_unusable`` ledger row finds its unusable reply (S272; ``is_unusable(row)``)."""
    cid = _uuid_or_none(call_log_id)
    if cid is None:
        return None
    return _as_dict(await conn.fetchrow(_BY_CALL_LOG_SQL, cid))
