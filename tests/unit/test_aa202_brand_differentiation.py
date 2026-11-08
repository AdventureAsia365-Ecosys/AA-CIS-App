"""AA-202 [AA-193·F1-followup]: brand differentiation profile injection.

The AA-198 resolver only selected 6 legacy brand columns, so the differentiating columns
(core_idea, customer_segment, customer_mindset, voice_examples, good_examples) never reached
generation → all brands read alike. `_build_brand_diff_block` assembles a structured profile +
contrast rule into the system prompt. Pure string assembly — no DB/LLM/network.
"""

from services.content_generation.batch_prompt import build_brand_diff_block as _build_brand_diff_block


def test_diff_block_full_profile():
    """All differentiating fields present → block contains profile + contrast + field text."""
    state = {
        "brand_core_idea": "Discreet executive adventure",
        "brand_customer_segment": "Senior professionals, $250k+",
        "brand_customer_mindset": "Wants privacy and effortless logistics",
        "brand_voice_examples": ["understated", "assured", "precise"],
        "brand_good_examples": "Dawn over Halong, just your crew and the mist.",
    }
    block = _build_brand_diff_block(state)
    assert "BRAND DIFFERENTIATION PROFILE" in block
    assert "Discreet executive adventure" in block
    assert "Senior professionals, $250k+" in block
    assert "Wants privacy and effortless logistics" in block
    assert "understated, assured, precise" in block
    assert "Dawn over Halong" in block
    assert "CONTRAST REQUIREMENT" in block


def test_diff_block_absent_for_legacy_brand():
    """Old/default brand with none of the new fields → empty block (backward-compatible)."""
    state = {
        "brand_system_prompt": "Some legacy system prompt",
        "brand_style_guide": "Legacy style",
        "brand_forbidden_words": ["cheap"],
    }
    block = _build_brand_diff_block(state)
    assert block == ""
    assert "BRAND DIFFERENTIATION PROFILE" not in block


def test_diff_block_or_condition_mindset_only():
    """core_idea + voice empty but customer_mindset present → block still built (OR condition)."""
    state = {
        "brand_core_idea": "",
        "brand_customer_segment": "",
        "brand_customer_mindset": "Wants slow, immersive culture",
        "brand_voice_examples": [],
        "brand_good_examples": "",
    }
    block = _build_brand_diff_block(state)
    assert "BRAND DIFFERENTIATION PROFILE" in block
    assert "Wants slow, immersive culture" in block
    assert "CONTRAST REQUIREMENT" in block
    # absent fields must not emit their labels
    assert "Core idea:" not in block
    assert "Voice (tone words):" not in block


def test_diff_block_voice_examples_only():
    """Only voice_examples present (core_idea/mindset empty) → OR condition still triggers block."""
    state = {
        "brand_voice_examples": ["playful", "warm"],
    }
    block = _build_brand_diff_block(state)
    assert "BRAND DIFFERENTIATION PROFILE" in block
    assert "playful, warm" in block


def test_diff_block_filters_empty_voice_entries():
    """Empty/None entries in voice_examples are filtered out before join."""
    state = {
        "brand_core_idea": "Core",
        "brand_voice_examples": ["assured", "", None, "precise"],
    }
    block = _build_brand_diff_block(state)
    assert "assured, precise" in block
