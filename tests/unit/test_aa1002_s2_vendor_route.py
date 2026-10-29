"""AA-1002 (gateway slice S2) — LLMClient routes a catalog model through the Anthropic Messages API when, and only
when, its first enabled deployment (by priority) is on an enabled Anthropic connection, or a probe pins one.

End to end through the real adapter on a fake HTTP transport (tests/unit/aa1002_fake_anthropic.py): the response
the caller gets (label, provider, tokens with the cache parts, cost from the catalog price, the model the API
reported), structured output conformed, refusal / empty / bad JSON as billed-unusable replies the gateway logs,
401 as an account-level refusal, a 429 as a plain failure the route moves past. The "nothing changes" side is
test_aa1002_no_behaviour_change.py.
"""
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import catalog, connections, deployments, vendor_route
from shared.llm_client.adapters import anthropic_messages
from shared.llm_client.call_log import _normalize_model_provider, billed_record
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import BilledUnusableReply, LLMClient, RouteUnusableReply
from shared.llm_client.connections import ProviderConnection
from shared.llm_client.deployments import ModelDeployment
from shared.llm_client.errors import THROTTLED, LLMAccessDenied, classify
from shared.llm_client.models import LLMRequest
from tests.unit.aa1002_fake_anthropic import (
    FakeAnthropic,
    anthropic_connection,
    error_reply,
    install,
    stream_reply,
)

SONNET55 = CatalogModel(model_key="sonnet-5-5", label="Sonnet 5.5", vendor="anthropic", provider="bedrock",
                        api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5-5"},
                        callable_via=("llm_client",), supports_temperature=False, max_output_tokens=128000,
                        price_in_per_mtok=2.0, price_out_per_mtok=10.0, price_cache_read_per_mtok=0.2,
                        price_cache_write_per_mtok=2.5, enabled=True)
HAIKU55 = CatalogModel(model_key="haiku-5-5", label="Haiku 5.5", vendor="anthropic", provider="bedrock",
                       api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-haiku-5-5"},
                       callable_via=("llm_client",), supports_temperature=False, price_in_per_mtok=0.1,
                       price_out_per_mtok=0.5, enabled=True)
GPT = CatalogModel(model_key="gpt-6-luna", label="GPT", vendor="openai", provider="bedrock", api_style="converse",
                   bedrock_profile_ids={"acc3": "openai.gpt-6-luna"}, price_in_per_mtok=1.0, price_out_per_mtok=1.0,
                   enabled=True)
API_ONLY = CatalogModel(model_key="claude-api-only", label="x", vendor="anthropic", provider="anthropic",
                        api_style="anthropic_messages", price_in_per_mtok=1.0, price_out_per_mtok=1.0, enabled=True)
MODELS = {m.model_key: m for m in (SONNET55, HAIKU55, GPT, API_ONLY)}

ACC3 = ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="assume_role", label="x",
                          data_scope="aws_only", enabled=True)


def _deps(priority=5, enabled=True, model="sonnet-5-5", wire="claude-sonnet-5-5", defaults=None):
    acc3 = ModelDeployment(model, "acc3", f"global.anthropic.{wire}", 10, True, "bedrock_profile")
    api = ModelDeployment(model, "anthropic-direct", wire, priority, enabled, "seed",
                          request_defaults=defaults or {})
    return [acc3, api]


@pytest.fixture
def gw(monkeypatch):
    """A client whose catalog and registry are in memory, Bedrock mocked, the Anthropic API faked."""
    fake = FakeAnthropic()
    fake.reader = install(monkeypatch, fake)
    state = {"conns": {"acc3": ACC3, "anthropic-direct": anthropic_connection()}, "deps": _deps()}

    def fetch(fn):
        return dict(state["conns"]) if fn is connections._fetch_all else list(state["deps"])

    connections.invalidate()
    deployments.invalidate()
    catalog.invalidate()
    monkeypatch.setattr(connections, "run_sync", fetch)
    monkeypatch.setattr("shared.llm_client.client.get_model_sync", lambda k: MODELS.get(k))
    monkeypatch.setattr("shared.llm_client.pricing._catalog_rates", _rates)
    rt = MagicMock()
    rt.converse.return_value = {"output": {"message": {"content": [{"text": "from bedrock"}]}},
                                "usage": {"inputTokens": 10, "outputTokens": 5}, "stopReason": "end_turn"}
    monkeypatch.setattr("shared.llm_client.bedrock_satellite.get_satellite_client", lambda *a, **k: rt)
    monkeypatch.setattr("shared.llm_client.client.stream_sink.delta_callback", lambda stage: None)
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        client = LLMClient()

    def reset():
        connections.invalidate()
        deployments.invalidate()
    yield client, fake, state, rt, reset
    anthropic_messages._clients.clear()
    connections._credentials.clear()
    connections.invalidate()
    deployments.invalidate()


def _rates(model):
    m = MODELS.get(model)
    if m is None:
        return None
    return {"in": m.price_in_per_mtok / 1000, "out": m.price_out_per_mtok / 1000,
            "cache_read": (m.price_cache_read_per_mtok or m.price_in_per_mtok * 0.1) / 1000,
            "cache_write": (m.price_cache_write_per_mtok or m.price_in_per_mtok * 1.25) / 1000}


def _req(**kw):
    base = dict(system_prompt="sys", user_prompt="usr", model_tier="sonnet-5-5")
    base.update(kw)
    return LLMRequest(**base)


# ── routing ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_first_enabled_anthropic_deployment_serves_the_model(gw):
    client, fake, state, rt, _ = gw
    fake.replies.append(stream_reply("hi there", input_tokens=100, output_tokens=50, cache_read=1000,
                                     cache_write=200, model="claude-sonnet-5-5-20261015"))
    resp = client.generate(_req(temperature=0.2))
    rt.converse.assert_not_called()
    body = fake.bodies[0]
    assert body["model"] == "claude-sonnet-5-5" and "temperature" not in body      # never sent, even if asked
    assert resp.content == "hi there" and resp.provider == "anthropic"
    assert resp.model_used == "anthropic-api-sonnet-5-5" and resp.provider_model == "claude-sonnet-5-5-20261015"
    assert resp.input_tokens == 100 + 1000 + 200                                   # Converse convention
    assert (resp.cache_read_tokens, resp.cache_write_tokens, resp.output_tokens) == (1000, 200, 50)
    expected = round((100 * 0.002 + 50 * 0.010 + 1000 * 0.0002 + 200 * 0.0025) / 1000, 6)
    assert resp.cost_usd == expected and resp.satellite_account is None
    # llm_call_log: model = the Model Key, provider = anthropic, for any caller that passes model=resp.model_used
    assert _normalize_model_provider(resp.model_used, None) == ("sonnet-5-5", "anthropic")
    assert billed_record(resp)["provider"] == "anthropic"


def test_max_tokens_is_capped_by_the_catalog(gw):
    client, fake, *_ = gw
    client.generate(_req(max_tokens=500_000))
    assert fake.bodies[0]["max_tokens"] == 128000


@pytest.mark.parametrize("change", ["deployment_disabled", "connection_disabled", "behind_acc3", "no_rows"])
def test_any_other_registry_state_keeps_bedrock(gw, change):
    client, fake, state, rt, reset = gw
    if change == "deployment_disabled":
        state["deps"] = _deps(enabled=False)
    elif change == "connection_disabled":
        state["conns"]["anthropic-direct"] = anthropic_connection(enabled=False)
    elif change == "behind_acc3":
        state["deps"] = _deps(priority=100)
    else:
        state["deps"] = []
    reset()
    resp = client.generate(_req())
    assert resp.content == "from bedrock" and resp.provider == "bedrock-satellite"
    assert fake.requests == [] and fake.reader.call_count == 0


def test_a_disabled_acc3_connection_ahead_is_skipped(gw):
    client, fake, state, rt, reset = gw
    state["conns"]["acc3"] = ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="assume_role",
                                                label="x", data_scope="aws_only", enabled=False)
    state["deps"] = _deps(priority=100)
    reset()
    assert client.generate(_req()).provider == "anthropic"


def test_a_non_anthropic_model_never_runs_on_the_anthropic_api(gw):
    client, fake, state, rt, reset = gw
    state["deps"] = [ModelDeployment("gpt-6-luna", "anthropic-direct", "claude-sonnet-5-5", 0, True, "admin")]
    reset()
    assert client.generate(_req(model_tier="gpt-6-luna")).provider == "bedrock-satellite"
    assert fake.requests == []


def test_registry_failure_falls_back_to_todays_path(gw, monkeypatch):
    client, fake, state, rt, reset = gw
    monkeypatch.setattr(vendor_route, "_first_enabled", MagicMock(side_effect=KeyError("boom")))
    assert client.generate(_req()).provider == "bedrock-satellite"


def test_api_only_model_without_a_deployment_fails_clearly(gw):
    client, fake, state, rt, reset = gw
    with pytest.raises(RuntimeError, match="served only by the Anthropic API"):
        client.generate(_req(model_tier="claude-api-only"))
    state["deps"] = [ModelDeployment("claude-api-only", "anthropic-direct", "claude-x-1", 10, True, "admin")]
    reset()
    assert client.generate(_req(model_tier="claude-api-only")).provider == "anthropic"


# ── pinned probe ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_pin_reaches_a_deployment_behind_bedrock(gw):
    client, fake, state, rt, reset = gw
    state["deps"] = _deps(priority=100, model="haiku-5-5", wire="claude-haiku-5-5")
    reset()
    resp = client.generate(_req(model_tier="haiku-5-5@anthropic-direct", stage="adhoc_aa1002", max_tokens=64))
    assert resp.provider == "anthropic" and resp.model_used == "anthropic-api-haiku-5-5"
    assert fake.bodies[0]["model"] == "claude-haiku-5-5" and fake.bodies[0]["output_config"] == {"effort": "medium"}
    assert client.generate(_req(model_tier="haiku-5-5")).provider == "bedrock-satellite"   # routes unchanged


@pytest.mark.parametrize("change,match", [("connection_disabled", "missing or disabled"),
                                          ("deployment_disabled", "is disabled"),
                                          ("no_row", "no deployment")])
def test_a_pin_needs_an_enabled_deployment_and_connection(gw, change, match):
    client, fake, state, rt, reset = gw
    if change == "connection_disabled":
        state["conns"]["anthropic-direct"] = anthropic_connection(enabled=False)
    elif change == "deployment_disabled":
        state["deps"] = _deps(enabled=False)
    else:
        state["deps"] = []
    reset()
    with pytest.raises(RuntimeError, match=match):
        client.generate(_req(model_tier="sonnet-5-5@anthropic-direct"))
    assert fake.requests == []


def test_a_pin_in_a_stage_route_is_skipped_not_called(gw, monkeypatch):
    client, fake, state, rt, reset = gw
    cfg = MagicMock(model_id="sonnet-5-5@anthropic-direct", fallback_model_ids=["gpt-6-luna"], role="judge",
                    account_route=None, shadow_model_id=None, shadow_sample_pct=0)
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda stage: cfg)
    resp = client.generate(LLMRequest(system_prompt="s", user_prompt="u", stage="t2_judge"))
    assert resp.provider == "bedrock-satellite" and fake.requests == []


# ── replies ────────────────────────────────────────────────────────────────────────────────────────────────────────

SCHEMA = {"name": "verdict", "schema": {"type": "object", "properties": {"score": {"type": "integer"}},
                                        "required": ["score"]}}


def test_structured_reply_is_conformed(gw):
    client, fake, *_ = gw
    fake.replies.append(stream_reply('{"score": 8}'))
    resp = client.generate(_req(json_schema=SCHEMA))
    assert resp.content == '{"score": 8}'
    assert fake.bodies[0]["output_config"]["format"]["schema"]["additionalProperties"] is False


@pytest.mark.parametrize("reply,match", [
    (dict(text="", stop_reason="refusal", stop_details={"type": "refusal", "category": "cyber"}), "refused"),
    (dict(text="", stop_reason="max_tokens"), "empty content"),
    (dict(text='{"score": "high"}'), "does not match schema"),
    (dict(text="not json"), "Expecting value"),
])
def test_billed_but_unusable_replies_carry_their_usage(gw, reply, match):
    client, fake, *_ = gw
    text = reply.pop("text")
    fake.replies.append(stream_reply(text, input_tokens=30, output_tokens=7, **reply))
    with patch("shared.llm_client.client.log_gateway_billed_failure") as logged:
        with pytest.raises(BilledUnusableReply, match=match) as e:
            client.generate(_req(json_schema=SCHEMA))
    billed = e.value.billed_usage[0]
    assert billed["model"] == "anthropic-api-sonnet-5-5" and billed["tokens_in"] == 30 and billed["tokens_out"] == 7
    assert billed["cost_usd"] > 0
    assert logged.call_count == 1                                            # the gateway's billed_unusable row
    assert _normalize_model_provider(billed["model"], None)[1] == "anthropic"


def test_refusal_on_a_route_moves_to_the_next_model(gw, monkeypatch):
    client, fake, *_ = gw
    fake.replies.append(stream_reply("", stop_reason="refusal"))
    cfg = MagicMock(model_id="sonnet-5-5", fallback_model_ids=["gpt-6-luna"], role="judge", account_route=None,
                    shadow_model_id=None, shadow_sample_pct=0)
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda stage: cfg)
    with patch("shared.llm_client.client.log_gateway_billed_failure"):
        resp = client.generate(LLMRequest(system_prompt="s", user_prompt="u", stage="t2_judge"))
    assert resp.provider == "bedrock-satellite" and resp.fallback_used is True


def test_refusal_on_every_model_is_a_route_unusable_reply(gw, monkeypatch):
    client, fake, state, rt, reset = gw
    state["deps"] = _deps() + _deps(model="haiku-5-5", wire="claude-haiku-5-5")
    reset()
    fake.replies += [stream_reply("", stop_reason="refusal"), stream_reply("", stop_reason="refusal")]
    cfg = MagicMock(model_id="sonnet-5-5", fallback_model_ids=["haiku-5-5"], role="writer", account_route=None,
                    shadow_model_id=None, shadow_sample_pct=0)
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda stage: cfg)
    with patch("shared.llm_client.client.log_gateway_billed_failure"):
        with pytest.raises(RouteUnusableReply):
            client.generate(LLMRequest(system_prompt="s", user_prompt="u", stage="s1_generate"))


def test_a_401_is_an_account_level_refusal(gw):
    client, fake, *_ = gw
    fake.replies.append(error_reply(401, "authentication_error", "invalid x-api-key"))
    with pytest.raises(LLMAccessDenied):
        client.generate(_req())


def test_a_429_is_a_throttle_not_a_denial(gw):
    client, fake, *_ = gw
    fake.replies.append(error_reply(429, "rate_limit_error", "slow down"))
    with pytest.raises(RuntimeError) as e:
        client.generate(_req())
    assert not isinstance(e.value, LLMAccessDenied) and classify(e.value) == THROTTLED


def test_a_missing_secret_fails_the_attempt_closed(gw):
    client, fake, *_ = gw
    fake.reader.side_effect = RuntimeError("ResourceNotFoundException")
    with pytest.raises(RuntimeError, match="secret unreadable"):
        client.generate(_req())
    assert fake.requests == []


def test_golden_set_guard_blocks_the_adapter():
    from services.golden_set import llm_guard
    undo = llm_guard.install("test")
    try:
        with pytest.raises(llm_guard.LLMCallForbidden):
            anthropic_messages.send(connection=anthropic_connection(), wire_id="claude-haiku-5-5", system="s",
                                    user="u", max_tokens=1, stage="x")
    finally:
        undo()
