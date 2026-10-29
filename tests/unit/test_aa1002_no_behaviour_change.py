"""AA-1002 (gateway slice S1) — the connection registry is off the routing path: no behaviour change.

Two proofs. Static: no module on a call path (generate / embed / decide / judge / shadow / batch / call log) imports
``connections`` or ``deployments``. Runtime: a Converse call and a legacy-chain call give byte-identical provider
requests and responses whether the registry is empty or holds a contradictory picture (acc3 disabled, other wire ids),
and the registry is never consulted.
"""
import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import catalog, connections, deployments
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
    assert on_disk == set(CALL_PATH) | {"connections.py", "deployments.py"}, \
        "new gateway module: add it to CALL_PATH (or move the registry check)"


# ── runtime ───────────────────────────────────────────────────────────────────────────────────────────────────────

SONNET5 = CatalogModel(model_key="sonnet-5", label="Sonnet 5", vendor="anthropic", provider="bedrock",
                       api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5"},
                       callable_via=("llm_client",), supports_temperature=False, price_in_per_mtok=2.0,
                       price_out_per_mtok=10.0, enabled=True)

CONTRADICTORY_CONNECTIONS = {
    "acc3": ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="assume_role", label="x",
                               data_scope="aws_only", enabled=False),
    "anthropic-direct": ProviderConnection(connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key",
                                           label="x", data_scope="vendor_direct", enabled=True,
                                           secret_name="aa-llm/dev/anthropic-direct",
                                           base_url="https://api.anthropic.com"),
}
CONTRADICTORY_DEPLOYMENTS = [
    ModelDeployment("sonnet-5", "anthropic-direct", "claude-sonnet-5", 0, True, "admin"),
    ModelDeployment("sonnet-5", "acc3", "global.anthropic.something-else", 10, True, "admin"),
    ModelDeployment("haiku", "anthropic-direct", "claude-haiku-4-5", 0, True, "admin"),
]


def _converse_response():
    return {"output": {"message": {"role": "assistant", "content": [{"text": "hello"}]}},
            "usage": {"inputTokens": 1000, "outputTokens": 500}, "stopReason": "end_turn"}


def _invoke_response():
    body = MagicMock()
    body.read.return_value = (b'{"content": [{"type": "text", "text": "legacy"}], "stop_reason": "end_turn", '
                              b'"usage": {"input_tokens": 10, "output_tokens": 5}}')
    return {"body": body}


def _run(registry: str):
    """One Converse call (catalog key) and one legacy-chain call (haiku), with the registry empty or contradictory.
    Returns what reached the provider and what the caller got back."""
    connections.invalidate()
    deployments.invalidate()
    if registry == "empty":
        fetch = MagicMock(side_effect=OSError("relation shared.llm_provider_connection does not exist"))
    else:
        fetch = MagicMock(side_effect=lambda fn: CONTRADICTORY_CONNECTIONS if fn is connections._fetch_all
                          else CONTRADICTORY_DEPLOYMENTS)
    rt = MagicMock()
    rt.converse.return_value = _converse_response()
    rt.invoke_model.return_value = _invoke_response()
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        client = LLMClient()
    with patch.object(connections, "run_sync", fetch), \
            patch.object(connections, "_fetch_secret") as secret, \
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
    assert fetch.call_count == 0, "the gateway consulted the connection registry"
    secret.assert_not_called()
    seen = {
        "converse_kwargs": rt.converse.call_args.kwargs,
        "invoke_kwargs": rt.invoke_model.call_args.kwargs if rt.invoke_model.called else None,
        "satellite_calls": gsc.call_args_list,
        "session_calls": session.call_args_list,
    }
    out = [(r.content, r.model_used, r.provider, r.satellite_account, r.fallback_used, r.cost_usd, r.stop_reason,
            r.input_tokens, r.output_tokens) for r in (converse, legacy)]
    return seen, out


def test_calls_are_byte_identical_whatever_the_registry_holds():
    catalog.invalidate()
    empty = _run("empty")
    contradictory = _run("contradictory")
    assert repr(empty) == repr(contradictory)
    seen, out = empty
    assert seen["converse_kwargs"]["modelId"] == "global.anthropic.claude-sonnet-5"
    assert seen["invoke_kwargs"] is not None, "the legacy haiku call did not reach invoke_model"
    assert out[0][0] == "hello" and out[1][0] == "legacy"
    assert out[0][3] == "acc3" and out[1][3] == "acc3"
