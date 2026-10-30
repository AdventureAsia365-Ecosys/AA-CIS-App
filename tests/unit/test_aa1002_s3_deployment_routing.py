"""AA-1002 (gateway slice S3) — a catalog model is spread over its deployments; legacy keys resolve through aliases;
satellite sessions and embeddings come from the connection registry.

Every network seam is faked (no AWS, no Anthropic, no OpenAI, no DB). The "nothing changes for today's rows" side is
test_aa1002_s3_routes_snapshot.py (every Dev stage) and test_aa1002_no_behaviour_change.py.
"""
import io
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import bedrock_satellite as bs
from shared.llm_client import catalog, connections, deployments, embed as embed_mod, vendor_route
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import BilledUnusableReply, LLMClient
from shared.llm_client.connections import BUILTIN_CONNECTIONS, ProviderConnection
from shared.llm_client.deployments import ModelAlias, ModelDeployment
from shared.llm_client.errors import LLMAccessDenied
from shared.llm_client.models import LLMRequest
from shared.llm_client.role_config import StageConfig

SCHEMA = {"name": "verdict", "schema": {"type": "object", "properties": {"score": {"type": "integer"}},
                                        "required": ["score"]}}

SONNET55 = CatalogModel(model_key="sonnet-5-5", label="Sonnet 5.5", vendor="anthropic", provider="bedrock",
                        api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5-5"},
                        callable_via=("llm_client",), supports_temperature=False, price_in_per_mtok=2.0,
                        price_out_per_mtok=10.0, enabled=True)
GPT6 = CatalogModel(model_key="gpt-6-luna", label="GPT-6 Luna", vendor="openai", provider="bedrock",
                    api_style="converse", bedrock_profile_ids={"acc3": "global.openai.gpt-6-luna"},
                    callable_via=("llm_client",), supports_temperature=False, price_in_per_mtok=0.1,
                    price_out_per_mtok=0.5, enabled=True)
SONNET = CatalogModel(model_key="sonnet", label="Claude Sonnet 4.6", vendor="anthropic", provider="bedrock",
                      api_style="anthropic_native", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-4-6"},
                      callable_via=("llm_client",), price_in_per_mtok=3.0, price_out_per_mtok=15.0, enabled=True)
HAIKU = CatalogModel(model_key="haiku", label="Claude Haiku 4.5", vendor="anthropic", provider="bedrock",
                     api_style="anthropic_native",
                     bedrock_profile_ids={"acc2": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                                          "acc3": "global.anthropic.claude-haiku-4-5-20251001-v1:0"},
                     callable_via=("llm_client",), price_in_per_mtok=1.0, price_out_per_mtok=5.0, enabled=True)
EMBED = CatalogModel(model_key="cohere-embed-v4", label="Cohere", vendor="cohere", provider="bedrock",
                     api_style="embed", bedrock_profile_ids={"acc2": "global.cohere.embed-v4:0"},
                     callable_via=("embed",), price_in_per_mtok=0.12, price_out_per_mtok=0.0, enabled=True)
MODELS = {m.model_key: m for m in (SONNET55, GPT6, SONNET, HAIKU, EMBED)}


def _org(enabled=True, **kw):
    """A second assume-role Bedrock connection (the future LLM account of S1-infra), account from a fixed field."""
    base = dict(connection_key="bedrock-llm-org", kind="bedrock", auth_mode="assume_role", label="org",
                data_scope="aws_only", enabled=enabled, account_id="111122223333", region="us-west-2",
                role_name="AA-LLM-Invoker", external_id="aa-llm-org-ext")
    base.update(kw)
    return ProviderConnection(**base)


def _anthropic(enabled=True):
    return ProviderConnection(connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key",
                              label="anthropic", data_scope="vendor_direct", enabled=enabled,
                              secret_name="aa-llm/dev/anthropic-direct", base_url="https://api.anthropic.com")


def _dep(model, conn, wire, prio, enabled=True, origin="admin"):
    return ModelDeployment(model, conn, wire, prio, enabled, origin)


SEED_DEPS = [
    _dep("sonnet-5-5", "acc3", "global.anthropic.claude-sonnet-5-5", 10, origin="bedrock_profile"),
    _dep("sonnet-5-5", "anthropic-direct", "claude-sonnet-5-5", 100, origin="seed"),
    _dep("gpt-6-luna", "acc3", "global.openai.gpt-6-luna", 10, origin="bedrock_profile"),
    _dep("sonnet", "acc3", "global.anthropic.claude-sonnet-4-6", 10, origin="legacy_constant"),
    _dep("haiku", "acc3", "global.anthropic.claude-haiku-4-5-20251001-v1:0", 10, origin="legacy_constant"),
    _dep("haiku", "acc2", "us.anthropic.claude-haiku-4-5-20251001-v1:0", 20, False, "legacy_constant"),
    _dep("cohere-embed-v4", "acc2", "global.cohere.embed-v4:0", 20, origin="bedrock_profile"),
]
SEED_ALIASES = [ModelAlias("haiku", 1, "haiku"), ModelAlias("sonnet", 1, "sonnet"), ModelAlias("sonnet", 2, "haiku")]


class World:
    """Registry + fake Bedrock / Anthropic / OpenAI, every provider call recorded as (api, where, model id)."""

    def __init__(self, monkeypatch):
        self.conns = {"acc3": BUILTIN_CONNECTIONS["acc3"], "acc2": BUILTIN_CONNECTIONS["acc2"],
                      "openai-direct": BUILTIN_CONNECTIONS["openai-direct"], "anthropic-direct": _anthropic(False),
                      "bedrock-llm-org": _org(enabled=False)}
        self.deps = list(SEED_DEPS)
        self.aliases = list(SEED_ALIASES)
        self.calls: list = []
        self.fail: dict = {}            # where -> exception to raise (provider outage), else answer
        self.anthropic_send = MagicMock(side_effect=self._anthropic)
        monkeypatch.setattr(connections, "run_sync", self._fetch)
        monkeypatch.setattr(catalog, "_models", dict(MODELS))
        monkeypatch.setattr(catalog, "_loaded_at", 10 ** 12)
        monkeypatch.setattr("shared.llm_client.pricing._catalog_rates", lambda m: None)
        monkeypatch.setattr("shared.llm_client.client.stream_sink.delta_callback", lambda stage: None)
        monkeypatch.setattr("shared.llm_client.client.log_gateway_billed_failure", MagicMock())
        monkeypatch.setattr(bs, "get_satellite_client", lambda service="bedrock-runtime", account="acc3":
                            self.runtime(account))
        monkeypatch.setattr(bs, "_get_satellite_session", self._session)
        monkeypatch.setattr(bs, "native_runtime", lambda conn, **kw: self.runtime(f"native:{conn.connection_key}"))
        from shared.llm_client.adapters import anthropic_messages
        monkeypatch.setattr(anthropic_messages, "send", self.anthropic_send)
        with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
            self.client = LLMClient()
        self.client._openai.chat.completions.create.side_effect = self._openai
        self.reset()

    def reset(self):
        connections.invalidate()
        deployments.invalidate()
        connections._connections = {}
        deployments._by_model = {}
        deployments._aliases = {}

    def _fetch(self, fn):
        if fn is connections._fetch_all:
            return dict(self.conns)
        if fn is deployments._fetch_aliases:
            return list(self.aliases)
        return list(self.deps)

    def _maybe_fail(self, where):
        exc = self.fail.get(where)
        if exc is not None:
            raise exc

    def runtime(self, where):
        rt = MagicMock()

        def converse(**kw):
            self.calls.append(("converse", where, kw["modelId"]))
            self._maybe_fail(where)
            if "toolConfig" in kw:
                return {"output": {"message": {"content": [{"toolUse": {"toolUseId": "t", "name": "verdict",
                                                                         "input": {"score": 3}}}]}},
                        "usage": {"inputTokens": 10, "outputTokens": 5}, "stopReason": "tool_use"}
            return {"output": {"message": {"content": [{"text": f"from {where}"}]}},
                    "usage": {"inputTokens": 10, "outputTokens": 5}, "stopReason": "end_turn"}

        def invoke(**kw):
            self.calls.append(("invoke_model", where, kw["modelId"]))
            self._maybe_fail(where)
            body = json.loads(kw["body"])
            if "output_dimension" in body or "input_type" in body:
                n = len(body["texts"])
                dim = body.get("output_dimension") or 3
                payload = {"embeddings": {"float": [[0.1] * dim for _ in range(n)]}}
            else:
                payload = {"content": [{"type": "text", "text": '{"score": 3}'}], "stop_reason": "end_turn",
                           "usage": {"input_tokens": 10, "output_tokens": 5}}
            return {"body": io.BytesIO(json.dumps(payload).encode()), "ResponseMetadata": {"HTTPHeaders": {}}}
        rt.converse.side_effect = converse
        rt.invoke_model.side_effect = invoke
        rt.bodies = []
        return rt

    def _session(self, account="acc3"):
        s = MagicMock()
        s.client.return_value = self.runtime(account)
        return s

    def _anthropic(self, **kw):
        self.calls.append(("anthropic", kw["connection"].connection_key, kw["wire_id"]))
        self._maybe_fail("anthropic-direct")
        r = MagicMock(text="from anthropic", input_tokens=10, output_tokens=5, cache_read_tokens=0,
                      cache_write_tokens=0, stop_reason="end_turn", stop_details=None, structured=False,
                      provider_model=kw["wire_id"], effort=None, request_id="r")
        return r

    def _openai(self, **kw):
        self.calls.append(("openai", "openai-direct", kw["model"]))
        raise RuntimeError("fake openai outage")

    def generate(self, **kw):
        base = dict(system_prompt="s", user_prompt="u")
        base.update(kw)
        return self.client.generate(LLMRequest(**base))


@pytest.fixture
def world(monkeypatch):
    w = World(monkeypatch)
    yield w
    w.reset()
    catalog.invalidate()
    vendor_route.circuit_open = vendor_route._never_open


def _route(monkeypatch, *keys):
    cfg = StageConfig(stage="adhoc_s3", role="writer", provider="claude", model_id=keys[0], account_route="acc3",
                      fallback_model_ids=tuple(keys[1:]))
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda stage: cfg)


# ── spread order ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_same_model_on_each_deployment_before_the_next_model(world, monkeypatch):
    world.conns["bedrock-llm-org"] = _org()
    world.deps.append(_dep("sonnet-5-5", "bedrock-llm-org", "us.anthropic.claude-sonnet-5-5", 20))
    world.reset()
    world.fail = {"acc3": RuntimeError("acc3 outage"), "bedrock-llm-org": RuntimeError("org outage")}
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    with pytest.raises(RuntimeError):
        world.generate(stage="adhoc_s3")
    assert world.calls == [("converse", "acc3", "global.anthropic.claude-sonnet-5-5"),
                           ("converse", "bedrock-llm-org", "us.anthropic.claude-sonnet-5-5"),
                           ("converse", "acc3", "global.openai.gpt-6-luna")]


def test_the_second_deployment_serves_when_the_first_is_down(world):
    world.conns["bedrock-llm-org"] = _org()
    world.deps.append(_dep("sonnet-5-5", "bedrock-llm-org", "us.anthropic.claude-sonnet-5-5", 20))
    world.reset()
    world.fail = {"acc3": RuntimeError("acc3 outage")}
    resp = world.generate(model_tier="sonnet-5-5")
    assert resp.content == "from bedrock-llm-org" and resp.satellite_account == "bedrock-llm-org"
    assert resp.model_used == "satellite-sonnet-5-5" and resp.provider == "bedrock-satellite"


def test_priority_decides_the_order(world):
    world.conns["bedrock-llm-org"] = _org()
    world.deps.append(_dep("sonnet-5-5", "bedrock-llm-org", "us.anthropic.claude-sonnet-5-5", 5))
    world.reset()
    assert world.generate(model_tier="sonnet-5-5").content == "from bedrock-llm-org"
    assert [c[1] for c in world.calls] == ["bedrock-llm-org"]


@pytest.mark.parametrize("switch_off", ["deployment", "connection"])
def test_a_disabled_deployment_or_connection_is_skipped(world, switch_off):
    world.conns["bedrock-llm-org"] = _org(enabled=switch_off != "connection")
    world.deps.append(_dep("sonnet-5-5", "bedrock-llm-org", "us.anthropic.claude-sonnet-5-5", 5,
                           enabled=switch_off != "deployment"))
    world.reset()
    assert world.generate(model_tier="sonnet-5-5").content == "from acc3"
    assert [c[1] for c in world.calls] == ["acc3"]


def test_an_open_circuit_is_skipped(world):
    world.conns["bedrock-llm-org"] = _org()
    world.deps.append(_dep("sonnet-5-5", "bedrock-llm-org", "us.anthropic.claude-sonnet-5-5", 20))
    world.reset()
    vendor_route.circuit_open = lambda conn, model: conn == "acc3"
    assert world.generate(model_tier="sonnet-5-5").content == "from bedrock-llm-org"
    assert [c[1] for c in world.calls] == ["bedrock-llm-org"]


def test_every_deployment_refused_at_account_level_is_an_access_denial(world):
    world.conns["bedrock-llm-org"] = _org()
    world.deps.append(_dep("sonnet-5-5", "bedrock-llm-org", "us.anthropic.claude-sonnet-5-5", 20))
    world.reset()
    denied = RuntimeError("AccessDeniedException: Error 002: Access to Bedrock models is not allowed for this account")
    world.fail = {"acc3": denied, "bedrock-llm-org": denied}
    with pytest.raises(LLMAccessDenied):
        world.generate(model_tier="sonnet-5-5")
    assert len(world.calls) == 2


def test_an_unusable_reply_is_the_models_answer_not_an_outage(world, monkeypatch):
    """A billed reply that fails its schema is raised at once: the same model on the next deployment is not asked."""
    world.conns["bedrock-llm-org"] = _org()
    world.deps.append(_dep("sonnet-5-5", "bedrock-llm-org", "us.anthropic.claude-sonnet-5-5", 20))
    world.reset()
    bad = {"output": {"message": {"content": [{"text": "no tool"}]}}, "usage": {"inputTokens": 9, "outputTokens": 1},
           "stopReason": "end_turn"}
    rt = MagicMock()
    rt.converse.return_value = bad
    monkeypatch.setattr(bs, "get_satellite_client", lambda service="bedrock-runtime", account="acc3": rt)
    with pytest.raises(BilledUnusableReply):
        world.generate(model_tier="sonnet-5-5", json_schema=SCHEMA)
    assert rt.converse.call_count == 2         # the AA-714 schema retry on acc3, then stop: org never asked


def test_a_task_role_deployment_runs_on_the_native_client(world):
    world.deps.append(_dep("gpt-6-luna", "acc2", "us.openai.gpt-6-luna", 5))
    world.reset()
    resp = world.generate(model_tier="gpt-6-luna")
    assert world.calls == [("converse", "native:acc2", "us.openai.gpt-6-luna")]
    assert resp.provider == "bedrock" and resp.model_used == "gpt-6-luna" and resp.satellite_account is None


# ── data scope until S5 ────────────────────────────────────────────────────────────────────────────────────────────

def test_a_vendor_api_row_is_never_a_fallback_behind_acc3(world):
    world.conns["anthropic-direct"] = _anthropic(enabled=True)
    world.reset()
    world.fail = {"acc3": RuntimeError("acc3 outage")}
    with pytest.raises(RuntimeError, match="acc3 outage"):
        world.generate(model_tier="sonnet-5-5")
    assert [c[0] for c in world.calls] == ["converse"]
    world.anthropic_send.assert_not_called()


def test_an_open_acc3_circuit_never_promotes_the_vendor_row(world):
    world.conns["anthropic-direct"] = _anthropic(enabled=True)
    world.reset()
    vendor_route.circuit_open = lambda conn, model: conn == "acc3"
    with pytest.raises(RuntimeError, match="no usable deployment"):
        world.generate(model_tier="sonnet-5-5")
    assert world.calls == []


def test_a_vendor_row_put_first_falls_back_to_aws(world):
    """S2's deliberate switch (an Anthropic row ahead of acc3) still works; when it fails the same model runs on acc3
    (content moves into AWS, never out)."""
    world.conns["anthropic-direct"] = _anthropic(enabled=True)
    world.deps = [d for d in world.deps if d.connection_key != "anthropic-direct"]
    world.deps.append(_dep("sonnet-5-5", "anthropic-direct", "claude-sonnet-5-5", 5))
    world.reset()
    assert world.generate(model_tier="sonnet-5-5").content == "from anthropic"
    world.fail = {"anthropic-direct": RuntimeError("429 rate_limit_error")}
    assert world.generate(model_tier="sonnet-5-5").content == "from acc3"


def test_an_openai_connection_never_serves_a_bedrock_catalog_row(world):
    world.deps.append(_dep("gpt-6-luna", "openai-direct", "gpt-6-luna", 1))
    world.reset()
    assert world.generate(model_tier="gpt-6-luna").content == "from acc3"
    assert [c[0] for c in world.calls] == ["converse"]


# ── legacy aliases ─────────────────────────────────────────────────────────────────────────────────────────────────

def _legacy_calls(world, monkeypatch, aliases: str, tier: str, schema=None, fail=None):
    monkeypatch.setenv("LLM_LEGACY_ALIASES", aliases)
    world.calls.clear()
    world.fail = fail or {}
    world.reset()
    try:
        r = world.generate(model_tier=tier, json_schema=schema)
        out = (r.content, r.model_used, r.provider, r.satellite_account, r.fallback_used, r.cost_usd,
               r.input_tokens, r.output_tokens, r.stop_reason)
    except Exception as e:  # noqa: BLE001
        out = (type(e).__name__, str(e))
    return list(world.calls), out


@pytest.mark.parametrize("tier", ["sonnet", "haiku"])
@pytest.mark.parametrize("schema", [None, SCHEMA])
@pytest.mark.parametrize("fail", [None, "acc3"])
def test_an_alias_makes_the_same_call_as_the_legacy_chain(world, monkeypatch, tier, schema, fail):
    """sonnet -> [sonnet, haiku], haiku -> [haiku] through the catalog + deployments send the very requests and return
    the very responses of _legacy_chain: success, structured (S251), and every step down (the chain's own error)."""
    failing = {"acc3": RuntimeError("acc3 outage")} if fail else None
    legacy = _legacy_calls(world, monkeypatch, "off", tier, schema, failing)
    with patch.object(LLMClient, "_legacy_chain", side_effect=AssertionError("legacy chain used")):
        alias = _legacy_calls(world, monkeypatch, "on", tier, schema, failing)
    assert alias == legacy
    assert legacy[0][0][0] == "invoke_model"
    assert legacy[0][0][2].endswith(":inference-profile/" + ("global.anthropic.claude-sonnet-4-6" if tier == "sonnet"
                                                             else "global.anthropic.claude-haiku-4-5-20251001-v1:0"))


def test_sonnet_degrades_to_haiku_like_the_chain(world, monkeypatch):
    calls = {}

    def invoke(prompt, model="sonnet", **kw):
        calls.setdefault("models", []).append((model, kw.get("profile_id")))
        if model == "sonnet":
            raise bs.BedrockUnavailable("sonnet down")
        return bs.BedrockInvokeResult(text="haiku says", model_used="haiku-4-5", latency_ms=1.0,
                                      usage={"input_tokens": 1, "output_tokens": 1}, stop_reason="end_turn")
    monkeypatch.setattr(bs, "invoke_claude", invoke)
    monkeypatch.setenv("LLM_LEGACY_ALIASES", "on")
    world.reset()
    resp = world.generate(model_tier="sonnet")
    assert resp.content == "haiku says" and resp.fallback_used is True and resp.satellite_account == "acc3"
    assert calls["models"] == [("sonnet", "global.anthropic.claude-sonnet-4-6"),
                               ("haiku", "global.anthropic.claude-haiku-4-5-20251001-v1:0")]


def test_an_alias_follows_the_registry(world, monkeypatch):
    """What the alias adds over the chain: the deployments decide (sonnet@acc3 switched off -> straight to Haiku)."""
    monkeypatch.setenv("LLM_LEGACY_ALIASES", "on")
    world.deps = [d for d in world.deps if not (d.model_key == "sonnet" and d.connection_key == "acc3")]
    world.deps.append(_dep("sonnet", "acc3", "global.anthropic.claude-sonnet-4-6", 10, enabled=False))
    world.reset()
    resp = world.generate(model_tier="sonnet")
    assert resp.fallback_used is True and resp.model_used == "satellite-haiku-4-5"
    assert [c[2].rsplit("/", 1)[-1] for c in world.calls] == ["global.anthropic.claude-haiku-4-5-20251001-v1:0"]


@pytest.mark.parametrize("state", ["no_alias_rows", "no_registry", "acc2_native_on", "target_not_in_catalog"])
def test_the_legacy_chain_is_used_whenever_the_alias_cannot_be_resolved(world, monkeypatch, state):
    monkeypatch.setenv("LLM_LEGACY_ALIASES", "on")
    if state == "no_alias_rows":
        world.aliases = []
    elif state == "no_registry":
        world.conns = {}
    elif state == "acc2_native_on":
        monkeypatch.setenv("LLM_LEGACY_ACC2_NATIVE", "on")
    else:
        world.aliases = [ModelAlias("haiku", 1, "haiku-gone")]
    world.reset()
    with patch.object(LLMClient, "_legacy_chain", return_value=MagicMock(content="chain")) as chain:
        assert world.generate(model_tier="haiku").content == "chain"
    chain.assert_called_once()


# ── rollback switches ──────────────────────────────────────────────────────────────────────────────────────────────

def test_aliases_are_off_by_default_and_the_switch_restores_the_chain(world, monkeypatch):
    monkeypatch.delenv("LLM_LEGACY_ALIASES", raising=False)
    with patch.object(LLMClient, "_run_alias") as run_alias:
        world.generate(model_tier="haiku")
    run_alias.assert_not_called()
    monkeypatch.setenv("LLM_LEGACY_ALIASES", "off")
    with patch.object(LLMClient, "_run_alias") as run_alias:
        world.generate(model_tier="haiku")
    run_alias.assert_not_called()


def test_routing_is_on_by_default_and_the_switch_restores_the_s2_path(world, monkeypatch):
    world.conns["bedrock-llm-org"] = _org()
    world.deps.append(_dep("sonnet-5-5", "bedrock-llm-org", "us.anthropic.claude-sonnet-5-5", 5))
    world.reset()
    monkeypatch.delenv("LLM_DEPLOYMENT_ROUTING", raising=False)
    assert world.generate(model_tier="sonnet-5-5").content == "from bedrock-llm-org"
    monkeypatch.setenv("LLM_DEPLOYMENT_ROUTING", "off")
    with patch.object(vendor_route, "deployment_plan", side_effect=AssertionError("plan read")):
        assert world.generate(model_tier="sonnet-5-5").content == "from acc3"   # bedrock_profile_ids[acc3], as S2


# ── embeddings ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _embed_cfg(monkeypatch, *keys):
    cfg = StageConfig(stage="f10_embed", role="embed", provider="bedrock", model_id=keys[0], account_route=None,
                      fallback_model_ids=tuple(keys[1:]))
    monkeypatch.setattr(embed_mod, "get_stage_config_sync", lambda stage: cfg)


def test_embed_keeps_dimension_and_input_type_through_the_registry(world, monkeypatch):
    _embed_cfg(monkeypatch, "cohere-embed-v4")
    bodies = []
    rt = MagicMock()

    def invoke(**kw):
        bodies.append((kw["modelId"], json.loads(kw["body"])))
        return {"body": io.BytesIO(json.dumps({"embeddings": {"float": [[0.5] * 256]}}).encode()),
                "ResponseMetadata": {"HTTPHeaders": {"x-amzn-bedrock-input-token-count": "7"}}}
    rt.invoke_model.side_effect = invoke
    monkeypatch.setattr(embed_mod, "_runtime_for", lambda key: rt if key == "acc2" else pytest.fail(key))
    resp = embed_mod.embed("f10_embed", ["hello"], input_type="search_query", dimensions=256)
    assert bodies == [("global.cohere.embed-v4:0", {"texts": ["hello"], "input_type": "search_query",
                                                    "embedding_types": ["float"], "output_dimension": 256})]
    assert len(resp.vectors[0]) == 256 and resp.account == "acc2" and resp.input_tokens == 7


def test_embed_spreads_over_deployments_and_uses_the_connection_client(world, monkeypatch):
    world.deps.append(_dep("cohere-embed-v4", "acc3", "global.cohere.embed-v4:0", 30))
    world.reset()
    _embed_cfg(monkeypatch, "cohere-embed-v4")
    clients = {}

    def runtime_for(key):
        conn = connections.connection_or_builtin(key)
        clients[key] = conn.auth_mode
        return world.runtime(key)
    monkeypatch.setattr(embed_mod, "_runtime_for", runtime_for)
    world.fail = {"acc2": RuntimeError("ThrottlingException")}
    resp = embed_mod.embed("f10_embed", ["a", "b"], dimensions=8)
    assert [c[1] for c in world.calls] == ["acc2", "acc3"]
    assert clients == {"acc2": "task_role", "acc3": "assume_role"}
    assert resp.account == "acc3" and len(resp.vectors) == 2 and len(resp.vectors[0]) == 8
    assert resp.fallback_used is False          # same model, another deployment: not a route fallback


def test_embed_runtime_for_comes_from_the_connection(monkeypatch):
    monkeypatch.setattr(bs, "_native_runtimes", {})
    sentinel = object()
    with patch.object(bs, "get_satellite_client", return_value=sentinel) as gsc:
        assert embed_mod._runtime_for("acc3") is sentinel
    gsc.assert_called_once_with("bedrock-runtime", account="acc3")
    with pytest.raises(RuntimeError, match="not a Bedrock connection"):
        embed_mod._runtime_for("openai-direct")


# ── satellite sessions from the registry ───────────────────────────────────────────────────────────────────────────

def _creds():
    from datetime import datetime, timedelta, timezone
    return {"Credentials": {"AccessKeyId": "a", "SecretAccessKey": "s", "SessionToken": "t",
                            "Expiration": datetime.now(timezone.utc) + timedelta(hours=1)}}


def test_the_session_cache_key_is_connection_role_external_id_region(monkeypatch):
    monkeypatch.setattr(bs, "_cached_sessions", {})
    monkeypatch.setattr(bs, "_cached_session_expiry", {})
    state = {"org": _org()}
    monkeypatch.setattr(connections, "run_sync", lambda fn: {"acc3": BUILTIN_CONNECTIONS["acc3"], **state}
                        if fn is connections._fetch_all else [])
    connections.invalidate()
    sts = MagicMock()
    sts.assume_role.side_effect = lambda **kw: _creds()
    with patch.object(bs.boto3, "client", return_value=sts), patch.object(bs.boto3, "Session"):
        bs._get_satellite_session("acc3")
        bs._get_satellite_session("acc3")                     # cached
        bs._get_satellite_session("org")
        state["org"] = _org(connection_key="org", external_id="rotated")
        connections.invalidate()
        bs._get_satellite_session("org")                      # new ExternalId -> new session
    calls = [(c.kwargs["RoleArn"], c.kwargs["ExternalId"]) for c in sts.assume_role.call_args_list]
    assert calls == [("arn:aws:iam::acc3-test-account:role/AA3-Bedrock-Invoker", "aa296-satellite-bedrock-acc3"),
                     ("arn:aws:iam::111122223333:role/AA-LLM-Invoker", "aa-llm-org-ext"),
                     ("arn:aws:iam::111122223333:role/AA-LLM-Invoker", "rotated")]
    assert len(bs._cached_sessions) == 3
    assert {k[0] for k in bs._cached_sessions} == {"acc3", "bedrock-llm-org", "org"}
    connections.invalidate()


@pytest.mark.parametrize("account", ["acc1", "acc2", "openai-direct", "", None])
def test_only_an_assume_role_bedrock_connection_is_a_satellite(account):
    with patch.object(bs.boto3, "client") as m_client:
        with pytest.raises(ValueError, match="Unknown satellite account"):
            bs.get_satellite_client("bedrock-runtime", account=account)
    m_client.assert_not_called()


def test_a_broken_registry_row_falls_back_to_the_builtin_mirror(monkeypatch):
    broken = ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="assume_role", label="x",
                                data_scope="aws_only", enabled=True)          # no role, ExternalId, region, account
    monkeypatch.setattr(connections, "run_sync", lambda fn: {"acc3": broken} if fn is connections._fetch_all else [])
    connections.invalidate()
    assert connections.connection_or_builtin("acc3") is BUILTIN_CONNECTIONS["acc3"]
    assert bs.inference_profile_arn("acc3", "p") == "arn:aws:bedrock:us-west-1:acc3-test-account:inference-profile/p"
    connections.invalidate()


def test_the_builtin_mirror_is_the_304_seed():
    """BUILTIN_CONNECTIONS only stands in for the registry: it must say exactly what migration 304 seeded."""
    sql = (Path(__file__).resolve().parents[2] / "api" / "migrations" / "304_llm_connection_deployment.sql").read_text()
    for key, conn in BUILTIN_CONNECTIONS.items():
        row = re.search(rf"\('{re.escape(key)}', '(\w+)', '(\w+)', '([^']*)', NULL, (NULL|'\w+'),\s+(NULL|'[^']*'), "
                        r"(NULL|'[^']*'), (NULL|'[^']*'), NULL, (NULL|'\w+'), (NULL|'[^']*'), '(\w+)', (true|false)",
                        sql)
        assert row, key
        kind, auth, label, acct_env, region, role, ext, cred_env, base_url, scope, enabled = row.groups()
        unq = (lambda v: None if v == "NULL" else v.strip("'"))
        assert (conn.kind, conn.auth_mode, conn.label, conn.data_scope, conn.enabled) == \
               (kind, auth, label, scope, enabled == "true")
        assert (conn.account_id_env, conn.region, conn.role_name, conn.external_id, conn.credential_env,
                conn.base_url) == tuple(unq(v) for v in (acct_env, region, role, ext, cred_env, base_url))
        assert conn.policy_problems() == []


def test_serving_accounts_come_from_the_registry(monkeypatch):
    org = _org(connection_key="acc9")
    monkeypatch.setattr(connections, "run_sync", lambda fn: {**BUILTIN_CONNECTIONS, "acc9": org}
                        if fn is connections._fetch_all else [])
    connections.invalidate()
    assert catalog.serving_accounts() == ("acc3", "acc9", "acc2")
    m = CatalogModel(model_key="x", label="x", vendor="v", provider="bedrock", api_style="converse",
                     bedrock_profile_ids={"acc9": "p", "acc1": "q"})
    assert m.account_for(None) == "acc9" and m.account_for("acc1") == "acc9"
    connections.invalidate()
