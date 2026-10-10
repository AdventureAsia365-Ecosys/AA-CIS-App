"""AA-695 (GO item 1, S223) — transit / unnamed-place atoms stay atoms but never form or join a
Segment, EXCEPT a rule-"transit" moment ranking rescues as an experience by its `activity_type`
(AA-694's `exclusion_after_pick`/`resolve_exclusions`; 199 live Segments ride on that). The
exclusion is one small named function, `forms_segment()`, with its Segment-level counterpart
`_existing_forms_segment()` for the join-candidate pseudo-atoms. Both reuse
`atom_ranking.atoms_say_transit`, not a copy.

Contract: an atom is excluded only when `classify_exclusion(place, action) == "unnamed_place"`,
or when it is `"transit"` by rule AND the atom's own `activity_type` is `"transit"` or NULL. A
rule-transit atom typed anything else (culture/trek/stay/food/bike/other/…) still forms/joins
Segments — ranking keeps deciding it.
"""
from services.acp_contract.segment_matching import (
    _existing_forms_segment, forms_segment, SegmentAtom,
)


def _atom(place, action, activity_type=None):
    return SegmentAtom("a1", "tour1", 1, place, action, "Japan", activity_type)


# ── forms_segment(): the four cases the contract names ─────────────────────────────────────

def test_unnamed_place_atom_never_forms_segment():
    """`classify_exclusion == "unnamed_place"` excludes regardless of activity_type — a place
    that names a kind of place, not somewhere, is never a Segment."""
    assert forms_segment(_atom("a nearby hot spring", "soak quietly")) is False
    # Even typed as a real experience, an unnamed place is still excluded (place rule wins).
    assert forms_segment(_atom("the trailhead", "set off on the trek", "trek")) is False


def test_transit_rule_transit_typed_atom_excluded():
    """Transit by rule AND the atom's own type agrees ("transit") — excluded."""
    assert forms_segment(_atom("the station", "arrive at the station", "transit")) is False


def test_transit_rule_untyped_atom_excluded():
    """Transit by rule AND no type at all (NULL) — excluded; a NULL type offers no second
    opinion, so the rule verdict stands (`atoms_say_transit([])` is False, so an untyped atom
    does not vote itself out of transit)."""
    assert forms_segment(_atom("the station", "arrive at the station", None)) is False


def test_transit_rule_but_experience_typed_atom_kept():
    """Transit by rule but typed something else (`culture`) — kept. This is the rescue AA-694's
    ranking makes: "Nanta — attend a nonverbal show" is a show, not transit. `atoms_say_transit`
    (reused, not copied) does not call a single `culture` atom transit."""
    assert forms_segment(_atom("Nanta theatre", "attend a nonverbal show", "culture")) is True
    assert forms_segment(_atom("Negombo beach", "attend a private candle-lit dinner", "food")) is True


def test_normal_named_moment_forms_segment():
    """A named place with a real (non-transit) action forms a Segment, typed or not."""
    assert forms_segment(_atom("Fushimi Inari shrine", "walk through the torii gates")) is True
    assert forms_segment(_atom("Fushimi Inari shrine", "walk through the torii gates", "culture")) is True


# ── _existing_forms_segment(): the Segment-level join-candidate counterpart ─────────────────

def _row(place, action, member_types):
    return {"canonical_place": place, "canonical_action": action, "member_types": member_types}


def test_existing_unnamed_segment_not_a_candidate():
    assert _existing_forms_segment(_row("a nearby hot spring", "soak", ["other"])) is False


def test_existing_transit_segment_with_transit_members_not_a_candidate():
    assert _existing_forms_segment(_row("the station", "arrive at the station", ["transit"])) is False


def test_existing_transit_segment_with_no_typed_members_not_a_candidate():
    """Transit by rule and no member is typed (`atoms_say_transit([])` False, no second opinion)
    — skipped, matching ranking's verdict."""
    assert _existing_forms_segment(_row("the station", "arrive at the station", [])) is False
    assert _existing_forms_segment(_row("the station", "arrive at the station", None)) is False


def test_existing_transit_segment_rescued_by_member_types_stays_a_candidate():
    """A rule-transit Segment whose typed members mostly say otherwise is a rescued experience —
    it stays a join candidate (the 199 live Segments)."""
    assert _existing_forms_segment(_row("Nanta theatre", "attend a nonverbal show", ["culture"])) is True
    # Majority rule: `atoms_say_transit` needs MOST typed members transit to exclude.
    assert _existing_forms_segment(
        _row("Nanta theatre", "attend a nonverbal show", ["culture", "culture", "transit"])
    ) is True


def test_existing_transit_segment_mostly_transit_members_not_a_candidate():
    """When most typed members ARE transit, `atoms_say_transit` is true — excluded."""
    assert _existing_forms_segment(
        _row("the station", "arrive at the station", ["transit", "transit", "culture"])
    ) is False


def test_existing_named_moment_is_a_candidate():
    assert _existing_forms_segment(_row("Fushimi Inari shrine", "walk the torii gates", ["culture"])) is True
    assert _existing_forms_segment(_row("Fushimi Inari shrine", "walk the torii gates", [])) is True
