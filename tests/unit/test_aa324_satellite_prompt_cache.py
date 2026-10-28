"""AA-324 — prompt caching was truly dead on every Bedrock satellite call (T1.5/T2.5,
the path essentially all real Sonnet/Haiku calls fall through to today, acc2-native being
blocked for channel-program accounts, AA-291/AA-329). STEP0 confirmed the real cause is NOT
the issue's original title hypothesis ("cache_control only works on acc2 direct" -- a Bedrock
cross-account platform limitation) -- it's 2 concrete, local code gaps:

  1. invoke_claude() (shared/llm_client/bedrock_satellite.py) sent `system` as a PLAIN STRING
     in the request body -- Bedrock's Anthropic-compatible InvokeModel API silently never
     caches a plain-string system field, no error, matching the "code looks right, cache
     stays 0" symptom exactly. Fixed: wraps `system` via the SAME build_cached_system_prompt()
     helper _call_bedrock() (acc2-native T1 path) already uses.
  2. _call_bedrock_satellite() (shared/llm_client/client.py) never read
     cache_read_input_tokens/cache_creation_input_tokens out of the satellite response's
     usage dict at all -- cache_read_tokens/cache_write_tokens on LLMResponse always defaulted
     to 0 for every satellite call, independently of whether Bedrock actually cached anything.

Covers:
  1. test_invoke_claude_wraps_system_as_cached_content_block — the real request body sent to
     bedrock_rt.invoke_model() carries a cache_control-bearing system block, not a bare string
  2. test_invoke_claude_no_system_omits_system_field — unchanged behavior when system=None
  3. test_call_bedrock_satellite_extracts_cache_stats_from_usage — LLMResponse.cache_read_tokens/
     cache_write_tokens now reflect the real usage dict instead of always 0
  4. test_call_bedrock_satellite_zero_cache_stats_when_absent — a response with no cache
     fields in usage (e.g. a model that genuinely doesn't support caching) stays a real 0,
     not an error
"""
import json
from unittest.mock import MagicMock, patch

import pytest


class TestAA324InvokeClaudeCacheWiring:
    def test_invoke_claude_wraps_system_as_cached_content_block(self):
        from shared.llm_client import bedrock_satellite as bs

        fake_response_body = json.dumps({
            "content": [{"text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "stop_reason": "end_turn",
        }).encode()
        mock_bedrock_rt = MagicMock()
        mock_bedrock_rt.invoke_model.return_value = {
            "body": MagicMock(read=MagicMock(return_value=fake_response_body))
        }
        mock_session = MagicMock()
        mock_session.client.return_value = mock_bedrock_rt

        with patch.object(bs, "_get_satellite_session", return_value=mock_session):
            bs.invoke_claude(
                "user prompt here", model="sonnet",
                system="brand rules + schema instructions (the cacheable prefix)",
                account="acc3",
            )

        call_kwargs = mock_bedrock_rt.invoke_model.call_args.kwargs
        sent_body = json.loads(call_kwargs["body"])
        assert isinstance(sent_body["system"], list)
        assert sent_body["system"][0]["type"] == "text"
        assert sent_body["system"][0]["text"] == "brand rules + schema instructions (the cacheable prefix)"
        assert sent_body["system"][0]["cache_control"] == {"type": "ephemeral"}

    def test_invoke_claude_no_system_omits_system_field(self):
        from shared.llm_client import bedrock_satellite as bs

        fake_response_body = json.dumps({
            "content": [{"text": "ok"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "stop_reason": "end_turn",
        }).encode()
        mock_bedrock_rt = MagicMock()
        mock_bedrock_rt.invoke_model.return_value = {
            "body": MagicMock(read=MagicMock(return_value=fake_response_body))
        }
        mock_session = MagicMock()
        mock_session.client.return_value = mock_bedrock_rt

        with patch.object(bs, "_get_satellite_session", return_value=mock_session):
            bs.invoke_claude("user prompt here", model="haiku", system=None, account="acc3")

        call_kwargs = mock_bedrock_rt.invoke_model.call_args.kwargs
        sent_body = json.loads(call_kwargs["body"])
        assert "system" not in sent_body


@pytest.mark.asyncio
class TestAA324SatelliteCacheStatsExtraction:
    async def test_call_bedrock_satellite_extracts_cache_stats_from_usage(self):
        from shared.llm_client.client import LLMClient
        from shared.llm_client.models import LLMRequest
        from shared.llm_client.pricing import BEDROCK_SONNET
        from shared.llm_client import bedrock_satellite as bs

        fake_result = bs.BedrockInvokeResult(
            text="generated content", model_used="sonnet-4-6", latency_ms=500.0,
            usage={
                "input_tokens": 200, "output_tokens": 50,
                "cache_read_input_tokens": 7000, "cache_creation_input_tokens": 0,
            },
            stop_reason="end_turn",
        )
        client = LLMClient()
        request = LLMRequest(user_prompt="write a tour description", system_prompt="brand rules")

        with patch("shared.llm_client.bedrock_satellite.invoke_claude", return_value=fake_result):
            resp = client._call_bedrock_satellite(request, model=BEDROCK_SONNET, account="acc3")

        assert resp.cache_read_tokens == 7000
        assert resp.cache_write_tokens == 0

    async def test_call_bedrock_satellite_zero_cache_stats_when_absent(self):
        from shared.llm_client.client import LLMClient
        from shared.llm_client.models import LLMRequest
        from shared.llm_client.pricing import BEDROCK_HAIKU
        from shared.llm_client import bedrock_satellite as bs

        fake_result = bs.BedrockInvokeResult(
            text="generated content", model_used="haiku-4-5", latency_ms=300.0,
            usage={"input_tokens": 100, "output_tokens": 20},  # no cache_* keys at all
            stop_reason="end_turn",
        )
        client = LLMClient()
        request = LLMRequest(user_prompt="write a tour description", system_prompt="brand rules")

        with patch("shared.llm_client.bedrock_satellite.invoke_claude", return_value=fake_result):
            resp = client._call_bedrock_satellite(request, model=BEDROCK_HAIKU, account="acc3")

        assert resp.cache_read_tokens == 0
        assert resp.cache_write_tokens == 0
