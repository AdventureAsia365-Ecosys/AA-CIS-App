"""S1-first step 1 — the code checks (services/quality/s1_checks.py). Synthetic pages only."""
from services.content_generation.seo_meta_utils import meta_complete_sentence, meta_cut_signature
from services.quality.s1_checks import meta_list_cut, run_checks

SRC = {
    "name": "Island Loop",
    "country": "Sri Lanka",
    "summary": "Wilpattu has approximately 30 species of mammals. Visit the 50 slate houses of the village.",
    "itineraries": ("Day 1: Colombo to Chilaw\nRelax at the beach in Chilaw.\n"
                    "Day 2: Chilaw\nFree day.\n"
                    "Day 3: Chilaw to Habarana\nDrive 3 hrs to Habarana.\n"
                    "Day 4: Habarana to Colombo\nDrive back.\n"),
    "duration": "4 days",
    "inclusions": "Breakfast and dinner\nGuide",
    "exclusions": "Lunches\nDrinks",
}


def _page(**kw):
    base = {
        "subtitle": "A four-day loop.",
        "summary": "A loop from Colombo through Chilaw and Habarana.",
        "itineraries": ("Day 1 — Colombo to Chilaw\nRelax at the beach.\n\nDay 2 — Chilaw\nA free day.\n\n"
                        "Day 3 — Chilaw to Habarana\nA 3-hour drive.\n\nDay 4 — Habarana to Colombo\nDrive back."),
        "seo_meta": ("A four-day Sri Lanka loop from Colombo to the beaches of Chilaw and the ruins near Habarana, "
                     "with breakfast, dinner and a local guide on every day."),
    }
    base.update(kw)
    return base


def codes(page):
    return [f["code"] for f in run_checks(SRC, page)]


def test_a_faithful_page_has_no_findings():
    assert codes(_page()) == []


def test_day_count_and_day_place_drift():
    it = ("Day 1 — Colombo to Chilaw\nBeach.\n\nDay 2 — Three days in Habarana\nRuins.\n\n"
          "Day 3 — Chilaw to Habarana\nDrive.\n\nDay 4 — Habarana to Colombo\nBack.\n\nDay 5 — Colombo\nExtra.")
    got = codes(_page(itineraries=it))
    assert "DAY_PLACE_MISMATCH" in got          # Habarana is a day-3 place, written on day 2
    assert "DAY_OUTPUT_EXTRA" in got


def test_meta_cut_and_list_cut_and_length():
    assert "META_CUT" in codes(_page(seo_meta="A four-day Sri Lanka loop from Colombo through Chilaw beaches and "
                                              "Habarana ruins, with breakfast and dinner on a single 20-kilometre."))
    assert meta_list_cut("This journey connects five sites, visiting Jaipur, Jodhpur, Udaipur, Agra, Khajuraho.")
    assert meta_list_cut("Travel north India over 22 days with local guides, trains.") is None  # one comma only
    assert meta_list_cut("Visit Lhasa, then travel north to Namtso Lake, Tibet's second-largest lake.") is None
    assert meta_list_cut("Visit Seoul, Busan, Gyeongju and Jeju over ten days, ideal for families.") is None
    assert "META_LENGTH" in codes(_page(seo_meta="A short loop from Colombo through Chilaw and Habarana."))


def test_meta_ending_in_a_place_name_is_complete():
    meta = ("Cycle from Luang Prabang's temples through the Plain of Jars to Vietnam's Ho Chi Minh Trail, imperial "
            "Hue, and Hoi An.")
    assert meta_complete_sentence(meta) is True
    assert meta_cut_signature(meta) is None
    assert meta_cut_signature("Explore the old town and the river by bike with an.") == "weak ending 'an'"


def test_truncated_itinerary():
    it = _page()["itineraries"] + "\n\nFrom there, continue to the tea fields, where endless"
    assert "TEXT_TRUNCATED" in codes(_page(itineraries=it))


def test_number_not_in_source_and_unit_conversion():
    assert "NUMBER_NOT_IN_SOURCE" in codes(_page(summary="Cycle 900 kilometres across the island."))
    src = dict(SRC, itineraries=SRC["itineraries"] + "Day 4: Pass at 3,730m\n")
    page = _page(summary="Cross the pass at 3,730m (12,238ft).")
    assert "NUMBER_NOT_IN_SOURCE" not in [f["code"] for f in run_checks(src, page)]


def test_number_bound_to_another_noun():
    got = run_checks(SRC, _page(summary="The village sits roughly 50 minutes from the station."))
    assert [f["code"] for f in got] == ["NUMBER_NOUN_MISMATCH"]
    # a bare count is the panel's call, not code's: "30 lakes" vs "30 species" is not flagged
    assert codes(_page(summary="The park has about 30 freshwater lakes.")) == []


def test_name_not_in_source_skips_generic_title_words():
    got = run_checks(SRC, _page(summary="Drive the Karakoram Highway to Chilaw."))
    assert [f["code"] for f in got] == ["NAME_NOT_IN_SOURCE"]
    assert codes(_page(itineraries=_page()["itineraries"].replace("Day 2 — Chilaw", "Day 2 — Chilaw Arrival "
                                                                                    "and Orientation"))) == []


def test_inclusion_contradicted_by_exclusions():
    got = run_checks(SRC, _page(summary="Lunch is included every day."))
    assert [f["code"] for f in got] == ["INCLUSION_CONTRADICTED"]
    got = run_checks(SRC, _page(summary="All meals are included."))
    assert [f["code"] for f in got] == ["INCLUSION_CONTRADICTED"]
    assert codes(_page(summary="Lunch is not included.")) == []
    assert codes(_page(summary="Breakfast is included.")) == []
