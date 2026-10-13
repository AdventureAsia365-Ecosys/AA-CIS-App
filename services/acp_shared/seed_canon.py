"""AA-772 (S234 round 2) — seed hygiene across place cards: one value per fact, and an overnight
card that the day's geography contradicts.

S234 panel class 7 ("seed contradictions and day order", 6/8 blogs): the Mongolia route seed gave
the writer two values for one fact — the Sod Camp card said Lake Khövsgöl "holds approximately two
percent of the world's fresh surface water", the Lake Khövsgöl card "about 0.4 percent" — and the
blogs printed both (#2) or picked one at random. The Sod Camp card also placed a Day 3 camp on the
Khövsgöl lakeshore while the Day 4 itinerary says "stop for lunch near Moron … before continuing to
the lakeshore camp": the camp of Day 3 is not at the lake, the card guessed the famous region.

Everything here is pure and deterministic (no LLM): `canonical_facts` keeps one value per
(place, measured thing) across the cards of a seed; `facts_conflict` is the pairwise test;
`overnight_card_off_route` flags an (unverified) overnight moment whose card region no other moment
of that day shares, when the next day's itinerary travels on to a lodging in that region.

Soft contradictions with no number ("ancient" vs "geologically recent") are NOT resolved here:
a word-level contradiction test would drop true facts on synonyms; both lines are kept.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

from services.acp_shared.place_cards import core_key, fold, is_generic_name, name_forms

# Spelled numbers a card fact uses ("two percent"). "one" is left out — "one of the deepest lakes"
# is not a quantity.
_WORD_NUMBERS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}
# Hedges and function words carry no subject.
_STOP = frozenset("""
a an the and or of in on at to for from by with as is are was were be been it its this that these those
which who whom whose than then there here into onto over under about approximately around roughly some
estimated nearly almost over more less most least up down per cent percent one also only just such
""".split())
_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?|[a-z]+")
_RANGE_SEP = frozenset({"to", "and"})
WINDOW_BEFORE = 4
WINDOW_AFTER = 6
MIN_SHARED_CONTEXT = 3  # S234 Mongolia: 2% vs 0.4% share {world, fresh, surface}; 70% vs 0.4% share 2
# The dimension a length/height number measures, next to it ('136 km LONG' vs '36 km WIDE', '262 m
# DEEP' vs '1,645 m ABOVE sea level'). Two quantities with different dimensions are two measures.
_DIMENSIONS = {
    "long": "length", "length": "length", "lengthwise": "length",
    "wide": "width", "width": "width", "across": "width", "breadth": "width", "broad": "width",
    "deep": "depth", "depth": "depth", "thick": "depth",
    "high": "height", "height": "height", "tall": "height", "elevation": "height", "altitude": "height",
    "above": "height", "below": "height", "sea": "height",
}


def _tokens(text: str) -> list[str]:
    """Lower-case, accent-folded word and number tokens; '%' → 'percent', a dash between numbers →
    'to', thousands separators dropped, decimals kept ('0.4')."""
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = t.replace("%", " percent ").replace("–", " to ").replace("—", " to ")
    t = re.sub(r"(\d)\s*-\s*(\d)", r"\1 to \2", t)
    t = re.sub(r"(\d),(\d{3})", r"\1\2", t)
    t = re.sub(r"(\d),(\d{3})", r"\1\2", t)  # 1,000,000
    return _TOKEN_RE.findall(t)


def _number(tok: str) -> Optional[float]:
    if re.fullmatch(r"\d+(?:\.\d+)?", tok):
        return float(tok)
    return _WORD_NUMBERS.get(tok)


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


def quantities(text: str) -> list[tuple[float, float, str, frozenset, str, str]]:
    """(low, high, unit, context words, "of" word, dimension) for every number in a fact. A range "20,000–24,000" or
    "1–2%" is one quantity. `unit` is the next word ('percent', 'metres', 'years'), or 'year' for a
    bare year (1000–2100). Context = content words within WINDOW_BEFORE/WINDOW_AFTER tokens, the
    unit itself excluded. The "of" word is the measure's whole ('percent of the WORLD's …'): two
    quantities with different "of" words are different measures. The dimension ('length', 'width',
    'depth', 'height' — `_DIMENSIONS`) is read from the 2 tokens after the unit, else the 2 before
    the number; '' when none."""
    toks = _tokens(text)
    out = []
    i = 0
    while i < len(toks):
        lo = _number(toks[i])
        if lo is None:
            i += 1
            continue
        start, hi, j = i, lo, i + 1
        if j + 1 < len(toks) and toks[j] in _RANGE_SEP and _number(toks[j + 1]) is not None:
            hi = _number(toks[j + 1])
            j += 2
        nxt = toks[j] if j < len(toks) else ""
        if nxt == "percent" or (nxt == "per" and toks[j + 1:j + 2] == ["cent"]):
            unit = "percent"
        elif 1000 <= lo <= 2100 and lo == int(lo) and (not nxt or nxt in _STOP or _number(nxt) is not None):
            unit = "year"
        else:
            unit = _stem(nxt) if nxt and _number(nxt) is None else ""
        window = toks[max(0, start - WINDOW_BEFORE):start] + toks[j + 1:j + 1 + WINDOW_AFTER]
        ctx = frozenset(_stem(w) for w in window
                        if len(w) >= 3 and w not in _STOP and _number(w) is None and _stem(w) != unit)
        k = j + 1 if unit and unit != "year" else j
        if toks[k:k + 1] == ["cent"]:
            k += 1
        of = ""  # '70 percent OF MONGOLIA's water' vs '0.4 percent OF the WORLD's' — two measures
        if toks[k:k + 1] == ["of"]:
            rest = [w for w in toks[k + 1:k + 3] if w not in _STOP]
            of = _stem(rest[0]) if rest else ""
        dim = next((_DIMENSIONS[w] for w in toks[k:k + 2] if w in _DIMENSIONS), "")
        dim = dim or next((_DIMENSIONS[w] for w in reversed(toks[max(0, start - 2):start]) if w in _DIMENSIONS), "")
        if unit:
            out.append((min(lo, hi), max(lo, hi), unit, ctx, of, dim))
        i = j
    return out


def _disjoint(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> bool:
    tol = 0.02 * max(abs(a_hi), abs(b_hi), 1e-9)
    return a_hi + tol < b_lo or b_hi + tol < a_lo


def facts_conflict(a: str, b: str, ignore: Iterable[str] = ()) -> bool:
    """Two facts about the same place state different numbers for the same measured thing: same
    unit, same "of" word and same dimension when both have one, at least MIN_SHARED_CONTEXT shared
    context words, non-overlapping values. `ignore` = words that do not count as shared context —
    the place's own name and country (review r2b: on two facts about one lake, 'lake khovsgol
    mongolia' alone made 'long' and 'wide' one measure)."""
    skip = {_stem(w) for w in ignore}
    for a_lo, a_hi, a_unit, a_ctx, a_of, a_dim in quantities(a):
        for b_lo, b_hi, b_unit, b_ctx, b_of, b_dim in quantities(b):
            if a_unit != b_unit or (a_of and b_of and a_of != b_of) or (a_dim and b_dim and a_dim != b_dim):
                continue
            if len((a_ctx & b_ctx) - skip) >= MIN_SHARED_CONTEXT and _disjoint(a_lo, a_hi, b_lo, b_hi):
                return True
    return False


def card_names(card: dict) -> set[str]:
    """Folded names a fact can use for the card's place ('lake khovsgol', 'khovsgol')."""
    name = card.get("display_name") or ""
    out = set(name_forms(name))
    if not is_generic_name(name) and len(core_key(name)) >= 4:
        out.add(core_key(name))
    return {n for n in out if n}


def _mentions(fact: str, names: Iterable[str]) -> bool:
    hay = f" {fold(fact)} "
    return any(f" {n} " in hay for n in names)


def _facts_of(card: dict) -> list[dict]:
    import json
    facts = card.get("facts") or []
    if isinstance(facts, str):
        facts = json.loads(facts or "[]")
    return [f for f in facts if isinstance(f, dict) and f.get("text")]


def canonical_facts(cards: list[dict], atom_counts: Optional[dict] = None) -> tuple[dict, list[dict]]:
    """One value per fact across the cards of one seed. `cards` in seed order (duplicates by
    card_id ignored). A fact is about its own card's place (cards with one core name + country are
    one place) plus any other card of the seed it names.

    Two facts of DIFFERENT cards about a shared place that `facts_conflict` → one stays, decided in
    this order: a 'verified' card's fact; a fact written on the card OF that place over one that
    only mentions it (the Sod Camp card's 'two percent' line about Lake Khövsgöl loses to the Lake
    Khövsgöl card); the place with more atoms in this seed (`atom_counts`, summed over cards of one
    place); the earlier card. Returns ({card_id: kept fact dicts}, dropped log)."""
    atom_counts = atom_counts or {}
    seen: dict[str, dict] = {}
    for c in cards:
        if c and c.get("card_id") and c["card_id"] not in seen:
            seen[c["card_id"]] = c
    names = {cid: card_names(c) for cid, c in seen.items()}
    # Parsed once: asyncpg returns jsonb `facts` as a str, and a re-parse would give new dicts
    # (review r2b — kept facts were matched by object identity and came back empty).
    parsed = {cid: _facts_of(c) for cid, c in seen.items()}
    name_words = {w for cid, c in seen.items()
                  for w in _TOKEN_RE.findall(" ".join(names[cid]) + " " + fold(c.get("country")))}
    order = {cid: i for i, cid in enumerate(seen)}

    def place_id(cid: str) -> str:
        # Two cards of one place ('Lake Khövsgöl' / 'Khövsgöl Lake', not yet merged) are one subject.
        c = seen[cid]
        if is_generic_name(c.get("display_name")) or not core_key(c.get("display_name")):
            return cid
        return f"{core_key(c.get('display_name'))}|{fold(c.get('country'))}"

    place_atoms: dict[str, int] = {}
    for cid in seen:
        place_atoms[place_id(cid)] = place_atoms.get(place_id(cid), 0) + atom_counts.get(cid, 1)

    def subjects(cid: str, text: str) -> set[str]:
        return {place_id(cid)} | {place_id(o) for o in seen if o != cid and _mentions(text, names[o])}

    def strength(cid: str, shared: set[str]) -> tuple:
        return (seen[cid].get("status") == "verified", place_id(cid) in shared,
                place_atoms[place_id(cid)], -order[cid])

    kept: list[tuple[str, dict, set, int]] = []
    dropped: list[dict] = []
    for cid in seen:
        for idx, fact in enumerate(parsed[cid]):
            subj = subjects(cid, fact["text"])
            wins = True
            for k in list(kept):
                shared = k[2] & subj
                if k[0] == cid or not shared or not facts_conflict(k[1]["text"], fact["text"], name_words):
                    continue
                if strength(cid, shared) > strength(k[0], shared):
                    kept.remove(k)
                    dropped.append({"card_id": k[0], "fact": k[1]["text"], "kept_card_id": cid,
                                    "kept_fact": fact["text"]})
                else:
                    wins = False
                    dropped.append({"card_id": cid, "fact": fact["text"], "kept_card_id": k[0],
                                    "kept_fact": k[1]["text"]})
                    break
            if wins:
                kept.append((cid, fact, subj, idx))
    keep = {(k[0], k[3]) for k in kept}
    return {cid: [f for i, f in enumerate(parsed[cid]) if (cid, i) in keep] for cid in seen}, dropped


# An overnight / lodging moment: its card names a camp or hotel, a kind of place that exists in
# many regions — the card drafter guesses the famous one (S234 'Sod Camp' → Khövsgöl lakeshore).
_OVERNIGHT_RE = re.compile(r"\b(overnight|check[- ]?in|stay the night|night (?:at|in)|sleep (?:at|in))\b", re.I)


def is_overnight(label: Optional[str]) -> bool:
    return bool(_OVERNIGHT_RE.search(label or ""))


# A later day that travels ON to a lodging ('before continuing to the lakeshore camp', Day 4 of the
# S234 Mongolia route): that day arrives at the region's lodging, so an earlier night was not there.
_ARRIVE_LODGING_RE = re.compile(
    r"\b(?:continu\w*|driv\w*|head\w*|travel\w*|transfer\w*|proceed\w*|mov\w* on|arriv\w*|reach\w*)"
    r"(?:\s+on)?\s+(?:to|towards?|at|in)?\s*(?:the|your|a|our)?\s*(?:[\w'-]+\s+){0,3}?"
    r"(?:camp|hotel|lodge|homestay|guesthouse|guest house|resort|farmhouse|ryokan|inn)\b", re.I)
# Generic administrative words of a region name: 'Khövsgöl Province' == 'Khövsgöl Province
# (Khövsgöl Aimag)' == 'Khövsgöl'.
_REGION_GENERIC = frozenset({"province", "aimag", "district", "dzongkhag", "region", "state", "prefecture",
                             "county", "oblast", "governorate", "municipality", "division", "area", "the", "of"})


def region_key(region: Optional[str]) -> str:
    """A region name without parentheticals, accents and generic administrative words."""
    words = [w for w in re.findall(r"[a-z0-9]+", fold(re.sub(r"\(.*?\)", " ", region or "")))
             if w not in _REGION_GENERIC]
    return " ".join(words)


def _part_region(p: dict) -> str:
    return region_key((p.get("card") or {}).get("region"))


def overnight_card_off_route(part: dict, same_day: list[dict], next_day: list[dict]) -> bool:
    """True when `part` is an overnight moment whose card the itinerary itself contradicts — all of:
    the card is not 'verified'; its region (`region_key`) matches no other carded moment of the same
    day (and one exists); and the NEXT day both travels on to a lodging (`_ARRIVE_LODGING_RE` in a
    moment's label or itinerary text) and has a moment carded in that region. S234: Day 3 'Sod Camp'
    card on the Khövsgöl lakeshore, Day 4 'stop for lunch near Mörön … before continuing to the
    lakeshore camp'. A plain transfer day (sleep in region B, explore B the next day) keeps its
    card: the next day does not arrive at a lodging there. Such a card's location — and the facts
    that follow from it — is not trusted: the seed falls back to the itinerary place + country."""
    card = part.get("card") or {}
    if not is_overnight(part.get("text")) or card.get("status") == "verified":
        return False
    key = region_key(card.get("region"))
    if not key:
        return False
    others = {_part_region(p) for p in same_day if p is not part and _part_region(p)}
    if not others or key in others:
        return False
    arrives = any(_ARRIVE_LODGING_RE.search(f"{p.get('text') or ''} {p.get('evidence') or ''}") for p in next_day)
    return arrives and any(_part_region(p) == key for p in next_day)


__all__ = ["quantities", "facts_conflict", "canonical_facts", "card_names", "is_overnight",
           "overnight_card_off_route", "region_key"]
