"""AA-505 — persist shared.llm_call_log for every real LLM call.

Fire-and-forget by design: a logging failure must never break the writer/judge call it's
recording. `record_call()` (async) and `record_call_sync()` (for the many plain-`def` call sites,
same "wrap at the sync/async boundary" reasoning as role_config.py) both swallow every exception
and log a warning instead of raising.

`quality_signal` is a required, positional-ish kwarg on purpose (no default) — the 02/09/2026
S171 decision was that every stage must pass a REAL, computed value, never leave this to a
convenient `{}` default. See docs/implementation-notes/AA-518.md for what each of the 16 stages
actually passes and why.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import threading
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

import asyncpg
import structlog

from shared.secrets import get_database_url

from .models import ANTHROPIC_API_LABEL_PREFIX, AZURE_OPENAI_LABEL_PREFIX

logger = structlog.get_logger()

_INSERT_SQL = """
    INSERT INTO shared.llm_call_log
        (tenant_id, stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal,
         content_piece_id, angle_gate_request_id, stop_reason, account, fallback_used, provider,
         job_id)
    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::uuid, $10::uuid, $11, $12, $13, $14,
            $15::uuid)
"""

# AA-1002 (S6, migration 312): the same row plus the cache split and how cost_usd was priced. Used only for a row that
# has something to say there (cache tokens, a caller-stated pricing_mode, or a model no price source knows: flagged
# 'estimate'); every other row keeps _INSERT_SQL, byte for byte. Before 312 is applied the columns are missing: the
# write falls back to _INSERT_SQL (the row is kept, its S6 fields dropped) and the S6 statement is not tried again for
# _S6_RETRY_SECONDS.
_INSERT_S6_SQL = """
    INSERT INTO shared.llm_call_log
        (tenant_id, stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal,
         content_piece_id, angle_gate_request_id, stop_reason, account, fallback_used, provider,
         job_id, cache_read_tokens, cache_write_tokens, pricing_mode)
    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::uuid, $10::uuid, $11, $12, $13, $14,
            $15::uuid, $16, $17, $18)
"""
_S6_RETRY_SECONDS = 300.0
_s6_missing_at = 0.0
# The statement as shipped. The AA-729 eval harness replaces _INSERT_SQL at run time (stage / tenant relabel,
# scripts/aa729/evalpatch.py): a replaced statement is always used as is, never the S6 one, so an eval row can never
# escape the relabel through the S6 path.
_SHIPPED_INSERT_SQL = _INSERT_SQL


def _s6_extras(model: str, cache_read_tokens, cache_write_tokens, pricing_mode) -> Optional[tuple]:
    """(cache_read_tokens, cache_write_tokens, pricing_mode) for _INSERT_S6_SQL, or None when the row has nothing
    for those columns. A model the pricing cannot price is flagged even when the caller said nothing. Never raises."""
    mode = pricing_mode if isinstance(pricing_mode, str) else None
    if mode is None:
        try:
            from . import pricing
            if pricing.pricing_mode_for(model) == "estimate":
                mode = "estimate"
        except Exception:  # noqa: BLE001 — a log write never fails on pricing
            mode = None
    cr, cw = (v if isinstance(v, int) and not isinstance(v, bool) else None
              for v in (cache_read_tokens, cache_write_tokens))
    if not (cr or cw or mode):
        return None
    return (cr, cw, mode)


def _s6_ready() -> bool:
    import time as _time
    return not (_s6_missing_at and _time.monotonic() - _s6_missing_at < _S6_RETRY_SECONDS)


async def _execute_rows(conn, insert_sql: str, base_rows: list[tuple], extras: list[Optional[tuple]], *,
                        many: bool) -> None:
    """INSERT the rows: _INSERT_S6_SQL when any row has S6 fields, the columns exist and ``insert_sql`` (the caller's
    _INSERT_SQL, read at call time) is the shipped statement; else ``insert_sql``."""
    global _s6_missing_at
    if any(e is not None for e in extras) and insert_sql == _SHIPPED_INSERT_SQL and _s6_ready():
        args = [(*b, *(e or (None, None, None))) for b, e in zip(base_rows, extras)]
        try:
            if many:
                await conn.executemany(_INSERT_S6_SQL, args)
            else:
                await conn.execute(_INSERT_S6_SQL, *args[0])
            return
        except asyncpg.exceptions.UndefinedColumnError:
            import time as _time
            _s6_missing_at = _time.monotonic()
            logger.warning("llm_call_log_s6_columns_missing", hint="migration 312 not applied: cache / pricing_mode "
                                                                   "columns dropped from this row")
    if many:
        await conn.executemany(insert_sql, base_rows)
    else:
        await conn.execute(insert_sql, *base_rows[0])

# AA-652 follow-up — the durable job (shared.job) whose handler is running, bound by the job
# worker around each handler. Same propagation as stream_sink.py: asyncio tasks,
# `asyncio.to_thread()` and LangGraph's `run_in_executor` all copy the context, so every
# record_call*() below the handler writes the job id without any call site knowing about jobs.
_current_job_id: ContextVar[Optional[str]] = ContextVar("llm_call_log_job_id", default=None)


def current_job_id() -> Optional[str]:
    return _current_job_id.get()


@contextmanager
def bind_job(job_id: Optional[str]) -> Iterator[None]:
    token = _current_job_id.set(job_id)
    try:
        yield
    finally:
        _current_job_id.reset(token)


# AA-741 G1 — the tenant whose content the running job is producing, bound by the job worker
# from the job payload (same propagation as `_current_job_id`). A record_call*() / decide() whose
# caller passes no tenant_id falls back to it, so deep stages that run for both platform and
# tenant content (S1 judge, brand audit, flag_fix, grounding, source facts, T10 judge, F10 embed)
# attribute tenant cost without threading tenant_id through every signature. Never bound for
# platform (aa_internal) jobs — platform rows stay tenant_id NULL, as before.
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"  # aa_internal
_current_tenant_id: ContextVar[Optional[str]] = ContextVar("llm_call_log_tenant_id", default=None)


def current_tenant_id() -> Optional[str]:
    return _current_tenant_id.get()


@contextmanager
def bind_tenant(tenant_id: Optional[str]) -> Iterator[None]:
    token = _current_tenant_id.set(_clean_tenant(tenant_id))
    try:
        yield
    finally:
        _current_tenant_id.reset(token)


# AA-919 WM-19 / WM-21 — an eval run (the writing-memory R2 runner, the targeted shadow) drives the REAL writer paths,
# so their rows would land under the production stages (s1_generate, t2_generate, t9_write, the judges, Jev) and on the
# tenant's usage. While an eval label is bound, every row of this module is written under the eval stage instead
# (`writing_memory_eval` / `writing_memory_shadow`), the real stage goes to quality_signal.real_stage, the run id to
# quality_signal.eval_run and the tenant / piece / request ids are written NULL (the tenant to
# quality_signal.eval_tenant), like the AA-729 harness did with in-process patches. Routing is untouched: the request's
# own stage still picks the model. The run's cost is then one indexed query (stage + eval_run), which is what the
# runner's hard cap reads. Same propagation as `_current_job_id`.
_eval_label: ContextVar[Optional[tuple[str, str]]] = ContextVar("llm_call_log_eval_label", default=None)


def eval_label() -> Optional[tuple[str, str]]:
    """(eval stage, run id) while an eval run is bound, else None."""
    return _eval_label.get()


@contextmanager
def bind_eval_stage(stage: str, run_id: str) -> Iterator[None]:
    if not stage or not run_id:
        raise ValueError("bind_eval_stage needs a stage and a run id")
    token = _eval_label.set((str(stage), str(run_id)))
    try:
        yield
    finally:
        _eval_label.reset(token)


def _relabel(stage: str, tenant_id, quality_signal: Optional[dict], content_piece_id, angle_gate_request_id):
    """(stage, row tenant, quality_signal, piece, request) as the INSERT sends them: unchanged without an eval
    label; relabelled to the eval stage (tenant / piece / request NULL) with one."""
    label = _eval_label.get()
    if label is None:
        return stage, _row_tenant(tenant_id, stage), quality_signal, content_piece_id, angle_gate_request_id
    eval_stage, run_id = label
    qs = dict(quality_signal or {})
    qs.setdefault("real_stage", stage)
    qs["eval_run"] = run_id
    tid = resolve_tenant(tenant_id)
    if tid is not None:
        qs.setdefault("eval_tenant", str(tid)[:64])
    return eval_stage, None, qs, None, None


def _clean_tenant(tenant_id) -> Optional[str]:
    """`None`, `""` and `"None"` (a `str(None)` at a call site) all mean no tenant."""
    if tenant_id is None or str(tenant_id) in ("", "None"):
        return None
    return str(tenant_id)


def resolve_tenant(tenant_id) -> Optional[str]:
    """The caller's tenant when it passed one, else the bound job's tenant."""
    return _clean_tenant(tenant_id) or current_tenant_id()


def _row_tenant(tenant_id, stage: Optional[str] = None) -> Optional[str]:
    """The `tenant_id` the INSERT sends as `$1::uuid`: `resolve_tenant`, or NULL when that is not a UUID.

    AA-993 follow-up: asyncpg refuses a non-UUID string for a `::uuid` argument before the row is sent, and the
    write swallows the error, so an already-billed call lost its row. Live, a job enqueued with the platform slug
    `"aa_internal"` lost every S1 row that way. A tenant is always a UUID, so a value that is not one is not a tenant:
    the row is written with tenant NULL (the platform convention) and the bad value is logged."""
    tid = resolve_tenant(tenant_id)
    if tid is None:
        return None
    try:
        return str(uuid.UUID(str(tid)))
    except ValueError:
        logger.warning("llm_call_log_tenant_not_uuid", stage=stage, tenant_id=str(tid)[:64])
        return None


def _normalize_model_provider(model: str, provider: Optional[str]) -> tuple[str, Optional[str]]:
    """AA-617 — central place so no call site has to know the model-string conventions.

    LLMResponse.model_used comes in three shapes today:
      - "satellite-haiku-4-5" / "satellite-sonnet-4-6"  (Mechanism A, _call_bedrock_satellite)
      - "haiku-4-5" / "sonnet-4-6"                       (Mechanism B, invoke_claude() direct)
      - "gpt-4.1"                                        (OpenAI)
    (_call_bedrock acc2-native returns the raw BEDROCK_SONNET/BEDROCK_HAIKU inference-profile id.)

    We strip the "satellite-" prefix so GROUP BY model is clean (the account distinction now lives
    in its own column), and derive `provider` from the shape when the caller didn't pass one:
      - "satellite-" prefix  -> bedrock-satellite
      - "anthropic-api-" prefix -> anthropic (AA-1002 S2, the Anthropic Messages API path)
      - "azure-openai-" prefix -> azure_openai (AA-1002 S9, an Azure OpenAI connection)
      - starts with "gpt"    -> openai
      - anything else        -> bedrock-native (acc2 raw inference-profile id, or already-clean id)
    A caller that DOES pass `provider` wins (e.g. it knows better than a string heuristic)."""
    clean = model
    derived = provider
    if model and model.startswith("satellite-"):
        clean = model[len("satellite-"):]
        derived = derived or "bedrock-satellite"
    elif model and model.startswith(ANTHROPIC_API_LABEL_PREFIX):
        # AA-1002 (S2): the Anthropic Messages API path ("anthropic-api-<model key>")
        clean = model[len(ANTHROPIC_API_LABEL_PREFIX):]
        derived = derived or "anthropic"
    elif model and model.startswith(AZURE_OPENAI_LABEL_PREFIX):
        # AA-1002 (S9): an Azure OpenAI connection ("azure-openai-<model key>")
        clean = model[len(AZURE_OPENAI_LABEL_PREFIX):]
        derived = derived or "azure_openai"
    elif model and model.lower().startswith("gpt"):
        derived = derived or "openai"
    elif model:
        derived = derived or "bedrock-native"
    return clean, derived


def _row_account(account: Optional[str], connection_key: Optional[str]) -> Optional[str]:
    """The `account` a row stores: the caller's (an AWS account key), else the vendor connection key (AA-1002 S6)."""
    if account is not None:
        return account
    return connection_key if isinstance(connection_key, str) and connection_key else None


def _int_or_none(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def billed_fields(resp) -> dict:
    """AA-1002 (S6): the billing facts of a gateway reply that a call site passes on to ``record_call*`` next to
    ``model=resp.model_used``: the cache split of the prompt (``cache_read_tokens`` / ``cache_write_tokens``), how
    ``cost_usd`` was priced (``pricing_mode``) and the vendor connection that answered (``connection_key``, stored
    as the row's account when the caller passes none). Read defensively: a test double (MagicMock) or an older
    response object gives None for anything that is not of the right type. Usage:
    ``record_call_sync(stage=..., model=resp.model_used, ..., **billed_fields(resp))``. The guard
    ``tests/unit/test_aa1002_s6_billed_fields.py`` fails on a site that logs ``model=<x>.model_used`` without it."""
    mode = getattr(resp, "pricing_mode", None)
    conn = getattr(resp, "connection_key", None)
    return {
        "cache_read_tokens": _int_or_none(getattr(resp, "cache_read_tokens", None)),
        "cache_write_tokens": _int_or_none(getattr(resp, "cache_write_tokens", None)),
        "pricing_mode": mode if isinstance(mode, str) else None,
        "connection_key": conn if isinstance(conn, str) and conn else None,
    }


async def record_call(
    *, stage: str, role: str, model: str, tokens_in: Optional[int], tokens_out: Optional[int],
    cost_usd: Optional[float], quality_signal: dict, tenant_id: Optional[str] = None,
    content_piece_id: Optional[str] = None, angle_gate_request_id: Optional[str] = None,
    stop_reason: Optional[str] = None,
    account: Optional[str] = None, fallback_used: Optional[bool] = None,
    provider: Optional[str] = None,
    cache_read_tokens: Optional[int] = None, cache_write_tokens: Optional[int] = None,
    pricing_mode: Optional[str] = None, connection_key: Optional[str] = None,
) -> None:
    """Opens its own short-lived connection — the call sites this is used from (S1 graph nodes,
    N7 E2-E5, the T10 judge, T5 atomize) are scattered across sync and async code with no
    single shared asyncpg.Pool threaded down to them (T9/T5's own async orchestrators DO have a
    pool in scope — those call sites pass it to `record_call_with_pool()` instead, see below, to
    avoid paying for a whole new connection when an open one is already sitting right there).

    `stop_reason` (AA-493): "end_turn"/"max_tokens"/"stop_sequence" (Anthropic) or
    "stop"/"length"/... (OpenAI's `finish_reason`, same slot) — None for any caller that hasn't
    been threaded through yet, so this stays backward compatible field-by-field.

    `account`/`fallback_used`/`provider` (AA-617): per-account LLM cost tracking. `account` is
    "acc2"/"acc3" (or None for OpenAI; rows before AA-998 may hold the retired fallback account);
    `fallback_used` is LLMResponse.fallback_used;
    `provider` is derived from the model string when not passed. `model` is stored with the
    "satellite-" prefix stripped (see _normalize_model_provider). All optional — a caller that
    hasn't been threaded through yet writes NULL, staying backward compatible field-by-field.

    `cache_read_tokens` / `cache_write_tokens` / `pricing_mode` (AA-1002 S6, migration 312): the cache split of the
    prompt and how `cost_usd` was priced (LLMResponse.pricing_mode); a model no price source knows is written with
    pricing_mode 'estimate' even when the caller passes nothing (``_s6_extras``).

    `connection_key` (AA-1002 S6): the vendor-API connection that answered (LLMResponse.connection_key:
    'openai-direct', 'anthropic-direct'), written to `account` when the caller passes no account, so a vendor row
    names its way in like a Bedrock row names its AWS account. Callers pass all four with ``**billed_fields(resp)``."""
    account = _row_account(account, connection_key)
    clean_model, provider = _normalize_model_provider(model, provider)
    row_stage, row_tenant, row_signal, row_piece, row_request = _relabel(
        stage, tenant_id, quality_signal, content_piece_id, angle_gate_request_id)
    extras = _s6_extras(model, cache_read_tokens, cache_write_tokens, pricing_mode)
    try:
        conn = await asyncpg.connect(get_database_url(), ssl="require")
        try:
            await _execute_rows(conn, _INSERT_SQL, [(
                row_tenant, row_stage, role, clean_model, tokens_in,
                tokens_out, cost_usd,
                json.dumps(row_signal), row_piece, row_request, stop_reason,
                account, fallback_used, provider, current_job_id(),
            )], [extras], many=False)
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("llm_call_log_write_failed", stage=stage, role=role, error=str(e))


async def record_call_with_pool(
    pool, *, stage: str, role: str, model: str, tokens_in: Optional[int], tokens_out: Optional[int],
    cost_usd: Optional[float], quality_signal: dict, tenant_id: Optional[str] = None,
    content_piece_id: Optional[str] = None, angle_gate_request_id: Optional[str] = None,
    stop_reason: Optional[str] = None,
    account: Optional[str] = None, fallback_used: Optional[bool] = None,
    provider: Optional[str] = None,
    cache_read_tokens: Optional[int] = None, cache_write_tokens: Optional[int] = None,
    pricing_mode: Optional[str] = None, connection_key: Optional[str] = None,
) -> None:
    """Same as record_call() but reuses an already-open asyncpg.Pool (T5/T9's async orchestrators
    both have one in scope) instead of opening a fresh connection per call. See record_call() for
    the AA-617 account/fallback_used/provider params.

    AA-756: a write that fails on the shared pool is retried once on a fresh connection
    (record_call). The call was already billed — 347k Jev calls (30/09–09/10) went unlogged when
    a single shared connection rejected the write with "another operation is in progress"."""
    account = _row_account(account, connection_key)
    clean_model, provider = _normalize_model_provider(model, provider)
    row_stage, row_tenant, row_signal, row_piece, row_request = _relabel(
        stage, tenant_id, quality_signal, content_piece_id, angle_gate_request_id)
    extras = _s6_extras(model, cache_read_tokens, cache_write_tokens, pricing_mode)
    try:
        async with pool.acquire() as conn:
            await _execute_rows(conn, _INSERT_SQL, [(
                row_tenant, row_stage, role, clean_model, tokens_in,
                tokens_out, cost_usd,
                json.dumps(row_signal), row_piece, row_request, stop_reason,
                account, fallback_used, provider, current_job_id(),
            )], [extras], many=False)
    except Exception as e:
        logger.warning("llm_call_log_pool_write_failed", stage=stage, role=role, error=str(e))
        await record_call(
            stage=stage, role=role, model=model, tokens_in=tokens_in, tokens_out=tokens_out,
            cost_usd=cost_usd, quality_signal=quality_signal, tenant_id=tenant_id,
            content_piece_id=content_piece_id, angle_gate_request_id=angle_gate_request_id,
            stop_reason=stop_reason, account=account, fallback_used=fallback_used, provider=provider,
            cache_read_tokens=cache_read_tokens, cache_write_tokens=cache_write_tokens, pricing_mode=pricing_mode,
        )


def record_call_sync(**kwargs) -> None:
    """Sync wrapper — same "already running inside asyncio.to_thread(), safe to asyncio.run()
    here" reasoning as role_config.py::get_stage_config_sync(), and the same one real exception
    (`s1_from_atom.py::_call_claude_satellite()`, called directly from async code, no
    `to_thread()`).

    AA-927: when a running loop is detected, the write runs on a short-lived NON-daemon thread with
    its own `asyncio.run()` (`_in_background`). It used to be `asyncio.ensure_future(...)` on the
    caller's loop with no reference kept: an unreferenced task can be garbage-collected, and the
    caller's `asyncio.run()` cancels every pending task when its loop closes, so the row of an
    already-billed call was lost. The common path (no running loop) is unchanged."""
    if _loop_running():
        _in_background(record_call, kwargs)
        return
    try:
        asyncio.run(record_call(**kwargs))
    except Exception as e:
        logger.warning("llm_call_log_write_failed_sync", stage=kwargs.get("stage"), error=str(e))


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


# AA-927: background writes started under a running loop. Strong references until each ends.
_PENDING_THREADS: set[threading.Thread] = set()
_PENDING_LOCK = threading.Lock()


def _in_background(coro_fn, kwargs: dict) -> None:
    """Run `coro_fn(**kwargs)` on its own non-daemon thread, inside a COPY of the caller's contextvars
    (the bound job id and tenant are part of the row; a plain thread does not inherit them)."""
    ctx = contextvars.copy_context()

    def run() -> None:
        try:
            asyncio.run(coro_fn(**kwargs))
        except Exception as e:  # noqa: BLE001 — fire-and-forget: never raise out of a log write
            logger.warning("llm_call_log_write_failed_sync",
                           stage=kwargs.get("stage") or ((kwargs.get("rows") or [{}])[0] or {}).get("stage"),
                           error=str(e))
        finally:
            with _PENDING_LOCK:
                _PENDING_THREADS.discard(threading.current_thread())

    t = threading.Thread(target=ctx.run, args=(run,), name="llm-call-log-write", daemon=False)
    with _PENDING_LOCK:
        _PENDING_THREADS.add(t)
    t.start()


def wait_for_pending_writes(timeout: float = 10.0) -> None:
    """Join the background writes started under a running loop (tests, scripts)."""
    with _PENDING_LOCK:
        threads = list(_PENDING_THREADS)
    for t in threads:
        t.join(timeout)


# ── AA-927: several rows at once, and billed calls whose reply was unusable ─────────────────────────

_ROW_FIELDS = ("stage", "role", "model", "tokens_in", "tokens_out", "cost_usd", "quality_signal",
               "tenant_id", "content_piece_id", "angle_gate_request_id", "stop_reason", "account",
               "fallback_used", "provider", "cache_read_tokens", "cache_write_tokens", "pricing_mode",
               "connection_key")


def _insert_args(row: dict) -> tuple:
    clean_model, provider = _normalize_model_provider(row.get("model") or "unknown", row.get("provider"))
    stage, tenant, signal, piece, request = _relabel(row["stage"], row.get("tenant_id"),
                                                     row.get("quality_signal") or {}, row.get("content_piece_id"),
                                                     row.get("angle_gate_request_id"))
    return (tenant, stage, row["role"], clean_model,
            row.get("tokens_in"), row.get("tokens_out"), row.get("cost_usd"),
            json.dumps(signal or {}), piece, request, row.get("stop_reason"),
            _row_account(row.get("account"), row.get("connection_key")),
            row.get("fallback_used"), provider, current_job_id())


async def record_calls(rows: list[dict]) -> None:
    """AA-927: N llm_call_log rows (each a dict of `record_call`'s keyword arguments) on ONE connection,
    in one transaction — the rows of one judge verdict (AA-714 repeat, AA-801 median3) land together or
    not at all. Uses the module `_INSERT_SQL`, looked up at call time like `record_call` (the AA-729
    harness relabels it). Swallows and logs every error, like `record_call`."""
    rows = [r for r in rows or [] if r]
    if not rows:
        return
    for r in rows:
        unknown = set(r) - set(_ROW_FIELDS)
        if unknown:
            raise TypeError(f"record_calls: unknown field(s) {sorted(unknown)}")
    try:
        conn = await asyncpg.connect(get_database_url(), ssl="require", timeout=15)
        try:
            async with conn.transaction():
                await _execute_rows(conn, _INSERT_SQL, [_insert_args(r) for r in rows],
                                    [_s6_extras(r.get("model") or "unknown", r.get("cache_read_tokens"),
                                                r.get("cache_write_tokens"), r.get("pricing_mode")) for r in rows],
                                    many=True)
        finally:
            await conn.close()
    except Exception as e:
        logger.warning("llm_call_log_write_failed", stage=rows[0].get("stage"), role=rows[0].get("role"),
                       rows=len(rows), error=str(e))


def record_calls_sync(rows: list[dict]) -> None:
    """Sync twin of `record_calls` (same running-loop handling as `record_call_sync`). Never raises."""
    if _loop_running():
        _in_background(record_calls, {"rows": rows})
        return
    try:
        asyncio.run(record_calls(rows))
    except Exception as e:
        logger.warning("llm_call_log_write_failed_sync", stage=(rows or [{}])[0].get("stage"), error=str(e))


# The quality_signal keys admin_llm_ops._TREE_SQL reads for the ok-rate (`ok_eligible` = the row HAS
# one of them, whatever its value). A billed-failure row or an extra judge run never carries any of
# them, so it adds cost and a call to the stage without moving the stage's ok-rate.
OK_RATE_KEYS = ("passed", "status", "json_parsed", "output_parsed", "landed_in_clamp",
                "meta_landed_in_band", "required_markers_present", "produced_statement")


def billed_record(resp, **overrides) -> dict:
    """The billing fields of one provider response (an LLMResponse or anything shaped like it) as the
    dict `record_billed_failure*` takes. `overrides` replace fields."""
    rec = {
        "model": getattr(resp, "model_used", None) or "unknown",
        "tokens_in": getattr(resp, "input_tokens", None),
        "tokens_out": getattr(resp, "output_tokens", None),
        "cost_usd": float(getattr(resp, "cost_usd", 0.0) or 0.0),
        "stop_reason": getattr(resp, "stop_reason", None),
        # AA-1002 (S6): a vendor-API reply has no AWS account; its connection key is the row's account.
        "account": _row_account(getattr(resp, "satellite_account", None), getattr(resp, "connection_key", None)),
        "fallback_used": getattr(resp, "fallback_used", None),
        "provider": getattr(resp, "provider", None) if isinstance(getattr(resp, "provider", None), str) else None,
        # AA-1002 (S6): the cache split and how cost_usd was priced (written by migration 312's columns)
        "cache_read_tokens": getattr(resp, "cache_read_tokens", None),
        "cache_write_tokens": getattr(resp, "cache_write_tokens", None),
        "pricing_mode": getattr(resp, "pricing_mode", None),
    }
    rec.update(overrides)
    return rec


def billed_unusable_rows(*, stage: str, role: str, billed: Optional[list], error: str,
                         quality_signal_extra: Optional[dict] = None, tenant_id: Optional[str] = None,
                         content_piece_id: Optional[str] = None,
                         angle_gate_request_id: Optional[str] = None) -> list[dict]:
    """The `record_calls` rows for billed calls whose reply ended unusable: one per `billed` record,
    quality_signal = {**extra minus OK_RATE_KEYS, "billed_unusable": true, "error": ...}."""
    signal = {k: v for k, v in (quality_signal_extra or {}).items() if k not in OK_RATE_KEYS}
    signal.update({"billed_unusable": True, "error": str(error or "")[:300]})
    return [dict(stage=stage, role=role, model=b.get("model") or "unknown", tokens_in=b.get("tokens_in"),
                 tokens_out=b.get("tokens_out"), cost_usd=b.get("cost_usd"), quality_signal=dict(signal),
                 tenant_id=tenant_id, content_piece_id=content_piece_id,
                 angle_gate_request_id=angle_gate_request_id, stop_reason=b.get("stop_reason"),
                 account=_row_account(b.get("account"), b.get("connection_key")),
                 fallback_used=b.get("fallback_used"), provider=b.get("provider"),
                 cache_read_tokens=b.get("cache_read_tokens"), cache_write_tokens=b.get("cache_write_tokens"),
                 pricing_mode=b.get("pricing_mode"))
            for b in billed or [] if b]


async def record_billed_failure(*, stage: str, role: str, billed: Optional[list], error: str,
                                quality_signal_extra: Optional[dict] = None, tenant_id: Optional[str] = None,
                                content_piece_id: Optional[str] = None,
                                angle_gate_request_id: Optional[str] = None) -> None:
    """AA-927: one llm_call_log row per billed provider response that ended unusable (malformed reply,
    a raise after the call). `billed` = `billed_record()` dicts; empty/None writes nothing (the call
    failed before anything was billed). No ok-rate key on these rows (OK_RATE_KEYS)."""
    await record_calls(billed_unusable_rows(
        stage=stage, role=role, billed=billed, error=error, quality_signal_extra=quality_signal_extra,
        tenant_id=tenant_id, content_piece_id=content_piece_id, angle_gate_request_id=angle_gate_request_id))


def record_billed_failure_sync(*, stage: str, role: str, billed: Optional[list], error: str,
                               quality_signal_extra: Optional[dict] = None, tenant_id: Optional[str] = None,
                               content_piece_id: Optional[str] = None,
                               angle_gate_request_id: Optional[str] = None) -> None:
    """Sync twin of `record_billed_failure` (never raises)."""
    rows = billed_unusable_rows(
        stage=stage, role=role, billed=billed, error=error, quality_signal_extra=quality_signal_extra,
        tenant_id=tenant_id, content_piece_id=content_piece_id, angle_gate_request_id=angle_gate_request_id)
    if rows:
        record_calls_sync(rows)


def record_unusable_sync(resp, *, stage: str, role: str, error, quality_signal_extra: Optional[dict] = None,
                         **ids) -> None:
    """AA-927 canonical caller fallback: `resp` came back from `LLMClient.generate()` (so it was billed)
    but the caller raised before writing its row — write a billed_unusable row for it. `resp` None = the
    call itself failed, nothing to write. `ids` = tenant_id / content_piece_id / angle_gate_request_id."""
    if resp is None:
        return
    record_billed_failure_sync(stage=stage, role=role, billed=[billed_record(resp)], error=str(error),
                               quality_signal_extra=quality_signal_extra, **ids)


async def record_unusable(resp, *, stage: str, role: str, error, quality_signal_extra: Optional[dict] = None,
                          **ids) -> None:
    """Async twin of `record_unusable_sync`."""
    if resp is None:
        return
    await record_billed_failure(stage=stage, role=role, billed=[billed_record(resp)], error=str(error),
                                quality_signal_extra=quality_signal_extra, **ids)
