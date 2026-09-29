"""AA-641 — flag_fix must not create new hard failures: deterministic seo_meta fit (T3) and a
per-field revert when a repair introduces a forbidden word. Mock-only (no AWS, DB or LLM)."""
import json
from unittest.mock import MagicMock, patch

from services.content_generation.flag_fix_node import _revert_introduced_forbidden, flag_fix_node
from services.content_generation.forbidden_words import VALIDATE_FORBIDDEN, all_forbidden, forbidden_in
from services.content_generation.graph import _VALIDATE_FORBIDDEN, validate_node
from services.content_generation.seo_meta_utils import (
    SEO_META_MAX, SEO_META_MIN, fit_seo_meta, meta_complete_sentence, meta_in_band,
)

_HEAD = ("Trek hidden Himalayan valleys with seasoned mountain guides and quiet "
         "lodges across remote alpine ridgelines")


def _mk(n):
    """Complete sentence of exactly n chars ending in a period."""
    pad = n - len(_HEAD) - 2
    return _HEAD + " " + ("o" * pad) + "."


# ── A. deterministic seo_meta fit ──────────────────────────────────────────────────────────────

def test_over_long_meta_is_fitted_into_band_as_a_complete_sentence():
    meta = _mk(148) + " Then a second sentence that pushes the whole meta far past the limit."
    assert len(meta) > SEO_META_MAX
    out = fit_seo_meta(meta)
    assert SEO_META_MIN <= len(out) <= SEO_META_MAX
    assert meta_complete_sentence(out) and out == _mk(148)


def test_meta_with_no_valid_candidate_is_left_untouched():
    too_short = "A short meta that is a sentence but far below the band floor."
    assert fit_seo_meta(too_short) == too_short
    no_boundary = "word " * 60            # over-long, no sentence end anywhere in band
    assert fit_seo_meta(no_boundary) == no_boundary


def test_in_band_meta_is_unchanged_and_forbidden_prefix_is_not_used():
    ok = _mk(150)
    assert fit_seo_meta(ok) == ok
    bad = _HEAD + " for a budget price " + ("o" * 20) + ". Another sentence closes it out here nicely."
    assert not meta_in_band(fit_seo_meta(bad), {"budget"})


def test_tenant_words_count_as_forbidden_for_the_fit():
    meta = _mk(148) + " Then a second sentence that pushes the whole meta far past the limit."
    assert fit_seo_meta(meta, tenant_forbidden=["himalayan"]) == meta   # only candidate has it


# ── B. per-field forbidden-word revert ────────────────────────────────────────────────────────

def test_repair_that_adds_a_forbidden_word_is_reverted_for_that_field_only():
    before = {"summary": "A quiet walk.", "itineraries": "Day 1: walk to the lake.", "name": "Lake"}
    after = {"summary": "A stunning walk.", "itineraries": "Day 1: walk to the lake at dawn.", "name": "Lake"}
    reverted = _revert_introduced_forbidden(before, after, {"summary", "itineraries"}, VALIDATE_FORBIDDEN)
    assert reverted == {"summary": ["stunning"]}
    assert after["summary"] == "A quiet walk."                           # reverted
    assert after["itineraries"] == "Day 1: walk to the lake at dawn."    # unaffected, passes through


def test_field_that_already_had_the_word_keeps_its_repair():
    before = {"summary": "A stunning walk, too long."}
    after = {"summary": "A stunning walk."}
    assert _revert_introduced_forbidden(before, after, {"summary"}, VALIDATE_FORBIDDEN) == {}
    assert after["summary"] == "A stunning walk."


def _llm_returning(payload: dict):
    resp = MagicMock(content=json.dumps(payload), model_used="haiku", cost_usd=0.001,
                     input_tokens=10, output_tokens=5, stop_reason="end_turn",
                     satellite_account=None, fallback_used=False)
    client = MagicMock()
    client.generate.return_value = resp
    return client


def test_flag_fix_node_reverts_the_field_and_tells_the_model_the_words():
    state = {
        "brand_audit_status": "flagged",
        "brand_audit_codes": ["SUMMARY_OFF_BRAND", "HIGHLIGHTS_TOO_GENERIC"],
        "brand_audit_issues": ["summary off brand", "highlights generic"],
        "brand_forbidden_words": ["glamping"],
        "failure_codes": [],
        "generated": {"name": "Lake Walk", "summary": "A quiet walk.", "highlights": ["Lake"]},
        "tour": {"duration": "1 day"},
    }
    client = _llm_returning({"summary": "A breathtaking glamping walk.", "highlights": ["Mirror lake at dawn"]})
    with patch("services.content_generation.flag_fix_node.LLMClient", return_value=client), \
         patch("services.content_generation.flag_fix_node.record_call_sync"):
        out = flag_fix_node(state)

    prompt = client.generate.call_args.args[0].user_prompt
    assert "NEVER USE" in prompt and "stunning" in prompt and "glamping" in prompt
    assert out["generated"]["summary"] == "A quiet walk."                 # reverted
    assert out["generated"]["highlights"] == ["Mirror lake at dawn"]      # kept
    assert out["fix_pass_fields"] == ["highlights"]


# ── shared list stays one source of truth ─────────────────────────────────────────────────────

def test_graph_re_exports_the_shared_list():
    assert _VALIDATE_FORBIDDEN is VALIDATE_FORBIDDEN


def test_validate_matches_tenant_words_and_ignores_blank_ones():
    assert all_forbidden(["Glamping", " ", ""]) == VALIDATE_FORBIDDEN + ["glamping"]
    assert forbidden_in({"summary": "A Glamping trip"}, ["glamping"]) == {"glamping"}
    state = {"generated": {"name": "Lake", "summary": "A quiet walk."}, "tour": {},
             "brand_forbidden_words": [" "], "retry_count": 0}
    assert "FORBIDDEN_WORD" not in (validate_node(state).get("failure_codes") or [])
