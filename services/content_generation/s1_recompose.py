"""AA-1049 path A-new (S277 audit, master-propagation map §1A) — recompose the FIXED fields of a published S1 Master
with today's S1 train compose code, keep the narrative, no model call.

The S1 train (``s1_train.compose_page``, the ``S1_CONTROL=contract`` path) composes the contract fields of a page by
code: the day headings ``Day N — Title`` numbered from the day plan, the source day's verbatim fact lines in place of
the writer's own (meals / overnight / distance ...), the SEO title fitted at a word boundary, the code meta template
when the stored meta fails the meta checks. A Master written before the train (or before a rule changed) differs from
what the train composes today; the S277 audit counted 708 of 844. This module is the pure half of the fix: it takes
the stored page, feeds its own day blocks to ``compose_page`` as the writer's day objects, and says what changed.

What it guarantees (``recompose``), or the Master is skipped with a typed reason:

* the narrative is unchanged: name, subtitle, summary, description and highlights byte for byte; per day block the
  title (whitespace-normalised) and every prose line (a fact clause "Label: value." at the end of a prose line is a
  fact line, not prose: the train strips it like any writer fact line); nothing before the first heading is dropped,
  no day block with prose is dropped (``narrative_changed``);
* the stored and the composed itinerary parse as canonical day blocks (A3, catalog ``_tour_day_text``) with the same
  number of blocks; a legacy-format itinerary is never written (A3 would re-read it whole, new atom ids)
  (``heading_parse``);
* no fact is lost (S277 pilot fix): a writer fact clause ("Overnight: X." at the end of a prose line, a "Label:
  value" line) is removed only when the same day gets the source's verbatim fact line with the same label and value
  (normalised compare, ``fact_keys``); any other clause stays where it was (``keep_unstated_facts``, the
  ``strip_facts`` hook of ``compose_page``). A stored fact the recomposed page no longer states is counted
  (``facts_lost``) and the Master is skipped (``facts_would_be_lost``);
* an itinerary whose lines are the same after compose (nothing cut, nothing added, only spacing or order) is kept
  byte for byte, so such a Master is a no-op unless its SEO fields change;
* a re-run on a recomposed page is a no-op: the stored lines that ARE the source day's verbatim fact lines are taken
  out before compose and composed back in the same place (``no_op``).

The contract verdict (``contract_pair``) is the S1 final contract recomputed today on the stored text and on the
recomposed text, with today's validate codes (as ``scripts/quality/audit_s1_masters.py`` does); a recompose whose
verdict is worse than the stored one or today's, or that adds a blocking code, is skipped (``contract_worse``).

Pure: no DB, no model. The DB side (selection, new version, publish) is ``s1_recompose_store``.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterator, Optional

# v2 (S277 pilot fix): a writer fact clause is removed only when the day's source fact line states it again
WRITER = "recompose-v2"
EXIT = "recompose"
FIELDS = ("day_headings", "fact_lines", "day_count", "seo_title", "seo_meta")
NARRATIVE_FIELDS = ("name", "subtitle", "summary", "description", "highlights")

# skip reasons (one per Master, first match wins in s1_recompose_store.decide)
NOT_V2 = "not_v2"
HUMAN_EDITED = "human_edited"
POLICY = "policy"
NO_OP = "no_op"
NARRATIVE_CHANGED = "narrative_changed"
HEADING_PARSE = "heading_parse"
FACTS_WOULD_BE_LOST = "facts_would_be_lost"
CONTRACT_WORSE = "contract_worse"
SAMPLE_DAYS = 2           # day blocks per Master in the DRY RUN sample

# how far a verdict is from a clean publish (a recompose may never move a Master up this scale)
VERDICT_RANK = {"publish": 0, "review": 1, "policy_hold": 2, "source_hold": 2, "degraded": 3}


@dataclass
class Recomposed:
    page: dict                               # the recomposed page (only itineraries / seo_title / seo_meta differ)
    fields: dict                             # FIELDS -> count (0 = unchanged)
    changed: list                            # the FIELDS that changed
    narrative_ok: bool
    heading_ok: bool
    report: dict = field(default_factory=dict)   # counts only (stored in the version metadata)
    sample: list = field(default_factory=list)   # [(before lines, after lines)] of the first SAMPLE_DAYS day blocks

    @property
    def no_op(self) -> bool:
        return not self.changed


# ── stored page -> the writer's day objects ─────────────────────────────────────────────────────────────────────


def _lines(text: Any) -> list[str]:
    return [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]


def _source_lines_by_day(plan: dict) -> dict[int, list[str]]:
    """{planned day: the verbatim fact lines code appends for it} (``s1_writer_v2.source_fact_lines``, before the DO
    NOT USE filter: a stored copy of a dropped line is a fact line too). Empty when the train keeps the writer's fact
    lines (no source day blocks)."""
    from .s1_writer_v2 import source_fact_lines
    if plan["mode"] == "source_days":
        return {d["day"]: source_fact_lines(d["source"]) for d in plan["days"]}
    if plan["mode"] == "single_day":
        lines = source_fact_lines(plan["days"][0]["source"])
        return {d["day"]: lines for d in plan["days"]}
    return {}


def _block_source_lines(first: int, last: int, by_day: dict[int, list[str]]) -> set[str]:
    out: set[str] = set()
    for n in range(first, last + 1):
        out |= set(by_day.get(n) or [])
    return out


def _compose_day_lines(first: int, last: int, by_day: dict[int, list[str]]) -> list[str]:
    """The source lines compose appends to a block covering first..last: those of its first planned day
    (``compose_itineraries`` reads the source of the first day it emits for a writer object)."""
    days = sorted(n for n in by_day if first <= n <= last)
    return list(by_day.get(days[0]) or []) if days else []


def stored_items(itineraries: Any, plan: dict) -> tuple[list[dict], str]:
    """(the stored itinerary as the writer's day objects, the text before the first heading). One object per canonical
    block (``day_parse.canonical_blocks``, the A3 / catalog reader), its body without the lines that are the verbatim
    fact lines compose appends to that block again (a re-run is a no-op); a stored source line of another day of a
    ranged block stays in the body (compose does not append it, so taking it out would lose it)."""
    from .day_parse import canonical_blocks
    from .day_parse import CANONICAL_DAY_RE

    text = str(itineraries or "")
    m = CANONICAL_DAY_RE.search(text)
    preamble = text[:m.start()].strip() if m else text.strip()
    by_day = _source_lines_by_day(plan)
    items = []
    for b in canonical_blocks(text):
        src = set(_compose_day_lines(b.first, b.last, by_day))
        body = "\n".join(ln for ln in str(b.body).splitlines() if ln.strip() not in src).strip()
        items.append({"day": f"{b.first}-{b.last}" if b.ranged else str(b.first), "title": b.title, "body": body})
    return items, preamble


# ── what a fact line states: (label, value) keys ───────────────────────────────────────────────────────────────

_MEAL_CODES = {"b": "breakfast", "l": "lunch", "d": "dinner"}


@lru_cache(maxsize=1)
def _clause_re() -> "re.Pattern":
    from .s1_train import _FACT_CLAUSE
    return re.compile(_FACT_CLAUSE, re.I)


def _clauses(text: str) -> list[str]:
    """The "Label: value." clauses of a fact-only line or of a trailing fact run, in order."""
    return [m.group(0).strip() for m in _clause_re().finditer(str(text or "")) if m.group(0).strip()]


def clause_key(clause: str) -> tuple[str, str]:
    """(label, value) of one "Label: value" clause, normalised: case, spacing, the end stop, "Meal" = "Meals" and the
    meal codes B / L / D = breakfast / lunch / dinner (order and repeats ignored), so "Meals: B, L" states what
    "Meals: breakfast and lunch." states. Any other wording difference is a different value."""
    label, _sep, value = str(clause).partition(":")
    lab = re.sub(r"^meal\b", "meals", " ".join(label.strip(" -•*\t").lower().split()))
    val = " ".join(value.lower().split()).strip(" .;,")
    val = re.sub(r"\s*([,;/&])\s*", r"\1 ", val).strip()
    if lab.startswith("meals"):
        toks = [t.strip(" .") for t in re.split(r"\s*(?:[,;/&+]|\band\b)\s*", val) if t.strip(" .")]
        if toks:
            val = ", ".join(sorted({_MEAL_CODES.get(t, t) for t in toks}))
    return lab, val


def fact_keys(line: str) -> list[tuple[str, str]]:
    """The facts a fact line states: one (label, value) per clause when the line is only "Label: value" clauses (the
    train's fact-only line), else the whole line as one key (a source line such as "Ride: 2 hours" or
    "Accommodation: Canal guesthouse. Breakfast included at 8.")."""
    from .s1_train import _FACT_ONLY_LINE_RE
    s = re.sub(r"^\s*[-•*]\s*", "", str(line or "")).strip()
    if _FACT_ONLY_LINE_RE.match(s):
        keys = [clause_key(c) for c in _clauses(s)]
        if keys:
            return keys
    return [("line", " ".join(s.lower().split()).strip(" ."))]


def keep_unstated_facts(body: str, source_lines: list[str]) -> tuple[str, int]:
    """``compose_page``'s ``strip_facts`` for the recompose: a writer fact clause (on a fact-only line, or in the run
    at the end of a prose line) is removed only when one of ``source_lines`` (the verbatim lines compose appends to
    this day) states the same label and value (``clause_key``); every other clause stays where it was, word for word.
    Returns (body, lines changed). Prose is never touched."""
    from .s1_train import _FACT_ONLY_LINE_RE, _TRAILING_FACTS_RE
    stated = {k for ln in source_lines or [] for k in fact_keys(ln)}
    kept, changed = [], 0
    for line in str(body or "").splitlines():
        if _FACT_ONLY_LINE_RE.match(line):
            clauses = _clauses(line)
            rest = [c for c in clauses if clause_key(c) not in stated]
            if clauses and len(rest) < len(clauses):
                changed += 1
                if rest:
                    kept.append(" ".join(rest))
                continue
            kept.append(line)
            continue
        m = _TRAILING_FACTS_RE.search(line)
        if m and line[:m.start()].strip():
            clauses = _clauses(line[m.start():])
            rest = [c for c in clauses if clause_key(c) not in stated]
            if clauses and len(rest) < len(clauses):
                changed += 1
                line = " ".join([line[:m.start()].rstrip(), *rest])
        kept.append(line)
    return "\n".join(kept).strip(), changed


def _dnu_keys(plan: dict, facts: dict) -> set:
    """The keys of the source fact lines compose leaves out on purpose (a DO NOT USE item: supplier name, phone...):
    a stored copy of one that goes is not a lost fact."""
    from .s1_writer_v2 import drop_do_not_use
    out: set = set()
    for lines in _source_lines_by_day(plan).values():
        kept = set(drop_do_not_use(lines, facts))
        out |= {k for ln in lines if ln not in kept for k in fact_keys(ln)}
    return out


def facts_lost(before: list[dict], after: list[dict], dnu: set) -> int:
    """How many facts a stored day block states (``blocks_of`` facts, per ``fact_keys``) that the recomposed block at
    the same place no longer states, a DO NOT USE source line aside. Must be 0 for a write."""
    lost = 0
    for i, x in enumerate(before):
        have = {k for ln in (after[i]["facts"] if i < len(after) else []) for k in fact_keys(ln)}
        lost += sum(1 for ln in x["facts"] for k in fact_keys(ln) if k not in have and k not in dnu)
    return lost


def _day_lines(text: Any) -> list[list[str]]:
    """The non-blank lines of each canonical day block (its heading first), in order."""
    from .day_parse import CANONICAL_DAY_RE
    text = str(text or "")
    heads = list(CANONICAL_DAY_RE.finditer(text))
    return [_lines(text[m.start():(heads[i + 1].start() if i + 1 < len(heads) else len(text))])
            for i, m in enumerate(heads)]


# ── what a page says, per day block ─────────────────────────────────────────────────────────────────────────────


def blocks_of(itineraries: Any, plan: dict) -> tuple[str, list[dict]]:
    """(preamble, [{head, title, days, facts, prose, clauses_cut}]) of a stored or composed itinerary: ``facts`` = the
    fact lines in order (the source's verbatim lines and the writer's "Label: value" lines), ``prose`` = every other
    line with a trailing fact clause cut (``s1_train.strip_writer_fact_lines``), blank lines dropped."""
    from .day_parse import CANONICAL_DAY_RE, canonical_blocks
    from .s1_train import _FACT_ONLY_LINE_RE, strip_writer_fact_lines

    text = str(itineraries or "")
    heads = list(CANONICAL_DAY_RE.finditer(text))
    preamble = text[:heads[0].start()].strip() if heads else text.strip()
    by_day = _source_lines_by_day(plan)
    out = []
    for m, b in zip(heads, canonical_blocks(text)):    # the same matches, in the same order
        src = _block_source_lines(b.first, b.last, by_day)
        facts, prose, cut_n = [], [], 0
        for ln in _lines(b.body):
            if ln in src or _FACT_ONLY_LINE_RE.match(ln):
                facts.append(ln)
                continue
            kept, cut = strip_writer_fact_lines(ln)
            if cut:
                cut_n += 1
                facts.append(ln[len(kept):].strip())
            if kept.strip():
                prose.append(kept.strip())
        out.append({"head": text[m.start():].splitlines()[0].strip(), "title": " ".join(b.title.split()),
                    "days": (b.first, b.last), "facts": facts, "prose": prose, "clauses_cut": cut_n})
    return preamble, out


def narrative_of(page: dict, plan: dict) -> dict:
    """What a recompose must never change: the narrative fields byte for byte, the preamble, and per day block its
    title and prose lines (``blocks_of``)."""
    preamble, blocks = blocks_of(page.get("itineraries"), plan)
    return {"fields": {k: json.dumps(page.get(k), sort_keys=True, default=str) for k in NARRATIVE_FIELDS},
            "preamble": preamble,
            "days": [(b["title"], tuple(b["prose"])) for b in blocks]}


# ── the recompose ───────────────────────────────────────────────────────────────────────────────────────────────


def recompose(page: dict, tour: dict, facts: dict, brand_words: list[str]) -> Recomposed:
    """The stored ``page`` with its fixed fields composed by the S1 train today. ``tour`` = the S1 source dict with the
    agency fields (``s1_writer_v2.with_agency_fields``), ``facts`` its fact table, ``brand_words`` the tenant's active
    brand forbidden words. Pure; no model call."""
    from . import s1_train
    from . import s1_writer_v2 as v2
    from .forbidden_words import all_forbidden
    from .itinerary_utils import parse_canonical_itinerary_days

    plan = v2.day_plan(tour, facts)
    forbidden = all_forbidden(brand_words)
    problems = v2.meta_problems({"seo_title": page.get("seo_title"), "seo_meta": page.get("seo_meta")},
                                tour, facts, forbidden)
    items, _pre = stored_items(page.get("itineraries"), plan)
    composed, rep = s1_train.compose_page(dict(page), {"itineraries": items}, tour, facts, plan, forbidden, problems,
                                          strip_facts=keep_unstated_facts)
    new = {**page, "itineraries": composed.get("itineraries"), "seo_title": composed.get("seo_title"),
           "seo_meta": composed.get("seo_meta")}
    old_lines = Counter(_lines(page.get("itineraries")))
    new_lines = Counter(_lines(new.get("itineraries")))
    if old_lines == new_lines:
        # nothing cut, nothing added (spacing or line order only): the stored itinerary is kept byte for byte
        new["itineraries"] = page.get("itineraries")
        new_lines = old_lines

    _pre_a, a = blocks_of(page.get("itineraries"), plan)
    _pre_b, b = blocks_of(new.get("itineraries"), plan)
    n = max(len(a), len(b))
    pad = {"head": None, "facts": None, "days": None}
    pairs = [(a[i] if i < len(a) else pad, b[i] if i < len(b) else pad) for i in range(n)]
    fields = {
        "day_headings": sum(1 for x, y in pairs if x["head"] != y["head"]),
        "fact_lines": sum(1 for x, y in pairs if x["facts"] != y["facts"]),
        "day_count": int(len(a) != len(b) or [x["days"] for x in a] != [y["days"] for y in b]),
        "seo_title": int(str(new.get("seo_title") or "") != str(page.get("seo_title") or "")),
        "seo_meta": int(str(new.get("seo_meta") or "") != str(page.get("seo_meta") or "")),
    }
    if str(new.get("itineraries") or "") == str(page.get("itineraries") or ""):
        fields.update(day_headings=0, fact_lines=0, day_count=0)
    elif not any(fields[k] for k in ("day_headings", "fact_lines", "day_count")):
        fields["day_headings"] = 1   # whitespace / block spacing only: still a write, named as layout
    narrative_ok = narrative_of(page, plan) == narrative_of(new, plan)
    old_days = parse_canonical_itinerary_days(str(page.get("itineraries") or ""))
    new_days = parse_canonical_itinerary_days(str(new.get("itineraries") or ""))
    # a legacy (non-canonical) itinerary is never written: A3 would re-read it whole with new atom ids (map M5)
    heading_ok = bool(old_days) and bool(new_days) and len(b) == len(a)
    keep = ("composed", "missing", "extra", "duplicate", "fact_lines", "writer_fact_lines_removed",
            "source_fact_lines", "seo_title", "meta")
    report = {k: rep[k] for k in keep if k in rep}
    lost = facts_lost(a, b, _dnu_keys(plan, facts))
    report.update(plan_mode=plan["mode"], meta_problems=len(problems), days=len(b),
                  # prose lines whose trailing fact run the recompose cut (the source line states it now)
                  fact_clauses_cut=sum(max(0, x["clauses_cut"] - y["clauses_cut"]) for x, y in zip(a, b)),
                  lines_removed=sum((old_lines - new_lines).values()),
                  lines_added=sum((new_lines - old_lines).values()), facts_lost=lost)
    before, after = _day_lines(page.get("itineraries")), _day_lines(new.get("itineraries"))
    sample = [(before[i] if i < len(before) else [], after[i] if i < len(after) else [])
              for i in range(min(SAMPLE_DAYS, max(len(before), len(after))))]
    return Recomposed(page=new, fields=fields, changed=[k for k in FIELDS if fields[k]],
                      narrative_ok=narrative_ok, heading_ok=heading_ok, report=report, sample=sample)


# ── the contract, before and after ──────────────────────────────────────────────────────────────────────────────


class _NoModel:
    """Every model path answered without a model (the audit's guard): Jev gives no verdict, LLMClient raises."""

    def __init__(self):
        self.attempts = 0

    def judge_units(self, units, sources, *, tenant_id=None):
        return {}

    def refuse(self, *_a, **_k):
        self.attempts += 1
        raise RuntimeError("recompose: no model call")


@contextmanager
def no_model() -> Iterator[_NoModel]:
    """No model call can leave this block: a validate or contract path that would ask one raises (counted)."""
    from contextlib import ExitStack
    from unittest.mock import patch

    from services.content_generation import grounding
    from shared.llm_client.client import LLMClient

    nm = _NoModel()
    with ExitStack() as st:
        st.enter_context(patch.object(grounding, "judge_units", nm.judge_units))
        st.enter_context(patch.object(LLMClient, "generate", nm.refuse))
        st.enter_context(patch.object(LLMClient, "__init__", nm.refuse))
        yield nm


def _json(v: Any, default):
    if isinstance(v, (bytes, str)):
        try:
            v = json.loads(v)
        except ValueError:
            return default
    if v is None:
        return default
    return v if isinstance(v, type(default)) else default


def _get(row: Any, key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return None


def validate_codes(page: dict, tour: dict, brand_words: list[str], writer_version: Optional[str]) -> list[str]:
    """Today's validate failure codes on ``page`` (``graph.validate_node``; a stored NULL text reads as "")."""
    from services.content_generation import graph
    gen = {k: ("" if v is None and k != "highlights" else v) for k, v in page.items()}
    state = {"generated": gen, "tour": dict(tour), "writer_version": writer_version,
             "brand_forbidden_words": list(brand_words), "seo": {}, "retry_count": 0}
    return sorted(set(graph.validate_node(state)["failure_codes"]))


def today_codes(stored: list[str], validate_today: list[str], writer_version: Optional[str]) -> list[str]:
    """The failure codes the live S1 exit would hand the contract: the hard-block codes from today's validate run,
    the other stored codes (grounding, rewrite-class) as stored (``audit_s1_masters.today_codes_of``)."""
    from services.content_generation.graph import hard_block_codes
    hard = hard_block_codes({"writer_version": writer_version})
    return sorted({c for c in stored if c not in hard} | set(validate_today))


def _page_row(row: Any, page: dict, failure_codes: list[str]) -> dict:
    out = {k: _get(row, k) for k in row.keys()} if hasattr(row, "keys") else dict(row)
    out.update(aa_itineraries=page.get("itineraries"), seo_title=page.get("seo_title"),
               seo_meta=page.get("seo_meta"), failure_codes=json.dumps(failure_codes))
    return out


def blocks(rec: dict) -> list[str]:
    return sorted({f["code"] for f in rec.get("findings") or [] if f.get("severity") == "block"})


def contract_pair(row: Any, page: dict, new_page: dict, tour: dict, brand_words: list[str]) -> dict:
    """The S1 final contract today on the stored page and on the recomposed page (``check_s1`` over the stored row
    with the page swapped in and today's validate codes), the stored verdict, and whether the recompose is worse."""
    from services.quality import final_contract as fc

    md = _json(_get(row, "metadata"), {})
    wv = md.get("writer_version")
    stored_codes = [str(c) for c in _json(_get(row, "failure_codes"), [])]
    old_codes = today_codes(stored_codes, validate_codes(page, tour, brand_words, wv), wv)
    new_codes = today_codes(stored_codes, validate_codes(new_page, tour, brand_words, wv), wv)

    def _rec(p: dict, codes: list[str]) -> dict:
        inp = fc.s1_input_from_row(_page_row(row, p, codes), brand_forbidden_words=brand_words)
        return fc.check_s1(inp, exit=EXIT, actual="approved").record()

    old, new = _rec(page, old_codes), _rec(new_page, new_codes)
    stored = (md.get("final_contract") or {}).get("verdict")
    rank = VERDICT_RANK.get
    worse = (rank(new["verdict"], 9) > rank(old["verdict"], 9)
             or (stored in VERDICT_RANK and rank(new["verdict"], 9) > rank(stored, 9))
             or bool(set(blocks(new)) - set(blocks(old))))
    policy = any(f.get("code") == fc.ELEPHANT_RIDING for f in (old.get("findings") or []) + (new.get("findings") or []))
    return {"old": old["verdict"], "new": new["verdict"], "stored": stored, "worse": worse, "policy": policy,
            "old_blocks": blocks(old), "new_blocks": blocks(new), "new_failure_codes": new_codes}


__all__ = ["WRITER", "EXIT", "FIELDS", "VERDICT_RANK", "FACTS_WOULD_BE_LOST", "Recomposed", "recompose", "stored_items",
           "blocks_of", "narrative_of", "fact_keys", "clause_key", "keep_unstated_facts", "facts_lost", "contract_pair",
           "validate_codes", "today_codes", "no_model"]
