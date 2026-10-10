"""
tests/unit/test_aa298_judge.py — the cross-weight judge backend
(services/acp_produce/judge_client.py, AA-298 Nhóm 3).

AA-753 (10/10/2026) trimmed this file: the F8/F9 gate_framework()/
gate_brand_seo_audit() tests moved out with the dead N7 gate stack
(services/acp_produce/gates.py was removed — 0 live callers; the live T10
gate stack is services/acp_content_writing/quality_gates.py). What remains
here is the part that covers LIVE code: judge_client.py, still used by
t10_judge via quality_gates.py.

Covers the two things ADR-2026-014/ADR-2026-027/L3 require of the judge
backend itself, verified by reading the real Bedrock call (not a docstring):
  1. invoke_judge()'s Nova Pro backend calls us.amazon.nova-pro-v1:0, a model
     distinct from the writer's model.
  2. judge_client.py never imports the writer/generation modules (context
     isolation enforced structurally, not just promised).
"""
from unittest.mock import MagicMock, patch

import pytest

from services.acp_produce.judge_client import NOVA_PRO_MODEL_ID, invoke_judge


@pytest.fixture(autouse=True)
def _pin_judge_model_to_nova_pro(monkeypatch):
    """These tests exercise invoke_judge()'s Nova Pro backend specifically.
    AA-518 (02/09/2026) changed invoke_judge()'s default "nova_pro" -> "gpt41"
    — pin it back here so the Nova Pro path/mock shape stays exercised,
    independent of that production default."""
    monkeypatch.setenv("JUDGE_MODEL", "nova_pro")


def _bedrock_response(text: str):
    import json
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
    profile the judge is checked against here."""
    from shared.llm_client.bedrock_satellite import INFERENCE_PROFILE_SONNET
    assert NOVA_PRO_MODEL_ID != INFERENCE_PROFILE_SONNET


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
