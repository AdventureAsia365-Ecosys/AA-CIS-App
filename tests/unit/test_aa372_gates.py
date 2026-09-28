"""
tests/unit/test_aa372_gates.py — services/acp_produce/gates.py F2/F3/F4/F6/F7
(AA-372) + F9 social rubric, tested directly (not through the pipeline).

Each gate gets >=1 pass case and >=1 fail case (AA-372 VERIFY checklist).
Follows test_aa298_gates.py's convention of calling the gate functions
directly with hand-built inputs, and test_aa298_judge.py's convention of
patching services.acp_produce.judge_client.boto3.client for the LLM gate.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from services.acp_produce.gates import (SOCIAL_SEO_FAILURE_CODES, gate_banned_patterns,
                                          gate_brand_seo_audit, gate_brand_seo_audit_social,
                                          gate_brief_compliance, gate_faq_dedup,
                                          gate_route_to_sellable, gate_structural_variance)
from services.acp_produce.models import Brief, KeywordRecord


@pytest.fixture(autouse=True)
def _pin_judge_model_to_nova_pro(monkeypatch):
    """F9's tests here patch services.acp_produce.judge_client.boto3.client with
    Nova/Converse-shaped mocks, none of it about which backend is the production
    default. AA-518 (02/09/2026) changed invoke_judge()'s default "nova_pro" ->
    "gpt41" -- pin it back here so these tests keep exercising the same Nova Pro
    path/mock shape they always have."""
    monkeypatch.setenv("JUDGE_MODEL", "nova_pro")
    # AA-659: production call sites now pass stage= and go through the gateway route; send the
    # route to the mocked Nova Pro backend so these gate-logic tests stay offline and unchanged.
    from services.acp_produce import judge_client
    monkeypatch.setattr(judge_client, "_invoke_judge_via_route",
                        lambda s, u, m, stage: judge_client._invoke_judge_legacy(s, u, m, "nova_pro"))


def _bedrock_response(data: dict):
    payload = {"output": {"message": {"content": [{"text": json.dumps(data)}], "role": "assistant"}},
               "usage": {"inputTokens": 10, "outputTokens": 5}}
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode()
    return {"body": body}


# ── F2 banned patterns ──

def test_f2_flags_a_banned_pattern_with_no_citation():
    result = gate_banned_patterns(
        "This trip takes you to a hidden gem in the mountains.", {}
    )
    assert result.gate == "F2_banned_patterns"
    assert result.passed is False
    assert any("hidden gem" in v for v in result.violations)


def test_f2_passes_clean_body():
    result = gate_banned_patterns(
        "The rickshaw ride opens the trip [R:atom_1].",
        {"atom_1": "Ride a rickshaw through Chandni Chowk."},
    )
    assert result.passed is True
    assert result.violations == []


def test_f2_b12_exempts_a_banned_phrase_genuinely_verbatim_in_the_cited_atom():
    """B12 fix: the atom's OWN source text legitimately contains 'hidden gem'
    (e.g. a guide's own description) — citing it verbatim must not trip F2."""
    result = gate_banned_patterns(
        "The guide calls it a hidden gem of the old town [R:atom_1].",
        {"atom_1": "Locals call this spot a hidden gem of the old town."},
    )
    assert result.passed is True


def test_f2_b12_does_not_exempt_a_banned_phrase_the_writer_added_near_a_citation():
    """A citation tag alone is not a free pass — the matched phrase must
    actually be present in the cited text, or it still fails."""
    result = gate_banned_patterns(
        "This is an unforgettable stop on the trip [R:atom_1].",
        {"atom_1": "The market opens at 8am."},
    )
    assert result.passed is False
    assert any("unforgettable" in v for v in result.violations)


# ── F3 structural variance ──

def test_f3_passes_blog_body_with_variance():
    body = "Short opener.\n\n" + "This is a much longer paragraph with several sentences. " * 5
    result = gate_structural_variance(body, "blog")
    assert result.gate == "F3_structural_variance"
    assert result.passed is True


def test_f3_fails_blog_body_with_no_one_sentence_paragraph():
    body = ("This paragraph has two sentences in it. It never stands alone.\n\n"
            "Neither does this one. It also has two sentences.")
    result = gate_structural_variance(body, "blog")
    assert result.passed is False
    assert any("one-sentence paragraph" in v for v in result.violations)


def test_f3_fails_blog_body_with_more_than_one_bulleted_list():
    body = (
        "Short opener.\n\n"
        "- item one\n- item two\n\n"
        "Some more prose here to separate the lists apart.\n\n"
        "- item three\n- item four"
    )
    result = gate_structural_variance(body, "blog")
    assert result.passed is False
    assert any("bulleted list" in v for v in result.violations)


def test_f3_no_ops_for_non_blog_channel():
    body = "Two sentences here. Never one alone. Two sentences here. Never one alone."
    result = gate_structural_variance(body, "facebook")
    assert result.passed is True


# ── F4 brief compliance ──

def _brief(**overrides):
    base = dict(
        brief_id="b1", slot_id="s1", keyword="rickshaw",
        demand=KeywordRecord(keyword="rickshaw", location="US"),
        required_h2s=["Getting there"], word_range=(5, 50),
        cta_target="https://example.com/trip", internal_links=[],
        framework="hub",
    )
    base.update(overrides)
    return Brief(**base)


def test_f4_passes_compliant_body():
    body = "## Getting there\n\nRide a rickshaw through the old town at dawn every single day."
    result = gate_brief_compliance(body, "blog", _brief())
    assert result.gate == "F4_brief_compliance"
    assert result.passed is True


def test_f4_fails_when_keyword_missing():
    body = "## Getting there\n\nRide a tuk-tuk through the old town at dawn."
    result = gate_brief_compliance(body, "blog", _brief())
    assert result.passed is False
    assert any("rickshaw" in v for v in result.violations)


def test_f4_fails_when_required_h2_missing():
    body = "## Somewhere else\n\nRide a rickshaw through the old town at dawn."
    result = gate_brief_compliance(body, "blog", _brief())
    assert result.passed is False
    assert any("Getting there" in v for v in result.violations)


def test_f4_fails_closed_when_brief_is_none_for_blog():
    result = gate_brief_compliance("Any body at all.", "blog", None)
    assert result.passed is False
    assert any("no Brief" in v for v in result.violations)


def test_f4_no_ops_for_non_blog_channel_even_without_brief():
    result = gate_brief_compliance("Any body at all.", "facebook", None)
    assert result.passed is True


# ── F6 route-to-sellable ──

def test_f6_passes_blog_with_cta_alive_and_in_body():
    result = gate_route_to_sellable(
        "Book it at https://example.com/trip.", "blog",
        "https://example.com/trip", True,
    )
    assert result.gate == "F6_route_to_sellable"
    assert result.passed is True


def test_f6_fails_closed_when_no_cta_target():
    result = gate_route_to_sellable("Some body.", "blog", None, True)
    assert result.passed is False
    assert any("no CTA target" in v for v in result.violations)


def test_f6_fails_closed_when_url_alive_is_none_no_row():
    result = gate_route_to_sellable(
        "Book it at https://example.com/trip.", "blog",
        "https://example.com/trip", None,
    )
    assert result.passed is False
    assert any("not confirmed alive" in v for v in result.violations)


def test_f6_fails_closed_when_url_alive_is_false():
    result = gate_route_to_sellable(
        "Book it at https://example.com/trip.", "blog",
        "https://example.com/trip", False,
    )
    assert result.passed is False


def test_f6_fails_blog_when_cta_not_literally_in_body():
    result = gate_route_to_sellable("Book your trip today.", "blog",
                                     "https://example.com/trip", True)
    assert result.passed is False
    assert any("not present in body" in v for v in result.violations)


def test_f6_skips_literal_cta_check_for_facebook():
    result = gate_route_to_sellable(
        "Come see the sunrise with us! Link in bio.", "facebook",
        "https://example.com/trip", True,
    )
    assert result.passed is True


# ── F7 FAQ dedup (intra-piece) ──

def test_f7_passes_body_with_no_faq_section():
    result = gate_faq_dedup("Just a plain body, no FAQ block at all.")
    assert result.gate == "F7_faq_dedup"
    assert result.passed is True


def test_f7_passes_faq_that_adds_new_information():
    body = (
        "The market opens early and closes by noon most days.\n\n"
        "## FAQ\n\n"
        "**Q: Is the market open on Sundays?**\n"
        "A: No, the market is closed every Sunday for a weekly cleaning schedule.\n"
    )
    result = gate_faq_dedup(body)
    assert result.passed is True


def test_f7_fails_faq_answer_that_restates_the_body():
    body = (
        "The market opens early and closes by noon on most weekdays throughout the season.\n\n"
        "## FAQ\n\n"
        "**Q: When is the market open?**\n"
        "A: The market opens early and closes by noon on most weekdays throughout the season.\n"
    )
    result = gate_faq_dedup(body)
    assert result.passed is False
    assert any("restates a body paragraph" in v for v in result.violations)


# ── F9 social rubric ──

def test_f9_social_unknown_channel_raises():
    try:
        gate_brand_seo_audit_social("some body", "blog", "rubric")
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_f9_social_facebook_uses_3_field_contract_not_blog_5_field():
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response({
        "status": "pass", "brand_fit": 1, "cta_clear": 1, "human_read": 1,
        "failure_codes": [], "notes": "clean",
    })
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit_social("Caption text here.", "facebook", "brand rubric text")

    assert result.gate == "F9_brand_seo_audit_social"
    assert result.passed is True
    assert audit["channel"] == "facebook"
    assert set(audit) >= {"brand_fit", "cta_clear", "human_read"}
    assert "seo_fit" not in audit
    assert "trip_type_accuracy" not in audit

    sent_user = json.loads(fake_client.invoke_model.call_args.kwargs["body"])["messages"][0]["content"][0]["text"]
    assert "cta_clear" in sent_user
    assert "seo_fit" not in sent_user


def test_f9_social_tiktok_uses_2_field_contract():
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response({
        "status": "flagged", "hook_strength": 0, "cta_clear": 1,
        "failure_codes": ["HOOK_WEAK"], "notes": "hook doesn't land",
    })
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit_social("HOOK: meh.\nSCRIPT: ...\nVISUAL: ...",
                                                       "tiktok", "brand rubric text")

    assert result.passed is False
    assert audit["channel"] == "tiktok"
    assert set(audit) >= {"hook_strength", "cta_clear"}
    assert "brand_fit" not in audit
    assert "human_read" not in audit
    assert audit["failure_codes"] == ["HOOK_WEAK"]


def test_f9_social_includes_notes_alongside_failure_codes():
    """AA-396: same notes-preservation fix as the blog gate (gates.py::
    _format_audit_reason) -- notes must survive into violations[0] even when
    failure_codes is non-empty."""
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response({
        "status": "flagged", "hook_strength": 0, "cta_clear": 1,
        "failure_codes": ["HOOK_WEAK"], "notes": "hook restates the destination name, no tension",
    })
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit_social("HOOK: meh.\nSCRIPT: ...\nVISUAL: ...",
                                                       "tiktok", "brand rubric text")

    assert "HOOK_WEAK" in result.violations[0]
    assert "restates the destination name" in result.violations[0]


def test_f9_social_drops_failure_codes_outside_fixed_vocabulary():
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response({
        "status": "flagged", "hook_strength": 0, "cta_clear": 1,
        "failure_codes": ["HOOK_WEAK", "SOME_MADE_UP_CODE"], "notes": "n/a",
    })
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        _, audit = gate_brand_seo_audit_social("body", "tiktok", "rubric")

    assert audit["failure_codes"] == ["HOOK_WEAK"]
    assert all(c in SOCIAL_SEO_FAILURE_CODES for c in audit["failure_codes"])


def test_f9_social_judge_unavailable_fails_not_silent_pass():
    with patch("services.acp_produce.judge_client.boto3.client", side_effect=RuntimeError("boom")):
        result, audit = gate_brand_seo_audit_social("body", "facebook", "rubric")

    assert result.passed is False
    assert audit is None


# ── AA-404 Part 4b: CTA-phrase allowlist for GENERIC_AI_WORDING/SUMMARY_OFF_BRAND ──

# Reconstructed from the real held piece de8337ba...:slot_845eb6ec83cdf1f082ec:
# blog#tiktok, round 2 of 3 (docs/implementation-notes/AA-404.md §3) — a real
# Nova Pro F9-social audit flagged the brand's OWN mandated CTA phrase
# ("Design This Journey", brand_standards.py:12) as GENERIC_AI_WORDING.
_REAL_CTA_FALSE_POSITIVE_NOTES = (
    "The summary contains generic AI wording such as 'An orientation walk through the city on the "
    "first evening draws things into focus.' This does not align with the required brand voice and "
    "language. Additionally, the summary uses phrases like 'Design This Journey' which, while "
    "aligned with the CTA, feels somewhat generic and not sufficiently unique or specific to the "
    "journey described."
)


def test_f9_social_allowlists_cta_phrase_only_generic_wording_complaint():
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response({
        "status": "flagged", "hook_strength": 1, "cta_clear": 1,
        "failure_codes": ["GENERIC_AI_WORDING"], "flagged_phrases": ["Design This Journey"],
        "notes": _REAL_CTA_FALSE_POSITIVE_NOTES,
    })
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit_social("piece body", "tiktok", "brand rubric text")

    assert result.passed is True
    assert audit["failure_codes"] == []


def test_f9_social_does_not_allowlist_when_a_different_phrase_is_also_flagged():
    """The CTA phrase appearing ALONGSIDE a genuinely different off-brand
    phrase is still a real complaint — only a CTA-phrase-ONLY complaint gets
    dropped."""
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response({
        "status": "flagged", "hook_strength": 1, "cta_clear": 1,
        "failure_codes": ["GENERIC_AI_WORDING"],
        "flagged_phrases": ["Design This Journey", "the terrain does its own work"],
        "notes": "mixed complaint",
    })
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit_social("piece body", "tiktok", "brand rubric text")

    assert result.passed is False
    assert audit["failure_codes"] == ["GENERIC_AI_WORDING"]


def test_f9_social_allowlist_drops_only_brand_wording_codes_not_others():
    """A CTA-phrase-only complaint drops GENERIC_AI_WORDING/SUMMARY_OFF_BRAND
    but must never touch an unrelated real failure code like
    CTA_MISSING_OR_WEAK — the gate should still fail on that."""
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response({
        "status": "flagged", "hook_strength": 1, "cta_clear": 0,
        "failure_codes": ["GENERIC_AI_WORDING", "CTA_MISSING_OR_WEAK"],
        "flagged_phrases": ["Design This Journey"],
        "notes": "CTA phrase flagged as generic AND the CTA itself is weak",
    })
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit_social("piece body", "tiktok", "brand rubric text")

    assert result.passed is False
    assert audit["failure_codes"] == ["CTA_MISSING_OR_WEAK"]


def test_f9_social_no_allowlist_when_flagged_phrases_missing():
    """A judge response that doesn't comply with the new `flagged_phrases`
    contract field (empty/absent) must fail closed, same as before this
    change — never silently pass just because evidence wasn't provided."""
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response({
        "status": "flagged", "hook_strength": 1, "cta_clear": 1,
        "failure_codes": ["GENERIC_AI_WORDING"], "flagged_phrases": [],
        "notes": "generic wording, no specific quote given",
    })
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit_social("piece body", "tiktok", "brand rubric text")

    assert result.passed is False
    assert audit["failure_codes"] == ["GENERIC_AI_WORDING"]


# ── AA-404 F9 STEP 0 follow-up: concrete good/bad example anchor (fix #2) +
# blog flagged_phrases evidence requirement (fix #3) ──────────────────────

def test_f9_social_prompt_carries_generic_ai_wording_anchor():
    """Fix #2: both F9 judge calls must see a concrete good/bad example for
    GENERIC_AI_WORDING, not just the bare code name — real week=1 data showed
    the judge flagging specific, well-grounded prose as "generic" with no
    anchor to check against."""
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response({
        "status": "pass", "hook_strength": 1, "cta_clear": 1, "failure_codes": [], "notes": "clean",
    })
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        gate_brand_seo_audit_social("piece body", "tiktok", "brand rubric text")

    sent_user = json.loads(fake_client.invoke_model.call_args.kwargs["body"])["messages"][0]["content"][0]["text"]
    assert "WHAT COUNTS AS GENERIC_AI_WORDING" in sent_user
    assert "royal burial mounds" in sent_user  # the real, confirmed-on-brand GOOD example
    assert "WHAT COUNTS AS SUMMARY_OFF_BRAND" in sent_user


def test_f9_blog_prompt_carries_generic_ai_wording_anchor():
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response({
        "status": "pass", "brand_fit": 1, "human_read": 1, "seo_fit": 1,
        "trip_type_accuracy": 1, "publish_readiness": 1, "failure_codes": [], "notes": "clean",
    })
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        gate_brand_seo_audit("piece text", "brand rubric text")

    sent_user = json.loads(fake_client.invoke_model.call_args.kwargs["body"])["messages"][0]["content"][0]["text"]
    assert "WHAT COUNTS AS GENERIC_AI_WORDING" in sent_user
    assert "royal burial mounds" in sent_user


def test_f9_blog_prompt_now_requires_flagged_phrases_evidence():
    """Fix #3: blog F9 previously had no structured evidence requirement at
    all (only free-text `notes`) -- less accountable than social's, which
    got flagged_phrases in PR #153 Part 4b. Must now match."""
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response({
        "status": "pass", "brand_fit": 1, "human_read": 1, "seo_fit": 1,
        "trip_type_accuracy": 1, "publish_readiness": 1, "failure_codes": [], "notes": "clean",
    })
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        gate_brand_seo_audit("piece text", "brand rubric text")

    sent_user = json.loads(fake_client.invoke_model.call_args.kwargs["body"])["messages"][0]["content"][0]["text"]
    assert "flagged_phrases" in sent_user
    assert "you MUST quote the exact offending phrase" in sent_user


def test_f9_blog_audit_carries_flagged_phrases_through_to_audit_dict():
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response({
        "status": "flagged", "brand_fit": 0, "human_read": 1, "seo_fit": 1,
        "trip_type_accuracy": 1, "publish_readiness": 0,
        "failure_codes": ["GENERIC_AI_WORDING"],
        "flagged_phrases": ["Experience the best of South Korea's rich culture"],
        "notes": "templated superlatives",
    })
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit("piece text", "brand rubric text")

    assert result.passed is False
    assert audit["flagged_phrases"] == ["Experience the best of South Korea's rich culture"]


def test_f9_blog_audit_flagged_phrases_defaults_to_empty_list_when_absent():
    """A judge response that omits `flagged_phrases` entirely (older
    behavior, or a model that ignores the new instruction) must not crash --
    degrades to an empty list, same fail-closed convention as social's own
    AA-404 Part 4b guard."""
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response({
        "status": "pass", "failure_codes": [],
    })
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit("piece text", "brand rubric text")

    assert audit["flagged_phrases"] == []
