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
from shared.llm_client.call_log import record_call_with_pool
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

_CARD_SYSTEM = """You write a short reference card about one real place that appears in a travel \
itinerary. The card is used by writers who must not get basic facts wrong.

Return ONLY JSON:
{"display_name": "...", "locality": "...", "region": "...", "country": "...",
 "naming_note": "...", "aliases": ["..."], "facts": [{"text": "...", "kind": "..."}]}

- display_name: the place's usual English name (fix spelling variants, e.g. "Pawangka" -> the \
common spelling, mentioning the variant in naming_note).
- locality: the town or city it is in or next to; region: province / state / historical region. \
Short names only. Empty string when it does not apply or you are not sure.
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
Write a number only if it is a well-established fact (e.g. a founding century).
- If the itinerary name is too generic to identify one real place (e.g. "the market", \
"hotel"), return {"display_name": "", "facts": []}."""

_CHECK_SYSTEM = """You check a reference card about a real place. For the place identity and \
for each numbered fact, answer whether it is accurate and well-established.

Return ONLY JSON: {"identity_ok": true/false, "verdicts": ["supported" | "unsure" | "wrong", ...]}
- identity_ok: the display name, locality, region and country are correct for the place the \
itinerary means. A tour may cross a border: do not reject only because the card's country is a \
neighbouring country of the one the tour is sold as.
- One verdict per numbered fact, same order. "supported" only when you are confident the fact \
is correct and widely documented; "unsure" when you cannot confirm it; "wrong" when it is false."""

# A changed prompt invalidates every stored miss (a new prompt may identify the place). It never
# re-drafts an existing card: ensure_place_cards() skips linked atoms and reuses cards by alias / core
# name, and its only callers are bounded (T8: one atom; place_cards_build: one tour, MAX_PLACES).
# AA-774 changed the facts bullet: old cards keep their facts and are filtered at seed time.
# AA-808 added the traveller-clause bullet (hash changes with it; existing cards are not re-drafted).
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


def _clean_facts(raw_facts, card_name: str = "") -> list[dict]:
    out = []
    for f in raw_facts if isinstance(raw_facts, list) else []:
        if isinstance(f, dict) and isinstance(f.get("text"), str) and f["text"].strip():
            kind = f.get("kind") if f.get("kind") in FACT_KINDS else "what_it_is"
            text = " ".join(f["text"].split())
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


def draft_card(atom: dict) -> tuple[dict, float, object]:
    """One `a3_place_card` call. Returns (card dict, cost, response)."""
    user = (f"PLACE AS THE ITINERARY NAMES IT: {atom['place']}\n"
            f"COUNTRY THE TOUR IS SOLD AS: {atom.get('country') or 'unknown'} "
            f"(the route may cross into a neighbouring country)\n"
            f"TOUR: {atom.get('tour_name') or 'unknown'}\n"
            f"ITINERARY SENTENCE: {_trim(atom.get('evidence'))}")
    resp = LLMClient().generate(LLMRequest(system_prompt=_CARD_SYSTEM, user_prompt=user,
                                           stage="a3_place_card", max_tokens=DRAFT_MAX_TOKENS))
    return _parse_json(resp.content), resp.cost_usd, resp


def check_card(card: dict, atom: dict) -> tuple[bool, list[str], float, object]:
    """One `a3_place_card_check` call. Returns (identity_ok, verdict per fact, cost, response)."""
    facts = card.get("facts") or []
    numbered = "\n".join(f"{i}. {f['text']}" for i, f in enumerate(facts, 1))
    user = (f"ITINERARY NAME: {atom['place']} (tour sold as {atom.get('country') or 'unknown'}; "
            f"the place may be in a neighbouring country; tour: {atom.get('tour_name') or 'unknown'}; "
            f"sentence: {_trim(atom.get('evidence'), 300)})\n"
            f"CARD IDENTITY: {card.get('display_name')} — locality: {card.get('locality') or '-'}, "
            f"region: {card.get('region') or '-'}, country: {card.get('country')}\n"
            f"FACTS:\n{numbered}")
    resp = LLMClient().generate(LLMRequest(system_prompt=_CHECK_SYSTEM, user_prompt=user,
                                           stage="a3_place_card_check", max_tokens=CHECK_MAX_TOKENS))
    data = _parse_json(resp.content)
    verdicts = data.get("verdicts") if isinstance(data.get("verdicts"), list) else []
    verdicts = [str(v).strip().lower() for v in verdicts]
    if len(verdicts) != len(facts):
        verdicts = ["unsure"] * len(facts)  # misaligned reply → keep nothing
    return bool(data.get("identity_ok")), verdicts, resp.cost_usd, resp


async def _log(pool, stage: str, role: str, resp, quality_signal: dict) -> None:
    await record_call_with_pool(
        pool, stage=stage, role=role, model=resp.model_used,
        tokens_in=getattr(resp, "input_tokens", None), tokens_out=getattr(resp, "output_tokens", None),
        cost_usd=resp.cost_usd, quality_signal=quality_signal,
        stop_reason=getattr(resp, "stop_reason", None), account=getattr(resp, "satellite_account", None),
        fallback_used=getattr(resp, "fallback_used", None),
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


async def build_card_for_group(group_key: tuple[str, str], atoms: list[dict], pool) -> tuple[Optional[str], str]:
    """Draft + check ONE card for every atom of one (place_norm, country) group and link them.
    Returns (card_id, 'linked'), (card_id, 'linked_after_draft') when the checked draft names an
    existing card (AA-809: no insert, see find_card_for_draft) or (None, miss reason); a miss is
    stored so the place is not drafted again before its retry_after."""
    rep = _representative(atoms)
    signal = {"atom_id": rep["atom_id"], "place": rep["place"], "group_atoms": len(atoms)}
    card, cost, resp = await asyncio.to_thread(draft_card, rep)
    await _log(pool, "a3_place_card", "validate", resp, signal)
    model = getattr(resp, "model_used", None)
    name = (card.get("display_name") or "").strip() if isinstance(card.get("display_name"), str) else ""
    raw_country = card.get("country") if isinstance(card.get("country"), str) else ""
    country = (raw_country or rep.get("country") or "").strip()
    card["facts"] = _clean_facts(card.get("facts"), name or rep["place"])
    card["aliases"] = _clean_aliases(card.get("aliases"))
    if not name or not country:
        await _record_miss(pool, group_key, atoms, "no_name", model)
        return None, "no_name"
    ok, verdicts, cost2, resp2 = await asyncio.to_thread(check_card, card, rep)
    kept = [f for f, v in zip(card["facts"], verdicts) if v == "supported"]
    await _log(pool, "a3_place_card_check", "validate", resp2,
               dict(signal, identity_ok=ok, facts=len(card["facts"]), kept=len(kept)))
    if not ok:
        logger.info("place_card_identity_rejected", atom_id=rep["atom_id"], place=rep["place"], card=name)
        await _record_miss(pool, group_key, atoms, "identity_rejected", model)
        return None, "identity_rejected"
    locality = (card.get("locality") or "").strip() or None
    region = (card.get("region") or "").strip() or None
    if is_generic_name(name) and not (locality or region):
        # 'City Palace' with no town is not one place: no card rather than one per country.
        await _record_miss(pool, group_key, atoms, "no_name", model)
        return None, "no_name"
    key = place_key(name, country, locality or region)
    fields = (name, locality, region, country, (card.get("naming_note") or "").strip() or None,
              json.dumps(kept), round(cost + cost2, 6))
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
            card_id = await _store_card(conn, key, fields)
        if card_id is not None:
            card_id = str(card_id)
            await conn.executemany(_LINK_SQL, [(a["atom_id"], card_id) for a in atoms])
            await conn.executemany(_ALIAS_SQL, _alias_rows(card_id, atoms, group_key[1], card, country))
            await conn.execute("DELETE FROM acp_shared.place_card_miss WHERE place_norm = $1 AND country_norm = $2",
                               group_key[0], group_key[1])
    if card_id is None:
        logger.info("place_card_same_as_rejected", atom_id=rep["atom_id"], place=rep["place"], card=name)
        await _record_miss(pool, group_key, atoms, "identity_rejected", model)
        return None, "identity_rejected"
    return card_id, outcome


async def build_card_for_atom(atom: dict, pool) -> Optional[str]:
    """One atom as a one-atom group (kept for callers/tests of the S233 API)."""
    card_id, _ = await build_card_for_group(atom_group_key(atom), [atom], pool)
    return card_id


def _empty_counters(atoms: int) -> dict:
    return {"atoms": atoms, "already_linked": 0, "skipped_by_rule": 0, "places": 0, "linked_existing": 0,
            "skipped_cached": 0, "over_cap": 0, "drafted": 0, "linked_new": 0, "missed": 0, "errors": 0,
            "built": 0, "linked_after_draft": 0, "miss_recorded": 0}


async def ensure_place_cards(atom_ids: list[str], pool, *, force: bool = False,
                             max_places: Optional[int] = None) -> dict:
    """Give every atom in `atom_ids` its place card where one can be had, paying for at most one
    draft per (place_norm, country) group (S234 guard). Order of work: free rule skip → existing
    card by alias / core name (no call) → stored miss (no call unless `force`) → draft. Soft-fails
    per group (a card is an aid, never a reason to fail a write).

    Returns counters: atoms, already_linked, skipped_by_rule, places (groups to resolve),
    linked_existing, skipped_cached, over_cap (groups left for a later run by `max_places`),
    drafted, linked_new, linked_after_draft (AA-809: a draft that named an existing card, linked to it
    with no insert), missed, errors, built (atoms newly linked by a draft)."""
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
        to_draft = to_draft[:max_places]
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


async def relink_tour_place_cards(pool, tour_id: str) -> dict:
    """AA-989 — the $0 relink of one tour's place atoms after atomize: card links are keyed on atom_id, and a
    re-atomize gives replaced atoms new ids. Free work only (`max_places=0`): rule-skip miss rows, existing cards by
    alias / core name / locality, stored-miss rows. Groups that would need a draft are left (`over_cap`) for the
    admin build or T8 on demand. Never calls a model."""
    async with pool.acquire() as conn:
        ids = [r["atom_id"] for r in await conn.fetch(_TOUR_PLACE_ATOMS_SQL, tour_id, RELINK_MAX_ATOMS)]
    return await ensure_place_cards(ids, pool, max_places=0)


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
           "find_card_for_draft", "match_key"]
