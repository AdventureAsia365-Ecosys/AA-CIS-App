"""AA-1002 (gateway slice S5) — the data policy is data: a stage's ``allowed_scopes`` against a connection's
``data_scope``.

The "nothing changes" side is ``test_aa1002_s3_routes_snapshot.py`` (every Dev stage, ``*_scoped`` cases with the
backfill of migration 311). This file proves what the scopes decide: the deployment plan, the S2 vendor path, the
OpenAI adapter gate, the admin refusals and options, the reads before migration 311. No AWS, no vendor, no DB.
"""
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import HTTPException

from shared.llm_client import connections, data_scope, deployments, role_config, vendor_route
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import (LLMClient, OpenAIPlatformBlocked, openai_platform_allowed, openai_scope_allowed,
                                      stage_scopes)
from shared.llm_client.connections import ProviderConnection
from shared.llm_client.deployments import ModelDeployment
from shared.llm_client.models import LLMRequest
from shared.llm_client.role_config import SAFE_DEFAULTS, StageConfig

MIGRATION = Path(__file__).resolve().parents[2] / "api" / "migrations" / "311_llm_stage_allowed_scopes.sql"
BOTH = ("aws_only", "vendor_direct")
AWS = ("aws_only",)

SONNET55 = CatalogModel(model_key="sonnet-5-5", label="Sonnet 5.5", vendor="anthropic", provider="bedrock",
                        api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5-5"},
                        callable_via=("llm_client",), price_in_per_mtok=2.0, price_out_per_mtok=10.0, enabled=True)
LUNA_API = CatalogModel(model_key="gpt-6-luna-openai", label="GPT-6 Luna (OpenAI API)", vendor="openai",
                        provider="openai", api_style="openai_chat", bedrock_profile_ids={}, wire_model="gpt-6-luna",
                        callable_via=("llm_client",), price_in_per_mtok=0.1, price_out_per_mtok=0.5, enabled=True)
LUNA = CatalogModel(model_key="gpt-6-luna", label="GPT-6 Luna", vendor="openai", provider="bedrock",
                    api_style="converse", bedrock_profile_ids={"acc3": "global.openai.gpt-6-luna"},
                    callable_via=("llm_client",), price_in_per_mtok=0.1, price_out_per_mtok=0.5, enabled=True)

CONNS = {
    "acc3": ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="assume_role", label="acc3",
                               data_scope="aws_only", enabled=True, account_id_env="BEDROCK_ACCT_PRIMARY",
                               region="us-west-1", role_name="AA3-Bedrock-Invoker",
                               external_id="aa296-satellite-bedrock-acc3"),
    "anthropic-direct": ProviderConnection(connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key",
                                           label="anthropic", data_scope="vendor_direct", enabled=True,
                                           secret_name="aa-llm/dev/anthropic-direct",
                                           base_url="https://api.anthropic.com"),
    "openai-direct": ProviderConnection(connection_key="openai-direct", kind="openai", auth_mode="api_key",
                                        label="openai", data_scope="vendor_direct", enabled=True,
                                        credential_env="OPENAI_API_KEY", base_url="https://api.openai.com/v1"),
}


def _dep(model, conn, prio, wire=None):
    return ModelDeployment(model, conn, wire or f"{conn}:{model}", prio, True, "test")


@pytest.fixture
def registry(monkeypatch):
    """The 304 / 306 shape for three models; ``deps`` may be replaced per test."""
    state = {"deps": [_dep("sonnet-5-5", "acc3", 10), _dep("sonnet-5-5", "anthropic-direct", 100),
                      _dep("gpt-6-luna-openai", "openai-direct", 10), _dep("gpt-6-luna", "acc3", 10)]}

    def rows(model_key, *, enabled_only=True):
        return sorted((d for d in state["deps"] if d.model_key == model_key and (d.enabled or not enabled_only)),
                      key=lambda d: (d.priority, d.connection_key))
    monkeypatch.setattr(connections, "registry_rows_sync", lambda: dict(CONNS))
    monkeypatch.setattr(connections, "get_connection_sync", lambda key: CONNS.get(key))
    monkeypatch.setattr(deployments, "deployments_for_sync", rows)
    monkeypatch.setattr(deployments, "has_rows_sync", lambda key: bool(rows(key, enabled_only=False)))
    monkeypatch.setattr(vendor_route, "circuit_open", lambda conn, model: False)
    return state


def _plan(model, scopes):
    return [t.connection.connection_key for t in vendor_route.deployment_plan(model, scopes)]


# ── the module ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_scopes_match_the_connection_check_and_migration_311():
    assert data_scope.DATA_SCOPES == connections.DATA_SCOPES == BOTH
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "allowed_scopes <@ ARRAY['aws_only', 'vendor_direct']::text[]" in sql
    assert "SET DEFAULT ARRAY['aws_only']::text[]" in sql


@pytest.mark.parametrize("value, expected", [
    (None, None), ("aws_only", None), (MagicMock(), None),
    (["aws_only"], AWS), (["vendor_direct", "aws_only"], BOTH), (("aws_only", "vendor_direct"), BOTH),
    (["anywhere"], AWS), ([], AWS), (["aws_only", "eu"], AWS),
])
def test_normalize_fails_closed(value, expected):
    assert data_scope.normalize(value) == expected


def test_backfill_rule():
    outside = data_scope.outside_aws_only({"gpt-6-luna-openai": frozenset({"vendor_direct"}),
                                           "sonnet-5-5": frozenset(BOTH), "gpt-6-luna": frozenset(AWS)})
    assert outside == {"gpt-6-luna-openai"}
    assert data_scope.backfill_scopes(["gpt-5.6-luna", "gpt-6-luna", "gpt-6-luna-openai", None], outside) == BOTH
    assert data_scope.backfill_scopes(["sonnet-5-5", "gpt-6-luna"], outside) == AWS


def test_safe_defaults_carry_the_backfill_of_their_routes():
    """The DB-down fallback gives every stage the scopes migration 311 would: both when its route names the OpenAI
    platform API (the catalog seed's provider='openai' rows), else aws_only."""
    from tests.unit.test_s235_openai_last_resort import _catalog_providers
    outside = {k for k, p in _catalog_providers().items() if p == "openai"}
    for stage, cfg in SAFE_DEFAULTS.items():
        route = [cfg.model_id, *cfg.fallback_model_ids, cfg.shadow_model_id]
        assert cfg.allowed_scopes == data_scope.backfill_scopes(route, outside), stage
    assert role_config._GENERIC_FALLBACK.allowed_scopes is None          # an unknown stage: no stage policy


# ── the deployment plan ───────────────────────────────────────────────────────────────────────────────────────────

def test_aws_only_stage_never_calls_a_vendor_api_even_first(registry):
    registry["deps"] = [_dep("sonnet-5-5", "anthropic-direct", 1), _dep("sonnet-5-5", "acc3", 10)]
    assert _plan(SONNET55, AWS) == ["acc3"]
    assert _plan(SONNET55, None) == ["anthropic-direct", "acc3"]       # no stage policy: S2's deliberate switch


def test_vendor_direct_stage_may_fall_back_to_a_vendor_api_behind_acc3(registry):
    assert _plan(SONNET55, BOTH) == ["acc3", "anthropic-direct"]
    assert _plan(SONNET55, None) == ["acc3"]                           # no stage policy: never a fallback (S3)
    assert _plan(SONNET55, AWS) == ["acc3"]


def test_a_model_reached_only_outside_aws_is_refused_for_an_aws_only_stage(registry):
    assert vendor_route.deployment_plan(LUNA_API, AWS) == []            # described, none usable: the route moves on
    assert _plan(LUNA_API, BOTH) == ["openai-direct"]
    assert _plan(LUNA_API, None) == ["openai-direct"]


def test_the_s2_vendor_path_obeys_the_scopes(registry):
    registry["deps"] = [_dep("sonnet-5-5", "anthropic-direct", 1), _dep("sonnet-5-5", "acc3", 10)]
    assert vendor_route.vendor_deployment_for(SONNET55, AWS) is None
    assert vendor_route.vendor_deployment_for(SONNET55, BOTH).connection.connection_key == "anthropic-direct"
    assert vendor_route.vendor_deployment_for(SONNET55).connection.connection_key == "anthropic-direct"


# ── the client ────────────────────────────────────────────────────────────────────────────────────────────────────

def _client():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    c._openai = MagicMock()
    return c


def _cfg(stage, scopes, route=("gpt-5.6-luna", "gpt-6-luna-openai")):
    return StageConfig(stage, "judge", "openai", route[0], "acc3", fallback_model_ids=tuple(route[1:]),
                       allowed_scopes=scopes)


def test_generate_reads_the_stage_config_once_and_binds_its_scopes(registry):
    seen = []
    c = _client()

    def call_key(request, key, acct, **kw):
        seen.append(stage_scopes(request))
        raise RuntimeError("down")
    cfg = _cfg("s1_judge", BOTH, ("haiku", "sonnet"))           # legacy keys: no catalog read before the call
    with patch("shared.llm_client.client.get_stage_config_sync", return_value=cfg) as read, \
            patch.object(c, "_call_key_logged", side_effect=call_key):
        with pytest.raises(RuntimeError):
            c.generate(LLMRequest(system_prompt="s", user_prompt="u", stage="s1_judge"))
    assert read.call_count == 1 and seen == [BOTH, BOTH]
    from shared.llm_client.client import _STAGE_SCOPES
    assert _STAGE_SCOPES.get() is None                                   # the binding ends with the call


def test_stage_scopes_reads_the_stage_config():
    with patch("shared.llm_client.client.get_stage_config_sync", return_value=_cfg("s1_judge", BOTH)):
        assert stage_scopes(LLMRequest(system_prompt="s", user_prompt="u", stage="s1_judge")) == BOTH
        assert stage_scopes(LLMRequest(system_prompt="s", user_prompt="u")) is None
    with patch("shared.llm_client.client.get_stage_config_sync", return_value=MagicMock()):
        assert stage_scopes(LLMRequest(system_prompt="s", user_prompt="u", stage="x")) is None
    with patch("shared.llm_client.client.get_stage_config_sync", side_effect=RuntimeError("db")):
        assert stage_scopes(LLMRequest(system_prompt="s", user_prompt="u", stage="x")) is None


def test_openai_gate_needs_the_catalog_row_and_the_stage_scope(registry):
    with patch("shared.llm_client.client.get_model_sync", side_effect={"gpt-6-luna-openai": LUNA_API}.get):
        assert openai_platform_allowed("gpt-6-luna-openai") is True
        assert openai_platform_allowed("gpt-4.1") is False                       # no catalog row: closed
    assert openai_scope_allowed(BOTH) is True
    assert openai_scope_allowed(None) is True                                    # no stage policy: S235 alone
    assert openai_scope_allowed(AWS) is False


@pytest.mark.parametrize("scopes, called", [(BOTH, True), (None, True), (AWS, False)])
def test_call_openai_refuses_an_aws_only_stage(registry, scopes, called):
    c = _client()
    c._openai.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="ok"), finish_reason="stop")],
        usage=MagicMock(prompt_tokens=10, completion_tokens=2, prompt_tokens_details=None))
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="t2_generate")
    with patch("shared.llm_client.client.get_model_sync", side_effect={"gpt-6-luna-openai": LUNA_API}.get), \
            patch("shared.llm_client.client.get_stage_config_sync", return_value=_cfg("t2_generate", scopes)):
        if called:
            assert c._call_openai(req, model="gpt-6-luna", catalog_model=LUNA_API).content == "ok"
        else:
            with pytest.raises(OpenAIPlatformBlocked, match="may not send content outside AWS"):
                c._call_openai(req, model="gpt-6-luna", catalog_model=LUNA_API)
    assert c._openai.chat.completions.create.called is called


def test_a_judge_route_reaches_the_openai_last_resort_only_with_vendor_direct(registry):
    """The S235 judge route, every Bedrock call failing: with {aws_only, vendor_direct} (the backfill) the OpenAI API
    is the last call; with {aws_only} it is never called."""
    models = {"gpt-5.6-luna": LUNA, "gpt-6-luna": LUNA, "gpt-6-luna-openai": LUNA_API}
    registry["deps"] = [_dep("gpt-5.6-luna", "acc3", 10), _dep("gpt-6-luna", "acc3", 10),
                        _dep("gpt-6-luna-openai", "openai-direct", 10)]
    for scopes, reached in ((BOTH, True), (AWS, False)):
        c = _client()
        c._openai.chat.completions.create.side_effect = RuntimeError("openai down")
        with patch("shared.llm_client.client.get_model_sync", side_effect=models.get), \
                patch("shared.llm_client.client.get_stage_config_sync",
                      return_value=_cfg("s1_judge", scopes, ("gpt-5.6-luna", "gpt-6-luna", "gpt-6-luna-openai"))), \
                patch.object(c, "_call_bedrock_converse", side_effect=RuntimeError("acc3 down")), \
                patch("shared.llm_client.client.breaker.guarded", side_effect=lambda conn, key, fn, **kw: fn()):
            with pytest.raises(RuntimeError):
                c.generate(LLMRequest(system_prompt="s", user_prompt="u", stage="s1_judge"))
        assert c._openai.chat.completions.create.called is reached, scopes


# ── role_config before / after migration 311 ──────────────────────────────────────────────────────────────────────

def _row(**kw):
    base = {"stage": "s1_judge", "role": "judge", "provider": "openai", "model_id": "gpt-5.6-luna",
            "account_route": "acc3", "fallback_model_ids": ["gpt-6-luna"], "shadow_model_id": None,
            "shadow_sample_pct": 0}
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_role_config_reads_the_scope_column(monkeypatch):
    conn = MagicMock(fetchrow=AsyncMock(return_value=_row(allowed_scopes=["aws_only", "vendor_direct"])),
                     close=AsyncMock())
    monkeypatch.setattr(role_config, "_scope_column_missing_at", 0.0)
    with patch.object(role_config.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(role_config, "get_database_url", return_value="postgresql://x"):
        cfg = await role_config._fetch_one("s1_judge")
    assert cfg.allowed_scopes == BOTH
    assert "allowed_scopes" in conn.fetchrow.call_args.args[0]


@pytest.mark.asyncio
async def test_role_config_before_migration_311_has_no_stage_policy(monkeypatch):
    calls = []

    async def fetchrow(sql, *args):
        calls.append(sql)
        if "allowed_scopes" in sql:
            raise asyncpg.exceptions.UndefinedColumnError('column "allowed_scopes" does not exist')
        return _row()
    conn = MagicMock(fetchrow=fetchrow, close=AsyncMock())
    monkeypatch.setattr(role_config, "_scope_column_missing_at", 0.0)
    with patch.object(role_config.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(role_config, "get_database_url", return_value="postgresql://x"):
        cfg = await role_config._fetch_one("s1_judge")
        assert cfg.allowed_scopes is None and len(calls) == 2
        await role_config._fetch_one("s1_judge")             # remembered: no second failing query in the window
        assert len(calls) == 3 and "allowed_scopes" not in calls[-1]


# ── admin ─────────────────────────────────────────────────────────────────────────────────────────────────────────

ADMIN_MODELS = [LUNA, LUNA_API, CatalogModel(model_key="gpt-5.6-luna", label="GPT-5.6 Luna", vendor="openai",
                                             provider="bedrock", api_style="converse",
                                             bedrock_profile_ids={"acc3": "x"}, callable_via=("llm_client",),
                                             price_in_per_mtok=0.1, price_out_per_mtok=0.5, enabled=True)]
REACH = {"gpt-6-luna": frozenset(AWS), "gpt-5.6-luna": frozenset(AWS),
         "gpt-6-luna-openai": frozenset({"vendor_direct"})}


def _judge_row(scopes):
    return {"stage": "t10_judge", "role": "judge", "model_id": "gpt-5.6-luna", "fallback_model_ids": ["gpt-6-luna"],
            "shadow_model_id": None, "shadow_sample_pct": 0, "allowed_scopes": scopes}


async def _patch_route(body, scopes):
    from api.routers import admin_llm_ops as ops
    row = _judge_row(scopes)

    async def setter(stage, **kw):
        return {"stage": stage, "model_id": row["model_id"], "updated_at": None, **kw}
    with patch.object(ops, "verify_admin_secret"), \
            patch.object(ops, "list_stage_configs", AsyncMock(return_value=[row])), \
            patch.object(ops, "_load_catalog", AsyncMock(return_value=ADMIN_MODELS)), \
            patch.object(ops, "_load_reach", AsyncMock(return_value=REACH)), \
            patch.object(ops, "set_stage_route", setter):
        return await ops.patch_llm_route("t10_judge", ops.LlmRoutePatch(**body), x_admin_secret="x",
                                         x_admin_user_id="a")


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [list(BOTH), None], ids=["vendor_direct_stage", "no_stage_policy"])
async def test_admin_accepts_the_openai_api_last_resort_where_the_stage_allows_it(scopes):
    # AA-1026 D7: a judge fallback-chain change carries its evidence (a break-glass reason here)
    out = await _patch_route({"fallback_model_ids": ["gpt-6-luna", "gpt-6-luna-openai"],
                              "break_glass_reason": "acc3 refuses every model since the outage"}, scopes)
    assert out["fallback_model_ids"] == ["gpt-6-luna", "gpt-6-luna-openai"]


@pytest.mark.asyncio
async def test_admin_refuses_a_vendor_api_model_for_an_aws_only_stage():
    with pytest.raises(HTTPException) as exc:
        await _patch_route({"fallback_model_ids": ["gpt-6-luna", "gpt-6-luna-openai"]}, ["aws_only"])
    assert exc.value.status_code == 422 and "may not send content outside AWS" in exc.value.detail
    assert "migration" in exc.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"fallback_model_ids": ["gpt-6-luna-openai", "gpt-6-luna"]},
                                  {"shadow_model_id": "gpt-6-luna-openai", "shadow_sample_pct": 5}])
async def test_admin_keeps_the_last_resort_rule_on_a_vendor_direct_stage(body):
    with pytest.raises(HTTPException) as exc:
        await _patch_route(body, list(BOTH))
    assert exc.value.status_code == 422 and "last fallback" in exc.value.detail


def test_options_mark_a_model_outside_the_stage_scope():
    from api.routers.admin_llm_ops import _catalog_options
    aws = {o["model_id"]: o for o in _catalog_options("judge", "t10_judge", ADMIN_MODELS, AWS, REACH)}
    assert aws["gpt-6-luna-openai"]["available"] is False
    assert "outside AWS" in aws["gpt-6-luna-openai"]["reason"]
    assert aws["gpt-6-luna"]["available"] is True
    both = {o["model_id"]: o for o in _catalog_options("judge", "t10_judge", ADMIN_MODELS, BOTH, REACH)}
    assert both["gpt-6-luna-openai"]["available"] is True
    none = {o["model_id"]: o for o in _catalog_options("judge", "t10_judge", ADMIN_MODELS)}
    assert none["gpt-6-luna-openai"]["available"] is True


def test_reach_comes_from_the_registry_else_from_the_provider(registry, monkeypatch):
    from api.routers.admin_llm_ops import _reach_scopes
    reach = _reach_scopes([SONNET55, LUNA_API, LUNA])
    assert reach == {"sonnet-5-5": frozenset(BOTH), "gpt-6-luna-openai": frozenset({"vendor_direct"}),
                     "gpt-6-luna": frozenset(AWS)}
    monkeypatch.setattr(connections, "registry_rows_sync", lambda: {})
    assert _reach_scopes([SONNET55, LUNA_API]) == {"sonnet-5-5": frozenset(AWS),
                                                   "gpt-6-luna-openai": frozenset({"vendor_direct"})}


@pytest.mark.asyncio
async def test_get_llm_config_returns_the_scopes_read_only():
    from api.routers import admin_llm_ops as ops
    rows = [dict(_judge_row(["aws_only"]), provider="openai", account_route="acc3", updated_at=None)]
    with patch.object(ops, "list_stage_configs", AsyncMock(return_value=rows)), \
            patch.object(ops, "_load_catalog", AsyncMock(return_value=ADMIN_MODELS)), \
            patch.object(ops, "_load_reach", AsyncMock(return_value=REACH)):
        out = await ops.get_llm_config()
    stage = out["stages"][0]
    assert stage["allowed_scopes"] == ["aws_only"]
    assert {o["model_id"]: o["available"] for o in stage["options"]}["gpt-6-luna-openai"] is False


def test_migration_311_is_a_contract_file_with_the_backfill_rule():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert not re.search(r"^-- migrate: expand", sql, re.M)
    assert "bool_and(c.data_scope <> 'aws_only')" in sql and "WHERE r.allowed_scopes IS NULL" in sql
    assert "REVOKE INSERT, UPDATE ON shared.llm_role_config FROM cis_app" in sql
    granted = re.findall(r"GRANT (?:INSERT|UPDATE) \(([^)]*)\)", sql)
    assert len(granted) == 2 and all("allowed_scopes" not in g for g in granted)
    # every other column of the table keeps its grant (db-schema-reference: 11 columns before 311)
    cols = {c.strip() for c in granted[0].split(",")}
    assert cols == {"stage", "role", "provider", "model_id", "account_route", "is_active", "updated_at", "updated_by",
                    "fallback_model_ids", "shadow_model_id", "shadow_sample_pct"}
