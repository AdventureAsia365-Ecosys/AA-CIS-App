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
  2. Only members whose place IS the Subject's place (`canonical_place`): the same normalised
     name form (`place_cards.name_forms`: accent fold, leading "the"/"a" dropped, a parenthetical
     is its own form), the same core name for a non-generic name ("Lake Khövsgöl" = "Khövsgöl
     Lake"), or the same place card (an alias of the Subject's place in that country, or the
     member atom's own `atom_place_card` link). Never a substring: "Gyeongju National Museum" is
     not "Gyeongju".
  3. Those members must share one country. Several countries (a still-mixed Segment with a
     generic name, "National Museum" in Bhutan and Laos) is ambiguous: no seed.
  4. Best member: exact name form first, then one with `evidence` (a richer seed — lessons:
     "Pawangka — visit" alone made the writer invent), then the lowest atom_id only as the last,
     deterministic tie-break.
No match → `(None, reason)`; the caller does not offer / does not pick the Subject and logs why.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from services.acp_shared.place_cards import core_key, fold, is_generic_name, name_forms, place_norm

SKIP_NO_MEMBER = "no_live_member"
SKIP_NO_TENANT_MEMBER = "no_member_on_tenant_tours"
SKIP_NO_PLACE_MATCH = "no_member_matches_subject_place"
SKIP_SEVERAL_COUNTRIES = "subject_place_in_several_countries"


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


def place_matches(subject_place: Optional[str], member: Member,
                  alias_cards: Optional[Mapping[tuple[str, str], str]] = None) -> tuple[bool, bool]:
    """(matches, exact) — whether `member` is about the Subject's place, and whether the match is the
    same written name (exact) rather than a core-name / place-card match.

    `alias_cards`: {(alias_norm, country_norm): card_id} from `acp_shared.place_card_alias` for the
    Subject's name forms (non-rejected cards only)."""
    if not (subject_place or "").strip() or not (member.place or "").strip():
        return False, False
    subject_forms, member_forms = _forms(subject_place), _forms(member.place)
    if place_norm(subject_place) == place_norm(member.place):
        return True, True
    if subject_forms & member_forms:
        return True, False
    if (not is_generic_name(subject_place) and not is_generic_name(member.place)
            and core_key(subject_place) and core_key(subject_place) == core_key(member.place)):
        return True, False
    if alias_cards:
        country = fold(member.country)
        subject_cards = {alias_cards[(f, country)] for f in subject_forms if (f, country) in alias_cards}
        if subject_cards:
            if member.card_id and str(member.card_id) in subject_cards:
                return True, False
            member_cards = {alias_cards[(f, country)] for f in member_forms if (f, country) in alias_cards}
            if member_cards & subject_cards:
                return True, False
    return False, False


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
    matched: list[tuple[Member, bool]] = []
    for m in live:
        ok, exact = place_matches(subject_place, m, alias_cards)
        if ok:
            matched.append((m, exact))
    if not matched:
        return None, SKIP_NO_PLACE_MATCH
    countries = {fold(m.country) for m, _ in matched if fold(m.country)}
    if len(countries) > 1:
        return None, SKIP_SEVERAL_COUNTRIES
    best = min(matched, key=lambda me: (not me[1], not (me[0].evidence or "").strip(), me[0].atom_id))
    return best[0], None


# ── DB reads (the only impure part) ───────────────────────────────────────────────────────────

# Live (active tour, not deleted, not an empty marker), non-alias members of the given Segments,
# with the tour's country and the atom's place card. owner_scope as AA-567: platform or this tenant.
_MEMBERS_SQL = """
    SELECT m.segment_id, ta.atom_id, ta.tour_id::text AS tour_id, ta.place, ta.evidence,
           rt.country, apc.card_id::text AS card_id
    FROM acp_contract.atom_segment_member m
    JOIN acp_contract.v_active_tour_atoms ta ON ta.atom_id = m.atom_id
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = ta.tour_id
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
        rows = await conn.fetch(_ALIAS_SQL, forms)
    except Exception:  # the alias table is an aid, never a reason to fail a Slate read or a pick
        return {}
    return {(r["alias_norm"], r["country_norm"]): r["card_id"] for r in rows}


__all__ = [
    "Member", "place_matches", "choose_representative", "load_segment_members", "load_alias_cards",
    "SKIP_NO_MEMBER", "SKIP_NO_TENANT_MEMBER", "SKIP_NO_PLACE_MATCH", "SKIP_SEVERAL_COUNTRIES",
]
