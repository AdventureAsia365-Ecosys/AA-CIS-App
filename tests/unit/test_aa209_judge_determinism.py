"""AA-209 PART 1: judge reproducibility at the client layer.

Root cause: shared/llm_client/client.py:_call_openai never forwarded temperature/seed, so the
GPT-4.1 judge ran at OpenAI's default temperature (1.0) with no seed — same content produced
different score_overall across versions (v4=7.0 vs v5=9.0).

These tests pin the fix:
  * _call_openai forwards temperature AND seed ONLY when the caller explicitly set them
    (forward-if-present), so content/T3-fallback calls that omit them keep default behavior.
  * judge_node builds its request with a low temperature + fixed seed so it is reproducible.
"""

from unittest.mock import MagicMock, patch

from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest, LLMResponse
from services.content_generation.judge_node import judge_node, _JUDGE_SEED, _JUDGE_TEMPERATURE


def _make_client() -> LLMClient:
    """Construct LLMClient without touching real boto3/openai, then swap in a mock openai client."""
    with patch("shared.llm_client.client.boto3.client"), \
         patch("shared.llm_client.client.openai.OpenAI"):
        client = LLMClient()
    fake_create_resp = MagicMock()
    fake_create_resp.choices = [MagicMock(message=MagicMock(content='{"ok": true}'), finish_reason="stop")]
    fake_create_resp.usage = MagicMock(prompt_tokens=100, completion_tokens=20)
    client._openai = MagicMock()
    client._openai.chat.completions.create.return_value = fake_create_resp
    return client


def _create_kwargs(client: LLMClient) -> dict:
    return client._openai.chat.completions.create.call_args.kwargs


def test_call_openai_forwards_temperature_and_seed_when_present():
    """Caller explicitly sets temperature + seed → both forwarded to the OpenAI SDK call."""
    client = _make_client()
    request = LLMRequest(
        system_prompt="sys", user_prompt="usr",
        model_tier="gpt-4.1", temperature=0.1, seed=42,
    )
    resp = client._call_openai(request, model="gpt-4.1")

    kwargs = _create_kwargs(client)
    assert kwargs["temperature"] == 0.1
    assert kwargs["seed"] == 42
    # core call shape preserved
    assert kwargs["model"] == "gpt-4.1"
    assert kwargs["max_tokens"] == request.max_tokens
    assert isinstance(resp, LLMResponse)
    assert resp.provider == "openai"
    assert resp.stop_reason == "stop"  # AA-493 — _make_client()'s fake response finish_reason


def test_call_openai_omits_temperature_and_seed_when_absent():
    """Caller relies on defaults (no explicit temperature/seed) → neither is forwarded.

    Guards the T3 content-fallback path: forwarding the 0.7 default would change its behavior.
    """
    client = _make_client()
    request = LLMRequest(system_prompt="sys", user_prompt="usr", model_tier="haiku")
    client._call_openai(request, model="gpt-4.1")

    kwargs = _create_kwargs(client)
    assert "temperature" not in kwargs
    assert "seed" not in kwargs


def test_call_openai_omits_seed_when_only_temperature_set():
    """Independent forwarding: temperature set, seed unset → only temperature forwarded."""
    client = _make_client()
    request = LLMRequest(system_prompt="sys", user_prompt="usr", temperature=0.3)
    client._call_openai(request, model="gpt-4.1")

    kwargs = _create_kwargs(client)
    assert kwargs["temperature"] == 0.3
    assert "seed" not in kwargs


def test_judge_node_builds_reproducible_request():
    """judge_node sends temperature=0.1 + fixed seed, both marked explicitly-set on the request."""
    captured = {}

    def _capture(req):
        captured["request"] = req
        return LLMResponse(
            content='{"brand_fit_score": 8, "cross_brand_distinct": 8, '
                    '"mission_present": true, "feedback": ""}',
            model_used="gpt-4.1", provider="openai",
            input_tokens=10, output_tokens=5, cost_usd=0.001,
        )

    state = {
        "brand_core_idea": "Discreet executive adventure",
        "brand_customer_mindset": "Wants privacy and effortless logistics",
        "brand_voice_examples": ["understated", "assured"],
        "generated": {"name": "X", "subtitle": "y", "summary": "z", "highlights": ["a"],
                      "itineraries": "Day 1 -- board.", "seo_title": "t", "seo_meta": "m"},
        "quality_score": 9.0,
        "feedback": "",
    }
    with patch("services.content_generation.brand_fit.LLMClient") as MockClient:
        MockClient.return_value.generate.side_effect = _capture
        judge_node(state)

    req = captured["request"]
    assert req.temperature == _JUDGE_TEMPERATURE == 0.1
    assert req.seed == _JUDGE_SEED == 42
    # both must be in model_fields_set so _call_openai actually forwards them
    assert "temperature" in req.model_fields_set
    assert "seed" in req.model_fields_set


def test_llmrequest_seed_defaults_to_none_and_unset():
    """New seed field is backward-compatible: default None and not in model_fields_set when omitted."""
    req = LLMRequest(system_prompt="s", user_prompt="u")
    assert req.seed is None
    assert "seed" not in req.model_fields_set
