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
from services.places import geomatch
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
    """sha256(RULE_VERSION ‖ sorted (day, place, activity_type, text[:280], place-card link fields, atom_id)
    of the active atoms). The card fields (card id, display name, country, locality, destination)
    are in the hash because a card usually arrives AFTER atomize (place_cards_build, T8 on demand)
    and changes the link key / locality of a component (design §3 "card identity preferred").
    AA-989: the atom id too — a re-upload (new tour_id → new atom ids, same text) or an action-only re-atomize
    left `geo.tour_place.atom_ids` on replaced atoms (rebase / skipped_unchanged). A changed id set now rebuilds
    the components; days and vectors are reused (days_hash / text_hash), so the rebuild is free unless names are
    new. One-time effect of this change: the next place_extract of every built tour rebuilds once."""
    rows = sorted(
        (int(a["itinerary_day"]) if a.get("itinerary_day") is not None else -1, (a.get("place") or "").strip(),
         (a.get("activity_type") or "").strip().lower(), (a.get("text") or "").strip()[:TEXT_MAX],
         str(a.get("card_id") or ""), (a.get("card_display_name") or "").strip(),
         (a.get("card_country") or "").strip(), (a.get("card_locality") or "").strip(),
         str(a.get("card_destination_id") or ""), str(a.get("atom_id") or ""))
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
    counters = {"atoms": len(atoms), "skipped": {}, "generic_without_locality": 0, "linked_to_feature": 0}
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
            # AA-847 (S246): a descriptive phrase around a linear feature ("sandbank on the Nam Ou River") links to
            # that feature's destination; the component id keeps the itinerary spelling (no id-map impact)
            feature = None if (a.get("card_display_name") or "").strip() else geomatch.linked_feature(place)
            counters["linked_to_feature"] += 1 if feature else 0
            link_name = (a.get("card_display_name") or "").strip() or feature or place
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


# ── AA-989: component ids survive a rebuild ─────────────────────────────────────────────────────
# TripPlanner pins a component by its id (trip_events add_component / remove_component / reorder, read back through
# catalog.v_tour_component). The id is uuid5(source, day, itinerary spelling, country, activity), so a rewrite that
# respells a place, retypes its activity or moves it a day would mint a new id and silently drop every saved pin.
# A rebuild therefore keeps the id of the PREVIOUS row of the same source for the same place: the uuid5 is only the
# id of a place's first build. Passes, in order, each one-to-one and only on an unambiguous pair (exactly one free
# old row and exactly one unmatched new component share the key); an ambiguous key falls to the next pass:
#   same day + itinerary spelling (activity retyped) → same day + destination → same day + place card →
#   same day + link name → any day + destination → any day + place card → any day + link name.
CARRY_PASSES = (
    ("spelling", True), ("destination", True), ("card", True), ("link_name", True),
    ("destination", False), ("card", False), ("link_name", False),
)


def _carry_key(kind: str, *, spelling: str, link_name: str, destination, card) -> Optional[str]:
    v = {"spelling": spelling, "link_name": link_name, "destination": destination, "card": card}[kind]
    return str(v) if v else None


def carry_over_ids(comps: list[Component], comp_dest: dict, old_rows: list[dict]) -> tuple[dict[str, str], dict]:
    """({new component id: previous id to keep}, counters). `old_rows`: the source's current geo.tour_place rows
    (component_id, day_index, name, name_norm, destination_id, place_card_id). Pure; the caller renames."""
    old = {str(o["component_id"]): o for o in old_rows}
    new_ids = {c.component_id for c in comps}
    free_old = {i for i in old if i not in new_ids}
    todo = [c for c in comps if c.component_id not in old]
    mapping: dict[str, str] = {}
    by_pass: dict[str, int] = {}
    for kind, same_day in CARRY_PASSES:
        if not free_old or not todo:
            break
        olds: dict[tuple, list[str]] = {}
        for i in sorted(free_old):
            o = old[i]
            k = _carry_key(kind, spelling=name_norm(o.get("name")), link_name=o.get("name_norm") or "",
                           destination=o.get("destination_id"), card=o.get("place_card_id"))
            if k:
                olds.setdefault((int(o["day_index"]) if same_day else None, k), []).append(i)
        news: dict[tuple, list[Component]] = {}
        for c in todo:
            k = _carry_key(kind, spelling=name_norm(c.name), link_name=c.name_norm,
                           destination=comp_dest.get(c.component_id), card=c.place_card_id)
            if k:
                news.setdefault((c.day_index if same_day else None, k), []).append(c)
        for key, cs in news.items():
            os_ = olds.get(key) or []
            if len(cs) == 1 and len(os_) == 1:
                mapping[cs[0].component_id] = os_[0]
                free_old.discard(os_[0])
                label = f"{kind}{'' if same_day else '_any_day'}"
                by_pass[label] = by_pass.get(label, 0) + 1
        todo = [c for c in todo if c.component_id not in mapping]
    counters = {"ids_kept": sum(1 for c in comps if c.component_id in old), "ids_carried": len(mapping),
                "ids_carried_by": by_pass, "ids_new": len(todo), "ids_dropped": len(free_old)}
    return mapping, counters


def apply_carry_over(comps: list[Component], comp_dest: dict, mapping: dict[str, str]) -> dict:
    """Renames the components in place; returns comp_dest re-keyed on the kept ids."""
    if not mapping:
        return comp_dest
    for c in comps:
        c.component_id = mapping.get(c.component_id, c.component_id)
    return {mapping.get(k, k): v for k, v in comp_dest.items()}


# ── AA-990: an id the rebuild could not keep → a redirect to the component that took its place ─────────────────────
# carry_over_ids keeps an id in place only on an unambiguous one-to-one pair. Every other previous row is dropped, and a
# TripPlanner pin on it dangled (S252: 322 of 371 pins after the S1 v2 place re-sync). A redirect is a lookup, not a
# served id, so it may be many-to-one and it may pick between candidates: for every dropped previous row, the current
# component of the same source for the same place, by rule (first rule with a candidate wins):
#   destination (same linked destination) → place card → itinerary spelling + country → link name + country,
# any day, the nearest day first, then the same activity, then the smaller id; a generic name ('Night Market') by
# spelling or link name only in the same locality. The redirects are cumulative per source
# (an older redirect whose target was dropped now is re-pointed through this build's redirects) and live in
# geo.tour_build.counts["id_redirects"]: CIS cannot write tripplanner.* (tp-017 grants the map to the role tripplanner
# only), so AA-CIS-Infra db/scripts/aa990_component_id_backfill.py copies them into tripplanner.component_id_map.
REDIRECTS_KEY = "id_redirects"
REDIRECTS_CAP = 1000          # per source; the newest are kept (a tour is rewritten a handful of times at most)
REDIRECT_RULES = ("destination", "card", "spelling", "link_name")
UNRESOLVABLE_LOCALITY = "?"   # geo.tour_place.locality_norm of a generic name with no locality (pipeline writes it)


def _name_locality(raw: Optional[str], locality_norm: Optional[str], *, link: bool) -> Optional[str]:
    """The locality a name key needs: '' for a distinctive name, the locality for a generic one, None when the name
    is generic and its locality is unknown (no key: a generic name is never matched across towns, design §4).
    The row's locality_norm is the locality of its LINK name (set iff the link name was generic when built), so a
    link name is generic when locality_norm is set or the name is kind-only; an itinerary spelling only when the
    spelling itself is kind-only ('Night Market' spelt for a card-linked place has locality_norm '' → None)."""
    loc = locality_norm or ""
    loc = "" if loc == UNRESOLVABLE_LOCALITY else loc
    generic = is_generic(raw) or (link and bool(locality_norm))
    if not generic:
        return ""
    return loc or None


def _redirect_key(kind: str, *, spelling: str, link_name: str, country: str, destination, card,
                  locality: Optional[str] = "") -> Optional[str]:
    """`locality` as _name_locality (None → no name key)."""
    if kind == "destination":
        return str(destination) if destination else None
    if kind == "card":
        return str(card) if card else None
    name = spelling if kind == "spelling" else link_name
    return f"{name}|{country}|{locality}" if name and locality is not None else None


def redirect_dropped(comps: list[Component], comp_dest: dict, old_rows: list[dict]) -> tuple[dict[str, str], dict]:
    """({dropped previous id: current component id}, counters). Call after apply_carry_over: `comps` carry their final
    ids and `comp_dest` is keyed on them. `old_rows` as carry_over_ids, plus `country_norm`, `activity` and
    `locality_norm` when known (a row without `country_norm` matches a name in any country of the tour). Pure.
    A generic name ('Night Market', 'Old Quarter') matches by spelling or link name only in the same locality: the
    Chiang Mai market never redirects to the Chiang Rai one; with no locality on either side it stays unredirected
    (counted in ids_unredirected_generic), never guessed by day distance."""
    final = {c.component_id for c in comps}
    keys: dict[str, dict[str, list[Component]]] = {k: {} for k in REDIRECT_RULES}
    for c in comps:
        loc_of = {"spelling": _name_locality(c.name, c.locality_norm, link=False),
                  "link_name": _name_locality(c.link_name, c.locality_norm, link=True)}
        for kind in REDIRECT_RULES:
            for country in ((c.country_norm, "*") if kind in loc_of else (c.country_norm,)):
                k = _redirect_key(kind, spelling=name_norm(c.name), link_name=c.name_norm, country=country,
                                  destination=comp_dest.get(c.component_id), card=c.place_card_id,
                                  locality=loc_of.get(kind, ""))
                if k:                       # "*": a previous row with no country matches the name in any country
                    keys[kind].setdefault(k, []).append(c)
    redirects: dict[str, str] = {}
    by_rule: dict[str, int] = {}
    unmatched = generic_unmatched = 0
    for o in sorted((o for o in old_rows if str(o["component_id"]) not in final), key=lambda r: str(r["component_id"])):
        day, activity = int(o["day_index"]), o.get("activity")
        country = o.get("country_norm") or "*"
        spelling, link = name_norm(o.get("name")), o.get("name_norm") or ""
        loc_of = {"spelling": _name_locality(o.get("name"), o.get("locality_norm"), link=False),
                  "link_name": _name_locality(link, o.get("locality_norm"), link=True)}
        for kind in REDIRECT_RULES:
            k = _redirect_key(kind, spelling=spelling, link_name=link, country=country,
                              destination=o.get("destination_id"), card=o.get("place_card_id"),
                              locality=loc_of.get(kind, ""))
            cands = keys[kind].get(k) if k else None
            if cands:
                best = min(cands, key=lambda c: (abs(c.day_index - day), c.activity != activity, c.day_index,
                                                 c.component_id))
                redirects[str(o["component_id"])] = best.component_id
                by_rule[kind] = by_rule.get(kind, 0) + 1
                break
        else:
            unmatched += 1
            generic_unmatched += 1 if None in loc_of.values() else 0
    return redirects, {"ids_redirected": len(redirects), "ids_redirected_by": by_rule, "ids_unredirected": unmatched,
                       "ids_unredirected_generic": generic_unmatched}


def merge_redirects(previous: Optional[dict], fresh: dict[str, str], final_ids: set[str],
                    cap: int = REDIRECTS_CAP) -> tuple[dict[str, str], int]:
    """(the source's redirects after this build, how many older ones were lost). An older redirect keeps its target
    when the target is still a component, follows this build's redirect when the target was dropped, else is lost.
    An id that is a component again (the place came back) is no longer redirected. At most `cap`, newest kept."""
    out: dict[str, str] = {}
    lost = 0
    for old, target in (previous or {}).items():
        if old in final_ids:
            continue
        t = target if target in final_ids else fresh.get(target)
        if t:
            out[old] = t
        else:
            lost += 1
    for old, t in fresh.items():
        out.pop(old, None)
        out[old] = t
    if len(out) > cap:
        out = dict(list(out.items())[-cap:])
    return out, lost
