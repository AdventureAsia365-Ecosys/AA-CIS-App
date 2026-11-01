"""AA-772 — place cards: what a real place is, where it is, how to name it, and a few checked
background facts the T8/T9 writer may cite (migration 220: acp_shared.place_card +
acp_shared.atom_place_card; migration 221: place_card_miss + place_card_alias).

Why: the A-series has no city, region or display name for a place — an atom carries the landmark
as the itinerary names it (`tour_atoms.place`) and the tour carries one country. S232: 12/20
Pawangka posts put the monastery in Luang Prabang, Ladakh or "a monastery in China"; the writer
padded thin seeds with unsourced "widely known background" that the F9 judge then flagged.

How a card is made (once per PLACE, in a job — never in an HTTP request, and never in T9):
0. Free filters first (S234): `card_skip_reason()` drops unnamed places ("ruins of an ancient
   monastery", "your hotel" — the ranking rule `names_somewhere`) and logistics places (hotels,
   camps, airports, border gates — only on stay/transit/food or untyped moments); atoms are
   grouped by (`place_norm`, tour country) so one place is drafted once; an alias or core-name
   match links an existing card with no call; a stored miss (`place_card_miss`) is not retried
   before its `retry_after`. A GENERIC name ("City Palace", "Night Market" — only kind words) is
   one group per atom, never matched by alias/core name, only by a card whose locality its
   itinerary sentence names.
1. `a3_place_card` (validate route; GPT-6 Luna since S252 migration 286, was GPT-5.6 Luna) gets the place, its
   itinerary sentence(s), the tour name and the country the tour is sold as, and returns the
   place's identity (display name, locality, region, country, naming note, aliases) plus 4-6
   short background facts. AA-774: only what the place is, its history, setting and naming — never
   access, permits, what visitors do or safety (trip facts come from the itinerary only).
2. `a3_place_card_check` (validate route; Claude Haiku, another vendor than the draft) checks the
   identity and each fact; only facts it marks "supported" are kept. Nothing about the trip
   itself (prices, hours, what the tour does) is ever a card fact.
3. Same real place → same card: `place_key` = core name | country (S234: no locality — it is LLM
   free text and split one lake into 5 cards); a generic name keeps it (core | locality | country:
   City Palace Jaipur ≠ Udaipur). A draft landing on an admin-REJECTED card's key replaces it when
   the identity differs, and is a cached miss when it repeats the rejected identity. Every atom
   of the group is linked in `atom_place_card`; the itinerary spelling and the draft's aliases go
   to `place_card_alias` (never a generic form). AA-809: before inserting, the checked draft is
   resolved against the existing cards of its country (`find_card_for_draft`: same key, alias, core
   name incl. legacy 3-part keys, a small feature-synonym fold — Waterfall = Falls, Tso = Lake); a
   hit links the atoms to that card (names + cost added, its facts untouched) instead of a duplicate.
Admins can mark a card verified or rejected, and merge duplicates (Content Quality page); a
rejected card is never shown to a writer.

S233 baseline (llm_call_log): 169 Sonnet drafts for 74 cards, $1.30; one unnamed atom was
drafted 35 times by T9 jobs (no negative cache). Guard: ≤1 draft per place group per call.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from json_repair import repair_json

from services.acp_contract.ranking_reference import PLACE_KINDS, names_somewhere
from shared import countries as _countries
from shared.llm_client.call_log import billed_fields, record_call_with_pool
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest

logger = structlog.get_logger()

MAX_FACTS = 6
MAX_ALIASES = 6
# GPT-6 Luna (migration 286) reasons inside max_tokens: at 1500 it returned an empty reply (S246), so an unusable
# draft became a paid no_name miss. S252 eval at 4000: max 1,384 output tokens, all end_turn; llm_call_log history
# p90 1,956. Billing is per token used, so the headroom costs nothing. The check stays at 1024 (Haiku 4.5 in the
# same eval: max 490, all end_turn).
DRAFT_MAX_TOKENS = 4000
CHECK_MAX_TOKENS = 1024
_CONCURRENCY = 4
# 'visiting' / 'practical' stay valid so cards drafted before AA-774 still validate and the admin view
# lists them; the drafter no longer asks for them and the writer seed never shows them
# (atom_seed.WRITER_FACT_KINDS).
FACT_KINDS = ("what_it_is", "history", "setting", "visiting", "practical", "naming")

# Bump when card_skip_reason()/place_norm() change: stored misses written under an older rule are
# then tried again (the PROMPT_SHA below does the same for a prompt change).
RULE_VERSION = 1
# How long a paid miss is trusted before the place may be drafted again.
MISS_RETRY = {
    "no_name": timedelta(days=30),            # the writer could not identify one real place
    "identity_rejected": timedelta(days=14),  # the checker rejected the identity
    "error": timedelta(days=1),               # exception / timeout — retry soon
}
# S262 (AA-991 probe) — the atom-level marker of a group left for a paid draft (`max_places` cut: the $0 relink after
# atomize, or a build over its 40-place cap). Not a miss: no place_card_miss row, card_id stays NULL, and every
# drafting path (place_cards_build, T8) still drafts the atom; a draft replaces the row (_LINK_SQL / _record_miss).
NEEDS_DRAFT = "needs_draft"

_CARD_SYSTEM = """You write a short reference card about one real place that appears in a travel \
itinerary. The card is used by writers who must not get basic facts wrong.

Return ONLY JSON:
{"display_name": "...", "locality": "...", "region": "...", "country": "...",
 "naming_note": "...", "aliases": ["..."], "facts": [{"text": "...", "kind": "..."}]}

- display_name: the place's usual English name (fix spelling variants, e.g. "Pawangka" -> the \
common spelling, mentioning the variant in naming_note).
- locality: the town, city, district or village the place is IN (its municipality). A park, garden, \
cave, monument, statue, temple, market or street in a city or district has that city or district as \
its locality. Empty string only when the place is outside any settlement (a national park, nature \
reserve, conservation area or wildlife sanctuary; a summit, peak, mountain, pass, trek camp, base camp \
or glacier; a lake outside any town; a trek trail in wild terrain), when the place is itself a town or \
village, or when you are not sure. Never the nearest town, trailhead or a neighbouring village instead.
- region: the first-level administrative division the place is in TODAY (a state, province, \
administrative region, or a city with province status), using its current name after any merger or \
reform. Never a geographic, trekking or marketing area: not "Northern Mongolia", "Central Vietnam", \
"Khumbu", "Everest Region" or "North Malé Atoll". A city that is itself a first-level unit (e.g. \
Almaty, Bishkek, Tashkent, Seoul, Busan, Hanoi, Huế, Da Nang, Bangkok, Phnom Penh, Ulaanbaatar, \
Beijing) is the region of every place in it and of itself: fill it, even when it equals the locality \
or the display name. Empty string when the place is itself a province or state, or you are not sure. \
Short names only.
- A place that spans a border (a border, a boundary river, an international bridge or crossing): \
locality and region empty, or both sides written as "A / B".
- country: the one country it is in today (a route may cross a border, so this can differ from \
the country the tour is sold as).
- naming_note: one sentence on how to name the location for an international travel audience \
(e.g. a widely used region name), and any spelling variants. Empty string if nothing to say.
- aliases: up to 4 other names or spellings people use for this same place (may be empty).
- facts: 4-6 short, standalone, well-established facts about the PLACE ITSELF (what it is, its \
history and era, its setting and what is physically there). Never write how visitors reach it, what \
visitors do or see, permits or access, or safety advice: those are trip facts and come only from the \
itinerary. kind is one of: what_it_is, history, setting, naming.
- Each fact describes the place only. Never end a background fact with a clause about travellers: \
how people reach it (roads, drives, distances by road, gateways, staging or stopping points), the \
views visitors get, what visitors do there, which other sites it is combined with, or "making it a \
stop / base / standard inclusion". Write such content not at all; a fact that is only about \
reaching or visiting the place is omitted.
- Only facts you are confident are widely documented. No prices, fees, opening hours, \
visitor numbers, rankings or superlatives, and nothing about the tour or trip itself. \
Write a number only if it is a well-established fact (e.g. a founding century). An exact number \
(a year, date, elevation, height, length, area or count) only when it is well known and you are \
certain of it; otherwise round it (a decade, a century, "over 3,000 metres") or leave it out. Do not \
add a person's birth and death years.
- No claims about the place's CURRENT status or administrative role (what it is the capital or seat \
of, what a building is used for now, whether it still operates or continues to serve): such claims go \
out of date after reforms, closures and restorations. Write them in the past tense with a date, or not \
at all. Name a designer or architect only when the attribution is certain and widely documented.
- If the itinerary name is too generic to identify one real place (e.g. "the market", \
"hotel"), return {"display_name": "", "facts": []}."""

_CHECK_SYSTEM = """You check a reference card about a real place. For the place identity and \
for each numbered fact, answer whether it is accurate and well-established.

Return ONLY JSON: {"identity_ok": true/false, "locality_ok": true/false, "region_ok": true/false, \
"verdicts": ["supported" | "unsure" | "wrong", ...]}
- identity_ok: the display name and country are correct for the place the itinerary means. A tour \
may cross a border: do not reject only because the card's country is a neighbouring country of the one \
the tour is sold as.
- locality_ok: false when the locality is not the town, city or village the place is IN — for example \
the nearest town of a place inside a national park, on a mountain, at a trek camp, pass, glacier or \
lake, or a neighbouring village. true when it is right or empty. A park, garden, cave, monument, \
statue, temple, market or street inside a city or district is IN that city or district (true).
- region_ok: false when the region is not the first-level administrative division (state, province, \
administrative region, city with province status) the place is in today — for example a geographic, \
trekking or marketing area ("Northern Mongolia", "Central Vietnam", "Khumbu", "North Malé Atoll") or a \
name replaced by an administrative reform. true when it is right or empty. A city that is itself a \
first-level unit (Almaty, Busan, Huế, Bangkok …) is a right region, also when it equals the locality \
or the display name.
- A place that spans a border (a border, a boundary river, an international bridge or crossing): a \
single locality or region on one side is false; empty or both sides as "A / B" is true.
- One verdict per numbered fact, same order. "supported" only when you are confident the fact \
is correct and widely documented; "unsure" when you cannot confirm it; "wrong" when it is false.
- Check every exact number in a fact on its own (years, dates, elevations, heights, lengths, areas, \
counts): "supported" only when you are confident that each number is exactly right; one number you \
cannot confirm makes the fact "unsure", one you know is off makes it "wrong".
- Answer "unsure" for any claim about the place's CURRENT status or administrative role (is the \
capital or seat of something, currently, still, continues to, now serves as, remains): reforms, \
closures and restorations make such claims go out of date and you have no source. Answer "unsure" \
for who designed it ("designed by", an architect's name): the card carries no source for it."""

# A changed prompt invalidates every stored miss (a new prompt may identify the place). It never
# re-drafts an existing card: ensure_place_cards() skips linked atoms and reuses cards by alias / core
# name, and its only callers are bounded (T8: one atom; place_cards_build: one tour, MAX_PLACES).
# AA-774 changed the facts bullet: old cards keep their facts and are filtered at seed time.
# AA-808 added the traveller-clause bullet (hash changes with it; existing cards are not re-drafted).
# AA-784 (S253) added the locality / region / current-status rules and the check's locality_ok / region_ok: the hash
# change retries stored misses once; existing cards change only through rebuild_place_cards().
# AA-784 (S253 round 3) added urban localities, first-level cities as regions, border-spanning places and the exact
# number rule (same effect: misses retried once, no card re-drafted).
PROMPT_SHA = hashlib.sha256(f"{_CARD_SYSTEM}\n{_CHECK_SYSTEM}\nfacts={MAX_FACTS}".encode()).hexdigest()[:16]


_ATOM_PLACE_QUERY = """
    SELECT ta.atom_id, ta.place, ta.evidence, ta.itinerary_day, ta.activity_type, rt.country,
           pt.aa_name AS tour_name
    FROM acp_contract.v_active_tour_atoms ta
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = ta.tour_id
    WHERE ta.atom_id = ANY($1::text[]) AND NOT ta.deleted AND NOT ta.is_empty_marker
      AND ta.place IS NOT NULL AND btrim(ta.place) <> ''
"""

CARD_FOR_ATOM_QUERY = """
    SELECT pc.card_id, pc.display_name, pc.locality, pc.region, pc.country, pc.naming_note,
           pc.facts, pc.status
    FROM acp_shared.atom_place_card apc
    JOIN acp_shared.place_card pc ON pc.card_id = apc.card_id
    WHERE apc.atom_id = $1 AND pc.status <> 'rejected'
"""


# ── Names: fold, place_norm, core key ─────────────────────────────────────────────────────────

# Letters NFKD does not decompose (Vietnamese đ, Nordic ø, …).
_FOLD_MAP = str.maketrans({"đ": "d", "Đ": "D", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "ß": "ss",
                           "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ı": "i", "’": "'"})
_LEADING_FILLER = frozenset({"the", "a", "an", "your", "our", "local", "standard"})
# Kind words that do not change WHICH place is meant ("Lake Khövsgöl" = "Khövsgöl Lake" =
# "Khövsgöl Nuur"; "Paro Town" = "Paro"). Deliberately NOT dzong/monastery/museum/river/valley:
# "Punakha" (town) and "Punakha Dzong" are two places. Stripped only at the EDGES of a name and
# only while a distinctive word remains ("City Palace" stays "city palace", never "palace").
_NEUTRAL_KIND_WORDS = frozenset({"lake", "nuur", "town", "city", "els", "volcano", "uul"})
_NEUTRAL_KIND_PAIRS = (("sand", "dunes"),)
# Words that only say what KIND of place it is (ranking's kind nouns + qualifiers + when, plus
# common landmark nouns the Japan-first ranking list lacks). A name made only of these
# ("City Palace", "Night Market", "Old Town", "National Museum") exists in many cities of one
# country, so its card key carries the locality and it is never reused across tours by name.
_GENERIC_PLACE_WORDS = frozenset(
    {w.lower() for w in PLACE_KINDS} | _NEUTRAL_KIND_WORDS | {
        "sand", "dunes", "palace", "palaces", "fort", "fortress", "citadel", "monastery", "pagoda",
        "cathedral", "church", "mosque", "tomb", "tombs", "square", "bazaar", "gate", "tower", "wall",
        "cave", "caves", "hill", "hills", "desert", "plaza", "bay", "canal", "springs", "spring", "hot",
        "royal", "summer", "winter", "food", "floating", "botanical", "zoo", "ruins", "monument",
        "memorial", "stupa", "fishing", "harbor", "viewpoint", "view", "point", "cliffs", "rice",
        "terraces", "tea", "plantation", "farm", "wildlife", "sanctuary", "reserve", "war", "art",
        "history", "folk", "cultural", "heritage", "craft", "crafts", "handicraft", "silk", "workshop",
        "complex", "site", "fishermen", "of", "and", "s",
    })
_PAREN_RE = re.compile(r"\(([^)]*)\)")


def fold(s: Optional[str]) -> str:
    """Accent-folded, lowercased, punctuation-free text: 'Điện Biên Phủ' == 'dien bien phu',
    'Khövsgöl' == 'khovsgol' (the S233 `_norm` kept ASCII only: 'kh vsg l')."""
    text = unicodedata.normalize("NFKD", (s or "").translate(_FOLD_MAP))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower().replace("'", "")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def place_norm(place: Optional[str]) -> str:
    """The grouping key of an itinerary place: fold() without leading articles/fillers
    ('the Standard Ger Camp' == 'ger camp')."""
    words = fold(place).split()
    while len(words) > 1 and words[0] in _LEADING_FILLER:
        words = words[1:]
    return " ".join(words)


def _base_words(name: Optional[str]) -> list[str]:
    return place_norm(_PAREN_RE.sub(" ", name or "")).split()


def _distinctive(words: list[str]) -> bool:
    return any(w not in _GENERIC_PLACE_WORDS for w in words)


def is_generic_name(name: Optional[str]) -> bool:
    """True when the name (outside any parenthetical) is made only of kind words / qualifiers —
    'City Palace', 'Lake Palace', 'Night Market', 'Old Town' — so it names a different place in
    every city. Such a name is never matched across tours by alias or core name."""
    words = _base_words(name)
    return bool(words) and not _distinctive(words)


def core_key(name: Optional[str]) -> str:
    """place_norm() of the name outside any parenthetical, minus neutral kind words at its edges
    ('Lake Khövsgöl' == 'Khövsgöl Lake' == 'khovsgol'). A word is only stripped when the words left
    start and end with a distinctive word, so a generic name keeps every word ('City Palace' !=
    'Lake Palace') and 'Lake' stays 'lake'."""
    words = _base_words(name)
    while True:
        edges = [tuple(p) for p in _NEUTRAL_KIND_PAIRS] + [(w,) for w in _NEUTRAL_KIND_WORDS]
        options = [words[len(e):] for e in edges if tuple(words[:len(e)]) == e]
        options += [words[:-len(e)] for e in edges if tuple(words[-len(e):]) == e]
        # The word left at the cut edge must itself be distinctive: 'Hoi An Old Town' keeps 'town'
        # ('hoi an old' would read as a different name), 'Khövsgöl Lake' drops it.
        rest = next((r for r in options if r and _distinctive(r[:1]) and _distinctive(r[-1:])), None)
        if rest is None:
            return " ".join(words)
        words = rest


def name_forms(name: Optional[str]) -> set[str]:
    """Every place_norm() a name stands for: the whole name, the name without its parenthetical,
    and the parenthetical itself ('Bayanzag (Flaming Cliffs)' → also 'bayanzag', 'flaming cliffs')."""
    out = {place_norm(name), place_norm(_PAREN_RE.sub(" ", name or ""))}
    out |= {place_norm(p) for p in _PAREN_RE.findall(name or "")}
    return {f for f in out if f}


def place_key(display_name: str, country: str, locality: Optional[str] = None) -> str:
    """S234: core name | country — locality is LLM free text and split one lake into 5 cards.
    A generic name ('City Palace') keeps the locality: core | locality | country, so City Palace
    Jaipur and City Palace Udaipur stay two cards (migration 220's invariant)."""
    if is_generic_name(display_name):
        return f"{core_key(display_name)}|{fold(locality)}|{fold(country)}"
    return f"{core_key(display_name)}|{fold(country)}"


def normalise_card_country(raw: Optional[str], tour_country: Optional[str]) -> str:
    """AA-990 — the ONE ISO country a new card is stored under (its canonical shared.countries name); a compound is
    never stored as a country. The model's free text ('Viet Nam', 'India and Pakistan', 'Pakistan, India, China')
    goes through shared.countries.one_country — the same rule services/places/pipeline.canonical keys place names on:
    whole first; else its parts (split on , / & ; and or — never on '-': Timor-Leste), the tour's country when it is
    one of them, else the first part. Nothing resolves -> the tour's
    country (today's fallback), canonical when it resolves. The place may be in another country than the tour
    (cross-border): a 'Tanzania' card on a tour sold as India stays Tanzania. Code only: the prompts are unchanged
    (a prompt change rotates PROMPT_SHA and pays for re-drafts)."""
    one = _countries.one_country(raw, tour_country)
    if one:
        return one.name
    tour = _countries.resolve(tour_country)
    if tour:
        return tour.name
    return (raw or tour_country or "").strip()


def atom_group_key(atom: dict) -> tuple[str, str]:
    """The (place_norm, country_norm) group an atom is drafted with. A generic itinerary name
    ('City Palace', 'Night Market') is its own group per atom — two tours' City Palaces may be in
    two cities — so its stored miss never blocks the rest of the country either."""
    norm = place_norm(atom["place"])
    if is_generic_name(atom["place"]):
        norm = f"{norm} @{atom['atom_id']}"
    return norm, fold(atom.get("country"))


def pick_local_card(place: str, country: str, text: str, cards: list[dict]) -> Optional[str]:
    """The one existing card for a generic itinerary name whose locality the itinerary text names
    ('City Palace' + 'Udaipur' in the sentence → the Udaipur card), or None (draft instead).
    `cards`: card_id, display_name, locality, country. Free; ambiguity → None."""
    form, words = place_norm(place), f" {fold(text)} "
    hits = {str(c["card_id"]) for c in cards
            if fold(c.get("country")) == fold(country) and form in name_forms(c["display_name"])
            and fold(c.get("locality")) and f" {fold(c.get('locality'))} " in words}
    return hits.pop() if len(hits) == 1 else None


# ── Free filter (C9: one rule with ranking) ───────────────────────────────────────────────────

# Logistics places: where the tour sleeps, eats or changes transport. "Base camp" is an attraction
# (Everest Base Camp), so it is kept.
_LOGISTICS_PLACE_RE = re.compile(
    r"\b(hotels?|resorts?|guest ?houses?|lodges?|accommodations?|airports?|stations?|terminals?|"
    r"piers?|jetty|jetties|(?<!base )camps?|homestays?|restaurants?|cafes?|"
    r"border (gate|crossing|post)s?|checkpoints?|information cent(er|re)s?)$")
_POSSESSIVE_RE = re.compile(r"^(your|our)\b")
# The logistics noun is trusted only when atomize also typed the moment as logistics (or left it
# untyped): "Hua Hin Railway Station" / "Dorasan Station" / "Everest View Hotel" typed culture or
# trek stay rankable moments, so they keep their card (lessons: a rule exclusion must match the
# next step's final decision — ranking has no logistics-noun rule).
LOGISTICS_ACTIVITY_TYPES = frozenset({"stay", "transit", "food"})


def card_skip_reason(place: Optional[str], activity_type: Optional[str] = None) -> Optional[str]:
    """Why a place gets no card without any LLM call, or None to card it.

    'unnamed' — the ranking rule (`names_somewhere`, ADR 0020: the same function
    `atom_ranking.classify_exclusion` uses for `unnamed_place`) or a 'your/our …' place;
    'logistics_place' — the folded place ends in a lodging/transport/eating noun AND the atom is
    typed stay/transit/food (or untyped)."""
    if not (place or "").strip():
        return "empty"
    folded = fold(place)
    if not names_somewhere(place) or _POSSESSIVE_RE.match(folded):
        return "unnamed"
    if _LOGISTICS_PLACE_RE.search(folded) and (not activity_type or activity_type in LOGISTICS_ACTIVITY_TYPES):
        return "logistics_place"
    return None


# ── LLM calls ──────────────────────────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = repair_json(text, return_objects=True)
    return data if isinstance(data, dict) else {}


# AA-808 — the drafter appends traveller clauses to background facts ("…, making it a natural
# stopping point", "offers panoramic views"): S235 card-kinds audit, 31 writer-kind facts both
# reviewers moved out. A writer-kind fact matching one of these is re-kinded so the writer seed
# (atom_seed.WRITER_FACT_KINDS) never shows it. Measured on that audit: 31/31 leaks caught, 0/22
# pure-background facts and 2/437 untouched background facts hit (tests/unit/test_aa808_*).
# Practical (getting there) is checked before visiting (what visitors see / do).
_KM = r"\d[\d,.]*\s?(?:km|kilomet(?:re|er)s)"
_TRAVELLER_CLAUSES = tuple((kind, name, re.compile(rx, re.IGNORECASE)) for kind, name, rx in (
    ("practical", "travellers", r"\btravell?ers?\b"),
    ("practical", "hub", r"\b(?:transit hub|staging point|stopping point|departure hub|arrival hub|"
                         r"entry (?:and exit )?points?)\b"
                         r"|\bstarting point for (?:overland|journeys|travel|trips|treks)\b"
                         r"|\bgateway (?:town )?(?:for|to) (?:travell?ers|visitors|tourists|overland)\b"),
    ("practical", "reach", r"\b(?:reached|reachable) (?:from|by|via|on|only)\b|\bto reach (?:it|the)\b"),
    ("practical", "access", r"\baccess (?:requires|is|via|by|from|to the)\b|\baccessible (?:by|via|from|only)\b"),
    ("practical", "road", r"\b(?:drive|road|highway) (?:to|from|into)\b|\bthe drive\b"),
    ("practical", "km_by_road", rf"\b{_KM}\b[^.;]{{0,60}}\b(?:road|highway|drive)\b"
                                rf"|\b(?:road|highway|drive)\b[^.;]{{0,40}}\b{_KM}\b"),
    ("practical", "approach", r"\bthe approach\b|\bapproach (?:trail|road|path|route)\b"),
    ("practical", "permit", r"\bpermits?\b"),
    ("practical", "trailhead", r"\b(?:car park|parking|round-trip|trailhead)\b"),
    ("practical", "ride", r"\b(?:horse|reindeer|pony|camel)(?:back)?(?: or \w+)? rides?\b"
                          r"|\bon (?:bicycle|bike|horseback)\b"),
    ("practical", "utilities", r"\b(?:electricity|power) grid\b|\bmobile (?:signal|coverage|network)\b"),
    ("practical", "overland", r"\boverland (?:travel|journeys?)\b"),
    ("visiting", "views", r"\b(?:offers?|offering|provides?|providing|commands?|commanding|with)\b[^.;]{0,40}\bviews?\b"
                          r"|\bpanoramic\b|\bviews? (?:of|over|toward|towards|across)\b"),
    ("visiting", "visible", r"\bvisible from\b|\bon clear days\b|\bin clear (?:conditions|weather)\b"),
    ("visiting", "visitors", r"\b(?:accessible|open|popular) (?:to|with|among) (?:visitors|tourists|the public)\b"
                             r"|\bvisitors (?:can|may|often|usually|typically)\b"
                             r"|\b(?:often|commonly|usually|frequently) visited\b|\bvisited together\b"
                             r"|\bvisits? (?:to|in)\b"),
    ("visiting", "trails", r"\b(?:walking|hiking|trekking) (?:trails?|routes?|paths?)\b"),
    ("visiting", "viewpoint", r"\bviewpoints?\b"),
    ("visiting", "combined", r"\b(?:combined|paired) with\b|\b(?:standard|common|popular) (?:inclusion|stop)\b"
                             r"|\binclusion on\b"),
    ("visiting", "making_it", r"\bmaking (?:it|the \w+)\b[^.;]{0,60}"
                              r"\b(?:stop|base|inclusion|experience|climb|popular)\b"),
))
_WRITER_KINDS = frozenset({"what_it_is", "history", "setting", "naming"})  # = atom_seed.WRITER_FACT_KINDS


def traveller_clause(text: str) -> Optional[tuple[str, str]]:
    """(kind, pattern name) when a fact talks about reaching or visiting the place — the kind it
    belongs to ('practical' / 'visiting') — or None for a pure background fact."""
    for kind, name, rx in _TRAVELLER_CLAUSES:
        if rx.search(text):
            return kind, name
    return None


# AA-784 (S253 round 3): a person's life dates in parentheses after a name — 'Kruba Srivichai (1878–1938)', '(born
# 1878)', '(d. 1939)', '(c. 1550 – 1612)'. The r2 spot check found 3 false kept facts, all small numeric slips; one
# was such a death year (1938 for 1939). The dates are never what the sentence is about, so they are dropped and the
# sentence is kept. A single year in parentheses ('completed (1927)') is left alone.
_YEAR = r"(?:c\.\s*|ca\.\s*|circa\s+)?\d{3,4}(?:\s*(?:CE|AD|BCE|BC))?"
_LIFE_DATES_RE = re.compile(
    r"(\b\w[\w'’.-]*)\s*\("
    rf"(?:(?P<range>{_YEAR}\s*(?:[–—-]|to)\s*{_YEAR})|(?:born|b\.|died|d\.)\s*{_YEAR})"
    r"\)")
# Review of round 3: a year RANGE after a capitalised word is also a polity, period, event or building span
# ('Ayutthaya Kingdom (1351–1767)', 'The Edo Period (1603–1868)', 'World War II (1939–1945)', 'Suryavarman II
# (1113–1150)' a reign) — often the fact's own content. Such a range is kept: after one of these head nouns, after a
# Roman numeral (a regnal name or a numbered war: the range is usually the reign), or when it is not life-span long.
_SPAN_HEAD_NOUNS = frozenset({
    "dynasty", "dynasties", "kingdom", "kingdoms", "empire", "empires", "period", "periods", "era", "eras", "age",
    "war", "wars", "rebellion", "revolution", "uprising", "invasion", "occupation", "siege", "campaign", "expedition",
    "conflict", "emergency", "movement", "restoration", "republic", "sultanate", "shogunate", "caliphate", "khanate",
    "regime", "rule", "reign", "raj", "protectorate", "colony", "mandate", "confederacy", "federation", "union",
    "administration", "government", "state", "hall", "palace", "temple", "cathedral", "church", "mosque", "pagoda",
    "monastery", "bridge", "tower", "fort", "fortress", "castle", "building", "auditorium", "museum", "station",
    "railway", "line", "road", "canal", "wall", "gate", "construction"})
_ROMAN_NUMERAL_RE = re.compile(r"^[IVXLC]+$")
_LIFE_SPAN_YEARS = (15, 110)  # a notable person's recorded life span; outside it the range is a period or a build
_YEAR_PART_RE = re.compile(r"(\d{3,4})\s*(BCE|BC|CE|AD)?")


def _range_years(text: str) -> Optional[int]:
    """Length in years of a 'Y1–Y2' range ('563–483 BCE' -> 80; a lone BC marker at the end applies to both)."""
    parts = _YEAR_PART_RE.findall(text)
    if len(parts) != 2:
        return None
    (y1, m1), (y2, m2) = parts
    if m2 in ("BC", "BCE") and not m1:
        m1 = m2
    a = -int(y1) if m1 in ("BC", "BCE") else int(y1)
    b = -int(y2) if m2 in ("BC", "BCE") else int(y2)
    return b - a


def _life_dates(m: re.Match) -> bool:
    head = m.group(1)
    if not head[0].isupper():
        return False
    if m.group("range") is None:  # '(born 1878)', '(d. 1939)': a person's
        return True
    if fold(head) in _SPAN_HEAD_NOUNS or _ROMAN_NUMERAL_RE.match(head):
        return False
    years = _range_years(m.group("range"))
    return years is not None and _LIFE_SPAN_YEARS[0] <= years <= _LIFE_SPAN_YEARS[1]


def strip_life_dates(text: Optional[str]) -> str:
    """`text` without parenthesised life dates right after a capitalised word — a name: 'Kruba Srivichai (1878–1938)
    is remembered …' -> 'Kruba Srivichai is remembered …'. Kept: after a lowercase word ('the hall (1905–1908)'), a
    range after a polity / period / event / building noun or a Roman numeral ('Ayutthaya Kingdom (1351–1767)',
    'Suryavarman II (1113–1150)'), and a range that is not life-span long (under 15 or over 110 years)."""
    return _LIFE_DATES_RE.sub(lambda m: m.group(1) if _life_dates(m) else m.group(0), text or "")


def _clean_facts(raw_facts, card_name: str = "") -> list[dict]:
    out = []
    for f in raw_facts if isinstance(raw_facts, list) else []:
        if isinstance(f, dict) and isinstance(f.get("text"), str) and f["text"].strip():
            kind = f.get("kind") if f.get("kind") in FACT_KINDS else "what_it_is"
            text = " ".join(strip_life_dates(f["text"]).split())
            hit = traveller_clause(text) if kind in _WRITER_KINDS else None
            if hit:
                logger.info("place_card_fact_rekinded", card=card_name, pattern=hit[1],
                            from_kind=kind, to_kind=hit[0], text=text[:120])
                kind = hit[0]
            out.append({"text": text, "kind": kind})
    return out[:MAX_FACTS]


def _clean_aliases(raw) -> list[str]:
    out = [" ".join(a.split()) for a in raw if isinstance(a, str) and a.strip()] if isinstance(raw, list) else []
    return out[:MAX_ALIASES]


def _trim(text: Optional[str], limit: int = 600) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


# ── AA-784 (S253): identity and fact hygiene — deterministic, no model call ───────────────────────
# Blind spot check of the live build (S252, 20 cards, GPT-6 Luna × Haiku 4.5): 7/20 wrong locality or region and
# 3/83 kept facts false. Region: 4/7 were geographic or trekking areas, not administrative divisions ("Northern
# Mongolia", "Central Vietnam", "Khumbu", "North Malé Atoll"). Locality: 2/7 were the nearest town of a point outside
# any settlement (Barafu Camp -> Moshi, Thado Koshi -> Lukla). Facts: 2/3 were present-tense status claims gone out of
# date (Pleiku "is the province's capital" before the 2025 merger; Nuwara Eliya Post Office "continues to serve as a
# post office" while closed for restoration) and 1 an authorship claim (Zenkov Cathedral "designed by Andrei Zenkov").
# The draft and check prompts carry the same rules; these functions hold them whatever the model answers. A field they
# clear becomes empty (unknown), never a guess: an empty locality / region is not an error, a wrong one is.

_DIRECTION_WORDS = frozenset({
    "north", "northern", "south", "southern", "east", "eastern", "west", "western", "central", "middle", "upper",
    "lower", "inner", "outer", "far", "northeast", "northeastern", "northwest", "northwestern", "southeast",
    "southeastern", "southwest", "southwestern"})
# Words of administrative units' own names. A region carrying one is never cleared by the SHAPE rules below
# ("Rift Valley Province", "Central Province", "Tibet Autonomous Region"). Not "region": "Central Highlands Region".
_ADMIN_WORDS = frozenset({
    "province", "provinces", "state", "prefecture", "county", "district", "division", "governorate", "oblast", "krai",
    "department", "municipality", "aimag", "aimak", "territory", "emirate", "canton", "voivodeship", "dzongkhag",
    "autonomous", "administrative", "metropolitan", "republic", "parish"})
# Landform words: a region named by one is an area, not an administrative unit ("Kathmandu Valley", "Mekong Delta",
# "Thar Desert", "Central Highlands").
_AREA_WORDS = frozenset({
    "valley", "valleys", "highlands", "plateau", "delta", "basin", "steppe", "desert", "mountains", "range",
    "lowlands", "foothills", "plains", "gorge", "coast", "coastline", "peninsula", "archipelago", "ghats"})
# Landform words that are also in real administrative names ("Kaafu Atoll", "Andaman and Nicobar Islands"): an area
# only together with a direction word ("North Malé Atoll", "Northern Islands").
_AREA_WORDS_WITH_DIRECTION = frozenset({"atoll", "atolls", "island", "islands", "plain", "hills", "lakes", "river"})
# A direction word + a country is a geographic area ("Northern Mongolia", "Central Vietnam", "South India") except
# these first-level divisions.
_DIRECTION_COUNTRY_DIVISIONS = frozenset({
    "inner mongolia", "south australia", "western australia", "northern ireland"})
# Trekking / mountain / marketing areas the drafter gives as a region, compared after the trailing area noun
# ("Everest Region" -> "everest"). None is an administrative division.
_AREA_NOUNS = frozenset({"region", "area", "zone", "massif", "sector"})
_NON_ADMIN_AREAS = frozenset({
    "khumbu", "everest", "annapurna", "langtang", "manaslu", "helambu", "rolwaling", "gokyo", "dolpo", "makalu",
    "kanchenjunga", "kangchenjunga", "sagarmatha", "upper mustang", "lower mustang", "golden triangle", "indochina",
    "himalaya", "himalayas", "karakoram", "hindu kush", "pamir", "pamirs", "gobi", "altai", "khangai", "silk road"})
_ROUTE_WORDS = frozenset({"trek", "circuit", "trail", "route"})
# A card whose own name ends in one of these IS a first-level division: it has no region above it.
_DIVISION_NAME_ENDS = frozenset({"province", "prefecture", "aimag", "aimak", "oblast", "governorate", "dzongkhag",
                                 "state", "krai"})

# Places outside any settlement: their locality is empty, never the nearest town (Barafu Camp -> Moshi).
# Round 3 (S253 r2 spot check): ONLY protected areas, summits / peaks / mountains / passes, camps / huts, glaciers and
# lakes. Valley / gorge / canyon / desert / plateau left the list: they hold settlements, and over-emptying a right
# locality is the defect the r2 check found. A park, garden, cave, monument, statue, temple, market or street keeps
# its locality (no rule here clears it).
_REMOTE_WORDS = frozenset({
    "camp", "camps", "hut", "huts", "pass", "glacier", "glaciers", "icefall", "peak", "peaks", "summit", "mount", "mt",
    "mountain", "mountains", "volcano", "crater", "ridge", "col", "range", "lake", "lakes", "tso", "tsho", "nuur"})
_REMOTE_PHRASES = ("national park", "nature reserve", "wildlife sanctuary", "conservation area", "protected area",
                   "biosphere reserve", "marine park")
_REMOTE_LEADING = frozenset({"lake", "mount", "mt", "glacier"})
_LAKE_WORDS = frozenset({"lake", "lakes", "tso", "tsho", "nuur"})
# Cities that are themselves first-level units (round 3, S253 r2 spot check: D20 Panfilov Park in Almaty, D13 A Lưới
# in Huế). Their name is a right region for every place in them AND for the city's own card, and a right locality
# for a lake in them (Hoan Kiem Lake, Hanoi). Compared on _city_key() ('Busan Metropolitan City' -> 'busan').
# Deliberately not Kathmandu (Bagmati), Colombo (Western Province), Manila (NCR), Yangon (Yangon Region).
FIRST_LEVEL_CITIES = frozenset({
    "almaty", "astana", "nur sultan", "shymkent", "bishkek", "osh", "tashkent",
    "seoul", "busan", "incheon", "daegu", "daejeon", "gwangju", "ulsan", "sejong",
    "hanoi", "ha noi", "ho chi minh", "hue", "da nang", "danang", "hai phong", "haiphong", "can tho",
    "bangkok", "tokyo", "vientiane", "phnom penh", "ulaanbaatar", "ulan bator",
    "delhi", "beijing", "shanghai", "chongqing", "tianjin", "hong kong", "macau", "macao", "singapore",
    "kuala lumpur", "putrajaya", "labuan", "jakarta", "taipei", "new taipei", "taichung", "tainan", "kaohsiung",
    "taoyuan", "naypyidaw", "nay pyi taw", "islamabad"})
# Status words around a first-level city's name ('Seoul Special City', 'Vientiane Prefecture', 'Delhi NCT').
_CITY_STATUS_WORDS = frozenset({
    "city", "special", "metropolitan", "metropolis", "capital", "municipality", "prefecture", "territory",
    "national", "federal", "union", "sar", "nct", "administrative", "of", "the"})


def _city_key(text: Optional[str]) -> str:
    return " ".join(w for w in fold(text).split() if w not in _CITY_STATUS_WORDS)


def first_level_city(text: Optional[str]) -> bool:
    """True when `text` names a city that is itself a first-level administrative unit (FIRST_LEVEL_CITIES)."""
    key = _city_key(text)
    return bool(key) and key in FIRST_LEVEL_CITIES


# A place spanning two countries or regions (a border, a boundary river, an international bridge): its locality and
# region are empty or written as both sides 'A / B' (D19 'Huay Xai / Chiang Khong'), never one side (D03
# 'Myanmar–Thailand border' -> 'Mae Fa Luang', 'Chiang Rai').
_BORDER_WORDS = frozenset({"border", "borders", "boundary", "frontier", "tripoint"})


def _compound_country(text: Optional[str]) -> bool:
    """'Thailand and Myanmar' / 'Nepal / Tibet' / 'Pakistan, India, China': two or more countries
    (shared.countries.resolve_parts). Not one country with 'and' in its name ('Bosnia and Herzegovina')."""
    return len(_countries.resolve_parts(text)) >= 2


def cross_border(name: Optional[str], *countries: Optional[str]) -> bool:
    """The card names a border (its display name has 'border' / 'boundary' …) or one of `countries` is a compound
    ('Thailand and Myanmar': legacy cards and raw drafts; new cards store one ISO country, AA-990)."""
    if any(w in _BORDER_WORDS for w in _words(name)):
        return True
    return any(_compound_country(c) for c in countries)


def _sides(value: str) -> list[str]:
    """The sides of a value written as every side, 'Huay Xai / Chiang Khong' -> ['Huay Xai', 'Chiang Khong'], or []
    for one side. Only '/' separates sides (the prompts' "A / B"): a comma is 'Town, Province' ('Mae Sai, Chiang
    Rai': one side)."""
    parts = [p.strip() for p in value.split("/")]
    return parts if len(parts) > 1 and all(parts) else []


_PASS_SUFFIX = "la"  # Tibetan / Ladakhi "pass" as the LAST word ("Khardung La", "Thorong La"), never "La Paz"
# A village is its own locality: a different settlement there is a neighbouring one (Thado Koshi -> Lukla).
# Nepali "-gaon" / "-gaun" (village) only on a Nepal card: "Goregaon" is a Mumbai suburb.
_VILLAGE_WORDS = frozenset({"village", "villages"})
_VILLAGE_SUFFIX_COUNTRIES = {"nepal": ("gaon", "gaun")}


def _words(text: Optional[str]) -> list[str]:
    return fold(text).split()


def _bare(text: Optional[str]) -> str:
    """core_key() without administrative words: 'Bulgan Province' == 'Bulgan', 'Paro Town' == 'Paro'."""
    return " ".join(w for w in core_key(text).split() if w not in _ADMIN_WORDS)


_COUNTRY_NAMES: dict[str, str] = {}


def _country_by_name(text: Optional[str]) -> Optional[str]:
    """The canonical country a text names by its name or ISO short name ('Vietnam', 'Viet Nam'), or None. Not by an
    alias or a code: shared.countries aliases include sub-national names ('Tibet' -> China) that are real regions."""
    if not _COUNTRY_NAMES:
        for c in _countries.all_countries():
            _COUNTRY_NAMES.setdefault(fold(c.name), c.name)
            _COUNTRY_NAMES.setdefault(fold(c.iso_name), c.name)
    return _COUNTRY_NAMES.get(fold(text))


def region_problem(region: Optional[str], *, name: Optional[str] = None,
                   country: Optional[str] = None) -> Optional[str]:
    """Why `region` is not a first-level administrative division of the card, or None to keep it.

    'country' — the region is the card's country; 'is_place' — the region is the place itself, or the place is itself
    a first-level division ('Bulgan Province'); 'trek_area' — a trekking / mountain / marketing area ('Khumbu',
    'Everest Region', 'Annapurna Circuit'); 'geographic' — a direction + a country ('Northern Mongolia',
    'Central Vietnam') or a landform area ('Kathmandu Valley', 'North Malé Atoll'). A list of regions
    ('Ladakh, Gilgit-Baltistan, Xinjiang': a range across borders) is left alone."""
    words = _words(region)
    if not words:
        return None
    whole = _country_by_name(region)
    if whole is not None:
        card_country = _countries.resolve(country)
        return "country" if card_country is None or whole == card_country.name else None
    name_words = _words(name)
    # exact name only: 'Khövsgöl Lake' sits in 'Khövsgöl Province', a town 'Bulgan' in 'Bulgan Province'
    # round 3: a first-level city is its own region ('Busan' on the Busan card, 'Hue' on the Huế card)
    if name_words and ((words == name_words and not first_level_city(region)) or name_words[-1] in _DIVISION_NAME_ENDS
                       or name_words[-2:] == ["autonomous", "region"]):
        return "is_place"
    if words[-1] in _ROUTE_WORDS:
        return "trek_area"
    stem = list(words)
    while len(stem) > 1 and stem[-1] in _AREA_NOUNS:
        stem.pop()
    if " ".join(stem) in _NON_ADMIN_AREAS:
        return "trek_area"
    if any(w in _ADMIN_WORDS for w in words):
        return None
    directed = any(w in _DIRECTION_WORDS for w in words)
    if any(w in _AREA_WORDS for w in words) or (directed and any(w in _AREA_WORDS_WITH_DIRECTION for w in words)):
        return "geographic"
    if directed and " ".join(words) not in _DIRECTION_COUNTRY_DIVISIONS:
        rest = " ".join(w for w in words if w not in _DIRECTION_WORDS)
        if rest and _country_by_name(rest) is not None:
            return "geographic"
    return None


def _remote_name(name: str, *, lakes: bool = True) -> bool:
    """The name's HEAD is a remote kind: its last word ('Barafu Camp', 'Ghurchi Lagna Pass', 'Thorong La'), a leading
    'Lake' / 'Mount' ('Mount Everest'), or a park phrase at its end ('Kilimanjaro National Park'). A building named
    after one is not remote: 'Sagarmatha National Park Visitor Centre' is in Namche Bazaar, 'Lake Palace' in Udaipur.
    Each form of the name counts ('Tonle Sap (Tonle Sap Lake)'). `lakes=False`: a lake head does not count (a lake
    in a first-level city: 'Hoan Kiem Lake', Hanoi)."""
    for form in name_forms(name):
        words = form.split()
        if not words:
            continue
        head = words[-1] in _REMOTE_WORDS and (lakes or words[-1] not in _LAKE_WORDS)
        lead = words[0] in _REMOTE_LEADING and (lakes or words[0] not in _LAKE_WORDS)
        if (head or lead or any(form.endswith(p) for p in _REMOTE_PHRASES)
                or (len(words) > 1 and words[-1] == _PASS_SUFFIX)):
            return True
    return False


def _village_name(name: str, country: Optional[str]) -> bool:
    words = _words(name)
    suffixes = _VILLAGE_SUFFIX_COUNTRIES.get(fold(country), ())
    return any(w in _VILLAGE_WORDS for w in words) or any(w.endswith(suffixes) for w in words if suffixes)


def locality_problem(locality: Optional[str], *, name: Optional[str], region: Optional[str] = None,
                     country: Optional[str] = None, place_names: tuple = ()) -> Optional[str]:
    """Why `locality` is not the settlement the place is IN, or None to keep it.

    'is_place' — the locality is the place itself (a town's card); 'remote_place' — the place (its display name or
    an itinerary name) is outside any settlement: a camp, hut, pass, glacier, peak, mountain, lake, national park … ;
    'village' — the place is a village and the locality is another settlement. A generic name ('City Palace') keeps
    its locality: its card key needs it.

    Round 3 (S253 r2 spot check): a locality equal to the region is KEPT. The S253 r1 'same_as_region' clear emptied
    right localities: a city named like its province (D04 Kruba Srivichai Monument, Chiang Mai / Chiang Mai), a
    first-level city (Busan / Busan, Almaty / Almaty). A park, garden, cave, monument, temple, market or street is
    never remote; a lake in a first-level city keeps that city."""
    if not _words(locality) or is_generic_name(name):
        return None
    if _bare(locality) == _bare(name):
        return "is_place"
    names = [n for n in (name, *place_names) if n]
    lakes = not first_level_city(locality)
    if any(_remote_name(n, lakes=lakes) for n in names):
        return "remote_place"
    if any(_village_name(n, country) for n in names):
        return "village"
    return None


def clean_identity(card: dict, *, place_names: tuple = (), country: Optional[str] = None) -> list[dict]:
    """Clear a drafted card's region / locality that break the AA-784 rules (in place). Region first. A place spanning
    a border (cross_border: a border name, or `country` / the card's own country a compound) keeps a field only as
    both sides 'A / B' ('cross_border' otherwise), each side also passing the field's own rules. Returns one
    {"field", "reason", "value"} per cleared field."""
    out = []
    name = card.get("display_name") if isinstance(card.get("display_name"), str) else ""
    spans = cross_border(name, country, card.get("country") if isinstance(card.get("country"), str) else None)
    for field in ("region", "locality"):
        value = card.get(field) if isinstance(card.get(field), str) else ""
        sides = [value]
        if spans and value.strip():
            sides = _sides(value)
        why = "cross_border" if not sides else None
        # every side still passes the field's own rules: a pass keeps no town on either side ('Sost / Tashkurgan'),
        # a country is no region ('Thailand / Myanmar')
        for side in sides:
            if why:
                break
            if field == "region":
                why = region_problem(side, name=name, country=country)
            else:
                why = locality_problem(side, name=name, region=card.get("region") or "", country=country,
                                       place_names=tuple(place_names))
        if why:
            out.append({"field": field, "reason": why, "value": value})
            card[field] = ""
    return out


# Present-tense status / administrative-role claims and authorship claims: the check keeps a fact only when it is
# sure, but it has no source and these go out of date or are commonly misattributed. Never kept on a card.
_PAST_MARKERS = r"(?:was|were|former(?:ly)?|until|once|old|historic(?:al)?|ancient|imperial|royal)"
_UNVERIFIABLE_CLAIMS = tuple((name, re.compile(rx, re.IGNORECASE)) for name, rx in (
    ("current_status", r"\b(?:currently|at present|presently|nowadays|to this day)\b|\bto date\b(?=\s*(?:[,.;]|$))"
                       r"|\b(?:is|are|remains?) still\b|\bcontinues? to\b"
                       r"|\bstill (?:serves?|functions?|operates?|houses?|stands?|in use|used|active|working|runs?)\b"
                       r"|\b(?:serves?|functions?|operates?) as\b"
                       r"|\b(?:today|now)\b[^.;]{0,40}\b(?:serves?|houses?|functions?|operates?|used as|home to|"
                       r"part of|belongs? to)\b"),
    ("admin_role", rf"\b(?:is|are|remains?)\b(?:(?!\b{_PAST_MARKERS}\b)[^.;]){{0,60}}?"
                   r"\b(?:(?<!in the )capital|administrative (?:cent(?:er|re)|seat|headquarters)|seat of)\b"),
    ("authorship", r"\b(?:designed|planned|engineered) by\b|\barchitects?\b|\bthe work of\b"),
))


# 'capital [city] [and (the) <up to 2 words> city|town] of (the) <country>': the national-capital phrasings.
_CAPITAL_OF = r"\bcapital(?: city)?(?: and (?:the )?(?:\w+ ){0,2}(?:city|town|metropolis))? of (?:the )?"
# A sentence naming one of these besides the national pattern is about a division ('the capital of Gia Lai Province
# in Vietnam', 'the provincial capital of Gia Lai, Vietnam'): not a national capital.
_DIVISION_MARKERS = _ADMIN_WORDS | {"provincial", "regional", "prefectural", "region", "regions"}


def _national_capital(text: str, country: Optional[str]) -> bool:
    """'… is the capital of Mongolia' / 'Malé is the capital and most populous city of the Maldives' / 'Hanoi is the
    capital of Viet Nam' on a card of that country: 'capital' is followed DIRECTLY by 'of (the) <the country's name or
    ISO name>', and no administrative word ('province', 'state', 'provincial' …) is in the sentence outside the
    country's own name ('Czech Republic'). 'Pleiku is the capital of Gia Lai Province in Vietnam' is not one."""
    c = _countries.resolve(country)
    names = {n for n in ({fold(country)} | ({fold(c.name), fold(c.iso_name)} if c else set())) if n}
    if not names:
        return False
    rx = re.compile(_CAPITAL_OF + "(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
                    + r")\b")
    for part in re.split(r"[.;]", text):
        f = fold(part)
        m = rx.search(f)
        if m and not set((f[:m.start()] + " " + f[m.end():]).split()) & _DIVISION_MARKERS:
            return True
    return False


def unverifiable_claim(text: Optional[str], country: Optional[str] = None) -> Optional[str]:
    """The claim kind ('current_status' / 'admin_role' / 'authorship') when a fact asserts something the check cannot
    verify without a source, or None. A national capital ('… is the capital of Mongolia' on a Mongolia card) is kept:
    stable and widely documented, unlike a provincial seat after a reform (Pleiku, 2025)."""
    text = " ".join((text or "").split())
    for name, rx in _UNVERIFIABLE_CLAIMS:
        if not rx.search(text):
            continue
        if name == "admin_role" and _national_capital(text, country):
            continue
        return name
    return None


def drop_unverifiable(facts: list[dict], country: Optional[str] = None, card_name: str = "") -> list[dict]:
    """`facts` without the unverifiable_claim() ones (logged)."""
    out = []
    for f in facts:
        why = unverifiable_claim(f.get("text"), country)
        if why:
            logger.info("place_card_fact_unverifiable", card=card_name, claim=why, text=(f.get("text") or "")[:120])
            continue
        out.append(f)
    return out


def draft_card(atom: dict, client=None) -> tuple[dict, float, object]:
    """One `a3_place_card` call. Returns (card dict, cost, response). `client`: an LLMClient-like object (AA-1026 I6:
    the contract smoke passes one that forces the acc2 fallback model); None = a new LLMClient."""
    user = (f"PLACE AS THE ITINERARY NAMES IT: {atom['place']}\n"
            f"COUNTRY THE TOUR IS SOLD AS: {atom.get('country') or 'unknown'} "
            f"(the route may cross into a neighbouring country)\n"
            f"TOUR: {atom.get('tour_name') or 'unknown'}\n"
            f"ITINERARY SENTENCE: {_trim(atom.get('evidence'))}")
    resp = (client or LLMClient()).generate(LLMRequest(system_prompt=_CARD_SYSTEM, user_prompt=user,
                                                       stage="a3_place_card", max_tokens=DRAFT_MAX_TOKENS))
    return _parse_json(resp.content), resp.cost_usd, resp


def _flag_false(value) -> bool:
    """True only for an explicit false ('false' as text too); a missing key keeps the field."""
    return value is False or (isinstance(value, str) and value.strip().lower() == "false")


def check_card(card: dict, atom: dict, client=None) -> tuple[bool, list[str], float, object, dict]:
    """One `a3_place_card_check` call. Returns (identity_ok, verdict per fact, cost, response, placement) where
    placement = {"locality_ok", "region_ok"} (AA-784: a wrong locality / region is cleared, not a rejected card)."""
    facts = card.get("facts") or []
    numbered = "\n".join(f"{i}. {f['text']}" for i, f in enumerate(facts, 1))
    user = (f"ITINERARY NAME: {atom['place']} (tour sold as {atom.get('country') or 'unknown'}; "
            f"the place may be in a neighbouring country; tour: {atom.get('tour_name') or 'unknown'}; "
            f"sentence: {_trim(atom.get('evidence'), 300)})\n"
            f"CARD IDENTITY: {card.get('display_name')} — locality: {card.get('locality') or '-'}, "
            f"region: {card.get('region') or '-'}, country: {card.get('country')}\n"
            f"FACTS:\n{numbered}")
    resp = (client or LLMClient()).generate(LLMRequest(system_prompt=_CHECK_SYSTEM, user_prompt=user,
                                                       stage="a3_place_card_check", max_tokens=CHECK_MAX_TOKENS))
    data = _parse_json(resp.content)
    verdicts = data.get("verdicts") if isinstance(data.get("verdicts"), list) else []
    verdicts = [str(v).strip().lower() for v in verdicts]
    if len(verdicts) != len(facts):
        verdicts = ["unsure"] * len(facts)  # misaligned reply → keep nothing
    placement = {"locality_ok": not _flag_false(data.get("locality_ok")),
                 "region_ok": not _flag_false(data.get("region_ok"))}
    return bool(data.get("identity_ok")), verdicts, resp.cost_usd, resp, placement


async def _log(pool, stage: str, role: str, resp, quality_signal: dict) -> None:
    await record_call_with_pool(
        pool, stage=stage, role=role, model=resp.model_used,
        tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
        cost_usd=resp.cost_usd, quality_signal=quality_signal,
        stop_reason=getattr(resp, "stop_reason", None), account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None), **billed_fields(resp),
    )


# ── Storage helpers ────────────────────────────────────────────────────────────────────────────

_LINK_SQL = """
    INSERT INTO acp_shared.atom_place_card (atom_id, card_id, miss_reason) VALUES ($1, $2::uuid, NULL)
    ON CONFLICT (atom_id) DO UPDATE SET card_id = EXCLUDED.card_id, miss_reason = NULL, resolved_at = now()
"""
# An atom-level miss never overwrites a link; it only records why the atom has no card (admin view).
_ATOM_MISS_SQL = """
    INSERT INTO acp_shared.atom_place_card (atom_id, card_id, miss_reason) VALUES ($1, NULL, $2)
    ON CONFLICT (atom_id) DO UPDATE SET miss_reason = EXCLUDED.miss_reason, resolved_at = now()
    WHERE acp_shared.atom_place_card.card_id IS NULL
      AND acp_shared.atom_place_card.miss_reason IS DISTINCT FROM EXCLUDED.miss_reason
"""
_ALIAS_SQL = """
    INSERT INTO acp_shared.place_card_alias (alias_norm, country_norm, card_id, source)
    VALUES ($1, $2, $3::uuid, $4) ON CONFLICT (alias_norm, country_norm) DO NOTHING
"""
_MISS_SQL = """
    INSERT INTO acp_shared.place_card_miss
        (place_norm, country_norm, reason, attempts, rule_version, prompt_sha, model, last_tried_at, retry_after)
    VALUES ($1, $2, $3, 1, $4, $5, $6, now(), $7)
    ON CONFLICT (place_norm, country_norm) DO UPDATE SET
        reason = EXCLUDED.reason, attempts = acp_shared.place_card_miss.attempts + 1,
        rule_version = EXCLUDED.rule_version, prompt_sha = EXCLUDED.prompt_sha, model = EXCLUDED.model,
        last_tried_at = now(), retry_after = EXCLUDED.retry_after
"""


def miss_is_active(row, now: Optional[datetime] = None) -> bool:
    """A stored miss blocks a new draft only while it is fresh and was written by the current rule
    and prompt."""
    if not row:
        return False
    now = now or datetime.now(timezone.utc)
    return (row["rule_version"] == RULE_VERSION and row["prompt_sha"] == PROMPT_SHA
            and row["retry_after"] is not None and row["retry_after"] > now)


async def _record_miss(pool, group_key: tuple[str, str], atoms: list[dict], reason: str,
                       model: Optional[str]) -> None:
    retry_after = datetime.now(timezone.utc) + MISS_RETRY[reason]
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(_MISS_SQL, group_key[0], group_key[1], reason, RULE_VERSION, PROMPT_SHA,
                           model, retry_after)
        await conn.executemany(_ATOM_MISS_SQL, [(a["atom_id"], reason) for a in atoms])


def itinerary_alias_forms(name: Optional[str]) -> set[str]:
    """name_forms() minus the generic ones — the forms safe to store as a country-wide alias."""
    return {f for f in name_forms(name) if not is_generic_name(f)}


def _alias_rows(card_id: str, atoms: list[dict], group_country: str, card: dict, card_country: str) -> list:
    """(alias_norm, country_norm, card_id, source) rows for a new card: the itinerary spelling under
    the tour's country, and the display name / parenthetical / draft aliases under both the card's
    and the tour's country (a Vietnamese place on a 'Laos' tour is looked up with 'laos').
    A generic form ('city palace', 'night market') is never an alias: it names a place in every
    city of the country."""
    rows = {}
    for a in atoms:
        for form in itinerary_alias_forms(a["place"]):
            rows.setdefault((form, group_country), "itinerary")
    countries = {c for c in (fold(card_country), group_country) if c}
    for source, names in (("display", [card.get("display_name")]), ("draft_alias", card.get("aliases") or [])):
        for name in names:
            for form in itinerary_alias_forms(name):
                for c in countries:
                    rows.setdefault((form, c), source)
    return [(form, c, card_id, source) for (form, c), source in rows.items()]


async def find_existing_cards(conn, keys: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """(place_norm, country_norm) → card_id, with no LLM call: an alias row first, then a card whose
    core name matches (new-format `place_key`, or any legacy 3-part key — S233 cards — compared
    in Python until the key backfill runs). Rejected cards are never matched. A generic name
    ('City Palace') is never matched here — see pick_local_card()."""
    keys = [k for k in keys if not is_generic_name(k[0])]
    if not keys:
        return {}
    found: dict[tuple[str, str], str] = {}
    rows = await conn.fetch(
        """
        SELECT a.alias_norm, a.country_norm, a.card_id
        FROM acp_shared.place_card_alias a
        JOIN acp_shared.place_card pc ON pc.card_id = a.card_id
        WHERE a.alias_norm = ANY($1::text[]) AND a.country_norm = ANY($2::text[]) AND pc.status <> 'rejected'
        """, [k[0] for k in keys], list({k[1] for k in keys}))
    by_alias = {(r["alias_norm"], r["country_norm"]): str(r["card_id"]) for r in rows}
    wanted = {}
    for k in keys:
        if k in by_alias:
            found[k] = by_alias[k]
        else:
            wanted[k] = f"{core_key(k[0])}|{k[1]}"
    if not wanted:
        return found
    cards = await conn.fetch(
        """
        SELECT card_id, display_name, country FROM acp_shared.place_card
        WHERE status <> 'rejected' AND (place_key = ANY($1::text[]) OR place_key LIKE '%|%|%')
        -- 3-part keys: S233 legacy cards and generic-name cards (skipped in Python)
        """, list(set(wanted.values())))
    by_core: dict[str, str] = {}
    for c in cards:
        for form in itinerary_alias_forms(c["display_name"]):
            by_core.setdefault(f"{core_key(form)}|{fold(c['country'])}", str(c["card_id"]))
    for k, ck in wanted.items():
        if ck in by_core:
            found[k] = by_core[ck]
    return found


async def find_local_cards(conn, groups: dict[tuple[str, str], list[dict]]) -> dict[tuple[str, str], str]:
    """Generic-name groups ('City Palace' on one atom) → the existing card whose locality the
    itinerary sentence names (pick_local_card), with no LLM call."""
    generic = {k: g[0] for k, g in groups.items() if len(g) == 1 and is_generic_name(g[0]["place"])}
    if not generic:
        return {}
    cards = [dict(r) for r in await conn.fetch(
        """
        SELECT card_id, display_name, locality, country FROM acp_shared.place_card
        WHERE status <> 'rejected' AND split_part(place_key, '|', 1) = ANY($1::text[])
        """, list({place_norm(a["place"]) for a in generic.values()}))]
    found = {}
    for k, a in generic.items():
        card_id = pick_local_card(a["place"], a.get("country") or "", a.get("evidence") or "", cards)
        if card_id:
            found[k] = card_id
    return found


# AA-809 — feature words that name the same KIND of water feature, folded only when a drafted card
# is compared with the existing cards of its country (never in place_key, so no key changes):
#   waterfall / waterfalls -> falls  ("Kuang Si Waterfall" = "Kuang Si Falls", Laos, Dev 14/10/2026)
# A trailing local word for "lake" is dropped while a distinctive word remains (core_key already
# drops lake / nuur at the edges):
#   tso  — Ladakhi / Tibetan "lake"   ("Pangong Tso" = "Pangong Lake")
#   tsho — Bhutanese (Dzongkha) spelling of tso ("Membar Tsho" = "Membar Lake")
#   co   — Tibetan pinyin "lake"      ("Nam Co" = "Lake Nam"); suffix only, so "Co Loa" is untouched
_FEATURE_SYNONYMS = {"waterfall": "falls", "waterfalls": "falls"}
_LAKE_SUFFIXES = frozenset({"tso", "tsho", "co"})


def match_key(name: Optional[str]) -> str:
    """core_key() with the feature-word synonyms above folded: 'Kuang Si Waterfall' ==
    'Kuang Si Falls', 'Pangong Tso' == 'Pangong Lake'. Used only to compare a draft with existing
    cards; a different place sharing a word ('Kuang Si Butterfly Park') keeps a different key."""
    words = [_FEATURE_SYNONYMS.get(w, w) for w in core_key(name).split()]
    if len(words) > 1 and words[-1] in _LAKE_SUFFIXES and _distinctive(words[:-1]):
        words = words[:-1]
    return " ".join(words)


_DRAFT_CANDIDATES_SQL = """
    SELECT card_id, place_key, display_name, locality, region, country FROM acp_shared.place_card
    WHERE status <> 'rejected' AND (place_key LIKE $1 OR lower(btrim(country)) = $2)
    -- AA-809: every non-rejected card of one country (2-part, legacy 3-part and generic keys)
"""
_ALIAS_LOOKUP_SQL = """
    SELECT a.alias_norm, a.country_norm, a.card_id
    FROM acp_shared.place_card_alias a
    JOIN acp_shared.place_card pc ON pc.card_id = a.card_id
    WHERE a.alias_norm = ANY($1::text[]) AND a.country_norm = ANY($2::text[]) AND pc.status <> 'rejected'
"""


def _card_locality(c) -> str:
    return fold(c.get("locality")) or fold(c.get("region"))


async def find_card_for_draft(conn, key: str, name: str, aliases: list[str], locality: Optional[str],
                              region: Optional[str], country: str) -> Optional[tuple[str, str]]:
    """AA-809 — the existing non-rejected card of the draft's country that a checked draft names, as
    (card_id, method), or None (insert as before). No LLM call. The first method with a hit wins:
    'place_key' (same key), 'alias' (alias table on the display name's forms), 'core_key' (same core
    name, legacy 3-part keys included), 'feature_synonym' (match_key), 'draft_alias' (alias table or
    core name on the draft's own aliases, minus any alias that is only part of the display name:
    'Punakha' for 'Punakha Dzong'). A generic name ('City Palace') only matches a generic card of the
    same core name AND locality (Jaipur != Udaipur). A method hitting two cards is ambiguous: None."""
    country_norm = fold(country)
    if not country_norm:
        return None
    cards = [dict(r) for r in await conn.fetch(_DRAFT_CANDIDATES_SQL, f"%|{country_norm}",
                                               (country or "").strip().lower())]
    cards = [c for c in cards if fold(c.get("country")) == country_norm]

    def pick(method: str, ids) -> Optional[tuple[str, str]]:
        ids = {str(i) for i in ids}
        if len(ids) > 1:
            logger.info("place_card_post_draft_ambiguous", card=name, method=method, cards=sorted(ids))
        return (ids.pop(), method) if len(ids) == 1 else None

    if is_generic_name(name):
        loc = fold(locality) or fold(region)
        hits = [c["card_id"] for c in cards if loc and is_generic_name(c["display_name"])
                and core_key(c["display_name"]) == core_key(name) and _card_locality(c) == loc]
        return pick("generic_locality", hits) if hits else None

    exact = [c["card_id"] for c in cards if c["place_key"] == key]
    if exact:
        return pick("place_key", exact)
    forms = itinerary_alias_forms(name)
    alias_forms = {f for a in aliases or [] for f in itinerary_alias_forms(a)
                   if not any(f == d or _contains_words(f, d) for d in forms)}

    async def by_alias(wanted: set[str]) -> list:
        if not wanted:
            return []
        rows = await conn.fetch(_ALIAS_LOOKUP_SQL, sorted(wanted), [country_norm])
        return [r["card_id"] for r in rows if r["country_norm"] == country_norm]

    def by_key(wanted: set[str], keyfn) -> list:
        keys = {keyfn(f) for f in wanted}
        return [c["card_id"] for c in cards
                if any(keyfn(f) in keys for f in itinerary_alias_forms(c["display_name"]))]

    hits = await by_alias(forms)
    if hits:
        return pick("alias", hits)
    for method, keyfn in (("core_key", core_key), ("feature_synonym", match_key)):
        hits = by_key(forms, keyfn)
        if hits:
            return pick(method, hits)
    hits = await by_alias(alias_forms) or by_key(alias_forms, match_key)
    return pick("draft_alias", hits) if hits else None


_REJECTED_CANDIDATES_SQL = """
    SELECT card_id, place_key, display_name, country FROM acp_shared.place_card
    WHERE status = 'rejected' AND place_key <> $1 AND lower(country) LIKE ANY($2::text[])
    -- AA-784 held: rejected cards under ANOTHER key whose stored country may be a compound naming the draft's
"""


def same_country(a: Optional[str], b: Optional[str]) -> bool:
    """AA-784 — one country written two ways: equal folded, or both resolve to the same canonical shared.countries
    name ('Russia' == 'Russian Federation', 'Viet Nam' == 'Vietnam'). A compound ('Nepal and China') resolves to
    nothing, so it only equals itself."""
    if fold(a) == fold(b):
        return True
    ca, cb = _countries.resolve(a), _countries.resolve(b)
    return ca is not None and cb is not None and ca.name == cb.name


def _compound_includes(card_country: Optional[str], country: str) -> bool:
    """The stored card's country is a COMPOUND (pre-AA-990: 'Nepal and China') that names `country`."""
    if _countries.resolve(card_country) is not None:
        return False
    parts = {c.name for c in _countries.resolve_parts(card_country)}
    return len(parts) > 1 and (_countries.name(country) or country) in parts


async def find_rejected_for_draft(conn, key: str, name: str, country: str) -> Optional[str]:
    """AA-784 — the REJECTED compound-country card a checked draft names, or None. A card stored before AA-990 under
    a compound country ('Nepal and China', C07) is never reached by a new draft: AA-990 stores one ISO country, so
    the draft has another place_key and never has the same identity (_store_card's rules cannot fire). Such a
    rejection is a hold an admin lifts (edit the card or verify it), never a duplicate under a new key. Same core name
    (or feature-synonym key), the draft's country one of the compound's. A rejected SINGLE-country card under another
    key keeps the AA-772 rule (the atoms get a fresh draft); one under the same key the _store_card rule. A generic
    name keys on its locality: not matched here."""
    if is_generic_name(name):
        return None
    c = _countries.resolve(country)
    spellings = sorted({fold(country), *(c.spellings() if c else ())} - {""})
    if not spellings:
        return None
    rows = [r for r in await conn.fetch(_REJECTED_CANDIDATES_SQL, key, [f"%{s}%" for s in spellings])
            if _compound_includes(r["country"], country)]
    forms = itinerary_alias_forms(name)
    for keyfn in (core_key, match_key):
        keys = {keyfn(f) for f in forms}
        for r in rows:
            if any(keyfn(f) in keys for f in itinerary_alias_forms(r["display_name"])):
                return str(r["card_id"])
    return None
    c = _countries.resolve(country)
    spellings = sorted({fold(country), *(s for s in (c.spellings() if c else ()))} - {""})
    if not spellings:
        return None
    rows = await conn.fetch(_REJECTED_CANDIDATES_SQL, key, [f"%{s}%" for s in spellings])
    forms = itinerary_alias_forms(name)
    for keyfn in (core_key, match_key):
        keys = {keyfn(f) for f in forms}
        for r in rows:
            if _card_in_country(r["country"], country) and any(
                    keyfn(f) in keys for f in itinerary_alias_forms(r["display_name"])):
                return str(r["card_id"])
    return None


# ── Build ──────────────────────────────────────────────────────────────────────────────────────

def _representative(atoms: list[dict]) -> dict:
    """The atom whose itinerary sentence says most, with up to 2 distinct sentences of the group."""
    rep = dict(max(atoms, key=lambda a: len(_trim(a.get("evidence")))))
    sentences = list(dict.fromkeys(_trim(a.get("evidence"), 300) for a in atoms if (a.get("evidence") or "").strip()))
    if len(sentences) > 1:
        rep["evidence"] = " / ".join(sentences[:2])
    return rep


def _same_identity(row, name: str, locality: Optional[str], region: Optional[str], country: str) -> bool:
    return all(fold(a) == fold(b) for a, b in ((row["display_name"], name), (row["locality"], locality),
                                               (row["region"], region), (row["country"], country)))


async def _store_card(conn, key: str, fields: tuple):
    """Insert the checked draft under `key`, or reuse the card already there (inside the caller's
    transaction). The existing card wins and the money spent is added to it — unless an admin
    REJECTED it: then a draft with a different identity replaces it (status back to auto_checked),
    and a draft repeating the rejected identity returns None (stored as an identity_rejected miss;
    atoms are never linked to a rejected card)."""
    name, locality, region, country, note, facts, cost = fields
    row = await conn.fetchrow(
        """
        SELECT card_id, status, display_name, locality, region, country FROM acp_shared.place_card
        WHERE place_key = $1 FOR UPDATE
        """, key)
    if row is not None and row["status"] == "rejected":
        if _same_identity(row, name, locality, region, country):
            await conn.execute(
                "UPDATE acp_shared.place_card SET cost_usd = cost_usd + $2, updated_at = now() WHERE card_id = $1",
                row["card_id"], cost)
            return None
        await conn.execute(
            """
            UPDATE acp_shared.place_card
            SET display_name = $2, locality = $3, region = $4, country = $5, naming_note = $6, facts = $7::jsonb,
                cost_usd = cost_usd + $8, status = 'auto_checked', verified_by = NULL, verified_at = NULL,
                updated_at = now()
            WHERE card_id = $1
            """, row["card_id"], *fields)
        return row["card_id"]
    if row is not None:
        await conn.execute(
            "UPDATE acp_shared.place_card SET cost_usd = cost_usd + $2, updated_at = now() WHERE card_id = $1",
            row["card_id"], cost)
        return row["card_id"]
    return await conn.fetchval(
        """
        INSERT INTO acp_shared.place_card
            (place_key, display_name, locality, region, country, naming_note, facts, cost_usd)
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
        ON CONFLICT (place_key) DO UPDATE
            SET cost_usd = acp_shared.place_card.cost_usd + EXCLUDED.cost_usd, updated_at = now()
        RETURNING card_id
        """, key, *fields)


async def _draft_checked(rep: dict, place_names: tuple, pool, signal: dict, *,
                         keep: Optional[dict] = None) -> dict:
    """Draft + AA-784 identity rules + check + unverifiable-fact filter for one place (2 paid calls, both logged).

    `keep` (rebuild_place_cards): the stored card's display_name and country — the draft's are replaced by them
    before the check, so what is checked is what is stored; a draft placing the place in another country is
    'country_changed' (no check call). Returns {"outcome": 'ok' | 'no_name' | 'identity_rejected' |
    'country_changed', "model", "cost", and when ok: "name", "locality", "region", "country", "note", "facts" (kept),
    "card" (the cleaned draft, aliases included), "cleared" (fields the rules or the check emptied)}."""
    card, cost, resp = await asyncio.to_thread(draft_card, rep)
    await _log(pool, "a3_place_card", "validate", resp, signal)
    model = getattr(resp, "model_used", None)
    out = {"outcome": "no_name", "model": model, "cost": round(cost or 0, 6)}
    name = (card.get("display_name") or "").strip() if isinstance(card.get("display_name"), str) else ""
    raw_country = card.get("country") if isinstance(card.get("country"), str) else ""
    country = normalise_card_country(raw_country, rep.get("country"))
    if not name or not country:
        return out
    if keep is not None:
        if not same_country(country, keep["country"]):
            logger.info("place_card_rebuild_country_changed", place=rep["place"], card=keep["display_name"],
                        stored=keep["country"], drafted=country)
            return dict(out, outcome="country_changed", drafted_country=country)
        name, country = keep["display_name"], keep["country"]
        card["display_name"], card["country"] = name, country
    card["facts"] = _clean_facts(card.get("facts"), name)
    card["aliases"] = _clean_aliases(card.get("aliases"))
    cleared = clean_identity(card, place_names=place_names, country=country)
    for c in cleared:
        logger.info("place_card_identity_cleaned", place=rep["place"], card=name, **c)
    ok, verdicts, cost2, resp2, placement = await asyncio.to_thread(check_card, card, rep)
    supported = [f for f, v in zip(card["facts"], verdicts) if v == "supported"]
    kept = drop_unverifiable(supported, country, name)
    for field in ("locality", "region"):
        if not placement[f"{field}_ok"] and (card.get(field) or "").strip():
            cleared.append({"field": field, "reason": "check", "value": card[field]})
            logger.info("place_card_identity_cleaned", place=rep["place"], card=name, **cleared[-1])
            card[field] = ""
    out["cost"] = round((cost or 0) + (cost2 or 0), 6)
    await _log(pool, "a3_place_card_check", "validate", resp2,
               dict(signal, identity_ok=ok, facts=len(card["facts"]), kept=len(kept),
                    unverifiable=len(supported) - len(kept), cleared=[c["field"] for c in cleared]))
    if not ok:
        logger.info("place_card_identity_rejected", atom_id=rep["atom_id"], place=rep["place"], card=name)
        return dict(out, outcome="identity_rejected")
    return dict(out, outcome="ok", name=name, country=country, card=card, facts=kept, cleared=cleared,
                locality=(card.get("locality") or "").strip() or None,
                region=(card.get("region") or "").strip() or None,
                note=(card.get("naming_note") or "").strip() or None)


async def build_card_for_group(group_key: tuple[str, str], atoms: list[dict], pool) -> tuple[Optional[str], str]:
    """Draft + check ONE card for every atom of one (place_norm, country) group and link them.
    Returns (card_id, 'linked'), (card_id, 'linked_after_draft') when the checked draft names an
    existing card (AA-809: no insert, see find_card_for_draft) or (None, miss reason); a miss is
    stored so the place is not drafted again before its retry_after."""
    rep = _representative(atoms)
    signal = {"atom_id": rep["atom_id"], "place": rep["place"], "group_atoms": len(atoms)}
    res = await _draft_checked(rep, tuple(a["place"] for a in atoms), pool, signal)
    if res["outcome"] != "ok":
        await _record_miss(pool, group_key, atoms, res["outcome"], res["model"])
        return None, res["outcome"]
    name, locality, region, country = res["name"], res["locality"], res["region"], res["country"]
    card, kept = res["card"], res["facts"]
    if is_generic_name(name) and not (locality or region):
        # 'City Palace' with no town is not one place: no card rather than one per country.
        await _record_miss(pool, group_key, atoms, "no_name", res["model"])
        return None, "no_name"
    key = place_key(name, country, locality or region)
    fields = (name, locality, region, country, res["note"], json.dumps(kept), res["cost"])
    outcome = "linked"
    async with pool.acquire() as conn, conn.transaction():
        # AA-809: the draft may name a card that exists under another key (S233 legacy 3-part key,
        # an alias, 'Waterfall' vs 'Falls'): link it, add the names and the cost, keep its facts.
        match = await find_card_for_draft(conn, key, name, card["aliases"], locality, region, country)
        if match is not None:
            card_id, method = match
            outcome = "linked_after_draft"
            logger.info("place_card_post_draft_match", atom_id=rep["atom_id"], place=rep["place"], card=name,
                        card_id=card_id, method=method)
            await conn.execute(
                "UPDATE acp_shared.place_card SET cost_usd = cost_usd + $2, updated_at = now() WHERE card_id = $1",
                card_id, fields[-1])
        else:
            rejected = await find_rejected_for_draft(conn, key, name, country)
            if rejected is not None:
                # AA-784: the draft names a rejected compound-country card (a hold): no duplicate, no link.
                logger.info("place_card_names_rejected", atom_id=rep["atom_id"], place=rep["place"], card=name,
                            rejected_card_id=rejected)
                card_id = None
            else:
                card_id = await _store_card(conn, key, fields)
        if card_id is not None:
            card_id = str(card_id)
            await conn.executemany(_LINK_SQL, [(a["atom_id"], card_id) for a in atoms])
            await conn.executemany(_ALIAS_SQL, _alias_rows(card_id, atoms, group_key[1], card, country))
            await conn.execute("DELETE FROM acp_shared.place_card_miss WHERE place_norm = $1 AND country_norm = $2",
                               group_key[0], group_key[1])
    if card_id is None:
        logger.info("place_card_same_as_rejected", atom_id=rep["atom_id"], place=rep["place"], card=name)
        await _record_miss(pool, group_key, atoms, "identity_rejected", res["model"])
        return None, "identity_rejected"
    return card_id, outcome


async def build_card_for_atom(atom: dict, pool) -> Optional[str]:
    """One atom as a one-atom group (kept for callers/tests of the S233 API)."""
    card_id, _ = await build_card_for_group(atom_group_key(atom), [atom], pool)
    return card_id


def _empty_counters(atoms: int) -> dict:
    return {"atoms": atoms, "already_linked": 0, "skipped_by_rule": 0, "places": 0, "linked_existing": 0,
            "skipped_cached": 0, "over_cap": 0, "drafted": 0, "linked_new": 0, "missed": 0, "errors": 0,
            "built": 0, "linked_after_draft": 0, "miss_recorded": 0, "needs_draft_recorded": 0}


async def ensure_place_cards(atom_ids: list[str], pool, *, force: bool = False,
                             max_places: Optional[int] = None) -> dict:
    """Give every atom in `atom_ids` its place card where one can be had, paying for at most one
    draft per (place_norm, country) group (S234 guard). Order of work: free rule skip → existing
    card by alias / core name (no call) → stored miss (no call unless `force`) → draft. Soft-fails
    per group (a card is an aid, never a reason to fail a write).

    Returns counters: atoms, already_linked, skipped_by_rule, places (groups to resolve),
    linked_existing, skipped_cached, over_cap (groups left for a later run by `max_places`),
    needs_draft_recorded (their atoms, given a NEEDS_DRAFT row), drafted, linked_new, linked_after_draft
    (AA-809: a draft that named an existing card, linked to it with no insert), missed, errors, built (atoms
    newly linked by a draft)."""
    ids = [a for a in dict.fromkeys(atom_ids) if a]
    counters = _empty_counters(len(ids))
    if not ids:
        return counters
    async with pool.acquire() as conn:
        # A link to a REJECTED card is not a card: such atoms are resolved again below.
        linked = {r["atom_id"] for r in await conn.fetch(
            """
            SELECT apc.atom_id FROM acp_shared.atom_place_card apc
            JOIN acp_shared.place_card pc ON pc.card_id = apc.card_id
            WHERE apc.atom_id = ANY($1::text[]) AND pc.status <> 'rejected'
            """, ids)}
        counters["already_linked"] = len(linked)
        rows = [dict(r) for r in await conn.fetch(_ATOM_PLACE_QUERY, [a for a in ids if a not in linked])]
    order = {a: i for i, a in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r["atom_id"], len(order)))

    groups: dict[tuple[str, str], list[dict]] = {}
    skipped: list[tuple[str, str]] = []
    for atom in rows:
        reason = card_skip_reason(atom["place"], atom.get("activity_type"))
        if reason:
            skipped.append((atom["atom_id"], reason))
            continue
        groups.setdefault(atom_group_key(atom), []).append(atom)
    counters["skipped_by_rule"] = len(skipped)
    counters["places"] = len(groups)

    async with pool.acquire() as conn:
        if skipped:
            await conn.executemany(_ATOM_MISS_SQL, skipped)
        existing = await find_existing_cards(conn, list(groups))
        existing.update(await find_local_cards(conn, groups))
        for key, card_id in existing.items():
            await conn.executemany(_LINK_SQL, [(a["atom_id"], card_id) for a in groups[key]])
            await conn.executemany(_ALIAS_SQL, [(f, key[1], card_id, "itinerary")
                                                for a in groups[key] for f in itinerary_alias_forms(a["place"])])
            counters["linked_existing"] += len(groups[key])
        todo = [k for k in groups if k not in existing]
        misses = {}
        if todo and not force:
            misses = {(r["place_norm"], r["country_norm"]): r for r in await conn.fetch(
                """
                SELECT place_norm, country_norm, reason, rule_version, prompt_sha, retry_after
                FROM acp_shared.place_card_miss
                WHERE place_norm = ANY($1::text[]) AND country_norm = ANY($2::text[])
                """, [k[0] for k in todo], list({k[1] for k in todo}))}
    now = datetime.now(timezone.utc)
    to_draft = [k for k in todo if not miss_is_active(misses.get(k), now)]
    counters["skipped_cached"] = len(todo) - len(to_draft)
    # AA-989 — a group skipped on its stored miss: its atoms get the atom-level miss row (no call), so an atom that a
    # re-atomize re-created is not counted as "never tried" (AA-991 probe place_card_links_missing).
    cached_rows = [(a["atom_id"], misses[k]["reason"]) for k in todo if k not in to_draft for a in groups[k]]
    if cached_rows:
        async with pool.acquire() as conn:
            await conn.executemany(_ATOM_MISS_SQL, cached_rows)
        counters["miss_recorded"] = len(cached_rows)
    if max_places is not None and len(to_draft) > max_places:
        counters["over_cap"] = len(to_draft) - max_places
        cut, to_draft = to_draft[max_places:], to_draft[:max_places]
        # S262 — a group left for a paid draft gets the atom-level NEEDS_DRAFT row, so "waiting for a draft" is not
        # read as "never tried / link lost" (AA-991 place_card_links_missing). card_id stays NULL: the `linked` read
        # above joins place_card, so a later build / T8 drafts these atoms and the draft replaces the row.
        pending = [(a["atom_id"], NEEDS_DRAFT) for k in cut for a in groups[k]]
        async with pool.acquire() as conn:
            await conn.executemany(_ATOM_MISS_SQL, pending)
        counters["needs_draft_recorded"] = len(pending)
    if not to_draft:
        return counters

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(key: tuple[str, str]) -> None:
        async with sem:
            counters["drafted"] += 1
            try:
                card_id, outcome = await build_card_for_group(key, groups[key], pool)
            except Exception as e:  # noqa: BLE001 — soft-fail per place
                counters["errors"] += 1
                logger.warning("place_card_build_failed", place=key[0], country=key[1], error=str(e)[:200])
                try:
                    await _record_miss(pool, key, groups[key], "error", None)
                except Exception as e2:  # noqa: BLE001
                    logger.warning("place_card_miss_write_failed", place=key[0], error=str(e2)[:200])
                return
            if card_id:
                counters["linked_after_draft" if outcome == "linked_after_draft" else "linked_new"] += 1
                counters["built"] += len(groups[key])
            else:
                counters["missed"] += 1

    await asyncio.gather(*(one(k) for k in to_draft))
    return counters


# AA-989 — the tour's active place atoms (the place_cards_build job's read, without its paid cap).
_TOUR_PLACE_ATOMS_SQL = """
    SELECT atom_id FROM acp_contract.v_active_tour_atoms
    WHERE tour_id = $1::uuid AND NOT deleted AND NOT is_empty_marker AND place IS NOT NULL AND btrim(place) <> ''
    ORDER BY itinerary_day NULLS LAST, atom_id
    LIMIT $2
"""
RELINK_MAX_ATOMS = 400   # = place_cards_build MAX_ATOMS (the largest tours have ~100 atoms)


# S262 — the tour's atoms still waiting for a draft after the free pass, and EVERY replaced predecessor place atom
# (same source: every version of the tour, deleted or superseded atoms) with the card it was linked to. card_id is
# NULL for a predecessor with a miss row, a needs_draft row, no row, or a rejected card: it still competes, so a
# cardless 'City Palace' on another day blocks a carry instead of being invisible (review fix, S262).
_WAITING_ATOMS_SQL = """
    SELECT k.atom_id, a.itinerary_day, btrim(a.place) AS place
      FROM acp_shared.atom_place_card k
      JOIN acp_contract.v_active_tour_atoms a ON a.atom_id = k.atom_id
     WHERE k.atom_id = ANY($1::text[]) AND k.card_id IS NULL AND k.miss_reason = 'needs_draft'
"""
_REPLACED_CARDS_SQL = """
    SELECT a.atom_id, a.itinerary_day, btrim(a.place) AS place, pc.card_id::text AS card_id
      FROM acp_contract.tour_atoms a
      JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = a.tour_id
      LEFT JOIN acp_shared.atom_place_card k ON k.atom_id = a.atom_id
      LEFT JOIN acp_shared.place_card pc ON pc.card_id = k.card_id AND pc.status <> 'rejected'
     WHERE COALESCE(rt.source_group_id, rt.tour_id) = (
               SELECT COALESCE(t.source_group_id, t.tour_id) FROM silver_aa_internal.raw_tours t
                WHERE t.tour_id = $1::uuid)
       AND NOT a.is_empty_marker AND a.place IS NOT NULL AND btrim(a.place) <> ''
       AND NOT EXISTS (SELECT 1 FROM acp_contract.v_active_tour_atoms v WHERE v.atom_id = a.atom_id)
"""


def _one_card(rows: list[dict]) -> Optional[str]:
    """The card when every row carries the same non-rejected card; None when there is no row, a cardless row, or
    two cards (a competing predecessor, never a guess)."""
    cards = {r.get("card_id") for r in rows}
    return next(iter(cards)) if len(cards) == 1 and None not in cards else None


def plan_carry_over(waiting: list[dict], replaced: list[dict]) -> dict[str, str]:
    """S262 — atom_id → card_id for waiting atoms whose replaced predecessors agree on ONE card (atom_repoint's
    narrowing, AA-990). Free, pure. `replaced` holds every predecessor place atom; card_id None = no usable card,
    and such a row still competes. Per waiting atom:
      - a generic name ('City Palace', 'Night Market': a different place in every city) only looks at its own
        itinerary day, never across days;
      - a distinctive name looks at every predecessor with the same place_norm(); when they disagree (two cards, or
        one cardless), only the same day counts, and nothing on that day → left for a draft (ambiguous, no weaker rule);
      - no same-name predecessor → the same day and the same core_key() (a respelling: 'X Pass (3550m)' after
        'X Pass').
    Linked only when every predecessor left carries the same card; anything else stays for the paid draft."""
    out: dict[str, str] = {}
    for w in waiting:
        pn, ck, day = place_norm(w["place"]), core_key(w["place"]), w.get("itinerary_day")
        same = [r for r in replaced if place_norm(r["place"]) == pn]
        if is_generic_name(w["place"]):
            same = [r for r in same if r.get("itinerary_day") == day]
        elif same and _one_card(same) is None:
            same = [r for r in same if r.get("itinerary_day") == day]
            if not same:
                continue                       # same-name predecessors disagree and none is on this day
        if not same:
            same = [r for r in replaced if r.get("itinerary_day") == day and core_key(r["place"]) == ck]
        card = _one_card(same)
        if card is not None:
            out[w["atom_id"]] = card
    return out


async def relink_tour_place_cards(pool, tour_id: str) -> dict:
    """AA-989 — the $0 relink of one tour's place atoms after atomize: card links are keyed on atom_id, and a
    re-atomize gives replaced atoms new ids. Free work only (`max_places=0`): rule-skip miss rows, existing cards by
    alias / core name / locality, stored-miss rows. Groups that would need a draft get a NEEDS_DRAFT row
    (`over_cap`, S262) for the admin build or T8 on demand; before that, a waiting atom whose replaced predecessor
    carried one card is linked to it (`carried_over`, S262, plan_carry_over). Never calls a model."""
    async with pool.acquire() as conn:
        ids = [r["atom_id"] for r in await conn.fetch(_TOUR_PLACE_ATOMS_SQL, tour_id, RELINK_MAX_ATOMS)]
    counters = await ensure_place_cards(ids, pool, max_places=0)
    counters["carried_over"] = 0
    if ids:
        try:
            counters["carried_over"] = await _carry_over(pool, tour_id, ids)
        except Exception as e:  # noqa: BLE001 — the NEEDS_DRAFT rows stand; a later build drafts them
            counters["carried_over"] = f"failed: {type(e).__name__}"
            logger.warning("place_card_carry_over_failed", tour_id=tour_id, error=str(e)[:200])
    return counters


async def _carry_over(pool, tour_id: str, ids: list[str]) -> int:
    async with pool.acquire() as conn:
        waiting = [dict(r) for r in await conn.fetch(_WAITING_ATOMS_SQL, ids)]
        if not waiting:
            return 0
        carry = plan_carry_over(waiting, [dict(r) for r in await conn.fetch(_REPLACED_CARDS_SQL, tour_id)])
        if carry:
            # _LINK_SQL replaces the NEEDS_DRAFT row. No alias row: the evidence is this tour's own history, not a
            # name that is safe country-wide (the paid draft adds aliases).
            await conn.executemany(_LINK_SQL, list(carry.items()))
    return len(carry)


# ── AA-784 (S253): rebuild named cards in place ───────────────────────────────────────────────────

REBUILD_MAX_CARDS = 40   # = place_cards_build MAX_PLACES: one paid draft + one check per card
REBUILD_MAX_ATOMS = 50   # the linked atoms read per card (the representative sentence needs only a few)
_REBUILD_CARDS_SQL = """
    SELECT card_id, place_key, display_name, locality, region, country, naming_note, facts, status
    FROM acp_shared.place_card WHERE card_id = ANY($1::uuid[])
"""
_REBUILD_ATOMS_SQL = """
    SELECT atom_id FROM acp_shared.atom_place_card WHERE card_id = $1::uuid ORDER BY atom_id LIMIT $2
"""
# status re-read in the WHERE: an admin who verified / rejected the card while the job ran wins.
_REBUILD_UPDATE_SQL = """
    UPDATE acp_shared.place_card
    SET locality = $2, region = $3, naming_note = $4, facts = $5::jsonb, cost_usd = cost_usd + $6, updated_at = now()
    WHERE card_id = $1::uuid AND status = 'auto_checked'
    RETURNING card_id
"""
_ADD_COST_SQL = "UPDATE acp_shared.place_card SET cost_usd = cost_usd + $2, updated_at = now() WHERE card_id = $1::uuid"
# AA-784 (S254): the rule-only placement clean-up of a card whose draft named another country — locality / region
# only; name, country, naming_note and facts stay as stored.
_REBUILD_PLACEMENT_SQL = """
    UPDATE acp_shared.place_card
    SET locality = $2, region = $3, cost_usd = cost_usd + $4, updated_at = now()
    WHERE card_id = $1::uuid AND status = 'auto_checked'
    RETURNING card_id
"""


def _identity(row) -> dict:
    return {"locality": row["locality"], "region": row["region"], "naming_note": row["naming_note"],
            "facts": len(stored_facts(row["facts"]))}


async def _rebuild_one(row, pool) -> dict:
    card_id = str(row["card_id"])
    out = {"card_id": card_id, "display_name": row["display_name"], "country": row["country"],
           "before": _identity(row)}
    if row["status"] != "auto_checked":
        return dict(out, outcome=f"skipped_{row['status']}")   # an admin's verdict stands
    if is_generic_name(row["display_name"]):
        return dict(out, outcome="skipped_generic")             # its key carries the locality
    async with pool.acquire() as conn:
        ids = [r["atom_id"] for r in await conn.fetch(_REBUILD_ATOMS_SQL, card_id, REBUILD_MAX_ATOMS)]
        atoms = [dict(r) for r in await conn.fetch(_ATOM_PLACE_QUERY, ids)] if ids else []
    if not atoms:
        return dict(out, outcome="no_atoms")
    rep = _representative(atoms)
    signal = {"atom_id": rep["atom_id"], "place": rep["place"], "group_atoms": len(atoms), "rebuild_card": card_id}
    res = await _draft_checked(rep, tuple(dict.fromkeys(a["place"] for a in atoms)), pool, signal,
                               keep={"display_name": row["display_name"], "country": row["country"]})
    out["cost_usd"] = res["cost"]
    if res["outcome"] == "country_changed":
        return await _clean_placement_only(row, atoms, res, out, pool)
    async with pool.acquire() as conn:
        if res["outcome"] != "ok":
            await conn.execute(_ADD_COST_SQL, card_id, res["cost"])
            return dict(out, outcome=res["outcome"])            # the card is left as it was
        facts = res["facts"]
        if not facts:
            # the new check kept nothing: keep the old checked facts, minus the claims the new rules drop
            facts = drop_unverifiable(stored_facts(row["facts"]), row["country"], row["display_name"])
        updated = await conn.fetchval(_REBUILD_UPDATE_SQL, card_id, res["locality"], res["region"], res["note"],
                                      json.dumps(facts), res["cost"])
        if updated is None:
            await conn.execute(_ADD_COST_SQL, card_id, res["cost"])
            return dict(out, outcome="status_changed")
    after = {"locality": res["locality"], "region": res["region"], "naming_note": res["note"], "facts": len(facts)}
    logger.info("place_card_rebuilt", card_id=card_id, card=row["display_name"], before=out["before"], after=after,
                cleared=res["cleared"])
    return dict(out, outcome="rebuilt", after=after, cleared=res["cleared"],
                facts_from="new_check" if res["facts"] else "old_filtered")


async def _clean_placement_only(row, atoms: list[dict], res: dict, out: dict, pool) -> dict:
    """AA-784 (S254) — the draft placed the card in another country (mostly a legacy compound-country card, 'Nepal and
    China', which AA-990 drafts never match): the guard still refuses the draft — its name, country, note and facts are
    not used — but the identity rules (clean_identity: cross-border 'A / B', region / locality rules) are applied to the
    STORED locality and region, so a stale value ('Korean Peninsula', 'Khumbu') is cleared. Free: no check call. A
    rule only ever empties a field, never writes a new value. 'country_changed' when nothing is cleared."""
    card = {"display_name": row["display_name"], "country": row["country"],
            "locality": row["locality"] or "", "region": row["region"] or ""}
    cleared = clean_identity(card, place_names=tuple(dict.fromkeys(a["place"] for a in atoms)),
                             country=row["country"])
    async with pool.acquire() as conn:
        if not cleared:
            await conn.execute(_ADD_COST_SQL, out["card_id"], res["cost"])
            return dict(out, outcome="country_changed", drafted_country=res.get("drafted_country"))
        locality, region = card["locality"].strip() or None, card["region"].strip() or None
        updated = await conn.fetchval(_REBUILD_PLACEMENT_SQL, out["card_id"], locality, region, res["cost"])
        if updated is None:
            await conn.execute(_ADD_COST_SQL, out["card_id"], res["cost"])
            return dict(out, outcome="status_changed")
    after = {"locality": locality, "region": region, "naming_note": row["naming_note"],
             "facts": out["before"]["facts"]}
    logger.info("place_card_placement_cleaned", card_id=out["card_id"], card=row["display_name"],
                stored_country=row["country"], drafted_country=res.get("drafted_country"), before=out["before"],
                after=after, cleared=cleared)
    return dict(out, outcome="country_changed_cleaned", drafted_country=res.get("drafted_country"), after=after,
                cleared=cleared)


async def rebuild_place_cards(card_ids: list[str], pool) -> dict:
    """AA-784 — re-draft and re-check named cards IN PLACE under the current prompts and identity rules (one paid
    draft + one check per card, ≤ REBUILD_MAX_CARDS). The card keeps its card_id, place_key, display_name, country,
    atom links and aliases — only locality, region, naming_note and facts change (the checked draft is held to the
    stored name and country; a draft placing it in another country is refused, and only the identity rules clear the
    stored locality / region: 'country_changed_cleaned', or 'country_changed' when nothing breaks them). Verified /
    rejected cards and generic-name cards are skipped. A miss (no_name / identity_rejected) leaves the card as it was
    and is reported, never stored as a place_card_miss (the place has a card). Returns counters + one row per card."""
    ids = [c for c in dict.fromkeys(str(c) for c in card_ids or []) if c]
    if len(ids) > REBUILD_MAX_CARDS:
        raise ValueError(f"at most {REBUILD_MAX_CARDS} cards per rebuild")
    async with pool.acquire() as conn:
        rows = {str(r["card_id"]): r for r in await conn.fetch(_REBUILD_CARDS_SQL, ids)} if ids else {}
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def one(cid: str) -> dict:
        if cid not in rows:
            return {"card_id": cid, "outcome": "not_found"}
        async with sem:
            try:
                return await _rebuild_one(rows[cid], pool)
            except Exception as e:  # noqa: BLE001 — soft-fail per card
                logger.warning("place_card_rebuild_failed", card_id=cid, error=str(e)[:200])
                return {"card_id": cid, "outcome": "error", "error": str(e)[:200]}

    cards = await asyncio.gather(*(one(c) for c in ids))
    counts: dict[str, int] = {}
    for c in cards:
        counts[c["outcome"]] = counts.get(c["outcome"], 0) + 1
    return {"requested": len(ids), "outcomes": counts,
            "cost_usd": round(sum(c.get("cost_usd") or 0 for c in cards), 6), "cards": list(cards)}


# ── Duplicates + merge (admin) ─────────────────────────────────────────────────────────────────

def _contains_words(a: str, b: str) -> bool:
    return bool(a) and bool(b) and a != b and f" {a} " in f" {b} "


def duplicate_clusters(cards: list[dict], aliases: list[dict] = ()) -> list[dict]:
    """Candidate duplicate groups for the admin to review — never merged automatically. Two cards
    of the same (folded) country pair up when they share a core name ('Lake Khövsgöl' / 'Khövsgöl
    Lake'), one name contains the other as whole words ('Paro' / 'Paro Town' — but also 'Punakha' /
    'Punakha Dzong', which is why a human decides), or an alias of one is a name of the other.
    Rejected cards are left out (an admin already judged them wrong — delete, never merge facts
    from them), and two generic-name cards ('City Palace') pair up only in the same locality.
    `cards`: card_id, display_name, country, status?, locality? (+ display fields, passed through)."""
    cards = [c for c in cards if c.get("status") != "rejected"]
    parent = {c["card_id"]: c["card_id"] for c in cards}
    reasons: dict[str, set] = {}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b, why):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
            reasons.setdefault(ra, set()).update(reasons.pop(rb, set()))
        reasons.setdefault(ra, set()).add(why)

    info = {c["card_id"]: (fold(c.get("country")), name_forms(c["display_name"]),
                           {core_key(f) for f in name_forms(c["display_name"])}) for c in cards}
    by_card = {c["card_id"]: c for c in cards}
    generic = {c["card_id"]: is_generic_name(c["display_name"]) for c in cards}
    alias_of: dict[str, set] = {}
    for a in aliases or []:
        alias_of.setdefault(str(a["card_id"]), set()).add((a["alias_norm"], a["country_norm"]))
    ids = list(parent)
    for i, a in enumerate(ids):
        ca, fa, ka = info[a]
        for b in ids[i + 1:]:
            cb, fb, kb = info[b]
            if ca != cb:
                continue
            if (generic[a] or generic[b]) and not (
                    generic[a] and generic[b]
                    and fold(by_card[a].get("locality")) == fold(by_card[b].get("locality"))):
                continue
            if ka & kb:
                union(a, b, "same_core_name")
            elif any(_contains_words(x, y) or _contains_words(y, x) for x in fa for y in fb):
                union(a, b, "name_contains")
            elif any((f, cb) in alias_of.get(a, ()) for f in fb) or any((f, ca) in alias_of.get(b, ()) for f in fa):
                union(a, b, "alias")
    groups: dict[str, list] = {}
    for c in cards:
        groups.setdefault(find(c["card_id"]), []).append(c)
    return [{"country": g[0].get("country"), "reasons": sorted(reasons.get(root, ())), "cards": g}
            for root, g in groups.items() if len(g) > 1]


def _facts_list(raw) -> list[dict]:
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    return [f for f in raw or [] if isinstance(f, dict) and f.get("text")]


def merged_facts(keep_facts, other_facts: list) -> list[dict]:
    """The kept card's facts, then the merged cards' facts not already there (folded text), added
    only while the card holds fewer than MAX_FACTS — a merge never drops a fact the card had.
    Lossless on purpose (review S234 r2b): the donor cards are deleted after the merge, so a
    number-conflict filter here would lose true facts for good (length vs width of one lake); a
    seed keeps one value per fact at seed time instead (`seed_canon.canonical_facts`)."""
    out = _facts_list(keep_facts)
    seen = {fold(f["text"]) for f in out}
    for facts in other_facts:
        for f in _facts_list(facts):
            if len(out) >= MAX_FACTS:
                return out
            if fold(f["text"]) not in seen:
                seen.add(fold(f["text"]))
                out.append(f)
    return out


class MergeError(ValueError):
    pass


async def merge_cards(conn, keep_id: str, from_ids: list[str]) -> dict:
    """Merge `from_ids` into `keep_id` (call inside a transaction): re-link their atoms, point
    their aliases at the kept card and add their names as aliases, add their facts (up to
    MAX_FACTS) and cost, then delete them. Cards must share a country. Facts are never taken from
    a rejected card, and never added to a verified card (its reviewed facts stay as reviewed);
    a rejected card cannot be the kept one. Raises MergeError / LookupError."""
    from_ids = [f for f in dict.fromkeys(from_ids) if f and f != keep_id]
    if not from_ids:
        raise MergeError("from_ids must name at least one card other than keep_id")
    rows = {str(r["card_id"]): r for r in await conn.fetch(
        """
        SELECT card_id, display_name, country, facts, cost_usd, status FROM acp_shared.place_card
        WHERE card_id = ANY($1::uuid[]) FOR UPDATE
        """, [keep_id, *from_ids])}
    missing = [c for c in [keep_id, *from_ids] if c not in rows]
    if missing:
        raise LookupError(f"place card(s) not found: {', '.join(missing)}")
    keep = rows[keep_id]
    if keep.get("status") == "rejected":
        raise MergeError("a rejected card cannot be kept; keep a good card and merge the rejected one into it")
    country = fold(keep["country"])
    if any(fold(rows[f]["country"]) != country for f in from_ids):
        raise MergeError("cards from different countries cannot be merged")
    moved = await conn.fetchval(
        """
        WITH m AS (
            UPDATE acp_shared.atom_place_card SET card_id = $1::uuid, miss_reason = NULL, resolved_at = now()
            WHERE card_id = ANY($2::uuid[]) RETURNING 1
        ) SELECT count(*) FROM m
        """, keep_id, from_ids)
    await conn.execute("UPDATE acp_shared.place_card_alias SET card_id = $1::uuid WHERE card_id = ANY($2::uuid[])",
                       keep_id, from_ids)
    alias_rows = [(form, country, keep_id, "merge")
                  for f in from_ids for form in itinerary_alias_forms(rows[f]["display_name"])]
    await conn.executemany(_ALIAS_SQL, alias_rows)
    donors = [] if keep.get("status") == "verified" else \
        [rows[f]["facts"] for f in from_ids if rows[f].get("status") != "rejected"]
    facts = merged_facts(keep["facts"], donors)
    cost = sum(float(rows[c]["cost_usd"] or 0) for c in [keep_id, *from_ids])
    await conn.execute(
        """
        UPDATE acp_shared.place_card SET facts = $2::jsonb, cost_usd = $3, updated_at = now()
        WHERE card_id = $1::uuid
        """, keep_id, json.dumps(facts), round(cost, 6))
    await conn.execute("DELETE FROM acp_shared.place_card WHERE card_id = ANY($1::uuid[])", from_ids)
    return {"keep_id": keep_id, "merged_ids": from_ids,
            "merged_names": [rows[f]["display_name"] for f in from_ids],
            "atoms_moved": int(moved or 0), "aliases_added": len(alias_rows), "facts": len(facts),
            "cost_usd": round(cost, 6)}


class FactEditError(ValueError):
    """An admin fact edit that cannot be applied (bad index, kind or text): HTTP 422."""


class FactEditConflict(ValueError):
    """The fact at an index is no longer the one the admin loaded (card changed since): HTTP 409."""


MAX_FACT_TEXT = 600


def stored_facts(raw) -> list:
    """The stored facts array as-is (jsonb may arrive as a str from asyncpg)."""
    if isinstance(raw, str):
        raw = json.loads(raw or "[]")
    return list(raw) if isinstance(raw, list) else []


def apply_fact_edits(raw_facts, edits: list[dict]) -> tuple[list, list[dict]]:
    """AA-774 (S235) — apply an admin's fact edits to a card's stored facts array.

    Each edit: {"index", "kind"?, "text"?, "drop"?, "expected_text"?}. `index` is the position in the
    stored array (every index refers to the array as loaded, drops are applied last). A kept fact keeps
    every other key it had; only `kind` / `text` change. `expected_text` (optional) must equal the
    stored text at that index, so a stale form cannot edit the wrong fact (FactEditConflict).
    Returns (new_facts, changes) with one {"index", "action", "before", "after"} per changed fact;
    an edit that changes nothing is not a change. Raises FactEditError on any invalid edit."""
    facts = stored_facts(raw_facts)
    if not edits:
        raise FactEditError("edits must name at least one fact")
    out = list(facts)
    dropped: set[int] = set()
    changes: list[dict] = []
    seen: set[int] = set()
    for e in edits:
        i = e.get("index")
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(facts):
            raise FactEditError(f"fact index {i} does not exist (card has {len(facts)} facts)")
        if i in seen:
            raise FactEditError(f"fact index {i} is edited twice")
        seen.add(i)
        before = facts[i]
        if not isinstance(before, dict):
            raise FactEditError(f"fact {i} is not an object and cannot be edited")
        exp = e.get("expected_text")
        if exp is not None and " ".join(str(exp).split()) != " ".join(str(before.get("text") or "").split()):
            raise FactEditConflict(f"fact {i} changed since it was loaded — reload and try again")
        kind, text, drop = e.get("kind"), e.get("text"), bool(e.get("drop"))
        if drop:
            if kind is not None or text is not None:
                raise FactEditError(f"fact {i}: drop cannot be combined with kind or text")
            dropped.add(i)
            changes.append({"index": i, "action": "drop", "before": before, "after": None})
            continue
        if kind is None and text is None:
            raise FactEditError(f"fact {i}: give a kind, a text or drop")
        after = dict(before)
        if kind is not None:
            if kind not in FACT_KINDS:
                raise FactEditError(f"fact {i}: kind must be one of {', '.join(FACT_KINDS)}")
            after["kind"] = kind
        if text is not None:
            clean = " ".join(str(text).split())
            if not clean:
                raise FactEditError(f"fact {i}: text cannot be empty (drop the fact instead)")
            if len(clean) > MAX_FACT_TEXT:
                raise FactEditError(f"fact {i}: text is longer than {MAX_FACT_TEXT} characters")
            after["text"] = clean
        if after != before:
            out[i] = after
            changes.append({"index": i, "action": "edit", "before": before, "after": after})
    if not changes:
        raise FactEditError("nothing to change: every edit matches the stored fact")
    return [f for j, f in enumerate(out) if j not in dropped], sorted(changes, key=lambda c: c["index"])


def render_card(card) -> tuple[str, list[str]]:
    """(location line, background fact lines) for the seed."""
    where = ", ".join(p for p in (card["display_name"], card["locality"], card["region"], card["country"]) if p)
    facts = card["facts"]
    if isinstance(facts, str):
        facts = json.loads(facts or "[]")
    return where, [f["text"] for f in facts or [] if isinstance(f, dict) and f.get("text")]


__all__ = ["ensure_place_cards", "build_card_for_atom", "build_card_for_group", "render_card", "place_key",
           "CARD_FOR_ATOM_QUERY", "draft_card", "check_card", "fold", "place_norm", "core_key", "name_forms",
           "card_skip_reason", "find_existing_cards", "find_local_cards", "pick_local_card", "is_generic_name",
           "atom_group_key", "itinerary_alias_forms", "miss_is_active", "RULE_VERSION", "PROMPT_SHA",
           "duplicate_clusters", "merged_facts", "merge_cards", "MergeError", "traveller_clause",
           "find_card_for_draft", "find_rejected_for_draft", "match_key", "region_problem", "locality_problem",
           "clean_identity", "unverifiable_claim", "drop_unverifiable", "rebuild_place_cards", "REBUILD_MAX_CARDS"]
