"""AA-712 — Model Gateway calls of the place pipeline, each logged to shared.llm_call_log (the job id
is attached by the worker's call_log.bind_job) and checked against the job's spend budget first.

Stages (migration 226): a3_place_days (day skeleton), a3_place_locate (coordinates per 40 names),
a3_place_embed (Cohere Embed v4, 1536-d, search_document — must equal TripPlanner's
tp_search_embed query vectors). Blocking gateway calls run in a worker thread (S201: a blocking call
on the worker loop starved /health).
"""
from __future__ import annotations

import asyncio
from typing import Optional

from shared.llm_client.call_log import billed_fields, record_call_with_pool
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest

DAYS_STAGE = "a3_place_days"
LOCATE_STAGE = "a3_place_locate"
EMBED_STAGE = "a3_place_embed"
EMBED_BATCH = 96
EMBED_DIMENSIONS = 1536
# Pre-call estimates for the budget check (design §8: days ≤ $0.028, locate ≤ $0.015 per call).
ESTIMATE_USD = {DAYS_STAGE: 0.04, LOCATE_STAGE: 0.02, EMBED_STAGE: 0.002}


def _generate_sync(stage: str, system: str, user: str, max_tokens: int):
    # temperature 0: extraction, not prose — the same tour should give the same skeleton.
    return LLMClient().generate(LLMRequest(system_prompt=system, user_prompt=user, stage=stage,
                                           max_tokens=max_tokens, temperature=0.0))


def make_generate(pool, stage: str, max_tokens: int, budget=None, quality_signal: Optional[dict] = None,
                  spent: Optional[dict] = None):
    """An async (system, user) -> text function for days.extract_tour / locate.locate_many."""
    async def generate(system: str, user: str) -> str:
        if budget is not None:
            budget.check(ESTIMATE_USD.get(stage, 0.05))
        resp = await asyncio.to_thread(_generate_sync, stage, system, user, max_tokens)
        if budget is not None:
            budget.charge(resp.cost_usd)
        if spent is not None:
            spent[stage] = spent.get(stage, 0.0) + float(resp.cost_usd or 0.0)
            spent["calls"] = spent.get("calls", 0) + 1
        await record_call_with_pool(
            pool, stage=stage, role="validate", model=resp.model_used,
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd, quality_signal={"app": "cis", "issue": "AA-712", **(quality_signal or {}),
                                                    "output_len_chars": len(resp.content or "")},
            stop_reason=getattr(resp, "stop_reason", None), account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None), **billed_fields(resp))
        return resp.content
    return generate


async def embed_texts(pool, texts: list[str], budget=None, spent: Optional[dict] = None) -> list[list[float]]:
    """One vector per text (96 per call). Raises when a call fails (the job retries)."""
    from shared.llm_client.embed import embed

    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = texts[i:i + EMBED_BATCH]
        if budget is not None:
            budget.check(ESTIMATE_USD[EMBED_STAGE])
        resp = await asyncio.to_thread(embed, EMBED_STAGE, batch, input_type="search_document",
                                       dimensions=EMBED_DIMENSIONS)
        if budget is not None:
            budget.charge(resp.cost_usd)
        if spent is not None:
            spent[EMBED_STAGE] = spent.get(EMBED_STAGE, 0.0) + float(resp.cost_usd or 0.0)
        await record_call_with_pool(
            pool, stage=EMBED_STAGE, role="embed", model=resp.model_used, tokens_in=resp.input_tokens,
            tokens_out=0, cost_usd=resp.cost_usd,
            quality_signal={"app": "cis", "issue": "AA-712", "texts": len(batch),
                            "dimensions": len(resp.vectors[0]), "tokens_estimated": resp.tokens_estimated},
            account=resp.account, fallback_used=resp.fallback_used,
            provider="bedrock-native" if resp.account == "acc2" else "bedrock-satellite", **billed_fields(resp))
        out.extend(resp.vectors)
    return out


def vector_literal(vec: list[float]) -> str:
    """pgvector text literal ('[0.1,0.2,...]'), cast with ::vector in SQL (no asyncpg codec)."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
