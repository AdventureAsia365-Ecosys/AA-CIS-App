"""AA-857 — which atom a Slate Subject is written from (its "seed"), and when a Subject has none.

S239: a tenant picked the Subject "Gyeongju" (South Korea) and T9 wrote about the Lok Virsa Museum
in Islamabad; "National Library" (Bhutan) produced a post about the Zorig Chusum craft school. The
Segment behind each Subject held other places (pre-S218 Segments mixed countries; a Segment also
groups similarly-worded places of one country), and `_resolve_representative_atom()` took
`ORDER BY atom_id LIMIT 1` — whichever member sorted first.

The rule here (pure, unit-tested; the DB reads live in `load_segment_members()`):
  1. Only members on the tenant's own tours (the Slate scored the Segment on those tours —
     `_fetch_segment_candidates()` — so a seed from another tenant's tour would be a place this
     tenant does not sell). `tenant_tour_ids=None` skips this filter.
  2. Only members whose place IS the Subject's place (`canonical_place`), graded by how sure the
     match is (`match_grade`, lower is surer):
       0 GRADE_EXACT   same normalised name (`place_cards.place_norm`: accent fold, leading
                       "the"/"a" dropped);
       1 GRADE_FORM    the same whole or base name form (a name without its parenthetical, or the
                       Subject's own parenthetical: "Bayanzag (Flaming Cliffs)" ~ "Flaming Cliffs");
       2 GRADE_CORE    the same core name for a non-generic name ("Lake Khövsgöl" = "Khövsgöl
                       Lake"), or the same place card (an alias of the Subject's place in that
                       country, or the member atom's own `atom_place_card` link);
       3 GRADE_PAREN   only through the MEMBER's parenthetical ("Taktsang Monastery (Paro)" for
                       "Paro") — the parenthetical names where the place is, not the place;
       4 GRADE_TOKENS  loose fallback, only when nothing above matched: the place word sets are a
                       subset/superset of each other — the relation A3 itself groups a Segment by
                       (`segment_matching._looks_like_one_moment`), and `_canonical()` names the
                       Segment after its SHORTEST wording, so a tenant whose tour says "Bukchon
                       Hanok Village" sits under "Bukchon". Never for a generic Subject or member
                       name ("National Museum"), and only when every such member is in one KNOWN
                       country. "Lok Virsa Museum" shares no word with "Gyeongju".
  3. The matched members of the best tier (grades 0-3 together, else grade 4) must share one
     country. Several countries (a still-mixed Segment with a generic name, "National Museum" in
     Bhutan and Laos) is ambiguous: no seed.
  4. Best member: lowest grade first, then one with `evidence` (a richer seed — lessons:
     "Pawangka — visit" alone made the writer invent), then the lowest atom_id only as the last,
     deterministic tie-break. Grade before evidence, so a with-evidence "Taktsang Monastery (Paro)"
     never beats an evidence-less "Paro Town" for the Subject "Paro".
No match → `(None, reason)`; the caller does not offer / does not pick the Subject and logs why.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from services.acp_contract.segment_matching import CONNECTORS
from services.acp_shared.place_cards import core_key, fold, is_generic_name, name_forms, place_norm
from shared.tour_liveness import served_tour_joins_sql

SKIP_NO_MEMBER = "no_live_member"
SKIP_NO_TENANT_MEMBER = "no_member_on_tenant_tours"
SKIP_NO_PLACE_MATCH = "no_member_matches_subject_place"
SKIP_SEVERAL_COUNTRIES = "subject_place_in_several_countries"
SKIP_LOOSE_COUNTRY_UNKNOWN = "loose_place_match_country_unknown"

GRADE_EXACT, GRADE_FORM, GRADE_CORE, GRADE_PAREN, GRADE_TOKENS = 0, 1, 2, 3, 4
_STRONG = GRADE_PAREN  # grades 0..3 are a name/card match; 4 is the loose word-set fallback
_PARENTHETICAL = re.compile(r"\([^)]*\)")  # as place_cards.name_forms() splits a name


@dataclass(frozen=True)
class Member:
    """One live, non-alias member atom of a Segment, as the seed rule sees it."""

    atom_id: str
    tour_id: str
    place: Optional[str]
    country: Optional[str] = None
    evidence: Optional[str] = None
    card_id: Optional[str] = None


def _forms(name: Optional[str]) -> set[str]:
    return name_forms(name)


def _base_forms(name: Optional[str]) -> set[str]:
    """The whole name and the name without its parenthetical — not the parenthetical alone."""
    return {f for f in (place_norm(name), place_norm(_PARENTHETICAL.sub(" ", name or ""))) if f}


def _place_words(name: Optional[str]) -> frozenset[str]:
    """A place's word set as A3 compares it (`segment_matching._place_tokens`: connectors dropped
    unless that leaves nothing), over the accent-folded text so "Khövsgöl" is one word."""
    words = fold(name).split()
    kept = [w for w in words if w not in CONNECTORS]
    return frozenset(kept or words)


def _strong_grade(subject_place: str, member: Member,
                  alias_cards: Optional[Mapping[tuple[str, str], str]]) -> Optional[int]:
    if place_norm(subject_place) == place_norm(member.place):
        return GRADE_EXACT
    subject_forms = _forms(subject_place)
    if subject_forms & _base_forms(member.place):
        return GRADE_FORM
    if (not is_generic_name(subject_place) and not is_generic_name(member.place)
            and core_key(subject_place) and core_key(subject_place) == core_key(member.place)):
        return GRADE_CORE
    if alias_cards:
        country = fold(member.country)
        subject_cards = {alias_cards[(f, country)] for f in subject_forms if (f, country) in alias_cards}
        if subject_cards:
            if member.card_id and str(member.card_id) in subject_cards:
                return GRADE_CORE
            member_cards = {alias_cards[(f, country)] for f in _forms(member.place)
                            if (f, country) in alias_cards}
            if member_cards & subject_cards:
                return GRADE_CORE
    if subject_forms & _forms(member.place):  # only through the member's own parenthetical
        return GRADE_PAREN
    return None


def _loose_match(subject_place: str, member: Member) -> bool:
    """The A3 grouping relation: one place's word set contains the other's. Non-generic only."""
    if is_generic_name(subject_place) or is_generic_name(member.place):
        return False
    one, two = _place_words(subject_place), _place_words(member.place)
    return bool(one and two) and (one <= two or two <= one)


def match_grade(subject_place: Optional[str], member: Member,
                alias_cards: Optional[Mapping[tuple[str, str], str]] = None) -> Optional[int]:
    """How surely `member` is about the Subject's place (GRADE_*; lower is surer), or None.

    `alias_cards`: {(alias_norm, country_norm): card_id} from `acp_shared.place_card_alias` for the
    Subject's and the members' name forms (non-rejected cards only)."""
    if not (subject_place or "").strip() or not (member.place or "").strip():
        return None
    grade = _strong_grade(subject_place, member, alias_cards)
    if grade is not None:
        return grade
    return GRADE_TOKENS if _loose_match(subject_place, member) else None


def place_matches(subject_place: Optional[str], member: Member,
                  alias_cards: Optional[Mapping[tuple[str, str], str]] = None) -> tuple[bool, bool]:
    """(matches, exact) for a name / place-card match (grades 0-3). The loose word-set fallback
    (GRADE_TOKENS) is not a match here: it only seeds a Subject when no member matches by name."""
    grade = match_grade(subject_place, member, alias_cards)
    if grade is None or grade > _STRONG:
        return False, False
    return True, grade == GRADE_EXACT


def choose_representative(
    subject_place: Optional[str], members: Iterable[Member], *,
    tenant_tour_ids: Optional[set[str]] = None,
    alias_cards: Optional[Mapping[tuple[str, str], str]] = None,
) -> tuple[Optional[Member], Optional[str]]:
    """The member atom a Subject is written from, or `(None, skip_reason)`. See module docstring."""
    live = list(members)
    if not live:
        return None, SKIP_NO_MEMBER
    if tenant_tour_ids is not None:
        live = [m for m in live if str(m.tour_id) in tenant_tour_ids]
        if not live:
            return None, SKIP_NO_TENANT_MEMBER
    graded = [(m, g) for m in live if (g := match_grade(subject_place, m, alias_cards)) is not None]
    strong = [(m, g) for m, g in graded if g <= _STRONG]
    if strong:
        matched = strong
        if len({fold(m.country) for m, _ in matched if fold(m.country)}) > 1:
            return None, SKIP_SEVERAL_COUNTRIES
    else:
        matched = graded
        if not matched:
            return None, SKIP_NO_PLACE_MATCH
        countries = {fold(m.country) for m, _ in matched}
        if "" in countries:
            return None, SKIP_LOOSE_COUNTRY_UNKNOWN
        if len(countries) > 1:
            return None, SKIP_SEVERAL_COUNTRIES
    best = min(matched, key=lambda mg: (mg[1], not (mg[0].evidence or "").strip(), mg[0].atom_id))
    return best[0], None


def choose_route_anchor(place: Optional[str], members: Iterable[Member], route_tour: str) -> Optional[Member]:
    """The atom-grain anchor of a Route Subject (AA-857): among the Route's first Segment's members on
    the Route's own tour (one tour = one country), the member that is the Segment's place, else any
    live member of that tour (evidence first, then lowest atom_id). T9 seeds a Route from every
    Segment along the walk, so this atom is only the anchor. Shared by `slate.pick_subject()` and the
    admin seed view (AA-863)."""
    on_tour = [m for m in members if str(m.tour_id) == route_tour]
    member, _skip = choose_representative(place, on_tour, tenant_tour_ids={route_tour})
    if member is None and on_tour:
        member = min(on_tour, key=lambda m: (not (m.evidence or "").strip(), m.atom_id))
    return member


# ── DB reads (the only impure part) ───────────────────────────────────────────────────────────

# Live (active tour, not deleted, not an empty marker), non-alias members of the given Segments,
# with the tour's country and the atom's place card. owner_scope as AA-567: platform or this tenant.
# AA-859 wave 4 (C9) — the rule of acp_contract.v_active_tour_atoms (migration 203: a live master tour, not
# deleted, not an empty marker) inlined on the base table, the 225 / 253 / 256 precedent: the view runs with its
# owner's rights (RLS bypassed) and 241 revoked the tenant role on it, while the pick runs on the tenant pool
# (SUBJECT_DECIDE_FLAG), where 266's policies on tour_atoms and 239's master read on published_tours apply.
# Same rows as the view for the main pool and the worker (cis_app reads every row of both tables).
# AA-986 L1 — the rule now also requires a live source (CIS 271 adds it to the view): the served-tour joins come
# from shared.tour_liveness, the one copy of the rule, instead of a hand copy here. The tenant pool reads raw_tours
# through 239's AA-master read policy, as it did for `rt.country` before.
_MEMBERS_SQL = f"""
    SELECT m.segment_id, ta.atom_id, ta.tour_id::text AS tour_id, ta.place, ta.evidence,
           rt.country, apc.card_id::text AS card_id
    FROM acp_contract.atom_segment_member m
    JOIN acp_contract.tour_atoms ta ON ta.atom_id = m.atom_id
    {served_tour_joins_sql("ta.tour_id")}
    LEFT JOIN acp_shared.atom_place_card apc ON apc.atom_id = ta.atom_id
    WHERE m.segment_id = ANY($1::text[]) AND NOT m.is_alias
      AND ta.owner_scope IN ('platform', $2::text) AND NOT ta.deleted AND NOT ta.is_empty_marker
"""

_ALIAS_SQL = """
    SELECT a.alias_norm, a.country_norm, a.card_id::text AS card_id
    FROM acp_shared.place_card_alias a
    JOIN acp_shared.place_card pc ON pc.card_id = a.card_id
    WHERE a.alias_norm = ANY($1::text[]) AND pc.status <> 'rejected'
"""


async def load_segment_members(conn, segment_ids: list[str], tenant_id: str) -> dict[str, list[Member]]:
    """{segment_id: [Member]} for the given Segments (one query)."""
    if not segment_ids:
        return {}
    rows = await conn.fetch(_MEMBERS_SQL, list(segment_ids), str(tenant_id))
    out: dict[str, list[Member]] = {}
    for r in rows:
        out.setdefault(r["segment_id"], []).append(Member(
            atom_id=r["atom_id"], tour_id=r["tour_id"], place=r["place"], country=r["country"],
            evidence=r["evidence"], card_id=r["card_id"]))
    return out


async def load_alias_cards(conn, places: Iterable[Optional[str]]) -> dict[tuple[str, str], str]:
    """{(alias_norm, country_norm): card_id} for every name form of `places` and of nothing else —
    `place_matches()` looks up both the Subject's forms and a member's forms, so callers pass both."""
    forms = sorted({f for p in places for f in _forms(p)})
    if not forms:
        return {}
    try:
        if _in_transaction(conn):
            # AA-859 wave 4 (C10) — a failed statement aborts the caller's whole transaction (the pick on the
            # tenant pool): a savepoint keeps the catch below from leaving it unusable.
            async with conn.transaction():
                rows = await conn.fetch(_ALIAS_SQL, forms)
        else:
            rows = await conn.fetch(_ALIAS_SQL, forms)
    except Exception:  # the alias table is an aid, never a reason to fail a Slate read or a pick
        return {}
    return {(r["alias_norm"], r["country_norm"]): r["card_id"] for r in rows}


def _in_transaction(conn) -> bool:
    """True for an asyncpg connection (or pool proxy) inside a transaction. Looked up on the class, so a test
    double without the method (or a mock that would answer anything) counts as "not in a transaction"."""
    check = getattr(type(conn), "is_in_transaction", None)
    if not callable(check):
        return False
    try:
        return check(conn) is True
    except Exception:
        return False


__all__ = [
    "Member", "match_grade", "place_matches", "choose_representative", "choose_route_anchor",
    "load_segment_members",
    "load_alias_cards", "SKIP_NO_MEMBER", "SKIP_NO_TENANT_MEMBER", "SKIP_NO_PLACE_MATCH",
    "SKIP_SEVERAL_COUNTRIES", "SKIP_LOOSE_COUNTRY_UNKNOWN", "GRADE_EXACT", "GRADE_FORM", "GRADE_CORE",
    "GRADE_PAREN", "GRADE_TOKENS",
]
