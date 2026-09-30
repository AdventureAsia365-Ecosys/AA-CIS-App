"""AA-694 A3-1 — the per-day atomize prompt fences the whole-tour preamble off as context.

Calibration (docs/calibration/a3_atom_in_text.md) found 63% of platform atoms were not in their own
day's text: build_day_user_prompt() sent the tour summary/highlights on every day with nothing
saying they were context only, so the model mined them each time.
"""
from services.acp_shared.atom_extraction import SYSTEM_PROMPT, build_day_user_prompt, day_fingerprint

ROW = {
    "name": "Bhutan Cultural Trek",
    "aa_summary": "Visit Punakha Dzong and raft the Mo Chu.",
    "aa_highlights": ["Hike to Tiger's Nest", "Raft the Mo Chu"],
    "inclusions": "All meals",
    "exclusions": "International flights",
}


def test_day_prompt_puts_preamble_under_context_and_day_last():
    prompt = build_day_user_prompt(ROW, 3, "Thimphu", "Free day in Thimphu.")
    ctx_at = prompt.index("TOUR CONTEXT (background only")
    day_at = prompt.index("DAY 3 (extract atoms only from this section)")
    assert ctx_at < prompt.index("Hike to Tiger's Nest") < day_at
    assert prompt.index("Free day in Thimphu.") > day_at
    assert prompt.rstrip().endswith("Free day in Thimphu.")


def test_day_prompt_drops_trip_wide_inclusions():
    prompt = build_day_user_prompt(ROW, 1, "Arrival", "Arrive at Paro.")
    assert "All meals" not in prompt and "International flights" not in prompt


def test_system_prompt_says_extract_only_from_day_section():
    assert "extract atoms ONLY from the DAY" in SYSTEM_PROMPT
    assert "evidence` must be quoted from the DAY section" in SYSTEM_PROMPT


def test_fingerprint_depends_on_system_prompt():
    """The rule change must re-read every day on the next run — the fingerprint hashes SYSTEM_PROMPT."""
    import services.acp_shared.atom_extraction as ax

    fp_now = day_fingerprint("Thimphu", "Free day in Thimphu.", "m")
    original = ax.SYSTEM_PROMPT
    try:
        ax.SYSTEM_PROMPT = original + " "
        assert ax.day_fingerprint("Thimphu", "Free day in Thimphu.", "m") != fp_now
    finally:
        ax.SYSTEM_PROMPT = original
