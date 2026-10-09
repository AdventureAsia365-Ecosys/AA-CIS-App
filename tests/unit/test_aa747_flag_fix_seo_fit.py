"""AA-747 item 1 — flag_fix stops sending seo_meta / seo_title to the LLM when the deterministic
fit (fit_seo_meta_final / fit_seo_title, AA-740) already resolves that field's codes.

Covers: a length/incomplete-sentence code dropped only when the fit resolves it (no LLM call when
nothing is left); content codes (BRAND_SEO_META_VIOLATION kept when the fit does NOT resolve it,
META_OPENER_ROBOTIC) stay with the LLM. No live Bedrock — LLMClient is mocked.
"""
import json
from unittest.mock import MagicMock, patch

from services.content_generation.flag_fix_node import (
    _apply_seo_fit_before_llm, _seo_code_fires, flag_fix_node,
)
from services.content_generation.seo_meta_utils import (
    SEO_META_MAX, fit_seo_meta_final, meta_in_band,
)


def _state(**overrides) -> dict:
    base = {
        "brand_audit_status": "flagged",
        "brand_audit_codes": [],
        "brand_audit_issues": [],
        "brand_audit_fields": [],
        "failure_codes": [],
        "lessons_extracted": [],
        "generated": {
            "name": "Bhutan Highland Journey",
            "subtitle": "A measured highland route",
            "summary": "A factual Bhutan trip.",
            "highlights": ["Taktsang hike", "Punakha dzong", "Dochula pass"],
            "itineraries": "Day 1 — Arrive in Paro.\nDay 2 — Thimphu.",
            "seo_title": "Bhutan Highland Journey",
            "seo_meta": "A measured Bhutan journey.",
        },
        "tour": {"duration": "10 days", "country": "Bhutan"},
        "seo": {},
        "cost_usd": 0.0,
        "model_tier": "haiku",
    }
    base.update(overrides)
    return base


# ── _seo_code_fires: re-check on the fitted value ────────────────────────────

def test_seo_code_fires_length_recheck():
    long_meta = "x" * 200
    assert _seo_code_fires("SEO_META_TOO_LONG", long_meta, "", set()) is True
    assert _seo_code_fires("SEO_META_TOO_LONG", "y" * 150, "", set()) is False
    assert _seo_code_fires("META_TOO_SHORT", "short.", "", set()) is True
    assert _seo_code_fires("SEO_TITLE_TOO_LONG", "t" * 70, "t" * 70, set()) is True
    assert _seo_code_fires("SEO_TITLE_TOO_LONG", "ok", "ok", set()) is False


def test_seo_code_fires_content_code_always_true():
    # a content/brand code is never deterministically re-checkable here → keep with the LLM
    assert _seo_code_fires("META_OPENER_ROBOTIC", "any complete sentence here.", "", set()) is True


# ── _apply_seo_fit_before_llm: drop codes/fields the fit resolves ────────────

def test_fit_resolves_too_long_drops_seo_meta_from_llm():
    """An over-length one-sentence meta the fit lands in band → seo_meta dropped from fix_keys and
    the fitted value written to content (no LLM needed for it)."""
    meta = ("This Bhutan journey covers Paro, Thimphu and Punakha with unhurried pacing and expert "
            "local guides throughout the route, and a comfortable private vehicle for every transfer "
            "between the valleys.")
    assert len(meta) > SEO_META_MAX
    fitted = fit_seo_meta_final(meta, {"duration": "10 days", "country": "Bhutan"}, [])
    assert meta_in_band(fitted, set()), "test premise: the fit must land this meta in band"

    state = _state(brand_audit_codes=["SEO_META_TOO_LONG"],
                   failure_codes=["SEO_META_TOO_LONG"])
    state["generated"]["seo_meta"] = meta
    content = dict(state["generated"])
    new_keys, dropped, changed = _apply_seo_fit_before_llm(state, content, {"seo_meta"})

    assert "seo_meta" not in new_keys          # no LLM call for seo_meta
    assert "SEO_META_TOO_LONG" in dropped
    assert content["seo_meta"] == fitted       # deterministic fix kept
    assert changed["seo_meta"] == fitted


def test_fit_does_not_resolve_keeps_seo_meta_for_llm():
    """A content code (META_OPENER_ROBOTIC) that the fit cannot resolve → seo_meta stays in the LLM
    request even if the meta is already in band on length."""
    meta = ("This measured Bhutan journey covers Paro, Thimphu and Punakha with unhurried pacing "
            "and comfortable transfers throughout.")
    state = _state(brand_audit_codes=["META_OPENER_ROBOTIC"])
    state["generated"]["seo_meta"] = meta
    content = dict(state["generated"])
    new_keys, dropped, changed = _apply_seo_fit_before_llm(state, content, {"seo_meta"})

    assert "seo_meta" in new_keys              # content code → still needs the LLM
    assert "META_OPENER_ROBOTIC" not in dropped


def test_fit_keeps_brand_violation_when_forbidden_word_remains():
    """BRAND_SEO_META_VIOLATION stays with the LLM when the fit cannot drop the budget word (it is
    in the middle of the only sentence, not a trailing clause)."""
    meta = "A budget Bhutan trip through Paro and Thimphu with guides and transfers throughout today."
    state = _state(brand_audit_codes=["BRAND_SEO_META_VIOLATION"])
    state["generated"]["seo_meta"] = meta
    content = dict(state["generated"])
    new_keys, dropped, changed = _apply_seo_fit_before_llm(state, content, {"seo_meta"})

    assert "seo_meta" in new_keys
    assert "BRAND_SEO_META_VIOLATION" not in dropped


# ── flag_fix_node end-to-end: no LLM call when the fit resolves everything ───

def test_flag_fix_makes_no_llm_call_when_fit_resolves_only_code():
    """Only code is SEO_META_TOO_LONG and the fit lands it in band → flag_fix makes NO LLM call,
    returns fix_pass_applied=True with the fitted meta."""
    meta = ("This Bhutan journey covers Paro, Thimphu and Punakha with unhurried pacing and expert "
            "local guides throughout the route, and a comfortable private vehicle for every transfer "
            "between the valleys.")
    fitted = fit_seo_meta_final(meta, {"duration": "10 days", "country": "Bhutan"}, [])
    assert meta_in_band(fitted, set())

    state = _state(brand_audit_status="pass",            # not brand-flagged
                   failure_codes=["SEO_META_TOO_LONG"])  # deterministic SEO code only
    state["generated"]["seo_meta"] = meta

    with patch("services.content_generation.flag_fix_node.LLMClient") as MockClient:
        result = flag_fix_node(state)
        MockClient.return_value.generate.assert_not_called()

    assert result["fix_pass_applied"] is True
    assert "seo_meta" in result["fix_pass_fields"]
    assert result["generated"]["seo_meta"] == fitted


def test_flag_fix_still_calls_llm_for_content_code():
    """A content code (META_OPENER_ROBOTIC) the fit can't resolve → the LLM is still called for
    seo_meta."""
    state = _state(brand_audit_codes=["META_OPENER_ROBOTIC"],
                   brand_audit_fields=["seo_meta"])
    state["generated"]["seo_meta"] = "Discover Bhutan with us on a journey through the valleys here."

    fixed = "A measured Bhutan journey through Paro and Thimphu for independent travellers all year."
    resp = MagicMock()
    resp.content = json.dumps({"seo_meta": fixed})
    resp.cost_usd = 0.001

    with patch("services.content_generation.flag_fix_node.LLMClient") as MockClient:
        MockClient.return_value.generate.return_value = resp
        result = flag_fix_node(state)
        # The LLM is called for the content code (one main fix call, plus the bounded seo_meta
        # band re-repair loop may add more — the point is the fit did NOT skip the LLM here).
        assert MockClient.return_value.generate.call_count >= 1

    assert result["fix_pass_applied"] is True
    assert "seo_meta" in result["fix_pass_fields"]
