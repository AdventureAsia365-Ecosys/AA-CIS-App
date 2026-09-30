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
    """Checkable units of the narrative fields: {field, sentence}. Highlights are one unit per item;
    itinerary day-title lines ("Day 3 — Paro to Thimphu") are skipped, their bodies are split."""
    units: list[dict] = []
    for field in GROUNDED_FIELDS:
        value = generated.get(field)
        if not value:
            continue
        if field == "highlights" and isinstance(value, list):
            chunks = [str(v) for v in value]
        elif field == "itineraries":
            lines = [ln for ln in _as_text(value).splitlines() if ln.strip() and not _DAY_TITLE_RE.match(ln)]
            chunks = [s for ln in lines for s in _SENT_SPLIT_RE.split(ln)]
        else:
            chunks = _SENT_SPLIT_RE.split(_as_text(value))
        for c in chunks:
            s = c.strip()
            if len(s.split()) >= MIN_WORDS:
                units.append({"field": field, "sentence": s})
    return units


def _h(text: str, n: int) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:n]


def subject_key(source: str, sentence: str) -> str:
    """Stable per (source, sentence): an unchanged sentence re-checked after flag_fix hits the
    Verdict cache instead of paying again."""
    return f"claim:{_h(source, 10)}:{_h(sentence, 12)}"


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


async def _judge_all(units: list[dict], source: str) -> dict[int, Any]:
    """One Jev Noul per unit, CONCURRENCY in flight, over a small pool opened for this tour."""
    import asyncpg
    from shared.llm_client.decide import decide
    from shared.secrets import get_database_url

    pool = await asyncpg.create_pool(get_database_url(), ssl="require", min_size=1, max_size=POOL_SIZE)
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(i: int, u: dict):
        async with sem:
            d = await decide(STAGE, subject_key(source, u["sentence"]),
                             {"source": source, "sentence": u["sentence"]}, [QUESTION], pool=pool)
            return i, d.verdicts.get(QUESTION)

    try:
        return dict(await asyncio.gather(*[one(i, u) for i, u in enumerate(units)]))
    finally:
        await pool.close()


def judge_units(units: list[dict], source: str) -> dict[int, Any]:
    """Sync entry for the S1 graph node (runs in LangGraph's worker thread). Fails open: on any
    error, or when called from a thread that already runs a loop, returns {} (no Jev signal) and the
    deterministic check still applies."""
    if not units:
        return {}
    try:
        asyncio.get_running_loop()
        logger.warning("grounding_judge_on_event_loop")
        return {}
    except RuntimeError:
        pass
    try:
        return asyncio.run(_judge_all(units, source))
    except Exception as exc:
        logger.warning("grounding_judge_failed", error=str(exc)[:200])
        return {}


def check_grounding(generated: dict, tour: dict, *, use_jev: bool = True) -> dict:
    """Run both signals over `generated`. Returns {violations, notes, units, jev_asked}."""
    units = sentence_units(generated or {})
    source = source_text(tour or {})
    source_parts = source_number_parts(tour or {})
    numeric = {i: n for i, u in enumerate(units) if (n := find_novel_numeric_claims(u["sentence"], source_parts))}
    verdicts = judge_units(units, source) if (use_jev and source) else {}
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


def grounding_node(state: dict) -> dict:
    """S1 graph node between brand_audit and flag_fix (A1 only): check, repair the violating
    sentences once, re-check. What is still unsupported stays in `grounding_violations` (and its
    codes in failure_codes); revalidate sends it to manual_check."""
    if state.get("is_tenant_rewrite"):
        return {**state, "grounding_ran": False}
    try:
        from .forbidden_words import all_forbidden

        generated, tour = state.get("generated", {}), state.get("tour", {})
        first = check_grounding(generated, tour)
        found = first["violations"]
        cost, fields, res = 0.0, [], first
        if found:
            fixed = repair(generated, found, tour, model_tier=state.get("model_tier"),
                           forbidden=all_forbidden(state.get("brand_forbidden_words")))
            cost, fields = fixed["cost_usd"], fixed["fields"]
            if fields:
                generated = fixed["generated"]
                res = check_grounding(generated, tour)
    except Exception as exc:                       # never break the rewrite
        logger.warning("grounding_node_failed", error=str(exc)[:200])
        return {**state, "grounding_ran": False}
    logger.info("grounding_done", units=res["units"], jev_asked=first["jev_asked"], found=len(found),
                repaired_fields=fields, remaining=len(res["violations"]), notes=len(res["notes"]))
    return {
        **state,
        "generated": generated,
        "cost_usd": state.get("cost_usd", 0) + cost,
        "failure_codes": list(dict.fromkeys((state.get("failure_codes") or []) + _codes(res["violations"]))),
        "grounding_ran": True,
        "grounding_found": found,
        "grounding_repaired_fields": fields,
        "grounding_violations": res["violations"],
        "grounding_notes": res["notes"],
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
