"""AA-1017 (registry v2, migration 320; root ADR 0009 §4) — request parameters per stage x model.

``shared.stage_model_params`` holds, per (stage, catalog model): max_tokens, min_output_tokens, temperature, effort and
reasoning_mode, each NULL = not set. The precedence is fixed:

1. the call site's explicit value (a field the caller set on ``LLMRequest``: ``max_tokens`` / ``temperature`` in
   ``model_fields_set``) — a hard limit of the code that knows its output;
2. the stage x model row;
3. the protocol default (``LLMRequest``'s own defaults; the catalog's ``max_output_tokens`` cap still applies after).

``min_output_tokens`` raises a max_tokens that came from step 2 or 3 to at least that value (never a call site's). The
table is EMPTY at migration 320, and ``apply`` then returns the very request object it was given, so every provider
request stays byte-identical (``tests/unit/test_aa1002_no_behaviour_change.py``). A request without a stage reads
nothing.

Which adapter uses what: max_tokens and temperature go through the request to every catalog path (Converse, the
Anthropic Messages API, OpenAI / Azure). ``effort`` is sent only on the Anthropic Messages API (as the deployment's
``effort_by_stage``; client._call_anthropic_messages). On Converse and OpenAI the gateway sends no effort or reasoning
field today (AA-803: Converse runs the model's default); a row that sets ``effort`` / ``reasoning_mode`` for such a
model is logged once (``stage_params_not_applied``) and not sent, until a probe has shown the provider's field. The
legacy keys (haiku / sonnet, ``_legacy_chain``) are not catalog paths and read nothing here.

Like catalog.py: the whole table is loaded in one query and cached in process (300 s; a failed load 60 s); the sync read
never raises (a DB failure or a table before migration 320 means no parameters, i.e. today's behaviour). Kill switch:
``LLM_STAGE_PARAMS=off``.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

import asyncpg
import structlog

from shared.secrets import get_database_url

logger = structlog.get_logger()

_CACHE_TTL_SECONDS = 300.0
_FAILURE_TTL_SECONDS = 60.0
_CONNECT_TIMEOUT_SECONDS = 3.0
SWITCH_ENV = "LLM_STAGE_PARAMS"

_SELECT_SQL = ("SELECT stage, model_key, max_tokens, min_output_tokens, temperature, effort, reasoning_mode "
               "FROM shared.stage_model_params")


@dataclass(frozen=True)
class StageModelParams:
    stage: str
    model_key: str
    max_tokens: Optional[int] = None
    min_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    effort: Optional[str] = None
    reasoning_mode: Optional[str] = None


def enabled() -> bool:
    return os.environ.get(SWITCH_ENV, "on").strip().lower() not in ("off", "0", "false", "no")


def _row_to_params(row) -> StageModelParams:
    t = row["temperature"]
    return StageModelParams(stage=row["stage"], model_key=row["model_key"], max_tokens=row["max_tokens"],
                            min_output_tokens=row["min_output_tokens"],
                            temperature=float(t) if t is not None else None, effort=row["effort"],
                            reasoning_mode=row["reasoning_mode"])


async def fetch_params(conn) -> dict[tuple[str, str], StageModelParams]:
    return {(r["stage"], r["model_key"]): _row_to_params(r) for r in await conn.fetch(_SELECT_SQL)}


async def _fetch_all() -> dict[tuple[str, str], StageModelParams]:
    conn = await asyncpg.connect(get_database_url(), ssl="require", timeout=_CONNECT_TIMEOUT_SECONDS)
    try:
        return await fetch_params(conn)
    finally:
        await conn.close()


def _run_sync(coro_fn):
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_fn())
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro_fn())).result(timeout=10)


_params: dict[tuple[str, str], StageModelParams] = {}
_loaded_at = 0.0
_failed_at = 0.0
_load_lock = threading.Lock()
_NOT_APPLIED_WARNED: set[tuple[str, str, str]] = set()


def _fresh(now: float) -> bool:
    return bool((_loaded_at and now - _loaded_at < _CACHE_TTL_SECONDS)
                or (_failed_at and now - _failed_at < _FAILURE_TTL_SECONDS))


def _ensure_loaded() -> None:
    global _params, _loaded_at, _failed_at
    if _fresh(time.monotonic()):
        return
    with _load_lock:
        now = time.monotonic()
        if _fresh(now):
            return
        try:
            _params = _run_sync(_fetch_all)
            _loaded_at, _failed_at = now, 0.0
        except Exception as e:  # noqa: BLE001 — never raises: no table before 320, DB down = no parameters
            _failed_at = now
            logger.warning("stage_model_params_read_failed", error=str(e)[:300],
                           hint="no stage x model parameters (migration 320 not applied, or DB down)")


def params_for(stage: Optional[str], model_key: str) -> Optional[StageModelParams]:
    """The row of (stage, model_key), or None (no stage, no row, switch off, table unreadable). Never raises."""
    if not stage or not model_key or not enabled():
        return None
    _ensure_loaded()
    return _params.get((stage, model_key))


def invalidate() -> None:
    global _loaded_at, _failed_at
    _loaded_at = 0.0
    _failed_at = 0.0


def resolve(request, params: Optional[StageModelParams], *, supports_temperature: bool = True) -> dict:
    """The request fields to change (pure): {"max_tokens": n, "temperature": t} for the values ``params`` sets and
    the call site did not, max_tokens raised to ``min_output_tokens``. Empty when nothing changes."""
    if params is None:
        return {}
    explicit = getattr(request, "model_fields_set", set()) or set()
    out: dict = {}
    max_tokens = request.max_tokens
    if "max_tokens" not in explicit:
        if params.max_tokens is not None:
            max_tokens = params.max_tokens
        if params.min_output_tokens is not None and max_tokens < params.min_output_tokens:
            max_tokens = params.min_output_tokens
        if max_tokens != request.max_tokens:
            out["max_tokens"] = max_tokens
    if params.temperature is not None and supports_temperature and "temperature" not in explicit:
        out["temperature"] = params.temperature
    return out


def apply(request, model) -> object:
    """``request`` with the stage x model parameters of ``model`` (a CatalogModel) applied, or the SAME object when
    there is nothing to apply (the empty table: byte-identical requests). A temperature the model does not take
    (``supports_temperature`` false) is never set. Never raises."""
    try:
        params = params_for(getattr(request, "stage", None), getattr(model, "model_key", ""))
        changes = resolve(request, params, supports_temperature=bool(getattr(model, "supports_temperature", True)))
    except Exception as e:  # noqa: BLE001 — a parameter read never breaks the call
        logger.warning("stage_params_apply_failed", stage=getattr(request, "stage", None), error=str(e)[:200])
        return request
    if not changes:
        return request
    # model_copy keeps model_fields_set, and the changed fields become "set", so the Converse path sends a temperature
    # that came from the table (it sends one only when the field is set).
    out = request.model_copy(update=changes)
    out.__pydantic_fields_set__ = set(request.model_fields_set) | set(changes)
    logger.info("stage_params_applied", stage=request.stage, model=getattr(model, "model_key", None),
                fields=sorted(changes))
    return out


def effort_for(stage: Optional[str], model_key: str) -> Optional[str]:
    """The effort the stage x model row sets (None = not set)."""
    p = params_for(stage, model_key)
    return p.effort if p is not None else None


def warn_not_applied(stage: Optional[str], model_key: str, api_style: str) -> None:
    """Log once per (stage, model, api style) that a row's effort / reasoning_mode is not sent on this path."""
    p = params_for(stage, model_key)
    if p is None or (p.effort is None and p.reasoning_mode is None):
        return
    key = (stage or "", model_key, api_style)
    if key in _NOT_APPLIED_WARNED:
        return
    _NOT_APPLIED_WARNED.add(key)
    logger.warning("stage_params_not_applied", stage=stage, model=model_key, api_style=api_style,
                   effort=p.effort, reasoning_mode=p.reasoning_mode,
                   reason="this path sends no effort / reasoning field yet (Anthropic Messages API only)")
