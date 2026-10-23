"""S1-first step 1 — the code-built fact table (services/quality/s1_facts.py). Synthetic sources only."""
from services.content_generation.day_parse import source_day_markers
from services.quality.s1_facts import (build_fact_table, clean_name, item_lines, number_mentions, sentences,
                                       verify_pointers)

RAFT = {
    "name": "1 Day River Rafting",
    "summary": "Raft the river with our guides. Contact Acme Rafting now to book!",
    "itineraries": ("After early breakfast you drive 3-4 hrs to the put-in at Fishling. Rafting for about 4 hrs "
                    "down to Mugling with a short lunch break. Optional zipline at an extra cost."),
    "duration": "1 DAY",
    "inclusions": "• Rafting trip with lunch, served on the river bank.\n• Experienced river guide and crew.",
    "exclusions": "What's Not Included\n×\nDrinks\n×\nTravel insurance\n×\nAny meals not mentioned in the itinerary",
}

TREK = {
    "name": "Valley Trek 5 Days",
    "itineraries": ("Day 1: Paro to Shana\nWalk 17 km along the river. Overnight camp.\n"
                    "Day 2 & 3: Shana to Thangthangka\nClimb to 3,610m.\n"
                    "Day 4: Thangthangka to Jangothang\n"
                    "Day 5: Return to Paro\n"),
    "duration": "6 days",
    "inclusions": "All meals during the trek\nLunch",
    "exclusions": "Lunch in Paro\nTips",
}


def test_every_verbatim_value_points_into_its_field():
    for tour in (RAFT, TREK):
        facts = build_fact_table(tour)
        assert verify_pointers(facts, tour) == []


def test_verify_pointers_catches_a_value_not_in_the_source():
    facts = build_fact_table(TREK)
    facts["places"].append({"kind": "verbatim", "value": "Bumthang", "field": "itineraries", "quote": "Bumthang"})
    bad = verify_pointers(facts, TREK)
    assert [b["value"] for b in bad] == ["Bumthang"]


def test_duration_kinds():
    assert build_fact_table(RAFT)["duration"]["value"] == "one_day"
    assert build_fact_table({"duration": "3 hours"})["duration"]["value"] == "hours"
    assert build_fact_table({"duration": "HALF DAY"})["duration"]["value"] == "hours"
    assert build_fact_table(TREK)["duration"]["value"] == "multi_day"
    assert build_fact_table({"duration": ""})["duration"]["value"] == "unknown"


def test_day_list_markers_expand_to_a_range():
    # "Day 2 & 3" and "Day 11, 12 & 13" are ranges (golden s1-004 read days 6/12/13 as missing before S248)
    assert source_day_markers("Day 05 & 06: A\nDay 11, 12 & 13: B") == [(5, 6, 0), (11, 13, 1)]
    assert source_day_markers("Day 1 - 3 hours drive") == [(1, 1, 0)]
    assert source_day_markers("Day 7 & 9: not consecutive") == [(7, 7, 0)]
    days = build_fact_table(TREK)["days"]
    assert [(d["day_from"], d["day_to"]) for d in days] == [(1, 1), (2, 3), (4, 4), (5, 5)]
    assert "Shana" in days[0]["places"]


def test_conflicts_duration_vs_markers_and_item_both_ways():
    conflicts = {c["value"] for c in build_fact_table(TREK)["conflicts"]}
    assert "duration_vs_day_markers" in conflicts       # 6 days vs 5 day markers
    assert "item_included_and_excluded" in conflicts    # lunch
    assert "name_vs_duration" in conflicts              # "5 Days" in the name


def test_items_optional_and_missing():
    facts = build_fact_table(RAFT)
    assert [e["value"] for e in facts["included"]][0].startswith("Rafting trip with lunch")
    excluded = {e["value"]: e for e in facts["excluded"]}
    assert "Drinks" in excluded and "What's Not Included" not in excluded and "×" not in excluded
    assert excluded["Any meals not mentioned in the itinerary"]["conditional"] is True
    assert any("Optional zipline" in e["value"] for e in facts["optional"])
    assert {"group_size", "minimum_age", "difficulty", "price"} <= set(facts["missing"])
    assert "meals" not in facts["missing"] and "transport" not in facts["missing"]


def test_operator_name_is_do_not_use_and_not_a_place():
    facts = build_fact_table(RAFT)
    assert [e["value"] for e in facts["do_not_use"] if e["reason"] == "operator"] == ["Acme Rafting"]
    assert not any("Acme" in e["value"] for e in facts["places"])


def test_number_mentions_units_and_roles():
    got = {m["span"]: (m["unit"], m["role"]) for m in number_mentions(
        "Drive 3-4 hrs, then 12km to Watchtower 8 at 9:00 am; a 12th-century temple at 4,250 metres, 5 sq km.")}
    assert got["3-4 hrs"] == ("h", "figure")
    assert got["12km"] == ("km", "figure")
    assert got["8"][1] == "label"
    assert got["9"][1] == "clock"
    assert got["12"][1] == "ordinal"
    assert got["4,250 metres"] == ("m", "figure")
    assert got["5 sq km"] == ("km2", "figure")


def test_number_words_stop_at_a_bracket_or_line():
    m = number_mentions("cycle to Ban Som (12) and begin an uphill trek")[0]
    assert m["words"] == []
    m = number_mentions("about 30 species of mammals")[0]
    assert m["words"][:2] == ["specie", "mammal"]


def test_sentences_keep_decimals_and_lines():
    assert sentences("It takes 2.5 hours. Then go.\n• Lunch") == ["It takes 2.5 hours.", "Then go.", "• Lunch"]


def test_item_lines_drop_headers_and_markers():
    assert item_lines("What's Included\nGuide\n×\n• Lunch") == ["Guide", "Lunch"]


def test_clean_name_drops_sentence_start_words():
    lower = {"visit", "early"}
    assert clean_name("Visit Mingora", lower) == "Mingora"
    assert clean_name("Early", lower) == ""
    assert clean_name("Malam Jabba", lower) == "Malam Jabba"
