"""AA-708 — match a Drive photo to a tour (its folder name) and a destination (its file name). Pure.

Folder layout on the CON board: country folder → one folder per tour → images named by place
("Olkhon Island1.jpeg", "Gyeongbokgung Palace (2).jpg"). Ambiguous matches stay unmatched; the
admin Photos page assigns them by hand.

AA-671 S257: resolve_destination() also reports WHY a label did not match (ambiguous / no candidate / no match),
for the photo_destination_match backfill (services/photos/destinations.py) and the sync.
AA-671 S258: version 2 of resolve_destination() (the job's {"version": 2}) breaks a tie between places on one spot and
matches a label that names a place near the tour (nearby_exact); see resolve_destination.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable, Mapping, Optional

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
# Outcomes of resolve_destination (the backfill job counts them; the sync counts matched / ambiguous / weak).
MATCHED, AMBIGUOUS, NO_MATCH, NO_CANDIDATE, NO_LABEL = "matched", "ambiguous", "no_match", "no_candidate", "no_label"
# A label found only INSIDE one place name ("Paro" in "Paro Taktsang"): reported with its suggestion, never written.
# Labels that are a town name are common on the CON board, and the town's landmark is not the town (review S257).
WEAK = "weak"
# Rules, strongest first. The first rule that names any destination decides: one destination -> matched, more than
# one -> ambiguous (never a guess, never a fall-through to a weaker rule). label_in_name only ever gives WEAK.
RULE_EXACT = "exact"                  # a whole-name form of the label == a whole-name form of a place name
RULE_CORE = "core"                    # same, kind words at the edges dropped ("Lake Khövsgöl" == "Khövsgöl Lake")
RULE_NAME_IN_LABEL = "name_in_label"  # a place name inside the label ("Olkhon Island sunset"); nested names: longest
RULE_LABEL_IN_NAME = "label_in_name"  # a distinctive label inside one place name: WEAK, a suggestion only
MIN_KEY = 4                           # shorter keys ("hue" aside) are too weak to match inside another name


# AA-671 S258 — matcher version 2 (the photo_destination_match job with {"version": 2}; the sync stays on version 1).
# Two $0 rules on top of version 1, both geographic, both decided before anything is written:
RULE_NEARBY_EXACT = "nearby_exact"    # the label IS the name of a place near the tour (not one of its own places)
TIE_SAME_SPOT = "same_spot"           # a tie between places that stand on one spot: any of them is the photo's place
SAME_SPOT_KM = 2.0                    # tied places at most this far apart are one spot (unmerged twins, two gates)
NEAR_ROUTE_KM = 20.0                  # a nearby_exact place lies at most this far from one of the tour's own places


@dataclass(frozen=True)
class DestinationMatch:
    outcome: str
    destination_id: Optional[str] = None
    rule: Optional[str] = None
    candidates: tuple[str, ...] = ()   # the tied destination ids when ambiguous (sorted)
    suggestion: Optional[str] = None   # WEAK only: the one place whose name holds the label (not written)
    tie_break: Optional[str] = None    # version 2: TIE_SAME_SPOT when a tie was broken (candidates = the tied ids)
    km: Optional[float] = None         # version 2, nearby_exact: distance to the nearest place of the tour


@dataclass(frozen=True)
class Place:
    """What version 2 knows about a place of the tour (or a place near it): where it is and how long the tour stays."""
    lat: Optional[float] = None
    lng: Optional[float] = None
    days: int = 0                      # distinct itinerary days of the tour at this place (0 = a graph stop only)
    first_day: Optional[int] = None


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


def resolve_destination(label: Optional[str], candidates: Iterable[tuple[str, Optional[str]]],
                        places: Optional[Mapping[str, Place]] = None,
                        nearby: Optional[Iterable[tuple[str, Optional[str], Place]]] = None) -> DestinationMatch:
    """Which of the tour's places a photo label names. `candidates` are (destination id, name) pairs: the places
    of the photo's own tour, several names per destination allowed (the itinerary spelling and the destination
    name). Pure, order-independent (ties are reported, not broken by input order).

    Version 2 (`places` given: destination id -> Place of the tour's places):
    - a tie at exact / core / name_in_label between places that stand on one spot (every pair at most SAME_SPOT_KM
      apart, all located) is broken: the place with the most tour days, then the earliest day, then the id
      (tie_break = same_spot). Places further apart stay ambiguous (two Night Markets in two towns).
    - when nothing of the tour matched (no_match), only a place name holds the label (weak, "Paro" in "Paro
      Taktsang") or several place names hold it (ambiguous at label_in_name): a place from `nearby` — (id, name,
      Place) of destinations outside the tour — whose whole name IS the label and that lies at most NEAR_ROUTE_KM
      from a located place of the tour is the answer (rule nearby_exact): the town Paro, not the monastery above it.
      Several such places: the same-spot tie-break, else ambiguous. A generic label ("Night Market") never matches
      this way.
    """
    cands = list(candidates)
    found = _resolve_v1(label, cands)
    if places is None:
        return found
    if found.outcome == AMBIGUOUS and found.rule in (RULE_EXACT, RULE_CORE, RULE_NAME_IN_LABEL):
        win = _same_spot(found.candidates, places)
        return DestinationMatch(MATCHED, win, found.rule, found.candidates, tie_break=TIE_SAME_SPOT) if win else found
    if nearby is not None and (found.outcome in (NO_MATCH, WEAK)
                               or (found.outcome == AMBIGUOUS and found.rule == RULE_LABEL_IN_NAME)):
        near = _nearby_exact(label, {str(d) for d, _ in cands if d}, places, nearby)
        if near is not None:
            return near
    return found


def km_between(a: Place, b: Place) -> Optional[float]:
    """Great-circle distance in km (haversine), None when either place has no coordinates."""
    if None in (a.lat, a.lng, b.lat, b.lng):
        return None
    la1, la2 = math.radians(a.lat), math.radians(b.lat)
    dla, dlo = la2 - la1, math.radians(b.lng - a.lng)
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def _same_spot(tied: Iterable[str], places: Mapping[str, Place]) -> Optional[str]:
    """The winner of a tie between places on one spot, or None (a place without coordinates, or two too far apart)."""
    ids = sorted(set(tied))
    if len(ids) < 2 or any(d not in places for d in ids):
        return None
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            km = km_between(places[a], places[b])
            if km is None or km > SAME_SPOT_KM:
                return None
    big = 1 << 30
    return min(ids, key=lambda d: (-places[d].days, places[d].first_day if places[d].first_day is not None else big,
                                   d))


def _nearby_exact(label: Optional[str], own: set[str], places: Mapping[str, Place],
                  nearby: Iterable[tuple[str, Optional[str], Place]]) -> Optional[DestinationMatch]:
    from services.acp_shared.place_cards import is_generic_name

    if is_generic_name(label):
        return None
    label_full = _full_forms(label or "")
    anchors = [p for d, p in places.items() if d in own and p.lat is not None and p.lng is not None]
    if not label_full or not anchors:
        return None
    hits: dict[str, float] = {}
    spots: dict[str, Place] = {}
    for did, name, where in nearby:
        did = str(did) if did else None
        if not did or did in own or not name or not (_full_forms(name) & label_full):
            continue
        dists = [k for k in (km_between(where, a) for a in anchors) if k is not None]
        if dists and min(dists) <= NEAR_ROUTE_KM:
            hits[did] = min(min(dists), hits.get(did, math.inf))
            spots[did] = where
    if not hits:
        return None
    if len(hits) == 1:
        (did, km), = hits.items()
        return DestinationMatch(MATCHED, did, RULE_NEARBY_EXACT, km=round(km, 1))
    tied = tuple(sorted(hits))
    win = _same_spot(tied, spots)
    if win:
        return DestinationMatch(MATCHED, win, RULE_NEARBY_EXACT, tied, tie_break=TIE_SAME_SPOT, km=round(hits[win], 1))
    return DestinationMatch(AMBIGUOUS, None, RULE_NEARBY_EXACT, tied)


def label_keys(label: Optional[str]) -> set[str]:
    """The whole-name keys of a label, to look up places by shared.destinations.name_norm (version 2 prefilter; the
    resolver compares the full forms of both names again)."""
    return {k for k in _full_forms(label or "") if len(k) >= 3}


def _resolve_v1(label: Optional[str], candidates: Iterable[tuple[str, Optional[str]]]) -> DestinationMatch:
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
        hits = sorted(d for d, keys in full.items() if any(f" {k} " in f" {n} " for k in keys_in for n in keys))
        if len(hits) == 1:
            # Never written: "Paro" (the town) inside "Paro Taktsang" (a monastery above it) is a different place.
            return DestinationMatch(WEAK, None, RULE_LABEL_IN_NAME, suggestion=hits[0])
        if hits:
            return DestinationMatch(AMBIGUOUS, None, RULE_LABEL_IN_NAME, tuple(hits))
    return DestinationMatch(NO_MATCH)


def match_destination(label: str, destinations: Iterable[tuple[str, Optional[str]]]) -> Optional[str]:
    """destination id named by the label, or None (no match, a weak match, or more than one place matches equally:
    resolve_destination)."""
    return resolve_destination(label, destinations).destination_id
