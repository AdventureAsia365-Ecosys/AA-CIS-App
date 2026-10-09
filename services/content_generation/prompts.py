import os
import re

import structlog

logger = structlog.get_logger()

# AA-747: per-day target word counts in the main generate prompt, behind a flag that defaults OFF.
# Follows how other S1 flags are read (os.environ.get, same as S1_SEO_REUSE_DAYS). Claude Code A/Bs
# it on 10 tours before turning it on — do NOT enable it here. tenant-config override is read first
# (per tour's own config) and falls back to the env var.
_S1_PER_DAY_TARGETS_ENV = "S1_PER_DAY_TARGETS"


def _flag_on(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def s1_per_day_targets_enabled(tenant_flags: dict | None = None) -> bool:
    """AA-747: whether to add explicit per-day TARGET word counts to the generate prompt. OFF by
    default. A tenant-config flag (``tenant_flags["S1_PER_DAY_TARGETS"]``) wins when present;
    otherwise the ``S1_PER_DAY_TARGETS`` env var decides (default off)."""
    if tenant_flags and _S1_PER_DAY_TARGETS_ENV in tenant_flags:
        return _flag_on(tenant_flags[_S1_PER_DAY_TARGETS_ENV])
    return _flag_on(os.environ.get(_S1_PER_DAY_TARGETS_ENV, "false"))


def _clamp_midpoint() -> float:
    """AA-747: midpoint of the per-day clamp band [ITINERARY_CLAMP_MIN, ITINERARY_CLAMP_MAX]
    (imported here, not hardcoded, so a clamp change moves the target with it)."""
    from .itinerary_utils import ITINERARY_CLAMP_MAX, ITINERARY_CLAMP_MIN
    return (ITINERARY_CLAMP_MIN + ITINERARY_CLAMP_MAX) / 2.0

# AA-353: line-anchored so a day-count number mentioned mid-line never matches — e.g. "Easy Day 82
# km Tarmac Road" as a day's OWN title text must not be misread as a "Day 82" marker (AA-346's own
# regression case). Only a line that STARTS with "Day <n>" (optionally "Day #3", "Day3", etc.) counts.
# S207: also accept a leading bullet/symbol ("► Day 01:", "• Day 2", "**Day 3**"), spelled-out numbers
# ("Day One:", "Day Twelve") and ranges ("Days 1–3:" → the range's first day). Measured on Dev
# 01/10/2026: Taiwan "Day One" (34 tours) and Sri Lanka "► Day 01" (11) fell back to an even split,
# which also handed the per-day nudge the WHOLE itinerary as "this day".
_NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty twenty-one twenty-two twenty-three twenty-four "
    "twenty-five twenty-six twenty-seven twenty-eight twenty-nine thirty".split())}
_DAY_MARKER_RE = re.compile(
    r"^[\s►▶•*#>\-–—·|]*days?\s*#?\s*(\d{1,3}|" + "|".join(sorted(_NUM_WORDS, key=len, reverse=True))
    + r")\b(?:\s*[-–—]\s*\d{1,3}\b)?[*\s]*[:\-–—|.]?\s*",
    re.IGNORECASE)


def _day_num(token: str) -> int:
    t = token.lower()
    return int(t) if t.isdigit() else _NUM_WORDS.get(t, 0)


def _estimate_day_count(duration_hint: str) -> int:
    """First integer found in a free-text duration string ("12 Days", "7D/6N") — 0 if none."""
    if not duration_hint:
        return 0
    m = re.search(r"(\d{1,3})", str(duration_hint))
    return int(m.group(1)) if m else 0


def parse_source_day_word_counts(itineraries_raw: str, duration_hint: str = "") -> dict:
    """AA-353: per-day word count (+ text) of the SOURCE itinerary, computed in code (not by the
    LLM — unlike AA-352's itinerary_day, this is a prompt INPUT needed before the model call, not
    an extraction target, so delegating to the model isn't an option here).

    Loose, line-anchored "Day N" boundary parser. Falls back to an even split of the total word
    count across an estimated day count (from ``duration_hint``) when fewer than 2 day-markers are
    found — logged, never silent, since a fallback means every day gets the SAME target ratio
    range, defeating the point of this feature for that one tour. In the fallback case, day_text
    maps every day to the full (unsegmented) source, since no per-day boundary could be found.

    Returns {"day_word_counts": {day_num: word_count}, "day_text": {day_num: text},
    "used_fallback": bool}.
    """
    text = itineraries_raw or ""
    lines = text.splitlines()
    matches = []  # (day_num, line_idx)
    for idx, line in enumerate(lines):
        m = _DAY_MARKER_RE.match(line.strip())
        if m and _day_num(m.group(1)) > 0:
            matches.append((_day_num(m.group(1)), idx))

    if len(matches) < 2:
        total_words = len(text.split())
        est_days = _estimate_day_count(duration_hint) or 1
        per_day = max(1, round(total_words / est_days))
        logger.warning("itinerary_day_parse_fallback", markers_found=len(matches),
                        estimated_days=est_days, total_words=total_words)
        return {
            "day_word_counts": {d: per_day for d in range(1, est_days + 1)},
            "day_text": {d: text for d in range(1, est_days + 1)},
            "used_fallback": True,
        }

    day_word_counts = {}
    day_text = {}
    for i, (day_num, line_idx) in enumerate(matches):
        end = matches[i + 1][1] if i + 1 < len(matches) else len(lines)
        segment_lines = list(lines[line_idx:end])
        # strip the "Day N —" marker prefix off the first line so it isn't double-counted
        segment_lines[0] = _DAY_MARKER_RE.sub("", segment_lines[0].strip(), count=1)
        segment_text = " ".join(segment_lines).strip()
        day_word_counts[day_num] = max(len(segment_text.split()), 1)
        day_text[day_num] = segment_text
    return {"day_word_counts": day_word_counts, "day_text": day_text, "used_fallback": False}


SYSTEM_PROMPT = """You are a professional travel content editor preparing tour content for a
master content catalog used by many different travel brands and audiences — family, adventure,
luxury, and budget alike, not one specific brand or demographic.

EDITORIAL VOICE:
- Calm, factual, editorial. NOT salesy. NOT generic.
- Write like a knowledgeable editor, not a marketing copywriter.
- Tone: Condé Nast Traveller, not TripAdvisor.
- Third person, descriptive. Never address the reader as "you", never use imperatives
  ("ride", "pack your bags"), never adopt a persona or mood/hype register.

NEUTRALIZE PROMOTIONAL SOURCE COPY (AA-724):
The SOURCE may itself be marketing copy — written in a second-person persona voice, with
imperatives, mood/lifestyle framing, or an adventure-operator "sales" tone (common for
cycling, biking, and adventure tours). Do NOT carry that voice across. Rewrite it into
neutral, third-person editorial catalogue prose. Specifically, strip and rephrase:
- Direct address and imperatives: "Sunglasses on", "pack your bags", "get ready to ride".
- Persona / lifestyle / mood framing: "made for riding", "on your own terms",
  "playlist-worthy roads", "the kind of route that justifies an early wake", "built for
  exactly what we're doing".
- Hype intensifiers and sales mood generally.
Keep every factual detail (places, distances, activities, days); change only the register.
A promotional source is not a licence to write promotional output.

STRICT RULES:
1. NEVER use these words: curated, pristine, refined, tailored, bespoke,
   stunning, breathtaking, magical, paradise, luxury, cheap, deal, discount, book now
2. Tour name (aa_name): Rewrite in a clear, specific editorial voice — evocative but specific.
   Good: "South Korea: Temple, Trail & Peninsula — 12 Days"
   Good: "Sri Lanka by Rail and Rickshaw — 10 Days"
   Forbidden in name: "Exploring", "Discover", "Amazing", "Epic", generic verbs.
   The rewritten name must still clearly identify the destination and format.
3. Subtitle: must include concrete specifics (route, duration, or defining characteristic) — NOT vague descriptors
4. Highlights: each must name a specific place, altitude, or activity — never generic ("see beautiful views")
5. Itineraries: rewrite each day in the client's brand voice using the style guide, as the
   structured "itineraries" array described in OUTPUT JSON FORMAT below — one object per source
   day, in day order. Do not merge, split, invent, or drop days.
   Each day title MUST name the place and/or the primary activity of that day.
   GOOD: "Trekking to Sapa Valley Villages"
   GOOD: "Mae Taeng Valley Cycling: Waterfalls, Farmland & Temple"
   FORBIDDEN generic titles: "Exploration", "Free Day", "Arrival Day", "Departure",
   "Transfer" as the whole title — these name no place or activity and trip
   ITINERARY_DAY_TITLE_GENERIC.
   Preserve all factual details (day numbers, named places, activities).
   Do not invent days or activities not present in the source.
   NEVER invent meal names (breakfast, lunch, dinner) or clock-times
   (e.g. "7:00 AM departure") unless they appear explicitly in the source
   itinerary — fabricating them is a PRODUCT_TRUTH_RISK. Describe activities
   by their sequence within the day, not by manufactured times or meals.
   LENGTH PER DAY: see PER-DAY SOURCE LENGTH below. Write each day's body in proportion to
   THAT day's own source length — a day with a lot of source detail should read fuller than a
   simple transit/rest day. Do NOT normalize every day to the same length regardless of source;
   that is the single most common mistake on this task.
6. Do not make factual claims you cannot verify from the source data
7. seo_meta must NOT contain budget travel language: "hostel", "budget", "public transport",
   "cheap", "backpacker", "dorm" — this base catalog reads as premium editorial regardless of
   price point; a specific tenant brand's own forbidden-word list (if any) applies on top.
8. SEO META LENGTH: seo_meta MUST be 140–155 characters — count carefully, NEVER under 140.
   It must be one complete sentence ending in a period. If a draft is under 140, expand it
   with concrete, relevant detail (place, activity, audience) — do NOT pad with filler words.

Output ONLY valid JSON. No preamble, no markdown, no explanation.
"""


_SUBTITLE_INSTRUCTIONS = {
    "standard": "concrete subtitle: route, duration, and 1-2 key landmarks or experiences",
    "seo":      (
        "SEO-optimised: lead with primary keyword (country + activity type),"
        " include duration — e.g. 'South Korea Cycling Tour: 9 Days, Seoul to East Sea'"
    ),
    "concise":  "concise value proposition — max 12 words, lead with country and defining activity",
}


# AA-608 (H3 finding): 81% of rewritten tours tripped ITINERARY_MEAL_TIME_INVENTED
# (breakfast/lunch/dinner or clock-times in the output itinerary = PRODUCT_TRUTH_RISK).
# The prompt already told the model "never invent meals unless in the source" — but the
# SOURCE itineraries are full of routine meal codes (B / L / D, "Meals: Breakfast, Dinner",
# "(B,L,D)"), so from the model's point of view meals ARE in the source and it faithfully
# carries them across. The fix (mirroring Ms.Thu's aa_batch_rewrite_v6 strip_itinerary_meal_metadata)
# is to strip these routine meal-logistics tokens from the source BEFORE it reaches the prompt,
# so the model never sees a meal code to reproduce. Substantive food prose (a named dining
# experience, a cooking class) is left untouched — only bare codes and "Meals:" logistics lines go.
_MEAL_CODES = r"B|L|D|BB|HB|FB"
# A parenthetical meal code at the end of a line: "... arrive at the ryokan. (B, D)"
_TRAILING_MEAL_CODE_RE = re.compile(
    r"\s*\(\s*(?:" + _MEAL_CODES + r")(?:\s*[,/&]\s*(?:" + _MEAL_CODES + r")){0,2}\s*\)\s*$",
    re.IGNORECASE,
)
# A whole line that is just a meal-inclusion label: "Meals: Breakfast, Lunch" / "Meals included: B, L, D"
_MEAL_LABEL_LINE_RE = re.compile(
    r"^\s*meals?(?:\s+included)?\s*[:\-]\s*.*$",
    re.IGNORECASE,
)


def strip_itinerary_meal_metadata(itinerary: str) -> str:
    """Remove routine itinerary meal-logistics tokens (B/L/D codes, 'Meals:' lines) while
    keeping meaningful food descriptions. AA-608: fed through before build_rewrite_prompt so
    the writer never sees a bare meal code to reproduce, which is what was producing the
    PRODUCT_TRUTH_RISK meal/time flags on 81% of tours.

    Conservative by design (same principle as segment matching in aa-social-media — a rule
    that over-reaches deletes real content): only drops (1) a whole line that is nothing but a
    'Meals[: included]' logistics label, and (2) a trailing parenthetical meal code at the end
    of a line. Prose that merely mentions a meal in a substantive way ('dinner is a multi-course
    kaiseki at the ryokan') is left entirely intact."""
    if not itinerary or not isinstance(itinerary, str):
        return itinerary
    out_lines = []
    for line in itinerary.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _MEAL_LABEL_LINE_RE.match(line):
            continue  # drop the whole logistics line
        line = _TRAILING_MEAL_CODE_RE.sub("", line)
        out_lines.append(line)
    cleaned = "\n".join(out_lines)
    # collapse the blank lines a dropped label may have left behind
    cleaned = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", cleaned)
    return cleaned.strip()


def build_rewrite_prompt(tour: dict, seo: dict, few_shots: list[dict] = None,
                         subtitle_focus: str = "standard", per_day_targets: bool = False) -> str:
    few_shot_text = ""
    if few_shots:
        examples = "\n\n".join([
            f"EXAMPLE {i+1}:\nINPUT: {f['input']}\nOUTPUT: {f['output']}"
            for i, f in enumerate(few_shots[:3])
        ])
        few_shot_text = f"\n\nEXAMPLES FOR REFERENCE:\n{examples}\n"

    seo_keywords = seo.get("keywords", {}).get("top_keywords", [])
    paa          = seo.get("people_also_ask", [])

    itineraries_raw = tour.get('itineraries') or tour.get('itinerary') or ""
    # AA-608: strip routine meal-logistics codes (B/L/D, "Meals:" lines) from the source
    # BEFORE it goes into the prompt, so the writer never sees a meal token to carry across
    # (root cause of the 81% ITINERARY_MEAL_TIME_INVENTED rate). Per-day word counts below
    # are still computed from the ORIGINAL raw so day proportions aren't skewed by the strip.
    itineraries_for_prompt = strip_itinerary_meal_metadata(itineraries_raw)
    # AA-314: tour['highlights'] is a list (parsed by the caller) — join it the same
    # way seo_keywords/paa below turn a list into readable prompt text, instead of
    # interpolating the list's Python repr straight into the prompt.
    highlights_raw = tour.get('highlights') or []
    highlights_text = ', '.join(highlights_raw) if isinstance(highlights_raw, list) else highlights_raw

    # AA-353: per-day source word counts, computed in code — see parse_source_day_word_counts
    # docstring for why this can't be an LLM extraction step the way AA-352's itinerary_day is.
    _day_counts = parse_source_day_word_counts(itineraries_raw, tour.get('duration'))
    _fallback_note = (
        " (source had no clear per-day markers — estimated evenly; treat as a rough guide)"
        if _day_counts["used_fallback"] else ""
    )
    # AA-747: when S1_PER_DAY_TARGETS is on, give the writer an explicit per-day TARGET word count
    # (source day words × the clamp midpoint) alongside the source count, so it aims straight at the
    # band the AA-353 clamp checks instead of inferring it from the 0.7x-1.3x guidance. OFF by
    # default — the line is byte-identical to before (just "Day N: W words") when the flag is off.
    if per_day_targets and not _day_counts["used_fallback"]:
        _mid = _clamp_midpoint()
        per_day_length_text = "\n".join(
            f"Day {d}: {w} words (target ~{max(1, round(w * _mid))} words)"
            for d, w in sorted(_day_counts["day_word_counts"].items())
        ) + _fallback_note
    else:
        per_day_length_text = "\n".join(
            f"Day {d}: {w} words" for d, w in sorted(_day_counts["day_word_counts"].items())
        ) + _fallback_note

    return f"""Rewrite the following tour content for a master content catalog.
{few_shot_text}
TOUR DATA:
- Name: {tour.get('name')}
- Country: {tour.get('country')}
- Duration: {tour.get('duration')}
- Summary: {tour.get('summary')}
- Description: {tour.get('description')}
- Highlights: {highlights_text}
- Itineraries: {itineraries_for_prompt}
- Inclusions: {tour.get('inclusions')}
- Exclusions: {tour.get('exclusions')}

PER-DAY SOURCE LENGTH (word count of the source above, one per day — use as the basis for each
day's target length per rule 5; aim roughly 0.7x-1.3x of that day's number, not a fixed length
applied to every day):
{per_day_length_text}

SEO CONTEXT:
- Target keywords: {', '.join(seo_keywords[:5])}
- People also ask: {'; '.join(paa[:3])}

CRITICAL LENGTH REQUIREMENT — seo_meta: aim 145–152 characters, hard band 140–155, a complete
sentence ending with a period. Under 140 is rejected. Count characters before finalizing.

OUTPUT JSON FORMAT:
{{
  "name": "Rewrite in a clear, specific editorial voice — evocative but specific. See STRICT RULES 2.",
  "subtitle": "{_SUBTITLE_INSTRUCTIONS.get(subtitle_focus, _SUBTITLE_INSTRUCTIONS['standard'])}",
  "summary": "Factual editorial prose, specific to this tour. No generic openers. No sentence limit.",
  "highlights": [
    "Specific activity at Named Location (include altitude if trekking)",
    "Specific activity at Named Location",
    "Add as many highlights as the source supports — minimum 3, no maximum"
  ],
  "itineraries": [
    {{"day": 1, "title": "Names the place/activity, never generic (see rule 5)",
      "body": "This day's prose, length per PER-DAY SOURCE LENGTH above (rule 5). NEUTRAL
        third-person editorial voice — the SAME register as the summary. Every day must read as a
        factual catalogue record: route type, distance, terrain, transfers, key stops, included
        activities, accommodation context. The source day may be promotional (second person,
        mood/persona/hype) — do NOT echo it. Strip phrases like 'playlist-worthy', 'adventure
        family', 'coastal magic', 'pure bliss', 'genuine immersion', 'the senses take over', or any
        first-person ('our launchpad', 'fellow adventurers'). State what happens, where, and for how
        long — not how it feels. See NEUTRALIZE PROMOTIONAL SOURCE COPY in the system prompt."}},
    "... one object per source day, in day order, matching PER-DAY SOURCE LENGTH above exactly —
    do not add, merge, or drop days. The neutral register applies to EVERY day, not just the first."
  ],
  "seo_title": "SEO title — MUST be under 60 chars",
  "seo_meta": "SEO meta description — MUST be 140-155 characters (NEVER under 140), a complete
    sentence ending in a period, opening with a concrete editorial clause. GOOD example (145 chars):
    'This Sri Lanka journey covers Sigiriya, Kandy and Yala with unhurried pacing, expert local
    guides and comfortable transfers throughout the route.'",
  "trip_type": "cultural|adventure|wellness|culinary|wildlife|trekking|festival|river_journey"
}}"""
