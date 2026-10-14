"""AA-712 — places of a tour (= TripPlanner itinerary components) from its active atoms. Port of
AA-TripPlanner-Web backend/extraction/deterministic.py + components.py (AA-674/AA-731, @ 6523816):
no model call — atoms already carry place + activity_type.

Changes vs TripPlanner (design §2.3 / §3):
  - one filter for "not a place to pin": `skip_reason()` = the place-card rule `card_skip_reason()`
    (unnamed places, logistics nouns on stay/transit/food atoms) plus TripPlanner's lodging/transit
    words under the same activity gate; transit atoms are never pinned (map_activity);
  - one component per (day, place, activity) instead of one per atom: `atom_ids` keeps every atom,
    `text_extract` is the longest atom text; its id is uuid5(source_id, day, name_norm, country,
    activity) — stable across re-atomize and re-upload (names.component_id);
  - `components_hash` gates components/link/embed: unchanged atoms → nothing is rebuilt or paid.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

from services.places import days as days_mod
from services.places.names import component_id, country_norm, is_generic, locality_key, name_norm

ACTIVITIES = ("trekking", "cultural_heritage", "wildlife_nature", "water_activities", "culinary",
              "wellness_relaxation", "adventure_sport", "local_immersion")

# tour_atoms.activity_type -> taxonomy ('other' resolved by keyword below; 'transit' not pinnable).
_ACTIVITY_TYPE_MAP = {
    "trek": "trekking",
    "culture": "cultural_heritage",
    "food": "culinary",
    "bike": "adventure_sport",
    "stay": "wellness_relaxation",
}
_OTHER_KEYWORDS = [
    ("wildlife_nature", ("safari", "national park", "wildlife", "jungle", "elephant", "bird", "nature",
                         "forest", "tea estate", "tea ")),
    ("water_activities", ("beach", "cruise", "kayak", "snorkel", "dive", "boat", "river", "lagoon", "swim",
                          "sail")),
    ("culinary", ("food", "lunch", "dinner", "cooking", "market", "tasting")),
    ("trekking", ("trek", "hike", "walk", "climb", "summit")),
]
_INTENSITY_BY_ACTIVITY = {
    "trekking": "active",
    "adventure_sport": "strenuous",
    "water_activities": "moderate",
    "wildlife_nature": "moderate",
    "cultural_heritage": "leisurely",
    "culinary": "leisurely",
    "wellness_relaxation": "leisurely",
    "local_immersion": "leisurely",
}
TEXT_MAX = 280
ALL_MONTHS = list(range(1, 13))
_LOGISTICS_TYPES = frozenset({"stay", "transit", "food", ""})


def map_activity(activity_type: Optional[str], text: str) -> Optional[str]:
    """Atom activity_type -> taxonomy value; None for 'transit' (caller skips)."""
    at = (activity_type or "").strip().lower()
    if at == "transit":
        return None
    if at in _ACTIVITY_TYPE_MAP:
        return _ACTIVITY_TYPE_MAP[at]
    if at in ("other", ""):
        t = (text or "").lower()
        for value, kws in _OTHER_KEYWORDS:
            if any(k in t for k in kws):
                return value
        return "local_immersion"
    return "local_immersion"


def skip_reason(place: Optional[str], activity_type: Optional[str]) -> Optional[str]:
    """Why an atom's place is not pinned, or None. The place-card rule first (one rule set with
    cards, design §3), then TripPlanner's lodging/transit words under the same activity gate."""
    from services.acp_shared.place_cards import card_skip_reason
    reason = card_skip_reason(place, activity_type)
    if reason:
        return reason
    at = (activity_type or "").strip().lower()
    if at in _LOGISTICS_TYPES and (days_mod.is_lodging(place) or days_mod.is_transit(place)):
        return "logistics_place"
    if at == "transit":
        return "transit"
    return None


@dataclass
class Component:
    component_id: str
    day_index: int
    name: str                  # the itinerary's spelling (atom place)
    link_name: str             # what is resolved: the place card's display name, else `name`
    name_norm: str             # name_norm(link_name) — the geo.place_name / destinations key
    country: str               # where the place is: the card's country, else the tour's
    country_norm: str
    locality_norm: Optional[str]   # None: a generic name with no locality → never resolved
    locality: Optional[str]
    activity: str
    intensity_level: str
    duration_hint: str
    season_months: list[int]
    text_extract: str
    atom_ids: list[str] = field(default_factory=list)
    place_card_id: Optional[str] = None

    @property
    def text_hash(self) -> str:
        return text_hash(self.text_extract)


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def components_hash(atoms: list[dict]) -> str:
    """sha256(RULE_VERSION ‖ sorted (day, place, activity_type, text[:280], place-card link fields)
    of the active atoms). The card fields (card id, display name, country, locality, destination)
    are in the hash because a card usually arrives AFTER atomize (place_cards_build, T8 on demand)
    and changes the link key / locality of a component (design §3 "card identity preferred")."""
    rows = sorted(
        (int(a["itinerary_day"]) if a.get("itinerary_day") is not None else -1, (a.get("place") or "").strip(),
         (a.get("activity_type") or "").strip().lower(), (a.get("text") or "").strip()[:TEXT_MAX],
         str(a.get("card_id") or ""), (a.get("card_display_name") or "").strip(),
         (a.get("card_country") or "").strip(), (a.get("card_locality") or "").strip(),
         str(a.get("card_destination_id") or ""))
        for a in atoms)
    blob = "\n".join("\t".join(str(x) for x in r) for r in rows)
    return hashlib.sha256(f"{days_mod.RULE_VERSION}\n{blob}".encode("utf-8")).hexdigest()


def build_components(source_id: str, tour_country: str, atoms: list[dict],
                     day_towns: Optional[dict[int, str]] = None) -> tuple[list[Component], dict]:
    """Atoms (atom_id, itinerary_day, place, activity_type, text, card_id?, card_display_name?,
    card_country?, card_locality?) -> merged components + skip counters. `day_towns` = the day's
    overnight anchor, the locality fallback of a generic name."""
    day_towns = day_towns or {}
    merged: dict[tuple, Component] = {}
    counters = {"atoms": len(atoms), "skipped": {}, "generic_without_locality": 0}
    for a in sorted(atoms, key=lambda x: (x.get("itinerary_day") or 0, str(x.get("atom_id")))):
        place = (a.get("place") or "").strip()
        day = a.get("itinerary_day")
        reason = "no_day" if day is None else skip_reason(place, a.get("activity_type"))
        activity = map_activity(a.get("activity_type"), a.get("text") or "") if reason is None else None
        if reason is None and activity is None:
            reason = "transit"
        if reason is not None:
            counters["skipped"][reason] = counters["skipped"].get(reason, 0) + 1
            continue
        day = int(day)
        key = (day, name_norm(place), activity)
        text = (a.get("text") or "").strip()[:TEXT_MAX] or place
        comp = merged.get(key)
        if comp is None:
            link_name = (a.get("card_display_name") or "").strip() or place
            link_country = a.get("card_country") or tour_country or ""
            locality = a.get("card_locality") or (day_towns.get(day) if is_generic(link_name) else None)
            loc_key = locality_key(link_name, locality)
            if loc_key is None:
                counters["generic_without_locality"] += 1
            comp = Component(
                # identity: the itinerary spelling + the tour's country (stable while the atom is)
                component_id=component_id(source_id, day, place, tour_country, activity),
                day_index=day, name=place, link_name=link_name, name_norm=name_norm(link_name),
                country=link_country, country_norm=country_norm(link_country),
                locality_norm=loc_key, locality=locality if loc_key else None,
                activity=activity, intensity_level=_INTENSITY_BY_ACTIVITY.get(activity, "moderate"),
                duration_hint="full_day", season_months=list(ALL_MONTHS), text_extract=text,
                place_card_id=str(a["card_id"]) if a.get("card_id") else None)
            merged[key] = comp
        elif len(text) > len(comp.text_extract):
            comp.text_extract = text
        comp.atom_ids.append(str(a["atom_id"]))
    out = sorted(merged.values(), key=lambda c: (c.day_index, c.name_norm, c.activity))
    counters["components"] = len(out)
    return out, counters
