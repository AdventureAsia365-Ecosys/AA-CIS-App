"""
tests/unit/test_aa298_judge.py — F8 cross-weight judge (services/acp_produce/
judge_client.py + gates.py::gate_framework(), AA-298 Nhóm 3).

Covers the 3 things ADR-2026-014/ADR-2026-027/L3 require and the AA-298
verify checklist names explicitly:
  1. F8 calls Nova Pro (us.amazon.nova-pro-v1:0), not the writer's model.
  2. The payload sent to the judge contains NO trace of the writer's
     generation system/user prompt (context isolation, verified by reading
     the actual payload — not by trusting a docstring).
  3. Binary 1/0 scoring with mandatory evidence — no 1-10 scale, no silent
     pass on missing/malformed judge output.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from services.acp_produce.gates import gate_brand_seo_audit, gate_framework
from services.acp_produce.judge_client import NOVA_PRO_MODEL_ID, invoke_judge


@pytest.fixture(autouse=True)
def _pin_judge_model_to_nova_pro(monkeypatch):
    """This whole file exercises gate_framework()/gate_brand_seo_audit()'s
    parsing/scoring logic via Nova Pro's Converse-shaped mock responses, none
    of it about which backend is the production default. AA-518 (02/09/2026)
    changed invoke_judge()'s default "nova_pro" -> "gpt41" -- pin it back here
    so every test in this file keeps exercising the same Nova Pro path/mock
    shape it always has, independent of that production default."""
    monkeypatch.setenv("JUDGE_MODEL", "nova_pro")


def _bedrock_response(text: str):
    payload = {
        "output": {"message": {"content": [{"text": text}], "role": "assistant"}},
        "stopReason": "end_turn",
        "usage": {"inputTokens": 20, "outputTokens": 10, "totalTokens": 30},
    }
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode()
    return {"body": body}


def test_invoke_judge_calls_nova_pro_model_id():
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response('{"ok": true}')
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client) as mock_boto:
        result = invoke_judge("system", "user")

    mock_boto.assert_called_once_with("bedrock-runtime", region_name="us-west-1")
    call_kwargs = fake_client.invoke_model.call_args.kwargs
    assert call_kwargs["modelId"] == NOVA_PRO_MODEL_ID == "us.amazon.nova-pro-v1:0"
    assert result["provider"] == "bedrock-acc2"


def test_invoke_judge_never_the_writer_model_id():
    """Direct assertion the checklist asks for: judge model != writer model.

    AA-392 (09/08/2026): S1-from-atom's writer moved off Palmyra X5
    (us.writer.palmyra-x5-v1:0, permanently rejected — AA-337's 1 req/min
    channel throttle) onto the same Bedrock satellite Sonnet inference
    profile N7's own writer uses. Checked against that real profile now,
    not the removed Palmyra constant."""
    from shared.llm_client.bedrock_satellite import INFERENCE_PROFILE_SONNET
    assert NOVA_PRO_MODEL_ID != INFERENCE_PROFILE_SONNET


def test_gate_framework_context_isolation_no_generation_prompt_in_judge_payload():
    """L3: judge must never see a writer's generation system/user prompt.
    Verified here by reading the EXACT payload sent to Nova and asserting a
    distinctive writer-prompt phrase is absent from it. (AA-611 removed the
    s1-from-atom writer module, so this checks the unique marker phrase directly
    rather than importing the deleted module's prompt constant.)"""
    fake_client = MagicMock()
    good_items = {"items": [{"criterion": c, "score": "1", "evidence": "quote"}
                             for c in ["covers the topic comprehensively via subsections",
                                       "each section answers a distinct sub-question"]]}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(good_items))

    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        gate_framework("Some piece body about a trip to Sri Lanka.", "hub")

    sent_body = json.loads(fake_client.invoke_model.call_args.kwargs["body"])
    sent_system = sent_body["system"][0]["text"]
    sent_user = sent_body["messages"][0]["content"][0]["text"]

    # A distinctive writer-prompt phrase must not leak into the judge call.
    assert "CLOSED WORLD RULE" not in sent_system  # a distinctive phrase unique to a writer prompt
    assert "CLOSED WORLD RULE" not in sent_user


def test_judge_client_module_never_imports_generation_or_writer_modules():
    """Structural check, not just a docstring promise: judge_client.py's own
    IMPORT STATEMENTS (not docstrings/comments, which legitimately reference
    the writer modules to explain the isolation) must not reference the
    writer's modules."""
    import ast
    import inspect

    from services.acp_produce import judge_client
    tree = ast.parse(inspect.getsource(judge_client))
    imported_names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.append(node.module)

    assert not any("content_generation" in name for name in imported_names)
    assert not any("acp_produce.generation" in name for name in imported_names)


def test_gate_framework_passes_when_all_criteria_scored_1_with_evidence():
    fake_client = MagicMock()
    data = {"items": [
        {"criterion": "opens with the reader's problem", "score": "1", "evidence": "Your bags are packed..."},
        {"criterion": "agitates concretely", "score": "1", "evidence": "the layover drags on..."},
        {"criterion": "resolves with the trip as solve", "score": "1", "evidence": "This trip fixes that."},
    ]}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result = gate_framework("piece text", "PAS")
    assert result.passed is True
    assert result.violations == []


def test_gate_framework_fails_on_score_0():
    fake_client = MagicMock()
    data = {"items": [{"criterion": "single clear action (CTA)", "score": "0", "evidence": ""}]}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result = gate_framework("piece text", "AIDA")
    assert result.passed is False
    assert any("single clear action" in v for v in result.violations)


def test_gate_framework_treats_score_1_without_evidence_as_fail():
    """Mandatory evidence citation — a 1 with no quote does not count.
    AA-396 follow-up: "ends with CTA" is no longer sent to the LLM for
    hook_story_cta (see below), so this uses "first line is the hook" — a
    criterion still on the LLM path — and gives the piece a real trailing
    CTA phrase so the new deterministic sub-check doesn't also fail it."""
    fake_client = MagicMock()
    data = {"items": [{"criterion": "first line is the hook", "score": "1", "evidence": ""}]}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result = gate_framework("Hook line.\n\nDesign This Journey.", "hook_story_cta")
    assert result.passed is False
    assert any("no evidence" in v for v in result.violations)


# ── AA-396 follow-up: hook_story_cta "ends with CTA" (DET, pulled off the LLM) ──

def test_gate_framework_hook_story_cta_rubric_no_longer_asks_llm_about_cta():
    """The Nova Pro prompt for hook_story_cta must carry ONLY the 2 remaining
    genuinely-semantic criteria — "ends with CTA" moved to _ends_with_cta()."""
    fake_client = MagicMock()
    data = {"items": [
        {"criterion": "first line is the hook", "score": "1", "evidence": "Hook line."},
        {"criterion": "one atom, one emotion", "score": "1", "evidence": "Hook line."},
    ]}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client) as mock_boto:
        gate_framework("Hook line.\n\nDesign This Journey.", "hook_story_cta")
    sent_body = json.loads(mock_boto.return_value.invoke_model.call_args.kwargs["body"])
    prompt = sent_body["messages"][0]["content"][0]["text"]
    assert "first line is the hook" in prompt
    assert "one atom, one emotion" in prompt
    assert "ends with CTA" not in prompt


def test_gate_framework_ends_with_cta_det_pass_markdown_link_literally_last():
    """Real AA-396 bug case (slot_c5471's blog piece): body's literal last
    content is a markdown CTA link, no trailing prose after it. Previously
    scored 0 by the LLM judge; must now pass via the deterministic check."""
    fake_client = MagicMock()
    data = {"items": [
        {"criterion": "first line is the hook", "score": "1", "evidence": "quote"},
        {"criterion": "one atom, one emotion", "score": "1", "evidence": "quote"},
    ]}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    body = (
        "Hook line about South Korea.\n\n"
        "Some body prose about the trip.\n\n"
        "[Design This Journey](https://aa-cis.lumiguides.it.com/)"
    )
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result = gate_framework(body, "hook_story_cta")
    assert result.passed is True


def test_gate_framework_ends_with_cta_det_pass_link_followed_by_coda_sentence():
    """Real AA-396 corpus (slot_4139's blog piece): CTA link is followed by
    a short coda sentence in the SAME final paragraph, not the literal last
    characters. This piece already passed under the old LLM judge; the
    deterministic check must not regress it."""
    fake_client = MagicMock()
    data = {"items": [
        {"criterion": "first line is the hook", "score": "1", "evidence": "quote"},
        {"criterion": "one atom, one emotion", "score": "1", "evidence": "quote"},
    ]}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    body = (
        "Hook line about South Korea.\n\n"
        "Some body prose about the trip.\n\n"
        "[Design This Journey](https://aa-cis.lumiguides.it.com/) with the "
        "understanding that South Korea will ask something of you in return."
    )
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result = gate_framework(body, "hook_story_cta")
    assert result.passed is True


def test_gate_framework_ends_with_cta_det_fail_no_cta_present():
    """Real AA-396 corpus (facebook pieces, slot_c5471/slot_4139): body ends
    on hashtags with no CTA phrase anywhere — a genuine failure the
    deterministic check must still catch."""
    fake_client = MagicMock()
    data = {"items": [
        {"criterion": "first line is the hook", "score": "1", "evidence": "quote"},
        {"criterion": "one atom, one emotion", "score": "1", "evidence": "quote"},
    ]}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    body = (
        "A journey through South Korea begins not with sightseeing, but with discipline.\n\n"
        "HASHTAGS: #AdventureAsia #SouthKorea #Taekwondo"
    )
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result = gate_framework(body, "hook_story_cta")
    assert result.passed is False
    assert any("ends with CTA" in v for v in result.violations)


def test_gate_framework_ends_with_cta_det_runs_even_when_llm_criteria_pass_all():
    """The deterministic violation must surface even when every LLM-judged
    criterion scores 1 -- it is combined with, not shadowed by, the judge
    result."""
    fake_client = MagicMock()
    data = {"items": [
        {"criterion": "first line is the hook", "score": "1", "evidence": "quote"},
        {"criterion": "one atom, one emotion", "score": "1", "evidence": "quote"},
    ]}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result = gate_framework("Hook line.\n\nNo call to action here at all.", "hook_story_cta")
    assert result.passed is False
    assert result.violations == ["framework criterion failed: ends with CTA"]


def test_gate_framework_ends_with_cta_det_not_applied_to_other_frameworks():
    """AIDA's "single clear action (CTA)" and reader_as_hero's "single CTA"
    stay purely LLM-judged -- the deterministic check is scoped to
    hook_story_cta only and must not fire for other frameworks."""
    fake_client = MagicMock()
    data = {"items": [{"criterion": "single clear action (CTA)", "score": "1", "evidence": "quote"}]}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result = gate_framework("No CTA phrase anywhere in this piece.", "AIDA")
    assert result.passed is True


def test_gate_framework_treats_empty_judge_output_as_fail_not_silent_pass():
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps({"items": []}))
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result = gate_framework("piece text", "hub")
    assert result.passed is False
    assert any("no rubric items" in v for v in result.violations)


def test_gate_framework_unknown_framework_falls_back_to_default_rubric():
    fake_client = MagicMock()
    data = {"items": [{"criterion": "structure matches the stated framework", "score": "1", "evidence": "quote"}]}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client) as mock_boto:
        result = gate_framework("piece text", "some_unknown_framework")
    assert result.passed is True
    sent_body = json.loads(mock_boto.return_value.invoke_model.call_args.kwargs["body"])
    assert "structure matches the stated framework" in sent_body["messages"][0]["content"][0]["text"]


# ── F9 brand_seo_audit ────────────────────────────────────────────────────

def test_gate_brand_seo_audit_passes_on_status_pass():
    fake_client = MagicMock()
    data = {"status": "pass", "brand_fit": 1, "human_read": 1, "seo_fit": 1,
            "trip_type_accuracy": 1, "publish_readiness": 1, "failure_codes": [], "notes": "clean"}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit("piece text", "brand rubric text")
    assert result.passed is True
    assert audit["status"] == "pass"


def test_gate_brand_seo_audit_fails_with_failure_codes_on_flagged():
    fake_client = MagicMock()
    data = {"status": "flagged", "brand_fit": 0, "human_read": 1, "seo_fit": 1,
            "trip_type_accuracy": 1, "publish_readiness": 0,
            "failure_codes": ["SUMMARY_OFF_BRAND", "GENERIC_AI_WORDING"], "notes": "reads like AI filler"}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit("piece text", "brand rubric text")
    assert result.passed is False
    assert "SUMMARY_OFF_BRAND" in result.violations[0]
    assert audit["failure_codes"] == ["SUMMARY_OFF_BRAND", "GENERIC_AI_WORDING"]


def test_gate_brand_seo_audit_drops_failure_codes_outside_fixed_vocabulary():
    """The judge must not be able to invent its own label — anything outside
    BRAND_SEO_FAILURE_CODES is silently dropped from the tracked set (but
    status=flagged/manual_check still fails the gate)."""
    fake_client = MagicMock()
    data = {"status": "flagged", "failure_codes": ["SUMMARY_OFF_BRAND", "MADE_UP_CODE_XYZ"]}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit("piece text", "brand rubric text")
    assert audit["failure_codes"] == ["SUMMARY_OFF_BRAND"]
    assert result.passed is False


def test_gate_brand_seo_audit_context_isolation():
    # AA-611: the s1-from-atom writer module was removed; assert a distinctive writer-prompt
    # phrase never leaks into the judge payload instead of importing the deleted constant.
    fake_client = MagicMock()
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps({"status": "pass", "failure_codes": []}))
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        gate_brand_seo_audit("piece text", "brand rubric text")

    sent_body = json.loads(fake_client.invoke_model.call_args.kwargs["body"])
    assert "CLOSED WORLD RULE" not in json.dumps(sent_body)


def test_gate_brand_seo_audit_includes_notes_alongside_failure_codes():
    """AA-396: `notes` must not be dropped when `failure_codes` is non-empty --
    it was the only channel that could ever carry the judge's actual
    explanation through to repair_fn (repair.py), and the old
    `", ".join(failure_codes) or audit.get("notes")` pattern discarded it
    every time failure_codes was non-empty (i.e. almost always)."""
    fake_client = MagicMock()
    data = {"status": "flagged", "brand_fit": 0, "human_read": 1, "seo_fit": 1,
            "trip_type_accuracy": 1, "publish_readiness": 0,
            "failure_codes": ["SUMMARY_OFF_BRAND"],
            "notes": "opens with a generic AI-sounding preamble before the first real fact"}
    fake_client.invoke_model.return_value = _bedrock_response(json.dumps(data))
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit("piece text", "brand rubric text")
    assert "SUMMARY_OFF_BRAND" in result.violations[0]
    assert "generic AI-sounding preamble" in result.violations[0]


def test_gate_brand_seo_audit_judge_unavailable_returns_none_audit_not_fabricated():
    fake_client = MagicMock()
    fake_client.invoke_model.side_effect = RuntimeError("Bedrock throttled")
    with patch("services.acp_produce.judge_client.boto3.client", return_value=fake_client):
        result, audit = gate_brand_seo_audit("piece text", "brand rubric text")
    assert result.passed is False
    assert audit is None  # no fabricated audit dict when the judge call itself failed
