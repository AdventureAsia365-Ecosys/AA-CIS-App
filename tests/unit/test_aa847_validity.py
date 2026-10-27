"""AA-847 — content-validity report (services/places/validity.py): reason codes, the same-place rule on a labelled
keep / merge pair set (false merges measured: the costly error, lesson S245 / AA-779), the report and its counts.

The labelled pairs are a regression set: a rule change that merges a KEEP pair or stops merging a MERGE pair fails
here. Seeded from the S237 UI verify (Kuang Si named several ways in one Laos tour) and from places of the Laos and
Vietnam tours, plus the review S246 wrong merges (same local name, different kinds: bridge / market / lake / night
market / old town); extend it with every pair the AA-847 probe (section 4) or an admin fix turns up.
"""
from __future__ import annotations

import pytest

from services.places import validity as v

P = v.Place

# (a, b, label): label "merge" = the same real place, "keep" = two places
LABELLED_PAIRS = [
    (P("k1", "Kuang Si Falls", "Laos", 19.7491, 101.9935, "geoapify", "point"),
     P("k2", "Tat Kuang Si", "Laos", 19.7480, 101.9920, "llm", "area"), "merge"),
    (P("k1", "Kuang Si Falls", "Laos", 19.7491, 101.9935, "geoapify", "point"),
     P("k3", "Kuang Si Waterfall", "Laos", 19.7500, 101.9900, "llm", "area"), "merge"),
    (P("k1", "Kuang Si Falls", "Laos", 19.7491, 101.9935, "geoapify", "point"),
     P("k4", "Kuangsi Waterfalls", "Laos", 19.7495, 101.9940, "geoapify", "point"), "merge"),
    (P("p1", "Pak Ou Caves", "Laos", 20.0500, 102.2190, "geoapify", "point"),
     P("p2", "Pak Ou Cave", "Laos", 20.0511, 102.2183, "llm", "area"), "merge"),
    (P("m1", "Mount Phousi", "Laos", 19.8924, 102.1352, "geoapify", "point"),
     P("m2", "Phou Si Hill", "Laos", 19.8930, 102.1355, "llm", "area"), "merge"),
    (P("h1", "Hoan Kiem Lake", "Vietnam", 21.0288, 105.8525, "geoapify", "point"),
     P("h2", "Hoan Kiem", "Vietnam", 21.0285, 105.8520, "llm", "area"), "merge"),
    # two temples of Luang Prabang ~0.9 km apart: "wat" is a kind, not a name
    (P("w1", "Wat Xieng Thong", "Laos", 19.8977, 102.1427, "geoapify", "point"),
     P("w2", "Wat Mai", "Laos", 19.8907, 102.1386, "geoapify", "point"), "keep"),
    # Vientiane: the That Luang stupa and the Wat That Luang Tai temple next to it (250 m): different places
    (P("t1", "That Luang", "Laos", 17.9763, 102.6335, "geoapify", "point"),
     P("t2", "Wat That Luang Tai", "Laos", 17.9741, 102.6340, "geoapify", "point"), "keep"),
    (P("s1", "Wat Sisaket", "Laos", 17.9625, 102.6094, "geoapify", "point"),
     P("s2", "Haw Phra Kaew", "Laos", 17.9617, 102.6106, "geoapify", "point"), "keep"),
    # same name, far apart: Ho Chi Minh City and the Ho Chi Minh Trail point
    (P("c1", "Ho Chi Minh City", "Vietnam", 10.7769, 106.7009, "geoapify", "point"),
     P("c2", "Ho Chi Minh Trail", "Vietnam", 16.4637, 107.2000, "llm", "area"), "keep"),
    # the same name in two countries is two places (destinations are unique per country)
    (P("x1", "Mekong Delta", "Vietnam", 10.0, 105.8, "llm", "area"),
     P("x2", "Mekong Delta", "Cambodia", 10.0, 105.8, "llm", "area"), "keep"),
    # same distinctive name 3 km apart, both confirmed points: kept (the strict 2 km rule)
    (P("n1", "Nong Khiaw", "Laos", 20.5700, 102.6100, "geoapify", "point"),
     P("n2", "Nong Khiaw Viewpoint", "Laos", 20.5700, 102.6380, "geoapify", "point"), "keep"),
    # review S246: the same local name, different kinds of place, all within 2 km — two places each
    (P("ha1", "Hoi An Old Town", "Vietnam", 15.8771, 108.3264, "geoapify", "point"),
     P("ha2", "Hoi An Night Market", "Vietnam", 15.8762, 108.3260, "geoapify", "point"), "keep"),
    (P("lb1", "Long Bien Bridge", "Vietnam", 21.0430, 105.8590, "geoapify", "point"),
     P("lb2", "Long Bien Market", "Vietnam", 21.0396, 105.8510, "geoapify", "point"), "keep"),
    (P("hk1", "Hoan Kiem Lake", "Vietnam", 21.0288, 105.8525, "geoapify", "point"),
     P("hk2", "Hoan Kiem Night Market", "Vietnam", 21.0350, 105.8510, "llm", "area"), "keep"),
    (P("m1", "Mount Phousi", "Laos", 19.8924, 102.1352, "geoapify", "point"),
     P("m3", "Phousi Market", "Laos", 19.8800, 102.1380, "llm", "area"), "keep"),
    (P("db1", "Dong Ba Market", "Vietnam", 16.4732, 107.5880, "geoapify", "point"),
     P("db2", "Dong Ba Bridge", "Vietnam", 16.4745, 107.5860, "geoapify", "point"), "keep"),
    (P("bt1", "Ben Thanh Market", "Vietnam", 10.7725, 106.6980, "geoapify", "point"),
     P("bt2", "Ben Thanh Night Market", "Vietnam", 10.7718, 106.6972, "geoapify", "point"), "keep"),
    # a town and the built place named after it: the bare name is the town
    (P("lp1", "Luang Prabang", "Laos", 19.8856, 102.1347, "geoapify", "point"),
     P("lp2", "Luang Prabang Night Market", "Laos", 19.8890, 102.1360, "geoapify", "point"), "keep"),
    (P("lb1", "Long Bien Bridge", "Vietnam", 21.0430, 105.8590, "geoapify", "point"),
     P("lb3", "Long Bien", "Vietnam", 21.0450, 105.8600, "llm", "area"), "keep"),
    # a natural feature and its bare name, and two spellings of one quarter: one place
    (P("m1", "Mount Phousi", "Laos", 19.8924, 102.1352, "geoapify", "point"),
     P("m4", "Phousi", "Laos", 19.8930, 102.1350, "llm", "area"), "merge"),
    (P("oq1", "Hanoi Old Quarter", "Vietnam", 21.0340, 105.8500, "geoapify", "point"),
     P("oq2", "Old Quarter, Hanoi", "Vietnam", 21.0345, 105.8505, "llm", "area"), "merge"),
]


def _confusion():
    out = {"merge_ok": 0, "merge_missed": 0, "keep_ok": 0, "false_merge": 0}
    for a, b, label in LABELLED_PAIRS:
        same, _ = v.is_same_place(a, b)
        if label == "merge":
            out["merge_ok" if same else "merge_missed"] += 1
        else:
            out["false_merge" if same else "keep_ok"] += 1
    return out


def test_labelled_pairs_no_false_merge_and_every_merge_found():
    c = _confusion()
    assert c["false_merge"] == 0, c
    assert c["merge_missed"] == 0, c


@pytest.mark.parametrize("a,b,label", LABELLED_PAIRS, ids=[f"{a.name}|{b.name}" for a, b, _ in LABELLED_PAIRS])
def test_each_labelled_pair(a, b, label):
    assert v.is_same_place(a, b)[0] is (label == "merge")


def test_keeper_prefers_a_confirmed_point_then_components():
    a = P("a", "Tat Kuang Si", "Laos", 19.748, 101.992, "llm", "area", components=9)
    b = P("b", "Kuang Si Falls", "Laos", 19.749, 101.993, "geoapify", "point", components=1)
    assert v.keeper_of([a, b]).destination_id == "b"
    c = P("c", "Kuang Si", "Laos", 19.749, 101.993, "geoapify", "point", components=4)
    assert v.keeper_of([a, b, c]).destination_id == "c"


def test_clusters_do_not_chain_through_a_third_place():
    """A ~ B and B ~ C but A !~ C: the group keeps only members that are the same place as the keeper (AA-847
    Problem 2: the rule is dedupe.plan_country — near_duplicate = what the rules alone merge)."""
    a = P("a", "Kuang Si Falls", "Laos", 19.7490, 101.9935, "geoapify", "point", components=5)
    b = P("b", "Kuang Si", "Laos", 19.7490, 101.9960, "llm", "area")         # ~0.3 km from a
    c = P("c", "Kuang Si", "Laos", 19.7490, 102.0120, "llm", "area")         # ~1.7 km from b, ~1.9 km from a
    assert v.is_same_place(b, c)[0] and not v.is_same_place(a, c)[0]
    groups = v.clusters([a, b, c])
    assert groups and groups[0][0].destination_id == "a"
    assert "c" not in [p.destination_id for p in groups[0]]


def test_a_pair_left_to_the_model_is_a_possible_duplicate_not_a_gate_item():
    """Ben Thanh Market / Ben Thanh (bare name vs a built place) is the two-model check's call: informational."""
    tours = [{"source_id": "s1", "tour_id": "t1", "title": "Saigon", "country": "Vietnam", "duration_raw": "1 day",
              "itinerary": "Day 1 — Saigon\nShop at Ben Thanh Market, then Ben Thanh at night."}]
    comps = [_comp("s1", 1, "Ben Thanh Market", "d1", "Ben Thanh Market", 10.7725, 106.6980),
             {**_comp("s1", 1, "Ben Thanh", "d2", "Ben Thanh", 10.7726, 106.6981, "llm", "area"),
              "country": "Vietnam"}]
    comps[0]["country"] = "Vietnam"
    items = v.evaluate(tours, comps)
    assert [i.reason for i in items] == ["possible_duplicate"]
    assert v.summarize(tours, items)["catalog"]["gate"]["pass"] is True
    assert "possible_duplicate" not in v.GATE_REASONS


# ── V1 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _itin(*days):
    return "\n\n".join(days)


def test_duration_ranged_heading_and_text_vs_days():
    text = _itin("Day 1 — Hanoi\nArrive.", "Day 2 — Hue\nFly.", "Day 3-4 — Ride\nCycle.", "Day 5 — Hoi An\nBye.")
    got = dict(v.duration_findings("14 Days", text))
    assert "ranged_heading" in got and "3-4" in got["ranged_heading"]
    assert got["duration_text_vs_days"] == "duration 14 days, itinerary 5 days"


def test_duration_missing_and_extra_days():
    text = _itin("Day 1 — A\nx", "Day 2 — B\nx", "Day 4 — C\nx")
    got = dict(v.duration_findings("3 days", text))
    assert got["missing_day_numbers"] == "missing 3"
    assert got["extra_days"] == "duration 3 days, itinerary 4 days"


def test_duration_days_follows_the_v_tour_rule():
    assert v.duration_days("12-day trip", "") == 12
    assert v.duration_days(None, "Day 1 — A\nx\nDay 7 — B\ny") == 7
    assert v.duration_days(None, "no days") is None
    assert v.duration_findings("2 days", _itin("Day 1 — A\nx", "Day 2 — B\ny")) == []


# ── V4 ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_day_text_names():
    assert v.day_text_names("Day 1 — Swim at Kuang Si Falls", "Kuang Si Falls")
    assert v.day_text_names("Visit Hội An old town", "Hoi An")
    assert not v.day_text_names("Day 5 — Khimsar to Jaisalmer", "Amber Fort")


def test_day_blocks_keep_the_last_repeated_day_and_expand_ranges():
    # AA-990 (S253): a "Day 2-3 —" block is the text of days 2 and 3 (it used to be folded into day 1)
    b = v.day_blocks(_itin("Day 1 — A\nfirst", "Day 2-3 — B\nrange", "Day 1 — C\nlast"))
    assert sorted(b) == [1, 2, 3]
    assert "last" in b[1] and "range" not in b[1]
    assert b[2] == b[3] == "B\nrange"


# ── the report ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _comp(src, day, name, dest, dname, lat, lng, located_by="geoapify", precision="point", rt=None, nn=None):
    return {"source_id": src, "day_index": day, "name": name, "name_norm": nn or name.lower(), "destination_id": dest,
            "destination_name": dname, "country": "Laos", "lat": lat, "lng": lng, "located_by": located_by,
            "precision": precision, "review_reason": None, "result_type": rt}


def test_report_flags_each_defect_once_with_reason_codes():
    tours = [{"source_id": "s1", "tour_id": "t1", "title": "Laos Kuang Si", "country": "Laos", "duration_raw": "3 days",
              "itinerary": _itin("Day 1 — Luang Prabang\nSwim at Kuang Si Falls and Tat Kuang Si.",
                                 "Day 2 — Ride\nThe Ho Chi Minh Trail by bike.", "Day 3 — Depart\nBye.")},
             {"source_id": "s2", "tour_id": "t2", "title": "Clean", "country": "Vietnam", "duration_raw": "1 day",
              "itinerary": "Day 1 — Hanoi\nHoan Kiem Lake walk."}]
    comps = [_comp("s1", 1, "Kuang Si Falls", "d1", "Kuang Si Falls", 19.749, 101.9935),
             _comp("s1", 1, "Tat Kuang Si", "d2", "Tat Kuang Si", 19.748, 101.992, "llm", "area"),
             _comp("s1", 1, "Kuang Si Waterfall", "d1", "Kuang Si Falls", 19.749, 101.9935, nn="kuang si waterfall"),
             _comp("s1", 2, "Ho Chi Minh Trail", "d3", "Ho Chi Minh Trail", 16.46, 107.2, rt="street"),
             _comp("s1", 3, "Amber Fort", "d4", "Amber Fort", 26.98, 75.85),
             {**_comp("s2", 1, "Hoan Kiem Lake", "d5", "Hoan Kiem Lake", 21.0288, 105.8525), "country": "Vietnam"}]
    items = v.evaluate(tours, comps)
    reasons = sorted((i.check, i.reason, i.source_id) for i in items)
    # "Kuang Si Waterfall" is named by "Kuang Si Falls" in its day (waterfall / falls are kind words); Amber Fort is not
    assert reasons == [("day_text", "day_text_not_naming", "s1"),
                       ("not_a_pin", "linear_feature", "s1"),
                       ("same_place", "near_duplicate", "s1"), ("same_place", "same_destination_names", "s1")]
    dup = next(i for i in items if i.reason == "near_duplicate")
    assert dup.keeper_id == "d1" and set(dup.destination_ids) == {"d1", "d2"}
    summary = v.summarize(tours, items)
    assert summary["catalog"]["tours"] == 2 and summary["catalog"]["tours_flagged"] == 1
    assert summary["by_country"]["Laos"]["checks"]["same_place"]["reasons"]["near_duplicate"] == 1
    assert summary["by_country"]["Vietnam"]["tours_flagged"] == 0
    counts = v.stats_counts(summary)
    assert counts["not_a_pin.linear_feature"] == 1 and counts["tours"] == 2
    assert set(counts) == ({"tours", "tours_flagged", "gate.items"}
                           | {f"{c}.{r}" for c in v.CHECKS for r in v.REASONS[c]})
    # gate: the near duplicate and the trail pin block; the spellings, the day text do not
    assert summary["catalog"]["gate"] == {"reasons": list(v.GATE_REASONS), "items": 2, "tours": 1, "pass": False}
    assert summary["by_country"]["Vietnam"]["gate"]["pass"] is True and counts["gate.items"] == 2
    pg = v.page(items, check="same_place", page_no=1, page_size=1)
    assert pg["total"] == 2 and len(pg["items"]) == 1


def test_after_the_fixes_the_kuang_si_tour_passes_the_gate():
    """The duplicate fix points Tat Kuang Si at the keeper (one pin, several spellings: same_destination_names stays,
    informational); a trail is stored as a line with its shape (tests/unit/test_aa847_lines.py): the tour passes."""
    tours = [{"source_id": "s1", "tour_id": "t1", "title": "Laos Kuang Si", "country": "Laos", "duration_raw": "1 day",
              "itinerary": "Day 1 — Luang Prabang\nSwim at Kuang Si Falls and Tat Kuang Si."}]
    comps = [_comp("s1", 1, "Kuang Si Falls", "d1", "Kuang Si Falls", 19.749, 101.9935),
             _comp("s1", 1, "Tat Kuang Si", "d1", "Kuang Si Falls", 19.749, 101.9935, nn="tat kuang si")]
    items = v.evaluate(tours, comps)
    assert [i.reason for i in items] == ["same_destination_names"]
    gate = v.summarize(tours, items)["catalog"]["gate"]
    assert gate["pass"] is True and gate["items"] == 0
    assert "same_destination_names" not in v.GATE_REASONS


def test_gate_reasons_are_in_the_taxonomy():
    assert set(v.GATE_REASONS) <= {r for rs in v.REASONS.values() for r in rs}


def test_every_reason_is_in_the_taxonomy():
    for check, reasons in v.REASONS.items():
        assert check in v.CHECKS and reasons
