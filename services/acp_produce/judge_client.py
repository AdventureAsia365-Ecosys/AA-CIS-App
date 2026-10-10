"""
services.acp_produce.judge_client — the T10 (F8/F9) judge call.

ADR-2026-014/027 (L3): the judge runs on a different vendor than the writer and never sees the
writer's generation prompt — only (piece text + rubric + corpus). This module imports nothing from
the writer modules, so that isolation can be checked by reading imports.

Every call goes through the gateway stage route (AA-659 / ADR 0006): the model, fallbacks and shadow
come from shared.llm_role_config for the `stage` (e.g. `t10_judge`). AA-757 (S224) removed the
pre-gateway paths (Nova Pro / GPT-5.6 Sol on Bedrock, GPT-4.1 direct OpenAI, JUDGE_MODEL) that only
the AA-351 comparison scripts could still reach.
"""
from __future__ import annotations

import json

from json_repair import repair_json


def invoke_judge(system_prompt: str, user_prompt: str, max_tokens: int = 2048, *, stage: str) -> dict:
    """One judge call through the stage route. temperature=0 is requested; the catalog drops it for
    models that do not accept it. Returns the raw text plus the fields the caller logs."""
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest

    resp = LLMClient().generate(LLMRequest(
        system_prompt=system_prompt, user_prompt=user_prompt, max_tokens=max_tokens,
        temperature=0, stage=stage,
    ))
    return {
        "text": resp.content,
        "model_used": resp.model_used,
        "provider": resp.provider,
        "input_tokens": resp.input_tokens,
        "output_tokens": resp.output_tokens,
        "stop_reason": resp.stop_reason,
        "account": resp.satellite_account,
        "fallback_used": resp.fallback_used,
        "cost_usd": resp.cost_usd,
    }


def parse_judge_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        salvaged = repair_json(raw, return_objects=True)
        if isinstance(salvaged, dict):
            return salvaged
        raise
