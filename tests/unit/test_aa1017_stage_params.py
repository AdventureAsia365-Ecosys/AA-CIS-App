"""AA-1017 (migration 320) — shared.stage_model_params: call site -> stage x model row -> protocol default.

The table is empty at 320: ``stage_params.apply`` must then hand back the very request object it got, so every provider
request is byte-identical (the golden snapshot test proves it for the gateway; this file pins the module). With a row:
max_tokens / temperature reach the Converse request, a call site's explicit value wins, min_output_tokens raises a
default max_tokens, a model without temperature never gets one, and effort reaches the Anthropic Messages API only.
"""
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import stage_params
from shared.llm_client.adapters import anthropic_messages as am
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.connections import ProviderConnection
from shared.llm_client.deployments import ModelDeployment
from shared.llm_client.models import LLMRequest
from shared.llm_client.stage_params import StageModelParams
from shared.llm_client.vendor_route import VendorDeployment

LUNA = CatalogModel(model_key="gpt-6-luna", label="l", vendor="openai", provider="bedrock", api_style="converse",
                    bedrock_profile_ids={"acc3": "global.openai.gpt-6-luna"}, callable_via=("llm_client",),
                    supports_temperature=True, price_in_per_mtok=0.2, price_out_per_mtok=1.2, enabled=True)
SONNET55 = CatalogModel(model_key="sonnet-5-5", label="s", vendor="anthropic", provider="bedrock",
                        api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5-5"},
                        callable_via=("llm_client",), supports_temperature=False, max_output_tokens=128000,
                        price_in_per_mtok=2.0, price_out_per_mtok=10.0, enabled=True)


@pytest.fixture
def table(monkeypatch):
    """The cached table, filled by the test (no DB)."""
    rows: dict = {}
    monkeypatch.setattr(stage_params, "_params", rows)
    monkeypatch.setattr(stage_params, "_loaded_at", 10 ** 12)          # fresh: no load
    monkeypatch.delenv(stage_params.SWITCH_ENV, raising=False)
    stage_params._NOT_APPLIED_WARNED.clear()
    return rows


def _req(**kw):
    return LLMRequest(system_prompt="s", user_prompt="u", stage="t2_judge", **kw)


def test_empty_table_returns_the_same_request_object(table):
    req = _req()
    assert stage_params.apply(req, LUNA) is req
    assert stage_params.apply(LLMRequest(system_prompt="s", user_prompt="u"), LUNA) is not None


def test_no_stage_reads_nothing(monkeypatch):
    load = MagicMock()
    monkeypatch.setattr(stage_params, "_ensure_loaded", load)
    assert stage_params.params_for(None, "gpt-6-luna") is None
    load.assert_not_called()


def test_the_row_sets_what_the_call_site_did_not(table):
    table[("t2_judge", "gpt-6-luna")] = StageModelParams("t2_judge", "gpt-6-luna", max_tokens=900, temperature=0.1)
    req = _req()
    out = stage_params.apply(req, LUNA)
    assert out is not req and (out.max_tokens, out.temperature) == (900, 0.1)
    assert {"max_tokens", "temperature"} <= out.model_fields_set
    assert (req.max_tokens, req.temperature) == (4096, 0.7)                 # the caller's object is untouched
    explicit = _req(max_tokens=200, temperature=0.9)
    assert stage_params.apply(explicit, LUNA) is explicit                   # the call site wins


def test_min_output_tokens_raises_only_a_default(table):
    table[("t2_judge", "gpt-6-luna")] = StageModelParams("t2_judge", "gpt-6-luna", min_output_tokens=8000)
    assert stage_params.apply(_req(), LUNA).max_tokens == 8000
    assert stage_params.apply(_req(max_tokens=100), LUNA).max_tokens == 100


def test_no_temperature_for_a_model_that_takes_none(table):
    table[("t2_judge", "sonnet-5-5")] = StageModelParams("t2_judge", "sonnet-5-5", temperature=0.2)
    req = _req()
    assert stage_params.apply(req, SONNET55) is req


def test_the_switch_and_a_failed_read(table, monkeypatch):
    table[("t2_judge", "gpt-6-luna")] = StageModelParams("t2_judge", "gpt-6-luna", max_tokens=900)
    monkeypatch.setenv(stage_params.SWITCH_ENV, "off")
    req = _req()
    assert stage_params.apply(req, LUNA) is req
    monkeypatch.delenv(stage_params.SWITCH_ENV)
    monkeypatch.setattr(stage_params, "_loaded_at", 0.0)
    monkeypatch.setattr(stage_params, "_failed_at", 0.0)
    with patch.object(stage_params, "_run_sync", side_effect=OSError("relation does not exist")):
        assert stage_params.params_for("t2_judge", "gpt-6-luna") is not None     # the last good read stays
    monkeypatch.setattr(stage_params, "_params", {})
    monkeypatch.setattr(stage_params, "_failed_at", 0.0)
    with patch.object(stage_params, "_run_sync", side_effect=OSError("db down")):
        assert stage_params.apply(req, LUNA) is req


def _converse(req, model):
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    rt = MagicMock()
    rt.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}},
                                "usage": {"inputTokens": 1, "outputTokens": 1}, "stopReason": "end_turn"}
    with patch("shared.llm_client.client.get_model_sync", return_value=model), \
            patch("shared.llm_client.client.get_stage_config_sync", return_value=None), \
            patch("shared.llm_client.client.deployment_plan", return_value=None), \
            patch("shared.llm_client.client.vendor_deployment_for", return_value=None), \
            patch("shared.llm_client.pricing._catalog_rates",
                  return_value={"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}), \
            patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt), \
            patch("shared.llm_client.client.stream_sink.delta_callback", return_value=None):
        c.generate(req)
    return rt.converse.call_args.kwargs


def test_converse_request_with_and_without_a_row(table):
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="t2_judge", model_tier="gpt-6-luna")
    before = _converse(req, LUNA)
    assert before["inferenceConfig"] == {"maxTokens": 4096}                 # empty table: today's request
    table[("t2_judge", "gpt-6-luna")] = StageModelParams("t2_judge", "gpt-6-luna", max_tokens=700, temperature=0.3,
                                                         effort="low")
    after = _converse(req, LUNA)
    assert after["inferenceConfig"] == {"maxTokens": 700, "temperature": 0.3}
    assert "additionalModelRequestFields" not in after                     # effort is not sent on Converse
    assert {k: v for k, v in after.items() if k != "inferenceConfig"} == \
        {k: v for k, v in before.items() if k != "inferenceConfig"}


def test_effort_reaches_the_anthropic_api_as_the_stage_effort(table):
    conn = ProviderConnection(connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key", label="a",
                              data_scope="vendor_direct", enabled=True, secret_name="aa-llm/dev/anthropic-direct")
    dep = ModelDeployment("sonnet-5-5", "anthropic-direct", "claude-sonnet-5-5", 100, True, "seed",
                          request_defaults={"effort": "medium", "effort_by_stage": {"other": "max"}})
    result = am.MessagesResult(text="ok", provider_model="m", stop_reason="end_turn", stop_details=None,
                               input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_write_tokens=0,
                               structured=False, effort=None)
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()

    def call():
        with patch.object(am, "send", return_value=result) as send, \
                patch("shared.llm_client.pricing._catalog_rates",
                      return_value={"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}):
            c._call_anthropic_messages(_req(), SONNET55, VendorDeployment(dep, conn))
        return send.call_args.kwargs["request_defaults"]

    assert call() is dep.request_defaults                                 # empty table: the deployment's own
    table[("t2_judge", "sonnet-5-5")] = StageModelParams("t2_judge", "sonnet-5-5", effort="high")
    sent = call()
    assert sent["effort_by_stage"] == {"other": "max", "t2_judge": "high"} and sent["effort"] == "medium"
    assert am.resolve_effort("claude-sonnet-5-5", "t2_judge", sent) == "high"
    assert dep.request_defaults["effort_by_stage"] == {"other": "max"}    # the deployment row is not changed


def test_effort_on_a_converse_model_is_said_once(table, monkeypatch):
    warn = MagicMock()
    monkeypatch.setattr(stage_params.logger, "warning", warn)
    table[("t2_judge", "gpt-6-luna")] = StageModelParams("t2_judge", "gpt-6-luna", reasoning_mode="off")
    stage_params.warn_not_applied("t2_judge", "gpt-6-luna", "converse")
    stage_params.warn_not_applied("t2_judge", "gpt-6-luna", "converse")
    assert warn.call_count == 1 and warn.call_args.args[0] == "stage_params_not_applied"


def test_the_read_sql_names_the_320_columns():
    for col in ("stage", "model_key", "max_tokens", "min_output_tokens", "temperature", "effort", "reasoning_mode"):
        assert col in stage_params._SELECT_SQL
    assert "shared.stage_model_params" in stage_params._SELECT_SQL
