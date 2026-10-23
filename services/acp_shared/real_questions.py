"""services.acp_shared.real_questions — AA-779 (S245) real-user question source chain for blogs.

A blog (T8 angles, T9 FAQ) used to read People Also Ask (PAA) only from the tour-level SERP
(`silver_aa_internal.seo_context`, one call on the tour title). Google often shows no PAA box for a
long, rare tour title, so the S245 checkpoint had none, while the questions of the blog's own places
were already in the DB. This module reads them, in a fixed order, at $0 (DB reads only):

  1. `segment_match` — PAA questions already landed on the piece's Segments by embedding match
     (`acp_contract.atom_matches`, kind 'question'; they passed the A3 Jev scope/landing gates).
  2. `place_paa`     — PAA bought for a keyword of one of the piece's places
     (`acp_contract.search_demand.people_also_ask`, any market, English questions only).
  2b. `serp_topup`   — PAA the admin-only SERP top-up (AA-779 B, click depth 2) bought for
     '<place> <country>' (`search_demand`, keyword `topup_keyword`, its own market `TOPUP_MARKET`
     that no other reader uses, so it never mixes with the Segment research cache).
  Off-meaning questions (about a place name as a word: 'What is a moron?' for Mörön) are dropped
  from every tier, using the piece's own place words.
  3. `tour_paa`      — the tour-level PAA the request snapshot holds (the only source before).

Later sources plug in as more `QuestionSource` objects in `DB_SOURCES` (before the tour tier):
AA-779 (B) the admin SERP top-up (`serp_topup`, reader `SerpTopupSource`, buyer
`question_topup.py`) and (C) a DataForSEO Autocomplete fallback (`autocomplete`, not built). Those
buy data in an admin job / script only; this module never calls
DataForSEO (AA-646: no tenant action buys DFS data). A question an LLM writes is never "real"
(`SOURCE_WRITER` only labels a FAQ question that matches no real one).

The source of each question is admin-only data (`content_piece.writer_input`); the tenant never sees
it (the request snapshot the tenant API returns carries question texts only).
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Protocol, Sequence

import structlog

from services.acp_content_writing.hygiene import COMMON_CAPS, content_words
from services.acp_contract.atom_ranking import (LANDING_Q, SCOPE_FOREIGN_Q, SCOPE_HERE_Q, country_scope,
                                                landing_moment, landing_subject_key, scope_dropped,
                                                scope_subject_key)
from services.acp_contract.ranking_reference import PLACE_KINDS, keyword_words
from shared.llm_client.decide import Decision, Verdict, platform_verdicts

logger = structlog.get_logger()

SOURCE_SEGMENT = "segment_match"
SOURCE_PLACE = "place_paa"
SOURCE_TOUR = "tour_paa"
# AA-779 (B) admin SERP top-up (reader: SerpTopupSource); (C) reserved, no reader yet.
SOURCE_SERP_TOPUP = "serp_topup"
SOURCE_AUTOCOMPLETE = "autocomplete"
REAL_SOURCES = (SOURCE_SEGMENT, SOURCE_PLACE, SOURCE_SERP_TOPUP, SOURCE_AUTOCOMPLETE, SOURCE_TOUR)
# A FAQ question that matches no real question (the writer or write_faq made it up).
SOURCE_WRITER = "writer"

PER_SOURCE_MAX = 12  # raw questions one source may add
CHAIN_MAX = 30       # raw questions in the job payload (paa_for_scope keeps at most 6 of them)
# A Segment whose questions_count cache is NULL (invalidated, not recomputed yet): matches newer than
# this before its last computation still count (a full landing pass takes minutes, not a day).
STALE_MATCH_SLACK = "1 day"


def fold(text: Optional[str]) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower()


def question_key(question: str) -> str:
    """The dedupe / match key: folded, whitespace collapsed, end punctuation cut."""
    return " ".join(fold(question).split()).rstrip(" ?.!")


# ---------------------------------------------------------------- language + meaning

_ENGLISH_OPENERS = frozenset("""what where when why how who whom whose which is are was were can could do does did
should would will shall may might has have had am isn aren wasn don doesn didn
""".split())
# Words of a German / French / Dutch question that an English one does not use ('Was ist …?').
# Not 'wat' (S247): it is the Thai / Lao / Khmer word for a temple ('Are there landmines near Angkor
# Wat?', 'Is Wat Pho worth visiting?'); a Dutch question starting 'Wat …' already fails the opener
# check, and 'moeite' / 'waard' / 'bezoeken' catch the ones that start with 'Is …'.
_FOREIGN_WORDS = frozenset("""ist sind und der das het een hoe waar est sont les une quel quelle quels pour wie
kann nicht mit ein eine zum zur dans avec welke kunnen moet gibt kostet bedeutet heisst moeite waard bezoeken
""".split())
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def looks_english(question: str) -> bool:
    """A PAA question in English starts with a question word or an auxiliary and has no German /
    French / Dutch function word (DE/FR/NL markets may return questions in their language)."""
    tokens = _TOKEN_RE.findall(fold(question))
    return bool(tokens) and tokens[0] in _ENGLISH_OPENERS and not (set(tokens) & _FOREIGN_WORDS)


# A question about the place NAME as a word (its meaning, spelling, an insult it sounds like) rather
# than the place: the DFS keyword of the town Mörön is 'moron', and its SERP asks about the insult.
_WORD_SENSE_RE = re.compile(
    r"\b(?:mean(?!\s+(?:annual|daily|monthly|yearly|temperatures?|depth|elevation|altitude|height|rainfall))"
    r"|means|meaning|meanings|definition|define|defined|synonyms?|antonyms?|slang|insult(?:s|ing)?"
    r"|swear\s+word|curse\s+word|bad\s+word|idiot|idiots|imbecile|stupid|spell(?:ed|ing)?|spelt"
    r"|pronounce[ds]?|pronunciation|etymology|another\s+word|the\s+word|a\s+word|the\s+term)\b")
# S245 r7 review — the later word cues ('a British word', 'word for', translation, dictionary,
# rhyme, slur) only count right next to one of the piece's own place names (see `_name_word_cue`):
# on their own they also fire on real traveller questions ('How do you say hello in Mongolian?',
# 'What is the Mongolian word for hello?', 'Is English translation available on Mongolia tours?').

# Local words for a KIND of landmark (temple, fortress, stupa, shrine …) that also make up place names
# ('Kyichu Lhakhang', 'Chorten Kora', 'Punakha Dzong', 'Wat Pho'). What one is or means is background a
# traveller asks ('What is a lhakhang?', 'What does the word "Chorten" mean?'; S247, AA-779 eval panel
# KEEP), not a place NAME used as a word, so a meaning cue on such a question does not drop it.
_LANDMARK_KINDS = frozenset("""
lhakhang lhakhangs chorten chortens dzong dzongs goemba gompa gonpa stupa stupas chedi chaitya dagoba
wat wats vihara wihan ubosot prang prasat pagoda pagodas zedi paya kyaung khiid ovoo ger torii jinja
mandir ghat ghats madrasa madrassa madrasah caravanserai hutong
""".split())
# Cues that stay off-meaning even on a landmark kind: insult / slang, another word, spelling, saying it.
_KIND_WORD_JUNK_RE = re.compile(
    r"\b(?:slang|insult(?:s|ing)?|swear|curse|bad\s+word|idiots?|imbecile|stupid|slur|derogatory|offensive|rude"
    r"|synonyms?|antonyms?|another\s+word|words?\s+for|rhymes?|spell(?:ed|ing)?|spelt|pronounce[ds]?"
    r"|pronunciation|how\s+do\s+you\s+say)\b")


def _asks_landmark_kind(q: str) -> bool:
    """The folded question `q` names a landmark kind and carries no junk word cue: a meaning cue on it
    ('What does dzong mean?', 'What is the meaning of Punakha Dzong?') asks about the landmark."""
    return bool(set(_TOKEN_RE.findall(q)) & _LANDMARK_KINDS) and not _KIND_WORD_JUNK_RE.search(q)


# Languages a 'What is <name> in <language>?' question asks a translation into.
_LANGUAGES = (
    "english|german|french|spanish|italian|portuguese|dutch|latin|greek|russian|polish|swedish|norwegian|danish"
    "|finnish|turkish|arabic|hebrew|hindi|urdu|chinese|mandarin|cantonese|japanese|korean|vietnamese|thai|malay"
    "|indonesian|tagalog|mongolian|tibetan|nepali|persian|farsi|swahili|sign\\s+language")
# A question that names the place for a traveller: a lowercase name with none of these is the word,
# not the place ('Is moron offensive in the UK?' has none; 'how far is moron from the lake?' has one).
_TRAVEL_CONTEXT = frozenset("""
visit visiting visited travel travelling traveling trip trips tour tours town city village capital province
region district country airport airports flight flights fly flying drive driving bus train road roads
hotel hotels stay stays camp camps lodge guesthouse weather climate winter summer spring autumn season
temperature temperatures cold hot warm snow rain far distance km kilometres kilometers miles located
location where population lake lakes river rivers mountain mountains valley park national near nearby
reach worth known famous things eat food restaurant museum monastery temple market safe safety deep
sights attractions frozen freeze freezes swim swimming hike hiking trek trekking open tourist tourists
tourism month months best time volcano volcanoes climb climbing steps pack packing expensive cheap cost
costs price prices spend spending festival festivals people language languages cruise cruises dangerous
crowded
""".split())
# Common English words that also make up place names ('Ha Long', 'Great Wall', 'Tiger's Nest', 'Golden
# Triangle'): never read as 'the place name used as a word' — a question using the word is ordinary
# English ('How long should I spend in Hanoi?'). 'moron' is deliberately not here: it is the case the
# test exists for (a rare word whose own sense Google answers).
_COMMON_ENGLISH = frozenset("""
long great golden gold white black blue green yellow orange pink purple brown grey gray dark light bright
rock rocks wall walls nest tiger tigers flaming fire water stone stones sand sun moon star stars sky sea
cliff cliffs old new high low big little small tall deep grand royal holy sacred happy free fine nice best
good north south east west upper lower central middle inner outer first second third last hidden lost
dragon elephant monkey horse eagle lion snake bird fish cat dog white crystal silver jade pearl iron
paradise heaven garden gardens bridge gate gates tower towers castle forest jungle desert island islands
bay beach peak point head cape spring springs wells well field fields stream wood woods green mount
crown king queen prince temple palace hall house home church market square street road way trail
triangle circle cross corner line view rise heights valley falls river lake city town village
""".split())
_NAME_TOKEN_RE = re.compile(r"[^\W\d_]+")


def _lowercase_only_use(question: str, name: str) -> bool:
    """The name occurs in the question, and only as a lowercase word after the first one —
    Google PAA capitalises a place ('Does Mörön have an airport?') but not the common word
    ('Is moron a British word?'). A first-word occurrence says nothing (always capitalised)."""
    seen = False
    for i, tok in enumerate(_NAME_TOKEN_RE.findall(question or "")):
        if fold(tok) != name:
            continue
        if i == 0 or tok[0].isupper():
            return False
        seen = True
    return seen


def _name_word_cue(q: str, n: str) -> bool:
    """A word cue right next to the (escaped, folded) place name `n` in the folded question `q`:
    'Is moron a British word?', 'Is moron a slur?', 'word for moron', 'translation of moron',
    'what rhymes with moron'."""
    return bool(
        re.search(rf"\b{n}\s+(?:is\s+)?(?:an?|the)\s+(?:\w+\s+){{0,2}}(?:words?|slur|term)\b", q)
        or re.search(rf"\b{n}\s+(?:is\s+)?(?:an?\s+)?(?:slur|derogatory)\b", q)
        or re.search(rf"\bwords?\s+for\s+(?:an?\s+|the\s+)?{n}\b", q)
        or re.search(rf"\btranslat\w*\s+(?:of\s+|for\s+)?(?:the\s+(?:word|name)\s+)?{n}\b", q)
        or re.search(rf"\b{n}\s+translat\w*", q)
        or re.search(rf"\b(?:rhymes?\s+with|dictionary\s+(?:definition|meaning)\s+of)\s+{n}\b", q)
        or re.search(rf"\b{n}\s+(?:rhymes?\s+with|in\s+the\s+dictionary)\b", q))


def _single_word(name: str) -> bool:
    return len(name) >= 4 and name not in _PLACE_GENERIC and name not in _COMMON_ENGLISH


def single_word_places(places: Iterable[str], countries: Iterable[str] = ()) -> set[str]:
    """The places whose whole name folds to ONE word ('Mörön' → 'moron'), the only names the
    lowercase-word test may read: a token of a longer name ('long' of 'Ha Long Bay', 'wall' of
    'Great Wall', 'khovsgol' of 'Lake Khövsgöl') is never one. Not a country, not a kind of
    place, not a common English word."""
    skip = {w for c in countries for w in _TOKEN_RE.findall(fold(c))}
    out: set[str] = set()
    for p in places:
        tokens = _TOKEN_RE.findall(fold(p))
        if len(tokens) == 1 and tokens[0] not in skip and _single_word(tokens[0]):
            out.add(tokens[0])
    return out


def is_off_meaning(question: str, names: Iterable[str] = (), lowercase_names: Iterable[str] = (),
                   countries: Iterable[str] = ()) -> bool:
    """True when the question is about a word, not a place: a word-sense cue ('Is moron a bad
    word?', 'What does it mean if someone calls you a moron?'), or — for `names`, the folded name
    tokens of the piece's own places — the name used as a word: a common noun ('What is a moron?',
    'Is moron offensive?', 'Why do people call someone a moron?'), with a word cue next to it ('Is
    moron a British word?', 'Is moron a slur?'), asked in another language ('What is moron in
    German?'), spelt / said / translated ('How do you spell moron?'). Real travel questions stay:
    'Does Mörön have an airport?', 'How far is Moron from Lake Khovsgol?', 'Is it rude to refuse
    tea in a ger?' (rude / offensive count only right after the name), 'How do you say hello in
    Mongolian?' (no place name next to the cue). A common English word ('long' of Ha Long Bay) is
    never read as a name.

    The lowercase test ('Is moron still used today?') reads only `lowercase_names` — folded
    single-word place names (`single_word_places`; default none). It fires only when such a name
    is written lowercase (never capitalised, never first), the question names no other place word
    of `names`/`lowercase_names` and no country of `countries` (folded country names), and has no
    travel word. The autocomplete tier passes none: its text is all lowercase.

    A landmark kind (`_LANDMARK_KINDS`: 'lhakhang', 'chorten', 'dzong', 'wat' …) is never read as a
    name, and a meaning cue on a question that names one stays ('What is a lhakhang?', 'What does the
    word "Chorten" mean?') unless it also has a junk cue (slang, another word, spelling)."""
    q = fold(question)
    kind_question = _asks_landmark_kind(q)
    if _WORD_SENSE_RE.search(q) and not kind_question:
        return True
    q_words = set(_TOKEN_RE.findall(q))
    # A landmark kind ('lhakhang' of Kyichu Lhakhang) is never read as the place name used as a word.
    names = {n for n in names if n and n not in _COMMON_ENGLISH and n not in _LANDMARK_KINDS}
    lower_ok = {n for n in lowercase_names if n and _single_word(n) and n not in _LANDMARK_KINDS}
    country_words = {w for c in countries for w in _TOKEN_RE.findall(fold(c))}
    for name in lower_ok:
        if (_lowercase_only_use(question, name)
                and not (q_words - {name}) & (_TRAVEL_CONTEXT | names | lower_ok | country_words)):
            return True
    for name in names | lower_ok:
        n = re.escape(name)
        if (re.search(rf"\b(?:a|an)\s+{n}\s*(?:\?|$|\band\b|\bor\b|\bvs\b)", q)
                or re.search(rf"\b(?:a|an)\s+(?:total|complete|real|absolute|utter|such)\s+{n}\b", q)
                or re.search(rf"\b{n}\s+(?:is\s+|so\s+)?(?:offensive|rude)\b", q)
                or re.search(rf"\b(?:call|calls|calling|called)\s+(?:you|someone|somebody|him|her|them|me)"
                             rf"\s+an?\s+{n}\b", q)
                or re.search(rf"\b{n}\s+in\s+(?:{_LANGUAGES})\b", q)
                or re.search(rf"\b(?:say|spell|write|translate)\s+(?:the\s+name\s+)?{n}\b", q)
                or re.search(rf"\b{n}\s+come\s+from\b", q)
                or _name_word_cue(q, n)):
            return True
    return False


# ---------------------------------------------------------------- the chain

@dataclass(frozen=True)
class RealQuestion:
    question: str
    source: str

    def as_dict(self) -> dict:
        return {"q": self.question, "source": self.source}


# Tiers whose questions can come from a DE / FR / NL market (`search_demand` of any market, and the
# atom_matches questions landed from it): only those are language-checked. The tour tier is the
# US / English SERP of the tour title, so 'Best time to visit Bhutan?' stays there.
ENGLISH_ONLY_SOURCES = frozenset({SOURCE_SEGMENT, SOURCE_PLACE, SOURCE_SERP_TOPUP, SOURCE_AUTOCOMPLETE})


def merge(tiers: Sequence[tuple[str, Iterable]], per_source: int = PER_SOURCE_MAX,
          limit: int = CHAIN_MAX, names: Iterable[str] = (),
          english_only: frozenset = ENGLISH_ONLY_SOURCES, lowercase_names: Iterable[str] = (),
          countries: Iterable[str] = (), drop: Optional[Mapping[str, str]] = None,
          stats: Optional[dict] = None) -> list[RealQuestion]:
    """The chain in tier order: cleaned (whitespace, 10–160 chars), English only for the
    `english_only` tiers, off-meaning questions out (word-sense cues, and a name of `names` — the
    piece's folded place words — used as a common noun: 'What is a moron?' for Mörön; the
    lowercase test with `lowercase_names` / `countries`, never on the all-lowercase autocomplete
    tier), a question already given by an earlier tier skipped, at most `per_source` from one tier
    and `limit` in all.

    AA-982 — `drop` ({question_key: reason}, from `a3_rejected`) removes a question A3 Jev already
    rejected for this piece from every tier after `segment_match` (those landed through the same
    gates, for their own Segment), so it cannot come back through a later tier either. A dropped
    question does not use its tier's room: the next one takes its place. `stats["a3_dropped"]`
    counts, per tier, the dropped questions met while that tier still had room (the ones that
    would otherwise have reached the chain)."""
    names = tuple(names)
    lowercase_names = tuple(lowercase_names)
    countries = tuple(countries)
    out: list[RealQuestion] = []
    seen: set[str] = set()
    for source, questions in tiers:
        taken = 0
        for raw in questions or []:
            if len(out) >= limit or taken >= per_source:
                break
            if not isinstance(raw, str):
                continue
            q = " ".join(raw.split())
            lower = () if source == SOURCE_AUTOCOMPLETE else lowercase_names
            if not 10 <= len(q) <= 160 or is_off_meaning(q, names, lower, countries):
                continue
            if source in english_only and not looks_english(q):
                continue
            key = question_key(q)
            if key in seen:
                continue
            seen.add(key)
            if drop and source != SOURCE_SEGMENT and key in drop:
                if stats is not None:
                    dropped = stats.setdefault("a3_dropped", {})
                    dropped[source] = dropped.get(source, 0) + 1
                continue
            out.append(RealQuestion(q, source))
            taken += 1
    return out


@dataclass(frozen=True)
class QuestionScope:
    """What a piece is about, resolved from its atoms / Route Segments."""
    segment_ids: tuple[str, ...]
    places: tuple[str, ...]
    countries: tuple[str, ...] = ()


class QuestionSource(Protocol):
    name: str

    async def fetch(self, conn, scope: QuestionScope) -> list[str]: ...


_SEGMENT_MATCH_SQL = f"""
    WITH seg AS (
        SELECT segment_id, questions_count, questions_computed_at
        FROM acp_contract.atom_segment WHERE segment_id = ANY($1::text[])
    ), hits AS (
        SELECT seg.questions_count, seg.questions_computed_at, am.query, am.distance, am.matched_by,
               am.matched_at,
               row_number() OVER (PARTITION BY seg.segment_id ORDER BY am.matched_at DESC, am.id DESC) AS rn
        FROM seg
        JOIN acp_contract.atom_segment_member m ON m.segment_id = seg.segment_id
        JOIN acp_contract.atom_matches am ON am.atom_id = m.atom_id AND am.kind = 'question'
    )
    SELECT query FROM hits
    WHERE CASE WHEN questions_count IS NOT NULL THEN rn <= questions_count
               ELSE questions_computed_at IS NULL
                    OR matched_at >= questions_computed_at - interval '{STALE_MATCH_SLACK}' END
    ORDER BY (matched_by = 'vector') DESC, distance ASC, query
"""


class SegmentMatchSource:
    """`atom_matches` rows of the Segments' atoms. The table is upsert-only (a question a later
    landing pass dropped keeps its old row), so per Segment only the `questions_count` newest rows
    count — exactly the questions its last landing pass claimed."""
    name = SOURCE_SEGMENT

    async def fetch(self, conn, scope: QuestionScope) -> list[str]:
        if not scope.segment_ids:
            return []
        rows = await conn.fetch(_SEGMENT_MATCH_SQL, list(scope.segment_ids))
        return [r["query"] for r in rows]


# AA-779 (B) — the `search_demand.market` the admin SERP top-up stores its rows under: US / English
# SERP with the PAA box expanded 2 levels. A market of its own keeps the top-up apart from Segment
# research's (keyword, market) cache: research reads only its real markets (US, UK, …) and never
# sees a no-volume top-up row as 'measured, no volume'; a top-up never overwrites a research row's
# PAA. S1 prefetch and atom ranking read rows with a volume only; the admin SEO views list it.
TOPUP_MARKET = "US-PAA2"

_PLACE_PAA_SQL = """
    SELECT keyword, market, search_volume, people_also_ask
    FROM acp_contract.search_demand
    WHERE keyword ILIKE ANY($1::text[]) AND market <> $2
      AND jsonb_typeof(people_also_ask) = 'array' AND jsonb_array_length(people_also_ask) > 0
    LIMIT 300
"""
_MARKET_ORDER = {"US": 1, "UK": 2, "AU": 3, "DE": 4, "FR": 5, "NL": 6}
_PLACE_GENERIC = frozenset(PLACE_KINDS) | COMMON_CAPS


def place_words(places: Iterable[str], countries: Iterable[str] = ()) -> set[str]:
    """The distinctive, folded words of the piece's place names a bought keyword must share:
    ≥ 4 letters, not a kind of place ('lake', 'province'), not a country."""
    skip = _PLACE_GENERIC | {w for c in countries for w in _TOKEN_RE.findall(fold(c))}
    return {w for p in places for w in _TOKEN_RE.findall(fold(p)) if len(w) >= 4 and w not in skip}


def rank_place_rows(rows, words: set[str]) -> list[str]:
    """Questions of the keywords that share a place word (whole words, accent-folded), the keyword
    sharing more place words first, then higher volume, then market (US, UK, AU, …)."""
    scored = []
    for r in rows:
        shared = keyword_words(fold(r["keyword"])) & words
        if not shared:
            continue
        paa = r["people_also_ask"]
        if isinstance(paa, str):
            paa = json.loads(paa) if paa else []
        scored.append(((-len(shared), -(r["search_volume"] or 0), _MARKET_ORDER.get(r["market"], 99),
                        r["keyword"]), [q for q in (paa or []) if isinstance(q, str)]))
    scored.sort(key=lambda s: s[0])
    return [q for _, qs in scored for q in qs]


def chain_filter_names(scope: "QuestionScope") -> dict:
    """The off-meaning filter's inputs for one scope (merge kwargs): every distinctive place word,
    the single-word place names the lowercase test may read, and the countries."""
    return {"names": sorted(place_words(scope.places, scope.countries)),
            "lowercase_names": sorted(single_word_places(scope.places, scope.countries)),
            "countries": list(scope.countries)}


def topup_keyword(place: str, country: str) -> str:
    """AA-779 (B) — the `search_demand` keyword the admin SERP top-up stores a place's questions
    under: the folded words of '<place> <country>' ('moron mongolia' for Mörön, Mongolia). The
    country makes Google read the town, not the word. Stored under market `TOPUP_MARKET`."""
    words = _TOKEN_RE.findall(fold(f"{place} {country}"))
    return " ".join(dict.fromkeys(words))


def topup_keywords(scope: QuestionScope) -> set[str]:
    return {topup_keyword(p, c) for p in scope.places for c in scope.countries if p and c}


class PlacePaaSource:
    """`search_demand` PAA bought (segment research, admin) for a keyword naming one of the
    piece's places — e.g. 'lake khovsgol' for a Lake Khövsgöl moment. Rows the SERP top-up wrote
    (`TOPUP_MARKET`) are left to `SerpTopupSource` (their own label)."""
    name = SOURCE_PLACE

    async def fetch(self, conn, scope: QuestionScope) -> list[str]:
        words = place_words(scope.places, scope.countries)
        if not words:
            return []
        rows = await conn.fetch(_PLACE_PAA_SQL, sorted(f"%{w}%" for w in words), TOPUP_MARKET)
        return rank_place_rows([r for r in rows if r["market"] != TOPUP_MARKET], words)


_TOPUP_SQL = """
    SELECT keyword, market, search_volume, people_also_ask
    FROM acp_contract.search_demand
    WHERE keyword = ANY($1::text[]) AND market = $2
      AND jsonb_typeof(people_also_ask) = 'array' AND jsonb_array_length(people_also_ask) > 0
"""


class SerpTopupSource:
    """AA-779 (B) — PAA the admin-only SERP top-up (services/acp_shared/question_topup.py, click
    depth 2) bought for '<place> <country>' of the piece's places. Read only: this module never
    buys (AA-646)."""
    name = SOURCE_SERP_TOPUP

    async def fetch(self, conn, scope: QuestionScope) -> list[str]:
        keys = topup_keywords(scope)
        if not keys:
            return []
        rows = await conn.fetch(_TOPUP_SQL, sorted(keys), TOPUP_MARKET)
        words = place_words(scope.places, scope.countries)
        rows = [r for r in rows if r["keyword"] in keys and r["market"] == TOPUP_MARKET]
        return rank_place_rows(rows, words) if words else []


# AA-779 (C) autocomplete appends its reader here, after serp_topup.
DB_SOURCES: tuple[QuestionSource, ...] = (SegmentMatchSource(), PlacePaaSource(), SerpTopupSource())

_SCOPE_SEGMENTS_SQL = """
    SELECT DISTINCT s.segment_id, s.canonical_place
    FROM acp_contract.atom_segment s
    WHERE s.segment_id IN (
        SELECT asm.segment_id FROM acp_contract.atom_segment_member asm WHERE asm.atom_id = ANY($1::text[])
        UNION
        SELECT COALESCE(al.segment_id_canonical, x.id)
        FROM unnest($2::text[]) AS x(id)
        LEFT JOIN acp_contract.atom_segment_alias al ON al.segment_id_old = x.id
    )
"""
_SCOPE_ATOMS_SQL = """
    SELECT DISTINCT ta.place, rt.country
    FROM acp_contract.tour_atoms ta
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    WHERE ta.atom_id = ANY($1::text[])
"""


async def resolve_scope(conn, atom_ids: Sequence[str], segment_ids: Sequence[str] = ()) -> QuestionScope:
    """The Segments of the atoms (plus given Route Segments, through a merged id's alias), their
    places, the atoms' own places and their tours' countries."""
    atom_ids = [str(a) for a in atom_ids if a]
    segment_ids = [str(s) for s in segment_ids if s]
    seg_rows = await conn.fetch(_SCOPE_SEGMENTS_SQL, atom_ids, segment_ids) if (atom_ids or segment_ids) else []
    atom_rows = await conn.fetch(_SCOPE_ATOMS_SQL, atom_ids) if atom_ids else []
    places = [r["canonical_place"] for r in seg_rows] + [r["place"] for r in atom_rows if r["place"]]
    return QuestionScope(
        segment_ids=tuple(sorted({r["segment_id"] for r in seg_rows})),
        places=tuple(dict.fromkeys(p for p in places if p)),
        countries=tuple(sorted({r["country"] for r in atom_rows if r["country"]})),
    )


async def collect_real_questions(pool, *, atom_ids: Sequence[str], segment_ids: Sequence[str] = (),
                                 tour_paa: Iterable = (),
                                 sources: Sequence[QuestionSource] = DB_SOURCES,
                                 a3_filter: bool = False, stats: Optional[dict] = None) -> list[RealQuestion]:
    """The whole chain for one piece. Soft-fails per source (a failed read is logged and skipped;
    the tour tier always remains), so writing never stops on it. $0: DB reads only.

    AA-982 — `a3_filter` (the T9 blog write) also drops the `A3_FILTER_SOURCES` questions A3 Jev
    already rejected for this piece (`a3_rejected`: stored verdicts only, no Jev call). `stats`, when
    given, receives the filter's counts for the caller's log / admin record; a failed lookup keeps
    every question (fail open, like Jev itself) and records `a3_error`."""
    tiers: list[tuple[str, list]] = []
    scope: Optional[QuestionScope] = None
    drop: dict[str, str] = {}
    try:
        async with pool.acquire() as conn:
            scope = await resolve_scope(conn, atom_ids, segment_ids)
            for source in sources:
                try:
                    tiers.append((source.name, await source.fetch(conn, scope)))
                except Exception as e:  # noqa: BLE001 — a missing source never blocks the write
                    logger.warning("real_questions_source_failed", source=source.name, error=str(e)[:200])
            if a3_filter:
                try:
                    drop = await a3_rejected(conn, scope, [q for name, qs in tiers if name in A3_FILTER_SOURCES
                                                           for q in qs or []], stats=stats)
                except Exception as e:  # noqa: BLE001 — a failed lookup keeps every question
                    logger.warning("real_questions_a3_filter_failed", error=str(e)[:200])
                    if stats is not None:
                        stats["a3_error"] = str(e)[:200]
    except Exception as e:  # noqa: BLE001
        logger.warning("real_questions_scope_failed", error=str(e)[:200])
    tiers.append((SOURCE_TOUR, list(tour_paa or [])))
    # The place words also drop a question using a place name as a common noun: the place_paa tier
    # buys exactly those ('moron' for Mörön), and T8 shows the merged list to the tenant.
    if stats is not None and a3_filter:
        stats.setdefault("a3_dropped", {})
    return merge(tiers, **(chain_filter_names(scope) if scope else {}), drop=drop, stats=stats)


# ---------------------------------------------------------------- A3 verdict reuse (AA-982)
#
# A3 (services/acp_contract/atom_ranking.py) asks two Jev questions about each PAA candidate of a
# Segment before it may land: `a3_question_about_here` (with `a3_question_foreign`, one call per
# (country set, question)) and `a3_landing_belongs` (per (moment, question)). Their verdicts sit in
# `shared.decision_log` under platform subject keys. The `place_paa` tier reads the same
# `search_demand` PAA, but nothing stopped a question A3 had already thrown out from reaching the
# T9 writer (S247: 32 of 52 place_paa writer questions were about_here rejects). This reads those
# verdicts back — no new gate, no Jev call, $0 — and drops a question only on what A3 itself would
# act on (enforce mode, confident, today's floors):
#   - about_here: `scope_dropped()` (A3's own rule) for the piece's own country set, or for the set
#     of one of its Segments that contains every piece country (not about anywhere in a larger set
#     ⇒ not about the piece's countries either; a smaller set's reject proves nothing);
#   - landing: a reject for EVERY moment of the piece (a Route touching one moment the question
#     does not belong to keeps it), unless about_here confidently accepts it for the piece.
# A question with no verdict (A3 never shortlisted it) is kept: a miss never drops.

A3_FILTER_SOURCES = frozenset({SOURCE_PLACE})
A3_LOOKUP_MAX = 200            # place_paa questions looked up per piece (the tier is ranked; the rest are kept)
A3_FILTER_ENV = "T9_A3_PAA_FILTER"
A3_REASON_ABOUT_HERE = "about_here"
A3_REASON_LANDING = "landing"
A3_QUESTION_KEYS = [SCOPE_FOREIGN_Q, SCOPE_HERE_Q, LANDING_Q]


def a3_filter_enabled() -> bool:
    """The kill switch (api env `T9_A3_PAA_FILTER`: start_write runs in the write request): on unless
    set to off / 0 / false / no."""
    return os.environ.get(A3_FILTER_ENV, "").strip().lower() not in ("off", "0", "false", "no")


@dataclass(frozen=True)
class A3Segment:
    """One Segment of the piece as A3 saw it: its moment and its tours' country set."""
    moment: str
    countries: tuple[str, ...]


# The same Segment view A3's precompute_question_landings() reads (active atoms only), so the moment
# and the country set match the subject keys it wrote.
_A3_SEGMENTS_SQL = """
    SELECT asg.segment_id, asg.canonical_place, asg.canonical_action,
           array_remove(array_agg(DISTINCT rt.country), NULL) AS countries
    FROM acp_contract.atom_segment asg
    JOIN acp_contract.atom_segment_member asm ON asm.segment_id = asg.segment_id
    JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = asm.atom_id
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
    WHERE asg.segment_id = ANY($1::text[])
    GROUP BY asg.segment_id, asg.canonical_place, asg.canonical_action
"""


async def load_a3_segments(conn, segment_ids: Sequence[str]) -> list[A3Segment]:
    if not segment_ids:
        return []
    rows = await conn.fetch(_A3_SEGMENTS_SQL, list(segment_ids))
    return [A3Segment(landing_moment(r["canonical_place"] or "", r["canonical_action"] or ""),
                      tuple(sorted({c for c in (r["countries"] or []) if c})))
            for r in rows]


def a3_scopes(piece_countries: Iterable[str], segments: Sequence[A3Segment]) -> list[str]:
    """The country-set strings whose about_here reject applies to the piece: its own set, and each
    Segment set that contains every piece country. None when the piece has no known country."""
    piece = {c for c in piece_countries if c}
    if not piece:
        return []
    sets = {country_scope(piece)} | {country_scope(s.countries) for s in segments
                                     if s.countries and piece <= set(s.countries)}
    return sorted(sets)


def a3_moments(segments: Sequence[A3Segment]) -> list[str]:
    return sorted({s.moment for s in segments if s.moment})


def a3_subject_keys(questions: Sequence[str], scopes: Sequence[str], moments: Sequence[str]) -> list[str]:
    return ([scope_subject_key(sc, q) for q in questions for sc in scopes]
            + [landing_subject_key(m, q) for q in questions for m in moments])


def a3_rejections(questions: Sequence[str], piece_countries: Iterable[str], segments: Sequence[A3Segment],
                  verdicts: Mapping[tuple[str, str], Verdict]) -> dict[str, str]:
    """{raw question: reason} for the questions A3's stored verdicts reject for this piece (rules in
    the block comment above). Pure; `verdicts` is `platform_verdicts()` output."""
    piece = {c for c in piece_countries if c}
    scopes = a3_scopes(piece, segments)
    own_scope = country_scope(piece) if piece else None
    moments = a3_moments(segments)
    out: dict[str, str] = {}
    for q in questions:
        for sc in scopes:
            key = scope_subject_key(sc, q)
            dec = Decision(verdicts={k: v for k in (SCOPE_FOREIGN_Q, SCOPE_HERE_Q)
                                     if (v := verdicts.get((k, key))) is not None})
            if scope_dropped(dec):
                out[q] = A3_REASON_ABOUT_HERE
                break
        if q in out or not moments:
            continue
        landing = [verdicts.get((LANDING_Q, landing_subject_key(m, q))) for m in moments]
        if not all(v is not None and v.enforced and v.zone == "reject" for v in landing):
            continue
        own = verdicts.get((SCOPE_HERE_Q, scope_subject_key(own_scope, q))) if own_scope else None
        if own is not None and own.enforced and own.zone == "accept":
            continue  # about the piece's country: a practical question A3 landing alone must not drop
        out[q] = A3_REASON_LANDING
    return out


async def a3_rejected(conn, scope: QuestionScope, questions: Sequence[str],
                      stats: Optional[dict] = None) -> dict[str, str]:
    """{question_key: reason} of the `questions` (raw tier text, as A3 stored it) that A3 Jev
    already rejected for this piece. Reads only: `acp_contract` Segments + `shared.decision_log`."""
    distinct = [q for q in dict.fromkeys(q for q in questions if isinstance(q, str) and q.strip())]
    looked_up = distinct[:A3_LOOKUP_MAX]
    segments = await load_a3_segments(conn, scope.segment_ids)
    scopes, moments = a3_scopes(scope.countries, segments), a3_moments(segments)
    keys = a3_subject_keys(looked_up, scopes, moments)
    verdicts = await platform_verdicts(conn, A3_QUESTION_KEYS, keys) if keys else {}
    rejected = a3_rejections(looked_up, scope.countries, segments, verdicts)
    if stats is not None:
        reasons: dict[str, int] = {}
        for reason in rejected.values():
            reasons[reason] = reasons.get(reason, 0) + 1
        stats.update({
            "a3_looked_up": len(looked_up), "a3_not_looked_up": len(distinct) - len(looked_up),
            "a3_with_verdict": len({q for q in looked_up
                                    if any((k, sk) in verdicts for k in A3_QUESTION_KEYS
                                           for sk in a3_subject_keys([q], scopes, moments))}),
            "a3_rejected": reasons, "a3_scopes": len(scopes), "a3_moments": len(moments),
            # the scope the lookup used, so a later check can recompute it exactly (admin record)
            "a3_countries": list(scope.countries), "a3_segment_ids": list(scope.segment_ids),
        })
    return {question_key(q): reason for q, reason in rejected.items()}


# ---------------------------------------------------------------- admin record

def label_questions(kept: Sequence[str], candidates: Sequence[dict]) -> list[dict]:
    """`kept` (paa_for_scope output, normalised text) with the source of the candidate each came
    from. A job payload from before the chain has no candidates: those questions came from the tour
    snapshot."""
    by_key = {question_key(c.get("q", "")): c.get("source") for c in candidates or [] if isinstance(c, dict)}
    return [{"question": q, "source": by_key.get(question_key(q), SOURCE_TOUR)} for q in kept or []]


def source_counts(candidates: Sequence[dict], kept: Sequence[dict]) -> dict:
    """{"candidates": {source: n}, "kept": {source: n}} for the admin piece view."""
    def _count(items, field):
        out: dict[str, int] = {}
        for it in items or []:
            src = it.get(field) if isinstance(it, dict) else None
            if src:
                out[src] = out.get(src, 0) + 1
        return out
    return {"candidates": _count(candidates, "source"), "kept": _count(kept, "source")}


FAQ_MATCH_OVERLAP = 0.8
# Words a light rewording adds or drops without changing what is asked ('Does Mongolia have any
# volcanoes?' → 'Does Mongolia have volcanoes?', r7): left out of the overlap.
_MATCH_FILLER = frozenset("any really actually ever exactly also still just there".split())


def _match_words(text: str) -> set[str]:
    return set(content_words(text or "")) - _MATCH_FILLER


def _match_source(question: str, pool_: Sequence[dict]) -> Optional[str]:
    key = question_key(question)
    words = _match_words(question)
    for item in pool_:
        cand = item.get("question") or item.get("q") or ""
        if question_key(cand) == key:
            return item.get("source")
    for item in pool_:
        cand_words = _match_words(item.get("question") or item.get("q") or "")
        if words and cand_words and len(words & cand_words) / max(len(words), len(cand_words)) >= FAQ_MATCH_OVERLAP:
            return item.get("source")
    return None


def faq_sources(faq_questions: Sequence[str], kept: Sequence[dict],
                candidates: Sequence[dict] = ()) -> list[dict]:
    """Each final FAQ question with the real source it matches (same text after folding, or
    ≥ 80% shared content words, filler words like 'any' left out) — the questions given to the
    writer first, then every candidate; `writer` when it matches none."""
    pool_ = list(kept or []) + list(candidates or [])
    return [{"question": q, "source": _match_source(q, pool_) or SOURCE_WRITER} for q in faq_questions or []]


# ---------------------------------------------------------------- real questions first in the FAQ (S245 r7)

def missing_real_questions(faq_questions: Sequence[str], given: Sequence[dict]) -> list[str]:
    """The real questions the writer was given (`given`: the in-scope, labelled
    `search_questions`, in chain order) that no FAQ question asks yet — what the FAQ
    regeneration must take first. A writer-made label never counts as real."""
    out: list[str] = []
    for item in given or []:
        if not isinstance(item, dict) or item.get("source") not in REAL_SOURCES:
            continue
        q = item.get("question") or item.get("q") or ""
        if q and not any(_match_source(f, [item]) for f in faq_questions or []):
            out.append(q)
    return out


def real_first_pairs(pairs: Sequence[tuple[str, str]], pool_: Sequence[dict], *,
                     max_pairs: int) -> tuple[list[tuple[str, str]], int]:
    """The FAQ with the pairs that ask a real question (matched as in `faq_sources` against
    `pool_`) first, in their order, then the writer's own in theirs. Only a reorder: a writer
    pair goes only when the FAQ is over `max_pairs` (S245 r7 review — trimming writer pairs to
    lift the real share would cut good answers and overstate the gain). Returns (pairs, real pair
    count)."""
    pairs = list(pairs or [])
    flags = [bool(_match_source(q, pool_)) for q, _ in pairs] if pool_ else [False] * len(pairs)
    real = [p for p, r in zip(pairs, flags) if r]
    if not real:
        return pairs, 0
    writer = [p for p, r in zip(pairs, flags) if not r]
    out = (real + writer)[:max_pairs]
    return out, min(len(real), max_pairs)


# ---------------------------------------------------------------- measuring (scripts/aa779_question_coverage.py)

def count_real(labelled: Sequence[dict], names: Iterable[str] = (), lowercase_names: Iterable[str] = (),
               countries: Iterable[str] = ()) -> int:
    """How many of `labelled` ({question, source}) are real: a real source and not off-meaning for
    the piece's place `names` (a junk question that passed an older filter never counts)."""
    names, lower, countries = tuple(names), tuple(lowercase_names), tuple(countries)
    return sum(1 for item in labelled or [] if isinstance(item, dict) and item.get("source") in REAL_SOURCES
               and not is_off_meaning(item.get("question") or item.get("q") or "", names, lower, countries))


def piece_question_stats(writer_input: dict, names: Iterable[str] = (), lowercase_names: Iterable[str] = (),
                         countries: Iterable[str] = ()) -> dict:
    """Done-when line 2 for one stored blog (`content_piece.writer_input`): the real questions it was
    given and the FAQ questions that ask one. A FAQ question counts as real only when it is not
    off-meaning itself and its recorded source is real; when it matches a given question, that one
    must not be off-meaning either."""
    names, lower, countries = tuple(names), tuple(lowercase_names), tuple(countries)

    def off(q: str) -> bool:
        return is_off_meaning(q, names, lower, countries)
    given = [g for g in (writer_input or {}).get("search_questions") or [] if isinstance(g, dict)]
    clean_given = [g for g in given if g.get("source") in REAL_SOURCES
                   and not off(g.get("question") or "")]
    junk_given = [g for g in given if g not in clean_given]
    faq = [f for f in (writer_input or {}).get("faq_questions") or [] if isinstance(f, dict)]
    faq_real = 0
    for f in faq:
        q = f.get("question") or ""
        if f.get("source") not in REAL_SOURCES or off(q):
            continue
        if junk_given and _match_source(q, junk_given) and not _match_source(q, clean_given):
            continue
        faq_real += 1
    out = {"given_real": len(clean_given), "given_off_meaning": len(junk_given), "faq_total": len(faq),
           "faq_real": faq_real}
    # S245 r7 review — pair counts before / after the FAQ step, so a higher real share can be told
    # apart from writer pairs removed (absent on pieces written before).
    for key in ("faq_pairs_before", "faq_pairs_after"):
        if isinstance((writer_input or {}).get(key), dict):
            out[key] = writer_input[key]
    return out


__all__ = [
    "SOURCE_SEGMENT", "SOURCE_PLACE", "SOURCE_TOUR", "SOURCE_SERP_TOPUP", "SOURCE_AUTOCOMPLETE",
    "SOURCE_WRITER", "REAL_SOURCES", "RealQuestion", "QuestionScope", "QuestionSource", "DB_SOURCES",
    "SegmentMatchSource", "PlacePaaSource", "SerpTopupSource", "TOPUP_MARKET", "topup_keyword", "topup_keywords",
    "ENGLISH_ONLY_SOURCES", "merge", "looks_english", "is_off_meaning", "single_word_places",
    "chain_filter_names",
    "question_key",
    "A3_FILTER_SOURCES", "A3_FILTER_ENV", "A3Segment", "a3_filter_enabled", "a3_rejections", "a3_rejected",
    "place_words", "rank_place_rows", "resolve_scope", "collect_real_questions", "label_questions", "source_counts",
    "faq_sources", "missing_real_questions", "real_first_pairs", "count_real", "piece_question_stats",
]
