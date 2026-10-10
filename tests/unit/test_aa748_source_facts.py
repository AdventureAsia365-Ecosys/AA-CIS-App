"""AA-748 — structured per-day source facts for the S1 writer (behind S1_STRUCTURED_FACTS).

Covers the design contract's test list:
- the day splitter is reused (no second one);
- the honesty guard drops a fabricated number and a fabricated meal;
- parser fallback (no day markers) -> no facts;
- flag OFF -> build_rewrite_prompt output byte-identical to today;
- flag ON (facts present) -> the SOURCE FACTS BY DAY block + its rule appear.

LLMClient is always mocked — no real LLM/AWS call.
"""
from types import SimpleNamespace

import pytest

from services.content_generation import prompts
from services.content_generation import source_facts as sf

_S1_FACTS_ENV = "S1_STRUCTURED_FACTS"

TOUR = {
    "name": "Bhutan Cultural Journey",
    "country": "Bhutan",
    "duration": "3 days",
    "summary": "Three days across Paro and Thimphu.",
    "itineraries": (
        "Day 1\nArrive in Paro and drive 54 km to Thimphu. Breakfast included.\n"
        "Day 2\nHike to Taktsang Monastery at 3120 m.\n"
        "Day 3\nDepart from Paro."
    ),
    "inclusions": "Breakfast daily. Private vehicle.",
}


class _FakeClient:
    """Stand-in for LLMClient — returns a canned JSON string, records no real call."""

    def __init__(self, payload):
        self._payload = payload

    def generate(self, request):
        return SimpleNamespace(
            content=self._payload, model_used="haiku", provider="claude",
            input_tokens=100, output_tokens=50, cost_usd=0.0001,
            fallback_used=False, satellite_account="acc3", stop_reason="end_turn",
        )


@pytest.fixture(autouse=True)
def _no_log_write(monkeypatch):
    """record_call_sync would try to open the DB; stub it so the extractor runs offline."""
    monkeypatch.setattr("shared.llm_client.call_log.record_call_sync", lambda **kw: None)


@pytest.fixture(autouse=True)
def _flag_off_by_default(monkeypatch):
    monkeypatch.delenv(_S1_FACTS_ENV, raising=False)


# --- flag resolution ---------------------------------------------------------------------------

def test_flag_default_off():
    assert sf.s1_structured_facts_enabled() is False
    assert sf.s1_structured_facts_enabled({}) is False


def test_flag_env_on(monkeypatch):
    monkeypatch.setenv(_S1_FACTS_ENV, "true")
    assert sf.s1_structured_facts_enabled() is True


def test_tenant_flag_overrides_env(monkeypatch):
    monkeypatch.setenv(_S1_FACTS_ENV, "true")
    assert sf.s1_structured_facts_enabled({"S1_STRUCTURED_FACTS": "false"}) is False
    monkeypatch.delenv(_S1_FACTS_ENV, raising=False)
    assert sf.s1_structured_facts_enabled({"S1_STRUCTURED_FACTS": "on"}) is True


# --- day splitter reuse ------------------------------------------------------------------------

def test_source_days_reuses_the_one_splitter():
    day_text, used_fallback = sf._source_days(TOUR)
    assert used_fallback is False
    assert set(day_text) == {1, 2, 3}
    assert "Thimphu" in day_text[1]
    assert "Taktsang" in day_text[2]


def test_fallback_source_returns_no_facts():
    # No "Day N" markers at all -> parser fallback -> no facts, prompt stays as today.
    tour = {"name": "No markers", "duration": "3 days",
            "itineraries": "A long paragraph with no day headers whatsoever, just prose."}
    out = sf.extract_day_facts(tour, client=_FakeClient("{}"))
    assert out == {"days": [], "used_fallback": True}


# --- honesty guard -----------------------------------------------------------------------------

def test_honesty_guard_drops_fabricated_number_and_meal():
    # Model returns a fabricated altitude (9999 m, not in source), a real one (3120 m), a
    # fabricated meal (dinner, not in source) and a real one (breakfast, in source).
    payload = (
        '{"days": ['
        '{"day": 1, "places": ["Paro", "Thimphu"], "distances": ["54 km"],'
        ' "meals": ["breakfast", "dinner"]},'
        '{"day": 2, "places": ["Taktsang Monastery"], "altitudes": ["3120 m", "9999 m"]}'
        ']}'
    )
    out = sf.extract_day_facts(TOUR, client=_FakeClient(payload))
    assert out["used_fallback"] is False
    days = {d["day"]: d for d in out["days"]}
    # real figures kept
    assert "54 km" in days[1]["distances"]
    assert "3120 m" in days[2]["altitudes"]
    # fabricated altitude dropped (9999 never in source)
    assert "9999 m" not in days[2]["altitudes"]
    # real meal kept, fabricated meal dropped
    assert "breakfast" in days[1]["meals"]
    assert "dinner" not in days[1]["meals"]
    # places are prose, not number-guarded — kept
    assert days[1]["places"] == ["Paro", "Thimphu"]


def test_extract_fails_open_on_llm_error():
    class _Boom:
        def generate(self, request):
            raise RuntimeError("bedrock down")

    out = sf.extract_day_facts(TOUR, client=_Boom())
    assert out == {"days": [], "used_fallback": False}


# --- prompt wiring -----------------------------------------------------------------------------

def _seo():
    return {"keywords": {"top_keywords": ["bhutan tour"]}, "people_also_ask": ["Is Bhutan safe?"]}


def test_prompt_flag_off_byte_identical():
    # structured_facts default (None) must produce the exact same string as before this change.
    baseline = prompts.build_rewrite_prompt(TOUR, _seo())
    with_none = prompts.build_rewrite_prompt(TOUR, _seo(), structured_facts=None)
    with_empty = prompts.build_rewrite_prompt(TOUR, _seo(), structured_facts={"days": []})
    assert with_none == baseline
    assert with_empty == baseline
    assert "SOURCE FACTS BY DAY" not in baseline


def test_prompt_flag_on_adds_facts_block_and_rule():
    facts = {"days": [
        {"day": 1, "places": ["Paro", "Thimphu"], "distances": ["54 km"], "meals": ["breakfast"],
         "activities": [], "transport": [], "durations": [], "altitudes": [], "times": [],
         "other_numbers": []},
        {"day": 2, "places": ["Taktsang Monastery"], "altitudes": ["3120 m"],
         "activities": ["hike"], "transport": [], "distances": [], "durations": [], "times": [],
         "meals": [], "other_numbers": []},
    ], "used_fallback": False}
    out = prompts.build_rewrite_prompt(TOUR, _seo(), structured_facts=facts)
    assert "SOURCE FACTS BY DAY" in out
    assert "SOURCE FACTS RULE" in out
    assert "Day 1:" in out and "Day 2:" in out
    assert "places: Paro, Thimphu" in out
    assert "altitudes: 3120 m" in out
    # the block sits before PER-DAY SOURCE LENGTH
    assert out.index("SOURCE FACTS BY DAY") < out.index("PER-DAY SOURCE LENGTH")
    # the raw itinerary is still in the prompt (facts constrain, not replace)
    assert "Taktsang" in out


def test_render_block_empty_when_no_nonempty_fields():
    # a day with every field empty renders nothing (so the flag-off invariant holds for empty facts)
    assert prompts._render_source_facts_block({"days": [{"day": 1, "places": []}]}) == ""
