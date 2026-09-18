"""AA-617 (epic AA-616) — per-account LLM cost instrumentation.

record_call*() now forward account/fallback_used/provider into shared.llm_call_log (migration
153), and normalize the model string (strip the "satellite-" prefix, derive provider when the
caller doesn't pass one). No live DB — asyncpg mocked.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.llm_client.call_log import _normalize_model_provider


def test_normalize_strips_satellite_prefix_and_derives_provider():
    assert _normalize_model_provider("satellite-haiku-4-5", None) == ("haiku-4-5", "bedrock-satellite")
    assert _normalize_model_provider("satellite-sonnet-4-6", None) == ("sonnet-4-6", "bedrock-satellite")


def test_normalize_derives_openai_and_native():
    assert _normalize_model_provider("gpt-4.1", None) == ("gpt-4.1", "openai")
    # A clean id with no prefix + no explicit provider defaults to native (acc2 raw profile id).
    assert _normalize_model_provider("sonnet-4-6", None) == ("sonnet-4-6", "bedrock-native")


def test_normalize_explicit_provider_wins():
    # Mechanism-B call sites pass provider="bedrock-satellite" explicitly even though the model
    # string has no prefix — the explicit value must not be overwritten by the heuristic.
    assert _normalize_model_provider("sonnet-4-6", "bedrock-satellite") == ("sonnet-4-6", "bedrock-satellite")


@pytest.mark.asyncio
async def test_record_call_forwards_account_fallback_provider():
    from shared.llm_client import call_log

    fake_conn = AsyncMock()
    with patch("shared.llm_client.call_log.asyncpg.connect", AsyncMock(return_value=fake_conn)), \
         patch("shared.llm_client.call_log.get_database_url", return_value="postgres://fake"):
        await call_log.record_call(
            stage="s1_generate", role="writer", model="satellite-haiku-4-5",
            tokens_in=100, tokens_out=50, cost_usd=0.01, quality_signal={"ok": True},
            stop_reason="end_turn", account="acc3", fallback_used=False,
        )
    args = fake_conn.execute.call_args.args
    # bind order tail: …, stop_reason, account, fallback_used, provider
    assert args[-4] == "end_turn"
    assert args[-3] == "acc3"
    assert args[-2] is False
    assert args[-1] == "bedrock-satellite"   # derived from the "satellite-" prefix
    # bind order: args[0]=SQL, then tenant_id, stage, role, model → model is args[4], stored
    # WITHOUT the "satellite-" prefix.
    assert args[4] == "haiku-4-5"


@pytest.mark.asyncio
async def test_record_call_openai_account_none_provider_openai():
    from shared.llm_client import call_log

    fake_conn = AsyncMock()
    with patch("shared.llm_client.call_log.asyncpg.connect", AsyncMock(return_value=fake_conn)), \
         patch("shared.llm_client.call_log.get_database_url", return_value="postgres://fake"):
        await call_log.record_call(
            stage="s1_judge", role="judge", model="gpt-4.1",
            tokens_in=100, tokens_out=50, cost_usd=0.02, quality_signal={"passed": True},
            stop_reason="stop",
        )
    args = fake_conn.execute.call_args.args
    assert args[-3] is None            # account: OpenAI has none
    assert args[-1] == "openai"        # provider derived from "gpt" prefix
    assert args[4] == "gpt-4.1"        # model is args[4] (args[0] is the SQL string)
