"""AA-635 — llm_call_log under-reported Bedrock spend vs the AWS bill.

Covers the three code-side causes: Haiku 4.5 priced at Claude 3 Haiku rates, prompt-cache tokens
never priced, and the A3 Search Demand ReAct loop never writing llm_call_log. Pure/mocked only.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.llm_client.models import LLMResponse
from shared.llm_client.pricing import BEDROCK_HAIKU, BEDROCK_SONNET, calc_cost


# ── F1: Haiku 4.5 rates ────────────────────────────────────────────────────────────────────────

def test_haiku_4_5_priced_at_1_and_5_per_mtok():
    assert calc_cost(BEDROCK_HAIKU, 1_000_000, 0) == pytest.approx(1.0)
    assert calc_cost(BEDROCK_HAIKU, 0, 1_000_000) == pytest.approx(5.0)


def test_sonnet_rates_unchanged():
    assert calc_cost(BEDROCK_SONNET, 1_000_000, 1_000_000) == pytest.approx(18.0)


@pytest.mark.parametrize("label", ["haiku", "haiku-4-5", "satellite-haiku-4-5"])
def test_haiku_labels_resolve_to_haiku_rates(label):
    assert calc_cost(label, 1000, 1000) == calc_cost(BEDROCK_HAIKU, 1000, 1000)


@pytest.mark.parametrize("label", ["sonnet", "sonnet-4-6", "satellite-sonnet-4-6"])
def test_sonnet_labels_resolve_to_sonnet_rates(label):
    assert calc_cost(label, 1000, 1000) == calc_cost(BEDROCK_SONNET, 1000, 1000)


def test_unknown_model_still_falls_back_to_sonnet_tier():
    assert calc_cost("some-new-model", 1000, 1000) == calc_cost(BEDROCK_SONNET, 1000, 1000)


# ── F2: prompt-cache tokens ────────────────────────────────────────────────────────────────────

def test_cache_write_priced_at_1_25x_input():
    # 1M cache-write Haiku tokens = 1.25 * $1
    assert calc_cost(BEDROCK_HAIKU, 0, 0, cache_write=1_000_000) == pytest.approx(1.25)


def test_cache_read_priced_at_0_1x_input():
    # 1M cache-read Sonnet tokens = 0.1 * $3
    assert calc_cost(BEDROCK_SONNET, 0, 0, cache_read=1_000_000) == pytest.approx(0.3)


def test_cache_defaults_keep_old_signature_result():
    assert calc_cost(BEDROCK_HAIKU, 1234, 567) == calc_cost(BEDROCK_HAIKU, 1234, 567, 0, 0)


def test_cache_none_values_treated_as_zero():
    assert calc_cost(BEDROCK_HAIKU, 100, 100, cache_read=None, cache_write=None) == \
        calc_cost(BEDROCK_HAIKU, 100, 100)


def test_llm_client_calc_cost_includes_cache_tokens():
    from shared.llm_client.client import LLMClient
    client = LLMClient.__new__(LLMClient)  # skip boto3/openai construction
    got = client._calc_cost(BEDROCK_HAIKU, 1000, 1000, cache_read=2000, cache_write=3000)
    assert got == calc_cost(BEDROCK_HAIKU, 1000, 1000, cache_read=2000, cache_write=3000)
    assert got > calc_cost(BEDROCK_HAIKU, 1000, 1000)


def test_satellite_call_cost_includes_cache_tokens():
    from shared.llm_client import client as client_mod

    result = MagicMock()  # stands in for bedrock_satellite.BedrockInvokeResult
    result.text = "ok"
    result.model_used = "haiku-4-5"
    result.latency_ms = 1
    result.stop_reason = "end_turn"
    result.usage = {"input_tokens": 1000, "output_tokens": 500,
                    "cache_read_input_tokens": 4000, "cache_creation_input_tokens": 2000}
    llm = client_mod.LLMClient.__new__(client_mod.LLMClient)
    req = MagicMock(user_prompt="u", system_prompt="s", max_tokens=100)
    with patch("shared.llm_client.bedrock_satellite.invoke_claude", return_value=result):
        resp = llm._call_bedrock_satellite(req, model=BEDROCK_HAIKU, account="acc3")
    assert resp.cost_usd == calc_cost(BEDROCK_HAIKU, 1000, 500, cache_read=4000, cache_write=2000)


# ── F3: A3 Search Demand loop writes llm_call_log ──────────────────────────────────────────────

def _pool_with_conn():
    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool


def _resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, model_used="satellite-haiku-4-5", provider="bedrock-satellite",
                       input_tokens=800, output_tokens=60, cost_usd=0.0011,
                       satellite_account="acc3", stop_reason="end_turn")


@pytest.mark.asyncio
async def test_search_demand_logs_every_turn():
    import asyncio
    from services.acp_contract import segment_research as sr

    fake_llm = MagicMock()
    fake_llm.generate.side_effect = [
        _resp('{"thought": "t", "tool": "done", "keywords": []}'),
    ]
    with patch.object(sr, "LLMClient", return_value=fake_llm), \
         patch.object(sr, "record_call_with_pool", new=AsyncMock()) as m_log:
        result = await sr._research_place(
            "kyoto", ["explore"], ["US"], [(2840, "United States", "en")], {}, MagicMock(),
            _pool_with_conn(), asyncio.Semaphore(1),
        )

    assert result.llm_calls == 1
    m_log.assert_awaited_once()
    kwargs = m_log.await_args.kwargs
    assert kwargs["stage"] == "a3_search_demand"
    assert kwargs["role"] in ("writer", "judge", "validate")  # llm_call_log.role CHECK
    assert kwargs["account"] == "acc3"
    assert kwargs["cost_usd"] == pytest.approx(0.0011)
    assert kwargs["tokens_in"] == 800 and kwargs["tokens_out"] == 60
    assert kwargs["quality_signal"] == {"step": 1, "tool": "done", "parsed": True}


@pytest.mark.asyncio
async def test_search_demand_logs_unparseable_turn_then_stops():
    import asyncio
    from services.acp_contract import segment_research as sr

    fake_llm = MagicMock()
    fake_llm.generate.side_effect = [_resp("not json at all")]
    with patch.object(sr, "LLMClient", return_value=fake_llm), \
         patch.object(sr, "record_call_with_pool", new=AsyncMock()) as m_log:
        result = await sr._research_place(
            "kyoto", ["explore"], ["US"], [(2840, "United States", "en")], {}, MagicMock(),
            _pool_with_conn(), asyncio.Semaphore(1),
        )

    assert result.llm_calls == 1
    m_log.assert_awaited_once()
    assert m_log.await_args.kwargs["quality_signal"] == {"step": 1, "tool": None, "parsed": False}
