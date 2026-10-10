"""AA-756 — grounding token trim: per-unit source + skip rule.

Covers (mock-only, no DB / no network):
  - sentence_units() carries the source day number for itinerary units.
  - unit_source(): day-slice (prev/this/next) + header + inclusions/exclusions for an itinerary
    unit; full source for subtitle/summary/highlights and for the fallback / no-day cases.
  - should_skip(): conservative — only skips a unit with no digit, no capitalised word after the
    first, and no promise cue.
  - check_grounding(): asks Jev per unit (per-unit source), skips the uncheckable, reports
    units_skipped, and the numeric check still runs on the FULL source.
  - scripts/aa756_grounding_tokens.estimate(): before/after on a clean-day-marker tour and a
    no-marker (fallback) tour.
"""
from services.content_generation import grounding as gr
from scripts.aa756_grounding_tokens import estimate


# A source with clean "Day N" markers so the day splitter succeeds.
TOUR_DAYS = {
    "name": "Bhutan Cultural Journey",
    "country": "Bhutan",
    "duration": "3 days",
    "summary": "Three days across Paro and Thimphu.",
    "itineraries": (
        "Day 1 — Arrival in Paro\n"
        "Arrive in Paro and transfer to the hotel. Overnight at the lodge.\n"
        "Day 2 — Thimphu\n"
        "Drive to Thimphu and visit the dzong. Lunch is included.\n"
        "Day 3 — Punakha\n"
        "Hike to the temple above the valley. Return by car in the afternoon.\n"
    ),
    "inclusions": "All breakfasts and one lunch. Private transfers throughout.",
    "exclusions": "International flights. Personal expenses.",
}

GEN_DAYS = {
    "subtitle": "Three days of dzongs and valleys in Bhutan",
    "summary": "A journey across Paro and Thimphu over three days.",
    "highlights": ["The dzong at Thimphu seen at dusk", "A quiet valley walk near Punakha"],
    "itineraries": (
        "Day 1 — Arrival in Paro\n"
        "Arrive in Paro and settle into the lodge for the night.\n"
        "Day 2 — Thimphu\n"
        "Drive to Thimphu and tour the fortress before lunch.\n"
        "Day 3 — Punakha\n"
        "Hike up to the temple above the valley floor.\n"
    ),
}


# ── sentence_units day numbers ────────────────────────────────────────────────────────────────

def test_sentence_units_carry_day_number_for_itinerary_units():
    units = gr.sentence_units(GEN_DAYS)
    by_field = {}
    for u in units:
        by_field.setdefault(u["field"], []).append(u)
    # non-itinerary units have day=None
    assert all(u["day"] is None for u in by_field.get("subtitle", []))
    assert all(u["day"] is None for u in by_field.get("summary", []))
    assert all(u["day"] is None for u in by_field.get("highlights", []))
    # itinerary units carry the day of the title they fall under
    itin = {u["sentence"]: u["day"] for u in by_field["itineraries"]}
    assert itin["Arrive in Paro and settle into the lodge for the night."] == 1
    assert itin["Drive to Thimphu and tour the fortress before lunch."] == 2
    assert itin["Hike up to the temple above the valley floor."] == 3
    # the "Day N —" title lines are never units
    assert not any(s.startswith("Day ") for s in itin)


# ── unit_source ───────────────────────────────────────────────────────────────────────────────

def test_unit_source_sends_only_the_day_slice_and_header_for_itinerary_units():
    unit = {"field": "itineraries", "sentence": "Drive to Thimphu and tour the fortress before lunch.",
            "day": 2}
    src = gr.unit_source(TOUR_DAYS, unit)
    # compact header, not the whole tour
    assert "NAME: Bhutan Cultural Journey" in src
    assert "COUNTRY: Bhutan" in src and "DURATION: 3 days" in src
    # day 2 and its neighbours (1 and 3) are present; the summary field is not
    assert "Drive to Thimphu and visit the dzong." in src          # day 2 own text
    assert "Arrive in Paro and transfer to the hotel." in src      # day 1 (previous)
    assert "Hike to the temple above the valley." in src           # day 3 (next)
    assert "Three days across Paro and Thimphu." not in src        # the summary is excluded
    # inclusions / exclusions always ride along (meals/transport/services live there)
    assert "INCLUSIONS:" in src and "one lunch" in src
    assert "EXCLUSIONS:" in src and "International flights." in src
    # genuinely smaller than the whole-tour source
    assert len(src) < len(gr.source_text(TOUR_DAYS))


def test_unit_source_first_day_has_no_previous_day():
    unit = {"field": "itineraries", "sentence": "x", "day": 1}
    src = gr.unit_source(TOUR_DAYS, unit)
    assert "DAY 1:" in src and "DAY 2:" in src and "DAY 0:" not in src


def test_unit_source_full_source_for_non_itinerary_fields():
    for field in ("subtitle", "summary", "highlights"):
        unit = {"field": field, "sentence": "A journey across Paro and Thimphu.", "day": None}
        assert gr.unit_source(TOUR_DAYS, unit) == gr.source_text(TOUR_DAYS)


def test_unit_source_full_source_when_days_cannot_be_split():
    # one "Day" marker only → parser falls back → never guess a slice, use the whole source
    tour = {"name": "T", "itineraries": "Day 1\nArrive. Explore the town.", "duration": "2 days"}
    unit = {"field": "itineraries", "sentence": "Explore the town.", "day": 1}
    assert gr.unit_source(tour, unit) == gr.source_text(tour)


def test_unit_source_full_source_when_unit_has_no_day():
    unit = {"field": "itineraries", "sentence": "Something before any day title.", "day": None}
    assert gr.unit_source(TOUR_DAYS, unit) == gr.source_text(TOUR_DAYS)


# ── should_skip ───────────────────────────────────────────────────────────────────────────────

def test_should_skip_true_only_when_nothing_to_check():
    # no digit, no cap after first word, no promise cue → skip
    assert gr.should_skip("The journey continues gently onward.") is True
    assert gr.should_skip("A relaxed and unhurried morning follows.") is True


def test_should_skip_false_on_digit():
    assert gr.should_skip("The walk takes about 40 minutes.") is False


def test_should_skip_false_on_proper_noun_after_first_word():
    assert gr.should_skip("Continue toward Thimphu in the morning.") is False
    # a capitalised first word alone does not keep it
    assert gr.should_skip("Gentle walking is the order of the day.") is True


def test_should_skip_false_on_any_promise_cue():
    for s in ("A warm dinner is served.", "Transfer continues at ease.",
              "The views open up slowly.", "A guide walks alongside.",
              "An overnight follows the walk."):
        assert gr.should_skip(s) is False, s


# ── check_grounding ───────────────────────────────────────────────────────────────────────────

def test_check_grounding_reports_units_skipped_and_asks_per_unit_source(monkeypatch):
    captured = {}

    def fake_judge(units, sources):
        captured["sources"] = sources
        return {}

    monkeypatch.setattr(gr, "judge_units", fake_judge)
    res = gr.check_grounding(GEN_DAYS, TOUR_DAYS)
    units = gr.sentence_units(GEN_DAYS)
    skipped = {i for i, u in enumerate(units) if gr.should_skip(u["sentence"])}
    assert res["units"] == len(units)
    assert res["units_skipped"] == len(skipped)
    # Jev is asked only for the non-skipped units …
    assert set(captured["sources"]) == set(range(len(units))) - skipped
    # … and an itinerary unit is asked against its per-unit source, not the whole tour.
    full = gr.source_text(TOUR_DAYS)
    itin_idx = [i for i, u in enumerate(units)
                if u["field"] == "itineraries" and i not in skipped]
    assert itin_idx and all(captured["sources"][i] != full for i in itin_idx)
    assert all(len(captured["sources"][i]) < len(full) for i in itin_idx)


def test_check_grounding_numeric_still_runs_on_full_source(monkeypatch):
    monkeypatch.setattr(gr, "judge_units", lambda units, sources: {})
    gen = {"summary": "The palace grounds cover approximately 40 hectares in Paro."}
    res = gr.check_grounding(gen, TOUR_DAYS, use_jev=False)
    hits = [v for v in res["violations"] if v["code"] == "UNSUPPORTED_NUMBER"]
    assert hits and hits[0]["novel_numbers"] == ["40"]


# ── offline estimate script ─────────────────────────────────────────────────────────────────────

# second fixture: NO day markers → the splitter falls back → unit_source uses the full source.
TOUR_NO_MARKERS = {
    "name": "Lao River Days",
    "country": "Laos",
    "duration": "2 days",
    "itineraries": "Arrive and settle in. Explore the riverside market with a guide.",
}
GEN_NO_MARKERS = {
    "summary": "Two gentle days along the river in Laos.",
    "itineraries": "Arrive and settle into the riverside guesthouse for the first night.",
}


def test_estimate_clean_day_markers_cuts_source_chars():
    stats = estimate([{"tour": TOUR_DAYS, "generated": GEN_DAYS}])
    assert stats["units_before"] == len(gr.sentence_units(GEN_DAYS))
    assert stats["units_after"] <= stats["units_before"]
    assert stats["units_skipped"] == stats["units_before"] - stats["units_after"]
    # with day slices the per-unit source totals fewer chars than the whole-tour source per unit
    assert stats["chars_after"] < stats["chars_before"]


def test_estimate_no_markers_falls_back_to_full_source():
    stats = estimate([{"tour": TOUR_NO_MARKERS, "generated": GEN_NO_MARKERS}])
    units = gr.sentence_units(GEN_NO_MARKERS)
    asked = [u for u in units if not gr.should_skip(u["sentence"])]
    full = gr.source_text(TOUR_NO_MARKERS)
    # no day markers → every asked unit uses the FULL source, so after == full * asked-count
    assert stats["units_before"] == len(units)
    assert stats["units_after"] == len(asked)
    assert stats["chars_after"] == len(full) * len(asked)
