"""AA-1047 (Quality W1, root ADR 0020 "Four sources of truth") — the FactSheet: one schema, two builders, one render.

ADR 0020 makes the FactSheet the owner of a piece's facts: per-line ids for what the source states, each with its
provenance, read alike by the writer, the checks, the repair step and the eval judge. Today two builders feed the
writers, each with its own text:

* the **tour builder** (S1 A1 Master, T2 tenant tour) = the raw source tour -> ``s1_facts.build_fact_table`` ->
  ``s1_writer_v2.render_fact_sheet`` (the S1 writer v2 FACT SHEET);
* the **route builder** (T9 blog / social) = the seed (atom or route moments) + the Facts block + the brand / audience
  / trip / destination lines -> ``service.grounding_text_from`` (what F1, F9, F17 / F18 and repair read as SOURCE).

This module wraps both as one ``FactSheet`` with typed, numbered lines. It is a **parity refactor**: ``render()`` is
byte-equal to the text each builder produced before (tests/unit/quality/test_aa1047_factsheet.py runs the frozen
legacy implementations over fixtures and generated inputs). Nothing a writer or a gate reads changes.

Lines carry:

* ``id``: stable within the sheet. Route lines keep the claim_rules ids (``S1``, ``P2``, ``F1`` ...: the F9 v2
  extraction already cites them); tour lines are ``<section>.<n>`` (``days.2``, ``included.4``, ``numbers.11``).
* ``kind``: the claim_rules letter (route) or the fact-table section (tour).
* ``provenance`` (ADR 0020): ``source_raw`` (the supplier's own text, verbatim), ``human_verified`` (a staff-written
  platform Facts entry; a human-edited Master later), ``tenant_asserted`` (the agency's own facts, brand and audience
  lines: support only for claims about the agency), ``llm_derived`` (atoms, place cards, the Master trip name: never
  the sole support that clears a claim), ``external`` (data bought from a provider, e.g. DataForSEO; no builder emits
  it yet).
* ``anchored`` (route seed lines): True / False when the caller knows whether the atom behind the line overlaps its
  raw source (``anchor_check``), None when unknown. An unanchored atom is framing only: never the sole support for a
  PASS (critic fix F10).
* ``field`` / ``quote`` (tour lines): the verbatim pointer into the source field (``s1_facts.verify_pointers``).

Exclusions are recorded, never silent: ``excluded`` lists what the builder's inputs offered and the sheet left out
(the T9 facts budget, the S1 figures cap), each with a reason. ``render_budgeted`` (the declared diff of W1 item 2)
drops whole lines in a fixed priority order when a reader has a size cap, and records each one: the eval judge's
16000-character cut is the reader this replaces.

Pure: no DB, no LLM, no I/O. Persistence: ``services.quality.factsheet_store``.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional, Sequence

SCHEMA = "factsheet/1"
# Bumped when a builder changes what lines it emits (the persisted fs_sha moves with it).
BUILDER_VERSION = "1"

BUILDER_TOUR = "tour"
BUILDER_ROUTE = "route"
BUILDERS = (BUILDER_TOUR, BUILDER_ROUTE)

SOURCE_RAW = "source_raw"
HUMAN_VERIFIED = "human_verified"
TENANT_ASSERTED = "tenant_asserted"
LLM_DERIVED = "llm_derived"
EXTERNAL = "external"
PROVENANCES = (SOURCE_RAW, HUMAN_VERIFIED, TENANT_ASSERTED, LLM_DERIVED, EXTERNAL)

# Exclusion reasons (FactSheet.excluded[].reason).
EXCLUDED_FACTS_BUDGET = "facts_budget"        # acp_content_writing.facts.budget_facts left the fact out of the seed
EXCLUDED_FIGURES_CAP = "figures_cap"          # s1_writer_v2.figures stops at MAX_FIGURES
EXCLUDED_READER_BUDGET = "reader_budget"      # render_budgeted dropped the line for a reader's size cap

# The anchor rule (critic fix F10, design "S0 FactSheet(route builder)"): an atom is anchored when its evidence (the
# "Source itinerary" text, else the atom label) is a substring of its tour's raw source, or when at least
# ANCHOR_MIN_SHARE of its content words (and at least ANCHOR_MIN_WORDS of them) occur in the raw source.
ANCHOR_MIN_SHARE = 0.6
ANCHOR_MIN_WORDS = 2


@dataclass(frozen=True)
class FactLine:
    id: str
    kind: str
    text: str
    provenance: str
    anchored: Optional[bool] = None
    field: Optional[str] = None
    quote: Optional[str] = None
    ref: Optional[str] = None
    block: int = 0
    place: Optional[str] = None

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None and not (k == "block" and v == 0)}


@dataclass(frozen=True)
class FactSheet:
    builder: str
    lines: tuple[FactLine, ...]
    excluded: tuple[dict, ...] = ()
    pointer_failures: tuple[dict, ...] = ()
    meta: dict = field(default_factory=dict)
    # The rendered text the writer and the checks read (parity with the legacy builders).
    text: str = ""

    def render(self) -> str:
        return self.text

    def record(self) -> dict:
        """The persisted shape (acp_shared.fact_sheet.lines / excluded / meta): canonical, JSON-safe."""
        return {"schema": SCHEMA, "builder": self.builder, "builder_version": BUILDER_VERSION,
                "lines": [ln.as_dict() for ln in self.lines], "excluded": [dict(e) for e in self.excluded],
                "pointer_failures": [dict(p) for p in self.pointer_failures], "meta": dict(self.meta)}

    def sha(self) -> str:
        """fs_sha: sha256 of the canonical JSON of the record plus the rendered text. Two builds of the same inputs give
        the same sha (the store reuses the row); any change in a line, an exclusion or the text moves it."""
        body = {**self.record(), "text": self.text}
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                         default=str).encode("utf-8")).hexdigest()

    def by_provenance(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for ln in self.lines:
            out[ln.provenance] = out.get(ln.provenance, 0) + 1
        return out

    def unanchored(self) -> list[FactLine]:
        return [ln for ln in self.lines if ln.anchored is False]

    def render_budgeted(self, max_chars: int) -> tuple[str, list[dict]]:
        """The declared diff of W1 item 2: the text for a reader with a size cap. Over the cap, whole lines go in a
        fixed order (lowest tier first, last line first within a tier) and each one is recorded with reason
        ``reader_budget``; a line is never cut. Route sheets: tier 2 = other trips (O) and audience (A), tier 1 =
        place background (P); S, F, T, L and B are never dropped (if the text is still over the cap the result says
        so with ``over_cap``). Tour sheets are returned whole (their reader, the S1 writer, has no cap)."""
        text = self.text
        if len(text) <= max_chars or self.builder != BUILDER_ROUTE:
            return text, []
        order = [ln for tier in _DROP_TIERS for ln in reversed([x for x in self.lines if x.kind in tier])]
        dropped: list[FactLine] = []
        for ln in order:
            if len(text) <= max_chars:
                break
            dropped.append(ln)
            text = _route_text_without(self.text, dropped)
        excluded = [{"id": ln.id, "kind": ln.kind, "reason": EXCLUDED_READER_BUDGET, "chars": len(ln.text)}
                    for ln in dropped]
        if len(text) > max_chars:
            excluded.append({"id": None, "kind": None, "reason": "over_cap", "chars": len(text) - max_chars})
        return text, excluded


# ------------------------------------------------------------------ anchors


class RawIndex:
    """The folded text and the content words of one tour's raw source, built once and reused for all its atoms."""

    def __init__(self, raw_text: Optional[str]):
        from services.acp_content_writing.hygiene import content_words, fold

        self.empty = not (raw_text or "").strip()
        self.norm = " ".join(fold(raw_text or "").split())
        self.words = set(content_words(raw_text or ""))


def anchor_check(evidence: Optional[str], label: Optional[str], raw_text) -> Optional[bool]:
    """True when an atom's evidence (else its label) is grounded in the tour's raw source (a text or a RawIndex);
    None when there is no raw text to check against. See ANCHOR_MIN_SHARE."""
    from services.acp_content_writing.hygiene import content_words, fold

    raw = raw_text if isinstance(raw_text, RawIndex) else RawIndex(raw_text)
    if raw.empty:
        return None
    probe = (evidence or "").strip() or (label or "").strip()
    if not probe:
        return False
    norm = " ".join(fold(probe).split())
    if norm and norm in raw.norm:
        return True
    words = set(content_words(probe))
    if not words:
        return False
    hits = words & raw.words
    return len(hits) >= min(ANCHOR_MIN_WORDS, len(words)) and len(hits) / len(words) >= ANCHOR_MIN_SHARE


# ------------------------------------------------------------------ route builder (T9)

# claim_rules kind -> provenance for the lines of a route sheet. F lines are resolved per entry (agency / platform).
_ROUTE_PROVENANCE = {"S": LLM_DERIVED, "L": LLM_DERIVED, "P": LLM_DERIVED, "O": LLM_DERIVED,
                     "B": TENANT_ASSERTED, "A": TENANT_ASSERTED}
# render_budgeted drop order: the first tier goes first.
_DROP_TIERS = (("O", "A"), ("P",))


def route_parts(*, seed: str, facts_text: Optional[str], brand_audience: Optional[dict], trip_name: Optional[str],
                destination: Optional[str]) -> list[str]:
    """The blocks of a T9 SOURCE, in the order service.grounding_text_from always joined them (AA-768 / AA-771 /
    S234 C11): seed, Facts block, BRAND, BRAND CORE IDEA, AUDIENCE, TRIP, DESTINATION."""
    parts = [seed, facts_text or ""]
    audience = brand_audience or {}
    if audience.get("brand_name"):
        parts.append(f"BRAND: {audience['brand_name']}")
    # AA-771 — the brand describing itself in its own words is a source, not an invented claim
    # (S232: 4 F9 flags on the brand's own stated approach were over-strict).
    if audience.get("core_idea"):
        parts.append(f"BRAND CORE IDEA: {audience['core_idea']}")
    if audience.get("customer_segment"):
        # S234 (C11) — kept for F1 (the brand's own '7–16'), labelled so the F9 judge stops taking
        # the segment as trip truth (S233: FAQ age answers passed on it).
        parts.append(f"AUDIENCE (who the brand writes for — not a fact about this trip): "
                     f"{audience['customer_segment']}")
    if trip_name:
        parts.append(f"TRIP: {trip_name}")
    if destination:
        parts.append(f"DESTINATION: {destination}")
    return parts


def _fact_owners(facts_text: Optional[str]) -> dict[str, str]:
    """fact_id -> 'agency' | 'platform' from the "Source: …; agency fact" line facts._fact_entry writes."""
    out: dict[str, str] = {}
    for para in re.split(r"\n\s*\n", facts_text or ""):
        m = re.match(r"\s*\[Fact id=([^\]]+)\]", para)
        if not m:
            continue
        owner = re.search(r"^Source: .*\b(agency|platform) fact\s*$", para, re.M)
        if owner:
            out[m.group(1).strip()] = owner.group(1)
    return out


def _fact_provenance(fact_id: Optional[str], owners: dict[str, str], scopes: dict[str, str]) -> str:
    scope = scopes.get(fact_id or "") or {"agency": "tenant", "platform": "platform"}.get(owners.get(fact_id or ""))
    # A platform fact is written and dated by AA staff (AA-814 /admin/facts); a tenant fact is the agency's word.
    # Unknown owner: the weaker reading.
    return HUMAN_VERIFIED if scope == "platform" else TENANT_ASSERTED


def _fact_chars(fact: dict) -> int:
    if fact.get("chars") is not None:
        return int(fact["chars"])
    return len(str(fact.get("title") or "")) + len(str(fact.get("body") or ""))


def excluded_fact_refs(facts: Sequence[dict]) -> list[dict]:
    """The compact, JSON-safe form of facts rows the budget left out (what a T9 context carries)."""
    return [{"fact_id": str(f.get("fact_id")), "scope": f.get("scope"), "chars": _fact_chars(f)} for f in facts]


def _trip_line_provenance(text: str) -> str:
    # DESTINATION is the tour's country (raw_tours.country); TRIP is the Master's trip name (S1 output); SCOPE is code
    # built from the atoms.
    if text.startswith("DESTINATION:"):
        return SOURCE_RAW
    return LLM_DERIVED


def build_route_factsheet(*, seed: str, facts_text: Optional[str], brand_audience: Optional[dict],
                          trip_name: Optional[str], destination: Optional[str],
                          fact_scopes: Optional[dict[str, str]] = None,
                          excluded_facts: Sequence[dict] = (),
                          block_anchored: Optional[Sequence[Optional[bool]]] = None,
                          meta: Optional[dict] = None) -> FactSheet:
    """The T9 sheet. ``render()`` == the legacy ``grounding_text_from`` for the same five arguments.

    ``fact_scopes`` (fact_id -> 'tenant' | 'platform') overrides the owner read from each entry's Source line.
    ``excluded_facts``: the facts rows the facts budget left out (``facts.fetch_facts_for_writing(excluded_out=)``),
    recorded with reason ``facts_budget``. ``block_anchored``: per seed block (route moment) in order, the
    ``anchor_check`` result of its atom; its S and L lines carry it."""
    from services.acp_content_writing.claim_rules import number_source_lines

    text = "\n\n".join(p for p in route_parts(seed=seed, facts_text=facts_text, brand_audience=brand_audience,
                                              trip_name=trip_name, destination=destination) if p)
    owners = _fact_owners(facts_text)
    scopes = {str(k): str(v) for k, v in (fact_scopes or {}).items()}
    anchors = list(block_anchored or [])
    lines = []
    for sl in number_source_lines(text):
        if sl.kind == "F":
            prov = _fact_provenance(sl.fact_id, owners, scopes)
        elif sl.kind == "T":
            prov = _trip_line_provenance(sl.text)
        else:
            prov = _ROUTE_PROVENANCE.get(sl.kind, LLM_DERIVED)
        anchored = None
        if sl.kind in ("S", "L") and 0 < sl.block <= len(anchors):
            anchored = anchors[sl.block - 1]
        lines.append(FactLine(id=sl.id, kind=sl.kind, text=sl.text, provenance=prov, anchored=anchored,
                              ref=sl.fact_id, block=sl.block, place=sl.place))
    excluded = tuple({"id": f"fact:{f.get('fact_id')}", "kind": "F", "reason": EXCLUDED_FACTS_BUDGET,
                      "scope": f.get("scope"), "chars": _fact_chars(f)} for f in excluded_facts)
    return FactSheet(builder=BUILDER_ROUTE, lines=tuple(lines), excluded=excluded, meta=dict(meta or {}), text=text)


def _route_text_without(text: str, dropped: Iterable[FactLine]) -> str:
    """``text`` with the dropped lines removed, read the way claim_rules.number_source_lines numbers them: an
    AUDIENCE paragraph goes whole; a P / O bullet goes from its own seed block, and a header left with no bullet goes
    with it."""
    drop = list(dropped)
    drop_top = {ln.text for ln in drop if ln.kind == "A"}
    drop_bullets: dict[int, list[str]] = {}
    for ln in drop:
        if ln.kind in ("P", "O"):
            drop_bullets.setdefault(ln.block, []).append(ln.text)
    from services.acp_content_writing.claim_rules import _FACT_HEAD_RE, _TOP_PREFIXES

    pieces = re.split(r"(\n\s*\n)", text)
    out: list[str] = []
    block = 0
    for i in range(0, len(pieces), 2):
        para = pieces[i]
        sep = pieces[i + 1] if i + 1 < len(pieces) else ""
        lines = [ln.rstrip() for ln in para.strip("\n").split("\n") if ln.strip()]
        if not lines:
            out.append(para + sep)
            continue
        if _FACT_HEAD_RE.match(lines[0].strip()):
            out.append(para + sep)
            continue
        if any(lines[0].startswith(p) for p, _k in _TOP_PREFIXES):
            joined = " ".join(ln.strip() for ln in lines)
            if joined in drop_top:
                continue
            out.append(para + sep)
            continue
        block += 1
        gone = list(drop_bullets.get(block, ()))
        if not gone:
            out.append(para + sep)
            continue
        kept: list[str] = []
        for raw in para.split("\n"):
            s = raw.rstrip()
            if s.startswith("- ") and s[2:].strip() in gone:
                gone.remove(s[2:].strip())
                continue
            kept.append(raw)
        out.append("\n".join(_drop_empty_headers(kept)) + sep)
    return _strip_trailing_blank_lines("".join(out))


def _strip_trailing_blank_lines(text: str) -> str:
    """Same result as ``re.sub(r"(\\n\\s*\\n)+$", "", text)`` in linear time (CodeQL py/redos on the regex): a
    whitespace tail that ends with a newline and holds at least two of them is cut from its first newline."""
    body = text.rstrip()
    tail = text[len(body):]
    if tail.endswith("\n") and tail.count("\n") >= 2:
        return body + tail[:tail.index("\n")]
    return text


def _drop_empty_headers(lines: list[str]) -> list[str]:
    from services.acp_content_writing.hygiene import SIBLING_HEADER

    headers = ("Place background", SIBLING_HEADER, "Other itineraries with the same moment")
    out: list[str] = []
    for i, raw in enumerate(lines):
        if raw.startswith(headers):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if not nxt.startswith("- "):
                continue
        out.append(raw)
    return out


# ------------------------------------------------------------------ tour builder (S1, T2)

# fact-table section -> FactLine kind; derived sections keep their name.
_TOUR_SECTIONS = ("days", "schedule", "places", "numbers", "included", "excluded", "optional", "do_not_use")


def build_tour_factsheet(tour: dict, *, agency: Optional[dict] = None, provenance: str = SOURCE_RAW,
                         meta: Optional[dict] = None) -> FactSheet:
    """The S1 / T2 sheet from one raw source tour (the S1 source dict: name, subtitle, summary, description,
    highlights, itineraries, country, duration, price, inclusions, exclusions) and its agency fields (raw_tours
    group_size / best_time_to_go, ``s1_writer_v2.with_agency_fields``). ``render()`` == the legacy
    ``render_fact_sheet(t, facts, day_plan(t, facts))`` of the S1 writer v2. Every line points into the source field
    it came from; ``pointer_failures`` = ``verify_pointers`` (the W1 bar: empty on every active source)."""
    from services.content_generation import s1_writer_v2 as v2
    from services.quality.s1_facts import build_fact_table, verify_pointers

    if provenance not in PROVENANCES:
        raise ValueError(f"unknown provenance {provenance!r}")
    t = v2.with_agency_fields(tour or {}, agency)
    facts = build_fact_table(t)
    plan = v2.day_plan(t, facts)
    text = v2.render_fact_sheet(t, facts, plan)
    lines: list[FactLine] = []
    for section in _TOUR_SECTIONS:
        for n, e in enumerate(facts.get(section) or [], start=1):
            lines.append(FactLine(id=f"{section}.{n}", kind=section, text=str(e["value"]), provenance=provenance,
                                  field=e.get("field"), quote=e.get("quote")))
    dur = facts["duration"]
    lines.append(FactLine(id="duration.1", kind="duration", text=str(dur.get("value")), provenance=provenance,
                          quote="; ".join(q["quote"] for q in dur.get("quotes") or []) or None))
    for n, c in enumerate(facts.get("conflicts") or [], start=1):
        lines.append(FactLine(id=f"conflicts.{n}", kind="conflicts", text=str(c.get("detail") or c["value"]),
                              provenance=provenance))
    for n, key in enumerate(facts.get("missing") or [], start=1):
        lines.append(FactLine(id=f"missing.{n}", kind="missing", text=str(key), provenance=provenance))
    shown = set(v2.figures(facts))
    excluded = []
    fig_rows = [(n, e) for n, e in enumerate(facts.get("numbers") or [], start=1)
                if e.get("role", "figure") == "figure"]
    for n, e in fig_rows:
        span = (" ".join([e["value"], *(e.get("words") or [])[:2]]).strip() if not e.get("unit") else e["value"])
        if span not in shown:
            excluded.append({"id": f"numbers.{n}", "kind": "numbers", "reason": EXCLUDED_FIGURES_CAP,
                             "chars": len(span)})
    return FactSheet(builder=BUILDER_TOUR, lines=tuple(lines), excluded=tuple(excluded),
                     pointer_failures=tuple(verify_pointers(facts, t)),
                     meta={"day_plan": plan["mode"], "days": len(plan["days"]), **(meta or {})}, text=text)


def raw_source_tour(row) -> tuple[dict, dict]:
    """(S1 source dict, agency dict) from a row that carries the raw_tours source columns (src_name … exclusions,
    country, duration, price_raw, group_size, best_time_to_go): the S1 run_tour row, the T2 ``_PT_SQL`` row and the
    measurement queries alike. A jsonb list may arrive as its JSON text (no codec on most connections)."""
    def get(key):
        try:
            return row.get(key) if hasattr(row, "get") else row[key]
        except (KeyError, IndexError):
            return None

    hl = get("src_highlights")
    if isinstance(hl, str):
        try:
            parsed = json.loads(hl) if hl.strip() else []
            hl = parsed if isinstance(parsed, list) else hl
        except ValueError:
            pass
    tour = {"name": get("src_name"), "subtitle": get("src_subtitle"), "summary": get("src_summary"),
            "description": get("src_description"), "highlights": hl if hl is not None else [],
            "itineraries": get("src_itineraries"), "country": get("country"), "duration": get("duration"),
            "price": get("price_raw"), "inclusions": get("inclusions"), "exclusions": get("exclusions")}
    agency = {"group_size": get("group_size"), "best_time_to_go": get("best_time_to_go")}
    return tour, agency


def raw_source_text(tour: dict, agency: Optional[dict] = None) -> str:
    """The raw source of one tour as one text (every S1 source field, agency lines included): what anchor_check
    reads."""
    from services.content_generation import s1_writer_v2 as v2
    from services.quality.s1_facts import source_fields

    return "\n".join(source_fields(v2.with_agency_fields(tour or {}, agency)).values())


__all__ = [
    "ANCHOR_MIN_SHARE", "ANCHOR_MIN_WORDS", "BUILDERS", "BUILDER_ROUTE", "BUILDER_TOUR", "BUILDER_VERSION",
    "EXCLUDED_FACTS_BUDGET", "EXCLUDED_FIGURES_CAP", "EXCLUDED_READER_BUDGET", "EXTERNAL", "FactLine", "FactSheet",
    "HUMAN_VERIFIED", "LLM_DERIVED", "PROVENANCES", "SCHEMA", "SOURCE_RAW", "TENANT_ASSERTED", "anchor_check",
    "RawIndex", "build_route_factsheet", "build_tour_factsheet", "excluded_fact_refs", "raw_source_text",
    "raw_source_tour", "route_parts",
]
