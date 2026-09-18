"""AA-493 — stop_reason/finish_reason no longer silently discarded.

Covers the pieces test_aa224_streaming_fallback.py (native Bedrock streaming) and
test_aa351_judge_gpt41.py (judge_client.py's 3 backends) don't already reach directly:
  1. bedrock_satellite.py::invoke_claude() — non-streaming invoke_model(), top-level
     `stop_reason` on the response payload (different shape from client.py's streaming parser).
  2. LLMClient._call_bedrock_satellite() — propagates BedrockInvokeResult.stop_reason into
     LLMResponse.stop_reason.
  3. shared/llm_client/call_log.py — record_call()/record_call_with_pool() forward stop_reason
     as the 11th positional bind param to the INSERT (shared.llm_call_log, migration 141).

No live DB, no live AWS — all boto3/asyncpg touch points mocked.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.llm_client.bedrock_satellite import invoke_claude
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest


def _fake_invoke_model_response(stop_reason="end_turn", text="Hello."):
    payload = {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 50, "output_tokens": 12},
        "stop_reason": stop_reason,
    }
    body = MagicMock()
    body.read.return_value = json.dumps(payload).encode()
    return {"body": body}


def _patched_satellite_session(bedrock_rt):
    session = MagicMock()
    session.client.return_value = bedrock_rt
    return patch("shared.llm_client.bedrock_satellite._get_satellite_session", return_value=session)


def test_invoke_claude_captures_stop_reason_end_turn():
    bedrock_rt = MagicMock()
    bedrock_rt.invoke_model.return_value = _fake_invoke_model_response("end_turn")
    with _patched_satellite_session(bedrock_rt):
        result = invoke_claude("prompt", model="sonnet", account="acc3")
    assert result.stop_reason == "end_turn"


def test_invoke_claude_captures_stop_reason_max_tokens():
    """The real motivating case: a satellite-writer response cut off at max_tokens must be
    distinguishable from a normal completion."""
    bedrock_rt = MagicMock()
    bedrock_rt.invoke_model.return_value = _fake_invoke_model_response("max_tokens", text="cut off mid")
    with _patched_satellite_session(bedrock_rt):
        result = invoke_claude("prompt", model="sonnet", account="acc3")
    assert result.stop_reason == "max_tokens"


def test_call_bedrock_satellite_propagates_stop_reason_to_llmresponse():
    with patch("shared.llm_client.client.boto3.client"), \
         patch("shared.llm_client.client.openai.OpenAI"):
        client = LLMClient()
    bedrock_rt = MagicMock()
    bedrock_rt.invoke_model.return_value = _fake_invoke_model_response("max_tokens")
    with _patched_satellite_session(bedrock_rt):
        resp = client._call_bedrock_satellite(
            LLMRequest(system_prompt="sys", user_prompt="usr", model_tier="sonnet"),
            model="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            account="acc3",
        )
    assert resp.stop_reason == "max_tokens"


@pytest.mark.asyncio
async def test_record_call_forwards_stop_reason_to_insert():
    from shared.llm_client import call_log

    fake_conn = AsyncMock()
    with patch("shared.llm_client.call_log.asyncpg.connect", AsyncMock(return_value=fake_conn)), \
         patch("shared.llm_client.call_log.get_database_url", return_value="postgres://fake"):
        await call_log.record_call(
            stage="t9_write", role="writer", model="sonnet-4-6",
            tokens_in=100, tokens_out=50, cost_usd=0.01,
            quality_signal={"ok": True}, stop_reason="max_tokens",
        )
    args = fake_conn.execute.call_args.args
    # AA-617 appended account/fallback_used/provider AFTER stop_reason, so stop_reason is now
    # the 4th-from-last bind param (…, stop_reason, account, fallback_used, provider).
    assert args[-4] == "max_tokens"


@pytest.mark.asyncio
async def test_record_call_with_pool_forwards_stop_reason_to_insert():
    from shared.llm_client import call_log

    fake_conn = AsyncMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=fake_conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)

    await call_log.record_call_with_pool(
        pool, stage="t5_atomize", role="writer", model="sonnet-4-6",
        tokens_in=100, tokens_out=50, cost_usd=0.01,
        quality_signal={"ok": True}, stop_reason="end_turn",
    )
    args = fake_conn.execute.call_args.args
    # AA-617: stop_reason is 4th-from-last now (…, stop_reason, account, fallback_used, provider).
    assert args[-4] == "end_turn"


@pytest.mark.asyncio
async def test_record_call_stop_reason_defaults_to_none_for_untouched_callers():
    """Backward compatibility: a caller that doesn't pass stop_reason at all still inserts
    cleanly (NULL column), matching migration 141's nullable ALTER TABLE."""
    from shared.llm_client import call_log

    fake_conn = AsyncMock()
    with patch("shared.llm_client.call_log.asyncpg.connect", AsyncMock(return_value=fake_conn)), \
         patch("shared.llm_client.call_log.get_database_url", return_value="postgres://fake"):
        await call_log.record_call(
            stage="t9_write", role="writer", model="sonnet-4-6",
            tokens_in=100, tokens_out=50, cost_usd=0.01, quality_signal={"ok": True},
        )
    args = fake_conn.execute.call_args.args
    # AA-617: bind tail is (…, stop_reason, account, fallback_used, provider). An untouched caller
    # leaves stop_reason/account/fallback_used NULL; provider is still DERIVED from the model
    # string ("sonnet-4-6" with no prefix + no explicit provider -> "bedrock-native").
    assert args[-4] is None            # stop_reason
    assert args[-3] is None            # account
    assert args[-2] is None            # fallback_used
    assert args[-1] == "bedrock-native"  # provider derived from clean model id
