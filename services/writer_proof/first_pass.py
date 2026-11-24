"""AA-1169 — why a T9 blog fails its first pass under the W5 measure, read from the stored proof rows ($0: no model
call, nothing written).

The W5 measure (services/writer_proof/decision.py): an item's first pass is the writer's first draft (R2 regenerate,
eval_writers._t9: `generate.write_content` only, no repair); it fails when the pinned claim instrument flags any claim
(instrument.verdict_of: every flagged sentence is in `details.judge_claims` as {class, claim, why}, the class is always
instrument.CLAIM_CLASS). first_pass_yield = the share of candidate rows with verdict pass.

The instrument names no policy class, so this module types each flagged claim itself, deterministically, from the
sentence's own words (the writing policy topics it would break, services/quality/writing_policy.yaml `claims`):

  number         a figure, year or spelled-out amount (D1 numbers; a "Day 3" heading word is not one)
  rarity         rarity, uniqueness, superlative or significance attached to the place or the trip (D13)
  suitability    who the trip suits, its pace, ease, preparation, safety or comfort (D14)
  route_timing   time of day, order, direction or movement between moments ("by evening", "the day begins",
                 "heading north", "comes into view"; D4 / D15 timing, F17 direction)
  scene          what the traveller sees, hears or feels there (D15 staged scene)
  operator       a claim about the brand or how it plans the trip (brand pack: instructions, never a source, D2)
  reflection     interpretation: what a place means, why it matters (D3 voice, unless it states a fact)
  other          none of the above

The first class that matches in CLASS_ORDER wins (a sentence with a number and a superlative is `number`). Each claim
is also placed in the output: its field (`seo` for the title / meta / slug, `faq` after the body's "## FAQ" heading,
else `body`) and its reading position, so `first` is the class of the first flagged sentence a reader meets.

`project` answers "what if a writer fix removed every flagged claim of these classes": the arm-B rows rewritten with
those claims dropped (a row with none left becomes a pass), then decision.computed_metrics and decision.decide on them.
It is an upper bound of the fix's effect at the instrument's current precision (a fix rarely removes a whole class, and
a rewritten sentence can be flagged for something else); the paid re-measure is the proof.

`gate_replay` re-runs the deterministic T10 gates (quality_gates.run_quality_gates with `f9_results=[]`: the F9 judge
is never called, the cross-tenant lookup is not run) on one stored first draft, with the gate inputs built from the
item's stored writer context the way run_write_background builds them (scripts/aa729/score.t9_gate_replay does the
same with the live F9 judge). Citation tags are stripped first, as the writer does before any gate reads a piece.

Pure but for `load` (READ ONLY transaction) and `run` (load + the pure parts). Output: ids, classes and counts only.
"""
from __future__ import annotations

import copy
import re
from collections import Counter
from statistics import mean
from typing import Any, Iterable, Optional

from services.writer_proof import decision

CLASS_ORDER = ("number", "rarity", "suitability", "route_timing", "scene", "operator", "reflection", "other")
FIELDS = ("seo", "body", "faq")
SEO_FIELDS = frozenset({"seo_title", "meta_description", "seo_meta", "slug", "title", "subtitle"})
WRITER = "t9_blog"

_NUM_WORDS = (r"two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|"
              r"sixty|seventy|eighty|ninety|hundred|hundreds|thousand|thousands|million|millions|dozen|percent|"
              r"half|quarter|twice|double")
# a day reference is a structure word, not a figure: "Day 3", "day three", "four days" ("one" alone never counts)
_DAY_REF = re.compile(rf"\b(?:day|days)\s+(?:\d+|one|{_NUM_WORDS})\b"
                      rf"|\b(?:\d+|one|{_NUM_WORDS})(?:st|nd|rd|th)?\s+days?\b", re.IGNORECASE)
_PATTERNS: dict[str, re.Pattern] = {
    "number": re.compile(rf"\d|\b(?:{_NUM_WORDS})\b", re.IGNORECASE),
    "rarity": re.compile(
        r"\b(?:only|rare|rarely|rarer|unique|uniquely|last|unchanged|untouched|unspoil\w*|few|fewer|fewest|"
        r"most|least|best|finest|greatest|largest|oldest|nowhere else|unlike|one of the|genuine\w*|truly|"
        r"remarkabl\w*|extraordinar\w*|striking\w*|unrivall?ed|exceptional\w*|unmatched|iconic|legendary|"
        r"difficult to (?:replicate|find|match)|hard to (?:replicate|find|match))\b", re.IGNORECASE),
    "suitability": re.compile(
        r"\b(?:suit|suits|suited|suitable|ideal for|perfect for|easy|easily|gentle|gently|no pressure|pressure|"
        r"pace|paced|unhurried|relaxed|leisurely|fitness|prepar\w*|safe|safety|safely|comfort\w*|reassur\w*|"
        r"accessible|demanding|challenging|strenuous|manageable|younger (?:travell?ers|children)|all ages)\b",
        re.IGNORECASE),
    "route_timing": re.compile(
        r"\b(?:begin|begins|began|beginning|start|starts|started|end|ends|ended|first|then|next|before|after|"
        r"afterwards|later|earlier|finally|by (?:the )?(?:morning|afternoon|evening|noon|night|nightfall|the time)|"
        r"morning|afternoon|evening|dawn|dusk|sunset|sunrise|overnight|arrive\w*|arrival|reach|reaches|reached|"
        r"depart\w*|leave|leaves|leaving|continu\w*|onward\w*|onwards|heads?|heading|(?:north|south|east|west)wards?|"
        r"(?:move|moves|moving|travel\w*|journey|route|runs?|continu\w*|heads?|heading|drive|ride)\s+(?:\w+\s+){0,3}"
        r"(?:north|south|east|west)\w*|comes? into view|pass(?:es)? through|on the way|en route|along the way|"
        r"the journey|pivot|turning point|transition|gives way)\b",
        re.IGNORECASE),
    "scene": re.compile(
        r"\b(?:sky|skies|light|air|silence|silent|quiet|quietly|sound|sounds|smell|smells|scent|feel|feels|felt|"
        r"view|views|see|sees|seen|watch|watching|glow|glows|mist|cool|warm|cold|colou?r|colou?rs|shadow|"
        r"shadows|breeze|wind|hush|stillness|echo\w*|sparkl\w*|shimmer\w*|look out|gaze)\b", re.IGNORECASE),
    "operator": re.compile(
        r"\b(?:we|our|us|designer|designers|designs|designed|crafts|crafted|curat\w*|tailor\w*|"
        r"private journeys?|travel designer)\b", re.IGNORECASE),
    "reflection": re.compile(
        r"\b(?:means|meaning|matter|matters|mattered|understand\w*|significan\w*|weight|story|stories|memory|"
        r"memories|identity|heart|soul|character|sense of|why|lesson|reminder|speaks to|testament|symbol\w*)\b",
        re.IGNORECASE),
}


def claim_class(text: str) -> str:
    """The policy topic of one flagged sentence (CLASS_ORDER, first match wins). Pure."""
    t = " ".join(str(text or "").split())
    for cls in CLASS_ORDER[:-1]:
        probe = _DAY_REF.sub(" ", t) if cls == "number" else t
        if _PATTERNS[cls].search(probe):
            return cls
    return "other"


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def _fields(output: Any) -> list[tuple[str, str]]:
    """[(output key, text)] in the judge's reading order (eval_judge.judge_output_text, instrument.output_fields)."""
    from services.writer_proof import instrument
    from services.writing_memory import eval_judge as ej
    return instrument.output_fields(ej.judge_output_text(WRITER, output if isinstance(output, dict) else None))


def locate(claim: str, output: Any) -> tuple[str, int]:
    """(field kind, reading position) of a flagged claim in the stored output: `seo` / `faq` / `body` and an index
    that orders claims as a reader meets them (fields in the judge's order, then characters). A claim not found
    (output missing, text cut at 240 chars and changed) is ("body", a large index): counted, never dropped. Pure."""
    probe = _norm(claim)[:80].rstrip(" .")
    offset = 0
    for key, text in _fields(output):
        flat = _norm(text)
        at = flat.find(probe) if probe else -1
        if at >= 0:
            if key in SEO_FIELDS:
                kind = "seo"
            else:
                faq_at = flat.find("## faq")
                kind = "faq" if 0 <= faq_at <= at else "body"
            return kind, offset + at
        offset += len(flat) + 1
    return "body", 10 ** 9


def item_record(row: dict) -> dict:
    """One result row as {sample_id, arm, verdict, flags: [{class, field, pos}] in reading order, first}. Pure."""
    claims = list((row.get("details") or {}).get("judge_claims") or [])
    flags = []
    for c in claims:
        text = c.get("claim") if isinstance(c, dict) else str(c)
        field, pos = locate(text, row.get("output"))
        flags.append({"class": claim_class(text), "field": field, "pos": pos})
    flags.sort(key=lambda f: f["pos"])
    return {"sample_id": row.get("sample_id"), "arm": _arm(row), "verdict": row.get("verdict"), "flags": flags,
            "first": flags[0]["class"] if flags else None}


def _arm(row: dict) -> Optional[str]:
    if row.get("arm") == "candidate" and row.get("mode") == "regenerate":
        return "B"
    if row.get("arm") == "control" and row.get("mode") in ("rejudge", "regenerate"):
        return "A"
    return None


def _sorted(c: Counter) -> dict[str, int]:
    return {k: c[k] for k in CLASS_ORDER if c.get(k)} | {k: v for k, v in sorted(c.items()) if k not in CLASS_ORDER}


def diagnose(rows: Iterable[dict]) -> dict[str, dict]:
    """Per arm (A, B): judged items, passes, first_pass_yield, flags per item, and per class the items whose FIRST
    flag it is, the items with at least one flag of it, all its flags, and the items it fails ALONE (every flag of
    the item is that class: the items a fix of that class alone would turn), plus flags per field. Counts only."""
    out: dict[str, dict] = {}
    for arm in ("A", "B"):
        recs = [item_record(r) for r in rows if _arm(r) == arm and r.get("verdict") in ("pass", "fail")]
        if not recs:
            continue
        first, anyc, allc, sole, by_field = Counter(), Counter(), Counter(), Counter(), Counter()
        for rec in recs:
            classes = {f["class"] for f in rec["flags"]}
            if rec["first"]:
                first[rec["first"]] += 1
            anyc.update(classes)
            allc.update(f["class"] for f in rec["flags"])
            by_field.update(f["field"] for f in rec["flags"])
            if len(classes) == 1:
                sole.update(classes)
        passes = sum(1 for r in recs if r["verdict"] == "pass")
        out[arm] = {"items": len(recs), "passes": passes, "first_pass_yield": round(passes / len(recs), 6),
                    "flags": sum(allc.values()), "flags_per_item": round(mean(len(r["flags"]) for r in recs), 6),
                    "first_class": _sorted(first), "items_with_class": _sorted(anyc), "flags_by_class": _sorted(allc),
                    "items_failed_by_class_alone": _sorted(sole),
                    "flags_by_field": {k: by_field[k] for k in FIELDS if by_field.get(k)}}
    return out


def _without(row: dict, fixed: frozenset[str]) -> dict:
    """The row as if the writer had not written any flagged claim of the `fixed` classes."""
    if _arm(row) != "B" or row.get("verdict") not in ("pass", "fail"):
        return row
    det = dict(row.get("details") or {})
    kept = [c for c in det.get("judge_claims") or []
            if claim_class(c.get("claim") if isinstance(c, dict) else str(c)) not in fixed]
    det["judge_claims"] = kept
    return {**row, "details": det, "verdict": "fail" if kept else "pass"}


def project(doc: dict, rows: list[dict], fixed: Iterable[str]) -> dict:
    """The arm-B metrics and the pre-registered verdict if a fix removed every flagged claim of the `fixed` classes
    (an upper bound, see the module docstring). Pure."""
    fx = frozenset(fixed)
    unknown = sorted(fx - set(CLASS_ORDER))
    if unknown:
        raise ValueError(f"unknown classes: {', '.join(unknown)} (known: {', '.join(CLASS_ORDER)})")
    moved = [_without(r, fx) for r in rows]
    comp = decision.computed_metrics(moved)
    return {"fixed": sorted(fx), "first_pass_yield": comp["first_pass_yield"],
            "fail_claims_per_item": comp["fail_claims_per_item"], "item_fail_rate": comp["item_fail_rate"],
            "decision": decision.decide(doc, moved)}


# ── the deterministic gates on a stored first draft ($0) ───────────────────────────────────────────────────────────

def gate_replay(context: dict, output: dict) -> dict:
    """{passed, first_failure, blocking}: the deterministic T10 gate stack on one stored draft (F9 judge skipped).
    Raises on an input that cannot be rebuilt (the caller counts it)."""
    from services.acp_content_writing import service as svc
    from services.acp_content_writing.quality_gates import expand_ledger, strip_citation_tags

    ctx = copy.deepcopy(context)
    svc._add_date_spans(ctx)
    channel, ba = ctx.get("channel") or "blog", ctx.get("brand_audience") or {}
    facts_text = ctx.get("facts_text") or ""
    spec = svc.channel_spec(channel, ba)
    hyg = svc.hygiene.HygieneContext(seed=ctx.get("atom_text") or "", scope=ctx.get("scope"),
                                     trip_name=ctx.get("trip_name"), brand_name=ba.get("brand_name"),
                                     cta=ctx.get("cta"), hashtags_max=spec.hashtags_max if spec else None,
                                     first_line_max=spec.first_line_max if spec else None,
                                     audience=ba.get("customer_segment"), facts=facts_text,
                                     trip_places=tuple(ctx.get("trip_places") or ()))
    trip_ctx = svc.trip_facts.TripFactContext(
        seed=ctx.get("atom_text") or "", source=f"{hyg.lexicon.own_text}\n{facts_text}",
        audience=ba.get("customer_segment"), cta=ctx.get("cta"), trip_places=tuple(ctx.get("trip_places") or ()),
        channel=channel, brand_name=ba.get("brand_name"))
    briefs = svc.hygiene.brief_texts(ba, ctx.get("chosen"))
    ctx["writer_trip_name"] = svc.hygiene.scoped_trip_name(ctx.get("trip_name"), ctx.get("scope"), hyg.lexicon)
    out = svc.run_quality_gates(
        content_text=strip_citation_tags(output.get("content_text") or ""), atom_text=svc._grounding_text(ctx),
        cta=ctx.get("cta"), brand_rubric_text=ctx.get("brand_rubric_text") or "", channel=channel,
        keyword=ctx.get("keyword"), seo_title=output.get("seo_title"),
        meta_description=output.get("meta_description"), slug=output.get("slug"), cannibalization_match=None,
        brand_pack=ba, hygiene_ctx=hyg, stock_blocking=True, sibling_texts=[], trip_ctx=trip_ctx,
        brief_texts=briefs, taken_titles=[], f9_results=[])
    blocking = sorted({g["gate"] for g in expand_ledger(out.get("gate_ledger") or [])
                       if not g.get("passed") and g.get("blocking", True)})
    return {"passed": bool(out.get("passed")), "first_failure": (out.get("first_failure") or {}).get("gate"),
            "blocking": blocking}


def gate_summary(pairs: Iterable[tuple[dict, dict]]) -> dict:
    """The gate replay over (context, output) pairs: passed, first failing gate counts, any failing gate counts,
    and the drafts that could not be replayed. Counts only."""
    first, anyg = Counter(), Counter()
    n = passed = errors = 0
    for ctx, output in pairs:
        n += 1
        try:
            r = gate_replay(ctx, output)
        except Exception:  # noqa: BLE001 — a stored input that cannot be rebuilt is counted, never fatal
            errors += 1
            continue
        passed += int(r["passed"])
        first[r["first_failure"] or "none"] += 1
        anyg.update(r["blocking"])
    return {"drafts": n, "passed": passed, "errors": errors, "first_failure": dict(first.most_common()),
            "any_blocking": dict(anyg.most_common())}


# ── the stored rows (READ ONLY) ────────────────────────────────────────────────────────────────────────────────────

ROWS_SQL = """
    SELECT r.result_id::text AS result_id, r.run_id::text AS run_id, g.sample_id, r.arm, r.mode, r.verdict,
           r.details, r.output, g.input
      FROM shared.eval_item_result r
      JOIN shared.eval_golden_item g ON g.item_id = r.item_id
     WHERE r.run_id = $1::uuid
     ORDER BY g.sample_id, r.arm
"""


def _json(v: Any) -> Any:
    import json
    return json.loads(v) if isinstance(v, str) else v


async def load(pool, prereg_id: str) -> tuple[Optional[str], Optional[str], list[dict]]:
    """(the counted B run id, its golden set id, its result rows with output, details and the item input), in one
    READ ONLY transaction; (None, None, []) before a B run has results."""
    from services.writer_proof.runner import counted_runs
    from services.writing_memory import r2
    async with pool.acquire() as conn:
        async with conn.transaction(readonly=True):
            prior = [dict(r) for r in await conn.fetch(r2.PREREG_RUNS_SQL, prereg_id)]
            run_b = counted_runs(prior)[0].get("B")
            if not run_b:
                return None, None, []
            set_id = await conn.fetchval(
                "SELECT golden_set_id::text FROM shared.prompt_eval_runs WHERE id = $1::uuid", run_b)
            rows = [dict(r) for r in await conn.fetch(ROWS_SQL, run_b)]
    for r in rows:
        for k in ("details", "output", "input"):
            r[k] = _json(r.get(k))
    return run_b, set_id, rows


def run(doc: dict, rows: list[dict], *, fixed: Iterable[str] = (), gates: bool = True) -> dict:
    """The first-pass report of one proof from its rows: diagnose, the projection of `fixed`, and the deterministic
    gate replay of each arm's stored drafts (regenerate rows that kept an output)."""
    from services.writing_memory import eval_writers as ew
    out: dict[str, Any] = {"classes": list(CLASS_ORDER), "diagnosis": diagnose(rows)}
    fx = list(fixed)
    if fx:
        out["projection"] = project(doc, rows, fx)
    if gates:
        out["gate_replay"] = {}
        for arm in ("A", "B"):
            pairs = []
            for r in rows:
                if _arm(r) == arm and r.get("mode") == "regenerate" and isinstance(r.get("output"), dict):
                    try:
                        pairs.append((ew._t9_context(r.get("input") or {}), r["output"]))
                    except Exception:  # noqa: BLE001 — counted below as a draft that could not be replayed
                        pairs.append(({}, r["output"]))
            if pairs:
                out["gate_replay"][arm] = gate_summary(pairs)
    return out


__all__ = ["CLASS_ORDER", "FIELDS", "ROWS_SQL", "claim_class", "diagnose", "gate_replay", "gate_summary",
           "item_record", "load", "locate", "project", "run"]
