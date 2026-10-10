"""AA-748 — structured per-day source facts for the S1 writer (behind S1_STRUCTURED_FACTS).

S218 audit v4: most S1 versions need at least one grounding repair and itineraries are often
compressed, both tracing back to the writer getting raw source text and paraphrasing it freely.
This module extracts a compact, per-day fact table from the source ONCE (one LLM call per tour on
the new ``s1_source_facts`` stage), so the writer prompt can constrain numbers/meals/places to what
the source actually lists while still keeping the raw itinerary for prose detail.

Design contract (AA-748, S224):
- ``extract_day_facts(tour)`` -> ``{"days": [...], "used_fallback": bool}``.
- The source is split with the ONE day splitter, ``prompts.parse_source_day_word_counts(...)
  ["day_text"]`` — never a second one. Parser fallback (no day markers) -> ``used_fallback=True``,
  no facts, the prompt stays as today.
- One LLM call per tour through ``LLMClient.generate()`` on stage ``s1_source_facts`` (role writer,
  a cheap model), JSON output, cost logged to ``shared.llm_call_log`` like every S1 stage.
- Honesty guard (deterministic): any number/time in the extracted facts that does not appear in the
  source (``grounding.source_number_parts(tour)`` + ``find_novel_numeric_claims``) is dropped;
  meals not literally present in the raw source are dropped. Any extraction failure -> no facts,
  prompt as today (fail open).
"""
from __future__ import annotations

import json
import os
from typing import Optional

import structlog

logger = structlog.get_logger()

STAGE = "s1_source_facts"

# The per-day fact fields the extractor returns and the prompt renders, in render order.
# AA-748 round 2 (S224): figures only. The SOURCE FACTS rule constrains numbers/times/meals; asking
# for places/activities/transport tripled the output tokens (Haiku bills output at 5x) and the
# round-1 rule that took places/activities from the list made itineraries thinner.
FACT_FIELDS = (
    "distances", "durations", "altitudes", "times", "meals", "other_numbers",
)
# Fields whose values are checked by the number/time honesty guard (a value that introduces a
# number the source never states is dropped). places/activities are prose, not number-bearing.
_NUMBER_GUARDED_FIELDS = ("distances", "durations", "altitudes", "times", "other_numbers")
# Common meal words a model may invent; a meal value is kept only if it appears literally in the
# raw source (case-insensitive).
_MEAL_WORDS = ("breakfast", "lunch", "dinner", "brunch", "supper", "snack", "tea")

_SYSTEM_PROMPT = """You extract a compact fact table from one travel tour's day-by-day itinerary.
For EACH day you are given, return only facts that are LITERALLY present in that day's source text —
never infer, summarise loosely, or add background. Do not invent numbers, distances, durations,
altitudes, clock-times or meals: copy them exactly as the source states them, or leave the field
empty. Return STRICT JSON only, no markdown, no explanation."""

_SCHEMA = {
    "name": "source_day_facts",
    "schema": {
        "type": "object",
        "properties": {
            "days": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "day": {"type": "integer"},
                        "distances": {"type": "array", "items": {"type": "string"}},
                        "durations": {"type": "array", "items": {"type": "string"}},
                        "altitudes": {"type": "array", "items": {"type": "string"}},
                        "times": {"type": "array", "items": {"type": "string"}},
                        "meals": {"type": "array", "items": {"type": "string"}},
                        "other_numbers": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["day"],
                },
            },
        },
        "required": ["days"],
    },
}


def _flag_on(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# AA-748: structured per-day source facts for the writer. Default OFF — with the flag off the writer
# prompt is byte-identical to today. Same resolve order as AA-747's s1_per_day_targets_enabled: a
# tenant-config flag (``tenant_flags["S1_STRUCTURED_FACTS"]``) wins when present; otherwise the
# S1_STRUCTURED_FACTS env var decides (default off).
_S1_STRUCTURED_FACTS_ENV = "S1_STRUCTURED_FACTS"


def s1_structured_facts_enabled(tenant_flags: dict | None = None) -> bool:
    """Whether to extract and feed structured per-day source facts to the S1 writer. OFF by
    default. A tenant-config flag wins over the ``S1_STRUCTURED_FACTS`` env var."""
    if tenant_flags and _S1_STRUCTURED_FACTS_ENV in tenant_flags:
        return _flag_on(tenant_flags[_S1_STRUCTURED_FACTS_ENV])
    return _flag_on(os.environ.get(_S1_STRUCTURED_FACTS_ENV, "false"))


def _source_days(tour: dict) -> tuple[dict, bool]:
    """Split the source itinerary by day with the ONE day splitter
    (prompts.parse_source_day_word_counts — never a second one). Returns ({day_num: text},
    used_fallback)."""
    from .prompts import parse_source_day_word_counts

    itineraries_raw = tour.get("itineraries") or tour.get("itinerary") or ""
    parsed = parse_source_day_word_counts(str(itineraries_raw), tour.get("duration") or "")
    return parsed["day_text"], parsed["used_fallback"]


def _extract_prompt(day_text: dict) -> str:
    """One compact prompt listing each day's source text, asking for the fact table."""
    blocks = []
    for day in sorted(day_text):
        text = (day_text[day] or "").strip()
        blocks.append(f"DAY {day}:\n{text}")
    fields = ", ".join(FACT_FIELDS)
    return (
        "Extract facts for each day below. For every day return an object with the day number and "
        f"these fields (each a list of short strings, empty if the day states none): {fields}.\n"
        "- distances / durations / altitudes / times: copy the exact figure+unit from the source.\n"
        "- meals: only meals the source literally names for that day.\n"
        "- other_numbers: any other source number with its unit.\n\n"
        + "\n\n".join(blocks)
        + '\n\nReturn JSON: {"days": [{"day": 1, "distances": [...], ...}, ...]}. Omit a field that is'
        + " empty and omit a day with no figures at all."
    )


def _coerce_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    s = str(value).strip()
    return [s] if s else []


def _normalize_days(raw_days) -> list[dict]:
    """Shape the model output into our fixed per-day dict (every FACT_FIELDS key present)."""
    out: list[dict] = []
    if not isinstance(raw_days, list):
        return out
    for item in raw_days:
        if not isinstance(item, dict):
            continue
        try:
            day = int(item.get("day"))
        except (TypeError, ValueError):
            continue
        day_facts = {"day": day}
        for field in FACT_FIELDS:
            day_facts[field] = _coerce_list(item.get(field))
        out.append(day_facts)
    return out


def _apply_honesty_guard(days: list[dict], tour: dict) -> list[dict]:
    """Deterministic honesty guard (reuses grounding.source_number_parts +
    find_novel_numeric_claims): drop any number/time value that introduces a number the source
    never states, and drop any meal not literally present in the raw source. Never raises — the
    caller already fails open on error, this keeps every field it can verify."""
    from services.acp_shared.grounding import find_novel_numeric_claims

    from .grounding import source_number_parts

    source_parts = source_number_parts(tour)
    raw_source = _raw_source_text(tour).lower()

    guarded: list[dict] = []
    for day in days:
        kept = {"day": day["day"]}
        for field in FACT_FIELDS:
            values = day.get(field) or []
            if field in _NUMBER_GUARDED_FIELDS:
                values = [v for v in values if not find_novel_numeric_claims(v, source_parts)]
            elif field == "meals":
                values = [v for v in values if _meal_in_source(v, raw_source)]
            kept[field] = values
        guarded.append(kept)
    return guarded


def _meal_in_source(value: str, raw_source_lower: str) -> bool:
    """A meal value is kept only when it is literally present in the raw source. If the value names
    a known meal word (breakfast/lunch/...), that word must appear in the source; otherwise the
    whole value string must appear."""
    v = (value or "").strip().lower()
    if not v:
        return False
    named = [w for w in _MEAL_WORDS if w in v]
    if named:
        return all(w in raw_source_lower for w in named)
    return v in raw_source_lower


def _raw_source_text(tour: dict) -> str:
    """The raw source fields a meal might legitimately be stated in (the itinerary plus inclusions/
    exclusions, where 'Meals: ...' logistics live), lower-cased by the caller."""
    parts = []
    for f in ("itineraries", "itinerary", "inclusions", "exclusions", "summary", "description"):
        value = tour.get(f)
        if value:
            parts.append(str(value))
    return "\n".join(parts)


def _parse_json(raw: str) -> Optional[dict]:
    """Decode the extractor output, with the same fence-strip + json-repair salvage the writer path
    uses. Returns None when nothing usable can be parsed."""
    from json_repair import repair_json

    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = repair_json(text, return_objects=True)
    return parsed if isinstance(parsed, dict) else None


def extract_day_facts(tour: dict, *, client=None, model_tier=None) -> dict:
    """Extract per-day source facts for one tour (AA-748). One LLM call on stage ``s1_source_facts``,
    JSON output, cost logged to shared.llm_call_log; the deterministic honesty guard then drops any
    fabricated number/time/meal. Fails open: any missing source, parser fallback, or extraction
    error returns no facts so the writer prompt stays exactly as today.

    Returns ``{"days": [{"day", "distances", "durations",
    "altitudes", "times", "meals", "other_numbers"}], "used_fallback": bool}``."""
    tour = tour or {}
    day_text, used_fallback = _source_days(tour)
    if used_fallback or not day_text:
        # No clear per-day markers -> no facts, prompt as today (fail open).
        return {"days": [], "used_fallback": True}

    try:
        from shared.llm_client.call_log import record_call_sync
        from shared.llm_client.client import LLMClient
        from shared.llm_client.models import LLMRequest

        client = client or LLMClient()
        resp = client.generate(LLMRequest(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_extract_prompt(day_text),
            model_tier=model_tier,
            stage=STAGE,
            temperature=0.1,
            max_tokens=4096,
            json_schema=_SCHEMA,
        ))
        parsed = _parse_json(resp.content)
        days = _normalize_days(parsed.get("days") if parsed else None)
        days = _apply_honesty_guard(days, tour)
        record_call_sync(
            stage=STAGE, role="validate", model=resp.model_used,
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd, tenant_id=None,
            quality_signal={"source": "s1_source_facts", "days": len(days)},
            stop_reason=getattr(resp, "stop_reason", None),
            account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None),
        )
    except Exception as exc:                       # never break the rewrite
        logger.warning("source_facts_extract_failed", error=str(exc)[:200])
        return {"days": [], "used_fallback": False}

    if not days:
        return {"days": [], "used_fallback": False}
    logger.info("source_facts_extracted", days=len(days))
    return {"days": days, "used_fallback": False}
