"""AA-768 (S234, PR5) — F9 v2 claim rules: the model types each sentence's claim and cites a
SOURCE line; this module applies the policy in code.

Why: F9 today is one judge call that classifies AND adjudicates with the policy written as prose.
S232 labelled its 127 flags: 48 real, 46 over-strict, 33 borderline — and all 46 over-strict flags
fall in categories the prompt already exempts (place background 25, paraphrase 14, brand self 4,
opinion 3). Adding the policy to the prompt did not move them. Meanwhile it passed trip promises,
social proof and details borrowed from other trips (S233 panel, 7/8 blogs).

F9 v2 splits the job:
1. `number_source_lines()` numbers and types every line of the gates' SOURCE (`service.
   _grounding_text`): S = this trip's own itinerary lines, L = location / naming, P = place-card
   background, O = other trips (siblings), F = Facts entries, B = brand, A = audience, T = trip /
   destination.
2. A cheap extraction call (not in this module) returns one row per piece sentence:
   {i, t (claim type), ref (source line id), q (≤12-word quote of that line), p (places)}.
   `build_extraction_prompt()` / `extraction_schema()` / `parse_rows()` are its pure halves.
3. `apply_rules()` maps type → allowed source kinds, verifies the quote (so a fabricated citation
   cannot pass), and runs deterministic tripwires on every sentence (so a trip promise typed
   'opinion' cannot pass either — the critic's category-leak risk).

S234 status: NOT wired into `run_quality_gates`. The offline replay
(`.tmp-session/s234/f9v2_replay.py`) decides whether it replaces the fact half of F9 in S235
(targets: real_kept ≥90%, over_strict_cleared ≥90%, 0 exempt-typed leaks).

C5 (plan): `hygiene.py` owns the scope lexicon, gazetteer, SOCIAL_PROOF_RE and the text
primitives; this module imports them. It never emits a scope verdict for the title or meta —
F12_scope does that once — and skips gazetteer places in the body for the same reason.

Pure: no DB, no LLM, no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from services.acp_content_writing.hygiene import (
    SIBLING_HEADER,
    SOCIAL_PROOF_RE,
    ScopeLexicon,
    _is_cta,
    build_scope_lexicon,
    content_words,
    fold,
    lev,
    phrase_tokens,
    places_named,
    proper_nouns,
    sentence_spans,
    strip_tags,
)

# ---------------------------------------------------------------- source lines

KIND_TRIP = "S"         # this trip's own itinerary: label, Day N, Source itinerary, Why it matters, Season
KIND_LOCATION = "L"     # Location / Naming
KIND_PLACE = "P"        # place-card background bullets (facts about the place, not the trip)
KIND_OTHER_TRIP = "O"   # "Other trips that include this same moment" bullets
KIND_FACT = "F"         # tenant / platform Facts entries
KIND_BRAND = "B"        # BRAND / BRAND CORE IDEA
KIND_AUDIENCE = "A"     # AUDIENCE — who the brand writes for, never a fact about this trip
KIND_TRIP_NAME = "T"    # TRIP / DESTINATION / SCOPE

# What the extraction call is shown. O and A are left out: they can never support a claim, and
# they are 10–14% of a blog seed (design f9-v2, "cost_effect").
SHOWN_KINDS = (KIND_TRIP, KIND_LOCATION, KIND_PLACE, KIND_FACT, KIND_BRAND, KIND_TRIP_NAME)

# compose_seed (atom_seed.py) line prefixes are a parsing contract — see the round-trip test.
_OLD_SIBLING_HEADER = "Other itineraries with the same moment"  # S232-era seeds
_BG_HEADER = "Place background"
_FACT_HEAD_RE = re.compile(r"^\[Fact id=([^\]]+)\]\s*$")
_MOMENT_HEAD_RE = re.compile(r"^\[Moment id=[^\]]+\]\s*$")
_LOCATION_NOTE_RE = re.compile(r"\s*\((?:do not|the itinerary calls it)[^)]*\)")
_SEED_PREFIXES = (
    ("Location:", KIND_LOCATION), ("Naming:", KIND_LOCATION),
    ("Source itinerary:", KIND_TRIP), ("Why it matters:", KIND_TRIP), ("Season:", KIND_TRIP),
)
_TOP_PREFIXES = (
    ("BRAND CORE IDEA:", KIND_BRAND), ("BRAND:", KIND_BRAND), ("AUDIENCE", KIND_AUDIENCE),
    ("TRIP:", KIND_TRIP_NAME), ("DESTINATION:", KIND_TRIP_NAME), ("SCOPE:", KIND_TRIP_NAME),
)
_SKIP_PREFIXES = ("Thin moment:",)


@dataclass(frozen=True)
class SourceLine:
    """One numbered SOURCE line. `place` is the Location of the seed block it belongs to (None for
    Facts and the brand/trip lines); `block` numbers the seed blocks (route moments) from 1."""
    id: str
    kind: str
    text: str
    place: Optional[str] = None
    block: int = 0
    fact_id: Optional[str] = None


def _location_place(line: str) -> str:
    return _LOCATION_NOTE_RE.sub("", line.split(":", 1)[1]).strip()


def number_source_lines(grounding_text: Optional[str]) -> list[SourceLine]:
    """Every non-empty line of the gates' SOURCE, typed and numbered per kind (S1, S2, …, P1, …).
    Paragraphs (blank-line separated) are seed blocks, `[Fact id=…]` entries, or the BRAND /
    AUDIENCE / TRIP / DESTINATION lines `_grounding_text` appends. In a seed block the first line
    (the label) and 'Day N …' lines are S; bullets under 'Place background' are P and under
    'Other trips…' (or the S232 'Other itineraries…') are O."""
    raw: list[tuple[str, str, Optional[str], int, Optional[str]]] = []
    block = 0
    for para in re.split(r"\n\s*\n", grounding_text or ""):
        lines = [ln.rstrip() for ln in para.strip("\n").split("\n") if ln.strip()]
        if not lines:
            continue
        m = _FACT_HEAD_RE.match(lines[0].strip())
        if m:
            body = " ".join(ln.strip() for ln in lines[1:])
            if body:
                raw.append((KIND_FACT, body, None, 0, m.group(1).strip()))
            continue
        top = next((k for p, k in _TOP_PREFIXES if lines[0].startswith(p)), None)
        if top:
            raw.append((top, " ".join(ln.strip() for ln in lines), None, 0, None))
            continue
        block += 1
        if _MOMENT_HEAD_RE.match(lines[0].strip()):
            lines = lines[1:]
        place = next((_location_place(ln) for ln in lines if ln.startswith("Location:")), None)
        mode = None
        for line in lines:
            if line.startswith(_BG_HEADER):
                mode = KIND_PLACE
                continue
            if line.startswith((SIBLING_HEADER, _OLD_SIBLING_HEADER)):
                mode = KIND_OTHER_TRIP
                continue
            if mode and line.startswith("- "):
                raw.append((mode, line[2:].strip(), place, block, None))
                continue
            mode = None
            if line.startswith(_SKIP_PREFIXES):
                continue
            kind = next((k for p, k in _SEED_PREFIXES if line.startswith(p)), KIND_TRIP)
            raw.append((kind, line.strip(), place, block, None))
    counters: dict[str, int] = {}
    out: list[SourceLine] = []
    for kind, text, place, blk, fact_id in raw:
        counters[kind] = counters.get(kind, 0) + 1
        out.append(SourceLine(f"{kind}{counters[kind]}", kind, text, place, blk, fact_id))
    return out


def scope_lexicon(source_lines: list[SourceLine], scope: Optional[str] = None,
                  trip_name: Optional[str] = None) -> ScopeLexicon:
    """hygiene's lexicon (C5) rebuilt from typed lines, so S232-era seeds (old sibling header)
    and route blocks read the same: own text = S/L/P/F lines, siblings = O lines."""
    own = [ln.text for ln in source_lines if ln.kind in (KIND_TRIP, KIND_LOCATION, KIND_PLACE, KIND_FACT)]
    sib = [ln.text for ln in source_lines if ln.kind == KIND_OTHER_TRIP]
    seed = "\n".join(own)
    if sib:
        seed += f"\n{SIBLING_HEADER} this same moment:\n" + "\n".join(f"- {s}" for s in sib)
    return build_scope_lexicon(seed, scope, trip_name)


# ---------------------------------------------------------------- piece sentences

@dataclass(frozen=True)
class PieceSentence:
    """`sid` is '1'…'n' for the body, 'T' for the SEO title and 'M' for the meta description."""
    sid: str
    where: str  # body | title | meta
    kind: str   # hygiene.sentence_spans kind, or 'title' / 'meta'
    text: str


def split_piece(content_text: Optional[str], seo_title: Optional[str] = None,
                meta: Optional[str] = None) -> list[PieceSentence]:
    """Numbered sentences of the piece (hygiene.sentence_spans, tags stripped; hashtag lines
    skipped; headings kept without their '#'), then the title and meta so the trip-promise rules
    cover them too."""
    out: list[PieceSentence] = []
    n = 0
    for span in sentence_spans(content_text or ""):
        if span["kind"] == "hashtags":
            continue
        text = span["clean"].lstrip("#").strip() if span["kind"] == "heading" else span["clean"]
        text = text.strip("* ").strip()
        if not text:
            continue
        n += 1
        out.append(PieceSentence(str(n), "body", span["kind"], text))
    for sid, where, value in (("T", "title", seo_title), ("M", "meta", meta)):
        clean = " ".join(strip_tags(value or "").split())
        if clean:
            out.append(PieceSentence(sid, where, where, clean))
    return out


# ---------------------------------------------------------------- tripwires

# Calibrated offline on the 127 S232 labels (.tmp-session/s234/f9v2_det_probe.py): together they
# hit 35/48 real violations and 11/46 over-strict flags.
TRIP_RE = re.compile(
    r"\b(you will|you'll|we will|we'll|includes?|included|guide[sd]?|transfer|hotel|lodge|camp|lunch|"
    r"dinner|breakfast|morning|afternoon|evening|pace[ds]?|pacing|private|access|arrange[sd]?|planned|"
    r"briefing|permit|visa|ticket|fee)\b", re.I)
SCENE_RE = re.compile(
    r"\b(steps?|stairs?|path|trail|road|worn|smell|scent|incense|sound|silence|quiet|light|shadow|"
    r"crowd|crowds|wind|cold|stone|dust|echo|bells?|prayer wheels?|flags?)\b", re.I)
CONTRAST_RE = re.compile(
    r"\bnot (just |merely |simply |only )?(a |an |the )?\w+[^.]{0,60}\b(but|—|;)|\brather than\b|\bunlike\b"
    r"|\bolder than\b|\bthe only\b|\bone of the (few|only)\b", re.I)
# What other travellers / itineraries do — not in hygiene.SOCIAL_PROOF_RE (which needs a verb of
# feeling/saying), but the same failure class: "Pawangka is not a stop that appears on every
# Ladakh itinerary." (S232 label 0).
RARITY_RE = re.compile(
    r"\b(most|many|few|some) (travell?ers|visitors|families|guests|people|tourists|parents|children)\b"
    r"|\btends? to (stay|sit|linger|be remembered)\b"
    r"|\b(visitors|travell?ers|guests|families) (often|usually|say|tell|describe|find|remember)\b"
    r"|\bwidely[- ]travell?ed\b|\b(rarely|seldom) (visited|seen|reached)\b|\bnever find their way\b"
    r"|\boff the beaten\b|\bappears on every\b|\bfew (itineraries|tours|trips)\b|\bunlike most\b", re.I)
REGULATORY_RE = re.compile(
    r"\b(visa|permits?|fees?|tariffs?|SDF|dress code|prices?|open|opens|closed|closes|hours)\b", re.I)
HYPOTHETICAL_RE = re.compile(r"\b(imagine|picture|if you|you might|you could|could|you may|perhaps)\b", re.I)
_OPTIONAL_CLAUSE_RE = re.compile(r"\boptional\b[^.;]*", re.I)
_OPTIONAL_SENT_RE = re.compile(r"\b(optional|optionally|can|could|may|might|if you (choose|wish|like|want))\b", re.I)

_WORD_VALUES = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000, "dozen": 12,
}
_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_UNITS = ("days?|hours?|minutes?|years?|metres?|meters?|km|kilomet(?:re|er)s?|nights?|dollars?|"
          "percent|per cent|feet|miles?|centur(?:y|ies)|storeys?|stor(?:y|ies)|monks?|steps?")
_NUMWORD = (rf"(?:{_TENS})(?:-(?:one|two|three|four|five|six|seven|eight|nine))?"
            r"|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
            r"sixteen|seventeen|eighteen|nineteen|hundred|thousand|dozen")
_WORDNUM_RE = re.compile(rf"\b({_NUMWORD})\b(?:[- ](?:US |U\.S\. )?({_UNITS})\b)?", re.I)
_RANGE_RE = re.compile(rf"\b({_NUMWORD})\s+(?:to|or|and)\s+({_NUMWORD})\b", re.I)
_DIGITS_RE = re.compile(r"(?<![\w.,])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def _word_value(word: str) -> int:
    parts = word.lower().split("-")
    return sum(_WORD_VALUES.get(p, 0) for p in parts)


def word_numbers(sentence: str) -> list[tuple[str, int, bool]]:
    """(phrase, value, has_unit) for every spelled-out number in `sentence`."""
    return [(m.group(0), _word_value(m.group(1)), bool(m.group(2)))
            for m in _WORDNUM_RE.finditer(strip_tags(sentence))]


def find_novel_word_numbers(sentence: str, source_texts: Iterable[str]) -> list[str]:
    """The spelled-out sibling of grounding.find_novel_numeric_claims (F1 sees digits only, so
    "a thirty-day e-visa … thirty to forty-five US dollars" passed it, S232). A word number counts
    only with a unit right after it ('thirty-day', 'forty-five US dollars') or as one end of a
    range ('thirty to forty-five'), and only when the SOURCE has neither the word nor its digits —
    a bare 'two monasteries' is left to the extraction call."""
    src = " ".join(source_texts)
    src_digits = {d.replace(",", "") for d in _DIGITS_RE.findall(src)}
    src_words = set(re.findall(r"[a-z]+(?:-[a-z]+)?", fold(src)))
    clean = strip_tags(sentence)
    in_range: set[int] = set()
    for m in _RANGE_RE.finditer(clean):
        in_range |= {m.start(1), m.start(2)}
    out = []
    for m in _WORDNUM_RE.finditer(clean):
        head, value = m.group(1), _word_value(m.group(1))
        if value <= 1 or not (m.group(2) or m.start() in in_range):  # 'one day' is idiom
            continue
        if str(value) in src_digits or fold(head) in src_words:
            continue
        out.append(m.group(0))
    return out


def mask_names(sentence: str, names: Iterable[str] = ()) -> str:
    """The sentence with its place names blanked, so 'Sod Camp' or 'Paro Private School' do not
    trip the TRIP / SCENE words ('camp', 'private'). Multi-word capitalised runs and the row's own
    place names go; a single capitalised word that opens the sentence stays ('Pacing is …')."""
    out = sentence
    for name in sorted(set(proper_nouns(sentence)) | {n for n in names if n}, key=len, reverse=True):
        out = re.sub(re.escape(name), " ", out, flags=re.I)
    return out


def _optional_overlap(line_text: str, sentence: str) -> bool:
    """Does the sentence talk about what the line lists as optional?"""
    words = {w for m in _OPTIONAL_CLAUSE_RE.finditer(line_text) for w in content_words(m.group(0))
             if len(w) >= _QUOTE_MIN_WORD and w != "optional"}
    return bool(words & set(content_words(sentence)))


def tripwires(sentence: str) -> set[str]:
    """Which deterministic tripwires a sentence hits: social | trip | scene | contrast | number |
    regulatory. ('number' = any digit or spelled-out number; novelty is checked separately.)"""
    hits = set()
    if SOCIAL_PROOF_RE.search(sentence) or RARITY_RE.search(sentence):
        hits.add("social")
    if TRIP_RE.search(sentence):
        hits.add("trip")
    if SCENE_RE.search(sentence):
        hits.add("scene")
    if CONTRAST_RE.search(sentence):
        hits.add("contrast")
    if _DIGITS_RE.search(strip_tags(sentence)) or word_numbers(sentence):
        hits.add("number")
    if REGULATORY_RE.search(sentence):
        hits.add("regulatory")
    return hits


# ---------------------------------------------------------------- quotes

_QUOTE_MIN_WORD = 4  # design said 5; "camp", "lake", "fort" are real anchors (S234 test)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", fold(strip_tags(text))).split())


def verify_quote(quote: Optional[str], line_text: str, sentence: str) -> bool:
    """A citation is real when its quote is in the cited line (accent-, case-, punctuation- and
    whitespace-folded) and shares at least one content word of 4+ letters with the sentence. So a
    model cannot cite 'S3' for a sentence S3 says nothing about."""
    q = _norm(quote or "")
    if len(q) < 3 or q not in _norm(line_text):
        return False
    q_words = {w for w in content_words(quote or "") if len(w) >= _QUOTE_MIN_WORD}
    return bool(q_words & set(content_words(sentence)))


# ---------------------------------------------------------------- rows and verdicts

CLAIM_TYPES = (
    "trip_fact", "service_promise", "place_background", "scene_detail", "comparison", "location",
    "number", "social_proof", "opinion", "brand_self", "hypothetical", "generic", "cta",
)
EXEMPT_TYPES = frozenset({"opinion", "brand_self", "generic", "cta"})


@dataclass(frozen=True)
class ClaimRow:
    """One extraction row: the sentence id, the model's claim type, the cited SOURCE line id, a
    short verbatim quote of that line, and the place names the sentence uses."""
    sid: str
    type: str = "generic"
    ref: str = ""
    quote: str = ""
    places: tuple = ()


@dataclass(frozen=True)
class Verdict:
    sid: str
    sentence: str
    declared: str           # the model's type ('' when no row came back)
    type: str               # the type the rules applied (may be re-typed by a tripwire)
    ok: bool
    blocking: bool
    problem: str = ""
    ref: str = ""


@dataclass(frozen=True)
class RulesConfig:
    """The two open product decisions (design f9-v2 G2 and 'uncited background'), as flags the
    replay measures. 'warn' passes the sentence with a non-blocking note; 'fail' blocks it."""
    hypothetical: str = "warn"
    uncited_background: str = "warn"


def parse_rows(data: Optional[dict], sentences: list[PieceSentence]) -> tuple[dict[str, ClaimRow], list[str]]:
    """Extraction JSON → {sid: ClaimRow} for the known sentence ids, plus the ids with no row
    (the caller re-asks once). Unknown types read as 'generic'; extra or duplicate rows are
    ignored (first wins)."""
    known = {s.sid for s in sentences}
    rows: dict[str, ClaimRow] = {}
    for r in (data or {}).get("rows") or []:
        if not isinstance(r, dict):
            continue
        sid = str(r.get("i", "")).strip().strip("[]#")
        if sid not in known or sid in rows:
            continue
        t = str(r.get("t") or "generic").strip()
        places = r.get("p") or []
        rows[sid] = ClaimRow(
            sid=sid, type=t if t in CLAIM_TYPES else "generic", ref=str(r.get("ref") or "").strip(),
            quote=str(r.get("q") or "").strip(),
            places=tuple(str(p).strip() for p in places if isinstance(p, str) and p.strip()),
        )
    return rows, [s.sid for s in sentences if s.sid not in rows]


def _snippet(text: str, n: int = 90) -> str:
    """A source line quoted inside a violation: no parentheses or semicolons, which would break
    sentence_repair._F9_CLAIM_RE."""
    s = re.sub(r"[();]", " ", text)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n].rstrip() + "…"


_KIND_PROBLEM = {
    KIND_OTHER_TRIP: "detail from another trip, {ref} — never promise it for this trip",
    KIND_PLACE: "place background, {ref}, is not a trip promise",
    KIND_AUDIENCE: "the audience line, {ref}, is not a fact about this trip",
    KIND_BRAND: "the brand line, {ref}, is not a fact about this trip",
    KIND_TRIP_NAME: "the trip name, {ref}, is not an itinerary line",
    KIND_LOCATION: "{ref} only says where the place is",
    KIND_FACT: "{ref} is a general fact, not this trip's itinerary",
}


def _in_scope(name: str, lexicon: ScopeLexicon) -> bool:
    toks = phrase_tokens(name)
    if not toks:
        return True
    allowed = lexicon.allowed

    def near(t: str) -> bool:
        limit = 2 if len(t) >= 7 else 1
        return any(abs(len(t) - len(a)) <= limit and lev(t, a) <= limit for a in allowed if " " not in a)

    return all(t in allowed or near(t) for t in toks)


def _places_with_background(source_lines: list[SourceLine]) -> list[set[str]]:
    """Token sets of the places that have P lines."""
    out = []
    for place in dict.fromkeys(ln.place for ln in source_lines if ln.kind == KIND_PLACE and ln.place):
        out.append(set(phrase_tokens(place)))
    return out


def _has_background(row_places: tuple, source_lines: list[SourceLine]) -> bool:
    with_bg = _places_with_background(source_lines)
    if not with_bg:
        return False
    if not row_places:
        return True  # some place has a card; an uncited background sentence should cite it
    for name in row_places:
        toks = set(phrase_tokens(name))
        if any(toks & bg for bg in with_bg):
            return True
    return False


@dataclass(frozen=True)
class _Ctx:
    by_id: dict
    lexicon: ScopeLexicon
    number_src: list
    source_lines: list
    cta: Optional[str]
    config: RulesConfig


PASS, WARN, FAIL = "pass", "warn", "fail"


def _cited_problem(line: Optional[SourceLine], ref: str, quoted: bool, allowed: tuple) -> Optional[str]:
    """None when the row cites an allowed kind with a verified quote; else the problem."""
    if line is None:
        return "no SOURCE line supports it"
    if line.kind not in allowed:
        return _KIND_PROBLEM.get(line.kind, "{ref} cannot support it").format(ref=ref)
    if not quoted:
        return f"the quote is not in {ref} — {_snippet(line.text)}"
    return None


def _decide(s: PieceSentence, row: ClaimRow, c: _Ctx) -> tuple[str, str, str]:
    """(applied type, pass|warn|fail, problem) for one sentence — the rule table."""
    t = row.type
    ref = row.ref.strip().upper()
    line = c.by_id.get(ref)
    quoted = line is not None and verify_quote(row.quote, line.text, s.text)
    hits = tripwires(mask_names(s.text, row.places))

    if _is_cta(s.text, c.cta):
        return "cta", PASS, ""  # the configured CTA itself; a model-typed 'cta' is exempt-typed
    novel = find_novel_word_numbers(s.text, c.number_src)
    if novel:
        return "number", FAIL, f"states {', '.join(novel)}, which the SOURCE does not give"
    if "social" in hits and (t == "social_proof" or t in EXEMPT_TYPES or not quoted):
        return "social_proof", FAIL, ("social proof or rarity claim — what other travellers or itineraries do "
                                      "is never in the SOURCE")
    if t == "hypothetical":
        if HYPOTHETICAL_RE.search(s.text):
            if c.config.hypothetical == "warn":
                return t, WARN, "imagined scene — check it stays plausible"
            return t, FAIL, "imagined scene the SOURCE does not give"
        t = "scene_detail"

    if t == "trip_fact":
        problem = _cited_problem(line, ref, quoted, (KIND_TRIP,))
        if problem is None and _optional_overlap(line.text, s.text) and not _OPTIONAL_SENT_RE.search(s.text):
            problem = f"{ref} lists it as optional — the sentence presents it as included"
        return t, (FAIL if problem else PASS), (f"trip promise: {problem}" if problem else "")
    if t == "service_promise":
        problem = _cited_problem(line, ref, quoted, (KIND_TRIP, KIND_FACT))
        return t, (FAIL if problem else PASS), (f"service promise: {problem}" if problem else "")
    if t == "place_background":
        problem = _cited_problem(line, ref, quoted, (KIND_PLACE, KIND_FACT, KIND_LOCATION, KIND_TRIP))
        if problem is None:
            return t, PASS, ""
        if line is not None and line.kind in (KIND_OTHER_TRIP, KIND_AUDIENCE):
            return t, FAIL, problem
        has_card = _has_background(row.places, c.source_lines)
        if c.config.uncited_background == "warn" and not has_card \
                and not ({"number", "regulatory", "scene", "trip"} & hits):
            return t, WARN, "uncited background about a place with no place card — check it"
        return t, FAIL, ("background not in the place background — cite a P line or cut it" if has_card else
                         "background with a number, a rule or a scene detail the SOURCE does not give")
    if t in ("scene_detail", "comparison"):
        problem = _cited_problem(line, ref, quoted, (KIND_TRIP, KIND_PLACE, KIND_FACT))
        label = "scene detail" if t == "scene_detail" else "comparison"
        return t, (FAIL if problem else PASS), (f"{label}: {problem}" if problem else "")
    if t == "location":
        if s.where != "body":
            return t, PASS, ""  # title / meta scope is F12_scope's (C5)
        # a gazetteer place (sibling or trip-name place) is F12_scope's too
        bad = [n for n in row.places if not places_named(c.lexicon, n) and not _in_scope(n, c.lexicon)]
        if bad:
            return t, FAIL, f"names {', '.join(repr(b) for b in bad)}, a place the SOURCE never puts this moment in"
        return t, PASS, ""
    if t == "social_proof":
        return t, FAIL, "social proof — what other travellers do is never in the SOURCE"
    if t == "number":
        return t, PASS, ""  # digits are F1's; spelled-out numbers were checked above
    # exempt: opinion, brand_self, generic, a model-typed cta
    if line is not None and quoted and line.kind in (KIND_TRIP, KIND_FACT, KIND_PLACE):
        return t, PASS, ""
    if "trip" in hits:
        return "uncited_trip", FAIL, f"reads as a trip promise with no itinerary line behind it, typed {t}"
    if {"scene", "contrast"} & hits:
        kind = "scene detail" if "scene" in hits else "contrast"
        return t, WARN, f"{kind} in a sentence typed {t} — check it"
    return t, PASS, ""


def apply_rules(rows: dict[str, ClaimRow], source_lines: list[SourceLine], sentences: list[PieceSentence], *,
                lexicon: Optional[ScopeLexicon] = None, cta: Optional[str] = None,
                config: RulesConfig = RulesConfig()) -> list[Verdict]:
    """One verdict per sentence. A sentence with no row is read as 'generic' (the tripwires
    decide), which is also the deterministic-only mode: `apply_rules({}, …)`.

    Order: a novel spelled-out number fails; social proof / rarity fails whatever the type; then
    the type's rule. Exempt types (opinion, brand_self, generic, cta) are re-typed and fail on a
    TRIP tripwire, warn on SCENE / CONTRAST. Only a sentence that really is the configured CTA
    (hygiene's ≥60% CTA-word rule) skips the tripwires — a model's 'cta' label alone does not."""
    c = _Ctx(by_id={ln.id: ln for ln in source_lines}, lexicon=lexicon or scope_lexicon(source_lines),
             number_src=[ln.text for ln in source_lines if ln.kind != KIND_OTHER_TRIP],
             source_lines=source_lines, cta=cta, config=config)
    out: list[Verdict] = []
    for s in sentences:
        row = rows.get(s.sid)
        declared = row.type if row else ""
        row = row or ClaimRow(s.sid)
        vtype, status, problem = _decide(s, row, c)
        ref = row.ref.strip().upper()
        out.append(Verdict(s.sid, s.text, declared, vtype, ok=status != FAIL, blocking=status == FAIL,
                           problem=problem, ref=ref if ref in c.by_id else ""))
    return out


_VIOLATION_HEAD = "audit flagged: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "


def format_violations(verdicts: list[Verdict], *, blocking: bool = True) -> list[str]:
    """The failing verdicts in F9's existing violation shape, so sentence_repair.plan and the
    admin highlights keep parsing them: one string, '"<sentence>" (claims: <type>; source:
    <problem>)' joined by '; '. `blocking=False` formats the warn verdicts instead."""
    if blocking:
        picked = [v for v in verdicts if not v.ok and v.blocking]
    else:
        picked = [v for v in verdicts if v.ok and v.problem]
    if not picked:
        return []
    parts = [f'"{v.sentence}" (claims: {v.type}; source: {_snippet(v.problem, 200)})' for v in picked]
    return [_VIOLATION_HEAD + "; ".join(parts)]


# ---------------------------------------------------------------- extraction prompt (pure halves)

EXTRACTION_SYSTEM = (
    "You label the claims in a travel piece. You do NOT decide whether a claim is true or "
    "supported; code does that from your labels. For EVERY numbered sentence return exactly one "
    "row: i = the sentence id; t = its main claim type; ref = the id of the ONE SOURCE line that "
    "states it (empty if none does); q = up to 12 words copied verbatim from that SOURCE line; "
    "p = every place name the sentence uses.\n"
    "Types: trip_fact (what this trip does, includes, visits, when or how); service_promise "
    "(guides, transport, pacing, comfort, safety, arrangements); place_background (what a place is, "
    "its history or role); scene_detail (what a place looks, sounds or smells like, its paths, "
    "crowds, light); comparison (contrast or ranking against other places or trips); location "
    "(where a place is); number (a figure, date, duration or price); social_proof (what other "
    "travellers, families or itineraries do or feel); opinion (the writer's view, mood or "
    "rhetoric); brand_self (the brand describing itself); hypothetical (an imagined scene: "
    "'imagine', 'picture', 'you might'); generic (no checkable claim); cta (the call to action).\n"
    "Cite only a line that really states the claim. Never invent a quote."
)


def extraction_schema() -> dict:
    """LLMRequest.json_schema for the extraction call (Converse forced tool / OpenAI strict)."""
    return {
        "name": "claim_rows",
        "schema": {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "i": {"type": "string"},
                            "t": {"type": "string", "enum": list(CLAIM_TYPES)},
                            "ref": {"type": "string"},
                            "q": {"type": "string"},
                            "p": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["i", "t", "ref", "q", "p"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["rows"],
            "additionalProperties": False,
        },
    }


def build_extraction_prompt(source_lines: list[SourceLine], sentences: list[PieceSentence]) -> str:
    """The user prompt: the numbered SOURCE first (stable prefix for the provider's prompt cache;
    O and A lines left out), then the numbered PIECE sentences."""
    src = "\n".join(f"{ln.id}: {ln.text}" for ln in source_lines if ln.kind in SHOWN_KINDS)
    piece = "\n".join(f"[{s.sid}] {s.text}" for s in sentences)
    return (f"SOURCE (numbered lines):\n{src}\n\n"
            f"PIECE (numbered sentences; T = SEO title, M = meta description):\n{piece}\n\n"
            f"Return one row for each of these {len(sentences)} sentence ids.")


# ---------------------------------------------------------------- deterministic CTA (unused in S234)

_CTA_MIN_SHARE = 0.8
_CTA_TAIL_SHARE = 0.35
_FAQ_HEAD_RE = re.compile(r"^#{1,6}\s*faq\b", re.I | re.M)


@dataclass(frozen=True)
class CtaCheck:
    ok: bool
    repairable: bool = True
    problem: str = ""
    matched: tuple = field(default_factory=tuple)


def _stem4(word: str) -> str:
    return word[:4] if len(word) >= 4 else word


def check_cta(content_text: Optional[str], cta: Optional[str], channel: Optional[str] = None) -> CtaCheck:
    """Replaces the LLM's CTA_NOT_REFLECTED (design f9-v2 item 4; S232 e2484582 failed on a
    reworded CTA). Pass when ≥80% of the CTA's content words (4-letter stems, so 'plan' ==
    'planning') appear in the closing part of the prose: the last 35% before a FAQ heading (blog),
    or the last two paragraphs or last 35%, whichever is longer (social). Hashtag lines and the FAQ
    never count. No CTA configured → fail, not repairable (as F6 today)."""
    want = {_stem4(w) for w in content_words(cta or "")}
    if not want:
        return CtaCheck(False, repairable=False, problem="no CTA configured for this piece")
    text = content_text or ""
    m = _FAQ_HEAD_RE.search(text)
    prose = text[:m.start()] if m else text
    lines = [ln for ln in prose.rstrip().split("\n")
             if not re.fullmatch(r"(?:#[A-Za-z][A-Za-z0-9]+[\s,]*)+", ln.strip() or "-")]
    prose = "\n".join(lines).rstrip()
    start = int(len(prose) * (1 - _CTA_TAIL_SHARE))
    if (channel or "").lower() != "blog":
        paras = [p for p in re.split(r"\n\s*\n", prose) if p.strip()]
        if len(paras) >= 2:
            start = min(start, prose.rfind(paras[-2]))
    tail = prose[max(0, start):]
    have = {_stem4(w) for w in content_words(tail)}
    matched = want & have
    if len(matched) / len(want) >= _CTA_MIN_SHARE:
        return CtaCheck(True, matched=tuple(sorted(matched)))
    missing = sorted(want - have)
    return CtaCheck(False, problem=f"CTA not found in the closing text — missing {', '.join(missing)}",
                    matched=tuple(sorted(matched)))


__all__ = [
    "CLAIM_TYPES", "ClaimRow", "CtaCheck", "EXEMPT_TYPES", "EXTRACTION_SYSTEM", "PieceSentence", "RulesConfig",
    "SHOWN_KINDS", "SourceLine", "Verdict", "apply_rules", "build_extraction_prompt", "check_cta", "mask_names",
    "extraction_schema", "find_novel_word_numbers", "format_violations", "number_source_lines", "parse_rows",
    "scope_lexicon", "split_piece", "tripwires", "verify_quote", "word_numbers",
]
