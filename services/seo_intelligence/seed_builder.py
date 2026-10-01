"""AA-197 [AA-193·F2]: DataForSEO seed + buyer-market resolution (pure, no I/O).

Builds a complete search seed from dirty raw_tours fields and resolves the DataForSEO
location/language from a tenant's target_market. Kept side-effect free so it is unit-testable
without DB or HTTP. The DFS client consumes the finished seed verbatim (no more appending
"tours" — that caused the `{country} tours tours` double-tours bug).
"""

import json
import re

# Known dirty country-COLUMN values observed in silver_aa_internal.raw_tours. AA-571 migration
# 149 cleaned the column itself (SRI-LANDKA -> Sri Lanka, OKINAWA -> Japan), so this dict is now
# a defensive fallback only, not the live path for either entry -- kept, not deleted, in case a
# future raw path re-introduces a dirty column value before ingestion validation catches it.
COUNTRY_NORMALIZE = {
    "SRI-LANDKA": "Sri Lanka",
}

# AA-571 Việc 1: sub-region/landmark override, keyed off the TOUR'S OWN TITLE (src_name), not
# the country column -- the country column is clean at country-level only (migration 149) and
# was never meant to carry sub-region detail. Evidence-based via a live catalog scan
# (2026-09-11): of 104 real Japan tours, 36 have "Okinawa" (or one of its own sub-locations)
# somewhere in src_name/itinerary text, and critically 24+ of those already say "Okinawa" (or
# a known sub-location) directly IN THE TITLE -- title-level detection alone catches
# effectively the whole real cluster without needing itinerary text. Deliberately NOT extended
# to Kyoto/Osaka/Hokkaido -- the same scan found ZERO title-level hits for any of them in the
# current catalog (Kyoto/Osaka only ever appear as an incidental itinerary stop inside broader
# multi-city Japan tours), so hardcoding them now would be guessing ahead of real data, not
# reading it -- add them here if/when a future catalog scan finds a real title-level cluster,
# same evidence bar as this one. "YAMBARU" (with b) is a real spelling variant of "Yanbaru"
# (with n) found live in an actual title -- both map here.
REGION_LANDMARKS: dict[str, str] = {
    "OKINAWA":  "Okinawa, Japan",
    "NAHA":     "Okinawa, Japan",
    "YANBARU":  "Okinawa, Japan",
    "YAMBARU":  "Okinawa, Japan",
    "IRIOMOTE": "Okinawa, Japan",
    "MIYAKO":   "Okinawa, Japan",
}

# DataForSEO location codes (google_ads / serp). Buyer markets we support today.
# AA-515: extended from {US, UK, AU} to also cover DE/FR/NL — confirmed via a live query
# against shared.tenant_seo_config (STEP0c, docs/claude_audit/AA-515-step0c-multimarket-
# schema.md) that a real tenant (exploreasia-co) already declares target_market.countries =
# [DE, FR, NL], none of which resolve_buyer_market() could previously return (silently fell
# back to US — a market that tenant never even listed). Codes are the standard Google Ads
# location criteria IDs (Germany/France/Netherlands), same numbering family as the 3 already
# here.
DFS_LOCATION_MAP = {
    "US": (2840, "United States"),
    "UK": (2826, "United Kingdom"),
    "AU": (2036, "Australia"),
    "DE": (2276, "Germany"),
    "FR": (2250, "France"),
    "NL": (2528, "Netherlands"),
}

# Buyer-market preference when a tenant targets several countries (lower = preferred).
# Used by resolve_buyer_market() (singular, unchanged) to pick ONE market. resolve_buyer_
# markets() (plural, AA-515) does not consult this for exclusion — it returns EVERY market in
# DFS_LOCATION_MAP a tenant declares, ranked by this only for a stable, arguable ordering.
MARKET_RANK = {"US": 1, "UK": 2, "AU": 3, "DE": 4, "FR": 5, "NL": 6}

# AA-515: reverse lookup, location_code -> 2-letter market code. resolve_buyer_markets() returns
# the literal (location_code, location_name, language_code) 3-tuple shape the build prompt asked
# for (matching resolve_buyer_market()'s own contract) — it does not carry the 2-letter code
# alongside it, so a caller that needs to persist a compact "market" column (acp_contract.
# search_demand/atom_ranking, AA-515) looks it up here rather than re-deriving it.
LOCATION_CODE_TO_MARKET = {code: market for market, (code, _name) in DFS_LOCATION_MAP.items()}

# Sensible default when target_market has no usable country.
_DEFAULT_MARKET = "US"

# activities blob is delimited by comma, pipe (U+2502) or newline.
_ACTIVITY_SPLIT = re.compile(r"[,│\n]+")


def detect_region_landmark(tour_name: str) -> str | None:
    """Sub-region override from the TOUR'S OWN TITLE (AA-571 Việc 1).

    Title-only, deliberately: a multi-city Japan tour that merely visits a landmark as one
    itinerary stop should not be re-labeled by it -- only a tour whose own title names the
    landmark is treated as being ABOUT that place. None when no landmark matches.
    """
    if not tour_name:
        return None
    name_upper = tour_name.upper()
    for landmark, region in REGION_LANDMARKS.items():
        if landmark in name_upper:
            return region
    return None


def normalize_country(raw: str, tour_name: str = "") -> str:
    """Map dirty/generic country text to the most specific display country available.

    tour_name is checked FIRST against known sub-region landmarks (AA-571) -- independent of
    the country column, which is clean at country-level only since migration 149. Falls back to
    the country column itself when no landmark matches. Empty -> ''.
    """
    landmark = detect_region_landmark(tour_name)
    if landmark:
        return landmark
    if not raw:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    mapped = COUNTRY_NORMALIZE.get(s.upper())
    if mapped:
        return mapped
    # Title-case word-by-word ("south korea" -> "South Korea")
    return " ".join(w.capitalize() for w in s.split())


def first_activity(activities) -> str:
    """First activity token from the jsonb value (single-elem array wrapping a delimited string)."""
    if isinstance(activities, str):
        # AA-702: no jsonb codec on the pool, so S1 passed the raw JSON text and this returned ""
        # for every tour — the AA-197 activity seed never reached DataForSEO.
        try:
            activities = json.loads(activities)
        except ValueError:
            activities = [activities]
    if not activities or not isinstance(activities, list):
        return ""
    first = activities[0]
    if first is None:
        return ""
    for token in _ACTIVITY_SPLIT.split(str(first)):
        token = token.strip()
        if token:
            return token
    return ""


def _seed_name(tour_name: str) -> str:
    """Tour title as a search phrase. An all-caps title ("LAYA-GASA TREK") is lowercased: nobody
    searches in capitals and the S1 writer was handed "LAYA-GASA TREK Bhutan" as its keyword. A code,
    "(Guided)" and a duration tail ("– 3-Days / 2-Nights") are dropped (S207 Taiwan trial)."""
    from services.photos.match import tour_title
    n = " ".join(tour_title(tour_name or "").split())
    return n.lower() if n.isupper() else n


def build_seed(country_raw: str, activities, tour_name: str = "") -> str:
    """Complete DFS seed. Never produces a double 'tours'.

    AA-251 (ADR-2026-021, hướng 4): priority is activity+country (most specific,
    unchanged AA-197 behavior) > tour_name+country > country-only "{country} tours".
    The country-only fallback made the seed — and every DFS keyword derived from
    it — generic, which is what made DFS_INTENT_UNDERUSED false-positive across
    ~85% of the KR/LK catalogue. tour_name (raw_tours.src_name, always populated)
    is the nearest per-tour specificity available without a live per-tour DFS call.
    """
    c = normalize_country(country_raw, tour_name)
    a = first_activity(activities)
    if a and c:
        return f"{a} in {c}"
    n = _seed_name(tour_name)
    if n:
        # S207: "CULINARY GEMS OF BHUTAN" + "Bhutan" doubled the country in the seed.
        return f"{n} {c}".strip() if c and c.casefold() not in n.casefold() else n
    if c:
        return f"{c} tours"
    return ""


# AA-702 — words in tour titles that say nothing about WHERE the tour goes. Stripping them leaves
# the place names ("Kang Yatse 2 and the Lhato Valley" -> "kang yatse lhato valley") that
# DataForSEO actually has search volume for.
_GENERIC_TITLE_WORDS = frozenset("""
    a an and the of to in on for with by from at via & tour tours trip trips day days night nights
    week weeks classic exploration explore explorer journey journeys adventure adventures discovery
    discover highlights highlight best amazing express retreat odyssey delights delight experience
    experiences escape grand ultimate essential essentials private small group luxury tailor made
    tailormade package holiday holidays vacation vacations itinerary through across around north
    south east west central northern southern eastern western exotic wonders wonder magic magical
    hidden gems gem secret secrets unexplored epic blissful tranquil serenity serendipity lens time
    embrace cultural culture wild nature one two three four five six seven eight nine ten eleven
    twelve fourteen fifteen twenty
    life departure departures monday tuesday wednesday thursday friday saturday sunday
""".split())
# Geographic nouns that help a seed but are too common to prove an idea is about THIS tour
# ("valley" matches every valley in the country) — kept in seeds, ignored when ranking.
_WEAK_PLACE_WORDS = frozenset(
    "valley lake lakes river rivers base camp island islands mountain mountains trek trekking hike "
    "hiking temple temples beach beaches coast village villages city cities park national road "
    "railway rail cycling bicycle e-bicycle bike biking safari wildlife pass peak "
    # S207 Taiwan trial: "Ancient Trails & Hot Springs" kept "ancient city of polonnaruwa" (Sri Lanka)
    "ancient old hot spring springs trail trails tea culture heritage".split())
# S207: no hyphen inside a token. "laya-gasa" stayed one term, so DataForSEO's "laya gasa trek"
# never matched it and the Laya-Gasa trek kept no keyword idea at all.
_TITLE_TOKEN = re.compile(r"[a-z][a-z']+")


def title_place_terms(tour_name: str, country_raw: str = "") -> list[str]:
    """Place-like words from the tour's own title: lowercase tokens minus generic travel words,
    numbers and the country's own words. Order kept, deduped. Pure."""
    country_words = set(normalize_country(country_raw).lower().replace(",", " ").split())
    title = (tour_name or "").lower()
    # S207: "3D2N Gangwon Biking, Hiking & Surfing Tour from Seoul" — Seoul is where it starts, not
    # where it goes; it brought "jeju island from seoul" / "seoul airport transfer".
    head = re.split(r"\bfrom\b", title, maxsplit=1)[0]
    if any(t not in _GENERIC_TITLE_WORDS and len(t) >= 3 for t in _TITLE_TOKEN.findall(head)):
        title = head
    out: list[str] = []
    for tok in _TITLE_TOKEN.findall(title):
        tok = tok.strip("'-")
        if len(tok) < 3 or tok in _GENERIC_TITLE_WORDS or tok in country_words or tok in out:
            continue
        out.append(tok)
    return out


def idea_seeds(country_raw: str, activities, tour_name: str = "") -> list[str]:
    """AA-702 — seeds for ONE keywords_for_keywords task (DFS prices per task, up to 20 keywords).

    The single tour-name seed (AA-251) almost never has search volume: all 224 S1 seo_context rows
    had only the seed itself as "top keyword". Sending the specific seed plus the title's place
    words and "country + activity" in the same task costs the same; rank_keyword_ideas() then keeps
    only ideas that name one of the tour's places or its activity (AA-251: generic country phrases
    are not this tour's keywords)."""
    c = normalize_country(country_raw, tour_name)
    seeds = [build_seed(country_raw, activities, tour_name)]
    places = title_place_terms(tour_name, country_raw)
    if places:
        seeds.append(f"{' '.join(places[:4])} {c}".strip())
        if len(places) > 1:
            seeds.append(" ".join(places[:4]))
    a = first_activity(activities)
    if a and c:
        seeds.append(f"{c} {a}")
    # No "{country} tours" seed: on Dev (Kang Yatse, Ladakh mountaineering) it pulled the whole
    # idea list towards "golden triangle india" / "kerala trip package" — high volume, wrong tour.
    out: list[str] = []
    for sd in seeds:
        sd = (sd or "").strip()
        if sd and sd.casefold() not in {x.casefold() for x in out}:
            out.append(sd)
    return out[:20]


def activity_terms(activities) -> list[str]:
    """Lowercase words of the tour's first activity ("Wildlife Safari" -> ["wildlife", "safari"]),
    generic words removed. Used as relevance terms next to the title's place words."""
    return [t for t in _TITLE_TOKEN.findall(first_activity(activities).lower())
            if len(t) >= 4 and t not in _GENERIC_TITLE_WORDS]


def _country_words() -> dict[str, str]:
    """Lowercase country names/aliases (≥ 4 letters) → canonical country, from COUNTRY_MASTER."""
    from shared.country_resolver import COUNTRY_MASTER
    out: dict[str, str] = {}
    for canonical, aliases in COUNTRY_MASTER.items():
        for w in [canonical, *aliases]:
            w = w.lower().replace("-", " ")
            if len(w) >= 4:
                out[w] = canonical
    return out


def has_word(term: str, text: str) -> bool:
    """Whole-word match (plural / -ing / -er forms allowed) — "life" must not match "wildlife" or "nightlife"."""
    return re.search(rf"(?<![a-z]){re.escape(term)}(?:s|es|ing|ers?)?(?![a-z])", text) is not None


def foreign_country(keyword: str, country: str, place_terms: list[str] | None = None) -> str | None:
    """The other country a keyword names ("village life bhutan" for a Korea tour), else None.
    Countries the tour's own title names ("Nepal and Bhutan Tour") are allowed."""
    if not country:
        return None
    words = _country_words()
    allowed = {normalize_country(country)}
    for t in place_terms or []:
        if t in words:
            allowed.add(words[t])
    kw = keyword.lower()
    for w, canonical in words.items():
        if canonical not in allowed and has_word(w, kw):
            return canonical
    return None


_TRANSFER_RE = re.compile(r"\b(transfers?|airport)\b", re.IGNORECASE)


def names_foreign_place(keyword: str, foreign_places) -> bool:
    """Does the keyword name a place of another country (whole words)? `foreign_places` = lowercase
    place names (shared.destinations of every other country). S207 Taiwan trial: "paro hot stone
    bath", "gasa hot springs" (Bhutan) and "ancient city of polonnaruwa" (Sri Lanka) for a Taiwan tour —
    no country word, so foreign_country() could not see them."""
    if not foreign_places:
        return False
    text = f" {re.sub(r'[^a-z0-9]+', ' ', keyword.lower()).strip()} "
    words = set(text.split())
    return any((f" {p} " in text) if " " in p else (p in words) for p in foreign_places)


def rank_keyword_ideas(ideas: list[dict], place_terms: list[str], cap: int = 25,
                       activity_words: list[str] | None = None, country: str = "",
                       foreign_places=None) -> list[dict]:
    """AA-702 — keep only ideas about THIS tour: the idea names a place from the title (weak geo
    nouns like "valley"/"trek" don't count) or the tour's activity. Then real volume first, places
    before activity-only, by volume. Ideas without volume stay (after) — they are still this tour's
    phrasing. Generic country ideas ("india tour packages") are dropped even with high volume. Pure."""
    places = [t for t in (place_terms or []) if t and t not in _WEAK_PLACE_WORDS]
    acts = [t for t in (activity_words or []) if t]

    def _vol(i: dict) -> int:
        v = i.get("search_volume")
        return v if isinstance(v, int) and v > 0 else 0

    def _kw(i: dict) -> str:
        return str(i.get("keyword", "")).lower()

    def _place(i: dict) -> bool:
        return any(has_word(t, _kw(i)) for t in places)

    def _activity(i: dict) -> bool:
        return any(has_word(t, _kw(i)) for t in acts)

    # S207 (Korea trial): whole words only, no other country (search_demand holds every country's
    # research keywords), no airport transfers.
    relevant = [i for i in ideas if (_place(i) or _activity(i)) and not is_lodging_search(_kw(i))
                and not _TRANSFER_RE.search(_kw(i)) and not foreign_country(_kw(i), country, place_terms)
                and not names_foreign_place(_kw(i), foreign_places)]
    with_vol = sorted((i for i in relevant if _vol(i) > 0), key=lambda i: (not _place(i), -_vol(i)))
    without = [i for i in relevant if _vol(i) == 0]
    return (with_vol + without)[:cap]


# AA-693 (S206) — a lodging word decides without Jev (moved here from segment_research_batch in S207
# so S1 keyword ideas use the same rule: the S1 pilot stored "druk hotel paro" for the Druk Path trek).
# "<airport/station> to hotel" is getting there, not a lodging search. Brand-only lodging names
# ("amankora", "como punakha") carry no such word and are not caught here.
_LODGING_WORD_RE = re.compile(
    r"\b(hotels?|resorts?|lodges?|residenc[ey]|residences|boutique|homestays?|guest ?houses?|hostels?|"
    r"inns?|motels?|ryokan|villas?|agoda|airbnb|booking\.com|accommodations?)\b", re.IGNORECASE)
_TRANSFER_TO_LODGING_RE = re.compile(r"\b(airport|station|terminal)\b.*\bto\b.*\bhotel", re.IGNORECASE)


def is_lodging_search(keyword: str) -> bool:
    return bool(_LODGING_WORD_RE.search(keyword or "")) and not _TRANSFER_TO_LODGING_RE.search(keyword or "")


def unmatched_countries(target_market: dict) -> list[str]:
    """AA-629 — countries the tenant actually declared that DFS_LOCATION_MAP does NOT know.

    Pure, side-effect free (same convention as the rest of this module) — deliberately separate
    from resolve_buyer_market()/resolve_buyer_markets() rather than changing either's return
    shape, since 5+ real call sites destructure a bare 3-tuple today and would break on a shape
    change (see resolve_buyer_markets()'s own docstring warning).

    Distinguishes the ONE case the US default is actually meant for from the bug AA-629 found:
    - `countries` empty/missing -> tenant declared nothing -> [] (US default is a reasonable
      product decision here, not a bug; nothing to record).
    - `countries` non-empty but NONE of them are in DFS_LOCATION_MAP -> tenant declared REAL
      markets we don't support -> returns exactly those (deduped, order preserved) so the
      caller can record a Tier-1 unmapped-market request instead of silently substituting US
      data for a market the tenant never even listed.
    - `countries` non-empty and AT LEAST ONE matches -> [] (resolve_buyer_market(s)() already
      has a real market to use; a tenant listing e.g. ["DE", "ZZ"] is not "all unmatched").
    """
    tm = target_market or {}
    countries = tm.get("countries") or []
    if not countries:
        return []
    if any(c in DFS_LOCATION_MAP for c in countries):
        return []
    return list(dict.fromkeys(countries))


def resolve_buyer_market(target_market: dict) -> tuple[int, str, str]:
    """(location_code, location_name, language_code) from tenant target_market.

    Picks the highest-priority (lowest MARKET_RANK) country present in target_market.countries.
    Empty/unknown -> US default. language passthrough (defaults 'en').

    AA-629: this US fallback is intentionally left UNCHANGED here (5+ call sites rely on always
    getting a usable 3-tuple back, some fire-and-forget/best-effort). A caller that needs to
    distinguish "tenant declared nothing" from "tenant declared real markets we don't support"
    (so it can record/surface that instead of silently treating them the same) should call
    unmatched_countries() alongside this, not rely on this function's return value alone.
    """
    tm = target_market or {}
    countries = tm.get("countries") or []
    lang = tm.get("language", "en") or "en"

    present = [c for c in countries if c in MARKET_RANK]
    chosen = min(present, key=lambda c: MARKET_RANK[c]) if present else _DEFAULT_MARKET
    location_code, location_name = DFS_LOCATION_MAP.get(chosen, DFS_LOCATION_MAP[_DEFAULT_MARKET])
    return location_code, location_name, lang


def resolve_buyer_markets(target_market: dict) -> list[tuple[int, str, str]]:
    """EVERY market a tenant sells to, not just the single highest-priority one.

    AA-515 (STEP0c, docs/claude_audit/AA-515-step0c-multimarket-schema.md): a live query
    confirmed all 3 real tenants already store multiple countries in `target_market.countries`
    (3 each) — resolve_buyer_market() (singular, kept UNCHANGED above) was always throwing 2 of
    3 away, not because the data was thin. This is a NEW function, not a signature change to
    the existing one — 3 real call sites (services/seo_intelligence/handler.py:58,
    api/routers/admin_pipeline.py:2296, admin_pipeline.py:2507) all destructure a single
    3-tuple and would break if resolve_buyer_market() started returning a list instead.

    Returns one (location_code, location_name, language_code) per country in
    `target_market.countries` that DFS_LOCATION_MAP knows, ordered by MARKET_RANK (unranked-
    but-known markets — none exist today, but sort stably if that changes — fall after every
    ranked one). Unknown-only or empty -> [US] (the same single-market fallback
    resolve_buyer_market() already uses), never an empty list — a caller fanning out per market
    should not need a separate empty-list branch for "no usable market" versus "one market,
    the default".

    AA-629: same note as resolve_buyer_market() — this fallback is unchanged; call
    unmatched_countries() alongside this when the caller needs to tell "declared nothing" apart
    from "declared real markets we don't support".
    """
    tm = target_market or {}
    countries = tm.get("countries") or []
    lang = tm.get("language", "en") or "en"

    known = [c for c in countries if c in DFS_LOCATION_MAP]
    if not known:
        known = [_DEFAULT_MARKET]
    # dict.fromkeys dedupes while keeping first-seen order stable before the rank-sort, in case
    # a tenant's countries array ever repeats a code.
    ordered = sorted(dict.fromkeys(known), key=lambda c: (MARKET_RANK.get(c, 999), c))
    return [(*DFS_LOCATION_MAP[c], lang) for c in ordered]
