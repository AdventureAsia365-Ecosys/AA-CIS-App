"""AA-1059 — `route_detection.route_gap_reason`: why one tour gets no Route from `derive_routes` (pure)."""
import pytest

from services.acp_contract.route_detection import ROUTE_GAP_REASONS, Moment, derive_routes, route_gap_reason


def _m(seg, day, place):
    return Moment(segment_id=seg, tour_id="tour-zz", day=day, place=place)


@pytest.mark.parametrize("moments,reason", [
    ([], "no_ranked_moment"),
    ([_m("s1", None, "Zzlo"), _m("s2", None, "Qarvi")], "no_day"),
    ([_m("s1", 3, "Zzlo"), _m("s2", 3, "Qarvi")], "one_day"),
    ([_m("s1", 1, "Zzlo"), _m("s2", 3, "Qarvi"), _m("s3", 5, "Brenn")], "no_consecutive_days"),
    ([_m("s1", 1, "Zzlo"), _m("s2", 2, "Zzlo")], "one_place"),
    ([_m("s1", 1, "Zzlo"), _m("s2", 2, "Qarvi")], "derivable"),
    # an undated moment beside a dated journey does not hide the journey
    ([_m("s0", None, "Brenn"), _m("s1", 1, "Zzlo"), _m("s2", 2, "Qarvi")], "derivable"),
])
def test_reason_per_case(moments, reason):
    assert route_gap_reason(moments) == reason
    assert reason in ROUTE_GAP_REASONS


def test_derivable_matches_derive_routes():
    ms = [_m("s1", 1, "Zzlo"), _m("s2", 2, "Qarvi"), _m("s3", 4, "Brenn")]
    assert bool(derive_routes(ms)) is (route_gap_reason(ms) == "derivable")


def test_a_segment_counted_on_its_first_day_only_can_break_the_run():
    """The stage keeps one moment per Segment at its FIRST day; a Segment the tour visits again later leaves that
    later day empty. Counting every day (the diagnostic's what-if) can then form a Route."""
    first_only = [_m("base", 1, "Zzlo"), _m("s3", 3, "Qarvi")]
    every_day = first_only + [_m("base", 2, "Zzlo")]
    assert route_gap_reason(first_only) == "no_consecutive_days"
    assert route_gap_reason(every_day) == "derivable"
