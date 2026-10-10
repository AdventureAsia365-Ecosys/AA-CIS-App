"""AA-756 — grounding token trim: per-unit source (no skip rule).

Covers (mock-only, no DB / no network):
  - sentence_units() carries the source day number for itinerary units.
  - unit_source(): day-slice (prev/this/next) + header + inclusions/exclusions for an itinerary
    unit; full source for subtitle/summary/highlights and for the fallback / no-day cases.
  - check_grounding(): asks Jev for EVERY unit (per-unit source, no skip rule — the removed keyword
    skip missed exactly the tour promises the policy forbids); the numeric check still runs on the
    FULL source.
  - scripts/aa756_grounding_tokens.estimate(): source chars before/after on a clean-day-marker tour
    and a no-marker (fallback) tour.
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
    # day 2 and its neighbours (1 and 3) are present, plus the source summary (S224)
    assert "Drive to Thimphu and visit the dzong." in src          # day 2 own text
    assert "Arrive in Paro and transfer to the hotel." in src      # day 1 (previous)
    assert "Hike to the temple above the valley." in src           # day 3 (next)
    assert "SUMMARY:\nThree days across Paro and Thimphu." in src   # tour-wide facts live there
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


# ── check_grounding — every unit asked, per-unit source, no skip ─────────────────────────────────

def test_check_grounding_asks_every_unit_with_per_unit_source(monkeypatch):
    captured = {}

    def fake_judge(units, sources):
        captured["sources"] = sources
        return {}

    monkeypatch.setattr(gr, "judge_units", fake_judge)
    res = gr.check_grounding(GEN_DAYS, TOUR_DAYS)
    units = gr.sentence_units(GEN_DAYS)
    assert res["units"] == len(units)
    assert "units_skipped" not in res                         # the skip rule is gone
    # every unit is asked (no skip rule) …
    assert set(captured["sources"]) == set(range(len(units)))
    # … and an itinerary unit is asked against its per-unit source, not the whole tour.
    full = gr.source_text(TOUR_DAYS)
    itin_idx = [i for i, u in enumerate(units) if u["field"] == "itineraries"]
    assert itin_idx and all(captured["sources"][i] != full for i in itin_idx)
    assert all(len(captured["sources"][i]) < len(full) for i in itin_idx)


def test_check_grounding_asks_descriptive_cue_free_sentence(monkeypatch):
    # the kind of sentence the removed keyword skip wrongly dropped (a forbidden tour promise with
    # no digit, no proper noun, no cue word) must still be asked.
    captured = {}
    monkeypatch.setattr(gr, "judge_units",
                        lambda units, sources: captured.setdefault("sources", sources) or {})
    gen = {"summary": "A full-day excursion to two medieval centers follows."}
    gr.check_grounding(gen, TOUR_DAYS)
    assert len(captured["sources"]) == len(gr.sentence_units(gen)) == 1


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
    assert stats["units"] == len(gr.sentence_units(GEN_DAYS))
    # with day slices the per-unit source totals fewer chars than the whole-tour source per unit
    assert stats["chars_after"] < stats["chars_before"]


def test_estimate_no_markers_falls_back_to_full_source():
    stats = estimate([{"tour": TOUR_NO_MARKERS, "generated": GEN_NO_MARKERS}])
    units = gr.sentence_units(GEN_NO_MARKERS)
    full = gr.source_text(TOUR_NO_MARKERS)
    # no day markers → every unit uses the FULL source, so after == full * unit-count (no saving)
    assert stats["units"] == len(units)
    assert stats["chars_after"] == len(full) * len(units)
    assert stats["chars_before"] == stats["chars_after"]


# ── a1_promise_unsupported (S224) ─────────────────────────────────────────────────────────────

def test_judge_all_asks_the_promise_question_in_the_same_call(monkeypatch):
    """The type-B question rides in the same decide() call; grounding still keys on QUESTION only."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    asked = []

    async def fake_decide(stage, subject, state, keys, pool=None):
        asked.append(keys)
        return SimpleNamespace(verdicts={gr.QUESTION: "claim-verdict", gr.PROMISE_QUESTION: "promise-verdict"})

    pool = MagicMock()
    pool.close = AsyncMock()
    monkeypatch.setattr("shared.llm_client.decide.decide", fake_decide)
    monkeypatch.setattr("asyncpg.create_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr("shared.secrets.get_database_url", lambda: "postgres://x")
    units = [{"field": "summary", "sentence": "A journey across Paro and Thimphu.", "day": None}]
    out = asyncio.run(gr._judge_all(units, {0: "SOURCE"}))
    assert asked == [[gr.QUESTION, gr.PROMISE_QUESTION]]
    assert out == {0: "claim-verdict"}
