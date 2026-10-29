"""AA-708 — match a Drive photo to a tour (its folder name) and a destination (its file name). Pure.

Folder layout on the CON board: country folder → one folder per tour → images named by place
("Olkhon Island1.jpeg", "Gyeongbokgung Palace (2).jpg"). Ambiguous matches stay unmatched; the
admin Photos page assigns them by hand.

AA-671 S257: resolve_destination() also reports WHY a label did not match (ambiguous / no candidate / no match),
for the photo_destination_match backfill (services/photos/destinations.py) and the sync.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Optional

_EXT = re.compile(r"\.[a-z0-9]{2,5}$", re.IGNORECASE)
# A trailing run of copy markers (" (2)", "copy", "_3", "-12"), matched REVERSED at the start of the reversed
# name (AA-912: the forward form (?:[\s_\-]*(?:\(\d+\)|copy|\d+))+$ backtracked exponentially on "000…0x";
# reversed + re.match has one start and one parse). Same result (fuzzed, a final '\n' included).
_TRAILING_REV = re.compile(r"(?:(?:\)\d+\(|ypoc|\d+(?!\d))[\s_\-]*)+", re.IGNORECASE)
_NON_WORD = re.compile(r"[^a-z0-9]+")
TOUR_RATIO = 0.85
TOKEN_SHARE = 0.9
# Real CON folder names (01/10/2026): "Best of Bhutan - 9 days", "Dabajianshan Trek 3 Day / 2 Night
# (Guided)", "GB-01 MANILA and SUBURBS - 4 hours", "Beijing, Xi'an and Shanghai — Nine Days".
_CODE = re.compile(r"^\s*[A-Z]{2,4}\s*[-–]\s*\d+\s*")
_PAREN = re.compile(r"\((?:guided|private|small group|self[- ]guided)[^)]*\)", re.IGNORECASE)
_DURATION = re.compile(
    r"\s*[-–—:|,]?\s*\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen)\s*-?\s*(?:days?|nights?|hours?|hrs?)\b.*$", re.IGNORECASE)


def norm(text: str) -> str:
    return _NON_WORD.sub(" ", (text or "").lower()).strip()


def _strip_trailing_counter(base: str) -> str:
    """Drop a trailing run of copy markers: " (2)", "copy", "_3", "-12" (any mix, any order)."""
    end = len(base) - 1 if base.endswith("\n") else len(base)  # the old '$' also matched before a final '\n'
    m = _TRAILING_REV.match(base[:end][::-1])
    return base[: end - m.end()] + base[end:] if m else base


def place_label(file_name: str) -> str:
    """"Olkhon Island1.jpeg" -> "Olkhon Island"; "Bukchon_Hanok-Village (2).JPG" -> "Bukchon Hanok Village"."""
    base = _EXT.sub("", (file_name or "").strip())
    base = _strip_trailing_counter(base)
    return " ".join(re.sub(r"[_\-]+", " ", base).split())


def tour_title(name: str) -> str:
    """A folder or tour name without its code, "(Guided)" and duration tail."""
    n = _PAREN.sub("", _CODE.sub("", (name or "").strip()))
    short = _DURATION.sub("", n)
    return (short if len(norm(short)) >= 4 else n).strip(" -–—:,")


def _token_share(a: str, b: str) -> float:
    ta, tb = set(a.split()), set(b.split())
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def match_tour(folder: Optional[str], tours: Iterable[tuple[str, ...]]) -> Optional[str]:
    """tour_id named by the folder. `tours` rows are (tour_id, name, *other names) — the raw name and,
    once written, the rewritten aa_name. Exact title wins; else one clear best by similarity, or all
    words of the shorter title shared (≥ 3 words)."""
    f = norm(tour_title(folder or ""))
    if len(f) < 4:
        return None
    best: dict[str, float] = {}
    for tid, *names in tours:
        for name in names:
            n = norm(tour_title(name or ""))
            if not n:
                continue
            if n == f:
                return tid
            score = SequenceMatcher(None, f, n).ratio()
            if min(len(f.split()), len(n.split())) >= 3 and _token_share(f, n) >= TOKEN_SHARE:
                score = max(score, TOUR_RATIO)
            best[tid] = max(best.get(tid, 0.0), score)
    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    if ranked and ranked[0][1] >= TOUR_RATIO and (len(ranked) == 1 or ranked[1][1] < ranked[0][1] - 0.05):
        return ranked[0][0]
    return None


# ── AA-671 S257: photo -> destination of ITS tour ───────────────────────────────────────────────
# Outcomes of resolve_destination (the backfill job counts them; the sync counts matched / ambiguous).
MATCHED, AMBIGUOUS, NO_MATCH, NO_CANDIDATE, NO_LABEL = "matched", "ambiguous", "no_match", "no_candidate", "no_label"
# Rules, strongest first. The first rule that names any destination decides: one destination -> matched, more than
# one -> ambiguous (never a guess, never a fall-through to a weaker rule).
RULE_EXACT = "exact"                  # a whole-name form of the label == a whole-name form of a place name
RULE_CORE = "core"                    # same, kind words at the edges dropped ("Lake Khövsgöl" == "Khövsgöl Lake")
RULE_NAME_IN_LABEL = "name_in_label"  # a place name inside the label ("Olkhon Island sunset"); nested names: longest
RULE_LABEL_IN_NAME = "label_in_name"  # a distinctive label inside one place name ("Taktsang" in "Paro Taktsang")
MIN_KEY = 4                           # shorter keys ("hue" aside) are too weak to match inside another name


@dataclass(frozen=True)
class DestinationMatch:
    outcome: str
    destination_id: Optional[str] = None
    rule: Optional[str] = None
    candidates: tuple[str, ...] = ()   # the tied destination ids when ambiguous (sorted)


def _full_forms(name: str) -> set[str]:
    """Whole-name keys of a place name: the destinations identity key (services.places.names.name_norm) and the
    place-card forms (with and without the parenthetical, and the parenthetical alone)."""
    from services.acp_shared.place_cards import name_forms
    from services.places.names import name_norm
    return {k for k in {name_norm(name), *name_forms(name)} if k}


def _core(name: str) -> Optional[str]:
    """core_key of a distinctive name (None for a kind-only name like "Night Market": it names a place in every
    town, so it never matches by its core)."""
    from services.acp_shared.place_cards import core_key, is_generic_name
    if is_generic_name(name):
        return None
    return core_key(name) or None


def _decide(hits: dict[str, int], rule: str) -> Optional[DestinationMatch]:
    """hits: destination id -> score (higher wins). None when nothing was hit (the next rule is tried)."""
    if not hits:
        return None
    top = max(hits.values())
    best = sorted(d for d, s in hits.items() if s == top)
    if len(best) == 1:
        return DestinationMatch(MATCHED, best[0], rule)
    return DestinationMatch(AMBIGUOUS, None, rule, tuple(best))


def resolve_destination(label: Optional[str], candidates: Iterable[tuple[str, Optional[str]]]) -> DestinationMatch:
    """Which of the tour's places a photo label names. `candidates` are (destination id, name) pairs: the places
    of the photo's own tour, several names per destination allowed (the itinerary spelling and the destination
    name). Pure, order-independent (ties are reported, not broken by input order)."""
    from services.acp_shared.place_cards import is_generic_name
    from services.places.names import name_norm

    lab = name_norm(label)
    if len(lab) < 3:
        return DestinationMatch(NO_LABEL)
    names: dict[str, set[str]] = {}
    for did, name in candidates:
        if did and name and name.strip():
            names.setdefault(str(did), set()).add(name)
    if not names:
        return DestinationMatch(NO_CANDIDATE)

    full = {d: set().union(*(_full_forms(n) for n in ns)) for d, ns in names.items()}
    label_full = _full_forms(label or "")
    found = _decide({d: 1 for d, keys in full.items() if keys & label_full}, RULE_EXACT)
    if found:
        return found

    label_core = _core(label or "")
    if label_core:
        cores = {d: {c for c in (_core(n) for n in ns) if c} for d, ns in names.items()}
        found = _decide({d: 1 for d, keys in cores.items() if label_core in keys}, RULE_CORE)
        if found:
            return found

    padded = [f" {k} " for k in label_full]
    inside: dict[str, str] = {}      # destination -> its longest name found inside the label
    for d, keys in full.items():
        hits = [k for k in keys if len(k) >= MIN_KEY and any(f" {k} " in p for p in padded)]
        if hits:
            inside[d] = max(hits, key=lambda k: (len(k), k))
    if inside:
        # The longest name wins only over names nested in it ("Olkhon" inside "Olkhon Island"); two separate places
        # in one label ("Brass Gate and Iron Gate") are ambiguous.
        top = max(len(k) for k in inside.values())
        winners = sorted(d for d, k in inside.items() if len(k) == top)
        win_key = inside[winners[0]]
        others = sorted(d for d, k in inside.items() if d not in winners and f" {k} " not in f" {win_key} ")
        if len(winners) == 1 and not others:
            return DestinationMatch(MATCHED, winners[0], RULE_NAME_IN_LABEL)
        return DestinationMatch(AMBIGUOUS, None, RULE_NAME_IN_LABEL, tuple(sorted(winners + others)))

    if not is_generic_name(label):
        keys_in = [k for k in label_full if len(k) >= MIN_KEY]
        found = _decide({d: 1 for d, keys in full.items()
                         if any(f" {k} " in f" {n} " for k in keys_in for n in keys)}, RULE_LABEL_IN_NAME)
        if found:
            return found
    return DestinationMatch(NO_MATCH)


def match_destination(label: str, destinations: Iterable[tuple[str, Optional[str]]]) -> Optional[str]:
    """destination id named by the label, or None (no match, or more than one place matches equally:
    resolve_destination)."""
    return resolve_destination(label, destinations).destination_id
