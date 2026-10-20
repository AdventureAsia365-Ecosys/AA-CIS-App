"""services.acp_shared.real_questions — AA-779 (S245) real-user question source chain for blogs.

A blog (T8 angles, T9 FAQ) used to read People Also Ask (PAA) only from the tour-level SERP
(`silver_aa_internal.seo_context`, one call on the tour title). Google often shows no PAA box for a
long, rare tour title, so the S245 checkpoint had none, while the questions of the blog's own places
were already in the DB. This module reads them, in a fixed order, at $0 (DB reads only):

  1. `segment_match` — PAA questions already landed on the piece's Segments by embedding match
     (`acp_contract.atom_matches`, kind 'question'; they passed the A3 Jev scope/landing gates).
  2. `place_paa`     — PAA bought for a keyword of one of the piece's places
     (`acp_contract.search_demand.people_also_ask`, any market, English questions only).
  Off-meaning questions (about a place name as a word: 'What is a moron?' for Mörön) are dropped
  from every tier, using the piece's own place words.
  3. `tour_paa`      — the tour-level PAA the request snapshot holds (the only source before).

Later sources plug in as more `QuestionSource` objects in `DB_SOURCES` (between `place_paa` and the
tour tier): AA-779 (B) an admin SERP top-up with click depth (`serp_topup`) and (C) a DataForSEO
Autocomplete fallback (`autocomplete`). Those buy data in an admin job only; this module never calls
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
# Reserved for AA-779 (B) / (C); no reader yet.
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
    r"|pronounce[ds]?|pronunciation|etymology|another\s+word|the\s+word|a\s+word|the\s+term)\b")


def is_off_meaning(question: str, names: Iterable[str] = ()) -> bool:
    """True when the question is about a word, not a place: a word-sense cue ('Is moron a bad
    word?', 'What does it mean if someone calls you a moron?'), or — for `names`, the folded name
    tokens of the piece's own places — the name used as a common noun ('What is a moron?', 'Is
    moron offensive?', 'Why do people call someone a moron?'). 'Is it rude to refuse tea in a ger?'
    stays: rude / offensive count only right after the name."""
    q = fold(question)
    if _WORD_SENSE_RE.search(q):
        return True
    for name in names:
        if not name:
            continue
        n = re.escape(name)
        if (re.search(rf"\b(?:a|an)\s+{n}\s*(?:\?|$|\band\b|\bor\b|\bvs\b)", q)
                or re.search(rf"\b{n}\s+(?:is\s+|so\s+)?(?:offensive|rude)\b", q)
                or re.search(rf"\b(?:call|calls|calling|called)\s+(?:you|someone|somebody|him|her|them|me)"
                             rf"\s+an?\s+{n}\b", q)):
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


class PlacePaaSource:
    """`search_demand` PAA bought (segment research, admin) for a keyword naming one of the
    piece's places — e.g. 'lake khovsgol' for a Lake Khövsgöl moment."""
    name = SOURCE_PLACE

    async def fetch(self, conn, scope: QuestionScope) -> list[str]:
        words = place_words(scope.places, scope.countries)
        if not words:
            return []
        rows = await conn.fetch(_PLACE_PAA_SQL, sorted(f"%{w}%" for w in words))
        return rank_place_rows(rows, words)


# AA-779 (B) serp_topup and (C) autocomplete append their readers here, after place_paa.
DB_SOURCES: tuple[QuestionSource, ...] = (SegmentMatchSource(), PlacePaaSource())

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


def _match_source(question: str, pool_: Sequence[dict]) -> Optional[str]:
    key = question_key(question)
    words = set(content_words(question))
    for item in pool_:
        cand = item.get("question") or item.get("q") or ""
        if question_key(cand) == key:
            return item.get("source")
    for item in pool_:
        cand_words = set(content_words(item.get("question") or item.get("q") or ""))
        if words and cand_words and len(words & cand_words) / max(len(words), len(cand_words)) >= FAQ_MATCH_OVERLAP:
            return item.get("source")
    return None


def faq_sources(faq_questions: Sequence[str], kept: Sequence[dict],
                candidates: Sequence[dict] = ()) -> list[dict]:
    """Each final FAQ question with the real source it matches (same text after folding, or
    ≥ 80% shared content words) — the questions given to the writer first, then every candidate;
    `writer` when it matches none."""
    pool_ = list(kept or []) + list(candidates or [])
    return [{"question": q, "source": _match_source(q, pool_) or SOURCE_WRITER} for q in faq_questions or []]


__all__ = [
    "SOURCE_SEGMENT", "SOURCE_PLACE", "SOURCE_TOUR", "SOURCE_SERP_TOPUP", "SOURCE_AUTOCOMPLETE",
    "SOURCE_WRITER", "REAL_SOURCES", "RealQuestion", "QuestionScope", "QuestionSource", "DB_SOURCES",
    "SegmentMatchSource", "PlacePaaSource", "ENGLISH_ONLY_SOURCES", "merge", "looks_english", "is_off_meaning",
    "question_key",
    "place_words", "rank_place_rows", "resolve_scope", "collect_real_questions", "label_questions", "source_counts",
    "faq_sources",
]
