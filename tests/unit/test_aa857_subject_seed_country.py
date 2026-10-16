"""AA-857 — a Slate Subject is seeded from an atom about ITS place (and country), and A3 Segments
never span two countries, even through an unknown-country or pseudo-Segment bridge.

Fixtures mirror the S239 live evidence (.tmp-session/s239/d1_subject_atoms_out.json): segment
d8aa8991e02d6754 "Gyeongju" (37 atoms, 6 countries; ORDER BY atom_id picked a Pakistan museum) and
8cc3b252cddaa88f "National Library" (Library + Traditional Arts & Craft School, Bhutan)."""
import asyncio

import pytest

from services.acp_contract import segment_matching as sm
from services.acp_contract.segment_matching import (
    MixedCountrySegmentError, SegmentAtom, assert_one_country, derive_segments, known_countries,
)
from services.acp_shared import slate
from services.acp_shared.subject_seed import (
    GRADE_CORE, GRADE_FORM, GRADE_PAREN, GRADE_TOKENS, SKIP_LOOSE_COUNTRY_UNKNOWN, SKIP_NO_MEMBER,
    SKIP_NO_PLACE_MATCH, SKIP_NO_TENANT_MEMBER, SKIP_SEVERAL_COUNTRIES, Member, choose_representative,
    match_grade, place_matches,
)

T_KR, T_BT, T_PK, T_LA = "tour-kr", "tour-bt", "tour-pk", "tour-la"


def _m(atom_id, place, country, tour, evidence=None, card_id=None):
    return Member(atom_id=atom_id, tour_id=tour, place=place, country=country, evidence=evidence,
                  card_id=card_id)


GYEONGJU = [
    _m("021bfcdfd62b82c1", "Lok Virsa Folk & Heritage Museum", "Pakistan", T_PK),
    _m("051a625114a0dbc0", "National Museum", "Bhutan", T_BT),
    _m("16d670be2a5a3074", "Folk Heritage Museum", "Bhutan", T_BT),
    _m("59052423fc6c005f", "National Museum", "Laos", T_LA),
    _m("7b6ce326871b5b83", "Gyeongju", "South Korea", T_KR, evidence="Explore the Silla tombs."),
    _m("ec646f06fedeb8dc", "Gyeongju National Museum", "South Korea", T_KR),
]

NATIONAL_LIBRARY = [
    _m("00480b6b9d0bfedd", "Traditional Arts & Craft School", "Bhutan", T_BT),
    _m("02458cef4b83b409", "National Library", "Bhutan", T_BT),
    _m("72de0908dd47d71e", "Traditional Arts and Craft School and National Library", "Bhutan", T_BT),
    _m("f916d98b8863050f", "The National Library", "Bhutan", T_BT,
       evidence="View manuscripts on Bhutanese Buddhism."),
]


# ── 1. representative atom ─────────────────────────────────────────────────────────────────────

def test_gyeongju_seed_is_the_gyeongju_atom_not_the_lowest_atom_id():
    member, skip = choose_representative("Gyeongju", GYEONGJU)
    assert skip is None
    assert member.atom_id == "7b6ce326871b5b83"          # was 021bfcdfd62b82c1 (Pakistan) by atom_id
    assert member.country == "South Korea"


def test_national_library_seed_is_a_library_atom_with_evidence_first():
    member, skip = choose_representative("National Library", NATIONAL_LIBRARY)
    assert skip is None
    # "The National Library" folds to the same form ("the" dropped) and has evidence -> preferred
    # over the bare-label atom; never the craft school (the lowest atom_id).
    assert member.atom_id == "f916d98b8863050f"


def test_substring_is_not_a_name_match_only_the_loose_tier():
    # Not a name match; only the lowest (word-set) tier, which never outranks a name match.
    assert place_matches("Gyeongju", GYEONGJU[5]) == (False, False)          # Gyeongju National Museum
    assert match_grade("Gyeongju", GYEONGJU[5]) == GRADE_TOKENS
    assert place_matches("National Library", NATIONAL_LIBRARY[2])[0] is False
    kr = [_m("z1", "Gyeongju National Museum", "South Korea", T_KR, evidence="rich"),
          _m("z9", "Gyeongju", "South Korea", T_KR)]
    assert choose_representative("Gyeongju", kr)[0].atom_id == "z9"


# ── 1b. A3 wording: canonical_place is the SHORTEST member wording (review finding, S239) ───────

@pytest.mark.parametrize("subject,tenant_place", [
    ("Bukchon", "Bukchon Hanok Village"),
    ("Gyeongju", "Gyeongju Historic Area"),
    ("Hoi An", "Hoi An Ancient Town"),
    ("Angkor Wat", "Angkor Wat Temple"),
    ("Halong Bay", "Halong Bay cruise"),
])
def test_tenant_longer_wording_still_seeds_the_subject(subject, tenant_place):
    """A3 groups 'Bukchon' with 'Bukchon Hanok Village' (word-set subset) and names the Segment
    after the shorter one; a tenant whose tour says the longer one must still get the Subject."""
    members = [_m("a0", subject, "South Korea", "other-tenant-tour"),
               _m("a1", tenant_place, "South Korea", T_KR)]
    member, skip = choose_representative(subject, members, tenant_tour_ids={T_KR})
    assert skip is None and member.atom_id == "a1"


def test_tenant_shorter_wording_seeds_a_longer_subject_too():
    members = [_m("a1", "Bukchon", "South Korea", T_KR)]
    assert choose_representative("Bukchon Hanok Village", members)[0].atom_id == "a1"


def test_loose_tier_still_rejects_the_s239_wrong_places():
    assert match_grade("Gyeongju", GYEONGJU[0]) is None                       # Lok Virsa museum
    assert match_grade("National Library", NATIONAL_LIBRARY[0]) is None       # craft school
    member, skip = choose_representative("Gyeongju", GYEONGJU, tenant_tour_ids={T_PK, T_BT, T_LA})
    assert member is None and skip == SKIP_NO_PLACE_MATCH


def test_loose_tier_needs_one_known_country_and_a_non_generic_name():
    two = [_m("a1", "Bukchon Hanok Village", "South Korea", "t1"),
           _m("a2", "Bukchon Village", "Japan", "t2")]
    assert choose_representative("Bukchon", two) == (None, SKIP_SEVERAL_COUNTRIES)
    unknown = [_m("a1", "Bukchon Hanok Village", "South Korea", "t1"),
               _m("a2", "Bukchon Village", None, "t2")]
    assert choose_representative("Bukchon", unknown) == (None, SKIP_LOOSE_COUNTRY_UNKNOWN)
    # a generic Subject name names a different place in every city: no loose seed
    assert match_grade("National Museum", _m("a3", "National Museum Gyeongju", "South Korea", "t")) is None
    assert match_grade("Gyeongju National Museum", _m("a4", "National Museum", "South Korea", "t")) is None


# ── 1c. graded matches: a member's parenthetical ranks below a real name match ─────────────────

def test_member_parenthetical_ranks_below_core_name_even_with_evidence():
    monastery = _m("a1", "Taktsang Monastery (Paro)", "Bhutan", T_BT, evidence="Hike to Tiger's Nest.")
    town = _m("a9", "Paro Town", "Bhutan", T_BT)
    assert match_grade("Paro", monastery) == GRADE_PAREN
    assert match_grade("Paro", town) == GRADE_CORE
    member, skip = choose_representative("Paro", [monastery, town])
    assert skip is None and member.atom_id == "a9"
    # still a match when it is the only one (the place is named, as where the stop is)
    assert choose_representative("Paro", [monastery])[0].atom_id == "a1"


def test_subject_parenthetical_is_a_form_match():
    member = _m("a1", "Flaming Cliffs", "Mongolia", "t")
    assert match_grade("Bayanzag (Flaming Cliffs)", member) == GRADE_FORM


def test_only_the_tenants_own_tours_seed_the_subject():
    # terra-family sells only the Bhutan tour: "Gyeongju" has no member there about Gyeongju.
    member, skip = choose_representative("Gyeongju", GYEONGJU, tenant_tour_ids={T_BT})
    assert member is None and skip == SKIP_NO_PLACE_MATCH
    member, skip = choose_representative("Gyeongju", GYEONGJU, tenant_tour_ids={"other"})
    assert member is None and skip == SKIP_NO_TENANT_MEMBER
    member, skip = choose_representative("Gyeongju", GYEONGJU, tenant_tour_ids={T_KR, T_BT})
    assert member.atom_id == "7b6ce326871b5b83"


def test_same_place_in_two_countries_is_ambiguous():
    member, skip = choose_representative("National Museum", GYEONGJU)
    assert member is None and skip == SKIP_SEVERAL_COUNTRIES
    member, skip = choose_representative("National Museum", GYEONGJU, tenant_tour_ids={T_LA})
    assert member.atom_id == "59052423fc6c005f"


def test_no_members_and_no_place_match_are_skips():
    assert choose_representative("Gyeongju", []) == (None, SKIP_NO_MEMBER)
    assert choose_representative("Uran Togoo", GYEONGJU) == (None, SKIP_NO_PLACE_MATCH)


def test_alias_and_core_name_match():
    lake = _m("a1", "Khövsgöl Lake", "Mongolia", "t")
    assert place_matches("Lake Khovsgol", lake) == (True, False)               # core name
    ta_dzong = _m("a2", "Ta Dzong", "Bhutan", "t", card_id="card-nm")
    aliases = {("national museum of bhutan", "bhutan"): "card-nm"}
    assert place_matches("National Museum of Bhutan", ta_dzong, aliases) == (True, False)
    # the alias belongs to Bhutan: the same words on a Laos atom do not match through it
    assert place_matches("National Museum of Bhutan", _m("a3", "Ta Dzong", "Laos", "t", card_id="card-nm"),
                         aliases)[0] is False


def test_exact_name_beats_alias_match():
    members = [_m("a0", "Ta Dzong", "Bhutan", "t", evidence="x", card_id="card-nm"),
               _m("a9", "National Museum of Bhutan", "Bhutan", "t")]
    aliases = {("national museum of bhutan", "bhutan"): "card-nm"}
    member, _ = choose_representative("National Museum of Bhutan", members, alias_cards=aliases)
    assert member.atom_id == "a9"


# ── slate wiring: pick resolves through the rule; propose drops a Subject with no seed ──────────

class _Conn:
    """A fake asyncpg connection answering the three reads the Segment branch makes."""

    def __init__(self, place, members, tour_ids):
        self.place, self.members, self.tour_ids = place, members, tour_ids

    async def fetchval(self, sql, *args):
        assert "canonical_place" in sql
        return self.place

    async def fetch(self, sql, *args):
        if "atom_segment_member" in sql:
            return [{"segment_id": args[0][0], "atom_id": m.atom_id, "tour_id": m.tour_id,
                     "place": m.place, "evidence": m.evidence, "country": m.country,
                     "card_id": m.card_id} for m in self.members]
        if "place_card_alias" in sql:
            return []
        if "tenant_tour_versions" in sql:
            return [{"tour_id": t} for t in self.tour_ids]
        raise AssertionError(sql)


def test_resolve_representative_atom_segment_branch_uses_the_place_rule():
    conn = _Conn("Gyeongju", GYEONGJU, [T_KR, T_BT, T_PK])
    out = asyncio.run(slate._resolve_representative_atom(conn, "tenant", "d8aa8991e02d6754", None))
    assert out == ("7b6ce326871b5b83", T_KR, None)


def test_resolve_representative_atom_returns_none_when_no_member_is_about_the_place():
    conn = _Conn("Gyeongju", GYEONGJU, [T_BT, T_PK])
    out = asyncio.run(slate._resolve_representative_atom(conn, "tenant", "d8aa8991e02d6754", None))
    assert out == (None, None, None)


def test_segment_candidates_without_a_seed_are_not_offered():
    conn = _Conn(None, GYEONGJU, [])
    rows = [{"segment_id": "d8aa8991e02d6754", "canonical_place": "Gyeongju"}]
    assert asyncio.run(slate._segments_with_seed("tenant", conn, rows, {T_BT})) == []
    assert asyncio.run(slate._segments_with_seed("tenant", conn, rows, {T_KR})) == rows


# ── 2. A3 Segment: never two countries ─────────────────────────────────────────────────────────

def _a(atom_id, place, country, action="visit", tour="t1"):
    return SegmentAtom(atom_id, tour, 1, place, action, country)


def test_unknown_country_atom_cannot_bridge_two_countries():
    """The S239 shape: a Korean and a Bhutan museum both match an unknown-country 'National
    Museum'. The pair veto alone let union-find glue all three together."""
    atoms = [_a("a_kr", "National Museum of Korea", "South Korea"),
             _a("b_unknown", "National Museum", ""),
             _a("c_bt", "National Museum", "Bhutan")]
    segments = derive_segments(atoms)
    by_id = {a.atom_id: a for a in atoms}
    assert all(len(known_countries(s.atom_ids, by_id)) <= 1 for s in segments)
    assert sorted(len(s.atom_ids) for s in segments) == [1, 2]


def test_pseudo_segment_with_unknown_country_cannot_bridge():
    """Incremental run: an existing Segment's pseudo-atom (no country) and a new atom of another
    country must not pull a third country's atom into the same Segment."""
    atoms = [_a("__existing_segment__s1", "Folk Heritage Museum", ""),
             _a("n_bt", "Folk Heritage Museum", "Bhutan"),
             _a("n_pk", "Lok Virsa Folk Heritage Museum", "Pakistan")]
    segments = derive_segments(atoms, involving=frozenset({"n_bt", "n_pk"}))
    by_id = {a.atom_id: a for a in atoms}
    for s in segments:
        assert len(known_countries(s.atom_ids, by_id)) <= 1


def test_same_country_chain_still_joins():
    atoms = [_a("a", "National Museum of Korea", "South Korea"),
             _a("b", "National Museum", ""),
             _a("c", "National Museum", "South Korea")]
    assert len(derive_segments(atoms)) == 1


def test_component_guard_is_order_independent_of_input_list():
    atoms = [_a("a_kr", "National Museum of Korea", "South Korea"),
             _a("b_unknown", "National Museum", ""),
             _a("c_bt", "National Museum", "Bhutan")]
    assert derive_segments(atoms) == derive_segments(list(reversed(atoms)))


def test_assert_one_country_guard():
    by_id = {"x": _a("x", "p", "Bhutan"), "y": _a("y", "p", "bhutan "), "z": _a("z", "p", "Laos"),
             "u": _a("u", "p", "")}
    assert_one_country(["x", "y", "u"], by_id)
    with pytest.raises(MixedCountrySegmentError):
        assert_one_country(["x", "z"], by_id)


def test_connected_without_country_map_keeps_old_behaviour():
    keyed = sorted({a.atom_id: sm._key(a) for a in [
        _a("a", "National Museum of Korea", "South Korea"), _a("b", "National Museum", ""),
        _a("c", "National Museum", "Bhutan")]}.items())
    assert len(sm._connected(keyed)) == 1
