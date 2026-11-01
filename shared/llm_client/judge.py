"""
shared.llm_client.judge — the one judge client (AA-765).

Every LLM judge of the content pipelines builds its request and reaches the gateway here:
- S1 / T2 brand fit and the T7 Debate candidate fit (`services/content_generation/brand_fit.py`, stages
  `s1_judge` / `t2_judge` / `t7_debate_judge`) through `call_judge`;
- the T10 F9 judge (`services/acp_content_writing/quality_gates.py`, stage `t10_judge`) through `invoke_judge`.

The model, fallbacks and shadow come from the stage route in `shared.llm_role_config` (AA-659 / ADR 0006);
nothing here names a model. The judge-vendor-differs-from-writer rule (ADR-2026-014/027) is enforced when a
route is saved in admin. This module imports nothing from the writer modules, so the judge never sees the
writer's generation prompt — only what its caller puts in the judge prompt.

This module makes the call and returns the reply; it writes no `shared.llm_call_log` row. The caller does,
once it knows the verdict (judge_node / debate / quality_gates — registered in
tests/unit/test_aa927_llm_call_site_registry.py).
"""
from __future__ import annotations

import json
from typing import Optional

from json_repair import repair_json


def judge_request(system_prompt: str, user_prompt: str, *, stage: str, temperature: float,
                  max_tokens: Optional[int] = None, seed: Optional[int] = None,
                  model_tier: Optional[str] = None):
    """The judge's LLMRequest. Only the optional fields a caller gives are set on the request: the gateway
    reads `model_fields_set` to decide whether temperature / seed are forwarded, and an unset max_tokens keeps
    the model default. `model_tier` is the gateway's per-request model override (adhoc measurement only — the
    caller guards the stage)."""
    from shared.llm_client.models import LLMRequest

    fields: dict = {"system_prompt": system_prompt, "user_prompt": user_prompt, "stage": stage,
                    "temperature": temperature}
    if max_tokens is not None:
        fields["max_tokens"] = max_tokens
    if seed is not None:
        fields["seed"] = seed
    if model_tier:
        fields["model_tier"] = model_tier
    return LLMRequest(**fields)


def call_judge(system_prompt: str, user_prompt: str, *, stage: str, temperature: float,
               max_tokens: Optional[int] = None, seed: Optional[int] = None, model_tier: Optional[str] = None,
               client=None):
    """One judge call through the stage route; returns the gateway's LLMResponse. `client` is an LLMClient the
    caller already holds (brand_fit passes its own, the seam its tests patch); None builds one."""
    if client is None:
        from shared.llm_client.client import LLMClient
        client = LLMClient()
    return client.generate(judge_request(system_prompt, user_prompt, stage=stage, temperature=temperature,
                                         max_tokens=max_tokens, seed=seed, model_tier=model_tier))


def invoke_judge(system_prompt: str, user_prompt: str, max_tokens: int = 2048, *, stage: str) -> dict:
    """The T10 judge call (temperature 0 is requested; the catalog drops it for models that do not accept
    it). Returns the raw text plus the fields the caller logs."""
    from shared.llm_client.call_log import billed_fields
    resp = call_judge(system_prompt, user_prompt, stage=stage, temperature=0, max_tokens=max_tokens)
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
        # AA-1002 (S6): cache split, pricing basis, vendor connection (call_log.billed_fields keys)
        **billed_fields(resp),
    }


def strip_json_fence(raw: str) -> str:
    """The reply without surrounding whitespace and without a ``` / ```json fence."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


def parse_judge_json_checked(raw: str) -> tuple[dict, bool]:
    """(verdict, repaired): `repaired` is True when the reply was not valid JSON and json_repair
    salvaged it (a truncated reply keeps only the entries before the cut — AA-777 trusts such a
    verdict for its flags but never for what it passed).

    AA-1026: the reply's first complete JSON value is read as is (text after it ignored: json_repair turned
    "verdict + note" into a list, which raised); only a reply whose first value does not decode is repaired."""
    from shared.llm_client.json_reply import JsonReplyError, first_json_value

    raw = strip_json_fence(raw)
    try:
        return json.loads(raw), False
    except json.JSONDecodeError:
        try:
            return first_json_value(raw, kinds=(dict, list), scan=False), False
        except JsonReplyError:
            pass
        salvaged = repair_json(raw, return_objects=True)
        if isinstance(salvaged, dict):
            return salvaged, True
        raise


def parse_judge_json(raw: str) -> dict:
    return parse_judge_json_checked(raw)[0]


__all__ = ["call_judge", "invoke_judge", "judge_request", "parse_judge_json", "parse_judge_json_checked",
           "strip_json_fence"]
