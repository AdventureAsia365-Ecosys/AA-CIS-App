"""AA-1002 (gateway slice S3) — the provider call order of every Dev stage route, for the before/after snapshot.

``capture(registry=..., env=...)`` sends one plain and one structured request through ``LLMClient.generate`` (an
``embed()`` call for an embed stage) for each of the 37 stage routes of ``snapshots/aa1002_s3_routes_fixture.json``
(Dev ``shared.llm_role_config`` + ``shared.llm_model_catalog`` of 29/10, taken from the root
``docs/generated/llm-routes.snapshot.json``), with EVERY provider call failing, so the gateway walks the whole route.
Each provider attempt is recorded in order: the API (converse, converse_stream, invoke_model, openai, anthropic), the
account / connection it went to and the wire model id. Nothing reaches AWS, OpenAI, Anthropic or a database.

Registry states:
- ``missing``: the connection registry cannot be read (``connections.run_sync`` raises): today's code path.
- ``seed``: the rows migrations 304 + 306 (+ 307 when the alias loader exists) write for this catalog: acc3, acc2 and
  openai-direct enabled, anthropic-direct and bedrock-llm-org disabled.
- ``seed_anthropic_on``: ``seed`` with anthropic-direct ENABLED (the admin switch of S2): its deployments sit behind
  acc3 (priority 100), so the call order must not change either.

The snapshot ``snapshots/aa1002_s3_pre_s3_route_calls.json`` was written by running THIS file unchanged against the
``origin/main`` checkout from before S3 (``ec0dd46b``, S2 on main), unit-test env (``BEDROCK_ACCT_PRIMARY=
acc3-test-account``)::

    python -I tests/unit/aa1002_s3_route_capture.py <checkout root> \
        > tests/unit/snapshots/aa1002_s3_pre_s3_route_calls.json

``test_aa1002_s3_routes_snapshot.py`` compares S3 code under each registry state and flag with it. The module imports
nothing S3 added at module level (it must run on the old checkout too).
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

FIXTURE = Path(__file__).resolve().parent / "snapshots" / "aa1002_s3_routes_fixture.json"
SCHEMA = {"name": "verdict", "schema": {"type": "object", "properties": {"score": {"type": "integer"}},
                                        "required": ["score"]}}


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def catalog_models(fixture: dict) -> dict:
    from shared.llm_client.catalog import CatalogModel
    out = {}
    for m in fixture["models"]:
        out[m["model_key"]] = CatalogModel(**{**m, "callable_via": tuple(m["callable_via"])})
    return out


def stage_configs(fixture: dict) -> dict:
    from shared.llm_client.role_config import StageConfig
    return {r["stage"]: StageConfig(stage=r["stage"], role=r["role"], provider=r["provider"], model_id=r["model_id"],
                                    account_route=r["account_route"],
                                    fallback_model_ids=tuple(r["fallback_model_ids"]),
                                    shadow_model_id=r["shadow_model_id"], shadow_sample_pct=r["shadow_sample_pct"])
            for r in fixture["routes"]}


# ── the registry rows migrations 304 / 306 / 307 write for the fixture catalog ──────────────────────────────────

def seed_connections(anthropic_enabled: bool = False) -> dict:
    from shared.llm_client.connections import ProviderConnection
    return {
        "acc3": ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="assume_role", label="acc3",
                                   data_scope="aws_only", enabled=True, account_id_env="BEDROCK_ACCT_PRIMARY",
                                   region="us-west-1", role_name="AA3-Bedrock-Invoker",
                                   external_id="aa296-satellite-bedrock-acc3"),
        "acc2": ProviderConnection(connection_key="acc2", kind="bedrock", auth_mode="task_role", label="acc2",
                                   data_scope="aws_only", enabled=True, region="us-west-1"),
        "openai-direct": ProviderConnection(connection_key="openai-direct", kind="openai", auth_mode="api_key",
                                            label="openai", data_scope="vendor_direct", enabled=True,
                                            credential_env="OPENAI_API_KEY", base_url="https://api.openai.com/v1"),
        "anthropic-direct": ProviderConnection(connection_key="anthropic-direct", kind="anthropic",
                                               auth_mode="api_key", label="anthropic", data_scope="vendor_direct",
                                               enabled=anthropic_enabled, secret_name="aa-llm/dev/anthropic-direct",
                                               base_url="https://api.anthropic.com"),
        "bedrock-llm-org": ProviderConnection(connection_key="bedrock-llm-org", kind="bedrock",
                                              auth_mode="assume_role", label="org", data_scope="aws_only",
                                              enabled=False, region="us-west-1", role_name="AA-LLM-Invoker"),
    }


_LEGACY_CONSTANTS = (
    ("haiku", "acc3", "global.anthropic.claude-haiku-4-5-20251001-v1:0", 10, True),
    ("sonnet", "acc3", "global.anthropic.claude-sonnet-4-6", 10, True),
    ("haiku", "acc2", "us.anthropic.claude-haiku-4-5-20251001-v1:0", 20, False),
    ("sonnet", "acc2", "us.anthropic.claude-sonnet-4-5-20250929-v1:0", 20, False),
)


def seed_deployments(models: dict) -> list:
    """304 a/b/c then 306, ON CONFLICT DO NOTHING (the first row of a (model, connection) pair wins)."""
    import re

    from shared.llm_client.deployments import ModelDeployment
    rows: dict = {}

    def add(model_key, conn, wire, prio, enabled, origin):
        rows.setdefault((model_key, conn), ModelDeployment(model_key, conn, wire, prio, enabled, origin))

    for mk, conn, wire, prio, enabled in _LEGACY_CONSTANTS:
        if mk in models:
            add(mk, conn, wire, prio, enabled, "legacy_constant")
    for m in models.values():
        if m.provider != "bedrock":
            continue
        for acct, profile in m.bedrock_profile_ids.items():
            if acct in ("acc3", "acc2") and profile:
                add(m.model_key, acct, profile, 10 if acct == "acc3" else 20,
                    acct == "acc3" or m.api_style == "embed", "bedrock_profile")
    for m in models.values():
        if m.provider == "openai":
            add(m.model_key, "openai-direct", m.wire_model or m.model_key, 10, True, "openai_wire")
    for m in models.values():                                                    # 306
        p = m.bedrock_profile_ids.get("acc3") or ""
        if m.vendor == "anthropic" and m.provider == "bedrock" and m.api_style == "converse" and p:
            wire = re.sub(r"-v[0-9]+:[0-9]+$", "", re.sub(r"^(global|us|eu|apac|jp|au)\.anthropic\.", "", p))
            if re.fullmatch(r"claude-[a-z0-9-]+", wire):
                add(m.model_key, "anthropic-direct", wire, 100, True, "seed")
    return list(rows.values())


SEED_ALIASES = (("haiku", 1, "haiku"), ("sonnet", 1, "sonnet"), ("sonnet", 2, "haiku"))   # migration 307


def _registry_fetch(registry: str, models: dict):
    """A stand-in for ``connections.run_sync`` (every registry read goes through it)."""
    if registry == "missing":
        return MagicMock(side_effect=OSError("relation shared.llm_provider_connection does not exist"))
    conns = seed_connections(anthropic_enabled=(registry == "seed_anthropic_on"))
    deps = seed_deployments(models)

    def fetch(fn):
        name = f"{fn.__module__.rsplit('.', 1)[-1]}.{fn.__name__}"
        if name == "connections._fetch_all":
            return conns
        if name == "deployments._fetch_all":
            return deps
        if name == "deployments._fetch_aliases":
            from shared.llm_client.deployments import ModelAlias
            return [ModelAlias(a, p, t) for a, p, t in SEED_ALIASES]
        raise AssertionError(f"unexpected registry read {name}")
    return MagicMock(side_effect=fetch)


def _reset_registry():
    from shared.llm_client import connections, deployments
    connections.invalidate()
    deployments.invalidate()
    connections._connections = {}
    deployments._by_model = {}
    if hasattr(deployments, "_aliases"):
        deployments._aliases = {}


# ── recording fakes ───────────────────────────────────────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self):
        self.calls: list = []

    def runtime(self, where: str):
        rt = MagicMock()

        def fail(api):
            def _f(*args, **kwargs):
                self.calls.append([api, where, kwargs.get("modelId")])
                raise RuntimeError(f"fake {api} outage")
            return _f
        for api in ("converse", "converse_stream", "invoke_model", "invoke_model_with_response_stream"):
            getattr(rt, api).side_effect = fail(api)
        return rt


def _run_stage(cfg, rec: Recorder, structured: bool):
    from shared.llm_client.client import LLMClient
    from shared.llm_client.models import LLMRequest
    from shared.llm_client import embed as embed_mod

    if cfg.role == "embed":
        try:
            embed_mod.embed(cfg.stage, ["a text"], dimensions=1024 if structured else None)
            return "ok"
        except Exception as e:  # noqa: BLE001
            return type(e).__name__
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        client = LLMClient()

    def openai_fail(**kwargs):
        rec.calls.append(["openai", "openai-direct", kwargs.get("model")])
        raise RuntimeError("fake openai outage")
    client._openai.chat.completions.create.side_effect = openai_fail
    client._bedrock = rec.runtime("acc2-native")
    req = LLMRequest(system_prompt="s", user_prompt="u", stage=cfg.stage, json_schema=SCHEMA if structured else None)
    try:
        client.generate(req)
        return "ok"
    except Exception as e:  # noqa: BLE001
        return type(e).__name__


def capture(registry: str = "missing", env: dict | None = None) -> dict:
    """{stage: {"plain": {"calls": [...], "raised": cls}, "structured": {...}}} for every stage of the fixture."""
    from shared.llm_client import catalog, connections

    fixture = load_fixture()
    models = catalog_models(fixture)
    cfgs = stage_configs(fixture)
    out: dict = {}
    for stage in sorted(cfgs):
        cfg = cfgs[stage]
        out[stage] = {}
        for variant in ("plain", "structured"):
            rec = Recorder()
            _reset_registry()
            catalog._models = dict(models)
            catalog._loaded_at, catalog._failed_at = time.monotonic(), 0.0
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, env or {}))
                stack.enter_context(patch.object(connections, "run_sync", _registry_fetch(registry, models)))
                stack.enter_context(patch("shared.llm_client.client.get_stage_config_sync", return_value=cfg))
                stack.enter_context(patch("shared.llm_client.embed.get_stage_config_sync", return_value=cfg))
                stack.enter_context(patch("shared.llm_client.client.random.random", return_value=1.0))
                stack.enter_context(patch("shared.llm_client.client.stream_sink.delta_callback", return_value=None))
                stack.enter_context(patch("shared.llm_client.client.log_gateway_billed_failure"))
                stack.enter_context(patch("shared.llm_client.bedrock_satellite.get_satellite_client",
                                          side_effect=lambda service="bedrock-runtime", account="acc3", rec=rec:
                                          rec.runtime(account)))

                def session_for(account="acc3", rec=rec):
                    s = MagicMock()
                    s.client.return_value = rec.runtime(account)
                    return s
                stack.enter_context(patch("shared.llm_client.bedrock_satellite._get_satellite_session",
                                          side_effect=session_for))
                stack.enter_context(patch("shared.llm_client.embed._runtime_for",
                                          side_effect=lambda account, rec=rec: rec.runtime(account)))

                def anthropic_send(rec=rec, **kwargs):
                    rec.calls.append(["anthropic", kwargs["connection"].connection_key, kwargs.get("wire_id")])
                    raise RuntimeError("fake anthropic outage")
                stack.enter_context(patch("shared.llm_client.adapters.anthropic_messages.send",
                                          side_effect=anthropic_send))
                raised = _run_stage(cfg, rec, variant == "structured")
            out[stage][variant] = {"calls": rec.calls, "raised": raised}
    catalog.invalidate()
    catalog._models = {}
    _reset_registry()
    return out


if __name__ == "__main__":
    import structlog

    os.environ.setdefault("BEDROCK_ACCT_PRIMARY", "acc3-test-account")
    os.environ.setdefault("ENVIRONMENT", "test")
    os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
    os.environ.setdefault("JWT_SECRET", "test-jwt-secret-ci-only-not-real")
    structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))   # stdout carries only the JSON
    sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else ".")
    print(json.dumps(capture(sys.argv[2] if len(sys.argv) > 2 else "missing"), indent=1, sort_keys=True))
