"""AA-329: shared itinerary day-parsing + per-day nudge-repair helpers (single source of truth).

ITINERARY_CLAMP_MIN/MAX and the per-day nudge call were originally private to graph.py (AA-353).
Extracted here so flag_fix_node.py can reuse the SAME nudge for ITINERARY_STILL_COMPRESSED —
graph.py imports flag_fix_node.py, so flag_fix_node.py cannot import graph.py back (circular).
Same fix AA-205 used for seo_meta (see seo_meta_utils.py's own docstring) — same pattern, new file.
"""
import json
import re

from json_repair import repair_json

from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest

# AA-353: hard clamp on actual/source word-count ratio per day, checked AFTER the model returns
# its structured itineraries array (the prompt's own 0.7x-1.3x guidance is a softer target the
# model aims for; this is the line that triggers a real fix).
ITINERARY_CLAMP_MIN = 0.6
ITINERARY_CLAMP_MAX = 1.5

_ITINERARY_NUDGE_SYSTEM_PROMPT = """You are a travel content editor for Adventure Asia, fixing the
LENGTH of ONE day of a tour itinerary that was written too short or too long relative to its
source detail. Rewrite ONLY this one day, in the same brand voice (calm, factual, editorial — not
salesy, not generic).

Rules that still apply:
- The day title MUST name the place and/or the primary activity — never generic
  ("Free Day", "Arrival Day", "Departure", "Transfer", "Exploration").
- Preserve all factual details (named places, activities) from the source day below.
- Do not invent activities not present in the source day.
- NEVER invent meal names (breakfast, lunch, dinner) or clock-times unless they appear
  explicitly in the source day.

Return JSON ONLY, no markdown, no preamble: {"title": "...", "body": "..."}"""


def in_clamp(ratio: float) -> bool:
    return ITINERARY_CLAMP_MIN <= ratio <= ITINERARY_CLAMP_MAX


def nudge_itinerary_day(client: LLMClient, source_day_text: str, current_title: str,
                         current_body: str, target_word_count: int):
    """AA-353: single targeted rewrite of one day that clamped outside [ITINERARY_CLAMP_MIN,
    ITINERARY_CLAMP_MAX] of its source word count. One attempt — callers do not loop this
    themselves (graph.py's generate_node never retries it; flag_fix_node.py's AA-329c repair
    calls it at most once per still-violating day and keeps the pre-fix day if the result is
    still out of clamp — see its own deterministic guard).
    """
    # AA-608: strip routine meal-logistics codes from the source day too, so a length nudge
    # cannot reintroduce a B/L/D token the main writer strip already removed.
    from .prompts import strip_itinerary_meal_metadata
    source_day_text = strip_itinerary_meal_metadata(source_day_text)
    user_prompt = f"""SOURCE (this day only, from the original tour itinerary):
{source_day_text}

CURRENT DRAFT (too short or too long relative to the source above):
Title: {current_title}
Body: {current_body}

TARGET LENGTH: approximately {target_word_count} words for the body.

Rewrite this one day's title and body to hit the target length while staying factual and
on-brand."""
    request = LLMRequest(
        system_prompt=_ITINERARY_NUDGE_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        # AA-518: was a hardcoded "haiku" literal — now admin-configurable via the
        # "s1_itinerary_nudge" stage (seeded to haiku, matching this exact prior value).
        stage="s1_itinerary_nudge",
    )
    resp = client.generate(request)
    raw = resp.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        salvaged = repair_json(raw, return_objects=True)
        parsed = salvaged if isinstance(salvaged, dict) else {}
    new_title = parsed.get("title") or current_title
    new_body = parsed.get("body") or current_body
    return new_title, new_body, resp


# AA-329: the "Day N — Title\nBody" canonical string format graph.py's _process_itineraries
# (AA-353) always serializes to before any downstream node (validate_node, flag_fix_node, ...)
# sees the itineraries field. Parsing it back into per-day title/body lets validate_node compute
# FRESH per-day word counts on every call (see AA-329 implementation notes for why this is
# preferred over trusting the one-shot state["itinerary_day_ratios"]) and lets flag_fix_node
# target a single violating day for repair without touching the others.
_CANONICAL_DAY_RE = re.compile(r"^Day\s+(\d+)\s+—\s*(.*)$", re.MULTILINE)


def parse_canonical_itinerary_days(text: str) -> dict:
    """{day_num: {"title": str, "body": str}} from the canonical "Day N — Title\\nBody" string.
    Returns {} when the text doesn't use that format (e.g. legacy/non-array-contract content) —
    callers must treat an empty result as "cannot determine", not "zero days"."""
    text = text or ""
    days = {}
    matches = list(_CANONICAL_DAY_RE.finditer(text))
    for i, m in enumerate(matches):
        day_num = int(m.group(1))
        title = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        days[day_num] = {"title": title, "body": body}
    return days


def serialize_itinerary_days(days: dict) -> str:
    """Inverse of parse_canonical_itinerary_days — same format _process_itineraries emits."""
    return "\n\n".join(
        f"Day {d} — {days[d]['title']}\n{days[d]['body']}".strip()
        for d in sorted(days)
    )


def generated_day_word_counts(text: str) -> dict:
    """{day_num: body_word_count} from the canonical string — body only, matching AA-353's own
    actual_words = len(body.split()) (title isn't counted toward length)."""
    return {d: len(v["body"].split()) for d, v in parse_canonical_itinerary_days(text).items()}
