"""S1-first step 1 (M2a, S248) — the code checks for one written S1 page against its source. Pure, $0, no LLM.

The S246 advisory (§4 point 7, q4-q8 §2.2 step 0) limits code to what code can decide: day structure, figures,
names outside the source, a cut meta, inclusions the source contradicts. Meaning-level faults (an invented promise,
a wrong direction, a number put on the wrong place) stay with the panel. Every check here reuses the function the
pipeline already runs for that question, so a page is judged the same way offline and online:

| code | class | reuses |
|---|---|---|
| DAY_* / SOURCE_DAY_CONTRADICTION | day structure | `services/quality/day_oracle.check` (AA-925) |
| META_CUT | cut meta | `seo_meta_utils.meta_cut_signature` + `meta_complete_sentence` |
| META_LENGTH | meta band | `seo_meta_utils.SEO_META_MIN / SEO_META_MAX` |
| TEXT_TRUNCATED | cut body | summary / description / itinerary ending without terminal punctuation |
| NUMBER_NOT_IN_SOURCE | figure | `acp_shared.grounding.find_novel_numeric_claims` over `content_generation.grounding.
|   |   | source_number_parts` (the A1 deterministic check) + `claim_rules.find_novel_word_numbers` |
| NUMBER_NOUN_MISMATCH | figure | the source has the number, but never with a unit of this kind ("50 minutes" vs |
|   |   | the source's "50 slate houses"); reads the fact table's numbers |
| NAME_NOT_IN_SOURCE | name | a capitalised name in the page the source never has (`hygiene.proper_nouns`) |
| DAY_PLACE_MISMATCH | day structure | a day title names a stop the source puts on other days only |
| INCLUSION_CONTRADICTED | inclusion | the page calls an item included while the fact table lists it as excluded |

These are measurements, not gates: `run_checks` is called by the step-1 report script only (S1-first freezes new
gates, advisory Q2). Each finding is {code, cls, field, sentence, detail}.
"""
from __future__ import annotations

import re
from typing import Optional

from services.acp_content_writing.claim_rules import find_novel_word_numbers
from services.acp_content_writing.hygiene import fold, phrase_tokens, proper_nouns
from services.acp_shared.grounding import find_novel_numeric_claims
from services.content_generation.day_parse import output_days, source_day_markers
from services.content_generation.grounding import source_number_parts
from services.content_generation.seo_meta_utils import (SEO_META_MAX, SEO_META_MIN, meta_complete_sentence,
                                                        meta_cut_signature)
from services.quality import day_oracle
from services.quality.s1_facts import (GENERIC_TITLE_WORDS, MEAL_GROUPS, YEAR_RE, build_fact_table, clean_name,
                                       field_text, item_groups, number_mentions, sentences, source_fields)

CLASS_DAY = "day_structure"
CLASS_META = "meta"
CLASS_TRUNC = "truncated_text"
CLASS_NUMBER = "number"
CLASS_NAME = "name"
CLASS_INCLUSION = "inclusion"

META_CUT = "META_CUT"
META_LENGTH = "META_LENGTH"
TEXT_TRUNCATED = "TEXT_TRUNCATED"
NUMBER_NOT_IN_SOURCE = "NUMBER_NOT_IN_SOURCE"
NUMBER_NOUN_MISMATCH = "NUMBER_NOUN_MISMATCH"
NAME_NOT_IN_SOURCE = "NAME_NOT_IN_SOURCE"
DAY_PLACE_MISMATCH = "DAY_PLACE_MISMATCH"
INCLUSION_CONTRADICTED = "INCLUSION_CONTRADICTED"

# Page fields the writer authored, in the order a reader meets them (metadata such as seo_keywords_used is never
# scanned — lesson S218/AA-738).
NARRATIVE_FIELDS = ("subtitle", "summary", "description", "highlights", "itineraries", "seo_meta")
TITLE_FIELDS = ("name", "seo_title")
_DAY_TITLE_RE = re.compile(r"^\s*Day\s+\d+\s*[—–-]")
_TERMINAL = tuple(".!?)\"'”’…")


def _finding(code: str, cls: str, field: str, sentence: str = "", detail: str = "") -> dict:
    return {"code": code, "cls": cls, "field": field, "sentence": sentence, "detail": detail}


def page_units(output: dict, fields=NARRATIVE_FIELDS + TITLE_FIELDS) -> list[tuple[str, str]]:
    """(field, sentence) for every sentence/line of the authored fields; itinerary day-title lines are kept (a
    title can carry a name or a figure)."""
    out = []
    for f in fields:
        value = output.get(f)
        if not value:
            continue
        if isinstance(value, list):
            for v in value:
                out.extend((f, s) for s in sentences(field_text(v)))
        else:
            out.extend((f, s) for s in sentences(field_text(value)))
    return out


# ------------------------------------------------------------------ day structure, meta, truncation


def check_days(tour: dict, output: dict) -> list[dict]:
    out = []
    for f in day_oracle.check(field_text(tour.get("itineraries")), field_text(tour.get("duration")),
                              field_text(output.get("itineraries"))):
        if f["code"] == day_oracle.DAY_CHECK_SKIPPED:
            continue
        out.append(_finding(f["code"], CLASS_DAY, "itineraries", detail=f["detail"]))
    return out


_LIST_TAIL_SKIP_RE = re.compile(r"^(?:and|or|plus|then|with|before|while|where|which|including|ideal|perfect|"
                                r"over|across|all|each|every|ending|finishing|from|in|on|at|for|to|by|as)\b", re.I)


def meta_list_cut(meta: str) -> Optional[str]:
    """A meta whose closing list lost its "and": "…with local guides, trains." / "…visiting Jaipur, Jodhpur, Agra,
    Khajuraho." — the shape a length cut leaves when it drops "…, and <last item>" (S248, 5 of the 14 cut metas on
    the golden set; meta_cut_signature reads only the final word). Not: an appositive ("…Namtso Lake, Tibet's
    second-largest lake."), a trailing phrase ("…, ideal for families."), a last item with its own "and"."""
    body = (meta or "").strip()
    if not body.endswith("."):
        return None
    segs = body[:-1].split(", ")
    if len(segs) < 3:
        return None
    last = segs[-1].strip()
    if (len(last.split()) > 4 or _LIST_TAIL_SKIP_RE.match(last) or re.search(r"\b(?:and|or)\b", last)
            or re.search(r"['’]s\b", last)):
        return None
    return f"closing list without a conjunction: '{last}'"


def _day_blocks(itinerary: str) -> dict[int, set[str]]:
    """Source day number → the folded tokens of its block (the "Day N" line up to the next marker; a range or list
    covers each of its days; an empty day inherits the day before). Any case, so an all-caps source title
    ("DAY 04 CEBU : SUMILON ISLAND") counts."""
    lines = (itinerary or "").splitlines()
    markers = source_day_markers(itinerary or "")
    blocks: dict[int, set[str]] = {}
    prev: set[str] = set()
    for i, (first, last, idx) in enumerate(markers):
        end = markers[i + 1][2] if i + 1 < len(markers) else len(lines)
        toks = set(re.findall(r"[a-z0-9]+", fold("\n".join(lines[idx:end]))))
        if not {t for t in toks if not t.isdigit() and t not in ("day", "days")}:
            toks = set(prev)   # an empty "Day 03" stays where the last described day was
        prev = toks
        for n in range(first, last + 1):
            blocks.setdefault(n, set()).update(toks)
    return blocks


def check_day_places(tour: dict, output: dict) -> list[dict]:
    """A written day whose title names a stop the source itinerary puts on other days only — not on that day and
    not on the day before (where a "X to Y" day starts): "Day 2 — Habarana" when the source has Habarana on days
    3–5. The day blocks drifted from the source (golden s1-012, s1-026, s1-030). A name the source itinerary never
    uses is left to NAME_NOT_IN_SOURCE; generic title words (Transfer, Riding, Temples) are skipped."""
    itinerary = field_text(tour.get("itineraries"))
    blocks = _day_blocks(itinerary)
    if len(blocks) < 2:
        return []
    # the source itinerary's place vocabulary: tokens of its capitalised names and its ALL-CAPS words
    lower = {fold(w) for w in re.findall(r"(?<![A-Za-z])[a-z][a-z’'-]+", "\n".join(source_fields(tour).values()))}
    every = {t for sent in sentences(itinerary) for name in proper_nouns(sent, keep_initial=True)
             for t in phrase_tokens(clean_name(name, lower))}
    every |= {fold(w) for w in re.findall(r"\b[A-Z]{4,}\b", itinerary)} - lower
    every -= set(phrase_tokens(field_text(tour.get("country"))))   # the country is on every day
    out = []
    days = output_days(field_text(output.get("itineraries")))
    last_day = max((n for n, _t, _f in days), default=0)
    for n, title, _first in days:
        if n not in blocks:
            continue
        near = blocks[n] | blocks.get(n - 1, set())
        if n == last_day:   # the last day goes home: back through the arrival city
            near |= blocks.get(min(blocks), set())
        wrong = []
        for name in sorted(proper_nouns(title, keep_initial=True)):
            for t in phrase_tokens(name):
                if (len(t) >= 4 and t in every and t not in near and t not in wrong
                        and not _generic(t, name) and any(t in v for v in blocks.values())):
                    wrong.append(t)
        if wrong:
            elsewhere = sorted(k for k, v in blocks.items() if set(wrong) & v)
            out.append(_finding(DAY_PLACE_MISMATCH, CLASS_DAY, "itineraries", f"Day {n} — {title}",
                                f"{', '.join(wrong)}: source day(s) {'/'.join(map(str, elsewhere[:6]))}, not day {n}"))
    return out


def check_meta(output: dict) -> list[dict]:
    meta = (output.get("seo_meta") or "").strip()
    if not meta:
        return []
    out = []
    sig = meta_cut_signature(meta) or meta_list_cut(meta)
    if sig or not meta_complete_sentence(meta):
        out.append(_finding(META_CUT, CLASS_META, "seo_meta", meta, sig or "not a complete sentence"))
    if not SEO_META_MIN <= len(meta) <= SEO_META_MAX:
        out.append(_finding(META_LENGTH, CLASS_META, "seo_meta", meta,
                            f"{len(meta)} chars, band {SEO_META_MIN}–{SEO_META_MAX}"))
    return out


# AA-645: a list item ("- Bike, helmet") or a code-built fact line ("Meals: B, L", "Overnight: Phakding") is a
# complete unit without a full stop — the short page (arm E) ends its fields with them. A cut prose sentence does not
# take this shape.
_LIST_OR_LABEL_LINE_RE = re.compile(r"^(?:[-•*]\s+\S|[A-Z][A-Za-z ]{1,30}:\s+\S)")


def check_truncation(output: dict) -> list[dict]:
    out = []
    for f in ("summary", "description", "itineraries"):
        text = field_text(output.get(f)).rstrip()
        if not text:
            continue
        last = text.splitlines()[-1].strip()
        if (_DAY_TITLE_RE.match(last) or last.endswith(_TERMINAL) or last[-1:].isdigit()
                or _LIST_OR_LABEL_LINE_RE.match(last)):
            continue
        out.append(_finding(TEXT_TRUNCATED, CLASS_TRUNC, f, last[-120:], "ends without terminal punctuation"))
    return out


# ------------------------------------------------------------------ figures

# Units a faithful rewrite may convert between (km ↔ miles, hours ↔ days): same family, no finding.
UNIT_FAMILY = {"km": "length", "m": "length", "ft": "length", "mi": "length",
               "h": "time", "min": "time", "day": "time", "night": "time", "week": "time",
               "km2": "area", "m2": "area", "ha": "area"}


_TO_FT, _TO_MI = 3.28084, 0.621371


def converted_numbers(facts: dict) -> set[str]:
    """The figures a faithful rewrite derives by converting a source length (m ↔ ft, km ↔ miles), floor and round,
    so "3,730m / 12,237ft" is not a novel 12237 (the A1 check adds time conversions only)."""
    out: set[str] = set()
    factor = {"m": _TO_FT, "ft": 1 / _TO_FT, "km": _TO_MI, "mi": 1 / _TO_MI}
    for e in facts["numbers"]:
        if e["unit"] not in factor or e.get("role", "figure") != "figure":
            continue
        for n in e["numbers"]:
            x = float(n) * factor[e["unit"]]
            out.update({str(int(x)), str(round(x))})
    return out


def unpadded_numbers(tour: dict) -> set[str]:
    """'Day 05' says 5: the zero-padded source figures without their leading zeros (the A1 digit reading keeps
    '05', so a page's 'Days 4 through 6' read as a novel 6 on golden s1-031)."""
    text = "\n".join(source_fields(tour).values())
    return {str(int(t)) for t in re.findall(r"(?<![\d.,])0\d+(?![\d.,])", text)}


def check_numbers(tour: dict, output: dict, facts: dict) -> list[dict]:
    src_parts = source_number_parts(tour) + [" ".join(sorted(converted_numbers(facts) | unpadded_numbers(tour)))]
    src_text = "\n".join(src_parts)
    # number → the (unit, words) bindings it has in the source, figures only (not "Day 10", "12th", "9:00")
    bindings: dict[str, list[tuple[Optional[str], list[str]]]] = {}
    for e in facts["numbers"]:
        if e.get("role", "figure") != "figure":
            continue
        for n in e["numbers"]:
            bindings.setdefault(n, []).append((e["unit"], e["words"]))
    out = []
    for field, sent in page_units(output):
        novel = find_novel_numeric_claims(sent, src_parts)
        novel += [w for w in find_novel_word_numbers(sent, [src_text]) if w not in novel]
        novel = [n for n in novel if not YEAR_RE.match(n)]
        if novel:
            out.append(_finding(NUMBER_NOT_IN_SOURCE, CLASS_NUMBER, field, sent, ", ".join(novel)))
            continue
        for m in number_mentions(sent):
            n = m["numbers"][0]
            # Only a figure with a unit is compared: a bare count ("281 residents" vs "281 people") is a wording
            # question the panel owns, and a range is checked by the novel-number test above.
            if (m["role"] != "figure" or not m["unit"] or YEAR_RE.match(n) or n not in bindings
                    or len(m["numbers"]) > 1):
                continue
            src = bindings[n]
            family = UNIT_FAMILY.get(m["unit"], m["unit"])
            if any(u and UNIT_FAMILY.get(u, u) == family for u, _w in src):
                continue
            # a bare source figure with nothing after it ("Khuspang (4680)") may carry any unit
            if any(u is None and not w for u, w in src):
                continue
            src_desc = "; ".join(sorted({f"{n} {u or ''} {' '.join(w)}".strip() for u, w in src}))[:160]
            out.append(_finding(NUMBER_NOUN_MISMATCH, CLASS_NUMBER, field, sent,
                                f"page '{m['span']}' vs source '{src_desc}'"))
            break
    return out


# ------------------------------------------------------------------ names


def _source_vocab(tour: dict) -> tuple[set[str], set[str]]:
    """(every folded token of the source, the folded tokens it also uses in lower case)."""
    text = "\n".join(source_fields(tour).values())
    toks = set(re.findall(r"[a-z0-9]+", fold(text)))
    lower = set(re.findall(r"(?<![A-Za-z])[a-z][a-z]+", text))
    return toks, {fold(w) for w in lower}


_DEMONYM_RE = re.compile(r"(?:ian|ean|ese|ish|ic|i)$")


def _generic(token: str, raw_name: str) -> bool:
    if token in GENERIC_TITLE_WORDS:
        return True
    # a one-word adjective of nationality / religion ('Laotian', 'Greek', 'Maldivian', 'Buddhist') is not a place
    return len(raw_name.split()) == 1 and bool(_DEMONYM_RE.search(token)) and len(token) > 4


def check_names(tour: dict, output: dict) -> list[dict]:
    """Names the source never has. On the old writer these are mostly place background, which the writer policy
    allows (memory writer-source-fidelity); so this is a review list, and a precision read on real names only."""
    src_toks, src_lower = _source_vocab(tour)
    page_text = "\n".join(field_text(output.get(f)) for f in NARRATIVE_FIELDS + TITLE_FIELDS)
    page_lower = {fold(w) for w in re.findall(r"(?<![A-Za-z])[a-z][a-z]+", page_text)}
    out = []
    seen: set[str] = set()
    for field, sent in page_units(output, NARRATIVE_FIELDS):
        for name in sorted(proper_nouns(sent)):
            toks = phrase_tokens(name)
            # a token is a name token when neither text uses it as an ordinary lower-case word
            unknown = [t for t in toks if t not in src_toks and t not in src_lower and t not in page_lower
                       and not _near(t, src_toks) and not _generic(t, name)]
            if not unknown:
                continue
            key = " ".join(unknown)
            if key in seen:
                continue
            seen.add(key)
            out.append(_finding(NAME_NOT_IN_SOURCE, CLASS_NAME, field, sent, f"'{name}' ({', '.join(unknown)})"))
    return out


def _near(token: str, vocab: set[str]) -> bool:
    """A spelling variant the source has: a plural/possessive or a one-letter difference on a long word
    ('Khovsgol' / 'Khuvsgul' is NOT near — two letters)."""
    if token.rstrip("s") in vocab or token + "s" in vocab:
        return True
    if len(token) < 7:
        return False
    for v in vocab:
        if abs(len(v) - len(token)) <= 1 and len(v) >= 7 and _one_edit(token, v):
            return True
    return False


def _one_edit(a: str, b: str) -> bool:
    if a == b:
        return True
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


# ------------------------------------------------------------------ inclusions

# An inclusion claim, not a list: "lunch included", "includes meals", "comes with", "all meals" ("including" and
# "served" describe, they do not promise).
_INCLUDED_CLAIM_RE = re.compile(r"\b(?:included|includes|provided|covered|comes?\s+with|all\s+meals)\b", re.I)
_NEGATED_RE = re.compile(r"\bnot\s+(?:be\s+)?(?:includ|provid|cover)|\bexclud|\bown\s+(?:cost|expense)|"
                         r"\bnot\s+part\b|\bavailable\s+(?:at|for)\b", re.I)


def check_inclusions(output: dict, facts: dict) -> list[dict]:
    excluded_groups: dict[str, str] = {}
    for e in facts["excluded"]:
        if e.get("conditional"):
            continue
        for g in e["groups"]:
            excluded_groups.setdefault(g, e["value"])
    included_groups = {g for e in facts["included"] for g in e["groups"]}
    out = []
    for field, sent in page_units(output, NARRATIVE_FIELDS):
        if not _INCLUDED_CLAIM_RE.search(sent) or _NEGATED_RE.search(sent):
            continue
        hits = []
        for g in item_groups(sent):
            if g in excluded_groups and g not in included_groups:
                hits.append(f"{g} (source: '{excluded_groups[g][:80]}')")
            elif g == "meals" and re.search(r"\b(?:all|full)\s+meals\b|\bmeals\b", sent, re.I):
                gaps = [m for m in MEAL_GROUPS if m in excluded_groups and m not in included_groups]
                if gaps:
                    hits.append(f"meals (source excludes {gaps[0]}: '{excluded_groups[gaps[0]][:80]}')")
        if hits:
            out.append(_finding(INCLUSION_CONTRADICTED, CLASS_INCLUSION, field, sent, "; ".join(hits)))
    return out


# ------------------------------------------------------------------ all


def run_checks(tour: dict, output: dict, facts: Optional[dict] = None) -> list[dict]:
    """Every code-check finding for one written S1 page (`output`: the generated_content fields) against its raw
    source (`tour`: the writer's input). `facts` defaults to build_fact_table(tour)."""
    facts = facts or build_fact_table(tour)
    return (check_days(tour, output) + check_day_places(tour, output) + check_meta(output) + check_truncation(output)
            + check_numbers(tour, output, facts) + check_names(tour, output) + check_inclusions(output, facts))
