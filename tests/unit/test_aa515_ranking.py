"""AA-515: seed_builder multi-market resolver, ranking_reference exclusions, atom_ranking
rank-sum/demand/questions attribution. Pure-function tests only — no DB, no LLM, no HTTP."""

from services.acp_contract.atom_ranking import (
    Candidate,
    _candidate_questions,
    classify_exclusion,
    compute_demand,
    rank_segments,
)
from services.acp_contract.ranking_reference import is_transit, names_somewhere
from services.seo_intelligence.seed_builder import (
    DFS_LOCATION_MAP,
    LOCATION_CODE_TO_MARKET,
    resolve_buyer_market,
    resolve_buyer_markets,
)

# ── resolve_buyer_markets (AA-515) ──────────────────────────────────────────────────────────


def test_resolve_buyer_markets_returns_every_known_country():
    result = resolve_buyer_markets({"countries": ["AU", "UK", "US"], "language": "en"})
    assert [r[1] for r in result] == ["United States", "United Kingdom", "Australia"]
    assert all(r[2] == "en" for r in result)


def test_resolve_buyer_markets_de_fr_nl_no_longer_falls_back_to_us():
    result = resolve_buyer_markets({"countries": ["DE", "FR", "NL"], "language": "en"})
    assert {r[1] for r in result} == {"Germany", "France", "Netherlands"}
    assert "United States" not in {r[1] for r in result}


def test_resolve_buyer_markets_empty_falls_back_to_single_us():
    result = resolve_buyer_markets({"countries": []})
    assert result == [(2840, "United States", "en")]


def test_resolve_buyer_markets_dedupes_repeated_country():
    result = resolve_buyer_markets({"countries": ["US", "US", "UK"]})
    assert len(result) == 2


def test_resolve_buyer_market_singular_unchanged():
    # 3 real call sites (handler.py:58, admin_pipeline.py:2296/2507) still unpack exactly one
    # 3-tuple — resolve_buyer_market() itself must not have moved. Picks by MARKET_RANK
    # priority (US=1), not by list order — "UK" listed first does not win over "US".
    code, name, lang = resolve_buyer_market({"countries": ["UK", "US"]})
    assert (code, name, lang) == (2840, "United States", "en")


def test_location_code_to_market_reverse_lookup_complete():
    for market, (code, _name) in DFS_LOCATION_MAP.items():
        assert LOCATION_CODE_TO_MARKET[code] == market


# ── ranking_reference exclusions (ADR 0019/0020, ported) ───────────────────────────────────


def test_is_transit_arrival():
    assert is_transit("arrive at the station") is True


def test_is_transit_real_activity_not_excluded():
    assert is_transit("walk the Nakasendo trail") is False


def test_is_transit_bare_lodging_is_frame():
    assert is_transit("stay overnight") is True


def test_is_transit_lodging_naming_a_bed_is_not_frame():
    assert is_transit("stay overnight at an onsen ryokan") is False


def test_is_transit_bare_meal_is_frame():
    assert is_transit("have breakfast") is True


def test_is_transit_meal_naming_food_is_not_frame():
    assert is_transit("eat oysters and anago meshi at harbour-front restaurants") is False


def test_names_somewhere_real_place():
    assert names_somewhere("Kinkaku-ji") is True


def test_names_somewhere_bare_kind_is_not_a_place():
    assert names_somewhere("a nearby hotel") is False


def test_names_somewhere_day_of_week_market_is_not_a_place():
    assert names_somewhere("Sunday market") is False


def test_names_somewhere_qualifier_museum_still_names_somewhere():
    # "Museum of Northern People" — a real museum; "Northern"/"People" are qualifiers, not
    # kinds, and must not be read as kinds here (places.py's own worked example).
    assert names_somewhere("Museum of Northern People") is True


def test_classify_exclusion_transit():
    assert classify_exclusion("Nagoya Station", "arrive by train") == "transit"


def test_classify_exclusion_unnamed_place():
    assert classify_exclusion("a nearby hot spring", "soak") == "unnamed_place"


def test_classify_exclusion_none_for_real_segment():
    assert classify_exclusion("Kinkaku-ji", "visit") is None


# ── compute_demand / compute_questions (word-overlap claim-by-name, no embedding-match) ────


def test_compute_demand_claims_by_shared_words():
    rows = [("nakasendo trail", "US", 6600), ("magome to tsumago", "US", 210)]
    demand = compute_demand("Magome", "walk the Nakasendo trail", rows)
    assert demand == {"US": 6600}  # "nakasendo" is the fit=1 keyword naming the action


def test_compute_demand_kind_only_overlap_is_refused():
    # "shrine" alone is a place-kind word — sharing only that proves nothing (place-kinds.toml's
    # own worked example: Amanoiwato Shrine must not claim "meiji jingu shrine").
    rows = [("meiji jingu shrine", "US", 27100)]
    demand = compute_demand("Amanoiwato Shrine", "visit", rows)
    assert demand == {}


def test_compute_demand_best_fit_then_volume_prefers_specific_over_generic():
    rows = [("matsumoto", "US", 14800), ("matsumoto castle", "US", 6600)]
    demand = compute_demand("Matsumoto Castle", "visit", rows)
    assert demand == {"US": 6600}  # fit=2 (matsumoto+castle) beats fit=1 (matsumoto) on volume


def test_candidate_questions_counts_distinct_claimed_paa():
    # AA-610 (Sub 2) — renamed from compute_questions(): this is now just the keyword-level
    # claim-by-name SHORTLIST (candidates for embedding-match to land on this Segment's atoms),
    # not the final questions count — see land_questions_for_segment() for the real landing.
    rows = [
        ("nakasendo trail", "US", ["Is the Nakasendo trail hard?", "How long is it?"]),
        ("unrelated keyword", "US", ["Something about nothing"]),
    ]
    result = _candidate_questions("Magome", "walk the Nakasendo trail", rows)
    assert result == {"Is the Nakasendo trail hard?", "How long is it?"}


# ── rank_segments (rank-sum, no weights, lowest total wins) ────────────────────────────────


def _candidate(segment_id, recurrence=1, questions=0, said=0, demand=None):
    return Candidate(
        segment_id=segment_id, place="Place", action="do",
        tour_ids=("t1",), recurrence=recurrence, questions=questions, said=said,
        demand=demand or {},
    )


def test_rank_segments_empty_returns_empty():
    assert rank_segments([], "US") == []


def test_rank_segments_lowest_total_wins():
    strong = _candidate("strong", recurrence=5, questions=3, said=500, demand={"US": 10000})
    weak = _candidate("weak", recurrence=1, questions=0, said=10, demand={"US": 10})
    ranked = rank_segments([strong, weak], "US")
    assert [r.segment_id for r in ranked] == ["strong", "weak"]
    assert ranked[0].total_rank < ranked[1].total_rank


def test_rank_segments_ties_share_a_rank():
    a = _candidate("a", recurrence=2)
    b = _candidate("b", recurrence=2)
    c = _candidate("c", recurrence=1)
    ranked = {r.segment_id: r for r in rank_segments([a, b, c], "US")}
    assert ranked["a"].recurrence_rank == ranked["b"].recurrence_rank == 1
    assert ranked["c"].recurrence_rank == 3  # competition ranking: 1, 1, 3 — not 1, 1, 2


def test_rank_segments_unmeasured_demand_gets_median_not_last():
    measured_low = _candidate("low", demand={"US": 10})
    measured_high = _candidate("high", demand={"US": 10000})
    unmeasured = _candidate("unmeasured", demand={})
    ranked = {r.segment_id: r for r in rank_segments(
        [measured_low, measured_high, unmeasured], "US",
    )}
    # 2 measured -> ranks {high: 1, low: 2}, median of [1, 2] at index 1 -> 2, not the worst (2
    # is already the worst of 2 measured, but crucially not appended as a 3rd/last place: with
    # 3 measured this diverges from "last" — this asserts it never falls outside the measured
    # range simply for being unmeasured).
    assert ranked["unmeasured"].demand_rank in (1, 2)


def test_rank_segments_one_market_per_call_no_best_of_n_anymore():
    """AA-545 — `rank_segments()` no longer picks a "best market" itself (that merge moved to
    read time, `services/acp_shared/slate.py`); calling it once per market for the SAME
    candidates can rank them differently, and `demand_market` always reports back exactly the
    market it was called with."""
    target = _candidate("s", demand={"US": 5, "UK": 50000})
    competitor = _candidate("c", demand={"US": 100000, "UK": 100})

    ranked_us = {r.segment_id: r for r in rank_segments([target, competitor], "US")}
    ranked_uk = {r.segment_id: r for r in rank_segments([target, competitor], "UK")}

    assert ranked_us["s"].demand_market == "US"
    assert ranked_uk["s"].demand_market == "UK"
    # "s" loses on US demand (5 vs 100000) but wins on UK demand (50000 vs 100).
    assert ranked_us["s"].demand_rank > ranked_us["c"].demand_rank
    assert ranked_uk["s"].demand_rank < ranked_uk["c"].demand_rank
