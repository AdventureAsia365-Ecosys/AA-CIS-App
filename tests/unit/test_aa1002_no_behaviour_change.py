"""AA-1002 (gateway slices S1 + S2) — the registry changes nothing unless an Anthropic API deployment is first.

S1 kept the connection registry off the routing path. S2 (the Anthropic Messages adapter) reads it through ONE module,
``vendor_route.py``, and only an enabled deployment on an enabled ``anthropic`` connection that sorts first by
priority changes where a call goes. Two proofs:

- Static: no call-path module imports ``connections`` / ``deployments``; only ``client.py`` imports ``vendor_route``.
- Runtime: a Converse call and a legacy-chain call give byte-identical provider requests and responses for every
  registry state that has no such first row (table missing, Anthropic row or connection disabled, Anthropic row
  behind acc3, an Anthropic row on a legacy key, a contradictory acc3 row), and the Anthropic adapter and the secret
  are never touched. The positive cases are in test_aa1002_s2_vendor_route.py.
"""
import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import catalog, connections, deployments
from shared.llm_client.adapters import anthropic_messages
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.connections import ProviderConnection
from shared.llm_client.deployments import ModelDeployment
from shared.llm_client.models import LLMRequest

LLM_CLIENT = Path(__file__).resolve().parents[2] / "shared" / "llm_client"
REGISTRY = {"connections", "deployments"}
CALL_PATH = ("client.py", "embed.py", "decide.py", "judge.py", "shadow.py", "bedrock_satellite.py",
             "bedrock_batch.py", "call_log.py", "catalog.py", "role_config.py", "pricing.py", "errors.py",
             "prompt_cache.py", "stream_sink.py", "column_mapper.py", "schema_check.py", "models.py")


def _imported_modules(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name.rsplit(".", 1)[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").rsplit(".", 1)[-1])
            names.update(a.name for a in node.names)
    return names


@pytest.mark.parametrize("name", CALL_PATH)
def test_call_path_modules_do_not_import_the_registry(name):
    assert not (_imported_modules(LLM_CLIENT / name) & REGISTRY), f"{name} reads the registry before slice S3"


def test_every_gateway_module_is_classified():
    on_disk = {p.name for p in LLM_CLIENT.glob("*.py")} - {"__init__.py"}
    assert on_disk == set(CALL_PATH) | {"connections.py", "deployments.py", "vendor_route.py"}, \
        "new gateway module: add it to CALL_PATH (or move the registry check)"


@pytest.mark.parametrize("name", [n for n in CALL_PATH if n != "client.py"])
def test_only_the_client_asks_the_vendor_route(name):
    assert "vendor_route" not in _imported_modules(LLM_CLIENT / name)


# ── runtime ───────────────────────────────────────────────────────────────────────────────────────────────────────

SONNET5 = CatalogModel(model_key="sonnet-5", label="Sonnet 5", vendor="anthropic", provider="bedrock",
                       api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5"},
                       callable_via=("llm_client",), supports_temperature=False, price_in_per_mtok=2.0,
                       price_out_per_mtok=10.0, enabled=True)

def _anthropic(enabled=True):
    return ProviderConnection(connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key", label="x",
                              data_scope="vendor_direct", enabled=enabled, secret_name="aa-llm/dev/anthropic-direct",
                              base_url="https://api.anthropic.com")


def _acc3(enabled=True):
    return ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="assume_role", label="x",
                              data_scope="aws_only", enabled=enabled)


ACC3_DEP = ModelDeployment("sonnet-5", "acc3", "global.anthropic.claude-sonnet-5", 10, True, "bedrock_profile")

# Registry states that must leave every call on today's path: (connections, deployments).
NO_ROUTE_STATES = {
    "anthropic_deployment_disabled": (
        {"acc3": _acc3(), "anthropic-direct": _anthropic()},
        [ACC3_DEP, ModelDeployment("sonnet-5", "anthropic-direct", "claude-sonnet-5", 0, False, "admin")]),
    "anthropic_connection_disabled": (
        {"acc3": _acc3(), "anthropic-direct": _anthropic(enabled=False)},
        [ACC3_DEP, ModelDeployment("sonnet-5", "anthropic-direct", "claude-sonnet-5", 0, True, "admin")]),
    "anthropic_behind_acc3": (
        {"acc3": _acc3(), "anthropic-direct": _anthropic()},
        [ACC3_DEP, ModelDeployment("sonnet-5", "anthropic-direct", "claude-sonnet-5", 100, True, "seed")]),
    "anthropic_on_a_legacy_key": (
        {"acc3": _acc3(), "anthropic-direct": _anthropic()},
        [ACC3_DEP, ModelDeployment("haiku", "anthropic-direct", "claude-haiku-4-5", 0, True, "admin")]),
    "contradictory_acc3": (
        {"acc3": _acc3(enabled=False), "anthropic-direct": _anthropic(enabled=False)},
        [ModelDeployment("sonnet-5", "acc3", "global.anthropic.something-else", 10, True, "admin"),
         ModelDeployment("sonnet-5", "anthropic-direct", "claude-sonnet-5", 0, True, "admin")]),
}


def _converse_response():
    return {"output": {"message": {"role": "assistant", "content": [{"text": "hello"}]}},
            "usage": {"inputTokens": 1000, "outputTokens": 500}, "stopReason": "end_turn"}


def _invoke_response():
    body = MagicMock()
    body.read.return_value = (b'{"content": [{"type": "text", "text": "legacy"}], "stop_reason": "end_turn", '
                              b'"usage": {"input_tokens": 10, "output_tokens": 5}}')
    return {"body": body}


def _run(registry: str):
    """One Converse call (catalog key) and one legacy-chain call (haiku) under a registry state.
    Returns what reached the provider and what the caller got back."""
    connections.invalidate()
    deployments.invalidate()
    connections._connections = {}          # a failed load keeps the last good registry: start from nothing
    deployments._by_model = {}
    if registry == "missing_table":
        fetch = MagicMock(side_effect=OSError("relation shared.llm_provider_connection does not exist"))
    else:
        conns, deps = NO_ROUTE_STATES[registry]
        fetch = MagicMock(side_effect=lambda fn: conns if fn is connections._fetch_all else deps)
    rt = MagicMock()
    rt.converse.return_value = _converse_response()
    rt.invoke_model.return_value = _invoke_response()
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        client = LLMClient()
    with patch.object(connections, "run_sync", fetch), \
            patch.object(connections, "_fetch_secret") as secret, \
            patch.object(anthropic_messages, "send") as send, \
            patch("shared.llm_client.client.get_model_sync", return_value=SONNET5), \
            patch("shared.llm_client.client.get_stage_config_sync", return_value=None), \
            patch("shared.llm_client.pricing._catalog_rates",
                  return_value={"in": 0.002, "out": 0.01, "cache_read": 0, "cache_write": 0}), \
            patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt) as gsc, \
            patch("shared.llm_client.bedrock_satellite._get_satellite_session") as session, \
            patch("shared.llm_client.client.stream_sink.delta_callback", return_value=None):
        session.return_value.client.return_value = rt
        converse = client.generate(LLMRequest(system_prompt="s", user_prompt="u", model_tier="sonnet-5"))
        legacy = client.generate(LLMRequest(system_prompt="s", user_prompt="u", model_tier="haiku"))
    send.assert_not_called()
    secret.assert_not_called()
    seen = {
        "converse_kwargs": rt.converse.call_args.kwargs,
        "invoke_kwargs": rt.invoke_model.call_args.kwargs if rt.invoke_model.called else None,
        "satellite_calls": gsc.call_args_list,
        "session_calls": session.call_args_list,
    }
    out = [(r.content, r.model_used, r.provider, r.satellite_account, r.fallback_used, r.cost_usd, r.stop_reason,
            r.input_tokens, r.output_tokens, r.provider_model) for r in (converse, legacy)]
    return seen, out


@pytest.mark.parametrize("registry", sorted(NO_ROUTE_STATES))
def test_calls_are_byte_identical_whatever_the_registry_holds(registry):
    catalog.invalidate()
    baseline = _run("missing_table")
    assert repr(_run(registry)) == repr(baseline)
    seen, out = baseline
    assert seen["converse_kwargs"]["modelId"] == "global.anthropic.claude-sonnet-5"
    assert seen["invoke_kwargs"] is not None, "the legacy haiku call did not reach invoke_model"
    assert out[0][0] == "hello" and out[1][0] == "legacy"
    assert out[0][3] == "acc3" and out[1][3] == "acc3"
    assert out[0][9] is None and out[0][2] == "bedrock-satellite"
