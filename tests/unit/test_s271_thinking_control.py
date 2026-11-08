"""AA-741 (S271, lane l10-thinking-budget) — the gateway's thinking budget per stage x model.

The Claude 5.x models take no numeric thinking budget (``budget_tokens`` is a 400 on Sonnet / Haiku / Opus 5.5) and
bill their adaptive thinking as output inside max_tokens; the bound is ``output_config.effort``, plus the ``thinking``
type when a stage must not think. ``shared.stage_model_params`` (effort, reasoning_mode) carries it per stage x model
(CIS 381 seeds the writer / validate efforts). Pinned here:

1. ``resolve_thinking``: the one ``thinking`` form each Claude family takes per reasoning_mode, never one that is a
   400 (between_tools only on Sonnet 5.5, never 'off' at xhigh / max, nothing on Opus 5.5 / Fable for 'off').
2. On the wire (fake Anthropic API): the stage's effort and thinking reach the request body; an empty table sends
   exactly today's body; a row is per model, so a fallback model never inherits the primary's thinking.
3. The cut-rate model: a fake API that bills thinking by effort cuts a ~1,300-token article at 12000 when sent at
   high (the 06/11 t9_write case) and answers it under 6000 output tokens at medium (the 381 row).
4. The ledger is unchanged: the same billed-field keys with or without a row; effort carries the value sent.
5. OpenAI / Azure: the row's effort as ``reasoning_effort`` on a reasoning model only, low / medium / high only.

No AWS, no vendor, no DB, no paid call. Model names are the catalog's keys or invented ("lantern-mini").
"""
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import call_log, connections, stage_params
from shared.llm_client.adapters import anthropic_messages as am
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.deployments import ModelDeployment
from shared.llm_client.models import LLMRequest
from shared.llm_client.stage_params import StageModelParams
from shared.llm_client.vendor_route import VendorDeployment
from tests.unit.aa1002_fake_anthropic import FakeAnthropic, anthropic_connection, install, stream_reply

SONNET55 = CatalogModel(model_key="sonnet-5-5", label="s", vendor="anthropic", provider="bedrock",
                        api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5-5"},
                        callable_via=("llm_client",), supports_temperature=False, max_output_tokens=128000,
                        price_in_per_mtok=2.0, price_out_per_mtok=10.0, enabled=True)
SONNET46 = CatalogModel(model_key="sonnet-4-6", label="s46", vendor="anthropic", provider="bedrock",
                        api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-4-6"},
                        callable_via=("llm_client",), supports_temperature=False, max_output_tokens=64000,
                        price_in_per_mtok=3.0, price_out_per_mtok=15.0, enabled=True)
GPT_OPENAI = CatalogModel(model_key="gpt-6-luna-openai", label="g", vendor="openai", provider="openai",
                          api_style="openai_chat", bedrock_profile_ids={}, callable_via=("llm_client",),
                          price_in_per_mtok=0.1, price_out_per_mtok=0.5, enabled=True)
LANTERN = CatalogModel(model_key="lantern-mini", label="l", vendor="openai", provider="openai",
                       api_style="openai_chat", bedrock_profile_ids={}, callable_via=("llm_client",),
                       price_in_per_mtok=0.1, price_out_per_mtok=0.5, enabled=True)


@pytest.fixture
def table(monkeypatch):
    """The cached stage x model table, filled by the test (no DB)."""
    rows: dict = {}
    monkeypatch.setattr(stage_params, "_params", rows)
    monkeypatch.setattr(stage_params, "_loaded_at", 10 ** 12)          # fresh: no load
    monkeypatch.delenv(stage_params.SWITCH_ENV, raising=False)
    stage_params._NOT_APPLIED_WARNED.clear()
    am._THINKING_WARNED.clear()
    return rows


@pytest.fixture
def fake(monkeypatch):
    f = FakeAnthropic()
    f.reader = install(monkeypatch, f)
    yield f
    am._clients.clear()
    connections._credentials.clear()


# ── 1. resolve_thinking ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("wire_id,mode,effort,want", [
    ("claude-sonnet-5-5", "off", "high", {"type": "between_tools"}),
    ("claude-sonnet-5-5", "off", "medium", {"type": "between_tools"}),
    ("claude-sonnet-5-5", "off", "xhigh", None),                       # 400 above high: adaptive is kept
    ("claude-sonnet-5-5", "adaptive", "high", {"type": "adaptive"}),
    ("claude-haiku-5-5", "off", "low", {"type": "disabled"}),
    ("claude-haiku-5-5", "off", "max", None),
    ("claude-opus-5", "off", "high", {"type": "disabled"}),
    ("claude-sonnet-5", "off", "medium", {"type": "disabled"}),
    ("claude-opus-5-5", "off", "low", None),                           # cannot turn thinking off at any effort
    ("claude-fable-5-1", "off", "low", None),
    ("claude-opus-5-5", "adaptive", "medium", {"type": "adaptive"}),
    ("claude-opus-4-8", "adaptive", "high", {"type": "adaptive"}),     # no thinking unless asked
    ("claude-opus-4-8", "off", "high", None),                          # already off: nothing sent
    ("claude-sonnet-4-6", "adaptive", None, {"type": "adaptive"}),
    ("claude-sonnet-4-6", "off", None, None),
    ("claude-haiku-4-5-20251001", "adaptive", None, None),             # manual budget only: not sent
    ("claude-haiku-4-5-20251001", "off", None, None),
    ("claude-lantern-9", "off", "low", None),                          # unknown family
    ("claude-sonnet-5-5", None, "high", None),
    ("claude-sonnet-5-5", "sometimes", "high", None),
])
def test_resolve_thinking_sends_only_a_form_the_model_takes(table, wire_id, mode, effort, want):
    assert am.resolve_thinking(wire_id, mode, effort) == want


def test_a_budget_is_never_sent_and_between_tools_only_to_sonnet_5_5(table):
    for family, facts in am._THINKING_FACTS.items():
        for value in facts.values():
            if isinstance(value, dict):
                assert set(value) == {"type"}, family                  # no budget_tokens / display / block_binding
                if value["type"] == "between_tools":
                    assert family == "claude-sonnet-5-5"


def test_a_mode_that_cannot_be_sent_is_warned_once(table, monkeypatch):
    warn = MagicMock()
    monkeypatch.setattr(am.logger, "warning", warn)
    am.resolve_thinking("claude-opus-5-5", "off", "low")
    am.resolve_thinking("claude-opus-5-5", "off", "low")
    assert warn.call_count == 1 and warn.call_args.args[0] == "llm_anthropic_thinking_not_sent"


def test_build_params_carries_thinking_only_when_a_mode_is_set():
    base = dict(wire_id="claude-sonnet-5-5", system="s", user="u", max_tokens=12000, stage="t9_write")
    assert "thinking" not in am.build_params(**base)
    assert am.build_params(**base, reasoning_mode="off")["thinking"] == {"type": "between_tools"}
    hot = am.build_params(**base, reasoning_mode="off", request_defaults={"effort_by_stage": {"t9_write": "max"}})
    assert "thinking" not in hot and hot["output_config"] == {"effort": "max"}


# ── 2. on the wire ──────────────────────────────────────────────────────────────────────────────────────────────────

def _dep(model_key, wire_id):
    return ModelDeployment(model_key, "anthropic-direct", wire_id, 100, True, "seed", request_defaults=None)


def _call(model, wire_id, stage="t9_write", max_tokens=12000):
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    req = LLMRequest(system_prompt="sys", user_prompt="usr", stage=stage, max_tokens=max_tokens)
    with patch("shared.llm_client.pricing._catalog_rates",
               return_value={"in": 2.0, "out": 10.0, "cache_read": 0.2, "cache_write": 2.5}), \
            patch("shared.llm_client.client.stream_sink.delta_callback", return_value=None):
        return c._call_anthropic_messages(req, model, VendorDeployment(_dep(model.model_key, wire_id),
                                                                       anthropic_connection()))


def test_the_empty_table_sends_todays_body(table, fake):
    _call(SONNET55, "claude-sonnet-5-5")
    body = fake.bodies[0]
    assert set(body) == {"model", "max_tokens", "system", "messages", "output_config", "stream"}
    assert body["output_config"] == {"effort": "high"} and "thinking" not in body


def test_the_stage_effort_and_thinking_reach_the_body(table, fake):
    table[("t9_write", "sonnet-5-5")] = StageModelParams("t9_write", "sonnet-5-5", effort="medium")
    resp = _call(SONNET55, "claude-sonnet-5-5")
    assert fake.bodies[0]["output_config"] == {"effort": "medium"} and "thinking" not in fake.bodies[0]
    assert resp.effort == "medium"
    table[("t9_write", "sonnet-5-5")] = StageModelParams("t9_write", "sonnet-5-5", effort="low", reasoning_mode="off")
    _call(SONNET55, "claude-sonnet-5-5")
    assert fake.bodies[1]["output_config"] == {"effort": "low"}
    assert fake.bodies[1]["thinking"] == {"type": "between_tools"}


def test_a_fallback_model_never_inherits_the_primarys_thinking(table, fake):
    table[("t9_write", "sonnet-5-5")] = StageModelParams("t9_write", "sonnet-5-5", effort="low", reasoning_mode="off")
    _call(SONNET46, "claude-sonnet-4-6")
    body = fake.bodies[0]
    assert "thinking" not in body and body["output_config"] == {"effort": "high"}   # Sonnet 4.6's own default
    table[("t9_write", "sonnet-4-6")] = StageModelParams("t9_write", "sonnet-4-6", reasoning_mode="off")
    _call(SONNET46, "claude-sonnet-4-6")
    assert "thinking" not in fake.bodies[1]                            # 4.6 is already off when nothing is sent


# ── 3. the cut-rate model ───────────────────────────────────────────────────────────────────────────────────────────

ARTICLE_TOKENS = 1300
THINKING_BY_EFFORT = {"low": 600, "medium": 3000, "high": 11000, "xhigh": 16000, "max": 24000}


def _billing_api(fake):
    """The fake API bills thinking + the article as output tokens and cuts the reply at max_tokens."""
    def reply():
        body = fake.bodies[-1]
        effort = (body.get("output_config") or {}).get("effort", "high")
        thinking = 0 if (body.get("thinking") or {}).get("type") == "between_tools" else THINKING_BY_EFFORT[effort]
        want = thinking + ARTICLE_TOKENS
        if want > body["max_tokens"]:
            return stream_reply("A walk that sta", stop_reason="max_tokens", output_tokens=body["max_tokens"])
        return stream_reply("A walk that starts at the old gate.", output_tokens=want)
    fake.replies[:] = [reply] * 10


def test_effort_high_cuts_the_article_and_medium_answers_it_under_6000(table, fake):
    _billing_api(fake)
    cut = _call(SONNET55, "claude-sonnet-5-5")
    assert cut.stop_reason == "max_tokens" and cut.output_tokens == 12000     # the 06/11 t9_write case
    table[("t9_write", "sonnet-5-5")] = StageModelParams("t9_write", "sonnet-5-5", effort="medium")  # CIS 381
    ok = _call(SONNET55, "claude-sonnet-5-5")
    assert ok.stop_reason == "end_turn" and ok.output_tokens < 6000
    assert ok.cost_usd < cut.cost_usd


# ── 4. the ledger ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_ledger_row_keys_do_not_change_and_effort_is_the_value_sent(table, fake):
    before = call_log.billed_fields(_call(SONNET55, "claude-sonnet-5-5"))
    table[("t9_write", "sonnet-5-5")] = StageModelParams("t9_write", "sonnet-5-5", effort="medium",
                                                         reasoning_mode="adaptive")
    after = call_log.billed_fields(_call(SONNET55, "claude-sonnet-5-5"))
    assert set(after) == set(before)
    assert before["effort"] == "high" and after["effort"] == "medium"
    assert fake.bodies[1]["thinking"] == {"type": "adaptive"}


# ── 5. OpenAI / Azure ───────────────────────────────────────────────────────────────────────────────────────────────

def _openai_call(model, stage="t9_write"):
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    c._openai = MagicMock()
    usage = MagicMock(prompt_tokens=100, completion_tokens=50, prompt_tokens_details=None,
                      completion_tokens_details=None)
    c._openai.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="ok"), finish_reason="stop")], usage=usage)
    with patch("shared.llm_client.client.get_model_sync", return_value=model), \
            patch("shared.llm_client.client.get_stage_config_sync", return_value=None), \
            patch("shared.llm_client.pricing.deployment_for", return_value=None), \
            patch("shared.llm_client.pricing._catalog_rates",
                  return_value={"in": 0.1, "out": 0.5, "cache_read": 0.01, "cache_write": 0}), \
            patch("shared.llm_client.client.stream_sink.delta_callback", return_value=None):
        resp = c._call_openai(LLMRequest(system_prompt="s", user_prompt="u", stage=stage), model=model.model_key,
                              catalog_model=model)
    return resp, c._openai.chat.completions.create.call_args.kwargs


def test_no_row_sends_no_reasoning_effort(table):
    resp, kwargs = _openai_call(GPT_OPENAI)
    assert "reasoning_effort" not in kwargs and resp.effort is None


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_the_row_effort_reaches_a_reasoning_model(table, effort):
    table[("t9_write", "gpt-6-luna-openai")] = StageModelParams("t9_write", "gpt-6-luna-openai", effort=effort)
    resp, kwargs = _openai_call(GPT_OPENAI)
    assert kwargs["reasoning_effort"] == effort and resp.effort == effort
    assert kwargs["max_completion_tokens"] == 4096                       # the gateway floor is not this lane's


def test_xhigh_asks_high_a_mode_or_a_model_that_does_not_reason_send_nothing(table, monkeypatch):
    """AA-1053: xhigh / max map explicitly to high (effort_rule.OPENAI_EFFORTS), the highest value every GPT
    reasoning model takes; before, nothing was sent (the model's default)."""
    warn = MagicMock()
    monkeypatch.setattr(stage_params.logger, "warning", warn)
    table[("t9_write", "gpt-6-luna-openai")] = StageModelParams("t9_write", "gpt-6-luna-openai", effort="xhigh",
                                                                reasoning_mode="off")
    resp, kwargs = _openai_call(GPT_OPENAI)
    assert kwargs["reasoning_effort"] == "high" and resp.effort == "high"
    assert warn.call_args.args[0] == "stage_params_not_applied"
    assert "effort" not in warn.call_args.kwargs and warn.call_args.kwargs["reasoning_mode"] == "off"
    table[("t9_write", "lantern-mini")] = StageModelParams("t9_write", "lantern-mini", effort="low")
    _, kwargs = _openai_call(LANTERN)
    assert "reasoning_effort" not in kwargs


def test_a_sent_effort_is_left_out_of_the_not_applied_warning(table, monkeypatch):
    warn = MagicMock()
    monkeypatch.setattr(stage_params.logger, "warning", warn)
    table[("t9_write", "gpt-6-luna-openai")] = StageModelParams("t9_write", "gpt-6-luna-openai", effort="low")
    _openai_call(GPT_OPENAI)
    warn.assert_not_called()
    stage_params._NOT_APPLIED_WARNED.clear()
    table[("t9_write", "gpt-6-luna-openai")] = StageModelParams("t9_write", "gpt-6-luna-openai", effort="low",
                                                                reasoning_mode="adaptive")
    _openai_call(GPT_OPENAI)
    assert warn.call_count == 1 and "effort" not in warn.call_args.kwargs
    assert warn.call_args.kwargs["reasoning_mode"] == "adaptive"
