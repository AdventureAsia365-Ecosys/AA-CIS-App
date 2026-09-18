"""
services.acp_angle_gate.generate — the one real LLM call T8 makes (workflow steps 5-6: sinh 3
angle + recommend 1).

Model: Bedrock Sonnet 4.5 via shared.llm_client.client.LLMClient (model_tier="sonnet") — the
same "exclusive LLM layer" services/content_generation/graph.py::generate_node() already uses
for its one real content-strategy LLM call, per the build task's explicit instruction ("đúng
exclusive LLM layer đã chốt"). This is deliberately NOT services/acp_produce/generation.py's
Bedrock-satellite-acc1-Sonnet-only path (that module writes long-form draft CONTENT, a different
job with its own separate AA-334 model decision) and NOT judge_client.py's Nova Pro path (that's
a cross-vendor QUALITY judge, must never share a model with any writer per ADR-2026-014/027) —
angle generation is a content-STRATEGY call, the same kind of job graph.py's generate_node()
does, so it reuses that module's own LLM layer, not either of the other two.

JSON parsing follows graph.py::generate_node()'s exact pattern: strip markdown fences -> try
json.loads() -> on failure, deterministic json-repair salvage (no re-ask) via the `json_repair`
package, same dependency already used there.
"""
from __future__ import annotations

import asyncio
import json

import structlog
from json_repair import repair_json

from services.acp_angle_gate.brand_audience import BrandAudience
from services.acp_angle_gate.goals import Goal
from services.acp_angle_gate.prompts import SYSTEM_PROMPT, build_user_prompt
from services.acp_shared.dfs_relevance import SearchDemandSignal
from services.acp_shared.piece_history import PriorPiece
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest
from shared.llm_client.call_log import record_call, record_call_with_pool

logger = structlog.get_logger()

_REQUIRED_ANGLE_FIELDS = ("name", "why_it_works", "formula_fit", "best_final_style")


class AngleGenerationError(Exception):
    """Raised when the LLM response can't be parsed into 3 valid angles, even after json-repair
    salvage — the caller (service.py) surfaces this as a real error, never silently persists a
    partial/malformed set of angles."""


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


def _parse_response(raw: str) -> dict:
    raw = _strip_fences(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        salvaged = repair_json(raw, return_objects=True)
        if isinstance(salvaged, dict) and salvaged.get("angles"):
            return salvaged
        raise AngleGenerationError(f"Could not parse LLM response as JSON: {raw[:300]!r}")


def _validate(parsed: dict) -> tuple[list[dict], int, str]:
    angles = parsed.get("angles")
    if not isinstance(angles, list) or len(angles) != 3:
        raise AngleGenerationError(f"Expected exactly 3 angles, got: {angles!r}")
    for i, a in enumerate(angles):
        missing = [f for f in _REQUIRED_ANGLE_FIELDS if not a.get(f)]
        if missing:
            raise AngleGenerationError(f"Angle {i} missing required field(s): {missing}")
        # AA-512 — "answers" is a soft field (unlike the 4 required ones above): an angle
        # genuinely answering zero of the supplied PAA questions is a valid, common outcome, not
        # a malformed response. Coerce anything not a clean list[str] to [] rather than raising —
        # ranking.py::rank_angles() re-verifies every claim anyway, so a garbage claim here just
        # fails to match, never crashes or corrupts a stored row.
        claimed = a.get("answers")
        a["answers"] = [c for c in claimed if isinstance(c, str)] if isinstance(claimed, list) else []
    recommended_index = parsed.get("recommended_index")
    if recommended_index not in (0, 1, 2):
        logger.warning("angle_gate_bad_recommended_index", value=recommended_index)
        recommended_index = 0  # deterministic fallback — never crash on this one soft field
    reason = parsed.get("recommendation_reason") or ""
    return angles, recommended_index, reason


async def generate_angles(
    *, content_seed: str, goal: Goal, brand_audience: BrandAudience,
    destination: str | None = None, trip_name: str | None = None,
    search_demand: SearchDemandSignal | None = None,
    piece_history: list[PriorPiece] | None = None,  # AA-498, optional (defaults None/empty so
    # existing tests/callers that don't pass it are unaffected)
    tenant_id=None, request_id=None, pool=None,  # AA-505 — attribution for llm_call_log, optional
    # (defaults None) so existing tests that call this without them are unaffected.
) -> tuple[list[dict], int, str, float]:
    """Returns (angles, recommended_index, recommendation_reason, cost_usd). `angles` is a list
    of exactly 3 dicts with the 4 required fields — service.py persists these directly into
    angle_gate_option rows. Raises AngleGenerationError on anything that can't be salvaged into
    a valid 3-angle set (never persists a partial/malformed result).

    AA-469 Việc 4 (flow-order fix) — no `channel` param anymore. Channel is now chosen AFTER an
    angle, not before angle generation — see this module's prompts.py sibling for why dropping
    it here doesn't lose any real channel-fit (T9's write prompt re-applies the full channel
    style block at write time regardless).

    `piece_history` (AA-498, Decision 4) — real prior pieces already written for this exact
    atom, so the LLM can avoid repeating an angle. Empty/None omits the prompt block entirely,
    same "no signal -> no prompt noise" precedent `search_demand` already set."""
    user_prompt = build_user_prompt(
        content_seed=content_seed, goal=goal,
        brand_audience=brand_audience, destination=destination, trip_name=trip_name,
        search_demand=search_demand, piece_history=piece_history,
    )
    client = LLMClient()
    request = LLMRequest(
        system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt, stage="t8_angle_gen", max_tokens=2048,
    )
    resp = await asyncio.to_thread(client.generate, request)
    parsed = _parse_response(resp.content)
    angles, recommended_index, reason = _validate(parsed)
    logger.info(
        "angle_gate_angles_generated", goal=goal["key"],
        model_used=resp.model_used, cost_usd=resp.cost_usd, recommended_index=recommended_index,
    )
    avg_answers = (sum(len(a["answers"]) for a in angles) / len(angles)) if angles else 0
    quality_signal = {
        "angles_generated": len(angles), "recommended_index": recommended_index,
        "avg_paa_answers_per_angle": round(avg_answers, 2),
    }
    if pool is not None:
        await record_call_with_pool(
            pool, stage="t8_angle_gen", role="writer", model=resp.model_used,
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd,
            tenant_id=str(tenant_id) if tenant_id else None,
            angle_gate_request_id=str(request_id) if request_id else None,
            quality_signal=quality_signal,
            stop_reason=getattr(resp, "stop_reason", None),
            account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None),
        )
    else:
        await record_call(
            stage="t8_angle_gen", role="writer", model=resp.model_used,
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd,
            tenant_id=str(tenant_id) if tenant_id else None,
            angle_gate_request_id=str(request_id) if request_id else None,
            quality_signal=quality_signal,
            stop_reason=getattr(resp, "stop_reason", None),
            account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None),
        )
    return angles, recommended_index, reason, resp.cost_usd


__all__ = ["AngleGenerationError", "generate_angles"]
