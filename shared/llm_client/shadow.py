"""AA-659 / ADR 0006 — persist one shadow run: primary vs shadow output (shared.llm_shadow_log)
plus the shadow's own cost row in shared.llm_call_log, so its spend stays visible.

Runs on the shadow's background thread (no event loop there). Never raises.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Optional

import asyncpg
import structlog

from shared.secrets import get_database_url

from .call_log import record_call
from .models import LLMRequest, LLMResponse

logger = structlog.get_logger()

_MAX_OUTPUT_CHARS = 16000

_INSERT_SQL = """
    INSERT INTO shared.llm_shadow_log
        (stage, primary_model, shadow_model, primary_output, shadow_output, shadow_error,
         primary_cost_usd, shadow_cost_usd, shadow_latency_ms, request_sha256)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
"""


def _request_hash(request: LLMRequest) -> str:
    return hashlib.sha256(f"{request.system_prompt}\n\n{request.user_prompt}".encode()).hexdigest()


async def _record(*, stage: str, role: str, request: LLMRequest, primary: LLMResponse,
                  shadow_model: str, shadow: Optional[LLMResponse], error: Optional[str],
                  latency_ms: int) -> None:
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        await conn.execute(
            _INSERT_SQL, stage, primary.model_used, shadow.model_used if shadow else shadow_model,
            (primary.content or "")[:_MAX_OUTPUT_CHARS],
            (shadow.content or "")[:_MAX_OUTPUT_CHARS] if shadow else None,
            error, primary.cost_usd, shadow.cost_usd if shadow else None, latency_ms,
            _request_hash(request),
        )
    finally:
        await conn.close()
    if shadow is not None:
        await record_call(
            stage=stage, role=role, model=shadow.model_used,
            tokens_in=shadow.input_tokens, tokens_out=shadow.output_tokens,
            cost_usd=shadow.cost_usd,
            quality_signal={"shadow": True, "shadow_of": primary.model_used},
            stop_reason=shadow.stop_reason, account=shadow.satellite_account,
            fallback_used=shadow.fallback_used, provider=shadow.provider,
        )


def record_shadow_sync(**kwargs) -> None:
    try:
        asyncio.run(_record(**kwargs))
    except Exception as e:
        logger.warning("llm_shadow_log_write_failed", stage=kwargs.get("stage"), error=str(e))
