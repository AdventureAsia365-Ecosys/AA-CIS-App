"""AA-1002 (gateway slices S1 + S2 + S3) — the registry changes nothing unless it says something new.

S1 kept the connection registry off the routing path. S2 (the Anthropic Messages adapter) read it through ONE module,
``vendor_route.py``. S3 routes through it (deployment spread, legacy aliases, connection-based satellite sessions and
embeddings), so the static proof is now an allowlist of the modules that may read it. The runtime proofs:

- a Converse call and a legacy-chain call give byte-identical provider requests and responses for every registry state
  that adds no new usable deployment (table missing, Anthropic row or connection disabled, Anthropic row behind acc3,
  an Anthropic row on a legacy key), and the Anthropic adapter and the secret are never touched;
- they match the golden snapshot of pre-S2 code under those states, with LLM_DEPLOYMENT_ROUTING on (default) and off;
- "contradictory_acc3" (every row of the model disabled or on a disabled connection) changed meaning in S3: the
  registry is authoritative once it describes a model, so the model is refused; with the rollback switch off it is
  today's path again. The positive cases are in test_aa1002_s2_vendor_route.py / test_aa1002_s3_*.py.
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
# S3: who may read the registry (and why). Everything else in the gateway must not.
REGISTRY_READERS = {
    "vendor_route.py": "the deployment plan (S2 vendor row, S3 spread)",
    "client.py": "the alias targets (deployments.alias_targets_sync); S5: the OpenAI connection's data scope",
    "embed.py": "the connection of an embedding deployment (_runtime_for)",
    "bedrock_satellite.py": "role / ExternalId / region of an assume-role connection",
    "catalog.py": "serving_accounts() = the enabled Bedrock connections",
    "deployments.py": "reads its own table through connections.run_sync",
    "pricing.py": "S6: the price overrides of the deployment a call ran on (deployments.deployments_for_sync)",
    "breaker.py": "S4: its own table (llm_provider_health) through connections.run_sync / _connect; the limits and the "
                  "credential error type of a connection it is handed",
    "connection_budget.py": "S10: which connections have a monthly budget (registry_rows_sync) and the month spend / "
                            "policy reads through connections.run_sync / _connect",
    "tenant_budget.py": "AA-1016: the tenant budget and month-spend reads through connections.run_sync / _connect",
    "prompt_registry.py": "AA-900: the shared.prompt_version read through connections.run_sync / _connect",
    "embedding_space.py": "AA-1026: the embedding space registry read through connections.run_sync / _connect",
}
CALL_PATH = ("client.py", "embed.py", "decide.py", "judge.py", "shadow.py", "bedrock_satellite.py",
             "bedrock_batch.py", "call_log.py", "catalog.py", "role_config.py", "pricing.py", "errors.py",
             "prompt_cache.py", "stream_sink.py", "column_mapper.py", "schema_check.py", "models.py", "breaker.py",
             "data_scope.py", "connection_budget.py",
             "invariant.py",      # AA-1018: report-only failure-domain rule (reads the tables by SQL, not the registry)
             "stage_params.py",   # AA-1017: stage x model parameters (its own table; empty = the same request object)
             "tenant_budget.py",  # AA-1016: tenant AI budget check (no budget row = nothing read beyond the tenant set)
             "prompt_registry.py",  # AA-900: prompt version lookup (no registered row = None on the ledger)
             "embedding_space.py",  # AA-1026: embedding space registry (unreadable = the built-in 328 mirror)
             "json_reply.py")  # AA-1026: reads the JSON value out of a reply text (pure, no I/O)


def _imported_modules(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name.rsplit(".", 1)[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").rsplit(".", 1)[-1])
            names.update(a.name for a in node.names)
    return names


@pytest.mark.parametrize("name", [n for n in CALL_PATH if n not in REGISTRY_READERS])
def test_other_call_path_modules_do_not_import_the_registry(name):
    assert not (_imported_modules(LLM_CLIENT / name) & REGISTRY), f"{name} reads the registry: add it to " \
                                                                  "REGISTRY_READERS with the reason, or route it"


def test_every_gateway_module_is_classified():
    on_disk = {p.name for p in LLM_CLIENT.glob("*.py")} - {"__init__.py"}
    assert on_disk == set(CALL_PATH) | {"connections.py", "deployments.py", "vendor_route.py"}, \
        "new gateway module: add it to CALL_PATH (or move the registry check)"


@pytest.mark.parametrize("name", [n for n in CALL_PATH if n not in ("client.py", "embed.py")])
def test_only_the_client_and_embed_ask_the_vendor_route(name):
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
}
# S3: every row of the model is disabled / on a disabled connection: refused with routing on, today's path when off.
REFUSED_STATES = {
    "contradictory_acc3": (
        {"acc3": _acc3(enabled=False), "anthropic-direct": _anthropic(enabled=False)},
        [ModelDeployment("sonnet-5", "acc3", "global.anthropic.something-else", 10, True, "admin"),
         ModelDeployment("sonnet-5", "anthropic-direct", "claude-sonnet-5", 0, True, "admin")]),
}
ALL_STATES = {**NO_ROUTE_STATES, **REFUSED_STATES}


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
        conns, deps = ALL_STATES[registry]
        fetch = MagicMock(side_effect=lambda fn: conns if fn is connections._fetch_all else
                          ([] if fn is deployments._fetch_aliases else deps))
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


# ── golden snapshot taken from pre-S2 code ─────────────────────────────────────────────────────────────────────────

PRE_S2_GOLDEN = Path(__file__).resolve().parent / "snapshots" / "aa1002_pre_s2_calls.json"


def _registry_patches(registry: str):
    if registry == "missing_table":
        fetch = MagicMock(side_effect=OSError("relation shared.llm_provider_connection does not exist"))
    else:
        conns, deps = ALL_STATES[registry]
        fetch = MagicMock(side_effect=lambda fn: conns if fn is connections._fetch_all else
                          ([] if fn is deployments._fetch_aliases else deps))
    return [patch.object(connections, "run_sync", fetch)]


@pytest.mark.parametrize("routing", ["default", "off"])
@pytest.mark.parametrize("registry", ["missing_table", *sorted(NO_ROUTE_STATES)])
def test_calls_match_the_golden_snapshot_of_pre_s2_code(registry, routing, monkeypatch):
    """Review S258: the byte-identical test above compares S2 with S2's own missing-table path. This one compares with
    what code from before S2 (origin/main dd8eea05, no vendor_route) sent and returned, captured by
    tests/unit/aa1002_golden_calls.py run unchanged on that checkout: Converse, Converse with a schema (forced tool)
    and the legacy InvokeModel chain."""
    import json

    from tests.unit.aa1002_golden_calls import capture

    if routing == "off":
        monkeypatch.setenv("LLM_DEPLOYMENT_ROUTING", "off")          # the S3 rollback switch

    catalog.invalidate()
    connections.invalidate()
    deployments.invalidate()
    connections._connections = {}
    deployments._by_model = {}
    with patch.object(anthropic_messages, "send") as send, patch.object(connections, "_fetch_secret") as secret:
        got = capture(_registry_patches(registry))
    send.assert_not_called()
    secret.assert_not_called()
    assert json.loads(json.dumps(got, sort_keys=True)) == json.loads(PRE_S2_GOLDEN.read_text())


def test_s3_refuses_a_model_whose_every_row_is_switched_off(monkeypatch):
    """S3: the registry describes sonnet-5 (rows exist) but nothing is usable: the model is refused, no provider call.
    The rollback switch (LLM_DEPLOYMENT_ROUTING=off) gives today's path (the S2 behaviour) back."""
    catalog.invalidate()
    with pytest.raises(RuntimeError, match="no usable deployment"):
        _run("contradictory_acc3")
    monkeypatch.setenv("LLM_DEPLOYMENT_ROUTING", "off")
    assert repr(_run("contradictory_acc3")) == repr(_run("missing_table"))
