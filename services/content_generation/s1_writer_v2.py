"""AA-645 / M2a S1-first step 2 (S248) — the S1 writer v2 ("prompt surgery") and the three A/B arms. Pure: prompt
strings, the code-built day plan and fact sheet, page assembly and the meta checks. No I/O, no LLM call: the offline
harness `scripts/aa645/s1_screen.py` makes the calls (through `LLMClient.generate()`, logged as `adhoc_aa645`).

Not wired into `graph.py`: step 4 of the plan (S252) wires the A/B winner, behind a flag, in shadow or review mode.
Until then nothing on the live path imports this module.

What the surgery removes from today's A1 writer (`prompts.SYSTEM_PROMPT` + `build_rewrite_prompt`), per the S246
content-quality advisory §5 step 2:

- per-day word targets, "0.7x-1.3x of the source", the 0.6-1.5 clamp and its nudge calls -> length follows the
  source; a thin day may be one sentence; no nudge.
- the `day_body` line asking every day for "route type, distance, terrain, transfers, key stops..." -> "what
  happens, in source order", plus a fact line built only from what the source states.
- seo_meta inside the writer JSON, rule 8 "expand it", the example meta promising guides and transfers -> a separate
  meta call, code-checked, one retry, never cut.
- raw DFS keywords + PAA questions in the writer prompt -> at most 3 keywords the source itself covers, meta only.
- the brand-differentiation / contrast block and the lessons append -> none: S1 is the neutral white-label master
  (house-style audit section 3).
- the raw source dump -> a code-built FACT SHEET: the source by day, figures, inclusions, conflicts, the "not in the
  source" list and the do-not-use list (`services.quality.s1_facts.build_fact_table`).

The three arms (q4-q8 §2.2): ``aplus`` = today's writer prompt byte for byte + a code-built DAYS block + the separate
meta call; ``w`` = the new writer + the meta call; ``e`` = a code-built short page where the model writes only the
name, subtitle, one hook sentence, highlights and one sentence per day, + the meta call. Each arm makes at most three
model calls per tour (writer, meta, one meta retry).
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from services.quality.s1_facts import build_fact_table, field_text, item_lines

ARMS = ("aplus", "w", "e")
VERSION = "s1-writer-v2/1"
WRITER_MAX_TOKENS = 16000   # Claude 5.5 models think adaptively and the thinking counts against max_tokens (S246)
META_MAX_TOKENS = 4000
META_TARGET = (130, 148)    # aim below the gate band 140–155 (lesson S233: LLMs overshoot a character target)
MAX_KEYWORDS = 3
MAX_FIGURES = 80

# ------------------------------------------------------------------ source preparation

AGENCY_FIELDS = (("group_size", "Group size"), ("best_time_to_go", "Best time to go"))


def with_agency_fields(tour: dict, row: Optional[dict] = None) -> dict:
    """`tour` (the S1 source dict) with the raw_tours agency fields the S1 writer never received (group size, best
    time to go) appended to `description` as labelled lines. Used for the writer, the fact table, the code checks and
    the panel packet alike, so a group size the page states is a source fact everywhere."""
    row = row or {}
    extra = [f"{label}: {str(row.get(key)).strip()}" for key, label in AGENCY_FIELDS
             if row.get(key) is not None and str(row.get(key)).strip()]
    if not extra:
        return dict(tour)
    desc = field_text(tour.get("description")).rstrip()
    return {**tour, "description": (desc + "\n\n" if desc else "") + "\n".join(extra)}


def source_day_blocks(itineraries: str) -> list[tuple[int, int, str]]:
    """(first day, last day, block text) per source "Day N" marker, line breaks KEPT (the fact lines of a day are
    lines) and the marker prefix stripped from the first line. Same markers as the day oracle
    (`day_parse.source_day_markers`); `prompts.parse_source_day_word_counts` joins a block into one line instead."""
    from services.content_generation.day_parse import _DAY_MARKER_RE, source_day_markers

    lines = (itineraries or "").splitlines()
    markers = source_day_markers(itineraries or "")
    out = []
    for i, (first, last, idx) in enumerate(markers):
        end = markers[i + 1][2] if i + 1 < len(markers) else len(lines)
        block = list(lines[idx:end])
        block[0] = _DAY_MARKER_RE.sub("", block[0].strip(), count=1)
        out.append((first, last, "\n".join(x.rstrip() for x in block).strip()))
    return out


def day_plan(tour: dict, facts: Optional[dict] = None) -> dict:
    """The day blocks the page must have, decided by code (the day-count oracle as an INPUT):

    * source "Day N" markers (≥ 2): one block per source day, a range ("Days 4–5") expanded to one block per day
      sharing the range's source text; a duration that disagrees with the markers is a source conflict (fact table
      `conflicts`) and the markers win — the writer cannot invent days;
    * a sub-day or one-day tour, or no markers and no usable duration: exactly one block (Day 1);
    * multi-day, no markers: one block per stated day; the source itinerary is given once, not split.

    Returns {"mode", "kind", "days": [{"day", "source", "range"}]} (`range` = (first, last) for a grouped day)."""
    facts = facts or build_fact_table(tour)
    kind = facts["duration"]["value"]
    itin = field_text(tour.get("itineraries"))
    if facts["day_source"] == "day_markers" and kind != "hours":
        days, seen = [], set()
        for first, last, text in source_day_blocks(itin):
            for n in range(first, last + 1):
                if n in seen:      # a source that repeats a day number: the oracle reports it, the plan keeps one
                    continue
                seen.add(n)
                days.append({"day": n, "source": text, "range": (first, last) if last > first else None})
        return {"mode": "source_days", "kind": kind, "days": sorted(days, key=lambda d: d["day"])}
    count = facts["duration"].get("day_count") if kind == "multi_day" else None
    if count and count > 1:
        return {"mode": "duration_days", "kind": kind,
                "days": [{"day": n, "source": "", "range": None} for n in range(1, count + 1)]}
    return {"mode": "single_day", "kind": kind, "days": [{"day": 1, "source": itin.strip(), "range": None}]}


def relevant_keywords(seo: Optional[dict], tour: dict, limit: int = MAX_KEYWORDS) -> list[str]:
    """DFS keywords the meta may use: not transactional, and every content word occurs in the source (the S233
    `keyword_covered` rule — a raw search query is not forced into a title). At most `limit`, in DFS order."""
    from services.acp_content_writing.quality_gates import keyword_covered
    from services.acp_content_writing.service import _TRANSACTIONAL_RE

    seo = seo or {}
    raw = (seo.get("keywords") or {}).get("top_keywords") or seo.get("top_keywords") or []
    source = "\n".join(field_text(tour.get(f)) for f in ("name", "summary", "description", "highlights",
                                                         "itineraries", "country", "duration"))
    out: list[str] = []
    for k in raw:
        kw = (k.get("keyword") if isinstance(k, dict) else k) or ""
        kw = str(kw).strip()
        if not kw or kw in out or _TRANSACTIONAL_RE.search(kw) or not keyword_covered(source, kw):
            continue
        out.append(kw)
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ the fact sheet (writer input)

def _bullets(values: list[str]) -> str:
    return "\n".join(f"- {v}" for v in values) if values else "- (none stated)"


def figures(facts: dict) -> list[str]:
    """The figures (number + unit / noun) the page may use, verbatim from the source, deduplicated."""
    out: list[str] = []
    for e in facts.get("numbers") or []:
        if e.get("role", "figure") != "figure":
            continue
        span = " ".join([e["value"], *(e.get("words") or [])[:2]]).strip() if not e.get("unit") else e["value"]
        if span not in out:
            out.append(span)
        if len(out) >= MAX_FIGURES:
            break
    return out


MISSING_LABELS = {"group_size": "group size", "minimum_age": "minimum age", "difficulty": "difficulty or fitness level",
                  "start_time": "start or pick-up time", "meals": "meals", "accommodation": "accommodation",
                  "transport": "transport", "exclusions": "what is not included", "price": "price"}


def ask_supplier(facts: dict) -> list[str]:
    """The "ask the supplier" list (advisory Q5): agency fields the source never states + source conflicts."""
    out = [f"{MISSING_LABELS.get(k, k)}: not stated in the source" for k in facts.get("missing") or []]
    out += [f"conflict: {c.get('detail') or c['value']}" for c in facts.get("conflicts") or []]
    return out


def render_fact_sheet(tour: dict, facts: dict, plan: dict) -> str:
    """The new writer's only input: the source organised by code. Every value is verbatim from the source."""
    days_line = ", ".join(f"Day {d['day']}" for d in plan["days"])
    groups = sorted({d["range"] for d in plan["days"] if d["range"]})
    group_note = "".join(f" Days {a}–{b} are one block in the source: write one short block per day that only"
                         f" restates what the source says for them." for a, b in groups)
    parts = [
        f"TOUR (source name): {field_text(tour.get('name')).strip()}",
        f"COUNTRY: {field_text(tour.get('country')).strip() or '(not stated)'}",
        f"DURATION (source, verbatim): {field_text(tour.get('duration')).strip() or '(not stated)'}",
        "",
        f"DAYS TO WRITE (checked by code): {days_line}.{group_note}",
    ]
    if plan["mode"] == "source_days":
        parts.append("\nSOURCE BY DAY (verbatim):")
        shown = set()
        for d in plan["days"]:
            key = d["range"] or (d["day"], d["day"])
            if key in shown:
                continue
            shown.add(key)
            label = f"Days {key[0]}–{key[1]}" if d["range"] else f"Day {d['day']}"
            parts.append(f"[{label}] {d['source'].strip() or '(no text in the source)'}")
    else:
        note = ("the source has no day markers; it is one day" if plan["mode"] == "single_day"
                else "the source has no day markers; follow its order and do not assign a place to a day unless the"
                     " source makes the day plain")
        itin_text = field_text(tour.get("itineraries")).strip() or "(none)"
        parts.append(f"\nSOURCE ITINERARY (verbatim; {note}):\n{itin_text}")
    hl = tour.get("highlights")
    hl_text = "\n".join(f"- {h}" for h in hl) if isinstance(hl, list) else field_text(hl)
    parts += [
        "\nSOURCE OVERVIEW (verbatim):",
        f"Summary: {field_text(tour.get('summary')).strip() or '(none)'}",
        f"Description: {field_text(tour.get('description')).strip() or '(none)'}",
        f"Highlights:\n{hl_text.strip() or '(none)'}",
        "\nINCLUDED (source):\n" + _bullets([e["value"] for e in facts.get("included") or []]),
        "\nNOT INCLUDED (source):\n" + _bullets([e["value"] for e in facts.get("excluded") or []]),
        "\nOPTIONAL OR CONDITIONAL (only ever as optional / conditional):\n"
        + _bullets([e["value"] for e in facts.get("optional") or []]),
        "\nFIGURES (the only numbers you may use, with the same unit and the same thing they count):\n"
        + _bullets(figures(facts)),
        "\nNOT IN THE SOURCE (never state or guess these):\n"
        + _bullets([MISSING_LABELS.get(k, k) for k in facts.get("missing") or []]),
        "\nCONFLICTS IN THE SOURCE (do not resolve them; leave the point out):\n"
        + _bullets([c.get("detail") or c["value"] for c in facts.get("conflicts") or []]),
        "\nDO NOT USE (never repeat):\n" + _bullets(sorted({e["value"] for e in facts.get("do_not_use") or []})),
    ]
    return "\n".join(parts)


# ------------------------------------------------------------------ prompts: W (new writer)

_BACKGROUND = {
    "allow": ("You may add one short clause of well-known background about a place the fact sheet names (what it"
              " is, its era or setting), never presented as part of this tour."),
    "forbid": "Do not add background about places: every name and statement comes from the fact sheet.",
}


def _forbidden_line(words: list[str]) -> str:
    return ("Never use these words or phrases in any field (any capitalisation): " + ", ".join(words) + "."
            if words else "")


def w_system_prompt(forbidden: list[str], background: str = "allow") -> str:
    return "\n".join(filter(None, [
        "You write the master page of one tour for a white-label tour catalogue. Travel agencies resell the page",
        "unchanged, so it must be accurate, neutral and free of any brand.",
        "",
        "SOURCE OF TRUTH: the FACT SHEET in the user message, built from the supplier's text. Nothing else.",
        "",
        "RULES",
        "1. Facts only from the fact sheet. Every activity, service, meal, transport mode, time, distance, altitude,"
        " view, wildlife sighting, lodging feature and inclusion you state must be in the fact sheet for THIS tour. "
        + _BACKGROUND.get(background, _BACKGROUND["allow"]),
        "2. Numbers: only the figures under FIGURES, with the same unit and attached to the same thing. Never"
        " convert, round, add up or estimate.",
        "3. NOT IN THE SOURCE and CONFLICTS IN THE SOURCE: never state or guess them; for a conflict, leave the point"
        " out.",
        "4. DO NOT USE: never repeat those items, never name the operator, the supplier or Adventure Asia, no web"
        " addresses, no calls to action (book, join, contact).",
        "5. Length follows the source. A day with little source text gets one or two sentences. Never pad, never"
        " expand, never write to a length. A short accurate page beats a long one.",
        "6. Days: write exactly the days under DAYS TO WRITE, in that order, one object each. Never merge, split, add"
        " or drop a day. The title names that day's place(s) or main activity from the source, in sentence case"
        " (e.g. \"Kathmandu to Mugling on the Trishuli\"), never only \"Arrival\", \"Departure\" or \"Free day\".",
        "7. Day body: what happens, in the source's order. When that day's source states any of distance, walking or"
        " driving time, altitude, overnight place or meals, end the body with one fact line holding only those, e.g."
        " \"Distance: 42 km. Overnight: Phakding. Meals: breakfast, lunch.\" Leave out what the source does not state;"
        " leave the line out when it states none.",
        "8. Included and not included: keep the source's meaning; never upgrade (\"meals as per itinerary\" is not"
        " \"all meals\").",
        "9. Voice: calm, factual, third person, present tense. No hype, no superlative the source does not make, no"
        " abstract filler (\"unhurried\", \"rewards attention\", \"a different register\"), no second person.",
        _forbidden_line(forbidden),
        "",
        "Output ONLY valid JSON. No preamble, no markdown.",
    ]))


_W_OUTPUT = """OUTPUT JSON:
{
  "name": "tour name: destination + format, specific, no hype (e.g. \\"Trishuli River Rafting — 1 Day\\")",
  "subtitle": "one sentence of facts from the sheet: route, duration or the defining activity",
  "summary": "one paragraph, at most 120 words: duration, main activity, start and end only when the sheet"""\
    """ states them, then up to 3 named stops",
  "highlights": ["up to 6 concrete items from the sheet (fewer when it has fewer), each a place or an activity"],
  "itineraries": [{"day": 1, "title": "...", "body": "..."}, "... one object per day under DAYS TO WRITE"],
  "trip_type": "cultural|adventure|wellness|culinary|wildlife|trekking|festival|river_journey"
}"""


def _examples_block(examples: Optional[list[dict]]) -> str:
    if not examples:
        return ""
    rows = []
    for i, ex in enumerate(examples[:5], 1):
        rows.append(f"EXAMPLE {i} (a different tour; copy its shape and restraint, never its facts):\n"
                    + json.dumps(ex.get("page") or ex, ensure_ascii=False))
    return "\n\nREFERENCE EXAMPLES\n" + "\n\n".join(rows) + "\n"


def w_user_prompt(tour: dict, facts: dict, plan: dict, examples: Optional[list[dict]] = None) -> str:
    return ("Write this tour's master page from the fact sheet." + _examples_block(examples)
            + "\n\nFACT SHEET\n" + render_fact_sheet(tour, facts, plan) + "\n\n" + _W_OUTPUT)


# ------------------------------------------------------------------ prompts: E (short page; code builds the frame)

def e_system_prompt(forbidden: list[str], background: str = "allow") -> str:
    return "\n".join(filter(None, [
        "You write a few short lines for the master page of one tour in a white-label tour catalogue. Code builds"
        " the rest of the page (the facts box, inclusions and the day fact lines) from the supplier's text.",
        "",
        "RULES",
        "1. Only facts from the FACT SHEET in the user message. "
        + _BACKGROUND.get(background, _BACKGROUND["allow"]),
        "2. Numbers only from FIGURES; never state what is under NOT IN THE SOURCE or CONFLICTS; never repeat DO NOT"
        " USE, the operator, the supplier or Adventure Asia; no calls to action.",
        "3. One sentence per day, at most 40 words, saying what happens that day per the source. Day titles name the"
        " day's place(s) or main activity, sentence case. Exactly the days under DAYS TO WRITE.",
        "4. Calm, factual, third person, present tense; no hype, no filler.",
        _forbidden_line(forbidden),
        "",
        "Output ONLY valid JSON. No preamble, no markdown.",
    ]))


_E_OUTPUT = """OUTPUT JSON:
{
  "name": "tour name: destination + format, specific, no hype",
  "subtitle": "one sentence of facts from the sheet",
  "hook": "one sentence, at most 30 words, on the tour's main draw as the source states it",
  "highlights": ["up to 5 concrete items from the sheet (fewer when the sheet has fewer)"],
  "days": [{"day": 1, "title": "...", "sentence": "..."}, "... one object per day under DAYS TO WRITE"],
  "trip_type": "cultural|adventure|wellness|culinary|wildlife|trekking|festival|river_journey"
}"""


def e_user_prompt(tour: dict, facts: dict, plan: dict, examples: Optional[list[dict]] = None) -> str:
    return ("Write the short lines for this tour from the fact sheet." + _examples_block(examples)
            + "\n\nFACT SHEET\n" + render_fact_sheet(tour, facts, plan) + "\n\n" + _E_OUTPUT)


# Fact lines code copies from a source day, verbatim (house-style appendix: "End with a fact line built only from
# source fields"). A line is kept whole; nothing is computed.
_FACT_LINE_RE = re.compile(r"^\s*[-•*]?\s*(?:meals?|overnight|accommodation|hotel|lodge|distance|driving|drive|"
                           r"walking|walk|trekking|trek|hiking|cycling|biking|ride|altitude|elevation|ascent|descent|"
                           r"level|grade)\b[^:\n]{0,30}:\s*\S.*$", re.I)
_MEAL_CODE_RE = re.compile(r"\(\s*((?:B|L|D|BB|HB|FB)(?:\s*[,/&]\s*(?:B|L|D)){0,2})\s*\)\s*$", re.I)


def source_fact_lines(day_source: str) -> list[str]:
    """Verbatim fact lines of one source day ("Overnight: Phakding", "Meals: B, L"), plus a trailing meal code
    "(B, L, D)" as "Meals: B, L, D". Order kept, duplicates dropped."""
    out: list[str] = []
    for line in (day_source or "").splitlines():
        s = line.strip()
        if _FACT_LINE_RE.match(s) and len(s) <= 200:
            s = re.sub(r"^[-•*]\s*", "", s)
            if s not in out:
                out.append(s)
        m = _MEAL_CODE_RE.search(s)
        if m:
            meal = "Meals: " + re.sub(r"\s*[,/&]\s*", ", ", m.group(1).upper())
            if meal not in out:
                out.append(meal)
    return out


_GLANCE_MAX_LINE = 80


def _single_kind(facts: dict, groups: set[str]) -> list[str]:
    """Included lines that are only about `groups` (a meal line, an accommodation line) and say so in their first
    three words, short enough for a facts box: "All meals (Breakfast on Day 01)" yes; "Rafting trip with lunch on
    the river bank" or "guide and crew with all the camping gear" no."""
    from services.quality.s1_facts import item_groups

    return [e["value"] for e in facts.get("included") or []
            if e.get("groups") and set(e["groups"]) <= groups and len(e["value"]) <= _GLANCE_MAX_LINE
            and set(item_groups(" ".join(e["value"].split()[:3]))) & groups]


def at_a_glance(tour: dict, facts: dict) -> list[str]:
    """The facts box of arm E: `Label: value` lines from source fields only; a field the source lacks is left out
    (it is on the ask-the-supplier list instead)."""
    lines = []
    dur = " · ".join(x.strip() for x in field_text(tour.get("duration")).splitlines() if x.strip())
    if dur:
        lines.append(f"Duration: {dur}")
    country = field_text(tour.get("country")).strip()
    if country:
        lines.append(f"Destination: {country}")
    for line in field_text(tour.get("description")).splitlines():   # with_agency_fields lines
        for _key, label in AGENCY_FIELDS:
            if line.startswith(label + ": "):
                lines.append(line.strip())
    acc = _single_kind(facts, {"accommodation"})
    if acc:
        lines.append("Accommodation: " + "; ".join(acc[:2]))
    meals = _single_kind(facts, {"breakfast", "lunch", "dinner", "meals"})
    if meals:
        lines.append("Meals: " + "; ".join(meals[:2]))
    return lines


def drop_do_not_use(lines: list[str], facts: dict) -> list[str]:
    """The verbatim source lines arm E copies, minus any that carries a DO NOT USE item (operator name, URL,
    e-mail, phone): code copies them unseen, so code must keep the supplier out."""
    bad = [fold_text(e["value"]) for e in facts.get("do_not_use") or [] if str(e.get("value") or "").strip()]
    return [ln for ln in lines if not any(b and b in fold_text(ln) for b in bad)]


def fold_text(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def assemble_e_page(tour: dict, facts: dict, plan: dict, llm: dict) -> dict:
    """Arm E's page: model lines + code-built facts box, day fact lines and inclusion lists. `Day N —` headings are
    kept (TripPlanner `parse_days` splits on them)."""
    by_day = {}
    for d in llm.get("days") or []:
        if isinstance(d, dict) and str(d.get("day", "")).isdigit():
            by_day.setdefault(int(d["day"]), d)
    blocks = []
    for d in plan["days"]:
        got = by_day.get(d["day"], {})
        title = str(got.get("title") or "").strip() or f"Day {d['day']}"
        body = [str(got.get("sentence") or "").strip()]
        body += drop_do_not_use(source_fact_lines(d["source"]), facts) if plan["mode"] == "source_days" else []
        blocks.append({"day": d["day"], "title": title, "body": "\n".join(b for b in body if b)})
    glance = drop_do_not_use(at_a_glance(tour, facts), facts)
    summary = str(llm.get("hook") or "").strip()
    if glance:
        summary = (summary + "\n\n" if summary else "") + "At a glance\n" + "\n".join(f"- {g}" for g in glance)
    inc = drop_do_not_use(item_lines(field_text(tour.get("inclusions"))), facts)
    exc = drop_do_not_use(item_lines(field_text(tour.get("exclusions"))), facts)
    desc = []
    if inc:
        desc.append("Included:\n" + "\n".join(f"- {v}" for v in inc))
    if exc:
        desc.append("Not included:\n" + "\n".join(f"- {v}" for v in exc))
    return {"name": llm.get("name"), "subtitle": llm.get("subtitle"), "summary": summary,
            "description": "\n\n".join(desc), "highlights": as_list(llm.get("highlights")),
            "itineraries": serialize_days(blocks), "trip_type": llm.get("trip_type")}


# ------------------------------------------------------------------ A+ (today's writer + the DAYS block)

def aplus_days_block(plan: dict) -> str:
    """Appended to today's writer user prompt for arm A+: the day structure decided by code."""
    days = ", ".join(str(d["day"]) for d in plan["days"])
    one = (" This tour is shorter than a day or exactly one day: write exactly one itinerary object (day 1)."
           if plan["mode"] == "single_day" else "")
    return (f"\n\nDAY STRUCTURE (checked by code; overrides any other day count above): write exactly these"
            f" itinerary objects, one per day, in this order: day {days}.{one}")


# ------------------------------------------------------------------ page assembly (W, A+)

def as_list(value) -> list:
    """A model's list field as a list: a bare string is one item (list("abc") would be its characters)."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def serialize_days(items: list) -> str:
    """The canonical "Day N — Title\\nBody" string from the writer's itineraries array, in the array's order and
    with duplicates kept (the day oracle must see a duplicate; serialize_itinerary_days keys by day and hides it)."""
    out = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        day = str(it.get("day", "")).strip()
        title = str(it.get("title") or "").strip()
        body = str(it.get("body") or "").strip()
        out.append(f"Day {day} — {title}\n{body}".strip())
    return "\n\n".join(out)


def assemble_page(writer: dict) -> dict:
    """W / A+ writer JSON → the generated_content page fields (no seo yet; the meta call adds them)."""
    itin = writer.get("itineraries")
    return {"name": writer.get("name"), "subtitle": writer.get("subtitle"), "summary": writer.get("summary"),
            "description": writer.get("description") or "", "highlights": as_list(writer.get("highlights")),
            "itineraries": serialize_days(itin) if isinstance(itin, list) else field_text(itin),
            "trip_type": writer.get("trip_type")}


def parse_json(raw: str) -> Optional[dict]:
    """A model reply as a dict: markdown fence stripped, then json.loads, then the json-repair salvage. Unlike
    graph.parse_generated_json it does not require a "name" key (the meta reply has none)."""
    from json_repair import repair_json

    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = repair_json(text, return_objects=True)
    return value if isinstance(value, dict) and value else None


# ------------------------------------------------------------------ the meta call

def meta_system_prompt(forbidden: list[str]) -> str:
    from services.content_generation.seo_meta_utils import SEO_META_FORBIDDEN
    lo, hi = META_TARGET
    return "\n".join(filter(None, [
        "You write the SEO title and the meta description for one tour page of a white-label tour catalogue.",
        "",
        "RULES",
        "1. seo_title: at most 60 characters; the place and the activity or duration; no brand, no hype.",
        f"2. seo_meta: one complete sentence ending in a period, {lo}–{hi} characters (count them). Facts only from"
        " the PAGE and FIGURES given: never a service, inclusion, view, guide, transfer or comfort the page does not"
        " state. Name the duration and the main places or activity.",
        "3. Use a KEYWORD only if it fits the sentence naturally; never force one in, never more than one.",
        "4. Never use: " + ", ".join(SEO_META_FORBIDDEN) + ", a call to action, Adventure Asia or any operator name.",
        _forbidden_line(forbidden),
        "",
        'Output ONLY valid JSON: {"seo_title": "...", "seo_meta": "..."}',
    ]))


def meta_user_prompt(page: dict, tour: dict, facts: dict, keywords: list[str]) -> str:
    return "\n".join([
        "PAGE",
        f"Name: {field_text(page.get('name'))}",
        f"Subtitle: {field_text(page.get('subtitle'))}",
        f"Summary: {field_text(page.get('summary'))[:1500]}",
        f"Duration (source): {field_text(tour.get('duration')).strip() or '(not stated)'}",
        f"Country: {field_text(tour.get('country')).strip() or '(not stated)'}",
        "FIGURES (only numbers you may use):\n" + _bullets(figures(facts)[:30]),
        "KEYWORDS (optional):\n" + _bullets(keywords),
    ])


def meta_retry_prompt(previous: Optional[dict], problems: list[str]) -> str:
    if not previous:
        return ("\n\nYOUR PREVIOUS ANSWER was not usable (" + "; ".join(problems) + "). Answer again with only the"
                ' JSON object {"seo_title": "...", "seo_meta": "..."}.')
    meta = field_text(previous.get("seo_meta"))
    return (f"\n\nYOUR PREVIOUS ANSWER: {json.dumps(previous, ensure_ascii=False)}\n"
            f"Its seo_meta has {len(meta)} characters. Fix these problems and answer again with the full JSON:\n"
            + "\n".join(f"- {p}" for p in problems))


def meta_problems(meta_json: Optional[dict], tour: dict, facts: dict, forbidden: list[str]) -> list[str]:
    """Code checks on the meta call's answer (the step-1 checks + the validate limits). [] = accept."""
    from services.content_generation.forbidden_words import has_word
    from services.content_generation.seo_meta_utils import (SEO_META_FORBIDDEN, SEO_META_MAX, SEO_META_MIN,
                                                            SEO_TITLE_MAX, meta_has_forbidden)
    from services.quality.s1_checks import check_meta, check_numbers

    if not meta_json:
        return ["the answer is not the JSON object asked for"]
    title = field_text(meta_json.get("seo_title")).strip()
    meta = field_text(meta_json.get("seo_meta")).strip()
    out = []
    if not title:
        out.append("seo_title is empty")
    elif len(title) > SEO_TITLE_MAX:
        out.append(f"seo_title has {len(title)} characters; at most {SEO_TITLE_MAX}")
    if not meta:
        return out + ["seo_meta is empty"]
    for f in check_meta({"seo_meta": meta}):
        out.append(f"seo_meta {f['detail']}" if f["code"] == "META_CUT"
                   else f"seo_meta has {len(meta)} characters; it must be {SEO_META_MIN}–{SEO_META_MAX}")
    if meta_has_forbidden(meta, SEO_META_FORBIDDEN):
        out.append("seo_meta uses budget language (" + ", ".join(SEO_META_FORBIDDEN) + ")")
    hits = sorted({w for w in forbidden if has_word(meta, w) or has_word(title, w)})
    if hits:
        out.append("forbidden words: " + ", ".join(hits))
    names = sorted({e["value"] for e in facts.get("do_not_use") or [] if str(e.get("value") or "").strip()
                    and fold_text(e["value"]) in fold_text(title + " " + meta)}
                   | ({"Adventure Asia"} if "adventure asia" in fold_text(title + " " + meta) else set()))
    if names:
        out.append("names the operator, a brand or a contact (white-label page): " + ", ".join(names))
    for f in check_numbers(tour, {"seo_title": title, "seo_meta": meta}, facts):
        out.append(f"a number not in the source: {f['detail']}")
    return out


def finalize_meta(page: dict, meta_json: Optional[dict]) -> dict:
    """The page with seo_title / seo_meta from the meta call, exactly as written (never cut)."""
    meta_json = meta_json or {}
    return {**page, "seo_title": field_text(meta_json.get("seo_title")).strip(),
            "seo_meta": field_text(meta_json.get("seo_meta")).strip()}


def arm_prompts(arm: str, tour: dict, facts: dict, plan: dict, *, forbidden: list[str],
                background: str = "allow", examples: Optional[list[dict]] = None,
                aplus_state: Optional[dict[str, Any]] = None) -> tuple[str, str]:
    """(system, user) of an arm's writer call. `aplus_state` is the graph-state dict today's builders read
    (tour, seo, brand_* fields); A+ uses them byte for byte and appends the DAYS block."""
    if arm == "w":
        return w_system_prompt(forbidden, background), w_user_prompt(tour, facts, plan, examples)
    if arm == "e":
        return e_system_prompt(forbidden, background), e_user_prompt(tour, facts, plan, examples)
    if arm == "aplus":
        from services.content_generation.batch_prompt import build_s1_system_prompt, build_s1_user_prompt
        state = {"tour": tour, "seo": {}, "few_shots": [], **(aplus_state or {})}
        return build_s1_system_prompt(state), build_s1_user_prompt(state) + aplus_days_block(plan)
    raise ValueError(f"unknown arm {arm!r}; one of {ARMS}")


def page_from_writer(arm: str, tour: dict, facts: dict, plan: dict, writer: dict) -> dict:
    """The page (no seo yet) from an arm's parsed writer reply."""
    if arm == "e":
        return assemble_e_page(tour, facts, plan, writer)
    return assemble_page(writer)
