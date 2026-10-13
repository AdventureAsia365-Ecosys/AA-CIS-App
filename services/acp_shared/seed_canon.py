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
`overnight_card_off_route` flags the overnight moment whose card region no other moment of that
day shares.

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


def quantities(text: str) -> list[tuple[float, float, str, frozenset, str]]:
    """(low, high, unit, context words, "of" word) for every number in a fact. A range "20,000–24,000" or
    "1–2%" is one quantity. `unit` is the next word ('percent', 'metres', 'years'), or 'year' for a
    bare year (1000–2100). Context = content words within WINDOW_BEFORE/WINDOW_AFTER tokens, the
    unit itself excluded. The "of" word is the measure's whole ('percent of the WORLD's …'): two
    quantities with different "of" words are different measures."""
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
        if unit:
            out.append((min(lo, hi), max(lo, hi), unit, ctx, of))
        i = j
    return out


def _disjoint(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> bool:
    tol = 0.02 * max(abs(a_hi), abs(b_hi), 1e-9)
    return a_hi + tol < b_lo or b_hi + tol < a_lo


def facts_conflict(a: str, b: str) -> bool:
    """Two facts about the same place state different numbers for the same measured thing: same
    unit, same "of" word when both have one, at least MIN_SHARED_CONTEXT shared context words,
    non-overlapping values."""
    for a_lo, a_hi, a_unit, a_ctx, a_of in quantities(a):
        for b_lo, b_hi, b_unit, b_ctx, b_of in quantities(b):
            if a_unit != b_unit or (a_of and b_of and a_of != b_of):
                continue
            if len(a_ctx & b_ctx) >= MIN_SHARED_CONTEXT and _disjoint(a_lo, a_hi, b_lo, b_hi):
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

    kept: list[tuple[str, dict, set]] = []
    dropped: list[dict] = []
    for cid in seen:
        for fact in _facts_of(seen[cid]):
            subj = subjects(cid, fact["text"])
            wins = True
            for k in list(kept):
                shared = k[2] & subj
                if k[0] == cid or not shared or not facts_conflict(k[1]["text"], fact["text"]):
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
                kept.append((cid, fact, subj))
    keep_ids = {id(f) for _, f, _ in kept}
    return {cid: [f for f in _facts_of(c) if id(f) in keep_ids] for cid, c in seen.items()}, dropped


# An overnight / lodging moment: its card names a camp or hotel, a kind of place that exists in
# many regions — the card drafter guesses the famous one (S234 'Sod Camp' → Khövsgöl lakeshore).
_OVERNIGHT_RE = re.compile(r"\b(overnight|check[- ]?in|stay the night|night (?:at|in)|sleep (?:at|in))\b", re.I)


def is_overnight(label: Optional[str]) -> bool:
    return bool(_OVERNIGHT_RE.search(label or ""))


def overnight_card_off_route(part: dict, same_day: list[dict]) -> bool:
    """True when `part` is an overnight moment whose card region differs from the region of every
    other carded moment of the same day (and at least one exists). Such a card's location — and the
    facts that follow from it — is not trusted: the seed falls back to the itinerary place + country."""
    card = part.get("card") or {}
    if not is_overnight(part.get("text")) or not card.get("region"):
        return False
    others = {fold((p.get("card") or {}).get("region")) for p in same_day
              if p is not part and (p.get("card") or {}).get("region")}
    return bool(others) and fold(card["region"]) not in others


__all__ = ["quantities", "facts_conflict", "canonical_facts", "card_names", "is_overnight",
           "overnight_card_off_route"]
