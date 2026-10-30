"""AA-1002 (gateway slice S9) — Azure OpenAI / OpenAI-compatible connections and the OpenAI key as a runtime secret.

- LLMClient builds no OpenAI client in __init__ any more; adapters/openai_chat.client_for builds one per connection at
  call time: kind openai -> openai.OpenAI(base_url), kind azure_openai -> openai.AzureOpenAI(azure_endpoint,
  api_version), per-connection timeout / retries (defaults 300 s / 1, never the SDK's 600 s / 2).
- The key comes from the connection's aa-llm/ secret when it names one, else its allowlisted credential env (today's
  OPENAI_API_KEY of openai-direct: a fallback only); fail closed before any network call; a 401 drops it.
- An Azure deployment of an OpenAI model is routed (vendor_route) and its reply is labelled azure-openai-<key>, so
  llm_call_log records provider azure_openai with the clean model key and the connection as account.
Never a network call: the SDK classes are fakes.
"""
import dataclasses
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import openai
import pytest

from shared.llm_client import catalog, connections, data_scope
from shared.llm_client.adapters import openai_chat
from shared.llm_client.call_log import _normalize_model_provider
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient, openai_scope_allowed
from shared.llm_client.connections import BUILTIN_CONNECTIONS, ProviderConnection
from shared.llm_client.deployments import ModelDeployment
from shared.llm_client.models import LLMRequest
from shared.llm_client.vendor_route import VendorDeployment, _callable

LUNA = CatalogModel(model_key="gpt-6-luna-openai", label="GPT-6 Luna (OpenAI)", vendor="openai",
                    provider="openai", api_style="openai_chat", bedrock_profile_ids={}, wire_model="gpt-6-luna",
                    callable_via=("llm_client",), supports_temperature=False, price_in_per_mtok=0.1,
                    price_out_per_mtok=0.5, price_cache_read_per_mtok=0.01, enabled=True)
SONNET = CatalogModel(model_key="sonnet-5-5", label="Sonnet 5.5", vendor="anthropic", provider="bedrock",
                      api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5-5"},
                      callable_via=("llm_client",), price_in_per_mtok=2.0, price_out_per_mtok=10.0, enabled=True)


def _azure(**kw):
    base = dict(connection_key="azure-luna", kind="azure_openai", auth_mode="api_key", label="Azure OpenAI",
                data_scope="vendor_direct", enabled=True, secret_name="aa-llm/dev/azure-luna",
                base_url="https://aa-dev.openai.azure.com", timeout_s=45, max_retries=0)
    base.update(kw)
    return ProviderConnection(**base)


class FakeSDK:
    """Stands in for openai.OpenAI / openai.AzureOpenAI: records constructor kwargs, answers one completion."""
    built: list = []

    def __init__(self, **kw):
        self.kw = kw
        FakeSDK.built.append((type(self).__name__, kw))
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=MagicMock(side_effect=self._create)))
        self.error = None

    def _create(self, **kw):
        if self.error is not None:
            raise self.error
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, prompt_tokens_details={"cached_tokens": 40})
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"score": 3}'), finish_reason="stop")])


class FakeOpenAI(FakeSDK):
    pass


class FakeAzure(FakeSDK):
    pass


@pytest.fixture(autouse=True)
def sdk(monkeypatch):
    FakeSDK.built = []
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(openai, "AzureOpenAI", FakeAzure)
    openai_chat._clients.clear()
    connections._credentials.clear()
    reader = MagicMock(return_value=json.dumps({"api_key": "az-test-key"}))
    monkeypatch.setattr(connections, "_fetch_secret", reader)
    yield reader
    openai_chat._clients.clear()
    connections._credentials.clear()


# ── the client factory ────────────────────────────────────────────────────────────────────────────────────────────

def test_llmclient_builds_no_openai_client_at_init(monkeypatch):
    built = MagicMock()
    monkeypatch.setattr(openai, "OpenAI", built)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with patch("shared.llm_client.client.boto3"):
        c = LLMClient()
    assert c._openai is None
    built.assert_not_called()                     # no key needed to build the gateway


def test_the_openai_connection_takes_the_env_key_only_as_a_fallback(sdk, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-test-key")
    client = openai_chat.client_for(BUILTIN_CONNECTIONS["openai-direct"])
    name, kw = FakeSDK.built[-1]
    assert name == "FakeOpenAI" and kw["api_key"] == "env-test-key" and kw["base_url"] == "https://api.openai.com/v1"
    assert kw["max_retries"] == openai_chat.DEFAULT_MAX_RETRIES and kw["timeout"].read == 300.0
    sdk.assert_not_called()                       # no secret named: the env fallback
    assert openai_chat.client_for(BUILTIN_CONNECTIONS["openai-direct"]) is client     # cached
    secret_row = dataclasses.replace(BUILTIN_CONNECTIONS["openai-direct"], secret_name="aa-llm/dev/openai-direct")
    openai_chat.client_for(secret_row)
    assert FakeSDK.built[-1][1]["api_key"] == "az-test-key"                          # the secret wins
    sdk.assert_called_once_with("aa-llm/dev/openai-direct")


def test_a_rotated_key_builds_a_new_client_and_forget_drops_it(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-one")
    first = openai_chat.client_for(BUILTIN_CONNECTIONS["openai-direct"])
    monkeypatch.setenv("OPENAI_API_KEY", "env-two")
    second = openai_chat.client_for(BUILTIN_CONNECTIONS["openai-direct"])
    assert first is not second and len(openai_chat._clients) == 1
    openai_chat.forget_connection("openai-direct")
    assert openai_chat._clients == {}


def test_the_azure_connection_uses_its_endpoint_api_version_and_limits(sdk):
    openai_chat.client_for(_azure())
    name, kw = FakeSDK.built[-1]
    assert name == "FakeAzure" and kw["azure_endpoint"] == "https://aa-dev.openai.azure.com"
    assert kw["api_version"] == openai_chat.AZURE_API_VERSION and kw["api_key"] == "az-test-key"
    assert kw["max_retries"] == 0 and kw["timeout"].read == 45.0
    openai_chat.client_for(_azure(), {"api_version": "2025-04-01-preview"})
    assert FakeSDK.built[-1][1]["api_version"] == "2025-04-01-preview"
    assert openai_chat.api_version_of({"api_version": "latest; rm -rf"}) == openai_chat.AZURE_API_VERSION


@pytest.mark.parametrize("conn, error", [
    (_azure(enabled=False), connections.ConnectionCredentialError),                      # switched off
    (_azure(base_url="https://evil.example.com"), connections.ConnectionCredentialError),  # host not on the list
    (_azure(secret_name="aa-cis/dev/rds"), connections.ConnectionCredentialError),        # another app's secret
    (_azure(base_url=None), connections.ConnectionCredentialError),                       # Azure needs its endpoint
    (_azure(credential_env="OPENAI_API_KEY", secret_name=None), connections.ConnectionCredentialError),  # env of openai
])
def test_refusals_happen_before_any_request(sdk, conn, error):
    with pytest.raises(error):
        openai_chat.client_for(conn)
    assert FakeSDK.built == []
    if conn.base_url is not None:
        sdk.assert_not_called()                  # the policy is checked before the secret is read


def test_no_openai_key_anywhere_fails_closed(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(connections.ConnectionCredentialError, match="empty"):
        openai_chat.client_for(BUILTIN_CONNECTIONS["openai-direct"])


# ── routing and the call ──────────────────────────────────────────────────────────────────────────────────────────

def test_an_azure_connection_serves_openai_rows_only():
    assert _callable(LUNA, _azure()) is None
    assert "OpenAI-model" in _callable(dataclasses.replace(SONNET, api_style="openai_chat"), _azure())
    assert "no azure_openai adapter" in _callable(dataclasses.replace(LUNA, api_style="converse"), _azure())


def test_the_stage_scope_reads_the_called_connection():
    aws_only_azure = _azure(data_scope="aws_only")
    assert openai_scope_allowed((data_scope.AWS_ONLY,), aws_only_azure)
    assert not openai_scope_allowed((data_scope.AWS_ONLY,), _azure())
    assert openai_scope_allowed(None, _azure())


@pytest.fixture
def luna(monkeypatch):
    monkeypatch.setattr(catalog, "_models", {LUNA.model_key: LUNA})
    monkeypatch.setattr(catalog, "_loaded_at", 10 ** 12)
    monkeypatch.setattr("shared.llm_client.client.stream_sink.delta_callback", lambda stage: None)
    monkeypatch.setattr("shared.llm_client.pricing.deployment_for", lambda model, conn: None)
    yield
    catalog.invalidate()


def _client():
    with patch("shared.llm_client.client.boto3"):
        return LLMClient()


def _target(conn):
    dep = ModelDeployment("gpt-6-luna-openai", conn.connection_key, "luna-deploy", 10, True, "admin")
    return VendorDeployment(dep, conn)


def test_an_azure_deployment_is_called_on_its_client_and_labelled(luna):
    schema = {"type": "object", "properties": {"score": {"type": "integer"}}, "required": ["score"]}
    req = LLMRequest(system_prompt="s", user_prompt="u", json_schema={"name": "v", "schema": schema})
    resp = _client()._dispatch_target(req, LUNA, _target(_azure()))
    name, kw = FakeSDK.built[-1]
    assert name == "FakeAzure"
    assert resp.model_used == "azure-openai-gpt-6-luna-openai" and resp.provider == "azure_openai"
    assert resp.connection_key == "azure-luna" and resp.cache_read_tokens == 40 and json.loads(resp.content) == {
        "score": 3}
    assert _normalize_model_provider(resp.model_used, None) == ("gpt-6-luna-openai", "azure_openai")
    expected = (60 * 0.1 + 20 * 0.5 + 40 * 0.01) / 1_000_000
    assert resp.cost_usd == round(expected, 6)   # priced on the catalog key (cache reads at the cache rate), 6 dp


def test_the_openai_direct_path_keeps_its_label_and_connection(luna, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-test-key")
    resp = _client()._call_openai(LLMRequest(system_prompt="s", user_prompt="u"), model="gpt-6-luna",
                                  catalog_model=LUNA)
    assert FakeSDK.built[-1][0] == "FakeOpenAI"
    assert resp.model_used == "gpt-6-luna-openai" and resp.provider == "openai"
    assert resp.connection_key == "openai-direct"


def test_a_401_drops_the_cached_key_and_client(luna, monkeypatch):
    class Unauthorized(Exception):
        status_code = 401

    forget = MagicMock()
    monkeypatch.setattr(openai_chat, "forget_connection", forget)
    client = openai_chat.client_for(_azure())
    client.error = Unauthorized("invalid key")
    with pytest.raises(Unauthorized):
        _client()._dispatch_target(LLMRequest(system_prompt="s", user_prompt="u"), LUNA, _target(_azure()))
    forget.assert_called_once_with("azure-luna")


def test_an_injected_client_is_used_for_every_call(luna):
    c = _client()
    c._openai = MagicMock()
    c._openai.chat.completions.create.return_value = FakeSDK()._create()
    c._dispatch_target(LLMRequest(system_prompt="s", user_prompt="u"), LUNA, _target(_azure()))
    assert c._openai.chat.completions.create.called and FakeSDK.built[-1][0] == "FakeSDK"   # no factory call


def test_a_credential_problem_is_raised_before_the_provider_is_reached(luna, sdk):
    sdk.side_effect = PermissionError("AccessDenied")
    with pytest.raises(connections.ConnectionCredentialError, match="secret unreadable"):
        _client()._dispatch_target(LLMRequest(system_prompt="s", user_prompt="u"), LUNA, _target(_azure()))
    assert FakeSDK.built == []
