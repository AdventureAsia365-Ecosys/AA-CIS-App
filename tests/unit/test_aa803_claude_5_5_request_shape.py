"""AA-803 (S246) — the Converse request the gateway sends to Claude Haiku 5.5 / Sonnet 5.5 (migration 259).

Both models answer with a 400 on: a non-default temperature / top_p, `budget_tokens`, thinking `disabled`
(Sonnet 5.5) and an assistant prefill. Sonnet 5.5 also 400s on a forced tool choice (`any` / `tool`);
Haiku 5.5 accepts one (the response then starts with the tool call, no thinking block) and keeps it. These
tests pin the request shape so none of the rejected fields is ever sent, and that structured output still
comes back.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import client as client_mod
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest


def _row(key, **kw):
    """The catalog row as migration 259 seeds it."""
    profile = f"global.anthropic.claude-{key}"
    base = dict(model_key=key, label=key, vendor="anthropic", provider="bedrock", api_style="converse",
                bedrock_profile_ids={"acc3": profile, "acc1": profile}, callable_via=("llm_client",),
                supports_temperature=False, max_output_tokens=128000, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


HAIKU55 = _row("haiku-5-5", price_in_per_mtok=0.1, price_out_per_mtok=0.5,
               price_cache_read_per_mtok=0.01, price_cache_write_per_mtok=0.125)
SONNET55 = _row("sonnet-5-5", price_in_per_mtok=2.0, price_out_per_mtok=10.0,
                price_cache_read_per_mtok=0.2, price_cache_write_per_mtok=2.5)
LUNA = CatalogModel(model_key="gpt-5.6-luna", label="l", vendor="openai", provider="bedrock",
                    api_style="converse", bedrock_profile_ids={"acc3": "global.openai.gpt-5.6-luna"},
                    callable_via=("llm_client",), supports_temperature=False,
                    price_in_per_mtok=0.2, price_out_per_mtok=1.2, enabled=True)
SONNET5 = _row("sonnet-5", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5"},
               max_output_tokens=None, price_in_per_mtok=2.0, price_out_per_mtok=10.0)
SCHEMA = {"name": "verdict", "schema": {"type": "object", "required": ["ok"],
                                        "properties": {"ok": {"type": "boolean"}}}}
NEW_MODELS = [HAIKU55, SONNET55]
NO_FORCED_TOOL = [SONNET55]          # models that 400 on toolChoice any / tool


@pytest.fixture(autouse=True)
def _fresh_rejections():
    client_mod._FORCED_TOOL_REJECTED.clear()
    yield
    client_mod._FORCED_TOOL_REJECTED.clear()


def _client():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        return LLMClient()


def _text_reply(text="hello", stop="end_turn"):
    # Adaptive thinking comes back as a reasoningContent block before the text; it must be ignored.
    return {"output": {"message": {"content": [
        {"reasoningContent": {"reasoningText": {"text": "", "signature": "sig"}}}, {"text": text}]}},
        "usage": {"inputTokens": 100, "outputTokens": 20}, "stopReason": stop}


def _tool_reply(value):
    return {"output": {"message": {"content": [
        {"reasoningContent": {"reasoningText": {"text": "", "signature": "sig"}}},
        {"toolUse": {"toolUseId": "t1", "name": "verdict", "input": value}}]}},
        "usage": {"inputTokens": 100, "outputTokens": 20}, "stopReason": "tool_use"}


def _generate(model, request, rt, account="acc3", stream_cb=None):
    c = _client()
    with patch("shared.llm_client.client.get_model_sync", return_value=model), \
            patch("shared.llm_client.client.get_stage_config_sync", return_value=None), \
            patch("shared.llm_client.pricing._catalog_rates",
                  return_value={"in": model.price_in_per_mtok / 1000, "out": model.price_out_per_mtok / 1000,
                                "cache_read": 0, "cache_write": 0}), \
            patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt) as gsc, \
            patch("shared.llm_client.client.stream_sink.delta_callback", return_value=stream_cb):
        resp = c.generate(request)
    gsc.assert_called_once_with("bedrock-runtime", account=account)
    return resp


def _req(model, **kw):
    return LLMRequest(system_prompt="sys", user_prompt="user", model_tier=model.model_key, **kw)


@pytest.mark.parametrize("model", NEW_MODELS, ids=lambda m: m.model_key)
def test_plain_call_shape(model):
    rt = MagicMock()
    rt.converse.return_value = _text_reply()
    resp = _generate(model, _req(model, temperature=0.1, max_tokens=4096), rt)
    kwargs = rt.converse.call_args.kwargs
    assert kwargs["modelId"] == f"global.anthropic.claude-{model.model_key}"
    assert kwargs["inferenceConfig"] == {"maxTokens": 4096}            # no temperature / topP
    assert "additionalModelRequestFields" not in kwargs                 # no thinking / budget_tokens
    assert "toolConfig" not in kwargs
    assert kwargs["system"] == [{"text": "sys"}]
    assert kwargs["messages"] == [{"role": "user", "content": [{"text": "user"}]}]  # no prefill
    assert rt.converse.call_count == 1
    assert resp.content == "hello"                                      # reasoning block ignored
    assert resp.model_used == f"satellite-{model.model_key}"
    assert resp.satellite_account == "acc3"
    assert resp.cost_usd == pytest.approx((100 * model.price_in_per_mtok + 20 * model.price_out_per_mtok) / 1e6)


@pytest.mark.parametrize("model", NEW_MODELS, ids=lambda m: m.model_key)
def test_max_tokens_capped_by_catalog(model):
    rt = MagicMock()
    rt.converse.return_value = _text_reply()
    _generate(model, _req(model, max_tokens=200000), rt)
    assert rt.converse.call_args.kwargs["inferenceConfig"]["maxTokens"] == 128000


@pytest.mark.parametrize("model", NEW_MODELS, ids=lambda m: m.model_key)
def test_routes_to_acc1_when_the_stage_prefers_it(model):
    rt = MagicMock()
    rt.converse.return_value = _text_reply()
    c = _client()
    with patch("shared.llm_client.client.get_model_sync", return_value=model), \
            patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt) as gsc, \
            patch("shared.llm_client.pricing._catalog_rates", return_value=None), \
            patch("shared.llm_client.client.stream_sink.delta_callback", return_value=None):
        resp = c._call_catalog_model(_req(model), model.model_key, "acc1")
    gsc.assert_called_once_with("bedrock-runtime", account="acc1")
    assert resp.satellite_account == "acc1"


@pytest.mark.parametrize("model", NO_FORCED_TOOL, ids=lambda m: m.model_key)
def test_structured_output_uses_auto_tool_choice(model):
    rt = MagicMock()
    rt.converse.return_value = _tool_reply({"ok": True})
    resp = _generate(model, _req(model, json_schema=SCHEMA), rt)
    kwargs = rt.converse.call_args.kwargs
    assert kwargs["toolConfig"]["toolChoice"] == {"auto": {}}           # never {"tool": ...} / {"any": ...}
    assert kwargs["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"] == {"json": SCHEMA["schema"]}
    assert kwargs["system"][0] == {"text": "sys"}                       # cache prefix unchanged
    assert "'verdict'" in kwargs["system"][1]["text"]
    assert kwargs["messages"][-1]["role"] == "user"
    assert "temperature" not in kwargs["inferenceConfig"]
    assert json.loads(resp.content) == {"ok": True}
    assert rt.converse.call_count == 1
    rt.converse_stream.assert_not_called()


@pytest.mark.parametrize("model", NO_FORCED_TOOL, ids=lambda m: m.model_key)
def test_structured_output_accepts_json_text_under_auto(model):
    rt = MagicMock()
    rt.converse.return_value = _text_reply('```json\n{"ok": false}\n```', stop="end_turn")
    resp = _generate(model, _req(model, json_schema=SCHEMA), rt)
    assert json.loads(resp.content) == {"ok": False}
    assert rt.converse.call_count == 1


@pytest.mark.parametrize("model", NO_FORCED_TOOL, ids=lambda m: m.model_key)
def test_schema_retry_keeps_auto_and_ends_on_a_user_turn(model):
    rt = MagicMock()
    rt.converse.side_effect = [_text_reply("I think it is fine."), _tool_reply({"ok": True})]
    resp = _generate(model, _req(model, json_schema=SCHEMA), rt)
    retry = rt.converse.call_args_list[1].kwargs
    assert retry["toolConfig"]["toolChoice"] == {"auto": {}}
    assert [m["role"] for m in retry["messages"]] == ["user", "assistant", "user"]
    assert json.loads(resp.content) == {"ok": True}
    assert resp.provider_requests == 2


@pytest.mark.parametrize("model", NEW_MODELS, ids=lambda m: m.model_key)
def test_stream_ignores_reasoning_deltas(model):
    rt = MagicMock()
    rt.converse_stream.return_value = {"stream": [
        {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "thinking..."}}}},
        {"contentBlockDelta": {"delta": {"text": "hel"}}},
        {"contentBlockDelta": {"delta": {"text": "lo"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 2}}},
    ]}
    seen = []
    resp = _generate(model, _req(model, temperature=0.3), rt, stream_cb=seen.append)
    kwargs = rt.converse_stream.call_args.kwargs
    assert "temperature" not in kwargs["inferenceConfig"]
    assert "additionalModelRequestFields" not in kwargs
    assert resp.content == "hello" and seen == ["hel", "lo"]


def test_haiku_5_5_keeps_the_forced_tool():
    """Haiku 5.5 accepts a forced tool choice: same shape as Haiku 4.5, no extra instruction block."""
    rt = MagicMock()
    rt.converse.return_value = _tool_reply({"ok": True})
    resp = _generate(HAIKU55, _req(HAIKU55, json_schema=SCHEMA, temperature=0.0), rt)
    kwargs = rt.converse.call_args.kwargs
    assert kwargs["toolConfig"]["toolChoice"] == {"tool": {"name": "verdict"}}
    assert kwargs["system"] == [{"text": "sys"}]
    assert kwargs["messages"][-1]["role"] == "user"
    assert "temperature" not in kwargs["inferenceConfig"]
    assert "additionalModelRequestFields" not in kwargs
    assert json.loads(resp.content) == {"ok": True}
    assert rt.converse.call_count == 1


def test_other_converse_models_keep_the_forced_tool():
    rt = MagicMock()
    rt.converse.return_value = _tool_reply({"ok": True})
    _generate(LUNA, _req(LUNA, json_schema=SCHEMA), rt)
    kwargs = rt.converse.call_args.kwargs
    assert kwargs["toolConfig"]["toolChoice"] == {"tool": {"name": "verdict"}}
    assert kwargs["system"] == [{"text": "sys"}]


def test_forced_tool_rejection_switches_to_auto_once_and_is_remembered():
    rejected = RuntimeError('An error occurred (ValidationException) when calling the Converse operation: '
                            'tool_choice: type "tool" and "any" are not supported for this model.')
    rt = MagicMock()
    rt.converse.side_effect = [rejected, _tool_reply({"ok": True})]
    resp = _generate(SONNET5, _req(SONNET5, json_schema=SCHEMA), rt)
    first, second = (c.kwargs for c in rt.converse.call_args_list)
    assert second["toolConfig"]["toolChoice"] == {"auto": {}}
    assert len(second["system"]) == 2
    assert json.loads(resp.content) == {"ok": True}
    assert "sonnet-5" in client_mod._FORCED_TOOL_REJECTED

    rt2 = MagicMock()
    rt2.converse.return_value = _tool_reply({"ok": False})
    _generate(SONNET5, _req(SONNET5, json_schema=SCHEMA), rt2)
    assert rt2.converse.call_count == 1                                  # straight to auto now
    assert rt2.converse.call_args.kwargs["toolConfig"]["toolChoice"] == {"auto": {}}


def test_unrelated_validation_error_is_not_retried():
    rt = MagicMock()
    rt.converse.side_effect = RuntimeError("ValidationException: The model returned an invalid request")
    with pytest.raises(RuntimeError):
        _generate(LUNA, _req(LUNA, json_schema=SCHEMA), rt)
    assert rt.converse.call_count == 1
    assert client_mod._FORCED_TOOL_REJECTED == set()
