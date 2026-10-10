"""AA-691 (A1-1) — grounding of S1 master content against the raw source tour.

A1 had no check that the rewrite is supported by the source (apart from brand_audit's meal/time
regex), and T3 only compares a tenant rewrite with master content, so a fact invented here reached
every tenant. Two signals, per sentence of the narrative fields:

1. **Deterministic** — `find_novel_numeric_claims` (the check T3 already uses): a number the source
   never states → `UNSUPPORTED_NUMBER`, unless Jev confidently says the sentence is supported
   (a unit conversion or a sum is not a fabrication).
2. **Jev** — the Noul `a1_claim_supported` per sentence. A confident no (reject zone, enforce
   mode only) → `UNSUPPORTED_CLAIM`. Shadow/grey verdicts only log (and show as soft notes).

Violations are quoted to flag_fix with the source; revalidate re-checks them after the repair and
sends anything still unsupported to manual_check (ADR 0007: block unpublished content, never delete).
Only A1 runs this — a T2 tenant rewrite is grounded against master content by T3.

AA-756 (grounding token trim): `a1_claim_supported` was 80% of Jev spend because every sentence was
sent with the WHOLE tour source (avg 2,408 input tokens). An itinerary sentence is now asked against
a per-unit source (`unit_source`) — a compact header + its own day's source + the neighbouring days
+ inclusions/exclusions — not the whole tour (measured −62% source chars on 50 real India tours).
Every unit with ≥ MIN_WORDS words is still asked: a keyword "skip" rule missed exactly the tour
promises the policy forbids (background about a named place is allowed, a promise the source never
states is not), so there is no skip. The numeric deterministic check still runs against the FULL
source (unchanged).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any, Optional

import structlog

from services.acp_shared.grounding import find_novel_numeric_claims

logger = structlog.get_logger()

STAGE = "s1_grounding"
QUESTION = "a1_claim_supported"
GROUNDED_FIELDS = ("subtitle", "summary", "highlights", "itineraries")
SOURCE_FIELDS = ("name", "subtitle", "summary", "description", "highlights", "itineraries",
                 "country", "duration", "price", "inclusions", "exclusions")
MIN_WORDS = 4                 # shorter units ("Day 3 — Paro") carry no checkable claim
CONCURRENCY = 4
POOL_SIZE = 2                 # _decide holds one connection at a time; 2 is plenty for 4 in flight

# Same sentence boundary T3 uses (tenant_pipeline._SENT_SPLIT_RE).
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'‘’“”])")
_DAY_TITLE_RE = re.compile(r"^\s*Day\s+\d+\s*[—–-]")
# Day number off a generated itinerary title line ("Day 3 — Paro to Thimphu") — maps a rewritten
# day to its source day for unit_source. Only the leading "Day N" of a title line is read.
_GEN_DAY_NUM_RE = re.compile(r"^\s*Day\s+(\d+)\b")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return "\n".join(_as_text(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_as_text(v)}" for k, v in value.items())
    return str(value)


_GLUED_RE = re.compile(r"(?<=[A-Za-z])(?=\d)")
_DURATION_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours)\b\.?(?:\s*(\d{1,2})\s*(?:m|min|mins|minutes)\b)?",
    re.IGNORECASE)
_MINUTES_RE = re.compile(r"(?<![\w.])(\d+)\s*(?:m|min|mins|minutes)\b", re.IGNORECASE)
_CLOCK_RE = re.compile(r"(?<![\w.])(\d{1,2})[.:](\d{2})(?!\d)")


def _fmt(x: float) -> str:
    return f"{x:g}"


def source_number_parts(tour: dict) -> list[str]:
    """What the numeric check compares against: the raw source fields, with numbers glued to letters
    split off ("3h260km" → "3h 260km", which the check's regex otherwise cannot see), plus the
    figures a faithful rewrite derives from them — a travel time in other units ("1h30m" → 90
    minutes / 1.5 hours; "90 min" → 1.5 hours) and a clock time's parts ("12.30" → 12, 30).
    Measured in the S204 calibration: these formats were 5 of the 7 false numeric hits in 60."""
    parts = [_GLUED_RE.sub(" ", _as_text(tour.get(f))) for f in SOURCE_FIELDS]
    text = "\n".join(parts)
    derived: set[str] = set()
    for m in _DURATION_RE.finditer(text):
        hours = float(m.group(1)) + (int(m.group(2)) / 60 if m.group(2) else 0)
        derived.update({_fmt(hours), _fmt(hours * 60)})
    for m in _MINUTES_RE.finditer(text):
        minutes = int(m.group(1))
        derived.add(_fmt(minutes / 60))
    for m in _CLOCK_RE.finditer(text):
        derived.update({str(int(m.group(1))), m.group(2)})
    return parts + [" ".join(sorted(derived))]


def source_text(tour: dict) -> str:
    """The raw source the writer was given, as one labelled text (what Jev reads)."""
    parts = []
    for f in SOURCE_FIELDS:
        text = _as_text(tour.get(f)).strip()
        if text:
            parts.append(f"{f.upper()}:\n{text}")
    return "\n\n".join(parts)


def sentence_units(generated: dict) -> list[dict]:
    """Checkable units of the narrative fields: {field, sentence, day}. Highlights are one unit per
    item; itinerary day-title lines ("Day 3 — Paro to Thimphu") are skipped, their bodies are split
    and each sentence carries the ``day`` number of the title it falls under (None for
    subtitle/summary/highlights and for itinerary text before any day title). The day number lets
    ``unit_source`` send only that day's source (AA-756)."""
    units: list[dict] = []
    for field in GROUNDED_FIELDS:
        value = generated.get(field)
        if not value:
            continue
        if field == "highlights" and isinstance(value, list):
            for v in value:
                s = str(v).strip()
                if len(s.split()) >= MIN_WORDS:
                    units.append({"field": field, "sentence": s, "day": None})
            continue
        if field == "itineraries":
            day: Optional[int] = None
            for ln in _as_text(value).splitlines():
                if not ln.strip():
                    continue
                m = _GEN_DAY_NUM_RE.match(ln)
                if m:                                   # a "Day N" title line sets the current day
                    day = int(m.group(1))
                    if _DAY_TITLE_RE.match(ln):         # a title line itself carries no claim
                        continue
                for c in _SENT_SPLIT_RE.split(ln):
                    s = c.strip()
                    if len(s.split()) >= MIN_WORDS:
                        units.append({"field": field, "sentence": s, "day": day})
            continue
        for c in _SENT_SPLIT_RE.split(_as_text(value)):
            s = c.strip()
            if len(s.split()) >= MIN_WORDS:
                units.append({"field": field, "sentence": s, "day": None})
    return units


def _h(text: str, n: int) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:n]


def subject_key(source: str, sentence: str) -> str:
    """Stable per (source, sentence): an unchanged sentence re-checked after flag_fix hits the
    Verdict cache instead of paying again."""
    return f"claim:{_h(source, 10)}:{_h(sentence, 12)}"


_HEADER_FIELDS = (("name", "NAME"), ("country", "COUNTRY"), ("duration", "DURATION"))


def _tour_header(tour: dict) -> str:
    """Compact header for a per-unit source: NAME / COUNTRY / DURATION only."""
    parts = []
    for key, label in _HEADER_FIELDS:
        text = _as_text(tour.get(key)).strip()
        if text:
            parts.append(f"{label}: {text}")
    return "\n".join(parts)


def _source_days(tour: dict) -> tuple[dict[int, str], bool]:
    """Source itinerary split by day, reusing prompts.parse_source_day_word_counts (the one day
    splitter — never a second one). Returns ({day_num: text}, used_fallback)."""
    from .prompts import parse_source_day_word_counts

    parsed = parse_source_day_word_counts(_as_text(tour.get("itineraries")), tour.get("duration") or "")
    return parsed["day_text"], parsed["used_fallback"]


def unit_source(tour: dict, unit: dict) -> str:
    """AA-756: the source a single grounding unit is judged against, instead of the whole tour.

    For an ``itineraries`` sentence with a known day number, send a compact tour header
    (NAME / COUNTRY / DURATION), the source SUMMARY (S224: writers lift tour-wide details from it;
    a temple's altitude stated only there was flagged as unsupported), that day's own source text,
    the previous and next day's source text (a writer often moves a detail one day), and
    INCLUSIONS / EXCLUSIONS (meals, transport, services live there). If the source days cannot be
    split (parser fallback / even split) or the unit has no day number, fall back to the full
    ``source_text(tour)`` — never guess a slice.
    subtitle / summary / highlights always keep the full source."""
    if unit.get("field") != "itineraries" or unit.get("day") is None:
        return source_text(tour)
    day_text, used_fallback = _source_days(tour)
    day = unit["day"]
    if used_fallback or day not in day_text:
        return source_text(tour)
    parts = []
    header = _tour_header(tour)
    if header:
        parts.append(header)
    summary = _as_text(tour.get("summary")).strip()
    if summary:
        parts.append(f"SUMMARY:\n{summary}")
    for d in (day - 1, day, day + 1):
        text = (day_text.get(d) or "").strip()
        if text:
            parts.append(f"DAY {d}:\n{text}")
    for f in ("inclusions", "exclusions"):
        text = _as_text(tour.get(f)).strip()
        if text:
            parts.append(f"{f.upper()}:\n{text}")
    return "\n\n".join(parts)


def classify(units: list[dict], numeric: dict[int, list[str]], verdicts: dict[int, Any]) -> dict:
    """Merge the two signals. `numeric[i]` = novel numbers of unit i; `verdicts[i]` = its Jev Verdict
    (or None when Jev was not asked). Returns {violations, notes}.

    - novel number and Jev not confidently "supported" → UNSUPPORTED_NUMBER (the deterministic hit
      stands in shadow/grey/error: fail-closed on numbers, as T3 does today);
    - Jev confidently "not supported" (enforced reject) → UNSUPPORTED_CLAIM;
    - a logged-only low probability (shadow or grey) → a soft note for the reviewer."""
    violations, notes = [], []
    for i, u in enumerate(units):
        v = verdicts.get(i)
        p = getattr(v, "probability", None)
        enforced = bool(v is not None and getattr(v, "enforced", False))
        zone = getattr(v, "zone", None)
        base = {"field": u["field"], "sentence": u["sentence"], "p": p}
        if numeric.get(i) and not (enforced and zone == "accept"):
            violations.append({**base, "code": "UNSUPPORTED_NUMBER", "novel_numbers": numeric[i]})
        elif enforced and zone == "reject":
            violations.append({**base, "code": "UNSUPPORTED_CLAIM"})
        elif v is not None and zone in ("reject", "grey") and p is not None and p < 0.5:
            notes.append({**base, "zone": zone, "mode": getattr(v, "mode", None)})
    return {"violations": violations, "notes": notes}


def issue_lines(violations: list[dict]) -> list[str]:
    """The feedback flag_fix reads: each violation quoted with what to do."""
    out = []
    for v in violations:
        if v["code"] == "UNSUPPORTED_NUMBER":
            what = f"states {', '.join(v.get('novel_numbers') or [])}, which the source never gives"
        else:
            what = "states a fact the source does not give"
        out.append(f"{v['code']} in {v['field']}: {json.dumps(v['sentence'])} {what}. "
                   f"Rewrite it to say only what the SOURCE says, or remove it.")
    return out


async def _judge_all(units: list[dict], sources: dict[int, str]) -> dict[int, Any]:
    """One Jev Noul per asked unit, CONCURRENCY in flight, over a small pool opened for this tour.
    ``sources[i]`` is the per-unit source for unit ``i`` (AA-756); a unit absent from ``sources``
    was skipped and is not asked. The subject_key uses that same per-unit source, so a verdict
    caches per unit."""
    import asyncpg
    from shared.llm_client.decide import decide
    from shared.secrets import get_database_url

    pool = await asyncpg.create_pool(get_database_url(), ssl="require", min_size=1, max_size=POOL_SIZE)
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(i: int, u: dict, src: str):
        async with sem:
            d = await decide(STAGE, subject_key(src, u["sentence"]),
                             {"source": src, "sentence": u["sentence"]}, [QUESTION], pool=pool)
            return i, d.verdicts.get(QUESTION)

    try:
        return dict(await asyncio.gather(*[one(i, units[i], src) for i, src in sources.items()]))
    finally:
        await pool.close()


def judge_units(units: list[dict], sources: dict[int, str]) -> dict[int, Any]:
    """Sync entry for the S1 graph node (runs in LangGraph's worker thread). ``sources`` maps the
    index of each unit that should be asked to its per-unit source (AA-756). Fails open: on any
    error, or when called from a thread that already runs a loop, returns {} (no Jev signal) and the
    deterministic check still applies."""
    if not sources:
        return {}
    try:
        asyncio.get_running_loop()
        logger.warning("grounding_judge_on_event_loop")
        return {}
    except RuntimeError:
        pass
    try:
        return asyncio.run(_judge_all(units, sources))
    except Exception as exc:
        logger.warning("grounding_judge_failed", error=str(exc)[:200])
        return {}


def check_grounding(generated: dict, tour: dict, *, use_jev: bool = True) -> dict:
    """Run both signals over `generated`. Returns {violations, notes, units, jev_asked}. The numeric
    check stays on the FULL source (unchanged); Jev is asked per unit against its per-unit source
    (`unit_source`) — every unit with ≥ MIN_WORDS words is asked (AA-756: no skip rule; a keyword
    skip missed exactly the tour promises the policy forbids)."""
    tour = tour or {}
    units = sentence_units(generated or {})
    source_parts = source_number_parts(tour)
    numeric = {i: n for i, u in enumerate(units) if (n := find_novel_numeric_claims(u["sentence"], source_parts))}
    if use_jev and source_text(tour):
        sources = {i: unit_source(tour, u) for i, u in enumerate(units)}
        verdicts = judge_units(units, sources)
    else:
        verdicts = {}
    out = classify(units, numeric, verdicts)
    out["units"] = len(units)
    out["jev_asked"] = len(verdicts)
    return out


REPAIR_SYSTEM = """You are Adventure Asia's fact editor. A writer rewrote a tour from a SOURCE and added
details the source does not give. Rewrite each numbered sentence so it keeps its place in the text
and its tone, but states only what the SOURCE states. Drop the unsupported detail (a figure, height,
distance, date, size, service, place or claim); never replace it with another invented one. If
nothing in the sentence is supported, return an empty string. Return strict JSON only."""


def _repair_prompt(violations: list[dict], source: str, forbidden) -> str:
    listed = "\n".join(
        f'{i}. [{v["field"]}] {json.dumps(v["sentence"])}'
        + (f' (unsupported figures: {", ".join(v["novel_numbers"])})' if v.get("novel_numbers") else "")
        for i, v in enumerate(violations, start=1)
    )
    return (f"SOURCE (the only facts you may state):\n{source}\n\nSENTENCES TO CORRECT:\n{listed}\n\n"
            + (f"Never use these words: {', '.join(sorted(forbidden))}.\n" if forbidden else "")
            + 'Return JSON mapping each number to its corrected sentence, e.g. {"1": "...", "2": ""}.')


def apply_replacements(generated: dict, violations: list[dict], replacements: dict, source_parts: list[str],
                       forbidden) -> tuple[dict, list[str]]:
    """Put each accepted replacement in place of its exact sentence. A replacement is refused (the
    original kept) when it still carries a novel number or adds a forbidden word. An empty
    replacement deletes the sentence; a highlight is only deleted while 3+ remain. Returns
    (new_generated, fields_changed)."""
    from .forbidden_words import forbidden_in

    out = dict(generated)
    changed: set[str] = set()
    for i, v in enumerate(violations, start=1):
        new = replacements.get(str(i))
        if new is None:
            continue
        new = str(new).strip()
        old, field = v["sentence"], v["field"]
        adds_forbidden = forbidden_in(new, forbidden) - forbidden_in(old, forbidden)
        if new and (find_novel_numeric_claims(new, source_parts) or adds_forbidden):
            continue
        value = out.get(field)
        if field == "highlights" and isinstance(value, list):
            items = [str(x) for x in value]
            if old not in items or (not new and len(items) <= 3):
                continue
            out[field] = [x for x in items if x != old] if not new else [new if x == old else x for x in items]
        elif isinstance(value, str) and old in value:
            if new:
                out[field] = value.replace(old, new, 1)
            else:
                out[field] = re.sub(r"[ \t]*" + re.escape(old) + r"[ \t]*", " ", value, count=1)
            out[field] = re.sub(r"  +", " ", out[field]).strip() if field != "itineraries" else out[field]
        else:
            continue
        changed.add(field)
    return out, sorted(changed)


def repair(generated: dict, violations: list[dict], tour: dict, *, model_tier=None, forbidden=()) -> dict:
    """One writer call that rewrites only the violating sentences (stage s1_flag_fix). Never raises:
    returns {generated, fields, cost_usd, error}."""
    from json_repair import repair_json
    from shared.llm_client.call_log import record_call_sync
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest

    source = source_text(tour)
    parts = source_number_parts(tour)
    try:
        resp = LLMClient().generate(LLMRequest(
            system_prompt=REPAIR_SYSTEM, user_prompt=_repair_prompt(violations, source, forbidden),
            model_tier=model_tier, stage="s1_flag_fix", max_tokens=4096,
        ))
        raw = resp.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            raw = raw[4:] if raw.startswith("json") else raw
        try:
            replacements = json.loads(raw)
        except json.JSONDecodeError:
            replacements = repair_json(raw, return_objects=True)
        replacements = replacements if isinstance(replacements, dict) else {}
        new_generated, fields = apply_replacements(generated, violations, replacements, parts, forbidden)
        record_call_sync(
            stage="s1_flag_fix", role="writer", model=resp.model_used,
            tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
            cost_usd=resp.cost_usd, tenant_id=None,
            quality_signal={"source": "grounding_repair", "sentences": len(violations), "fields_changed": fields},
            stop_reason=getattr(resp, "stop_reason", None),
            account=getattr(resp, "satellite_account", None),
            fallback_used=getattr(resp, "fallback_used", None),
        )
        return {"generated": new_generated, "fields": fields, "cost_usd": resp.cost_usd, "error": ""}
    except Exception as exc:
        logger.warning("grounding_repair_failed", error=str(exc)[:200])
        return {"generated": generated, "fields": [], "cost_usd": 0.0, "error": str(exc)[:200]}


def _codes(violations: list[dict]) -> list[str]:
    return list(dict.fromkeys(v["code"] for v in violations))


def repair_and_recheck(generated: dict, violations: list[dict], tour: dict, *, model_tier=None,
                       brand_forbidden_words=None) -> dict:
    """One sentence repair against the source, then a re-check. Returns {generated, violations,
    notes, fields, cost_usd}; `generated` is unchanged when the repair changed nothing."""
    from .forbidden_words import all_forbidden

    fixed = repair(generated, violations, tour, model_tier=model_tier,
                   forbidden=all_forbidden(brand_forbidden_words))
    if not fixed["fields"]:
        return {"generated": generated, "violations": violations, "notes": None, "fields": [],
                "cost_usd": fixed["cost_usd"]}
    res = check_grounding(fixed["generated"], tour)
    return {"generated": fixed["generated"], "violations": res["violations"], "notes": res["notes"],
            "fields": fixed["fields"], "cost_usd": fixed["cost_usd"]}


def grounding_node(state: dict) -> dict:
    """S1 graph node between brand_audit and flag_fix (A1 only): check, repair the violating
    sentences once, re-check. What is still unsupported stays in `grounding_violations` (and its
    codes in failure_codes); revalidate sends it to manual_check."""
    if state.get("is_tenant_rewrite"):
        return {**state, "grounding_ran": False}
    try:
        generated, tour = state.get("generated", {}), state.get("tour", {})
        first = check_grounding(generated, tour)
        found = first["violations"]
        cost, fields, violations, notes = 0.0, [], found, first["notes"]
        if found:
            rr = repair_and_recheck(generated, found, tour, model_tier=state.get("model_tier"),
                                    brand_forbidden_words=state.get("brand_forbidden_words"))
            generated, violations, fields, cost = rr["generated"], rr["violations"], rr["fields"], rr["cost_usd"]
            notes = rr["notes"] if rr["notes"] is not None else notes
    except Exception as exc:                       # never break the rewrite
        logger.warning("grounding_node_failed", error=str(exc)[:200])
        return {**state, "grounding_ran": False}
    logger.info("grounding_done", units=first["units"], jev_asked=first["jev_asked"], found=len(found),
                repaired_fields=fields, remaining=len(violations), notes=len(notes))
    return {
        **state,
        "generated": generated,
        "cost_usd": state.get("cost_usd", 0) + cost,
        "failure_codes": list(dict.fromkeys((state.get("failure_codes") or []) + _codes(violations))),
        "grounding_ran": True,
        "grounding_found": found,
        "grounding_repaired_fields": fields,
        "grounding_violations": violations,
        "grounding_notes": notes,
    }


def regrounding(state: dict) -> Optional[dict]:
    """Re-check after flag_fix (revalidate). None when grounding did not run for this tour."""
    if not state.get("grounding_ran"):
        return None
    try:
        return check_grounding(state.get("generated", {}), state.get("tour", {}))
    except Exception as exc:
        logger.warning("regrounding_failed", error=str(exc)[:200])
        return None


async def record_approved_outcome(conn_or_pool, tour: dict, generated: dict) -> int:
    """AA-756 — a human approved this master version in the Review Queue, so every grounded sentence
    is now known-supported truth. Stamp `a1_claim_supported` truth=True on each sentence unit's
    decision_log rows, keyed by the SAME subject_key the S1 run used.

    `tour` is the raw source dict (the SOURCE_FIELDS shape built in _execute_run_tour); `generated`
    is the approved version's grounded fields (keys subtitle/summary/highlights/itineraries). Uses
    the SAME per-unit source (`unit_source`) + `subject_key` as the grounding run (AA-756), so an
    approved sentence maps to exactly the row Jev wrote. Best-effort: never raises, returns rows
    updated."""
    from shared.llm_client.decide import record_outcome

    try:
        source = source_text(tour or {})
        units = sentence_units(generated or {})
    except Exception as exc:
        logger.warning("record_approved_outcome_build_failed", error=str(exc)[:200])
        return 0
    if not source or not units:
        return 0
    updated = 0
    for u in units:
        updated += await record_outcome(
            conn_or_pool, QUESTION, subject_key(unit_source(tour or {}, u), u["sentence"]),
            truth=True, source="review_approve")
    logger.info("record_approved_outcome", units=len(units), rows_updated=updated)
    return updated
