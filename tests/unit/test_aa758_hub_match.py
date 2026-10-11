"""AA-758 — hub reuse match (best_hub_match). Regression tests for the 862-on-one / 405-orphan
bug: the old overlap denominator was min(|a|,|b|), which let any single-tour hub score 1.0 and
collapse unrelated families onto one hub. The fix is Jaccard over the UNION. Pure-function, no DB.
"""

from services.acp_contract.route_detection import SHARED_ENOUGH, best_hub_match


def test_single_tour_hub_no_longer_scores_a_perfect_match():
    """THE BUG: a hub that covers only {t1}, matched against a 10-tour family that happens to
    include t1. Old min() denominator → 1/1 = 1.0 (reuse). Jaccard over union → 1/10 = 0.1,
    well below SHARED_ENOUGH, so it is NOT reused."""
    member = {f"t{i}" for i in range(10)}  # 10-tour family, includes t0
    old = {"hub-single": {"t0"}}
    hub_id, ratio = best_hub_match(member, old)
    assert hub_id == "hub-single"            # it is the only candidate, so it's returned...
    assert ratio == 0.1                       # ...but with its TRUE ratio
    assert ratio < SHARED_ENOUGH              # so the caller will NOT reuse it


def test_genuine_overlap_clears_threshold():
    member = {"t1", "t2", "t3"}
    old = {"hub-real": {"t1", "t2", "t3"}}    # identical set → Jaccard 1.0
    hub_id, ratio = best_hub_match(member, old)
    assert hub_id == "hub-real"
    assert ratio == 1.0


def test_jaccard_is_over_the_union_not_the_smaller_set():
    # family {t1,t2,t3,t4}, hub {t1,t2} → inter 2, union 4 → 0.5 (min() would give 2/2 = 1.0)
    member = {"t1", "t2", "t3", "t4"}
    old = {"hub": {"t1", "t2"}}
    _hub_id, ratio = best_hub_match(member, old)
    assert ratio == 0.5


def test_no_overlap_returns_none():
    member = {"t1", "t2"}
    old = {"hub-x": {"t9", "t8"}}
    hub_id, ratio = best_hub_match(member, old)
    assert hub_id is None and ratio == 0.0


def test_empty_old_hubs_returns_none():
    assert best_hub_match({"t1"}, {}) == (None, 0.0)


def test_best_of_several_is_highest_jaccard():
    member = {"t1", "t2", "t3"}
    old = {
        "weak":   {"t1", "x1", "x2", "x3"},   # inter 1, union 6 → 0.1666
        "strong": {"t1", "t2", "t3", "t4"},   # inter 3, union 4 → 0.75
        "none":   {"z1"},                      # 0
    }
    hub_id, ratio = best_hub_match(member, old)
    assert hub_id == "strong"
    assert round(ratio, 2) == 0.75


def test_tie_break_is_deterministic_not_dict_order():
    """Two hubs with the EXACT same ratio and overlap must resolve by hub_id, so the result is
    stable regardless of insertion order (the old code kept the first on strict >, so one
    arbitrary hub won every tie)."""
    member = {"t1", "t2"}
    old_a = {"hub-b": {"t1", "t2"}, "hub-a": {"t1", "t2"}}
    old_b = {"hub-a": {"t1", "t2"}, "hub-b": {"t1", "t2"}}
    assert best_hub_match(member, old_a) == best_hub_match(member, old_b)
    # higher hub_id wins the tuple comparison (deterministic, order-independent)
    assert best_hub_match(member, old_a)[0] == "hub-b"


def test_one_dominant_hub_cannot_absorb_an_unrelated_family():
    """Regression for the 862-on-one symptom: a huge hub covering many tours must NOT win against
    a small family it shares only a sliver with."""
    big_hub_tours = {f"b{i}" for i in range(60)}
    member = {"n1", "n2", "b0"}               # shares exactly 1 tour with the 60-tour hub
    old = {"big": big_hub_tours}
    hub_id, ratio = best_hub_match(member, old)
    # inter 1, union 62 → ~0.016, far below SHARED_ENOUGH → not reused
    assert ratio < SHARED_ENOUGH
