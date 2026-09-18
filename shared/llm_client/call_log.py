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
import json
from typing import Optional

import asyncpg
import structlog

from shared.secrets import get_database_url

logger = structlog.get_logger()

_INSERT_SQL = """
    INSERT INTO shared.llm_call_log
        (tenant_id, stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal,
         content_piece_id, angle_gate_request_id, stop_reason, account, fallback_used, provider)
    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::uuid, $10::uuid, $11, $12, $13, $14)
"""


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
      - starts with "gpt"    -> openai
      - anything else        -> bedrock-native (acc2 raw inference-profile id, or already-clean id)
    A caller that DOES pass `provider` wins (e.g. it knows better than a string heuristic)."""
    clean = model
    derived = provider
    if model and model.startswith("satellite-"):
        clean = model[len("satellite-"):]
        derived = derived or "bedrock-satellite"
    elif model and model.lower().startswith("gpt"):
        derived = derived or "openai"
    elif model:
        derived = derived or "bedrock-native"
    return clean, derived


async def record_call(
    *, stage: str, role: str, model: str, tokens_in: Optional[int], tokens_out: Optional[int],
    cost_usd: Optional[float], quality_signal: dict, tenant_id: Optional[str] = None,
    content_piece_id: Optional[str] = None, angle_gate_request_id: Optional[str] = None,
    stop_reason: Optional[str] = None,
    account: Optional[str] = None, fallback_used: Optional[bool] = None,
    provider: Optional[str] = None,
) -> None:
    """Opens its own short-lived connection — the call sites this is used from (S1 graph nodes,
    N7 E2-E5, judge_client.py, T5 atomize) are scattered across sync and async code with no
    single shared asyncpg.Pool threaded down to them (T9/T5's own async orchestrators DO have a
    pool in scope — those call sites pass it to `record_call_with_pool()` instead, see below, to
    avoid paying for a whole new connection when an open one is already sitting right there).

    `stop_reason` (AA-493): "end_turn"/"max_tokens"/"stop_sequence" (Anthropic) or
    "stop"/"length"/... (OpenAI's `finish_reason`, same slot) — None for any caller that hasn't
    been threaded through yet, so this stays backward compatible field-by-field.

    `account`/`fallback_used`/`provider` (AA-617): per-account LLM cost tracking. `account` is
    "acc1"/"acc2"/"acc3" (or None for OpenAI); `fallback_used` is LLMResponse.fallback_used;
    `provider` is derived from the model string when not passed. `model` is stored with the
    "satellite-" prefix stripped (see _normalize_model_provider). All optional — a caller that
    hasn't been threaded through yet writes NULL, staying backward compatible field-by-field."""
    clean_model, provider = _normalize_model_provider(model, provider)
    try:
        conn = await asyncpg.connect(get_database_url(), ssl="require")
        try:
            await conn.execute(
                _INSERT_SQL, tenant_id, stage, role, clean_model, tokens_in, tokens_out, cost_usd,
                json.dumps(quality_signal), content_piece_id, angle_gate_request_id, stop_reason,
                account, fallback_used, provider,
            )
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
) -> None:
    """Same as record_call() but reuses an already-open asyncpg.Pool (T5/T9's async orchestrators
    both have one in scope) instead of opening a fresh connection per call. See record_call() for
    the AA-617 account/fallback_used/provider params."""
    clean_model, provider = _normalize_model_provider(model, provider)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                _INSERT_SQL, tenant_id, stage, role, clean_model, tokens_in, tokens_out, cost_usd,
                json.dumps(quality_signal), content_piece_id, angle_gate_request_id, stop_reason,
                account, fallback_used, provider,
            )
    except Exception as e:
        logger.warning("llm_call_log_write_failed", stage=stage, role=role, error=str(e))


def record_call_sync(**kwargs) -> None:
    """Sync wrapper — same "already running inside asyncio.to_thread(), safe to asyncio.run()
    here" reasoning as role_config.py::get_stage_config_sync(), and the same one real exception
    (`s1_from_atom.py::_call_claude_satellite()`, called directly from async code, no
    `to_thread()`). Unlike the config read, a log write's result is never needed synchronously —
    when a running loop is detected, this schedules the write as a background task
    (`asyncio.ensure_future`) instead of blocking, rather than spinning up a worker thread just
    to wait on a fire-and-forget write."""
    try:
        asyncio.get_running_loop()
        asyncio.ensure_future(record_call(**kwargs))
        return
    except RuntimeError:
        pass  # no running loop — the common path, asyncio.run() below is safe
    try:
        asyncio.run(record_call(**kwargs))
    except Exception as e:
        logger.warning("llm_call_log_write_failed_sync", stage=kwargs.get("stage"), error=str(e))
