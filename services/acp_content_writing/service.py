"""
services.acp_content_writing.service — T9 + T10-inline orchestration.

AA-466: /write is now 202 Accepted + poll (real API Gateway 504s on long LLM+T10 runs,
AA-453/465 — up to 2 attempts x (1 write/rewrite LLM call + 1 T10 gate check) could run ~89s).
`start_write()` does everything that was always fast/no-LLM (fetch+validate the request,
resolve goal/channel/brand/atom/trip context, insert a `content_piece` placeholder row with
status='processing') and is awaited synchronously by the router — same 404/409/422 error
contract as before. `run_write_background()` is the part that was always slow (the write/rewrite
+ T10-check loop) — launched via `asyncio.create_task()` by the router (strong-ref pattern, see
that file) and updates the SAME placeholder row in place when done. The write/check loop body
itself was UNCHANGED from the pre-AA-466 single-function version at the time of AA-466 — only the
HTTP/persistence layer around it moved. AA-528 later added the gate-regression-guard inside this
same loop (see MAX_ATTEMPTS below) — the first change to the loop's own decision logic since.

Max 2 total write attempts, confirmed cap (Phase 1 §2c's real N7 convergence data: judge-class
checks converge on repair only 2.5%-14.6% of the time — a low cap is better supported by that
data than N7's own 3-8 round range, which was calibrated for a background job with no tenant
waiting on it).

Every blocking LLM call (write, rewrite, and quality_gates.py's 2 judge gates) runs inside
`asyncio.to_thread()` from the very first version of this module — not patched in after an
incident, per the build task's explicit instruction and Phase 1's own documented lesson from
N7 (AA-416 only fixed this symptom after 2 real production ALB-timeout incidents).
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Optional
from uuid import UUID

from services.acp_shared.writing_progress import progress_show, progress_step

import structlog

from shared import tour_liveness as _tl
from fastapi import Request

from services.acp_angle_gate import service as angle_gate_service
from services.acp_angle_gate.brand_audience import fetch_brand_audience
from services.acp_angle_gate.channel_style import get_channel_style
from services.acp_angle_gate.goals import get_goal
from services.acp_shared.atom_seed import atom_seed_text, route_moment_seeds
from services.acp_shared.seed_canon import is_overnight
from services.acp_content_writing import seo_grounding
from services.acp_shared.channel_spec import channel_spec, first_sentence
from services.acp_content_writing.quality_gates import (
    _META_DESCRIPTION_MAX_CHARS as _META_MAX_CHARS, _META_DESCRIPTION_MIN_CHARS as _META_MIN_CHARS,
    _SEO_TITLE_MAX_CHARS, _SEO_TITLE_MIN_CHARS, _length_bounds_for_channel, gate_complete, gate_faq_contract,
    keyword_covered, title_taken,
)
from services.acp_content_writing.facts import fetch_facts_for_writing, format_facts_block
from services.acp_content_writing import (authenticity, faq_answer_fit, faq_filter, hygiene, length_trim,
                                          sentence_repair, text_integrity, trip_facts)
from services.acp_content_writing.generate import (SEO_FIT_STAGE, SentenceRepairError, fit_seo_fields,
                                                   repair_sentences, rewrite_with_feedback, write_content, write_faq)
from services.acp_content_writing.quality_gates import (CHEAP_REPAIR_GATES, F13_REPEAT_GATE, F14_GATE, F19_GATE,
                                                          FAQ_MAX_PAIRS, FAQ_MIN_PAIRS, F9PassMemory,
                                                          deep_strip_citation_tags, drop_restated_faq,
                                                          drop_subjectless_faq, drop_unanswerable_faq,
                                                          faq_answer_problem, faq_pair_count, faq_pairs,
                                                          hold_failure, merge_faq_pairs, never_holds,
                                                          replace_faq_section, seed_place_names,
                                                          run_quality_gates, strip_citation_tags,
                                                          unused_source_facts)
from services.acp_shared.audit_log import TenantAuditAction, write_audit_log
from services.acp_shared.content_embedding import compute_embedding, embedding_to_pgvector_literal
from services.acp_shared.piece_similarity import find_similar_pieces
from services.acp_shared.real_questions import (SOURCE_PLACE, SOURCE_WRITER, a3_filter_enabled,
                                                collect_real_questions, faq_sources,
                                                is_off_meaning, label_questions, missing_real_questions,
                                                real_first_pairs, source_counts)
from services.acp_shared.route_guard import request_route_segment_ids
from services.acp_planning.tenant_pool import fetch_tenant_trips, fetch_tenant_trips_on
from services.acp_produce.brand import fetch_brand_rubric_text
from services.acp_content_writing.gate_eval import ledger_snapshot  # AA-791
from services.quality import degraded  # AA-925
from services.quality.registry import threshold

logger = structlog.get_logger()

# AA-770 (S233) — attempt 3 exists only as a sentence-level repair of attempt 2 (never a full
# rewrite, which invents new facts); it replaces the AA-485 buffer retry (a second content_piece
# row per request). A piece whose attempt-2 failures are not sentence-repairable stops at 2.
MAX_ATTEMPTS = 3

# AA-499/AA-484 — the ONE similarity threshold Nghiệp confirmed (Q6=B, 25/07/2026, AA-332
# origin, cited in AA-484's Linear comment), for BOTH the within-tenant `within_tenant_reuse`
# flag (AA-499) and the cross-tenant `F10_cannibalization_cross_tenant` BLOCKING gate (AA-484) —
# a shared threshold for the same underlying question ("is this near-duplicate content?") is
# more defensible than inventing a second number for the softer, flag-only case.
_REUSE_SIMILARITY_THRESHOLD = threshold("t10.reuse_similarity")


class ContentWritingError(Exception):
    """Base class for this package's own domain errors."""


class RequestNotReadyError(ContentWritingError):
    """T9 requires angle_gate_request.status == 'approved' AND channel already set (T8 workflow
    steps 1-8 complete — AA-469 Việc 4 added step 8, picking a channel, AFTER the angle choice
    that used to be the last gate here)."""


class MissingCTAError(ContentWritingError):
    """Neither angle_gate_request.cta (usually NULL today, see migration 114's header) nor a
    tenant-supplied cta_override was available. STEP0's Open Question #2 resolved: T9 asks
    rather than fabricates a generic per-channel CTA (SKILL_v2.md's own step 4 says "ask for
    the specific CTA" — a human decision, not an inferred one)."""


_ATOM_TEXT_QUERY = """
    SELECT text FROM acp_contract.v_active_tour_atoms
    WHERE atom_id = $1 AND owner_scope IN ('platform', $2) AND NOT deleted AND NOT is_empty_marker
"""


async def _fetch_atom_text(tenant_id: UUID, atom_id: str, pool) -> str:
    """AA-567: `owner_scope IN ('platform', tenant_id)`, not tenant-only — see
    `acp_angle_gate/service.py::_fetch_atom_for_tenant()`'s docstring (same fix, same reasoning)
    for why the old tenant-only filter was a real bug, not an intentional isolation boundary.
    Kept local per the same precedent AA-449 already set ("kept local here rather than added to
    tenant_pool.py since it's T8-specific") — this one is T9-specific and only needs the text
    field, not the full atom dict T8's version returns."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_ATOM_TEXT_QUERY, atom_id, str(tenant_id))
        # AA-768 — the atom's evidence/hook + Segment siblings' evidence, not just its short label.
        seed = await atom_seed_text(conn, atom_id, str(tenant_id)) if row is not None else None
    if row is None:
        raise ContentWritingError(f"atom_id={atom_id!r} not found (deleted, or not a live platform/tenant atom)")
    return seed or row["text"]


async def _tenant_missing_brand_rules(tenant_id: str, pool) -> bool:
    """AA-484 — a cheap diagnostic-only check (called only when a cannibalization match already
    fired, see run_write_background()'s own comment), NOT a gate on its own: True when this
    tenant has no active `shared.tenant_brand_rules` row, the exact `brand_rules = {}` fallback
    condition `api/routers/v1_tours.py::rewrite_tour()` silently accepts (AA-425's own real
    finding, cited in this issue's own Linear description, is that this produces generic,
    convergence-prone LLM output — plausible root cause worth surfacing, not proof)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM shared.tenant_brand_rules WHERE tenant_id = $1::uuid AND is_active = true LIMIT 1",
            tenant_id,
        )
    return row is None


_ROUTE_SEGMENT_TEXT_QUERY = """
    SELECT m.atom_id, ta.text, ta.itinerary_day, ta.created_at,
           (SELECT bool_and(ar.excluded_reason IS NOT NULL) FROM acp_contract.atom_ranking ar
            WHERE ar.segment_id = m.segment_id AND ar.tour_id = ta.tour_id
              AND ar.superseded_at IS NULL) AS excluded
    FROM acp_contract.atom_segment_member m
    JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = m.atom_id
    WHERE m.segment_id = $1 AND ta.tour_id = $2::uuid
      AND ta.owner_scope IN ('platform', $3::text)
      AND NOT m.is_alias AND NOT ta.deleted AND NOT ta.is_empty_marker
    ORDER BY m.atom_id LIMIT 1
"""
# AA-567 — owner_scope IN ('platform', tenant_id), matching the identical fix to
# services/acp_shared/slate.py::_resolve_representative_atom()'s own Route-pick branch (this
# query is that same resolution, applied per-Segment across the whole walk — see this module's
# docstring above). Same reasoning: platform-wide atomize (AA-526) means a Route's member
# Segments are built from the shared atom pool by default.
# S234 (AA-768) — + itinerary_day (the walk is sorted by day) and `excluded`: TRUE when every
# current atom_ranking row of this (segment, tour) has an excluded_reason (transit, lodging, meal,
# unnamed place — exclusion does not vary by market), the same filter route_detection.py uses.
# NULL (no current ranking row) keeps the Segment.
# S234 round 2 — + created_at: within one day the moments follow the order A3 extracted them in
# (a3_atomize inserts one atom per autocommit statement, in the model's itinerary order; no
# ordinal column exists).


def _walk_order(item: tuple[int, dict]) -> tuple:
    """S234 round 2 — a route moment's place in the walk: itinerary_day (no day last); within a
    day an overnight moment last (it ends the day — S234 listed 'Sod Camp — overnight' before the
    Day 3 volcano), then the atom's extraction order (created_at), then the Segment order."""
    i, r = item
    day = r.get("itinerary_day")
    created = r.get("created_at")
    ts = created.timestamp() if hasattr(created, "timestamp") else float("inf")
    return (day is None, day or 0, is_overnight(r.get("text")), ts, i)


async def _fetch_route_segments(
    tenant_id: UUID, trip_id: Optional[str], segment_ids: list[str], pool,
    atoms_out: Optional[list] = None,
) -> list[tuple[str, str]]:
    """AA-511 Gap A (2026-09-02) — every Segment's live representative atom. Same per-Segment
    resolution `services/acp_shared/slate.py::_resolve_representative_atom()` already uses for its
    OWN single-atom fallback (identical query, one tour_id filter, first live non-alias member
    atom) — applied here to every Segment in the walk, not just the first.

    Best-effort per Segment (matches the precedent of the retired `route_detection.py::create_route_pick()`,
    "a partial/empty join degrades the snapshot's detail but never fails route_pick creation") —
    a Segment rebuilt away since pick time is silently skipped rather than failing the whole
    write; only an ENTIRELY empty join (every Segment gone) raises, since there would be nothing
    left to write from.

    AA-513 — returns `(atom_id, text)` pairs, not just joined text (was `_fetch_route_text()`
    before this build): the prompt now needs to know WHICH atom_id backs each Segment's text, so
    it can tell the model to tag a fact with THAT Segment's own id rather than one blanket id for
    the whole piece (the real gap AA-513 closes — see docs/claude_audit/
    AA-513-step0-investigation.md §1). `atom_text` for the quality GATES (gate_grounding()/
    gate_banned_patterns()) is still built by joining just the text half of each pair — see
    start_write() below — deliberately NOT the labeled prompt text, to avoid a real risk this
    STEP0 flagged: an atom_id like "atom_00af646e46" contains digits that
    find_novel_numeric_claims() would otherwise pick up as a false "supporting number".

    S234 (AA-768) — the walk is sorted by itinerary_day (S233: the scope read "ruins (day 14) → Sod
    Camp (day 3) …" because segment_ids are not in day order); a Segment whose ranking excluded it
    (A3's transit/meal/unnamed-place decision) is skipped, unless that would leave nothing; and
    moments at the same place merge into one block (atom_seed.route_moment_seeds, C10), so a pair
    is (first atom_id of the block, block). `atoms_out`, when given, receives every kept
    (atom_id, label) in day order — resolve_facts() re-reads the merged seeds from it."""
    if not trip_id:
        raise ContentWritingError(
            "route_segment_ids is set but trip_id is missing — cannot resolve any Segment's atom."
        )
    rows: list[tuple[int, dict]] = []
    async with pool.acquire() as conn:
        for i, segment_id in enumerate(segment_ids):
            row = await conn.fetchrow(_ROUTE_SEGMENT_TEXT_QUERY, segment_id, trip_id, str(tenant_id))
            if row is not None:
                rows.append((i, dict(row)))
        kept = [(i, r) for i, r in rows if not r.get("excluded")]
        if rows and not kept:
            logger.warning("t9_route_all_segments_excluded", segment_ids=segment_ids[:20])
            kept = rows
        kept.sort(key=_walk_order)
        atoms = [(r["atom_id"], r["text"]) for _, r in kept]
        segments = await route_moment_seeds(conn, atoms, str(tenant_id)) if atoms else []
    if not segments:
        raise ContentWritingError(
            f"None of route_segment_ids={segment_ids!r} resolved to a live atom for trip_id="
            f"{trip_id!r} — the Route was rebuilt away. Refresh the Slate and pick again."
        )
    if atoms_out is not None:
        atoms_out.extend(atoms)
    return segments


_SCOPE_QUERY = """
    SELECT atom_id, itinerary_day, place FROM acp_contract.v_active_tour_atoms
    WHERE atom_id = ANY($1::text[])
"""


async def _scope_line(atom_ids: list[str], pool) -> Optional[str]:
    """S233 — what part of the trip this piece covers. S232: 10/10 Terra blogs promised "Laos to
    Vietnam / to Hanoi" in the title while every seed moment was in Laos; 10/10 A&H blogs said
    "east to west" on a Thimphu/Paro-only seed."""
    try:
        async with pool.acquire() as conn:
            rows = {r["atom_id"]: r for r in await conn.fetch(_SCOPE_QUERY, [a for a in atom_ids if a])}
    except Exception as e:  # noqa: BLE001 — a scope line is an aid, never a reason to fail a write
        logger.warning("t9_scope_line_failed", error=str(e)[:200])
        return None
    moments = [rows[a] for a in atom_ids if a in rows]
    if not moments:
        return None
    days = sorted({m["itinerary_day"] for m in moments if m["itinerary_day"]})
    places = list(dict.fromkeys(m["place"] for m in moments if m["place"]))
    if len(moments) == 1:
        where = f" (day {days[0]})" if days else ""
        return (f"SCOPE: this piece is about one moment of the trip{where}: {places[0] if places else 'the seed'}. "
                "Do not promise, name or imply any other part of the trip in the title, headings or body.")
    span = f"days {days[0]}–{days[-1]}" if len(days) > 1 else (f"day {days[0]}" if days else "part")
    return (f"SCOPE: this piece covers {span} of the trip, in this order: {' → '.join(places)}. "
            "The title, headings, meta description and body must not promise, name or imply any "
            "place, leg or direction of travel outside these moments, even if the TRIP name does.")


_TRIP_PLACES_QUERY = """
    WITH span AS (
        SELECT min(itinerary_day) AS lo, max(itinerary_day) AS hi
        FROM acp_contract.v_active_tour_atoms WHERE atom_id = ANY($3::text[])
    )
    SELECT DISTINCT a.place FROM acp_contract.v_active_tour_atoms a, span
    WHERE a.tour_id = $1::uuid AND a.owner_scope IN ('platform', $2::text)
      AND NOT a.deleted AND NOT a.is_empty_marker
      AND a.place IS NOT NULL AND btrim(a.place) <> ''
      AND span.lo IS NOT NULL AND a.itinerary_day IS NOT NULL
      AND (a.itinerary_day < span.lo OR a.itinerary_day > span.hi)
      AND NOT (a.atom_id = ANY($3::text[]))
"""


async def _trip_places_outside(tenant_id, trip_id: Optional[str], atom_ids: list[str], pool) -> list[str]:
    """S234 round 3 (r2 §4 fix 3c) — every place of the FULL trip on a day outside this piece's days
    (the trip's itinerary atoms), for the out-of-scope gazetteer: 'Ulaanbaatar' on a days 3–14
    Khövsgöl blog (r2 #5, #17), Thimphu / Paro on a Punakha day-4 post. [] without a trip, without
    a scoped day, or on any error — scope is an aid, never a reason to fail a write."""
    ids = [a for a in atom_ids if a]
    if not trip_id or not ids:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_TRIP_PLACES_QUERY, trip_id, str(tenant_id), ids)
        return [r["place"] for r in rows if isinstance(r["place"], str)]
    except Exception as e:  # noqa: BLE001
        logger.warning("t9_trip_places_failed", error=str(e)[:200])
        return []


# S233 checkpoint: related_keywords[0] was "khovsgol and gobi tour mongolia price" for all 8
# Mongolia blogs — titles became "…Mongolia Price of Memory" and no meta could hold it (F4).
_TRANSACTIONAL_RE = re.compile(
    r"\b(price|prices|cost|costs|cheap|deal|deals|discount|book|booking|fare|fares|budget|for sale|review|reviews"
    r"|reddit|tripadvisor)\b", re.I)


_GENERIC_PLACE_WORDS = {"tour", "trip", "visit", "travel", "camp", "camps", "lake", "river", "ruins", "ancient",
                        "monastery", "temple", "village", "town", "city", "valley", "national", "park", "market",
                        "district", "province", "region", "museum", "palace", "fort", "hotel", "airport", "road"}


def _fold_kw(text: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c)).lower()


def _location_only(keyword: str, seed_text: str) -> Optional[str]:
    """S234 round 2 — the keyword without its ungrounded product words ('punakha dzong meditation
    retreat' → 'punakha dzong'), kept only when at least 2 words remain."""
    bad = set(seo_grounding.unsupported_products(keyword, seed_text))
    words = [w for w in keyword.split() if not any(st in bad for st in seo_grounding.product_terms(w))]
    while words and words[-1].lower() in {"and", "or", "with", "for", "in", "of", "the"}:
        words.pop()
    return " ".join(words) if len(words) >= 2 else None


# AA-779 — how many raw PAA questions ride in the job payload (paa_for_scope keeps at most 6).
PAA_RAW_MAX = 12


def choose_keyword(related_keywords: list, seed_text: str, out_of_scope=()) -> Optional[str]:
    """The blog's SEO keyword: a non-transactional related keyword that shares a place word with
    the piece's own moment(s) and names no place in `out_of_scope` (folded gazetteer entries,
    hygiene.build_scope_lexicon — S234). None otherwise — the writer then writes title/meta
    naturally.

    S234 round 2 (AA-772) — a keyword's product words ('meditation', 'retreat', 'cruise', 'trek' …,
    seo_grounding.PRODUCT_STEMS) must be used by the seed itself; otherwise the next candidate is
    tried, and when none is grounded, the first place-matching one with its product words removed
    (if 2+ words remain). The country of a "Location:" line is read with its parenthetical removed
    (S234: '(do not place it in any other town, region or country)' hid it, so 'Bhutan meditation
    retreat' matched every Punakha seed on 'bhutan')."""
    candidates = [k.strip() for k in related_keywords or [] if isinstance(k, str) and k.strip()
                  and not _TRANSACTIONAL_RE.search(k)
                  and not any(hygiene.names_place(k, g) for g in out_of_scope)]
    if not candidates:
        return None
    # Only the places of the piece's own moment(s): the "Location:" lines, minus country names and
    # generic words (S233: "camp" in "Sod Camp" matched "gobi tour tourist camp" on a Khövsgöl route;
    # "other trips" lines had picked "gobi" for a monastery-ruins piece).
    loc_lines = [re.sub(r"\(.*?\)", " ", line.split(":", 1)[1])
                 for line in (seed_text or "").splitlines() if line.startswith("Location:")]
    locs = " ".join(loc_lines)
    if not locs.strip():  # no Location line (atom without a country): the label's place part
        locs = " ".join(line.split(" — ", 1)[0] for line in (seed_text or "").splitlines() if " — " in line)
        locs = re.sub(r"\(.*?\)", " ", locs)
    # The country = after the last comma, or the whole line when it has none (a card-less atom with
    # no place: 'Location: Bhutan (do not name …)' — review r2b).
    countries = {w for line in loc_lines for w in re.findall(r"[a-z]{4,}", _fold_kw(line.rsplit(",", 1)[-1]))}
    seed_words = ({w for w in re.findall(r"[a-z]{4,}", _fold_kw(locs))} - countries - _GENERIC_PLACE_WORDS)
    fallback = None
    for k in candidates:
        if not seed_words & set(re.findall(r"[a-z]{4,}", _fold_kw(k))):
            continue
        ungrounded = seo_grounding.unsupported_products(k, seed_text)
        if not ungrounded:
            return k
        logger.info("t9_keyword_ungrounded", keyword=k, products=ungrounded)
        fallback = fallback or _location_only(k, seed_text)
    return fallback  # None: no keyword about this moment — the writer titles it naturally


def _held_reason_from(first_failure: dict) -> str:
    return f"{first_failure['gate']}: {'; '.join(first_failure['violations'][:3])}"


def _hold_or_ship(gate_ledger: list, flags: list) -> tuple[str, Optional[str], list]:
    """(status, held_reason, flags) for a piece that stops with failures: 'held' on the first
    product-truth gate (quality_gates.hold_failure); 'approved' when only a style-only gate
    (F15_voice) or an under-floor F14_social_shape (AA-780) is left — it is added to the flags so the
    admin still sees it (quality_gates.never_holds)."""
    hold = hold_failure(gate_ledger)
    if hold is not None:
        return "held", _held_reason_from(hold), flags
    # Only the BLOCKING entries being waived here (mirrors _waive_style_only): a non-blocking warn —
    # e.g. a post-cut F14 under-floor warn — is already in `flags` from run_quality_gates or the cut.
    style = [g for g in gate_ledger if not g["passed"] and g.get("blocking", True) and never_holds(g)]
    return "approved", None, list(flags) + [{**g, "blocking": False} for g in style if g not in flags]


# AA-786 (Gate-1 policy, S235) — channels where an F9 product-truth failure left after the last
# attempt is cut in code. PR #709 review (orchestrator, under AA-770 "held = admin-only label; the
# tenant always receives content"): the F9 entry is waived only when EVERY flagged sentence was cut;
# anything left keeps F9 blocking, so the piece is labelled held for the admin. AA-777 extends the cut
# to the blog on its attempt 3 only (BLOG_TRUTH_CUT_ATTEMPT), never under the blog's minimum length.
F9_CUT_NOT_HOLD_CHANNELS = frozenset({"linkedin"})
# AA-777 — the blog attempt where an F1/F9 truth item that survived one sentence repair is cut, not
# repaired again (attempt 2 stays the sentence repair). S233+S234: sentence repair fixes F9 49%, a full
# rewrite 0/6, and a second repair adds new blocking fails the regression guard then reverts (16×).
BLOG_TRUTH_CUT_ATTEMPT = MAX_ATTEMPTS
TRUTH_CUT_GATES = frozenset({"F1_grounding", "F9_cta_fact"})
# AA-777 (r6 fix 6) — an F17_trip_fact item the blog's attempt 3 cuts too, when F17 was already sent to a
# sentence repair once: deterministic findings whose own instruction is to cut (a time / position / pace /
# background base the source lacks, an optional activity sold as fixed, an interpreted thin-moment coda).
# Never the family caveat ('add ONE', never a cut) or a played-down hazard ('keep the hazard').
F17_CUT_GATE = "F17_trip_fact"
_F17_CUT_RE = re.compile(r"\bcut it\b|cut the added clause")


def _plain(text: Optional[str]) -> str:
    return " ".join(strip_citation_tags(text or "").split())


_SPACE_BEFORE_MARK = re.compile(r"\s+([.,;:!?…])")


def _cut_key(text: Optional[str]) -> str:
    """AA-777 (AA-786 review leftover) — the form a cut is counted in: tag-free, whitespace-normalised and no
    space before a mark. sentence_repair quotes a sentence whose tag sat before its full stop as
    'Zhabdrung .' while strip_citation_tags gives 'Zhabdrung.', so a kept sentence was counted as cut."""
    return _SPACE_BEFORE_MARK.sub(r"\1", _plain(text))


def _blog_cut_refusal(before: str, after: Optional[str]) -> Optional[str]:
    """AA-777 — why a cut of flagged sentences on a blog is refused, or None when it may stand. Never
    under the blog minimum (`_BLOG_MIN_CHARS`, measured on the whitespace-normalised tag-free text — the
    stricter of the F4 measure and this one), and never a FAQ contract or a complete ending the text had
    before the cut. A refused cut leaves the flagged sentence in, so the piece stays held on it."""
    if after is None:
        return "no flagged sentence is safe to cut (the hook, the CTA, a FAQ question or an orphaned pronoun)"
    lo, _ = _length_bounds_for_channel("blog")
    n = len(_plain(after))
    if n < lo:
        return f"the cut would leave the blog at {n} chars, under the {lo}-char minimum"
    for gate in (gate_faq_contract, gate_complete):
        new = gate(after)
        if not new["passed"] and gate(before)["passed"]:
            return f"the cut would fail {new['gate']}"
    return None


def _cut_gates_repaired(repair_log: list[dict]) -> frozenset:
    """AA-777 — the cut gates (F1_grounding, F9_cta_fact and, r6 fix 6, F17_trip_fact) an earlier attempt
    already sent to a sentence repair: failing and blocking in the trimmed ledger (AA-791) of a repair_log
    entry the sentence repair ran on (`repair_mode` 'sentences')."""
    return frozenset(
        g.get("gate") for e in repair_log if e.get("repair_mode") == "sentences"
        for g in e.get("gate_ledger") or []
        if g.get("gate") in TRUTH_CUT_GATES | {F17_CUT_GATE} and not g.get("passed") and g.get("blocking", True))


def _truth_repaired_before(repair_log: list[dict]) -> bool:
    """AA-777 — did an earlier attempt already send a failing F1/F9 truth gate to a sentence repair?
    A truth gate first seen after a full rewrite, or only after a style-only repair, still gets its one
    repair."""
    return bool(_cut_gates_repaired(repair_log) & TRUTH_CUT_GATES)


def _cuttable(item: dict, repaired: frozenset) -> bool:
    """AA-777 — may the blog's attempt 3 cut this plan() item? A truth item (its `gate` is F1/F9) once
    any truth gate was repaired before (the S239 #761 rule); r6 fix 6 — an item with an F17 'cut it'
    finding once F17 was repaired before (r6 a2133050: the thin coda held at attempt 3 because the F9
    item on the same sentence was dropped and F17 items were never cut). r7 review — never an item whose
    sentence must stay (the family caveat 'add ONE', a played-down hazard 'keep the hazard'), checked first:
    merged with a truth or 'cut it' finding, it would otherwise be cut and the hazard lost."""
    if sentence_repair.keeps_sentence(item.get("problem")):
        return False
    if item.get("gate") in TRUTH_CUT_GATES and repaired & TRUTH_CUT_GATES:  # plan() puts a truth gate first
        return True
    return (F17_CUT_GATE in (item.get("gates") or [item.get("gate")]) and F17_CUT_GATE in repaired
            and bool(_F17_CUT_RE.search(item.get("problem") or "")))


def _cut_blog_truth_items(content_text: str, items: list[dict], *, ignore: frozenset = frozenset(),
                          cta: Optional[str] = None, moments: Optional[list] = None,
                          repaired: frozenset = TRUTH_CUT_GATES) -> tuple[str, list[dict], Optional[dict]]:
    """AA-777 — blog, attempt 3: cut the F1_grounding / F9_cta_fact items of `items` (sentence_repair.plan
    on attempt 2's ledger, i.e. truth items still failing after attempt 2's sentence repair) with
    sentence_repair.delete_flagged (+ tidy_after_cut) instead of sending them to a second repair call.
    Returns (text, items left to repair — relocated in the returned text, entry or None):
    - no truth item → (text, items, None), nothing changes;
    - a refused cut (`_blog_cut_refusal`: under the blog minimum, a FAQ / ending broken, nothing safe to
      cut) or a left item that cannot be found again → (text, items, entry 'truth_cut_refused'): the
      old path (one more repair of every item) runs and the hold stays if it fails;
    - otherwise → (cut text, the other items + any truth item delete_flagged refused, entry 'truth_cut').
    r6 fix 6 — `repaired` (`_cut_gates_repaired`) also lets an F17 'cut it' item be cut (`_cuttable`); the
    default keeps the #761 behaviour (F1/F9 items only)."""
    truth = [it for it in items if _cuttable(it, repaired)]
    if not truth:
        return content_text, items, None
    cut = sentence_repair.delete_flagged(content_text, truth, ignore=ignore, cta=cta, moments=moments)
    refusal = _blog_cut_refusal(content_text, cut)
    if refusal is None:
        plain_after = _cut_key(cut)
        cut_items = [it for it in truth if _cut_key(it["sentence"]) not in plain_after]
        rest: list[dict] = []
        for it in items:
            if it in cut_items:
                continue
            # AA-984 — a heading item left to repair is found again as a heading
            locate = sentence_repair.locate_heading if it.get("heading") else sentence_repair.locate_sentence
            span = locate(cut, it["sentence"])
            if span is None:
                refusal = "a sentence left to repair could not be found after the cut"
                break
            rest.append({**it, "start": span[0], "end": span[1]})
    if refusal is not None:
        return content_text, items, {"repair_mode": "truth_cut_refused", "sentences_cut": 0,
                                     "truth_cut_refused": refusal}
    # AA-984 — a flagged heading is never cut: delete_flagged gave it its section's place name
    headings = [it for it in cut_items if it.get("heading")]
    cut_items = [it for it in cut_items if not it.get("heading")]
    entry = {"repair_mode": "truth_cut", "sentences_cut": len(cut_items),
             "cut_sentences": [it["sentence"][:200] for it in cut_items],
             "cut_gates": sorted({g for it in cut_items + headings for g in (it.get("gates") or [it.get("gate")])})}
    if headings:
        entry["headings_replaced"] = len(headings)
        entry["replaced_headings"] = [it["sentence"][:200] for it in headings]
    return cut, rest, entry


def _is_floor_block(g: dict) -> bool:
    """AA-780 — a failed, blocking F14_social_shape that never holds: the attempt-1 under-floor block."""
    return g["gate"] == F14_GATE and not g["passed"] and g.get("blocking", True) and never_holds(g)


def _floor_only_failure(blocking_fails: list) -> bool:
    """AA-780 review — the attempt's blocking failures are all gates that never hold, one of them the
    under-floor F14 block (the AA-528 guard does not revert a rewrite of such an attempt that is repairable)."""
    return (bool(blocking_fails) and all(never_holds(g) for g in blocking_fails)
            and any(_is_floor_block(g) for g in blocking_fails))


def _waive_floor_block(gate_ledger: list, flags: list) -> tuple[list, list]:
    """AA-780 review — (ledger, flags) with the under-floor F14 block of a reverted-to attempt 1 stored
    `blocking: false, waived: true` and shown as the F14 flag (replacing an F14 flag of the discarded
    rewrite). A no-op for any other ledger."""
    if not any(_is_floor_block(g) for g in gate_ledger):
        return gate_ledger, flags
    ledger = [{**g, "blocking": False, "waived": True} if _is_floor_block(g) else g for g in gate_ledger]
    waived = [g for g in ledger if g["gate"] == F14_GATE and g.get("waived")]
    return ledger, [f for f in flags if f.get("gate") != F14_GATE] + waived


def _cut_truth_items(content_text: str, gate_ledger: list, flags: list, *, channel: str,
                     ignore: frozenset = frozenset(), cta: Optional[str] = None,
                     moments: Optional[list] = None, spec=None) -> tuple[str, list, list, Optional[dict]]:
    """AA-786 — LinkedIn cut-not-rewrite, run once on the LAST attempt. When the piece is about to stop
    with a blocking F9_cta_fact made only of product-truth items (unsupported claims, TEXT_INTEGRITY —
    never a missing CTA), cut each flagged sentence that is safe to cut (sentence_repair.delete_flagged:
    not the hook, not the CTA, no orphaned pronoun).
    - Every flagged sentence gone → the F9 entry is stored `blocking: false, waived: true` (and added to
      the flags, so the admin still sees it).
    - Any flagged sentence left (a refused cut, a quote that matches no sentence) → F9 stays blocking and
      the piece is held (an admin label; the tenant still receives it, AA-770).
    Both cases record `sentences_cut` / `sentences_left` on the F9 entry and in the repair_log entry.
    After a cut the length gates (F4 extreme length, F14 social shape) are re-run on the cut text and
    replace their ledger entries, so a piece cut below the band is held on that gate.
    Returns (content_text, gate_ledger, flags, repair_log entry or None); a no-op for any other channel
    or ledger. The blog has its own cut, before the gates of its attempt 3 (`_cut_blog_truth_items`, AA-777)."""
    if channel not in F9_CUT_NOT_HOLD_CHANNELS:
        return content_text, gate_ledger, flags, None
    f9 = next((g for g in gate_ledger if g["gate"] == "F9_cta_fact" and not g["passed"]
               and g.get("blocking", True)), None)
    if f9 is None or not sentence_repair.f9_truth_only(f9):
        return content_text, gate_ledger, flags, None
    items, unlocated = sentence_repair.truth_items(content_text, f9)
    cut = sentence_repair.delete_flagged(content_text, items, ignore=ignore, cta=cta, moments=moments) \
        if items else None
    plain_after = _cut_key(cut if cut is not None else content_text)
    left = [it["sentence"] for it in items if _cut_key(it["sentence"]) in plain_after] + unlocated
    n_cut = len(items) - (len(left) - len(unlocated))
    pre_cut = content_text
    if cut is not None and n_cut:
        content_text = cut
    else:
        n_cut = 0
    fresh: dict = {}
    too_short = False
    if n_cut:  # review N1 — the length gates read the cut text, not the pre-cut one
        from services.acp_content_writing.quality_gates import gate_extreme_length, gate_social_shape
        length, shape = gate_extreme_length(_plain(content_text), channel), gate_social_shape(content_text, spec)
        fresh = {length["gate"]: length, shape["gate"]: shape}
        before = gate_social_shape(pre_cut, spec)  # the same check on the text before the cut

        def under(r: dict) -> bool:
            return any(" under the " in v for v in r.get("violations") or [])
        # a post the cut pushed under its band (or to an empty text) is not waived
        too_short = (under(shape) and not under(before)) or not length["passed"]
    all_cut = n_cut > 0 and not left and not too_short
    reason = (f"{n_cut} flagged sentence(s) cut on {channel}"
              + (f"; {len(left)} flagged sentence(s) left — held for admin review" if left else "")
              + ("; the cut left the post under its length band — held for admin review" if too_short else "")
              + " (AA-786)")
    marks = {"sentences_cut": n_cut, "sentences_left": left}
    new_f9 = ({**f9, **marks, "blocking": False, "waived": True, "waived_reason": reason} if all_cut
              else {**f9, **marks, "cut_reason": reason})
    ledger = [fresh.get(g["gate"], new_f9 if g is f9 else g) for g in gate_ledger]
    entry = {"gate_targeted": "F9_cta_fact", "repair_mode": "f9_truth_cut" if n_cut else "f9_cut_refused",
             **marks, "violations": [reason], "repairable": False}
    return content_text, ledger, _refresh_flags(flags, fresh) + ([new_f9] if all_cut else []), entry


def _refresh_flags(flags: list, fresh: dict) -> list:
    """AA-780 review — after a cut re-runs the length/shape gates (`fresh`, gate name → result), their
    warn flags from before the cut (e.g. an F14 under-floor warn with the pre-cut word count) are
    replaced by the post-cut warns, so the admin drawer never shows a stale and a fresh entry side by side.
    A fresh BLOCKING failure stays only in the ledger (it holds the piece)."""
    kept = [f for f in flags if f.get("gate") not in fresh]
    return kept + [r for r in fresh.values() if not r["passed"] and not r.get("blocking", True)]


def _enforce_authenticity(content_text: str, gate_ledger: list, flags: list, status: str,
                          held_reason: Optional[str], *, cta: Optional[str], channel: str,
                          spec=None) -> tuple[str, list, list, str, Optional[str], Optional[dict]]:
    """AA-819 (AUP 2026-11) — fail closed on F19_authenticity after the write/repair loop. The loop
    already sent the flagged sentences to the sentence repair (t9_repair); when a review, customer
    first-person line or independence claim is still in the body, it is CUT here (the hook too — the
    rule wins over the piece's shape; never the CTA). A hold alone is not enough: a held piece still
    reaches the tenant and can be published (AA-770).
    - Nothing left → the F19 entry is stored `blocking: false, waived: true` with `sentences_cut`
      and `cut_sentences` (admin-only evidence; the tenant projection drops the ledger) and is added
      to the flags.
    - A title / meta hit left (the SEO fit refused to keep one but could not replace it) → F19 stays
      blocking with what is left; the piece is held for the admin.
    The deterministic shape gates (F4 extreme length, F0 complete, F14 social shape / first line, and
    F16 FAQ on a blog) are re-run on the cut text and replace their entries; the status is re-derived
    from the new ledger.
    Returns (content_text, gate_ledger, flags, status, held_reason, repair_log entry or None)."""
    f19 = next((g for g in gate_ledger if g["gate"] == F19_GATE and not g["passed"]), None)
    if f19 is None:
        return content_text, gate_ledger, flags, status, held_reason, None
    cut_text, cut = authenticity.cut_flagged(content_text, cta)
    if not cut:
        return content_text, gate_ledger, flags, status, held_reason, None
    from services.acp_content_writing.quality_gates import (gate_complete, gate_extreme_length, gate_faq_contract,
                                                            gate_first_line, gate_social_shape)
    fresh = {r["gate"]: r for r in (gate_extreme_length(_plain(cut_text), channel), gate_complete(cut_text),
                                    gate_social_shape(cut_text, spec), gate_first_line(cut_text, spec),
                                    # review fix — a cut FAQ pair can leave the blog under FAQ_MIN_PAIRS
                                    gate_faq_contract(cut_text))}
    left = authenticity.find_issues(cut_text, cta) + [v for v in f19["violations"]
                                                       if v.startswith(("title ", "meta "))]
    marks = {"sentences_cut": len(cut), "cut_sentences": [c[:200] for c in cut]}
    reason = f"{len(cut)} sentence(s) cut in code after the repair (AA-819 AUP guard)"
    if left:
        new_f19 = {**f19, **marks, "violations": left, "cut_reason": reason + "; still flagged — held for admin review"}
    else:
        new_f19 = {**f19, **marks, "blocking": False, "waived": True, "waived_reason": reason}
    ledger = [fresh.get(g["gate"], new_f19 if g is f19 else g) for g in gate_ledger]
    flags = _refresh_flags(flags, fresh) + ([] if left else [new_f19])
    status, held_reason, flags = _hold_or_ship(ledger, flags)
    entry = {"gate_targeted": F19_GATE, "repair_mode": "aup_cut", **marks, "violations": [reason],
             "repairable": False}
    return cut_text, ledger, flags, status, held_reason, entry


F15_STOCK_CUT_FLAG = "F15_stock_cut"


def _cut_stock_lines(content_text: str, gate_ledger: list, flags: list, *, channel: str, spec=None,
                     cta: Optional[str] = None, ignore: frozenset = frozenset(),
                     moments: Optional[list] = None) -> tuple[str, list, list, Optional[dict]]:
    """AA-780 (S246 panel) — after the last attempt, cut every sentence that still matches
    channel_spec.STOCK_PATTERNS, in code (no LLM, no new gate). F15_voice blocks a stock line on attempt 1
    only (one sentence repair); a stock line in a later attempt's text was a warn, so t9l-010 shipped
    "That's the detail worth paying attention to …" (all 15 S246 posts reached attempt 2 or 3).
    - Only a channel with a spec (LinkedIn): STOCK_PATTERNS is read only there (gate_voice).
    - The cut is sentence_repair.delete_flagged: never the hook, never the CTA, no orphaned pronoun, and
      kept only when the integrity check passes. A refused sentence stays, with its F15_voice warn.
    - The cut is not made when the cut text would be under the post's word band or fail F4 (the r4 false
      passes were too-short posts; a post already under the floor is never made shorter); the stock warn
      then stays too.
    - The re-run F4 / F14 results replace their ledger entries and warn flags (`_refresh_flags`); the
      F15_voice entry keeps only the violations whose phrase is still in the text (passed when none is),
      and the cut sentences are shown as a non-blocking `F15_stock_cut` flag. Status is never changed.
    Returns (content_text, gate_ledger, flags, repair_log entry or None)."""
    if spec is None:
        return content_text, gate_ledger, flags, None
    items = hygiene.stock_line_items(content_text)
    if not items:
        return content_text, gate_ledger, flags, None
    cut = sentence_repair.delete_flagged(content_text, items, ignore=ignore, cta=cta, moments=moments)
    from services.acp_content_writing.quality_gates import gate_extreme_length, gate_social_shape
    refused = None
    if cut is None:
        refused = "no flagged sentence was safe to cut (hook, CTA, orphaned pronoun or integrity)"
    else:
        length, shape = gate_extreme_length(_plain(cut), channel), gate_social_shape(cut, spec)
        if (any(" under the " in v for v in shape.get("violations") or []) or not length["passed"]
                or shape.get("blocking")):
            refused = "the cut would leave the post under its length band"
    if refused:
        entry = {"gate_targeted": F15_STOCK_CUT_FLAG, "repair_mode": "stock_cut_refused", "sentences_cut": 0,
                 "violations": [f"stock line(s) kept: {refused} (AA-780)"], "repairable": False}
        return content_text, gate_ledger, flags, entry
    plain_after = _cut_key(cut)
    gone = [it for it in items if _cut_key(it["sentence"]) not in plain_after]
    if not gone:
        return content_text, gate_ledger, flags, None
    folded_after = " ".join(cut.split()).lower()

    def still_there(v: str) -> bool:
        # "stock phrase '<phrase>' — …: '<sentence>'" (gate_voice); other F15 findings are left as they are
        if not v.startswith("stock phrase '"):
            return True
        return v[len("stock phrase '"):].split("' — ", 1)[0].lower() in folded_after

    def trim_f15(g: dict) -> dict:
        if g.get("gate") != "F15_voice" or g.get("passed"):
            return g
        left = [v for v in g.get("violations") or [] if still_there(v)]
        if left == list(g.get("violations") or []):
            return g
        if left:
            return {**g, "violations": left}
        return {**g, "violations": [], "passed": True, "blocking": False}

    fresh = {length["gate"]: length, shape["gate"]: shape}
    ledger = [trim_f15(fresh.get(g["gate"], g)) for g in gate_ledger]
    new_flags = [trim_f15(f) for f in _refresh_flags(flags, fresh)]
    new_flags = [f for f in new_flags if not (f.get("gate") == "F15_voice" and f.get("passed"))]
    note = [f"cut stock line '{it['phrase']}': '{it['sentence'][:200]}'" for it in gone]
    new_flags.append({"gate": F15_STOCK_CUT_FLAG, "passed": False, "blocking": False, "repairable": False,
                      "violations": note})
    entry = {"gate_targeted": F15_STOCK_CUT_FLAG, "repair_mode": "stock_cut", "sentences_cut": len(gone),
             "cut_sentences": [it["sentence"][:200] for it in gone],
             "violations": [f"{len(gone)} stock-line sentence(s) cut in code after the last attempt (AA-780)"],
             "repairable": False}
    return cut, ledger, new_flags, entry


async def _after_cut(content_text: str, tenant_id: Optional[str], embedding):
    """AA-786 review N1 — after the LinkedIn cut: the writer's summary may still state the cut claim, so
    it is cleared (a None summary is a normal soft-fail outcome, AA-498); the embedding is recomputed on
    the cut text (one embedding call; the old one is kept if that call fails). Returns (summary, embedding)."""
    if not tenant_id:
        return None, embedding
    fresh = await asyncio.to_thread(compute_embedding, strip_citation_tags(content_text))
    return None, fresh if fresh is not None else embedding


def _grounding_text(context: dict) -> str:
    """AA-768 — the gates' view of what the writer was given as fact: the seed (atom or joined
    route moments) + the Facts block + the audience and trip lines build_user_prompt() also
    shows the writer. The audience/trip lines were missing before, so a number the brand itself
    states (terra-family's "children aged 7–16") failed F1 as invented (S231 pilot).
    S234 round 2 — the TRIP line is the one the writer got (`writer_trip_name`, the scoped trip name)
    when run_write_background() set it."""
    trip = context["writer_trip_name"] if "writer_trip_name" in context else context.get("trip_name")
    return grounding_text_from(
        seed=context["atom_text"], facts_text=context.get("facts_text"),
        brand_audience=context.get("brand_audience"), trip_name=trip,
        destination=context.get("destination"),
    )


def grounding_from_writer_input(writer_input: dict) -> str:
    """AA-769 (S234) — the same grounding text, rebuilt from a stored piece's `writer_input`
    (writer_input_snapshot() below), for the admin sentence rewrite and its checks."""
    wi = writer_input or {}
    return grounding_text_from(
        seed=wi.get("seed") or "", facts_text=wi.get("facts"), brand_audience=wi.get("brand_pack"),
        trip_name=wi["writer_trip"] if "writer_trip" in wi else wi.get("trip"), destination=wi.get("destination"),
    )


def grounding_text_from(*, seed: str, facts_text: Optional[str], brand_audience: Optional[dict],
                        trip_name: Optional[str], destination: Optional[str]) -> str:
    """The one implementation behind _grounding_text() and grounding_from_writer_input()."""
    parts = [seed, facts_text or ""]
    audience = brand_audience or {}
    if audience.get("brand_name"):
        parts.append(f"BRAND: {audience['brand_name']}")
    # AA-771 — the brand describing itself in its own words is a source, not an invented claim
    # (S232: 4 F9 flags on the brand's own stated approach were over-strict).
    if audience.get("core_idea"):
        parts.append(f"BRAND CORE IDEA: {audience['core_idea']}")
    if audience.get("customer_segment"):
        # S234 (C11) — kept for F1 (the brand's own '7–16'), labelled so the F9 judge stops taking
        # the segment as trip truth (S233: FAQ age answers passed on it).
        parts.append(f"AUDIENCE (who the brand writes for — not a fact about this trip): "
                     f"{audience['customer_segment']}")
    if trip_name:
        parts.append(f"TRIP: {trip_name}")
    if destination:
        parts.append(f"DESTINATION: {destination}")
    return "\n\n".join(p for p in parts if p)


def _seo_title_ok(title, keyword: Optional[str]) -> bool:
    return (isinstance(title, str) and _SEO_TITLE_MIN_CHARS <= len(title) <= _SEO_TITLE_MAX_CHARS
            and keyword_covered(title, keyword))


def _meta_ok(meta, keyword: Optional[str]) -> bool:
    return (isinstance(meta, str) and _META_MIN_CHARS <= len(meta) <= _META_MAX_CHARS
            and meta.rstrip().endswith((".", "!", "?")) and keyword_covered(meta, keyword))


def _ascii_slug(slug: Optional[str]) -> Optional[str]:
    """S233 — a slug with accents ("…-khövsgöl") failed F4 lowercase-kebab; fold it to ASCII."""
    if not isinstance(slug, str) or not slug:
        return slug
    folded = re.sub(r"[^a-z0-9]+", "-", _fold_kw(slug)).strip("-")
    return folded[:60].rstrip("-") or slug


def _title_variant(title: Optional[str], brand: Optional[str], taken: list[str]) -> Optional[str]:
    """S234 round 4 — a deterministic way out of a title clash the fit could not fix: the title with
    ' | <brand>' when that is inside the band and not taken itself; else None (F4 reports the clash)."""
    if not title or not brand:
        return None
    cand = f"{title} | {brand}"
    ok = _SEO_TITLE_MIN_CHARS <= len(cand) <= _SEO_TITLE_MAX_CHARS and not title_taken(cand, taken)
    return cand if ok else None


async def _fit_seo_meta(seo_meta: dict, keyword: Optional[str], *, body: str, context: dict,
                        request_id, hyg: Optional[hygiene.HygieneContext] = None,
                        taken_titles: Optional[list[str]] = None) -> tuple[dict, float]:
    """AA-768 (S233) — when the blog writer's title or meta is outside the T10 band, regenerate
    both with one short call (generate.fit_seo_fields) instead of cutting the string. The S232
    deterministic cut (fit_seo_title, built for tour names) turned "Bhutan's Craft Bazaar:
    Thimphu's Weekend Market…" into the 21-char "Bhutan's Craft Bazaar" on 6/20 blogs and cut
    metas mid-phrase. A new value is kept only if it is inside the band and keeps the keyword;
    otherwise the writer's own stays and F4 reports it. Returns (seo_meta, cost).

    S234 — with `hyg`, a title or meta naming a place outside the piece's scope (r1: 'Gobi' in
    8/8 titles) is out of band too; the fit is told never to use those places, and a fitted value
    that still names one is rejected (F12 then reports the writer's own)."""
    if seo_meta and seo_meta.get("slug"):
        seo_meta = {**seo_meta, "slug": _ascii_slug(seo_meta["slug"])}
    lex = hyg.lexicon if hyg is not None else None

    taken = list(taken_titles or [])

    def title_ok(value) -> bool:
        # S234 round 4 (r3 #0 = #19) — never a title an earlier piece of this brand already uses
        # AA-819 — nor a review / customer voice / independence claim ('Honest Review: …', 'Rated 4.9')
        return (_seo_title_ok(value, keyword) and (lex is None or not hygiene.places_named(lex, value))
                and not title_taken(value, taken) and not authenticity.field_hits(value))

    seed = context.get("atom_text") or ""

    def meta_ok(value) -> bool:
        # S234 round 2 — a meta may not sell a product the seed and the post never mention
        # ('a Bhutan meditation retreat' on a Punakha Dzong visit, 6/6 S234 Punakha posts).
        # S234 round 3 — nor a role the seed dates as ended in the present tense ('remains Bhutan's …
        # administrative center today', r2 #1/#9/#13) or a rarity claim the seed does not make.
        return (_meta_ok(value, keyword) and (lex is None or not hygiene.places_named(lex, value))
                and not seo_grounding.meta_unsupported_products(value, seed, body)
                and not trip_facts.date_authority_claims("", seed, extra_texts=(("meta", value),))
                and not trip_facts.rarity_claims("", hygiene.split_seed(seed)[0], extra_texts=(("meta", value),))
                and not authenticity.field_hits(value))  # AA-819

    if not seo_meta or (title_ok(seo_meta.get("seo_title")) and meta_ok(seo_meta.get("meta_description"))):
        return seo_meta, 0.0
    audience = context.get("brand_audience") or {}
    forbidden = sorted({p for v in (seo_meta.get("seo_title"), seo_meta.get("meta_description"))
                        for p in (hygiene.places_named(lex, v) if lex is not None else [])})
    forbidden += [w for w in seo_grounding.meta_unsupported_products(seo_meta.get("meta_description"), seed, body)
                  if w not in forbidden]
    forbidden += [m for v in (seo_meta.get("seo_title"), seo_meta.get("meta_description"))
                  for _, m in authenticity.field_hits(v) if m not in forbidden]  # AA-819
    if trip_facts.date_authority_claims("", seed, extra_texts=(("meta", seo_meta.get("meta_description")),)):
        forbidden += [w for w in ("still", "remains", "today", "active center", "administrative center")
                      if w not in forbidden]
    extra = {"forbidden_words": forbidden} if forbidden else {}
    if title_taken(seo_meta.get("seo_title"), taken):
        clash = [t for t in taken if title_taken(t, [seo_meta.get("seo_title")])]
        extra["taken_titles"] = clash[:1] + [t for t in taken if t not in clash][:9]
    try:
        fitted, cost = await asyncio.to_thread(
            fit_seo_fields, body=body, seo_title=seo_meta.get("seo_title"),
            meta_description=seo_meta.get("meta_description"), keyword=keyword,
            brand_name=audience.get("brand_name"), tenant_id=context.get("tenant_id"),
            angle_gate_request_id=str(request_id), **extra,
        )
    except Exception as e:  # soft-fail: F4 reports the writer's own values
        logger.warning("t9_seo_fit_failed", request_id=str(request_id), error=str(e)[:200])
        return seo_meta, 0.0

    def merge(base: dict, cand: dict) -> dict:
        out = dict(base)
        for key, ok in (("seo_title", title_ok), ("meta_description", meta_ok)):
            if not ok(out.get(key)) and ok(cand.get(key)):
                out[key] = cand[key]
        return out

    out = merge(seo_meta, fitted)
    if not (title_ok(out.get("seo_title")) and meta_ok(out.get("meta_description"))):
        # LLMs miscount characters: one more try, told exactly how far off the current values are.
        try:
            again, cost2 = await asyncio.to_thread(
                fit_seo_fields, body=body,
                seo_title=fitted.get("seo_title") or out.get("seo_title"),
                meta_description=fitted.get("meta_description") or out.get("meta_description"),
                keyword=keyword, brand_name=audience.get("brand_name"), tenant_id=context.get("tenant_id"),
                angle_gate_request_id=str(request_id), **extra,
            )
            cost += cost2
            out = merge(out, again)
        except Exception as e:  # noqa: BLE001
            logger.warning("t9_seo_fit_retry_failed", request_id=str(request_id), error=str(e)[:200])
    if title_taken(out.get("seo_title"), taken):
        variant = _title_variant(out.get("seo_title"), audience.get("brand_name"), taken)
        if variant and _seo_title_ok(variant, keyword):
            out = {**out, "seo_title": variant}
    return out, cost


_FACT_BLOCK_RE = re.compile(r"\[Fact id=([^\]]+)\]\n.*?(?=\n\n\[Fact id=|\Z)", re.DOTALL)
_CITE_RE = re.compile(r"\[(R|F):([^\]]+)\]")


def _repair_source(content_text: str, items: list[dict], context: dict) -> str:
    """S234 — what a sentence repair is shown: only the route moment(s) and fact(s) the flagged
    sentences cite ([R:id] / [F:id]) plus the brand line, not the whole grounding text (19.4k
    chars on the S233 Mongolia route: about $0.02 → $0.007 per Sonnet repair). A one-moment piece
    sends its own seed (about 2.4k chars). A route sentence that cites nothing gets the whole
    grounding text, as before."""
    cited: dict[str, list[str]] = {"R": [], "F": []}
    for it in items:
        for kind, ident in _CITE_RE.findall(content_text[it["start"]:it["end"]]):
            cited[kind].append(ident.strip())
    route = [(a, t) for a, t in (context.get("route_segments") or [])]
    if len(route) > 1:
        by_id = dict(route)
        moments = [by_id[i] for i in dict.fromkeys(cited["R"]) if i in by_id]
        if not moments and not cited["F"]:
            return _grounding_text(context)
    else:
        moments = [context.get("atom_text") or ""]
    facts = {m.group(1).strip(): m.group(0) for m in _FACT_BLOCK_RE.finditer(context.get("facts_text") or "")}
    parts = moments + [facts[i] for i in dict.fromkeys(cited["F"]) if i in facts]
    audience = context.get("brand_audience") or {}
    if audience.get("brand_name"):
        parts.append(f"BRAND: {audience['brand_name']}")
    if audience.get("core_idea"):
        parts.append(f"BRAND CORE IDEA: {audience['core_idea']}")
    return "\n\n".join(p for p in parts if p)


def _final_integrity(content_text: str, hyg: hygiene.HygieneContext, gate_ledger: list[dict], flags: list[dict],
                     status: str, held_reason: Optional[str]) -> tuple:
    """S234 round 4 — `text_integrity.final_pass` on the text about to be stored. Its edits go in the
    flags (F13_final_pass, non-blocking); its residue (the F13_repeat findings left — the same
    `hygiene.find_issues()["repeat"]` the gate reads) replaces the ledger's F13_repeat entry and holds
    an approved piece. Returns (content_text, gate_ledger, flags, status, held_reason)."""
    content_text, log, residue = text_integrity.final_pass(content_text, hyg)
    if log:
        flags = list(flags) + [{"gate": "F13_final_pass", "passed": False, "blocking": False, "repairable": False,
                                "violations": log}]
    if not residue:
        return content_text, gate_ledger, flags, status, held_reason
    entry = {"gate": F13_REPEAT_GATE, "passed": False, "blocking": True, "repairable": True,
             "violations": residue[:10]}
    ledger = [g for g in gate_ledger if g.get("gate") != F13_REPEAT_GATE] + [entry]
    if status == "approved":
        status, held_reason = "held", _held_reason_from(entry)
    return content_text, ledger, flags, status, held_reason


def _degraded_final(gate_ledger: list[dict], flags: list[dict], repair_log: list[dict], status: str,
                    held_reason: Optional[str], *, piece_id=None) -> tuple:
    """AA-925 — the final attempt's `degraded[]` (services/quality/degraded.py): the F9 outage marker on its
    ledger result and the job's repair-contract reverts. Adds the admin flag + a repair_log entry, logs one
    `quality_degraded` event and holds an approved piece on a hard entry. Returns
    (status, held_reason, flags, repair_log)."""
    contract = sum(int(e.get("contract_reverts") or 0) for e in repair_log if isinstance(e, dict))
    lst = degraded.t9_entries(gate_ledger, contract)
    if not lst:
        return status, held_reason, flags, repair_log
    degraded.emit(lst, stage="t9", piece_id=str(piece_id) if piece_id else None)
    flag = degraded.t9_flag(lst)
    new_status, new_reason = degraded.apply_t9_hold(status, held_reason, lst)
    entry = {"step": "degraded", "degraded": lst, "held": new_status != status}
    return new_status, new_reason, list(flags) + [flag], list(repair_log) + [entry]


def _waive_style_only(gate_ledger: list[dict], status: str) -> list[dict]:
    """S234 round 2 (panel §3, piece #7) — an APPROVED piece can still carry a failed style-only gate
    (F15_voice) marked `blocking: true` when the AA-528 guard reverted to attempt 1's ledger. The
    piece ships with it by design (hold_failure skips style-only gates), so the persisted ledger
    says so too: `blocking: false, waived: true`. Readers count blocking failures from gate_ledger
    (CONTEXT.md), and "failed blocking yet approved" read as a gate bug."""
    if status != "approved":
        return gate_ledger
    # AA-780 — an under-floor F14_social_shape kept from attempt 1 the same way (never_holds).
    return [{**g, "blocking": False, "waived": True}
            if not g["passed"] and g.get("blocking", True) and never_holds(g) else g
            for g in gate_ledger]


def _show_live_text(content_text: str, seo_meta: dict, channel: str) -> None:
    """AA-668 — put the text the write keeps into the live view (no-op without a bound tracker):
    the blog view reads a JSON envelope, every other channel plain text. Called after a sentence
    repair is spliced (the repair reply itself never streams) and right before the save, so the
    final snapshot is the saved piece, not the last streamed draft."""
    if channel == "blog":
        progress_show({"seo_title": (seo_meta or {}).get("seo_title"),
                       "meta_description": (seo_meta or {}).get("meta_description"),
                       "body": content_text})
    else:
        progress_show(content_text)


def _style_only_repair(gate_ledger: list[dict]) -> bool:
    """Every blocking failure a sentence repair will fix is one the cheap t9_repair stage handles
    (S234 round 2: F15_voice — stock phrase, contrast frame, sibling template; round 3: F13_repeat,
    F17_trip_fact, F18_rarity — the repair keeps, softens or cuts a sentence the seed already bounds).
    F1 / F9 / leak / brand failures in the same attempt keep the t9_write stage."""
    failed = [g for g in gate_ledger if not g["passed"] and g.get("blocking", True)
              and g["gate"] not in sentence_repair.OWNED_ELSEWHERE]
    return bool(failed) and all(g["gate"] in CHEAP_REPAIR_GATES for g in failed)


async def _enforce_faq_contract(content_text: str, *, context: dict, hyg: hygiene.HygieneContext,
                                request_id, brand_ignore: frozenset,
                                fix_subjects: bool = False,
                                required: Optional[list[str]] = None) -> tuple[str, float, list[str]]:
    """S234 round 2 (panel §5 fix 1) — a blog must end with "## FAQ" and ≥ FAQ_MIN_PAIRS pairs, counted
    after drop_restated_faq / drop_unanswerable_faq. When short: ONE t9_repair call
    (generate.write_faq, about $0.004–0.008 on Haiku) writes a new FAQ from the moment's own seed
    lines + Facts, answering first from the facts the body does not tell yet (unused_source_facts,
    deterministic). The new FAQ is the writer's surviving pairs plus the regenerated ones (S234 r2
    review: replacing the section threw away 1–2 good pairs and left the blog short again), with
    prose found after the old FAQ moved above it (the CTA stays before the FAQ); hygiene.autofix and
    both droppers run, then a pair that asks an earlier question again goes (merge_faq_pairs) and
    the FAQ is capped at FAQ_MAX_PAIRS. The result is kept only when it has more pairs than before
    and passes the repair integrity check. F16_faq then reports whatever is still short. Returns
    (text, cost, log lines).

    AA-779 — the call also gets the piece's in-scope People Also Ask questions
    (context["paa_questions"]). `fix_subjects`: the FAQ has ≥ FAQ_MIN_PAIRS pairs, but some answers
    do not answer their question (quality_gates.faq_answer_problem) and could not be dropped without
    going under the minimum — the same one regeneration runs; those pairs leave the base before the
    merge, and the merged FAQ goes through drop_subjectless_faq too. Kept when it has more pairs, or
    (fix_subjects) at least as many with fewer failing ones.

    S245 r7 — `required`: the real search questions the FAQ does not ask yet (missing_real_questions).
    The same one regeneration answers them first (write_faq required_questions); the merged FAQ puts
    real-question pairs first (real_first_pairs: a reorder; a writer pair goes only above
    FAQ_MAX_PAIRS). Kept also when it asks more real questions than before and does not fall under
    min(before, FAQ_MIN_PAIRS). An off-meaning question is never required (checked again here)."""
    before = faq_pair_count(content_text)
    own_seed = hygiene.split_seed(context.get("atom_text") or "")[0]
    if required:
        names, lower, countries = seed_place_names(own_seed)
        required = [q for q in required if q and not is_off_meaning(q, names, lower, countries)]
    required = [q for q in (required or []) if q][:FAQ_MAX_PAIRS]
    if before >= FAQ_MIN_PAIRS and not fix_subjects and not required:
        return content_text, 0.0, []
    facts_text = context.get("facts_text") or ""
    audience = context.get("brand_audience") or {}
    source = own_seed + (f"\n\nFACTS:\n{facts_text}" if facts_text else "")
    subject_source = f"{own_seed}\n{facts_text}"
    body = content_text.partition("## FAQ")[0]
    paa_questions = [q for q in context.get("paa_questions") or [] if q not in required]
    real_pool = _real_question_pool(context)
    try:
        pairs, cost = await asyncio.to_thread(
            write_faq, source=source, body=body, brand_name=audience.get("brand_name"),
            tenant_id=context.get("tenant_id"), angle_gate_request_id=str(request_id),
            unused_facts=unused_source_facts(source, body),
            **({"paa_questions": paa_questions} if paa_questions else {}),
            **({"required_questions": required} if required else {}),
        )
    except Exception as e:  # noqa: BLE001 — F16_faq reports the short FAQ
        logger.warning("t9_faq_regen_failed", request_id=str(request_id), error=str(e)[:200])
        return content_text, 0.0, []
    # AA-779 — a pair that does not answer its question is not part of the base the regeneration adds to
    context_for_check = f"{body}\n{subject_source}"
    base = [(q, a) for q, a in faq_pairs(content_text) if not faq_answer_problem(q, a, context_for_check)]
    failing_before = faq_pair_count(content_text) - len(base)
    new = replace_faq_section(content_text, base + list(pairs))
    new, _ = hygiene.autofix(new, hyg)
    new, _ = drop_restated_faq(new)
    new, _ = drop_unanswerable_faq(new, own_seed, facts_text)
    new, _, failing_after = drop_subjectless_faq(new, subject_source)
    # S245 r7 — a pair asking a real search question goes before the writer's own (and wins a duplicate)
    ordered = _real_pairs_first(faq_pairs(new), real_pool)
    merged, real_after = real_first_pairs(merge_faq_pairs(ordered), real_pool, max_pairs=FAQ_MAX_PAIRS)
    merged = merged[:FAQ_MAX_PAIRS]
    if merged != faq_pairs(new):
        new = replace_faq_section(new, merged)
    after = faq_pair_count(new)
    real_before = sum(1 for f in faq_sources([q for q, _ in faq_pairs(content_text)], real_pool)
                      if f["source"] != SOURCE_WRITER) if real_pool else 0
    problems = hygiene.integrity_problems(content_text, new, brand_ignore, context.get("cta"))
    better = (after > before or (fix_subjects and after >= before and failing_after < failing_before)
              or (bool(required) and real_after > real_before and after >= min(before, FAQ_MIN_PAIRS)))
    if not better or problems:
        why = "; ".join(problems[:2]) if problems else f"{after} pair(s) left after the droppers"
        return content_text, cost, [f"FAQ regeneration not kept ({why})"]
    real_note = f", real questions {real_before} → {real_after}" if required else ""
    return new, cost, [f"regenerated the FAQ section ({before} → {after} pairs{real_note})"]


def _real_question_pool(context: dict) -> list[dict]:
    """S245 r7 — what a FAQ question is matched against to be ordered first / counted as real: only
    the in-scope, labelled questions the writer got (paa_for_scope kept them). A chain candidate
    paa_for_scope dropped never makes a writer pair 'real'; the full candidate pool is used only for
    the admin faq_questions provenance record."""
    return [g for g in context.get("search_questions") or []
            if isinstance(g, dict) and g.get("source") != SOURCE_WRITER]


def _real_pairs_first(pairs: list[tuple[str, str]], real_pool: list[dict]) -> list[tuple[str, str]]:
    """Stable: pairs asking a real question first (so merge_faq_pairs keeps the real twin)."""
    if not real_pool:
        return list(pairs)
    tags = [f["source"] != SOURCE_WRITER for f in faq_sources([q for q, _ in pairs], real_pool)]
    return [p for p, t in zip(pairs, tags) if t] + [p for p, t in zip(pairs, tags) if not t]


async def _apply_faq_answer_fit(content_text: str, flags: list[dict], *, context: dict, piece_id=None,
                                request_id=None) -> tuple[str, list[dict], float]:
    """S247 (AA-779) — the answer-fit step on the final blog text (faq_answer_fit.apply_answer_fit). Stores its
    record in context["faq_answer_fit"] (writer_input, admin-only) and adds what `on` dropped to the admin-only
    F7_faq_autofix flag (a new entry, or the attempt's existing one). Returns (text, flags, cost)."""
    content_text, record = await faq_answer_fit.apply_answer_fit(
        content_text, context=context, piece_id=piece_id, request_id=request_id)
    if not record:
        return content_text, flags, 0.0
    context["faq_answer_fit"] = record
    lines = faq_answer_fit.fit_log_lines(record)
    if lines:
        entry = next((f for f in flags if f.get("gate") == "F7_faq_autofix"), None)
        if entry is None:
            flags = flags + [{"gate": "F7_faq_autofix", "passed": False, "blocking": False, "repairable": False,
                              "violations": lines}]
        else:
            entry["violations"] = list(entry.get("violations") or []) + lines
    return content_text, flags, float(record.get("cost_usd") or 0.0)


def real_first_faq(content_text: str, context: dict) -> tuple[str, list[str]]:
    """S245 r7 (AA-779) — deterministic, every blog attempt after the FAQ contract: real-question
    pairs first (real_first_pairs: a reorder; no writer pair is dropped unless the FAQ is over
    FAQ_MAX_PAIRS). No change when the FAQ asks no real question. Returns (text, log lines for the
    admin-only F7_faq_autofix flag)."""
    real_pool = _real_question_pool(context)
    pairs = faq_pairs(content_text)
    if not real_pool or not pairs:
        return content_text, []
    out, real_n = real_first_pairs(pairs, real_pool, max_pairs=FAQ_MAX_PAIRS)
    if out == pairs:
        return content_text, []
    dropped = len(pairs) - len(out)
    log = [f"FAQ puts its {real_n} real search question(s) first"
           + (f" and drops {dropped} writer question(s) over the {FAQ_MAX_PAIRS}-pair cap" if dropped else "")]
    return replace_faq_section(content_text, out), log


def faq_pair_stats(content_text: str, context: dict) -> dict:
    """S245 r7 review — {pairs, real} of a FAQ (real = asks an in-scope given question), recorded
    before and after the FAQ step so the Done-when line-2 checkpoint can tell real questions added
    from writer questions removed."""
    pool_ = _real_question_pool(context)
    questions = [q for q, _ in faq_pairs(content_text)]
    real = sum(1 for f in faq_sources(questions, pool_) if f["source"] != SOURCE_WRITER) if pool_ else 0
    return {"pairs": len(questions), "real": real}


async def _fix_first_line(content_text: str, *, spec, context: dict, hyg: hygiene.HygieneContext,
                          request_id, brand_ignore: frozenset) -> tuple[str, float, list[str]]:
    """S234 round 2 — LinkedIn hook line still over the brand's first_line_max after
    hygiene.autofix's deterministic split (S234 #4 165, #12 209 chars: the first SENTENCE is the long
    part). ONE t9_repair call (about $0.001–0.002 on Haiku) rewrites only that sentence to ≤120
    characters; the splice is re-split, then kept only when the line fits and the integrity check
    passes. F14_first_line reports what is still too long."""
    if spec is None or not spec.first_line_max:
        return content_text, 0.0, []
    head = content_text.strip().split("\n", 1)[0].strip()
    if len(strip_citation_tags(head)) <= spec.first_line_max:
        return content_text, 0.0, []
    first = next((sp for sp in hygiene.sentence_spans(content_text) if sp["kind"] == "prose"), None)
    if first is None:
        return content_text, 0.0, []
    item = {"start": first["start"], "end": first["end"], "sentence": first["clean"],
            "problem": (f"this opening line is {len(first['clean'])} characters; LinkedIn cuts the hook at "
                        f"{spec.first_line_max}. Rewrite it as ONE sentence of at most 120 characters with the same "
                        "meaning and no new fact, place, number or stock phrase")}
    item["context"] = sentence_repair.neighbours(content_text, item)
    try:
        reps, cost = await asyncio.to_thread(
            repair_sentences, sentences=[item], content_seed=_repair_source(content_text, [item], context),
            facts_text=None, channel=context.get("channel"), tenant_id=context.get("tenant_id"),
            angle_gate_request_id=str(request_id), stage=SEO_FIT_STAGE, mode="first_line",
        )
    except Exception as e:  # noqa: BLE001 — F14_first_line reports the long line
        logger.warning("t9_first_line_fix_failed", request_id=str(request_id), error=str(e)[:200])
        return content_text, 0.0, []
    if not (reps and reps[0].strip()):
        return content_text, cost, ["first-line rewrite returned nothing; kept the original"]
    new = sentence_repair.splice(content_text, [dict(item)], reps, ignore=brand_ignore)
    new, _ = hygiene.autofix(new, hyg)
    line = strip_citation_tags(new.strip().split("\n", 1)[0].strip())
    problems = hygiene.integrity_problems(content_text, new, brand_ignore, context.get("cta"))
    if len(line) > spec.first_line_max or problems:
        why = "; ".join(problems[:2]) if problems else f"still {len(line)} characters"
        return content_text, cost, [f"first-line rewrite not kept ({why})"]
    return new, cost, [f"rewrote the first sentence to fit the hook line ({len(head)} → {len(line)} characters)"]


_SIBLING_TEXTS_QUERY = """
    SELECT content_text FROM acp_shared.content_piece
    WHERE tenant_id = $1::uuid AND angle_gate_request_id = $2::uuid AND piece_id <> $3::uuid
      AND status IN ('approved', 'held') AND btrim(content_text) <> ''
    ORDER BY created_at DESC
    LIMIT 5
"""


async def _sibling_texts(tenant_id: Optional[str], request_id: UUID, piece_id: UUID, pool) -> list[str]:
    """S234 round 2 (panel §5 fix 6) — up to 5 earlier pieces of the SAME angle_gate_request, one
    SELECT, for the F15_voice sibling-template check. [] on any error or without a tenant."""
    if not tenant_id:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SIBLING_TEXTS_QUERY, str(tenant_id), str(request_id), str(piece_id))
        return [r["content_text"] for r in rows if isinstance(r["content_text"], str)]
    except Exception as e:  # noqa: BLE001
        logger.warning("t9_sibling_texts_failed", request_id=str(request_id), error=str(e)[:200])
        return []


_SIBLING_TITLES_QUERY = """
    SELECT seo_title FROM acp_shared.content_piece
    WHERE tenant_id = $1::uuid AND channel = $2 AND piece_id <> $3::uuid
      AND status IN ('approved', 'held') AND seo_title IS NOT NULL AND btrim(seo_title) <> ''
    ORDER BY created_at DESC
    LIMIT 50
"""


async def _sibling_titles(tenant_id: Optional[str], channel: Optional[str], piece_id: UUID, pool) -> list[str]:
    """S234 round 4 (r3 panel §4 fix 5) — the SEO titles of this tenant's 50 most recent pieces on the
    same channel, one SELECT, so a new title never repeats one (r3 #0 = #19 'Lake Khövsgöl: Mongolia's
    Family Adventure Hub'). [] on any error or without a tenant (fail-open: F4 then cannot see a clash)."""
    if not tenant_id or not channel:
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SIBLING_TITLES_QUERY, str(tenant_id), channel, str(piece_id))
        return [r["seo_title"] for r in rows if isinstance(r["seo_title"], str)]
    except Exception as e:  # noqa: BLE001
        logger.warning("t9_sibling_titles_failed", piece_id=str(piece_id), error=str(e)[:200])
        return []


def _repair_feedback(gate_ledger: list[dict], channel: Optional[str] = None) -> list[str]:
    """AA-768 — every failed BLOCKING gate's violations, each prefixed with its gate, for the
    rewrite. Before this only `first_failure` was sent, so a piece failing F1 + F4 + F3 + F9 got
    one gate fixed per attempt and held on the rest (S231 pilot, atlas-hearth blog).
    AA-984 — a blog's over-max F4 violation also carries the size to reach; the target is the blog band's,
    so another channel (max 6,000) never gets it (S248 review)."""
    target = F4_OVER_MAX_TARGET if channel == "blog" else ""
    return [
        f"{g['gate']}: {v}" + (target if g["gate"] == "F4_extreme_length" and _F4_OVER_RE.search(v) else "")
        for g in gate_ledger if not g["passed"] and g.get("blocking", True)
        for v in g["violations"]
    ]


# AA-984 — an over-long blog's rewrite is told the size to reach (the violation alone gave no number; S247 r9
# 32789658 came back at 14,576 chars after its rewrite and was held). Under the trim target, so the rewrite
# lands inside the band with room for the FAQ regeneration.
_F4_OVER_RE = re.compile(r"far beyond")
F4_OVER_MAX_TARGET = (f" — cut the piece to under {length_trim.REWRITE_TARGET_CHARS:,} characters: shorten the "
                      "longest sections; keep every heading, the CTA and the FAQ")


_SIBLING_OPENERS_QUERY = """
    SELECT left(cp.content_text, 400) AS head
    FROM acp_shared.content_piece cp
    JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id
    WHERE cp.tenant_id = $1::uuid AND cp.status IN ('approved', 'held') AND btrim(cp.content_text) <> ''
      AND COALESCE(cp.channel, r.channel) = $4
      AND (cp.angle_gate_request_id = $2::uuid
           OR (r.atom_id = $3 AND cp.created_at > now() - interval '30 days'))
    ORDER BY cp.created_at DESC
    LIMIT 5
"""


async def _sibling_openers(tenant_id: UUID, request_id: UUID, atom_id: Optional[str], channel: str,
                           pool) -> list[str]:
    """AA-771 (S234) — first sentences of this tenant's earlier pieces from the same brief (or the
    same atom in the last 30 days) on the same channel, so the writer does not reuse them (S233:
    Terra 3/3 opened 'There is a checkpoint before you reach the inner temples of Paro Taktsang.').
    An aid: [] on any error, and never for blog (its prompt stays byte-identical)."""
    if channel == "blog":
        return []
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SIBLING_OPENERS_QUERY, str(tenant_id), str(request_id), atom_id, channel)
        openers = [first_sentence(r["head"]) for r in rows if isinstance(r["head"], str)]
    except Exception as e:  # noqa: BLE001
        logger.warning("t9_sibling_openers_failed", request_id=str(request_id), error=str(e)[:200])
        return []
    return list(dict.fromkeys(o for o in openers if o))


async def start_write(
    tenant_id: UUID, request_id: UUID, pool, cta_override: Optional[str] = None,
) -> dict:
    """Fast pre-flight (no LLM call) — everything write_and_check() used to do before its
    write/check loop. Raises RequestNotReadyError / MissingCTAError /
    angle_gate_service.RequestNotFoundError — the router maps each to an HTTP status, UNCHANGED
    from the pre-AA-466 synchronous contract for these specific conditions. On success, inserts
    the `content_piece` placeholder row (status='processing') and returns it — the router
    returns this as the 202 body, then launches run_write_background() with the piece_id."""
    # AA-497 (AA-494 Decision 3) — verified this guard needs NO change. The reopen -> re-choose
    # cycle (services/acp_angle_gate/service.py::reopen_request()/choose_angle()) always lands
    # back on 'approved' before a second write can happen — choose_angle()'s final UPDATE sets
    # 'approved' unconditionally regardless of whether it was called from 'pending_choice' or
    # 'reusable' — so this function never actually sees status='reusable' in practice, confirming
    # the design intent stated in AA-497's own task description.
    req = await angle_gate_service.fetch_request(tenant_id, request_id, pool)
    # AA-986 L3 (ADR 0015 decision 5): no T9 write on a tour AA no longer serves or has paused. TourNotServedError
    # -> 409 tour_discontinued / tour_paused (router), instead of the old 500 when the atom left the live view.
    async with pool.acquire() as conn:
        tour_of = (await _tl.request_tours(conn, tenant_id, [request_id])).get(str(request_id))
        await _tl.ensure_served(conn, tour_of, "write new content")
    if req["status"] != "approved":
        raise RequestNotReadyError(
            f"request_id={request_id} is status={req['status']!r}, expected 'approved' — "
            "T8's angle-choice step (workflow step 7) must be complete before T9 can write."
        )
    if not req["channel"]:
        # AA-522 — channel is now always set at request creation (services.acp_shared.slate.
        # pick_subject(), from the picked Subject) — the old post-angle-choice Channel step
        # (angle_gate_service.set_channel()) is gone along with Luồng B. Defensive in practice:
        # this should be unreachable via the shipped flow — kept as a real error (not a silent
        # fallback) if that invariant is ever violated, same reasoning as the "chosen is None"
        # defensive check just below.
        raise RequestNotReadyError(
            f"request_id={request_id} has no channel set — T8's channel-choice step "
            "(workflow step 8) must be complete before T9 can write."
        )
    chosen = next((a for a in req["angles"] if a["chosen"]), None)
    if chosen is None:
        # Defensive — choose_angle() (T8) always sets exactly one chosen=true before flipping
        # status to 'approved'; this is unreachable via the real API, kept as a real error
        # rather than a silent fallback if that invariant is ever violated.
        raise ContentWritingError(f"request_id={request_id} is approved but has no chosen angle")

    # AA-771 (S233) — the brand pack (T0 fields) is read first: its default_cta (migration 220)
    # is the CTA when the request and the write call carry none.
    brand_audience = await fetch_brand_audience(tenant_id, pool)
    cta = req["cta"] or cta_override or brand_audience.get("default_cta")
    if not cta or not cta.strip():
        raise MissingCTAError(
            f"request_id={request_id} has no CTA (angle_gate_request.cta is NULL — see "
            "migration 114), no cta_override in the write request and no default CTA in the "
            "tenant's brand identity."
        )

    # AA-511 Gap A — a Route/Blog pick carries its whole walk (`route_segment_ids`, migration
    # 134); every other request (Segment pick, or the pre-Slate atom-picker) keeps the original
    # single-atom seed. Only the seed text changes — goal/angle/CTA/brand context below are
    # already request-level, not atom-level, and untouched by this branch.
    #
    # AA-513 — `route_segments` (None for every non-Route request, unchanged legacy behavior)
    # carries each Segment's own (atom_id, text) pair so prompts.py can label them separately and
    # ask the model to tag a fact with the RIGHT Segment's id, not one blanket id for the whole
    # piece. `atom_text` (fed to the quality GATES below, gate_grounding()/gate_banned_patterns())
    # stays the plain joined text either way — deliberately NOT the labeled prompt text, see
    # _fetch_route_segments()'s own docstring for why.
    route_segments: Optional[list[tuple[str, str]]] = None
    route_atoms: list[tuple[str, str]] = []  # S234 — every kept moment, day order (merged blocks hide some)
    # AA-519 Việc 4 — req already carries subject_hub_name (angle_gate_service.fetch_request()'s
    # own LEFT JOIN acp_shared.subject -> acp_contract.route) and route_segment_ids (AA-511 Gap
    # A) — this was the real gap: both were resolved here and then dropped, never written onto
    # content_piece. Set once, immutable across the write/rewrite retry loop below (same as
    # channel/angle_gate_option_id) — NOT recomputed at _finalize_piece() time.
    route_hub_name = req.get("subject_hub_name")
    route_segment_count = None
    if req.get("route_segment_ids"):
        # AA-914 — only the Route Segments the trip has a live atom in (merged ids followed to the
        # live one); a stale snapshot's foreign / orphaned id is dropped and logged, not counted.
        segment_ids = await request_route_segment_ids(pool, req, tenant_id=tenant_id, consumer="t9_write")
        route_segment_count = len(segment_ids)
        route_segments = await _fetch_route_segments(tenant_id, req["trip_id"], segment_ids, pool,
                                                     atoms_out=route_atoms)
        atom_text = "\n\n".join(text for _, text in route_segments)
    else:
        atom_text = await _fetch_atom_text(tenant_id, req["atom_id"], pool)
    scope_atoms = [a for a, _ in (route_atoms or route_segments)] if route_segments else [req["atom_id"]]
    scope = await _scope_line(scope_atoms, pool)
    trip_places = await _trip_places_outside(tenant_id, req.get("trip_id"), scope_atoms, pool)
    goal = get_goal(req["goal"])
    if goal is None:
        raise ContentWritingError(f"request_id={request_id} has an unknown goal={req['goal']!r}")
    channel_style = get_channel_style(req["channel"])
    if channel_style is None:
        raise ContentWritingError(f"request_id={request_id} has an unknown channel={req['channel']!r}")
    # AA-768 — the writer never got the tenant's own name and made one up in the S232 re-run
    # ("Adventure Airlines" for Trail Pulse). The T0 brand name wins; the tenant account name is
    # the fallback when T0 has none.
    if not brand_audience.get("brand_name"):
        async with pool.acquire() as conn:
            brand_name = await conn.fetchval("SELECT name FROM shared.tenants WHERE tenant_id = $1", tenant_id)
        if isinstance(brand_name, str) and brand_name.strip():
            brand_audience = {**brand_audience, "brand_name": brand_name.strip()}

    destination = trip_name = None
    if req["trip_id"]:
        trips = await fetch_tenant_trips(tenant_id, pool)
        trip = next((t for t in trips if str(t.id) == req["trip_id"]), None)
        if trip:
            trip_name = trip.name
            destination = trip.destination

    async with pool.acquire() as conn:
        brand_rubric_text = await fetch_brand_rubric_text(conn, str(tenant_id))

    # AA-497 — angle_gate_option_id (migration 124's Decision 2 column, unpopulated until now):
    # denormalized record of WHICH of the 3 options this specific piece was written from, so a
    # piece's history stays accurate even after a later reopen()+re-choice changes `chosen` on
    # the request. `chosen["option_id"]` is present because fetch_request() (AA-497) now selects
    # it — a request written before this change has no rows to backfill (0 approved requests
    # existed live as of this migration, confirmed via STEP0-refresh), so there's no gap to close.
    option_id = chosen.get("option_id")
    sibling_openers = await _sibling_openers(tenant_id, request_id, req["atom_id"], req["channel"], pool)
    # AA-469 Việc 4 (flow-order fix) — content_piece.channel (migration 124's Decision 2 column,
    # unpopulated until now per that migration's own header) finally has a real value to write:
    # channel is set on the request (angle_gate_service.set_channel(), step 8) before a write can
    # even start (see the guard above), so req["channel"] is always real here.
    piece = await _insert_placeholder_piece(
        pool, tenant_id=tenant_id, request_id=request_id, angle_gate_option_id=option_id,
        channel=req["channel"], route_hub_name=route_hub_name,
        route_segment_count=route_segment_count,
    )

    # AA-514 — the SEO keyword gate_seo_surface() checks title/meta against, reused from T8's OWN
    # already-snapshotted signal (AA-501, migration 127) rather than a new DFS call: the first
    # related keyword T8 saw when generating angles for this exact request. `None` (no snapshot,
    # or an empty related_keywords list) is a real, common case — prompts.py's own
    # `_keyword_line()` already has a no-keyword instruction for it, not a bug to work around.
    dfs_snapshot = req.get("dfs_paa_snapshot") or {}
    related_keywords = dfs_snapshot.get("related_keywords") or []
    # S234 — a keyword naming a place outside the piece's scope ('khovsgol and gobi tour mongolia'
    # on a Khövsgöl-only route) would force that place into the title and meta.
    out_of_scope = hygiene.build_scope_lexicon(atom_text, scope, trip_name, trip_places=tuple(trip_places)).gazetteer
    keyword = choose_keyword(related_keywords, atom_text, out_of_scope=out_of_scope)
    # AA-779 — the snapshot's People Also Ask questions (per TOUR, not scoped). The t9_write job
    # filters them to this piece (paa_for_scope, once the Facts are known) for the blog FAQ.
    # S245 (AA-779 item 2A) — a blog reads real questions of its own Segments and places first
    # (services/acp_shared/real_questions.py, $0 DB reads), the tour's last; `real_questions` keeps
    # each one's source for the admin record (writer_input), never for the tenant.
    tour_paa = snapshot_tour_paa(dfs_snapshot)
    real_questions: list[dict] = []
    a3_stats: Optional[dict] = None
    if req["channel"] == "blog":
        # AA-982 — drop the place_paa questions A3 Jev already rejected for this piece (stored verdicts,
        # no new gate, no Jev call); kill switch T9_A3_PAA_FILTER=off. The counts go to the log and
        # the admin record (writer_input.a3_question_filter).
        a3_on = a3_filter_enabled()
        a3_stats = {} if a3_on else None
        chain = await collect_real_questions(pool, atom_ids=scope_atoms, tour_paa=tour_paa[:PAA_RAW_MAX],
                                             a3_filter=a3_on, stats=a3_stats)
        real_questions = [r.as_dict() for r in chain]
        people_also_ask = [r.question for r in chain]
        if a3_stats is not None:
            a3_stats["place_paa_kept"] = sum(1 for r in chain if r.source == SOURCE_PLACE)
            logger.info("t9_place_paa_a3_filter", piece_id=str(piece["piece_id"]), request_id=str(request_id),
                        dropped=sum((a3_stats.get("a3_dropped") or {}).values()), **a3_stats)
    else:
        people_also_ask = tour_paa[:PAA_RAW_MAX]

    # AA-529 — Facts Entry source: ALL scope='platform' facts + this tenant's own scope='tenant'
    # facts, never another tenant's (fetch_facts_for_writing()'s own WHERE clause enforces that).
    # Fetched once per start_write() call, formatted into a labeled block, and carried in
    # `context` for run_write_background()/_run_buffer_retry_attempt() to pass into every
    # write/rewrite call AND fold into the grounding-gate's own `atom_text` argument — see this
    # module's own AA-529 comments below for why it's merged there rather than threaded as a
    # wholly separate pipeline.
    # AA-764 — Facts are fetched in the t9_write job (resolve_facts(), called first thing in
    # run_write_background()), not here: this is still the HTTP request.

    context = {
        "atom_text": atom_text, "goal": goal, "channel_style": channel_style,
        "brand_audience": brand_audience, "chosen": chosen, "cta": cta,
        "destination": destination, "trip_name": trip_name,
        "brand_rubric_text": brand_rubric_text, "channel": req["channel"],
        "atom_id": req["atom_id"], "route_segments": route_segments, "keyword": keyword,
        "people_also_ask": people_also_ask,  # AA-779 — raw; the job keeps the in-scope ones
        "real_questions": real_questions,  # S245 (AA-779 2A) — [{q, source}], blog only; admin record
        **({"a3_question_filter": a3_stats} if a3_stats is not None else {}),  # AA-982 — admin record
        "scope": scope,  # S233
        "trip_places": trip_places,  # S234 round 3 — the full trip's places outside the scope
        "tenant_id": str(tenant_id),  # AA-505 — attribution for llm_call_log
        "facts_text": None,  # AA-529 — filled by resolve_facts() in the job ("" when none exist)
        "facts_pending": True,  # AA-764
        # AA-485 — route_hub_name/route_segment_count weren't previously carried in `context`
        # (only used inline, at the placeholder INSERT above) because nothing needed them again
        # afterward. The buffer-retry path (run_write_background()'s own trailing step) inserts
        # a SECOND placeholder piece for the same request and needs the same values — added here
        # rather than re-fetched, since `context` already has everything else that piece needs.
        "route_hub_name": route_hub_name, "route_segment_count": route_segment_count,
        "route_atoms": route_atoms,  # S234 — resolve_facts() re-reads the merged route seeds from it
        "sibling_openers": sibling_openers,  # S234 (AA-771) — non-blog channels only
    }
    return {"piece": piece, "context": context}


def snapshot_tour_paa(snapshot: Optional[dict]) -> list[str]:
    """S245 (AA-779) — the tour-level People Also Ask of a request snapshot. A blog snapshot written
    since S245 holds the angle prompt's merged list in `people_also_ask` and the tour's own in
    `tour_people_also_ask`; an older snapshot (or a non-blog one) has only `people_also_ask`."""
    snapshot = snapshot or {}
    questions = snapshot.get("tour_people_also_ask", snapshot.get("people_also_ask")) or []
    return [q for q in questions if isinstance(q, str)]


async def _insert_placeholder_piece(
    pool, *, tenant_id: UUID, request_id: UUID, angle_gate_option_id=None, channel=None,
    route_hub_name: Optional[str] = None, route_segment_count: Optional[int] = None,
) -> dict:
    # AA-497 (migration 125) — attempt_number=1 is still correct as the INITIAL value for every
    # new write session (T9's own internal retry loop, run_write_background(), overwrites it via
    # _finalize_piece() with the final 1-or-2 it actually took) — this is no longer required to
    # be unique per angle_gate_request_id (migration 125 dropped that constraint), since a
    # reopened request can now have more than one content_piece row over time.
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO acp_shared.content_piece
                (tenant_id, angle_gate_request_id, angle_gate_option_id, channel, attempt_number,
                 content_text, status, route_hub_name, route_segment_count)
            VALUES ($1, $2, $3, $4, 1, '', 'processing', $5, $6)
            RETURNING piece_id, tenant_id, angle_gate_request_id, attempt_number, content_text,
                      status, held_reason, gate_ledger, repair_log, created_at,
                      route_hub_name, route_segment_count
            """,
            tenant_id, request_id, angle_gate_option_id, channel,
            route_hub_name, route_segment_count,
        )
        # AA-554 mục H.1 — a content_piece now genuinely exists for this request: mark the Slate
        # proposal it came from (if any) 'used'. Fires from EVERY call site of this function
        # (the primary write in start_write() AND AA-485's buffer retry, _maybe_buffer_retry_
        # held_piece()) — both are real evidence this proposal "became content", not just the
        # first attempt. Only transitions FROM 'picked' (pick_subject()'s own state,
        # services/acp_shared/slate.py) so a future 'cut' (mục H.2, backend-only for now) is never
        # silently overwritten by a stray write; already-'used' stays 'used' (idempotent, matches
        # the buffer retry inserting a second content_piece for the same request_id).
        # angle_gate_request.subject_id is NULL for the pre-Slate atom-picker path (migration
        # 133) — the subquery then matches nothing, a safe no-op, not a special case to guard.
        await conn.execute(
            """
            UPDATE acp_shared.subject SET state = 'used'
            WHERE state = 'picked'
              AND subject_id = (
                  SELECT subject_id FROM acp_shared.angle_gate_request WHERE request_id = $1::uuid
              )
            """,
            request_id,
        )

        # AA-559 — semantic tenant-activity log: this content_piece row now genuinely exists
        # (status='processing') — NOT "content finished", the actual LLM write/gate loop hasn't
        # run yet (that completion is logged separately, _finalize_piece()'s own
        # CONTENT_PIECE_FINISHED below). Same connection/transaction as the INSERT+UPDATE above.
        await write_audit_log(
            conn, tenant_id=str(tenant_id), actor=f"tenant:{tenant_id}",
            action=TenantAuditAction.CONTENT_PIECE_CREATED, resource_type="content_piece",
            resource_id=str(row["piece_id"]),
            details={"request_id": str(request_id), "channel": channel},
        )
    return _row_to_dict(row)


async def _reread_seed(context: dict, tenant_id: UUID, pool) -> None:
    """Re-read each moment's seed so it carries any place card built since start_write (T8 or the
    place_cards_build job). A context with no atom (hand-built) keeps its seed."""
    route_segments = context.get("route_segments")
    if not route_segments and not context.get("atom_id"):
        return
    async with pool.acquire() as conn:
        if route_segments:
            # S234 (AA-768) — re-merge from every kept moment (a merged block shows only its first
            # atom id), so a card built since start_write lands in the merged block too.
            route_atoms = [(a, t) for a, t in (context.get("route_atoms") or route_segments)]
            fresh = await route_moment_seeds(conn, route_atoms, str(tenant_id)) or route_segments
            context["route_segments"] = fresh
            context["atom_text"] = "\n\n".join(text for _, text in fresh)
        else:
            context["atom_text"] = await atom_seed_text(conn, context["atom_id"], str(tenant_id)) \
                or context["atom_text"]


async def resolve_facts(request_id: UUID, context: dict, pool) -> None:
    """AA-764 (G5) — the T9 source step, run in the t9_write job (not the HTTP request). Fills
    `context` in place, once (the buffer retry reuses the same context); a context without
    `facts_pending` (built before AA-764, or by a test) is left untouched.

    S233: (1) the seed is re-read so it carries the place cards (AA-772) of the piece's atoms —
    S234 (plan C8): T9 only READS cards; it never drafts one (S233: 36 of 169 card drafts, one
    unnamed atom 35 times, came from this step). Cards are built by T8 for the request atom and
    by the place_cards_build job; (2) Facts are the platform + tenant facts for the trip's country
    (facts.country), with no per-fact Jev call (S232: no Jev on the T axis)."""
    if not context.get("facts_pending"):
        return
    tenant_id = UUID(context["tenant_id"])
    try:
        await _reread_seed(context, tenant_id, pool)
    except Exception as e:  # noqa: BLE001 — the seed built at start_write still works
        logger.warning("t9_seed_reread_failed", request_id=str(request_id), error=str(e)[:200])
    facts = await fetch_facts_for_writing(tenant_id, pool, country=context.get("destination"))
    context["facts_text"] = format_facts_block(facts)
    context["facts_pending"] = False


def _add_date_spans(context: dict) -> None:
    """S234 round 3 (r2 §4 fix 5) — when a seed dates a role as ended ('served as the capital and seat
    of government of Bhutan until 1955') and gives its start, the writer and the gates get the span
    as a stated fact line (trip_facts.with_date_spans): r2 #12 wrote 'for nearly three centuries' for
    1637–1955. Idempotent; route segments get the same line in their own block."""
    if context.get("atom_text"):
        context["atom_text"] = trip_facts.with_date_spans(context["atom_text"])
    if context.get("route_segments"):
        context["route_segments"] = [(a, trip_facts.with_date_spans(t)) for a, t in context["route_segments"]]


async def run_write_background(request_id: UUID, piece_id: UUID, context: dict, pool) -> None:
    """The write/rewrite + T10-check loop — UNCHANGED body from the pre-AA-466 single-function
    write_and_check(), just reading its inputs from `context` (built by start_write()) instead
    of local variables, and calling `_finalize_piece()` (an UPDATE by piece_id) instead of
    `_persist_piece()` (an INSERT). Launched via `asyncio.create_task()` + strong-ref (see
    api/routers/v1_content_writing.py) — same GC-safety pattern api/routers/v1_tours.py's
    `trigger_rewrite()` already established (AA-425), not the bare `asyncio.create_task()`
    v1_s4_blog.py uses. Any uncaught exception here means the background task itself failed
    (Bedrock throttle, network error, anything) BEFORE producing real content — written back as
    status='failed', distinct from status='held' (a real, complete, gate-blocked outcome with
    real content_text) — see migration 118's header for why these must not be conflated."""
    await resolve_facts(request_id, context, pool)  # AA-764 — seed re-read + Facts, in the job
    _add_date_spans(context)  # S234 round 3 — 'served … until 1955' → the span as a stated fact
    atom_text, goal, channel_style = context["atom_text"], context["goal"], context["channel_style"]
    brand_audience, chosen, cta = context["brand_audience"], context["chosen"], context["cta"]
    destination, trip_name = context["destination"], context["trip_name"]
    brand_rubric_text, channel, atom_id = context["brand_rubric_text"], context["channel"], context["atom_id"]
    # AA-513 — None for every non-Route request (dict.get, not context["route_segments"]:
    # backward-compatible with any pre-AA-513 caller/test that builds a context dict by hand
    # without this key).
    route_segments = context.get("route_segments")
    # AA-514 — same dict.get() backward-compatibility as route_segments above.
    keyword = context.get("keyword")
    # AA-505 — same dict.get() backward-compatibility (None for any pre-AA-505 test that builds
    # a context dict by hand without this key).
    tenant_id = context.get("tenant_id")
    # AA-529 — same dict.get() backward-compatibility; "" (falsy) for any pre-AA-529 test/caller.
    facts_text = context.get("facts_text") or ""
    # AA-529 — the grounding-gate's OWN view of the source text: atom_text (or the joined route
    # segments) PLUS the Facts block, so a claim genuinely sourced from a Fact (e.g. a price) is
    # no longer flagged as an unsupported number by gate_grounding()/gate_banned_patterns().
    # Deliberately NOT what's passed as `content_seed=` to
    # write_content()/rewrite_with_feedback() below (those get `facts_text=facts_text` as its own
    # argument instead, appended once inside build_user_prompt()) — this avoids the Facts block
    # appearing twice in what the model actually reads.
    spec = channel_spec(channel, brand_audience)
    hyg = hygiene.HygieneContext(seed=atom_text, scope=context.get("scope"), trip_name=trip_name,
                                 brand_name=(brand_audience or {}).get("brand_name"), cta=cta,
                                 hashtags_max=spec.hashtags_max if spec else None,
                                 first_line_max=spec.first_line_max if spec else None,
                                 audience=(brand_audience or {}).get("customer_segment"), facts=facts_text,
                                 trip_places=tuple(context.get("trip_places") or ()))
    # S234 round 3 — trip facts / rarity (F17/F18) read the moment's own seed lines + Facts; the
    # brand pack and the chosen angle are the brief F15 checks for pasted phrases.
    trip_ctx = trip_facts.TripFactContext(
        seed=atom_text, source=f"{hyg.lexicon.own_text}\n{facts_text}",
        audience=(brand_audience or {}).get("customer_segment"), cta=cta,
        trip_places=tuple(context.get("trip_places") or ()),
        channel=channel, brand_name=(brand_audience or {}).get("brand_name"))  # AA-775 — pace_promise
    briefs = hygiene.brief_texts(brand_audience, chosen)
    # S234 round 2 (panel §5 fix 4) — a part-of-trip piece gets the trip name without its duration
    # and its out-of-scope places ('Mongolia: Khövsgöl Lake and Gobi Desert — 15 Days' → 'Mongolia:
    # Khövsgöl Lake'); the hygiene context keeps the full name (its gazetteer needs those places).
    writer_trip = hygiene.scoped_trip_name(trip_name, context.get("scope"), hyg.lexicon)
    context["writer_trip_name"] = writer_trip
    grounding_text = _grounding_text(context)  # AA-768 — + audience/trip lines
    # S234 — scope lexicon + text rules for this piece (pure; rebuilt per job, never stored in the
    # JSON job payload).
    # AA-771 (S234) — the channel spec (LinkedIn) caps the hashtags, moves them to the last line and
    # splits an over-long first line, in hygiene.autofix after the last LLM step of each attempt.
    sibling_openers = context.get("sibling_openers") or []
    openers_kw = {"sibling_openers": sibling_openers} if sibling_openers else {}
    # AA-779 — the blog's in-scope People Also Ask questions: the writer, the rewrite and the FAQ
    # regeneration (_enforce_faq_contract reads context["paa_questions"]) prefer one a seed fact answers.
    # S247 (AA-779, ADR 0011) — behind T9_FAQ_LLM_FILTER (default off) an LLM step on stage t9_faq_filter
    # chooses them instead (or, in shadow mode, only records its verdicts); any failure keeps paa_for_scope.
    paa_questions: list[str] = []
    if channel == "blog":
        paa_questions, faq_filter_record = await faq_filter.select_faq_questions(
            context.get("people_also_ask"), own_seed=hyg.lexicon.own_text, facts_text=facts_text,
            out_of_scope=hyg.lexicon.gazetteer, context=context, piece_id=piece_id, request_id=request_id)
        if faq_filter_record:
            context["faq_filter"] = faq_filter_record
    context["paa_questions"] = paa_questions
    # S245 (AA-779 2A) — the source of each question the writer got (admin-only, writer_input).
    context["search_questions"] = (label_questions(paa_questions, context.get("real_questions") or [])
                                   if channel == "blog" else [])
    search_kw = {"search_questions": paa_questions} if paa_questions else {}
    brand_ignore = hygiene.brand_words((brand_audience or {}).get("brand_name"))
    sibling_texts = await _sibling_texts(context.get("tenant_id"), request_id, piece_id, pool)
    # S234 round 4 — only the blog channel has a search title (F4 and the publish path are blog-only)
    taken_titles = await _sibling_titles(context.get("tenant_id"), channel, piece_id, pool) if channel == "blog" else []

    attempt = 1  # bound before the try block so the except handler always has a real value
    try:
        total_cost = 0.0
        content_text: str = ""
        gate_ledger: list[dict] = []
        repair_log: list[dict] = []
        # AA-519 Việc 5 — non-blocking (warn) gate failures (ADR 0023 flag-not-block), from whichever
        # attempt actually shipped. Same "final attempt wins"
        # convention content_text/gate_ledger/seo_meta already follow.
        flags: list[dict] = []
        # AA-498 (Decision 4) — same "final attempt wins" convention as seo_meta/content_text:
        # whichever attempt actually gets persisted is the one whose summary is kept. `None` is
        # a normal, soft-fail outcome (model didn't produce one) — never blocks persistence.
        summary: str | None = None
        status = "held"
        held_reason = "unreachable"  # overwritten every branch below; kept non-None for mypy/clarity
        # AA-514 — the LATEST attempt's own seo_meta is what gets persisted, same "final attempt
        # wins" precedent content_text/gate_ledger already follow — {} (all-None) for every
        # non-blog channel, see generate.py's own write_content()/rewrite_with_feedback() docstring.
        seo_meta: dict = {"seo_title": None, "meta_description": None, "slug": None}
        # AA-499/AA-484 — this attempt's own embedding, computed inside the loop (not just after
        # it) because AA-484's cannibalization gate needs it BEFORE run_quality_gates() runs, on
        # EVERY attempt (a real blocking gate has to be re-checked on the retry, same as every
        # other gate). Kept across the loop and reused (not recomputed) by the post-loop
        # within-tenant reuse check below — one embedding call per attempt, not two.
        embedding: list[float] | None = None
        # AA-528 — snapshot of the immediately-preceding attempt's own finalized outcome, taken
        # right before that attempt gets overwritten by a rewrite. Only used by the gate-
        # regression-guard below; stays None on attempt 1 (nothing to compare against yet).
        prev_snapshot: dict | None = None
        # AA-570 — whichever attempt did NOT become the final content_text (at most one, since
        # MAX_ATTEMPTS=2), captured for the Data Flywheel/lesson-log use case. Set either inside
        # the gate-regression-guard block below (the just-generated attempt 2 that got reverted
        # AWAY from) or, in the post-loop fallback, from `prev_snapshot` (attempt 1, discarded in
        # favor of whatever attempt 2 produced) — see that fallback's own comment for why both
        # paths are needed. Stays None whenever only 1 attempt ever ran.
        discarded_attempt: dict | None = None
        faq_repair_regen = False  # S234 r2 review — the one FAQ regeneration after a sentence repair
        # AA-777 — the job's F9 sticky-pass memory: the body sentences (folded) an earlier F9 verdict of
        # this piece passed; run_quality_gates drops a later re-flag of one of them while it is unchanged.
        # AA-865 — an F9PassMemory also keeps the last judged text, so a pass survives a deletion-only repair.
        f9_passed: set[str] = F9PassMemory()

        for attempt in range(1, MAX_ATTEMPTS + 1):
            if attempt == 1:
                content_text, cost, seo_meta, summary = await asyncio.to_thread(
                    write_content, content_seed=atom_text, goal=goal, channel_style=channel_style,
                    brand_audience=brand_audience, angle=chosen, cta=cta,
                    destination=destination, trip_name=writer_trip, atom_id=atom_id, scope=context.get("scope"),
                    route_segments=route_segments, keyword=keyword,
                    tenant_id=tenant_id, angle_gate_request_id=str(request_id),
                    facts_text=facts_text, **openers_kw, **search_kw,
                )
                fresh_text = True
            else:
                fresh_text = False
                # AA-528 — capture attempt (attempt-1)'s outcome before rewrite_with_feedback()
                # overwrites content_text/gate_ledger/etc. below. This is what a detected
                # regression gets reverted to.
                prev_snapshot = {
                    "content_text": content_text, "gate_ledger": gate_ledger, "seo_meta": seo_meta,
                    "summary": summary, "embedding": embedding,
                }
                # AA-768 — when attempt 1 failed only on quoted invented facts, fix just those
                # sentences (sentence_repair.py) so the rewrite cannot invent new ones; anything
                # else, or a failed repair call, takes the full rewrite as before.
                repair_items = sentence_repair.plan(content_text, gate_ledger)
                # AA-777 — blog, attempt 3: an F1/F9 truth item still failing after one sentence repair
                # is cut in code (never under the blog minimum) instead of a second repair call.
                truth_cut, truth_cut_done = None, False
                cut_repaired = _cut_gates_repaired(repair_log) if channel == "blog" else frozenset()
                if (channel == "blog" and attempt >= BLOG_TRUTH_CUT_ATTEMPT and repair_items and cut_repaired):
                    cut_text, repair_items, truth_cut = _cut_blog_truth_items(
                        content_text, repair_items, ignore=brand_ignore, cta=cta, moments=hyg.moments,
                        repaired=cut_repaired)
                    repair_log[-1].update(truth_cut or {})
                    if truth_cut and truth_cut["repair_mode"] == "truth_cut":
                        truth_cut_done = True
                        content_text = cut_text
                        _show_live_text(content_text, seo_meta, channel)
                        logger.info("t9_blog_truth_cut", request_id=str(request_id), attempt=attempt,
                                    sentences_cut=truth_cut["sentences_cut"], left_to_repair=len(repair_items))
                    elif truth_cut:
                        logger.info("t9_blog_truth_cut_refused", request_id=str(request_id), attempt=attempt,
                                    reason=truth_cut["truth_cut_refused"])
                cut_only = truth_cut_done and not repair_items
                replacements = None
                if repair_items:
                    # S234 — the ±2 neighbouring sentences as context, and only the cited source.
                    for item in repair_items:
                        item["context"] = sentence_repair.neighbours(content_text, item)
                    # S234 round 2 — a style-only repair (F15_voice) runs on the cheap t9_repair stage.
                    stage_kw = {"stage": SEO_FIT_STAGE} if _style_only_repair(gate_ledger) else {}
                    progress_step("revise")  # AA-668: the repair call no longer reports to the live view
                    try:
                        replacements, cost = await asyncio.to_thread(
                            repair_sentences, sentences=repair_items,
                            content_seed=_repair_source(content_text, repair_items, context),
                            facts_text=None, channel=channel, tenant_id=tenant_id,
                            angle_gate_request_id=str(request_id), **stage_kw,
                        )
                    except SentenceRepairError as e:
                        logger.warning("t9_sentence_repair_failed", request_id=str(request_id), error=str(e)[:200])
                if cut_only:
                    # AA-777 — every item left was a truth item and all were cut: no repair call; the
                    # gates below judge the cut text (repair_log[-1] already carries the truth_cut entry).
                    cost = 0.0
                elif repair_items == []:
                    # S234 review — only a title/meta scope violation failed: the SEO fit below
                    # owns it; the text stays as it is (no rewrite call).
                    cost = 0.0
                    repair_log[-1]["repair_mode"] = "seo_fields"
                elif replacements is not None:
                    # S234 round 2 — splice_checked reverts any repair that leaves a new repeat or a
                    # pronoun with no referent (panel §5 fix 7: #19, #10) to its pre-repair sentence.
                    # AA-777 (r6 fix 1) — and a replacement that adds a number / time / pace word the sentence
                    # and the source lack keeps its old sentence (`trip_ctx` + the F1 grounding text).
                    content_text = sentence_repair.splice_checked(content_text, repair_items, replacements,
                                                                  ignore=brand_ignore, cta=cta, moments=hyg.moments,
                                                                  trip_ctx=trip_ctx, source=grounding_text)
                    _show_live_text(content_text, seo_meta, channel)  # AA-668: the spliced text, not the patch
                    # AA-777 — a truth cut followed by a repair of the other items keeps both in the log
                    repair_log[-1]["repair_mode"] = "truth_cut+sentences" if truth_cut_done else "sentences"
                    repair_log[-1]["sentences_repaired"] = len(repair_items)
                    # S234 — replacements turned into deletes because they restated a neighbour.
                    guarded = sum(item.get("guard") == "repeat" for item in repair_items)
                    if guarded:
                        repair_log[-1]["repeat_guard_deletes"] = guarded
                    kept = sum(item.get("guard") == "repeat_kept" for item in repair_items)
                    if kept:
                        repair_log[-1]["repeat_guard_kept"] = kept
                    hazard_kept = sum(item.get("guard") == "hazard_kept" for item in repair_items)
                    if hazard_kept:  # AA-777 r7 review — a repair that cut a hazard sentence was not spliced
                        repair_log[-1]["hazard_kept"] = hazard_kept
                    deduped = sum(item.get("guard") == "dedupe" for item in repair_items)
                    if deduped:
                        repair_log[-1]["dedupe_after_repair"] = True
                    specifics = [item for item in repair_items if item.get("guard") == "specifics"]
                    if specifics:
                        repair_log[-1]["specifics_reverts"] = len(specifics)
                        repair_log[-1]["specifics_problems"] = [p for it in specifics for p in it["specifics"]][:5]
                    reverted = [item for item in repair_items if item.get("guard") == "integrity"]
                    if reverted:
                        repair_log[-1]["integrity_reverts"] = len(reverted)
                        repair_log[-1]["integrity_problems"] = [p for it in reverted for p in it["integrity"]][:5]
                    # AA-925 repair contract (truncation / balance) — reverted sentences, counted as degraded
                    contract = [item for item in repair_items if item.get("guard") == "contract"]
                    if contract:
                        repair_log[-1]["contract_reverts"] = len(contract)
                        repair_log[-1]["contract_problems"] = [p for it in contract for p in it["contract"]][:5]
                elif attempt >= 3 and repair_items and (cut := sentence_repair.delete_flagged(
                        content_text, repair_items, ignore=brand_ignore, cta=cta, moments=hyg.moments)) is not None \
                        and (channel != "blog" or _blog_cut_refusal(content_text, cut) is None):
                    # S234 round 3 (r2 §4 fix 7) — the attempt-3 repair call failed: cut the flagged
                    # sentence(s) in code rather than hold a piece on one sentence (r2 #7, #13).
                    content_text, cost = cut, 0.0
                    repair_log[-1]["repair_mode"] = "truth_cut+sentences_cut" if truth_cut_done else "sentences_cut"
                    repair_log[-1]["sentences_repaired"] = len(repair_items)
                elif truth_cut_done:
                    # AA-777 — the truth items were cut but the repair of the other items failed: keep the
                    # cut text (never back to attempt 2's flagged sentence); the gates below report the rest.
                    cost = 0.0
                elif attempt >= 3:
                    # AA-770 — attempt 3 never does a full rewrite; keep attempt 2 as it is.
                    content_text, gate_ledger = prev_snapshot["content_text"], prev_snapshot["gate_ledger"]
                    seo_meta, summary = prev_snapshot["seo_meta"], prev_snapshot["summary"]
                    embedding = prev_snapshot["embedding"]
                    # AA-786 — the last attempt: LinkedIn cuts the flagged F9 sentences (review P2).
                    content_text, gate_ledger, flags, cut_entry = _cut_truth_items(
                        content_text, gate_ledger, flags, channel=channel, ignore=brand_ignore, cta=cta,
                        moments=hyg.moments, spec=spec)
                    if cut_entry:
                        repair_log.append({"attempt": attempt - 1, **cut_entry})
                        if cut_entry["sentences_cut"]:
                            summary, embedding = await _after_cut(content_text, tenant_id, embedding)
                    status, held_reason, flags = _hold_or_ship(gate_ledger, flags)
                    prev_snapshot = None  # nothing was discarded by this attempt
                    attempt -= 1
                    break
                else:
                    content_text, cost, seo_meta, summary = await asyncio.to_thread(
                        rewrite_with_feedback, content_seed=atom_text, goal=goal, channel_style=channel_style,
                        brand_audience=brand_audience, angle=chosen, cta=cta,
                        revision_feedback=repair_log[-1].get("feedback") or repair_log[-1]["violations"],
                        destination=destination, trip_name=writer_trip, atom_id=atom_id, scope=context.get("scope"),
                        route_segments=route_segments, keyword=keyword,
                        tenant_id=tenant_id, angle_gate_request_id=str(request_id),
                        facts_text=facts_text, **openers_kw, **search_kw,
                    )
                    fresh_text = True
            total_cost += cost
            if channel == "blog":
                # S234 round 4 — the search title/meta exist for the blog only: no publish path, gate or
                # tenant view reads them on another channel, and the fit made 1–2 t9_repair calls per
                # attempt on every LinkedIn post (r3 #6/#10 stored a partial or empty pair from it).
                seo_meta, seo_cost = await _fit_seo_meta(seo_meta, keyword, body=content_text, context=context,
                                                         request_id=request_id, hyg=hyg, taken_titles=taken_titles)
                total_cost += seo_cost

            # AA-484 — cross-tenant cannibalization check, done HERE (before run_quality_gates(),
            # which stays synchronous/pool-free — see quality_gates.gate_cannibalization()'s own
            # docstring) so it's a real re-checked-every-attempt BLOCKING gate, not a post-hoc
            # note. tenant_id guard mirrors AA-499's own (real in production, only a hand-built
            # test context could omit it). Soft-fail: a failed/empty embedding just means no
            # cannibalization_match — never itself a reason to hold.
            embedding = None
            cannibalization_match: dict | None = None
            if tenant_id:
                embed_text = strip_citation_tags(content_text)
                embedding = await asyncio.to_thread(compute_embedding, embed_text)
                if embedding is not None:
                    cross_matches = await find_similar_pieces(
                        embedding, pool, cross_tenant=True,
                        exclude_tenant_id=UUID(tenant_id), limit=1,
                    )
                    if cross_matches and cross_matches[0].similarity >= _REUSE_SIMILARITY_THRESHOLD:
                        top = cross_matches[0]
                        # AA-484's own issue text (STEP0, citing AA-425's real finding): the
                        # highest-risk group for cannibalization is a tenant with NO active
                        # tenant_brand_rules (brand_rules={} fallback -> generic LLM drift ->
                        # higher chance of converging on similar wording). One extra query, only
                        # on the rare path where a match already fired — surfaced as a diagnostic
                        # hint in the held reason, not a separate gate.
                        cannibalization_match = {
                            "piece_id": top.piece_id, "tenant_id": top.tenant_id,
                            "similarity": top.similarity,
                            "writer_missing_brand_rules": await _tenant_missing_brand_rules(tenant_id, pool),
                        }

            faq_dropped, faq_unanswerable = 0, []
            # S234 — deterministic fixes after the last LLM step of this attempt (write/rewrite,
            # sentence splice and SEO fit all ran above), before the gates read the text.
            content_text, hygiene_log = hygiene.autofix(content_text, hyg)
            # S234 round 3 (r2 §4 fix 9) — the configured CTA line, word for word, after the last edit
            content_text, cta_log = hygiene.ensure_cta(content_text, cta)
            hygiene_log += cta_log
            # S234 round 3 review — a family brand's missing access caveat goes in from the seed, in
            # code (no repair call that the repeat guard could turn into a cut of the anchor sentence)
            content_text, caveat_log = trip_facts.add_family_caveats(content_text, trip_ctx, brand_ignore)
            hygiene_log += caveat_log
            if channel == "blog":  # AA-770 — F7 auto-fix instead of a hold/rewrite
                if fresh_text:  # S245 r7 review — the writer's own FAQ, before any FAQ step
                    context["faq_pairs_before"] = faq_pair_stats(content_text, context)
                content_text, faq_dropped = drop_restated_faq(content_text)
                own_seed = hygiene.split_seed(atom_text)[0]
                content_text, faq_unanswerable = drop_unanswerable_faq(content_text, own_seed, facts_text)
                # AA-779 — an answer that does not name its question's subject (or only says what is not
                # the case) goes while more than FAQ_MIN_PAIRS remain; the ones kept at the minimum make
                # this writer text's one FAQ regeneration rewrite them (never a hold of its own).
                content_text, subject_log, subject_short = drop_subjectless_faq(
                    content_text, f"{own_seed}\n{facts_text}")
                faq_unanswerable = list(faq_unanswerable) + subject_log
                # S234 round 2 — FAQ contract: one regeneration per writer text. S234 r2 review: a
                # sentence repair can cut a FAQ pair (a repeat-guard delete, an emptied answer, an
                # answer that now copies the body) and F16_faq is not repairable, so a repair that took
                # a full FAQ below FAQ_MIN_PAIRS gets one more regeneration per request.
                faq_fell = (not fresh_text and not faq_repair_regen and prev_snapshot is not None
                            and faq_pair_count(content_text) < FAQ_MIN_PAIRS
                            <= faq_pair_count(prev_snapshot["content_text"]))
                if faq_fell:
                    faq_repair_regen = True
                # S245 r7 — the real search questions the writer's FAQ does not ask: the same one
                # regeneration per writer text answers them first.
                missing_real = (missing_real_questions([q for q, _ in faq_pairs(content_text)],
                                                       context.get("search_questions") or [])
                                if fresh_text else [])
                if fresh_text or faq_fell:
                    before_regen = content_text
                    content_text, faq_cost, faq_regen = await _enforce_faq_contract(
                        content_text, context=context, hyg=hyg, request_id=request_id, brand_ignore=brand_ignore,
                        fix_subjects=fresh_text and subject_short > 0, required=missing_real)
                    total_cost += faq_cost
                    if content_text != before_regen:
                        # the new FAQ replaced the kept non-answers: report what the new one still has
                        content_text, again_log, _ = drop_subjectless_faq(content_text, f"{own_seed}\n{facts_text}")
                        faq_unanswerable = [x for x in faq_unanswerable if not x.startswith("kept FAQ pair")] \
                            + again_log
                    faq_unanswerable = list(faq_unanswerable) + faq_regen
                content_text, real_first_log = real_first_faq(content_text, context)
                faq_unanswerable = list(faq_unanswerable) + real_first_log
            first_line_log: list[str] = []
            if spec is not None:  # S234 round 2 — hook line still too long after the deterministic split
                content_text, fl_cost, first_line_log = await _fix_first_line(
                    content_text, spec=spec, context=context, hyg=hyg, request_id=request_id,
                    brand_ignore=brand_ignore)
                total_cost += fl_cost
            # S234 round 4 (r3 panel §4 fixes 1–4, 8) — after the last edit of this attempt (FAQ
            # regeneration and the first-line rewrite included): the deterministic trip-fact fixes
            # (thin-moment heading / coda, a hazard played down, a background place made the base),
            # then the final text-integrity pass (one CTA, no duplicate, no fragment, normalised
            # whitespace). What it cannot fix is F13_repeat in the gates below.
            content_text, tf_log = trip_facts.fix_trip_facts(content_text, trip_ctx)
            content_text, final_log, _ = text_integrity.final_pass(content_text, hyg)
            # AA-984 — an over-long blog is trimmed into its band here (whole low-value paragraphs, never a
            # heading, the FAQ or the CTA), so it is never rewritten or held for length alone (S247 r9 32789658).
            content_text, trim_log = length_trim.trim_to_band(content_text, channel=channel, cta=cta,
                                                              ignore=brand_ignore, moments=hyg.moments,
                                                              trip_ctx=trip_ctx)
            first_line_log = first_line_log + tf_log + final_log + trim_log
            progress_step("check")  # AA-637 live view (no-op without a bound tracker)
            outcome = await asyncio.to_thread(
                run_quality_gates, content_text=content_text, atom_text=grounding_text, cta=cta,
                brand_rubric_text=brand_rubric_text, channel=channel, keyword=keyword,
                seo_title=seo_meta.get("seo_title"), meta_description=seo_meta.get("meta_description"),
                slug=seo_meta.get("slug"), cannibalization_match=cannibalization_match,
                brand_pack=brand_audience, hygiene_ctx=hyg, **openers_kw,
                # S234 review — a stock phrase blocks (one sentence repair) on attempt 1 only.
                stock_blocking=attempt == 1,
                sibling_texts=sibling_texts,  # S234 round 2 — F15 sibling templates
                trip_ctx=trip_ctx, brief_texts=briefs,  # S234 round 3 — F17/F18, brief phrases in F15
                taken_titles=taken_titles,  # S234 round 4 — F4: a title this brand already used
                f9_passed_sentences=f9_passed,  # AA-777 — sticky pass, one set per job
            )
            gate_ledger = outcome["gate_ledger"]
            flags = outcome["flags"]
            faq_log = ([f"{faq_dropped} FAQ pair(s) that copied a body sentence were removed"] if faq_dropped else []) \
                + faq_unanswerable
            if faq_log:
                flags.append({"gate": "F7_faq_autofix", "passed": False, "blocking": False, "repairable": False,
                              "violations": faq_log})
            if hygiene_log or first_line_log:  # S234 — every in-place edit, shown in the admin drawer
                flags.append({"gate": "F13_autofix", "passed": False, "blocking": False, "repairable": False,
                              "violations": hygiene_log + first_line_log})

            if outcome["passed"]:
                status, held_reason = "approved", None
                break

            first_failure = outcome["first_failure"]
            repair_log.append({
                "attempt": attempt, "gate_targeted": first_failure["gate"],
                "violations": first_failure["violations"], "repairable": first_failure["repairable"],
                # AA-768 — what the rewrite is actually told: every blocking failure, not one gate.
                "feedback": _repair_feedback(gate_ledger, channel),
                # AA-791 — this attempt's whole ledger (trimmed), so every attempt's gates are kept.
                "gate_ledger": ledger_snapshot(gate_ledger),
            })
            logger.info(
                "t9_attempt_failed_quality_check", request_id=str(request_id), attempt=attempt,
                gate=first_failure["gate"], repairable=first_failure["repairable"],
            )

            # AA-528 gate-regression-guard — a full rewrite (not a targeted field fix) can break a
            # gate that was already passing while it fixes the one it was told to fix (real cases:
            # piece 8b2562a1 introduced a brand-new F1_grounding failure on attempt 2 that attempt
            # 1 never had; piece 319bd3d8 fabricated a different ungrounded number while fixing the
            # one it was told about). Compare this attempt's BLOCKING gate failures against the
            # immediately-preceding attempt's: if at least one gate that previously passed now
            # fails, AND this attempt is not net-better (same or more total blocking failures),
            # the rewrite is treated as a regression — revert to the previous attempt's own
            # content/gate_ledger/seo_meta/summary/embedding rather than persisting a rewrite that
            # traded a known problem for a new (or additional) one. Deliberately does NOT spend an
            # extra LLM call (option (a) from the issue) — this is option (b): pick the better of
            # the two already-computed attempts.
            if prev_snapshot is not None:
                prev_ledger = prev_snapshot["gate_ledger"]
                prev_blocking_fail = {g["gate"] for g in prev_ledger if g.get("blocking", True) and not g["passed"]}
                cur_blocking_fail = {g["gate"] for g in gate_ledger if g.get("blocking", True) and not g["passed"]}
                new_regressions = cur_blocking_fail - prev_blocking_fail
                # AA-780 review — an attempt 1 that blocked only on gates that never hold, one of them the
                # under-floor F14 (a too-short post sent to a full rewrite), had no product-truth problem for
                # this rewrite to trade against: reverting would ship the short post the rewrite was for (the
                # r4 false pass). When this attempt's failures are all sentence-repairable and an attempt is
                # left, keep it and let the next attempt repair it. (An F15-only attempt 1 still reverts: its
                # text is the full-length, truth-clean one.)
                prev_fail = [g for g in prev_ledger if g.get("blocking", True) and not g["passed"]]
                if (new_regressions and attempt < MAX_ATTEMPTS and _floor_only_failure(prev_fail)
                        and first_failure["repairable"]
                        and sentence_repair.plan(content_text, gate_ledger) is not None):
                    logger.info("t9_gate_regression_kept_after_floor_rewrite", request_id=str(request_id),
                                attempt=attempt, regressed_gates=sorted(new_regressions))
                    new_regressions = set()
                if new_regressions and len(cur_blocking_fail) >= len(prev_blocking_fail):
                    logger.info(
                        "t9_gate_regression_reverted", request_id=str(request_id), attempt=attempt,
                        regressed_gates=sorted(new_regressions),
                        prev_blocking_fail=sorted(prev_blocking_fail),
                        cur_blocking_fail=sorted(cur_blocking_fail),
                    )
                    # AA-570 — capture THIS attempt's just-generated rewrite before it's
                    # overwritten below by the revert. In this specific branch, attempt 1 (not
                    # this attempt) is the one that ends up as the final content_text, so it must
                    # NOT be reported as discarded — this attempt's own regressed content is the
                    # real discard.
                    discarded_attempt = {
                        "attempt_number": attempt, "content_text": content_text,
                        "gate_ledger": gate_ledger,
                    }
                    content_text = prev_snapshot["content_text"]
                    # AA-780 review — attempt 1's under-floor F14 block asked for the rewrite just discarded;
                    # it never holds, so it is waived now (and shown as a flag) instead of blocking the next
                    # step: a remaining F1/F9 still gets its attempt-3 sentence repair (sentence_repair.plan
                    # returns None while an F14 block is in the ledger), and nothing else → the piece ships.
                    prev_ledger, flags = _waive_floor_block(prev_ledger, flags)
                    gate_ledger = prev_ledger
                    seo_meta = prev_snapshot["seo_meta"]
                    summary = prev_snapshot["summary"]
                    embedding = prev_snapshot["embedding"]
                    repair_log.append({
                        "attempt": attempt, "gate_targeted": "gate_regression_guard",
                        "violations": [
                            f"Rewrite reverted — attempt {attempt} newly failed "
                            f"{sorted(new_regressions)} without fixing more than it broke; "
                            f"kept attempt {attempt - 1}'s output instead."
                        ],
                        "repairable": False,
                    })
                    first_failure = next(
                        (g for g in prev_ledger if not g["passed"] and g.get("blocking", True)), None
                    )

            # AA-770 — attempt 3 only when attempt 2's failures are all sentence-repairable.
            last_try = attempt >= MAX_ATTEMPTS or (
                attempt == MAX_ATTEMPTS - 1 and sentence_repair.plan(content_text, gate_ledger) is None)
            if first_failure is None or not first_failure["repairable"] or last_try:
                # AA-613 — under the 3-tier severity model, `first_failure` is now ONLY ever a
                # BLOCKING (product-truth) gate: grounding/banned/CTA/FACT_CHECK/cannibalization/
                # SEO/structure. Brand-voice/framework failures are non-blocking warns (they never
                # become first_failure — outcome["passed"] is True and this branch isn't reached),
                # so a warn-only piece ships as 'approved'. `held` therefore now means precisely
                # "unresolved product-truth after ≤2 retries" — the tenant still SEES this piece
                # (My Content shows held content the same as approved, AA-613 #7), but it cannot be
                # published until the tenant edits it or an admin clears it (v1_publish gate, #8).
                # S234 review — held only on a product-truth gate; a piece whose only blocking
                # failure left is style-only (F15_voice stock phrase) ships with it as a flag.
                # AA-786 — on the LAST attempt only (review P2), LinkedIn cuts the flagged F9 sentences;
                # F9 is waived only when every one of them is gone.
                if last_try:
                    content_text, gate_ledger, flags, cut_entry = _cut_truth_items(
                        content_text, gate_ledger, flags, channel=channel, ignore=brand_ignore, cta=cta,
                        moments=hyg.moments, spec=spec)
                    if cut_entry:
                        repair_log.append({"attempt": attempt, **cut_entry})
                        if cut_entry["sentences_cut"]:
                            summary, embedding = await _after_cut(content_text, tenant_id, embedding)
                status, held_reason, flags = _hold_or_ship(gate_ledger, flags)
                break

        # AA-780 (S246 panel) — a STOCK_PATTERNS line left in the final attempt's text (F15 blocks on
        # attempt 1 only) is cut in code here, whatever the status; never the hook or the CTA.
        content_text, gate_ledger, flags, stock_entry = _cut_stock_lines(
            content_text, gate_ledger, flags, channel=channel, spec=spec, cta=cta, ignore=brand_ignore,
            moments=hyg.moments)
        if stock_entry:
            repair_log.append({"attempt": attempt, **stock_entry})
            if stock_entry["sentences_cut"]:
                summary, embedding = await _after_cut(content_text, tenant_id, embedding)

        # AA-819 (AUP 2026-11) — a review / customer voice / independence claim the repair could not
        # remove is cut here, never shipped (a held piece still reaches the tenant, AA-770).
        content_text, gate_ledger, flags, status, held_reason, aup_entry = _enforce_authenticity(
            content_text, gate_ledger, flags, status, held_reason, cta=cta, channel=channel, spec=spec)
        if aup_entry:
            repair_log.append({"attempt": attempt, **aup_entry})
            summary, embedding = await _after_cut(content_text, tenant_id, embedding)

        # AA-452 — mandatory, tenant-facing-leak-prevention step: strip every [R:atom_id]/[F:id]
        # citation tag (channel='blog' only ever produces one, prompts.py's _BLOG_FORMAT_INSTRUCTIONS)
        # from content_text AND from every gate_ledger/repair_log violation string (a gate's own
        # violation message can itself quote a tagged excerpt — see quality_gates.gate_grounding()'s
        # own comment) BEFORE this piece is persisted or returned, regardless of status ('approved'
        # or 'held' both go through this — a held piece is still fully visible to the tenant per
        # _hold()'s own "hold VISIBLE, never silent" precedent, so a held piece leaking a raw tag
        # would be exactly as real a leak as an approved one). Runs unconditionally for every
        # channel — a no-op for the 7 that never produce a tag, real for blog.
        content_text = strip_citation_tags(content_text)
        gate_ledger = deep_strip_citation_tags(gate_ledger)
        repair_log = deep_strip_citation_tags(repair_log)
        flags = deep_strip_citation_tags(flags)
        if held_reason:
            held_reason = strip_citation_tags(held_reason)
        # S247 (AA-779, ADR 0011) — behind T9_FAQ_LLM_FILTER (default off) one t9_faq_answer_fit call checks
        # that each final FAQ answer answers its question; `on` drops the ones that do not (never a hold, never
        # a regeneration), `shadow` only records. After the tag strip (the model reads clean text), before the
        # final integrity pass (it reads the result).
        if channel == "blog":
            content_text, flags, fit_cost = await _apply_faq_answer_fit(
                content_text, flags, context=context, piece_id=piece_id, request_id=request_id)
            total_cost += fit_cost
        # S234 round 4 (r3 panel §4 fix 1) — the very last step before the piece is stored, after every
        # repair, revert and the tag strip: fail closed — a duplicate, a second CTA or a fragment the
        # pass cannot remove holds the piece (r3 #19 was approved with three visible duplicates).
        content_text, gate_ledger, flags, status, held_reason = _final_integrity(
            content_text, hyg, gate_ledger, flags, status, held_reason)
        # AA-925 (Q-2, design §6.3) — the very last verdict step: a hard fail-open detector on the FINAL attempt
        # (the F9 judge outage marker on its ledger) labels an approved piece held. Reverses AA-770's "an F9
        # outage is a warn, not a hold"; `held` stays an admin-only label, the tenant still receives the piece.
        # The marker is a non-blocking flag + a repair_log entry, never a blocking ledger gate (that would
        # trigger a rewrite and confuse the AA-528 guard).
        status, held_reason, flags, repair_log = _degraded_final(gate_ledger, flags, repair_log, status,
                                                                 held_reason, piece_id=piece_id)

        # AA-570 — the common case: attempt 2 ran and its own gate-regression-guard branch above
        # never fired, so `discarded_attempt` is still None even though a real attempt WAS
        # thrown away (attempt 1, overwritten by attempt 2's rewrite). `prev_snapshot` is exactly
        # that attempt's own outcome, captured right before the rewrite ran.
        if discarded_attempt is None and attempt > 1 and prev_snapshot is not None:
            discarded_attempt = {
                "attempt_number": attempt - 1, "content_text": prev_snapshot["content_text"],
                "gate_ledger": prev_snapshot["gate_ledger"],
            }
        if discarded_attempt is not None:
            discarded_attempt = {
                **discarded_attempt,
                "content_text": strip_citation_tags(discarded_attempt["content_text"]),
                "gate_ledger": deep_strip_citation_tags(discarded_attempt["gate_ledger"]),
            }

        # AA-499 (Decision 5) — within-tenant reuse flag, 'approved' only (a held piece never
        # ships, nothing useful to compare it against or index it for). Reuses the LAST attempt's
        # own `embedding` (computed above, inside the loop, for AA-484's cannibalization check on
        # that same attempt's content_text) rather than recomputing — one embedding call per
        # attempt total, not two. Never affects gate pass/fail — a pure metadata annotation on an
        # already-decided outcome. Soft-fail throughout: a missing embedding (call failed, or
        # tenant_id absent) just leaves this flag absent — never blocks persistence.
        if embedding is not None and tenant_id:
            similar = await find_similar_pieces(
                embedding, pool, tenant_id=UUID(tenant_id), exclude_piece_id=piece_id, limit=1,
            )
            # Only the SAME tenant, a DIFFERENT atom — Decision 4's own history block
            # already covers "avoid repeating an angle for the SAME atom"; this is the
            # complementary case: two different atoms converging on near-identical content.
            match = next((s for s in similar if s.atom_id != atom_id), None)
            if match and match.similarity >= _REUSE_SIMILARITY_THRESHOLD:
                flags.append({
                    "gate": "within_tenant_reuse", "blocking": False,
                    "similarity": round(match.similarity, 4), "similar_piece_id": match.piece_id,
                    "violations": [
                        f"This piece reads very similar (cosine similarity "
                        f"{match.similarity:.2f}) to a piece already written for a "
                        f"different atom on this account."
                    ],
                })

        gate_ledger = _waive_style_only(gate_ledger, status)  # S234 round 2 — panel §3, piece #7
        _show_live_text(content_text, seo_meta, channel)  # AA-668: the final snapshot is the saved text
        progress_step("save")  # AA-637 live view
        if channel == "blog":
            # S245 (AA-779 2A) — where each final FAQ question came from (a real source, or `writer`).
            context["faq_questions"] = faq_sources([q for q, _ in faq_pairs(content_text)],
                                                   context.get("search_questions") or [],
                                                   context.get("real_questions") or [])
            context["faq_pairs_after"] = faq_pair_stats(content_text, context)
        await _finalize_piece(
            pool, piece_id=piece_id, attempt_number=attempt,
            content_text=content_text, status=status, held_reason=held_reason,
            gate_ledger=gate_ledger, repair_log=repair_log, flags=flags,
            discarded_attempts=[discarded_attempt] if discarded_attempt else [],
            seo_title=seo_meta.get("seo_title"), meta_description=seo_meta.get("meta_description"),
            slug=seo_meta.get("slug"), content_summary=summary,
            # AA-770 (S233) — a held piece now reaches the tenant too, so it is indexed like an
            # approved one (F10 / within-tenant reuse must see it).
            content_embedding=embedding,
            writer_input=writer_input_snapshot(context),
        )
        logger.info(
            "t9_write_and_check_done", request_id=str(request_id), status=status,
            attempts=attempt, cost_usd=total_cost,
        )

        # AA-770 (S233) — the AA-485 buffer retry (a second content_piece row per held request) is
        # gone: attempt 3 above is its replacement, on the same row.
    except Exception as exc:
        logger.error(
            "t9_write_background_failed", request_id=str(request_id), piece_id=str(piece_id),
            attempt=attempt, error_type=type(exc).__name__, error=str(exc),
        )
        try:
            await _finalize_piece(
                pool, piece_id=piece_id, attempt_number=attempt, content_text="",
                status="failed", held_reason=f"{type(exc).__name__}: {exc}",
                gate_ledger=[], repair_log=[], flags=[],
            )
        except Exception:
            logger.error(
                "t9_write_background_failed_status_write_also_failed",
                request_id=str(request_id), piece_id=str(piece_id),
            )


def writer_input_snapshot(context: dict) -> dict:
    """AA-769 — what the T9 writer was given, stored on the piece for the admin Content Quality
    page (never in a tenant projection). Admin-facing, so the brand pack is stored whole."""
    chosen = context.get("chosen") or {}
    goal = context.get("goal") or {}
    return {
        "seed": context.get("atom_text"),
        "route_moments": [{"atom_id": a, "seed": t} for a, t in (context.get("route_segments") or [])],
        "facts": context.get("facts_text") or "",
        "scope": context.get("scope"),
        "brand_pack": context.get("brand_audience") or {},
        "brand_rubric": context.get("brand_rubric_text"),
        "angle": {k: chosen.get(k) for k in ("name", "why_it_works", "formula_fit", "best_final_style")},
        "goal": goal.get("key"), "channel": context.get("channel"), "keyword": context.get("keyword"),
        "cta": context.get("cta"), "trip": context.get("trip_name"), "destination": context.get("destination"),
        # S234 round 2 — the TRIP line the writer actually got (scoped trip name); absent before.
        **({"writer_trip": context["writer_trip_name"]} if "writer_trip_name" in context else {}),
        # S234 round 3 — the full trip's places outside the scope (out-of-scope gazetteer); absent before.
        **({"trip_places": context["trip_places"]} if context.get("trip_places") else {}),
        # S245 (AA-779 2A) — blog only: the real search questions the writer got, with their source;
        # how many each source offered / kept; and the source of each final FAQ question. Admin-only.
        **({"search_questions": context.get("search_questions") or [],
            "question_sources": source_counts(context.get("real_questions") or [],
                                              context.get("search_questions") or []),
            "faq_questions": context.get("faq_questions") or [],
            **{k: context[k] for k in ("faq_pairs_before", "faq_pairs_after") if k in context}}
           if context.get("channel") == "blog" and "real_questions" in context else {}),
        # AA-982 — how many place_paa questions the stored A3 verdicts dropped; absent when the switch is off.
        **({"a3_question_filter": context["a3_question_filter"]}
           if context.get("channel") == "blog" and context.get("a3_question_filter") is not None else {}),
        # S247 (AA-779, ADR 0011) — the LLM question filter's mode, verdicts and fallback; absent when it is off.
        **({"faq_filter": context["faq_filter"]} if context.get("faq_filter") else {}),
        # S247 (AA-779, ADR 0011) — the answer-fit step's mode, verdicts, drops and fallback; absent when it is off.
        **({"faq_answer_fit": context["faq_answer_fit"]} if context.get("faq_answer_fit") else {}),
    }


async def _finalize_piece(
    pool, *, piece_id: UUID, attempt_number: int, content_text: str,
    status: str, held_reason: Optional[str], gate_ledger: list[dict], repair_log: list[dict],
    flags: list[dict],
    seo_title: Optional[str] = None, meta_description: Optional[str] = None,
    slug: Optional[str] = None, content_summary: Optional[str] = None,
    content_embedding: Optional[list[float]] = None,
    discarded_attempts: Optional[list[dict]] = None,
    writer_input: Optional[dict] = None,
) -> dict:
    # AA-514 — seo_title/meta_description/slug default None (the exception handler's own
    # "failed" finalize call above never has a real seo_meta to pass, and every non-blog piece
    # is None either way) — same "NULL means never populated/not applicable" convention every
    # other blog-only column in this schema already uses (migration 136's own header comment).
    # AA-519 Việc 5 — flags (ADR 0023 non-blocking gate results) persisted the same way, NOT
    # route_hub_name/route_segment_count (Việc 4) — those are set once at INSERT and never
    # change across a write session, so they're deliberately absent from this UPDATE.
    # AA-498 (Decision 4) — content_summary defaults None the same way: the exception handler's
    # "failed" call has no real summary, and a soft-fail (model didn't produce one) is None too.
    # AA-499 (Decision 5) — content_embedding, same None-default convention: only ever real for
    # a status='approved' piece (run_write_background()'s own gate), None otherwise. asyncpg has
    # no built-in `vector` codec (confirmed — this is the first code anywhere to write to a
    # pgvector column in this repo), so the value is pre-formatted as pgvector's own text literal
    # ('[0.1,0.2,...]') by embedding_to_pgvector_literal() and cast explicitly in SQL.
    embedding_literal = embedding_to_pgvector_literal(content_embedding) if content_embedding else None
    # AA-570 — every attempt that did NOT become this row's content_text (migration 148). `[]`
    # default for the exception-handler/buffer-retry call sites (neither ever has more than 1
    # attempt) — only run_write_background()'s own main loop ever passes a non-empty list.
    discarded_attempts = discarded_attempts or []
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE acp_shared.content_piece
            SET attempt_number = $2, content_text = $3, status = $4, held_reason = $5,
                gate_ledger = $6::jsonb, repair_log = $7::jsonb,
                seo_title = $8, meta_description = $9, slug = $10, flags = $11::jsonb,
                content_summary = $12, content_embedding = $13::vector,
                discarded_attempts = $14::jsonb,
                writer_input = COALESCE($15::jsonb, writer_input)  -- AA-769 (migration 220)
            WHERE piece_id = $1
            RETURNING piece_id, tenant_id, angle_gate_request_id, attempt_number, content_text,
                      status, held_reason, gate_ledger, repair_log, created_at,
                      seo_title, meta_description, slug, route_hub_name, route_segment_count, flags,
                      discarded_attempts
            """,
            piece_id, attempt_number, content_text, status, held_reason,
            json.dumps(gate_ledger), json.dumps(repair_log), seo_title, meta_description, slug,
            json.dumps(flags), content_summary, embedding_literal, json.dumps(discarded_attempts),
            json.dumps(writer_input, default=str) if writer_input else None,
        )

        # AA-559 — semantic tenant-activity log: the REAL "content finished writing" event
        # (unlike _insert_placeholder_piece()'s CONTENT_PIECE_CREATED, which fires before any
        # LLM/gate work). Covers all 3 call sites of this function (main loop, exception-handler
        # fallback, buffer retry) in one place — every one of them reaches a terminal status
        # (approved/held/failed).
        await write_audit_log(
            conn, tenant_id=str(row["tenant_id"]), actor=f"tenant:{row['tenant_id']}",
            action=TenantAuditAction.CONTENT_PIECE_FINISHED, resource_type="content_piece",
            resource_id=str(row["piece_id"]),
            details={
                "status": status, "held_reason": held_reason, "attempt_number": attempt_number,
                "request_id": str(row["angle_gate_request_id"]),
            },
        )
    return _row_to_dict(row)


# AA-613 — the tenant-facing projection. The tenant experience is flat: they see the CONTENT and
# nothing about gates/held/retries/technical status. This is the ONE shape every tenant-facing
# read of a piece returns (fetch_piece / fetch_latest_piece_for_request / update_piece_content_text),
# converging on the strict boundary fetch_review()/fetch_review_list() already drew (AA-501):
#   - `ready_state`: "ready" once there's real content to use (approved OR held — a held piece
#     is fully delivered to the tenant now, only its PUBLISH is gated, AA-613 #8), "in_progress"
#     while T9 is still writing, "not_ready" for a hard failure (no content produced).
#   - `content_text`: only for ready pieces (approved/held); None while processing/failed.
#   - `flags`: tenant-safe non-blocking notes only (never gate_ledger/repair_log/held_reason).
# Deliberately DOES NOT expose: raw `status`, `held_reason`, `gate_ledger`, `repair_log`,
# `discarded_attempts`, `attempt_number` — all internal/admin-only (kept in the DB, read via the
# admin monitor, never here).
_TENANT_READY_STATE = {"approved": "ready", "held": "ready", "processing": "in_progress"}


def _tenant_safe_piece(row) -> dict:
    d = _row_to_dict(row)
    status = d.get("status")
    ready_state = _TENANT_READY_STATE.get(status, "not_ready")
    has_content = status in ("approved", "held")
    return {
        "piece_id": d["piece_id"],
        "angle_gate_request_id": d["angle_gate_request_id"],
        "channel": d.get("channel"),
        "ready_state": ready_state,
        "content_text": d["content_text"] if has_content else None,
        "seo_title": d.get("seo_title"),
        "meta_description": d.get("meta_description"),
        "slug": d.get("slug"),
        "route_hub_name": d.get("route_hub_name"),
        "route_segment_count": d.get("route_segment_count"),
        # AA-770 (S233) — no gate notes on the tenant side; they live in the admin Content Quality page.
        "created_at": d["created_at"],
    }


def _row_to_dict(row) -> dict:
    gate_ledger = row["gate_ledger"]
    repair_log = row["repair_log"]
    return {
        "piece_id": str(row["piece_id"]),
        "tenant_id": str(row["tenant_id"]),
        "angle_gate_request_id": str(row["angle_gate_request_id"]),
        "attempt_number": row["attempt_number"],
        "content_text": row["content_text"],
        "status": row["status"],
        "held_reason": row["held_reason"],
        "gate_ledger": json.loads(gate_ledger) if isinstance(gate_ledger, str) else gate_ledger,
        "repair_log": json.loads(repair_log) if isinstance(repair_log, str) else repair_log,
        "created_at": row["created_at"].isoformat(),
        # AA-514 — None for every non-blog piece and every pre-AA-514 row. `_insert_placeholder_
        # piece()`'s own RETURNING doesn't select these 3 columns (nothing to return before T9
        # has even run) — asyncpg Record has no .get(), so membership is checked via .keys().
        "seo_title": row["seo_title"] if "seo_title" in row.keys() else None,
        "meta_description": row["meta_description"] if "meta_description" in row.keys() else None,
        "slug": row["slug"] if "slug" in row.keys() else None,
        # AA-519 Việc 4 — set once at INSERT, immutable across the write session (unlike flags
        # below, which only exists after the first write+gate-check attempt).
        "route_hub_name": row["route_hub_name"] if "route_hub_name" in row.keys() else None,
        "route_segment_count": (
            row["route_segment_count"] if "route_segment_count" in row.keys() else None
        ),
        # AA-569 — export/edit need the piece's own channel (Blog gets text+HTML export, every
        # other channel text-only); absent from _insert_placeholder_piece()'s RETURNING (set at
        # INSERT time but that statement doesn't select it back), same "missing key = not
        # selected by this particular query" convention as the fields above.
        "channel": row["channel"] if "channel" in row.keys() else None,
        # AA-570 — every attempt discarded in favor of this row's own content_text (migration
        # 148). [] for every single-attempt piece and every pre-AA-570 row, same "missing key or
        # NULL means never populated" convention as flags above.
        "discarded_attempts": (
            (json.loads(row["discarded_attempts"]) if isinstance(row["discarded_attempts"], str)
             else row["discarded_attempts"]) or []
            if "discarded_attempts" in row.keys() and row["discarded_attempts"] is not None else []
        ),
        # AA-519 Việc 5 — absent from _insert_placeholder_piece()'s RETURNING (nothing to report
        # before T10 has run once), same "missing key means never populated yet" convention
        # seo_title/etc. above already use.
        "flags": (
            (json.loads(row["flags"]) if isinstance(row["flags"], str) else row["flags"]) or []
            if "flags" in row.keys() and row["flags"] is not None else []
        ),
    }


# AA-817 — joined through angle_gate_request (RLS, tenant_id) rather than reading
# angle_gate_option (no tenant_id, no RLS) by request_id alone: on the tenant pool another tenant's
# option ids are then invisible too, and on the admin pool the WHERE keeps the same boundary.
_CHOSEN_OPTION_QUERY = """
    SELECT o.option_id
    FROM acp_shared.angle_gate_option o
    JOIN acp_shared.angle_gate_request r ON r.request_id = o.request_id
    WHERE o.request_id = $1 AND o.chosen = true AND r.tenant_id = $2
"""

_LATEST_PIECE_FOR_OPTION_QUERY = """
    SELECT piece_id, tenant_id, angle_gate_request_id, attempt_number, content_text, status,
           held_reason, gate_ledger, repair_log, created_at, seo_title, meta_description, slug,
           route_hub_name, route_segment_count, flags
    FROM acp_shared.content_piece
    WHERE angle_gate_request_id = $1 AND tenant_id = $2
      AND (angle_gate_option_id = $3 OR angle_gate_option_id IS NULL)
    ORDER BY created_at DESC
    LIMIT 1
"""


async def fetch_latest_piece_for_request(
    tenant_id: UUID, request_id: UUID, pool, request: Optional[Request] = None,
) -> Optional[dict]:
    """AA-522 — resume support for the T8/T9 wizard's write step. The real bug this issue was
    filed for: a 422 (missing CTA) from start_write() only ever surfaced as CLIENT-side React
    state (AngleGateTab.tsx's old `needsCtaInput`) — a page reload at that exact moment wiped it,
    leaving the tenant on an empty Write card with no CTA form and no way forward, even though
    nothing was actually lost server-side. Fixed by giving the FE a way to ask the server "what's
    the real state of this request's write, right now" on every load, instead of trusting local
    state a reload can wipe.

    Scoped to the CURRENTLY chosen angle option (same option-id-first / chosen=true-fallback
    convention `fetch_review()` above already uses) so a stale piece from a since-reopened-and-
    re-chosen angle (AA-497) is never mistaken for the current one — reopen_request() doesn't
    delete the old content_piece row, it just stops being the option this request currently
    points to. Returns None (not an error) when nothing has been written yet for this option —
    the normal case right after an angle is first chosen.

    AA-817 — when `request` is passed (the router does) and the aa817:content_piece_reads_pool flag
    is on, both reads run on the aa_app_user tenant pool inside one transaction with app.tenant_id
    set LOCAL, so RLS (content_piece, angle_gate_request) holds the tenant boundary even if a WHERE
    were wrong. Fail closed: flag on without a tenant pool or tenant id is a 503, never the admin
    pool. Flag off, or a direct caller with only `pool`: the admin pool, as before."""
    if request is not None:
        from api.core.aa544_tenant_pool import CONTENT_PIECE_READS_FLAG, acquire_scoped_conn
        async with acquire_scoped_conn(
            request, str(tenant_id) if tenant_id else "", CONTENT_PIECE_READS_FLAG, fail_closed=True,
        ) as (conn, used_tenant_pool):
            row = await _latest_piece_for_request(conn, tenant_id, request_id)
        logger.info("aa817_latest_piece", tenant_id=str(tenant_id), used_tenant_pool=used_tenant_pool)
    else:
        async with pool.acquire() as conn:
            row = await _latest_piece_for_request(conn, tenant_id, request_id)
    # AA-613 — tenant-safe projection (this feeds the tenant wizard's resume/poll).
    return _tenant_safe_piece(row) if row else None


async def _latest_piece_for_request(conn, tenant_id: UUID, request_id: UUID):
    option_row = await conn.fetchrow(_CHOSEN_OPTION_QUERY, request_id, tenant_id)
    option_id = option_row["option_id"] if option_row else None
    return await conn.fetchrow(_LATEST_PIECE_FOR_OPTION_QUERY, request_id, tenant_id, option_id)


_FETCH_PIECE_QUERY = """
    SELECT piece_id, tenant_id, angle_gate_request_id, attempt_number, content_text,
           status, held_reason, gate_ledger, repair_log, created_at,
           seo_title, meta_description, slug, channel,
           route_hub_name, route_segment_count, flags
    FROM acp_shared.content_piece
    WHERE piece_id = $1 AND tenant_id = $2
"""


async def fetch_piece(tenant_id: UUID, piece_id: UUID, pool, request: Optional[Request] = None) -> dict:
    """AA-544 Stage 5 canary — when `request` is passed (the router does; a direct caller that
    only has a bare `pool` falls back to it unchanged, same admin-pool behavior as before this
    stage) and the aa544:stage5:get_piece_pool flag is on, this runs through the aa_app_user
    tenant pool with app.tenant_id set LOCAL to the transaction. S241 D-12: a missing key is ON;
    rollback = SET <key> false, which gives the pre-AA-544 behavior (admin pool). The WHERE
    tenant_id = $2 clause below already scoped this query correctly before RLS existed — RLS is
    defense-in-depth here, not the only thing standing between tenants for this query.

    AA-817 PR-A — fail closed (the flag has been on since S239): flag on without a tenant pool, a
    tenant id or a tenant connection is a 503, never the admin pool. Serves GET /pieces/{id} and
    GET /pieces/{id}/export."""
    if request is not None:
        from api.core.aa544_tenant_pool import STAGE5_GET_PIECE_FLAG, acquire_scoped_conn
        async with acquire_scoped_conn(
            request, str(tenant_id) if tenant_id else "", STAGE5_GET_PIECE_FLAG, fail_closed=True,
        ) as (conn, used_tenant_pool):
            row = await conn.fetchrow(_FETCH_PIECE_QUERY, piece_id, tenant_id)
            served = await _piece_served(conn, tenant_id, row)
        logger.info("aa544_stage5_fetch_piece", tenant_id=str(tenant_id), used_tenant_pool=used_tenant_pool)
    else:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_FETCH_PIECE_QUERY, piece_id, tenant_id)
            served = await _piece_served(conn, tenant_id, row)
    if row is None:
        raise ContentWritingError(f"piece_id={piece_id} not found for this tenant")
    # AA-613 — tenant-safe projection: content + ready_state + flags only, never held_reason/
    # gate_ledger/repair_log/raw status. This is a tenant endpoint (GET/PATCH /v1/.../pieces/{id}).
    # AA-986 L3: plus the served fields of the piece's tour.
    return _tl.with_served(_tenant_safe_piece(row), served)


async def _piece_served(conn, tenant_id, row):
    """AA-986 L3: the ServedState of a piece row's tour (None: no row, or no state)."""
    if row is None:
        return None
    rid = str(row["angle_gate_request_id"])
    return (await _tl.request_served_states(conn, tenant_id, [rid])).get(rid)


# AA-859 W2 — module level so the tests can mutate it (forgotten-WHERE check). Ownership + status in the
# WHERE, as before; on the tenant pool RLS (content_piece tenant_isolation, USING + WITH CHECK) also holds
# the tenant boundary.
_PIECE_EDIT_SQL = """
    UPDATE acp_shared.content_piece
    SET content_text = $3
    WHERE piece_id = $1 AND tenant_id = $2 AND status IN ('approved', 'held')
    RETURNING piece_id, tenant_id, angle_gate_request_id, attempt_number, content_text,
              status, held_reason, gate_ledger, repair_log, created_at,
              seo_title, meta_description, slug, channel,
              route_hub_name, route_segment_count, flags
"""


def _piece_edit_not_found(piece_id: UUID) -> ContentWritingError:
    return ContentWritingError(f"piece_id={piece_id} not found for this tenant, or not in an editable state")


async def _write_piece_edit_audit(conn_or_pool, tenant_id: UUID, piece_id: UUID, content_text: str) -> None:
    await write_audit_log(
        conn_or_pool, tenant_id=str(tenant_id), actor=f"tenant:{tenant_id}",
        action=TenantAuditAction.CONTENT_PIECE_EDITED, resource_type="content_piece",
        resource_id=str(piece_id), details={"content_length": len(content_text)},
    )


async def update_piece_content_text(
    tenant_id: UUID, piece_id: UUID, content_text: str, pool, request: Optional[Request] = None,
) -> dict:
    """AA-569 — tenant hand-edit on My Content (ReviewList.tsx). Ownership + status both enforced
    in the WHERE clause, not a separate check: a 'processing' piece has nothing real to edit yet
    (still being written) and a 'failed' piece has no content_text worth keeping (see
    run_write_background()'s exception-handler finalize call) — same 2-status boundary
    fetch_review_list() already draws for "does this piece have real content_text to show".

    AA-859 W2 — when `request` is passed (the router does), the UPDATE goes through
    acquire_scoped_conn(PIECE_EDIT_FLAG, fail_closed=True): flag on, it runs on the aa_app_user tenant
    pool in one transaction with app.tenant_id set LOCAL (RLS holds the boundary even if the WHERE were
    wrong), and the audit row is written on MAIN after that transaction committed, best-effort
    (`aa859_piece_edit_audit_failed`, the edit still answers 200). Flag off, or a direct caller with only
    `pool`: the pre-AA-859 behaviour exactly (admin pool, autocommit UPDATE, audit on the same
    connection, an audit failure propagates). One `aa859_piece_edit` line per request with
    used_tenant_pool."""
    if request is None:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_PIECE_EDIT_SQL, piece_id, tenant_id, content_text)
            if row is None:
                raise _piece_edit_not_found(piece_id)
            await _write_piece_edit_audit(conn, tenant_id, piece_id, content_text)
        # AA-613 — tenant-safe projection (this is a tenant edit endpoint, PATCH /v1/.../pieces/{id}).
        return _tenant_safe_piece(row)

    from api.core.aa544_tenant_pool import PIECE_EDIT_FLAG, acquire_scoped_conn
    async with acquire_scoped_conn(
        request, str(tenant_id) if tenant_id else "", PIECE_EDIT_FLAG, fail_closed=True,
    ) as (conn, used_tenant_pool):
        row = await conn.fetchrow(_PIECE_EDIT_SQL, piece_id, tenant_id, content_text)
        logger.info("aa859_piece_edit", tenant_id=str(tenant_id), piece_id=str(piece_id),
                    updated=row is not None, used_tenant_pool=used_tenant_pool)
        if row is None:
            raise _piece_edit_not_found(piece_id)
        if not used_tenant_pool:  # flag off: the old shape, audit next to the UPDATE on the admin conn
            await _write_piece_edit_audit(conn, tenant_id, piece_id, content_text)
    if used_tenant_pool:
        # AA-859 MAIN exception: audit_log has no RLS and no aa_app_user grant; written after the tenant
        # transaction committed. Best-effort: the edit is saved, a lost audit row must not turn it into a 500.
        try:
            await _write_piece_edit_audit(pool, tenant_id, piece_id, content_text)
        except Exception as exc:
            logger.warning("aa859_piece_edit_audit_failed", tenant_id=str(tenant_id), piece_id=str(piece_id),
                           error=repr(exc))
    # AA-613 — tenant-safe projection (this is a tenant edit endpoint, PATCH /v1/.../pieces/{id}).
    return _tenant_safe_piece(row)


# ── AA-501: tenant-facing pre-T11 review (deliberately narrower than fetch_piece() above) ──────

# AA-614 — held → "ready" (was "not_ready"), converging fetch_review()/fetch_review_list() onto
# the SAME held-is-ready rule _tenant_safe_piece() / fetch_piece() already use (AA-613 #7/#8): a
# held piece is a real, complete, product-truth-unresolved outcome — the tenant SEES it in My
# Content exactly like an approved piece (content_text is already returned for held below), only
# its PUBLISH is gated (v1_publish 422). Before AA-614 these two tenant read paths disagreed —
# fetch_piece said "ready", the list said "not_ready" — so the same held piece looked deliverable
# on its detail page but broken in the list. "not_ready" now means precisely "no content to show"
# (failed / processing produced none), never "content exists but is gated".
_READY_STATE_MAP = {
    "approved": "ready",
    "processing": "in_progress",
    "held": "ready",
    "failed": "not_ready",
}

_LATEST_PIECE_FOR_REQUEST_QUERY = """
    SELECT piece_id, status, content_text, channel, angle_gate_option_id, created_at,
           route_hub_name, route_segment_count, flags
    FROM acp_shared.content_piece
    WHERE angle_gate_request_id = $1 AND tenant_id = $2
    ORDER BY created_at DESC
    LIMIT 1
"""
# Deliberately does NOT select gate_ledger/repair_log/held_reason — STEP0 §4's own
# recommendation was to strip these at the SQL layer, not just the API response layer, so a
# future field added to fetch_review()'s dict can never accidentally leak them by copying
# fetch_piece()'s SELECT list.
#
# AA-519 Việc 4/5 — route_hub_name/route_segment_count/flags ARE selected here, deliberately: all
# 3 are tenant-safe by construction (route metadata is just a label, flags only ever holds
# non-blocking "note for a human" gate results, never the full technical ledger) — this doesn't
# reopen the STEP0 §4 boundary above, it's a different, narrower field.

_ATOM_CONTEXT_QUERY = """
    SELECT text, activity_type, emotional_hook, season_note
    FROM acp_contract.tour_atoms
    WHERE atom_id = $1 AND owner_scope IN ('platform', $2) AND NOT deleted AND NOT is_empty_marker
"""


async def _fetch_atom_context(tenant_id: UUID, atom_id: str, pool) -> Optional[dict]:
    """Atom context for the review screen (AA-501) — text/activity_type/emotional_hook/
    season_note only, the fields the build task asked for (persona_fit/media are
    AA-internal signals, out of scope here). AA-567: `owner_scope IN ('platform', tenant_id)`,
    not tenant-only — same fix, same reasoning as this module's own `_fetch_atom_text()` /
    `acp_angle_gate.service._fetch_atom_for_tenant()`."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_ATOM_CONTEXT_QUERY, atom_id, str(tenant_id))
    return dict(row) if row else None


async def fetch_review(tenant_id: UUID, request_id: UUID, pool, request: Optional[Request] = None) -> dict:
    """AA-501 — the tenant-facing pre-T11 review: full write context (atom/tour/goal/angle/
    DFS-PAA/channel) plus the latest content_piece for this request, WITHOUT any of T10's
    technical detail. This is deliberately STRICTER than GET .../pieces/{piece_id}
    (fetch_piece() above, which the T8/T9 wizard's own end-of-flow card uses and which DOES
    return held_reason) — Nghiệp confirmed this divergence explicitly for the new screen, it is
    not an inconsistency to reconcile.

    `content_piece` is no longer 1-row-per-request since migration 125 (AA-497 reopen/re-write) —
    picks the LATEST piece by created_at, never assumes attempt_number orders across a request's
    lifetime (STEP0 §1's own warning).

    Raises angle_gate_service.RequestNotFoundError (request doesn't exist / isn't this tenant's,
    propagates from fetch_request() below) or ContentWritingError (request exists but T9 has
    never written anything under it yet) — the router maps both to 404.

    AA-859 wave 4 — when `request` is passed (the router does), every read runs in ONE acquire_scoped_conn
    block behind REVIEWS_FLAG (fail closed): the request + options + Subject header (fetch_request_on), the
    latest piece, the atom context and the tenant's trips, on the aa_app_user tenant pool with app.tenant_id
    LOCAL (RLS 229 / 241 / 265 / 266 hold the boundary even if a WHERE were wrong). No main-pool read at all:
    the review does not use the generation status. Flag off (or a direct caller with only `pool`): the
    main pool, as before."""
    if request is None:
        req = await angle_gate_service.fetch_request(tenant_id, request_id, pool)
        async with pool.acquire() as conn:
            piece_row = await conn.fetchrow(_LATEST_PIECE_FOR_REQUEST_QUERY, request_id, tenant_id)
        if piece_row is None:
            raise _no_written_content(request_id)
        atom_context = await _fetch_atom_context(tenant_id, req["atom_id"], pool)
        trips = await fetch_tenant_trips(tenant_id, pool) if req["trip_id"] else []
        async with pool.acquire() as conn:   # AA-986 L3: the served fields of the request's tour
            served = (await _tl.request_served_states(conn, tenant_id, [str(request_id)])).get(str(request_id))
        return _tl.with_served(_review_dict(req, piece_row, atom_context, trips), served)

    from api.core.aa544_tenant_pool import REVIEWS_FLAG, acquire_scoped_conn
    async with acquire_scoped_conn(
        request, str(tenant_id) if tenant_id else "", REVIEWS_FLAG, fail_closed=True,
    ) as (conn, used_tenant_pool):
        logger.info("aa859_review_detail", tenant_id=str(tenant_id), request_id=str(request_id),
                    used_tenant_pool=used_tenant_pool)
        req = await angle_gate_service.fetch_request_on(conn, tenant_id, request_id)
        piece_row = await conn.fetchrow(_LATEST_PIECE_FOR_REQUEST_QUERY, request_id, tenant_id)
        if piece_row is None:
            raise _no_written_content(request_id)
        atom_row = await conn.fetchrow(_ATOM_CONTEXT_QUERY, req["atom_id"], str(tenant_id))
        trips = await fetch_tenant_trips_on(conn, tenant_id) if req["trip_id"] else []
        served = (await _tl.request_served_states(conn, tenant_id, [str(request_id)])).get(str(request_id))
    return _tl.with_served(_review_dict(req, piece_row, dict(atom_row) if atom_row else None, trips), served)


def _no_written_content(request_id: UUID) -> ContentWritingError:
    return ContentWritingError(
        f"request_id={request_id} has no written content yet — T9's write step hasn't run "
        "for this request."
    )


def _review_dict(req: dict, piece_row, atom_context: Optional[dict], trips: list) -> dict:
    """The tenant-facing review (AA-501) from the rows fetch_review() read, either pool."""
    # AA-469 Việc 4 — same COALESCE(cp.channel, agr.channel) every other real read site uses
    # (v1_publish.py, admin_a4.py's content-log): a piece's own channel is immutable once
    # written; the parent request's channel can move on to a different value before its NEXT
    # write session.
    channel = piece_row["channel"] or req["channel"]

    # AA-497 — option_id-first join, chosen=true fallback only for pre-AA-497 rows with no
    # angle_gate_option_id, same lesson v1_publish.py already applies (chosen is mutable after a
    # reopen(), a piece's own angle_gate_option_id is not).
    option_id = piece_row["angle_gate_option_id"]
    angle = None
    if option_id:
        angle = next((a for a in req["angles"] if a["option_id"] == option_id), None)
    if angle is None:
        angle = next((a for a in req["angles"] if a["chosen"]), None)

    tour_context = None
    if req["trip_id"]:
        trip = next((t for t in trips if str(t.id) == req["trip_id"]), None)
        if trip:
            tour_context = {"name": trip.name, "destination": trip.destination}

    goal_obj = get_goal(req["goal"]) if req["goal"] else None

    ready_state = _READY_STATE_MAP.get(piece_row["status"], "not_ready")
    # Content is only meaningful to show for approved/held — migration 115/118's own "held keeps
    # real writer output visible for review, failed/processing never produced any" distinction.
    # 'failed'/'processing' rows have content_text = '' by construction, never a partial draft.
    content_text = piece_row["content_text"] if piece_row["status"] in ("approved", "held") else None

    return {
        "request_id": req["request_id"],
        "channel": channel,
        "ready_state": ready_state,
        "content_text": content_text,
        # AA-519 Việc 4 — NULL/None for a Segment pick or pre-Slate request, same convention as
        # every other optional field in this response.
        "route_hub_name": piece_row["route_hub_name"],
        "route_segment_count": piece_row["route_segment_count"],
        "goal": (
            {"key": req["goal"], "label": goal_obj["name"] if goal_obj else req["goal"]}
            if req["goal"] else None
        ),
        "angle": (
            {
                "name": angle["name"], "why_it_works": angle["why_it_works"],
                "formula_fit": angle["formula_fit"], "best_final_style": angle["best_final_style"],
            } if angle else None
        ),
        "atom": atom_context,
        "tour": tour_context,
        "dfs_paa_snapshot": req["dfs_paa_snapshot"],
        "cta": req["cta"],
        "created_at": piece_row["created_at"].isoformat(),
    }


# AA-501 — the browse-all-pieces list `/portal/t10-review` shows (build task: "Danh sách bài viết
# theo channel, mỗi bài mở ra xem đủ ngữ cảnh"). One row per angle_gate_request (its LATEST
# content_piece, same AA-497 "no longer 1:1" rule as fetch_review() above), full context embedded
# directly — no separate per-row detail fetch, matching how admin_a4.py's content-log and this
# app's other list endpoints already return everything up front rather than a paginated
# expand-fetch. Deliberately does NOT select gate_ledger/repair_log/held_reason, same as
# fetch_review().
_TENANT_REVIEWS_QUERY = """
    SELECT * FROM (
        SELECT DISTINCT ON (cp.angle_gate_request_id)
            cp.piece_id, cp.angle_gate_request_id, cp.status, cp.content_text,
            cp.created_at, cp.route_hub_name, cp.route_segment_count, cp.flags,
            COALESCE(cp.channel, agr.channel) AS channel, agr.goal, agr.cta,
            agr.trip_id, agr.dfs_paa_snapshot,
            COALESCE(ago.name, ago_chosen.name) AS angle_name,
            COALESCE(ago.why_it_works, ago_chosen.why_it_works) AS angle_why_it_works,
            COALESCE(ago.formula_fit, ago_chosen.formula_fit) AS angle_formula_fit,
            COALESCE(ago.best_final_style, ago_chosen.best_final_style) AS angle_best_final_style,
            ta.text AS atom_text, ta.activity_type AS atom_activity_type,
            ta.emotional_hook AS atom_emotional_hook, ta.season_note AS atom_season_note
        FROM acp_shared.content_piece cp
        JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
        LEFT JOIN acp_shared.angle_gate_option ago ON ago.option_id = cp.angle_gate_option_id
        LEFT JOIN acp_shared.angle_gate_option ago_chosen
            ON ago_chosen.request_id = agr.request_id AND ago_chosen.chosen = true
            AND cp.angle_gate_option_id IS NULL
        LEFT JOIN acp_contract.tour_atoms ta
            ON ta.atom_id = agr.atom_id AND ta.owner_scope IN ('platform', $2)
        WHERE cp.tenant_id = $1
        ORDER BY cp.angle_gate_request_id, cp.created_at DESC
    ) latest
    ORDER BY latest.created_at DESC
"""
# Live-verify (30/08/2026) caught a real bug here: using the SAME placeholder ($1) both bare
# (compared to cp.tenant_id, a uuid column) and cast ($1::text, compared to ta.owner_scope, a
# text column) makes asyncpg's single-preparation type inference pick ONE type for $1 across the
# whole statement — it resolved to text (from the explicit cast), and Postgres then has no
# `uuid = text` operator for the `cp.tenant_id = $1` comparison ("operator does not exist: uuid =
# text"). Fixed by binding tenant_id twice as two separate, unambiguously-typed params ($1 uuid,
# $2 text) — same value, no ambiguity for either placeholder.
#
# AA-567 — the LEFT JOIN's own owner_scope filter was ALSO the tenant-only bug (see
# _fetch_atom_text()'s docstring above for the full reasoning): for any request whose atom was
# platform-owned (the common case since AA-526), this join silently matched nothing and every
# atom_* column came back NULL — no error, just a quietly incomplete review row. Fixed to
# `owner_scope IN ('platform', $2)`.


async def fetch_review_list(tenant_id: UUID, pool, request: Optional[Request] = None) -> list[dict]:
    """AA-501 — GET /v1/content-writing/reviews. One call, one query (plus one
    fetch_tenant_trips() call, only when at least one row has a trip_id — never per-row) rather
    than N+1 calls into fetch_review() per request; the SQL is intentionally independent of
    fetch_review()'s own query (same precedent as admin_a4.py's content-log and v1_publish.py's
    /pending each keeping their own SQL rather than sharing an abstraction).

    AA-859 wave 4 — with `request` (the router), both reads run in one acquire_scoped_conn block behind
    REVIEWS_FLAG (fail closed; content_piece / angle_gate_* RLS, tour_atoms 266, tenant_tour_pages 265)."""
    trips_by_id: dict[str, object] = {}
    if request is None:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_TENANT_REVIEWS_QUERY, tenant_id, str(tenant_id))
            served = await _tl.request_served_states(conn, tenant_id, [str(r["angle_gate_request_id"]) for r in rows])
        if any(r["trip_id"] for r in rows):
            trips = await fetch_tenant_trips(tenant_id, pool)
            trips_by_id = {str(t.id): t for t in trips}
    else:
        from api.core.aa544_tenant_pool import REVIEWS_FLAG, acquire_scoped_conn
        async with acquire_scoped_conn(
            request, str(tenant_id) if tenant_id else "", REVIEWS_FLAG, fail_closed=True,
        ) as (conn, used_tenant_pool):
            logger.info("aa859_reviews_list", tenant_id=str(tenant_id), used_tenant_pool=used_tenant_pool)
            rows = await conn.fetch(_TENANT_REVIEWS_QUERY, tenant_id, str(tenant_id))
            if any(r["trip_id"] for r in rows):
                trips_by_id = {str(t.id): t for t in await fetch_tenant_trips_on(conn, tenant_id)}
            served = await _tl.request_served_states(conn, tenant_id, [str(r["angle_gate_request_id"]) for r in rows])

    items = []
    for r in rows:
        goal_obj = get_goal(r["goal"]) if r["goal"] else None
        ready_state = _READY_STATE_MAP.get(r["status"], "not_ready")
        content_text = r["content_text"] if r["status"] in ("approved", "held") else None

        tour_context = None
        trip_id = str(r["trip_id"]) if r["trip_id"] else None
        if trip_id and trip_id in trips_by_id:
            trip = trips_by_id[trip_id]
            tour_context = {"name": trip.name, "destination": trip.destination}

        snapshot = r["dfs_paa_snapshot"]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)

        items.append({
            "request_id": str(r["angle_gate_request_id"]),
            "piece_id": str(r["piece_id"]),
            "channel": r["channel"],
            "ready_state": ready_state,
            "content_text": content_text,
            # AA-519 Việc 4/5 — same fields/conventions as fetch_review() above.
            "route_hub_name": r["route_hub_name"],
            "route_segment_count": r["route_segment_count"],
            "goal": (
                {"key": r["goal"], "label": goal_obj["name"] if goal_obj else r["goal"]}
                if r["goal"] else None
            ),
            "angle": (
                {
                    "name": r["angle_name"], "why_it_works": r["angle_why_it_works"],
                    "formula_fit": r["angle_formula_fit"], "best_final_style": r["angle_best_final_style"],
                } if r["angle_name"] else None
            ),
            "atom": (
                {
                    "text": r["atom_text"], "activity_type": r["atom_activity_type"],
                    "emotional_hook": r["atom_emotional_hook"], "season_note": r["atom_season_note"],
                } if r["atom_text"] else None
            ),
            "tour": tour_context,
            "dfs_paa_snapshot": snapshot,
            "cta": r["cta"],
            "created_at": r["created_at"].isoformat(),
        })
        _tl.with_served(items[-1], served.get(str(r["angle_gate_request_id"])))   # AA-986 L3
    return items


__all__ = [
    "ContentWritingError", "RequestNotReadyError", "MissingCTAError", "MAX_ATTEMPTS",
    "start_write", "run_write_background", "fetch_piece", "fetch_latest_piece_for_request",
    "fetch_review", "fetch_review_list", "update_piece_content_text",
]
