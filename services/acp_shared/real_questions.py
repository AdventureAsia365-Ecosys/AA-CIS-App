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
     '<place> <country>' (`search_demand`, keyword `topup_keyword`, market US).
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
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol, Sequence

import structlog

from services.acp_content_writing.hygiene import COMMON_CAPS, content_words
from services.acp_contract.ranking_reference import PLACE_KINDS, keyword_words

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
_FOREIGN_WORDS = frozenset("""ist sind und der das het een wat hoe waar est sont les une quel quelle quels pour wie
kann nicht mit ein eine zum zur dans avec welke kunnen moet gibt kostet bedeutet heisst
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
    r"|pronounce[ds]?|pronunciation|etymology|another\s+word|the\s+word|a\s+word|the\s+term"
    # S245 r7 (AA-779 follow-up): 'Is moron a British word?' — a word with a qualifier in between,
    # 'words', translation / dictionary / rhyme cues.
    r"|(?:a|an|the)\s+(?:\w+\s+){1,2}words?|words?\s+for|words|translat(?:e|es|ed|ing|ion|ions)"
    r"|dictionary|vocabulary|rhymes?\s+with|how\s+do\s+you\s+say|slur|derogatory)\b")

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
tourism month months best time
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


def is_off_meaning(question: str, names: Iterable[str] = (),
                   lowercase_names: Optional[Iterable[str]] = None) -> bool:
    """True when the question is about a word, not a place: a word-sense cue ('Is moron a bad
    word?', 'What does it mean if someone calls you a moron?', 'Is moron a British word?'), or —
    for `names`, the folded name tokens of the piece's own places — the name used as a word:
    a common noun ('What is a moron?', 'Is moron offensive?', 'Why do people call someone a
    moron?'), asked in another language ('What is moron in German?'), spelt / said / translated
    ('How do you spell moron?'), or written lowercase with no travel context at all ('Is moron
    still used today?'). Real travel questions stay: 'Does Mörön have an airport?', 'How far is
    Moron from Lake Khovsgol?', 'Is it rude to refuse tea in a ger?' (rude / offensive count only
    right after the name).

    `lowercase_names` (default: `names`) limits the lowercase test to real place names: a caller
    whose `names` also hold capitalised line openers of a seed ('Fly to Mörön') passes the names
    found mid-sentence only, so 'Can you fly to Mörön?' is not read as the word 'fly'."""
    q = fold(question)
    if _WORD_SENSE_RE.search(q):
        return True
    q_words = set(_TOKEN_RE.findall(q))
    names = tuple(n for n in names if n)
    lower_ok = set(names) if lowercase_names is None else set(lowercase_names)
    for name in set(names) | lower_ok:
        if not name:
            continue
        n = re.escape(name)
        if name in lower_ok and (len(name) >= 4 and name not in _PLACE_GENERIC
                                 and _lowercase_only_use(question, name)
                                 and not (q_words - {name}) & _TRAVEL_CONTEXT):
            return True
        if name not in names:
            continue
        if (re.search(rf"\b(?:a|an)\s+{n}\s*(?:\?|$|\band\b|\bor\b|\bvs\b)", q)
                or re.search(rf"\b(?:a|an)\s+(?:total|complete|real|absolute|utter|such)\s+{n}\b", q)
                or re.search(rf"\b{n}\s+(?:is\s+|so\s+)?(?:offensive|rude)\b", q)
                or re.search(rf"\b(?:call|calls|calling|called)\s+(?:you|someone|somebody|him|her|them|me)"
                             rf"\s+an?\s+{n}\b", q)
                or re.search(rf"\b{n}\s+in\s+(?:{_LANGUAGES})\b", q)
                or re.search(rf"\b(?:say|spell|write|translate)\s+(?:the\s+name\s+)?{n}\b", q)
                or re.search(rf"\b{n}\s+come\s+from\b", q)):
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
          english_only: frozenset = ENGLISH_ONLY_SOURCES) -> list[RealQuestion]:
    """The chain in tier order: cleaned (whitespace, 10–160 chars), English only for the
    `english_only` tiers, off-meaning questions out (word-sense cues, and a name of `names` — the
    piece's folded place words — used as a common noun: 'What is a moron?' for Mörön), a question
    already given by an earlier tier skipped, at most `per_source` from one tier and `limit` in all."""
    names = tuple(names)
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
            if not 10 <= len(q) <= 160 or is_off_meaning(q, names):
                continue
            if source in english_only and not looks_english(q):
                continue
            key = question_key(q)
            if key in seen:
                continue
            seen.add(key)
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


_PLACE_PAA_SQL = """
    SELECT keyword, market, search_volume, people_also_ask
    FROM acp_contract.search_demand
    WHERE keyword ILIKE ANY($1::text[])
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


def topup_keyword(place: str, country: str) -> str:
    """AA-779 (B) — the `search_demand` keyword the admin SERP top-up stores a place's questions
    under: the folded words of '<place> <country>' ('moron mongolia' for Mörön, Mongolia). The
    country makes Google read the town, not the word; the same key lets the chain tell top-up rows
    from segment-research rows."""
    words = _TOKEN_RE.findall(fold(f"{place} {country}"))
    return " ".join(dict.fromkeys(words))


def topup_keywords(scope: QuestionScope) -> set[str]:
    return {topup_keyword(p, c) for p in scope.places for c in scope.countries if p and c}


class PlacePaaSource:
    """`search_demand` PAA bought (segment research, admin) for a keyword naming one of the
    piece's places — e.g. 'lake khovsgol' for a Lake Khövsgöl moment. Rows the SERP top-up wrote
    for this scope are left to `SerpTopupSource` (their own label)."""
    name = SOURCE_PLACE

    async def fetch(self, conn, scope: QuestionScope) -> list[str]:
        words = place_words(scope.places, scope.countries)
        if not words:
            return []
        rows = await conn.fetch(_PLACE_PAA_SQL, sorted(f"%{w}%" for w in words))
        topup = topup_keywords(scope)
        return rank_place_rows([r for r in rows if r["keyword"] not in topup], words)


_TOPUP_SQL = """
    SELECT keyword, market, search_volume, people_also_ask
    FROM acp_contract.search_demand
    WHERE keyword = ANY($1::text[]) AND market = 'US'
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
        rows = await conn.fetch(_TOPUP_SQL, sorted(keys))
        words = place_words(scope.places, scope.countries)
        return rank_place_rows([r for r in rows if r["keyword"] in keys], words) if words else []


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
                                 sources: Sequence[QuestionSource] = DB_SOURCES) -> list[RealQuestion]:
    """The whole chain for one piece. Soft-fails per source (a failed read is logged and skipped;
    the tour tier always remains), so writing never stops on it. $0: DB reads only."""
    tiers: list[tuple[str, list]] = []
    scope: Optional[QuestionScope] = None
    try:
        async with pool.acquire() as conn:
            scope = await resolve_scope(conn, atom_ids, segment_ids)
            for source in sources:
                try:
                    tiers.append((source.name, await source.fetch(conn, scope)))
                except Exception as e:  # noqa: BLE001 — a missing source never blocks the write
                    logger.warning("real_questions_source_failed", source=source.name, error=str(e)[:200])
    except Exception as e:  # noqa: BLE001
        logger.warning("real_questions_scope_failed", error=str(e)[:200])
    tiers.append((SOURCE_TOUR, list(tour_paa or [])))
    # The place words also drop a question using a place name as a common noun: the place_paa tier
    # buys exactly those ('moron' for Mörön), and T8 shows the merged list to the tenant.
    names = place_words(scope.places, scope.countries) if scope else set()
    return merge(tiers, names=sorted(names))


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


def real_first_pairs(pairs: Sequence[tuple[str, str]], pool_: Sequence[dict], *, min_pairs: int,
                     max_pairs: int) -> tuple[list[tuple[str, str]], int]:
    """The FAQ with the pairs that ask a real question (matched as in `faq_sources` against
    `pool_`) first, in their order, then the writer's own: a writer pair stays while the FAQ is
    under `min_pairs` or the writer pairs are fewer than the real ones; `max_pairs` caps it all.
    With no real pair the FAQ is returned as it is. Returns (pairs, real pair count)."""
    pairs = list(pairs or [])
    flags = [bool(_match_source(q, pool_)) for q, _ in pairs] if pool_ else [False] * len(pairs)
    real = [p for p, r in zip(pairs, flags) if r][:max_pairs]
    if not real:
        return pairs, 0
    out = list(real)
    writer_n = 0
    for p, r in zip(pairs, flags):
        if r or len(out) >= max_pairs:
            continue
        if len(out) < min_pairs or writer_n < len(real):
            out.append(p)
            writer_n += 1
    return out, len(real)


# ---------------------------------------------------------------- measuring (scripts/aa779_question_coverage.py)

def count_real(labelled: Sequence[dict], names: Iterable[str] = ()) -> int:
    """How many of `labelled` ({question, source}) are real: a real source and not off-meaning for
    the piece's place `names` (a junk question that passed an older filter never counts)."""
    names = tuple(names)
    return sum(1 for item in labelled or [] if isinstance(item, dict) and item.get("source") in REAL_SOURCES
               and not is_off_meaning(item.get("question") or item.get("q") or "", names))


def piece_question_stats(writer_input: dict, names: Iterable[str] = ()) -> dict:
    """Done-when line 2 for one stored blog (`content_piece.writer_input`): the real questions it was
    given and the FAQ questions that ask one. A FAQ question counts as real only when it is not
    off-meaning itself and its recorded source is real; when it matches a given question, that one
    must not be off-meaning either."""
    names = tuple(names)
    given = [g for g in (writer_input or {}).get("search_questions") or [] if isinstance(g, dict)]
    clean_given = [g for g in given if g.get("source") in REAL_SOURCES
                   and not is_off_meaning(g.get("question") or "", names)]
    junk_given = [g for g in given if g not in clean_given]
    faq = [f for f in (writer_input or {}).get("faq_questions") or [] if isinstance(f, dict)]
    faq_real = 0
    for f in faq:
        q = f.get("question") or ""
        if f.get("source") not in REAL_SOURCES or is_off_meaning(q, names):
            continue
        if junk_given and _match_source(q, junk_given) and not _match_source(q, clean_given):
            continue
        faq_real += 1
    return {"given_real": len(clean_given), "given_off_meaning": len(junk_given), "faq_total": len(faq),
            "faq_real": faq_real}


__all__ = [
    "SOURCE_SEGMENT", "SOURCE_PLACE", "SOURCE_TOUR", "SOURCE_SERP_TOPUP", "SOURCE_AUTOCOMPLETE",
    "SOURCE_WRITER", "REAL_SOURCES", "RealQuestion", "QuestionScope", "QuestionSource", "DB_SOURCES",
    "SegmentMatchSource", "PlacePaaSource", "SerpTopupSource", "topup_keyword", "topup_keywords",
    "ENGLISH_ONLY_SOURCES", "merge", "looks_english", "is_off_meaning",
    "question_key",
    "place_words", "rank_place_rows", "resolve_scope", "collect_real_questions", "label_questions", "source_counts",
    "faq_sources", "missing_real_questions", "real_first_pairs", "count_real", "piece_question_stats",
]
