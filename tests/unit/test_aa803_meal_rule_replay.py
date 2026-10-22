"""AA-803 (S247) — scripts/aa803/meal_rule_replay.py, the pure part (no DB, no S3, no Jev)."""
import json

from scripts.aa803 import meal_rule_replay as rp
from services.content_generation import brand_audit_node as ba
from services.content_generation import grounding as g
from services.content_generation import meal_claims as mc

SRC = ("Day 1: Hanoi - Arrive.\nDay 2: Ninh Binh - Boat through the karsts. Meals: B, L\n"
       "Day 3: Halong Bay - Board the junk.\nDay 4: Hue - Depart.")


def _row(itin, codes=(), fix_fields=(), tour_id="t-1", inclusions="", gc_id="gc-1"):
    return {"gc_id": gc_id, "tour_id": tour_id, "version_num": 1, "fix_pass_fields": json.dumps(list(fix_fields)),
            "aa_itineraries": itin, "aa_subtitle": "", "aa_summary": "", "aa_highlights": "[]",
            "src_name": "T", "src_subtitle": "", "src_summary": "", "src_description": "", "src_highlights": "[]",
            "src_itineraries": SRC, "country": "Vietnam", "duration": "4 Days", "price_raw": "",
            "inclusions": inclusions, "exclusions": "", "brand_audit_codes": json.dumps(list(codes))}


def _itin(day2, day3="Cruise among the karsts."):
    return (f"Day 1 — Hanoi\nArrive.\n\nDay 2 — Ninh Binh\nBoat through the karsts. {day2}\n\n"
            f"Day 3 — Halong Bay\n{day3}\n\nDay 4 — Hue\nDepart.")


def test_a_jev_waived_version_the_rule_clears_needs_jev_no_more():
    r = rp.classify_version(_row(_itin("Lunch is served at a riverside restaurant.")), ba, g, mc)
    assert r["waived_by_stored_code"] and r["rule_clears_all"] and not r["still_needs_jev"]
    assert r["old_units"] == 1 and r["old_units_cleared"] == 1 and r["jev_units"] == 0
    assert not r["fires_jev_off"] and r["fires_jev_off_old"]


def test_a_jev_waived_version_the_rule_does_not_clear_still_needs_jev():
    r = rp.classify_version(_row(_itin("Dinner is served at a riverside restaurant.")), ba, g, mc)
    assert r["waived_by_stored_code"] and r["still_needs_jev"] and r["jev_units"] == 1
    assert r["verdicts"][0]["reason"] == "not_in_day_list"


def test_a_stored_code_or_a_rewritten_itinerary_is_not_a_waiver():
    assert not rp.classify_version(_row(_itin("Lunch is served here."), codes=[rp.MEAL_CODE]), ba, g, mc)[
        "waived_by_stored_code"]
    assert not rp.classify_version(_row(_itin("Lunch is served here."), fix_fields=["itineraries"]), ba, g, mc)[
        "comparable"]


def test_aggregate_shares_and_bar():
    rows = [_row(_itin("Lunch is served at a riverside restaurant."), tour_id="a"),
            _row(_itin("Dinner is served at a riverside restaurant."), tour_id="b"),
            _row(_itin("Walk on."), tour_id="c")]
    t = rp.aggregate([rp.classify_version(r, ba, g, mc) for r in rows], flag_fix_cost=0.01)
    assert t["tours"] == 3 and t["tours_waived_by_stored_code"] == 2 and t["tours_still_needing_jev"] == 1
    assert t["waiver_share_before"] == round(2 / 3, 4) and t["waiver_share_after"] == round(1 / 3, 4)
    assert t["waiver_bar_met"] is False
    assert t["jev_meal_units_old"] == 2 and t["jev_meal_units_removed_vs_old"] == 1
    assert t["claims_cleared_by_path"] == {"day_list": 1}


def test_the_frozen_old_regex_is_the_pre_aa803_one():
    assert rp.OLD_ITIN_MEAL_INVENTED.search("Lunch is served at the lodge.")
    assert not rp.OLD_ITIN_MEAL_INVENTED.search("Lunch and dinner are included.")
    assert ba.ITIN_MEAL_INVENTED.search("Lunch and dinner are included.")
