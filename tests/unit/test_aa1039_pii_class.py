"""AA-1039 (R6, CIS 373; root ADR 0019 d12 / TL-12) — the data class of a gateway stage.

``shared.llm_role_config.pii_class`` is ``content`` or ``pii``. A pii stage is held to allowed_scopes = {aws_only} by
a CHECK of migration 373 and the gateway calls it only on a US deployment on our AWS accounts (a ``us.`` profile, or
an in-region id on a ``us-*`` connection; never ``global.``, never a vendor API, never the hard-coded legacy chain).
Every row is ``content`` today, so this file also proves nothing changes for a content stage. No AWS, no vendor, no DB.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from fastapi import HTTPException

from shared.llm_client import connections, data_scope, deployments, invariant, role_config, vendor_route
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient, stage_pii_class
from shared.llm_client.connections import ProviderConnection
from shared.llm_client.deployments import ModelDeployment
from shared.llm_client.models import LLMRequest
from shared.llm_client.role_config import SAFE_DEFAULTS, StageConfig

AWS = ("aws_only",)
BOTH = ("aws_only", "vendor_direct")

HAIKU = CatalogModel(model_key="haiku-4-5", label="Haiku 4.5", vendor="anthropic", provider="bedrock",
                     api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-haiku-4-5"},
                     callable_via=("llm_client",), price_in_per_mtok=1.0, price_out_per_mtok=5.0, enabled=True)
NOVA = CatalogModel(model_key="nova-lite", label="Nova Lite", vendor="amazon", provider="bedrock",
                    api_style="converse", bedrock_profile_ids={"acc2": "us.amazon.nova-lite-v1:0"},
                    callable_via=("llm_client",), price_in_per_mtok=0.06, price_out_per_mtok=0.24, enabled=True)

CONNS = {
    "acc3": ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="assume_role", label="acc3",
                               data_scope="aws_only", enabled=True, account_id_env="BEDROCK_ACCT_PRIMARY",
                               region="us-west-1", role_name="Invoker", external_id="ext-acc3"),
    "acc2": ProviderConnection(connection_key="acc2", kind="bedrock", auth_mode="task_role", label="acc2",
                               data_scope="aws_only", enabled=True, region="us-west-1"),
    "anthropic-direct": ProviderConnection(connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key",
                                           label="anthropic", data_scope="vendor_direct", enabled=True,
                                           secret_name="aa-llm/dev/anthropic-direct",
                                           base_url="https://api.anthropic.com"),
}


def _dep(model, conn, prio, wire):
    return ModelDeployment(model, conn, wire, prio, True, "test")


@pytest.fixture
def registry(monkeypatch):
    state = {"deps": [
        _dep("haiku-4-5", "acc3", 10, "global.anthropic.claude-haiku-4-5"),
        _dep("haiku-4-5", "acc2", 20, "us.anthropic.claude-haiku-4-5"),
        _dep("haiku-4-5", "anthropic-direct", 100, "claude-haiku-4-5"),
        _dep("nova-lite", "acc2", 10, "us.amazon.nova-lite-v1:0"),
    ]}

    def rows(model_key, *, enabled_only=True):
        return sorted((d for d in state["deps"] if d.model_key == model_key and (d.enabled or not enabled_only)),
                      key=lambda d: (d.priority, d.connection_key))
    monkeypatch.setattr(connections, "registry_rows_sync", lambda: dict(CONNS))
    monkeypatch.setattr(connections, "get_connection_sync", lambda key: CONNS.get(key))
    monkeypatch.setattr(deployments, "deployments_for_sync", rows)
    monkeypatch.setattr(deployments, "has_rows_sync", lambda key: bool(rows(key, enabled_only=False)))
    monkeypatch.setattr(vendor_route, "circuit_open", lambda conn, model: False)
    return state


def _plan(model, scopes, pii=None):
    return [t.connection.connection_key for t in vendor_route.deployment_plan(model, scopes, pii_class=pii)]


# ── the module ────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (None, "content"), ("content", "content"), ("pii", "pii"), (MagicMock(), "content"), ("personal", "pii"),
])
def test_normalize_pii_class_fails_closed_on_unknown_text(value, expected):
    assert data_scope.normalize_pii_class(value) == expected


@pytest.mark.parametrize("wire, region, geo", [
    ("global.anthropic.claude-haiku-4-5", "us-west-1", "global"),
    ("us.anthropic.claude-haiku-4-5", "us-west-1", "us"),
    ("eu.anthropic.claude-haiku-4-5", "us-west-1", "eu"),
    ("amazon.titan-embed-text-v2:0", "us-west-1", "us"),         # in-region id on a US connection
    ("amazon.titan-embed-text-v2:0", "eu-west-1", "unknown"),
    ("claude-haiku-4-5", None, "unknown"),                       # a vendor API (no region)
    (None, None, "unknown"),
])
def test_deployment_geo(wire, region, geo):
    assert data_scope.deployment_geo(wire, region) == geo


def test_pii_allows_only_our_accounts_in_the_us():
    assert data_scope.pii_allows("content", "vendor_direct", "claude-haiku-4-5", None)
    assert data_scope.pii_allows(None, "aws_only", "global.x", "us-west-1")
    assert data_scope.pii_allows("pii", "aws_only", "us.x", "us-west-1")
    assert not data_scope.pii_allows("pii", "aws_only", "global.x", "us-west-1")
    assert not data_scope.pii_allows("pii", "vendor_direct", "us.x", "us-west-1")
    assert not data_scope.pii_allows("pii", "aws_only", "x", "eu-west-1")


def test_safe_defaults_are_content():
    """Every stage of today is content (migration 373's backfill); the DB-down fallback agrees."""
    assert all(cfg.pii_class == "content" for cfg in SAFE_DEFAULTS.values())
    assert role_config._GENERIC_FALLBACK.pii_class == "content"


# ── the deployment plan ───────────────────────────────────────────────────────────────────────────────────────────

def test_content_stage_plan_is_unchanged(registry):
    assert _plan(HAIKU, BOTH) == _plan(HAIKU, BOTH, "content") == ["acc3", "acc2", "anthropic-direct"]
    assert _plan(HAIKU, AWS, "content") == ["acc3", "acc2"]


def test_pii_stage_skips_global_profiles_and_vendor_apis(registry):
    assert _plan(HAIKU, AWS, "pii") == ["acc2"]
    assert _plan(HAIKU, BOTH, "pii") == ["acc2"]                 # belt and braces: the CHECK forbids BOTH anyway
    assert _plan(NOVA, AWS, "pii") == ["acc2"]


def test_pii_stage_with_only_global_deployments_gets_an_empty_plan(registry):
    registry["deps"] = [_dep("haiku-4-5", "acc3", 10, "global.anthropic.claude-haiku-4-5")]
    assert _plan(HAIKU, AWS, "pii") == []


# ── the client ────────────────────────────────────────────────────────────────────────────────────────────────────

def _client():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    c._openai = MagicMock()
    return c


def test_stage_pii_class_reads_role_config():
    with patch("shared.llm_client.client.get_stage_config_sync",
               return_value=StageConfig("x", "writer", "claude", "haiku-4-5", "acc3", pii_class="pii")):
        assert stage_pii_class(LLMRequest(system_prompt="s", user_prompt="u", stage="x")) == "pii"
    assert stage_pii_class(LLMRequest(system_prompt="s", user_prompt="u")) is None


def test_pii_stage_never_takes_a_path_the_registry_does_not_describe():
    """No registry rows (plan None): a content stage takes today's Converse path, a pii stage is refused (that path
    calls the catalog's acc3 profile, a global. one)."""
    c = _client()
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="x")
    with patch("shared.llm_client.client.get_model_sync", return_value=HAIKU), \
            patch("shared.llm_client.client.deployment_plan", return_value=None), \
            patch("shared.llm_client.client.vendor_deployment_for", return_value=None), \
            patch("shared.llm_client.client.stage_scopes", return_value=AWS), \
            patch("shared.llm_client.client.stage_role", return_value="writer"), \
            patch("shared.llm_client.client.stage_pii_class", return_value="pii"):
        with pytest.raises(RuntimeError, match="personal-data stage"):
            c._call_catalog_model(req, "haiku-4-5", "acc3")


def test_pii_stage_never_takes_the_legacy_chain():
    c = _client()
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="x")
    with patch("shared.llm_client.client.legacy_aliases_enabled", return_value=False), \
            patch("shared.llm_client.client.stage_pii_class", return_value="pii"), \
            patch.object(c, "_legacy_chain") as chain:
        with pytest.raises(RuntimeError, match="personal-data stage"):
            c._legacy_call(req, "haiku", "acc3")
    assert not chain.called
    with patch("shared.llm_client.client.legacy_aliases_enabled", return_value=False), \
            patch("shared.llm_client.client.stage_pii_class", return_value="content"), \
            patch.object(c, "_legacy_chain", return_value="ok") as chain:
        assert c._legacy_call(req, "haiku", "acc3") == "ok"


# ── role_config before / after migration 373 ──────────────────────────────────────────────────────────────────────

def _row(**kw):
    base = {"stage": "x", "role": "writer", "provider": "claude", "model_id": "haiku-4-5", "account_route": "acc3",
            "fallback_model_ids": [], "shadow_model_id": None, "shadow_sample_pct": 0, "allowed_scopes": ["aws_only"]}
    base.update(kw)
    return base


@pytest.fixture
def fresh_columns(monkeypatch):
    for name in ("_scope_column_missing_at", "_deadline_column_missing_at", "_switch_column_missing_at",
                 "_pii_column_missing_at"):
        monkeypatch.setattr(role_config, name, 0.0)


@pytest.mark.asyncio
async def test_role_config_reads_the_pii_column(fresh_columns):
    conn = MagicMock(fetchrow=AsyncMock(return_value=_row(pii_class="pii")), close=AsyncMock())
    with patch.object(role_config.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(role_config, "get_database_url", return_value="postgresql://x"):
        cfg = await role_config._fetch_one("x")
    assert cfg.pii_class == "pii"
    assert "pii_class" in conn.fetchrow.call_args.args[0]


@pytest.mark.asyncio
async def test_role_config_before_migration_373_reads_content(fresh_columns):
    calls = []

    async def fetchrow(sql, *args):
        calls.append(sql)
        if "pii_class" in sql:
            raise asyncpg.exceptions.UndefinedColumnError('column "pii_class" does not exist')
        return _row()
    conn = MagicMock(fetchrow=fetchrow, close=AsyncMock())
    with patch.object(role_config.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(role_config, "get_database_url", return_value="postgresql://x"):
        cfg = await role_config._fetch_one("x")
        assert cfg.pii_class == "content" and cfg.allowed_scopes == AWS and len(calls) == 2
        assert "degrade_mode" in calls[-1]                     # only the new column is dropped
        await role_config._fetch_one("x")                      # remembered: no second failing query in the window
        assert len(calls) == 3 and "pii_class" not in calls[-1]


@pytest.mark.asyncio
async def test_admin_list_carries_the_class(fresh_columns):
    async def fetch(sql, *args):
        row = _row(updated_at=None, updated_by="m", is_active=True)
        return [dict(row, pii_class="pii")] if "pii_class" in sql else [row]
    conn = MagicMock(fetch=fetch, close=AsyncMock())
    with patch.object(role_config.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(role_config, "get_database_url", return_value="postgresql://x"):
        rows = await role_config.list_stage_configs()
        assert rows[0]["pii_class"] == "pii"
        role_config._pii_column_missing_at = __import__("time").monotonic()     # as after a missing-column error
        rows = await role_config.list_stage_configs()
    assert rows[0]["pii_class"] is None                # before 373: the Routing card shows "no class yet"


# ── admin refusals ────────────────────────────────────────────────────────────────────────────────────────────────

def test_admin_refuses_a_model_with_no_us_deployment_for_a_pii_stage(registry):
    from api.routers import admin_llm_ops as ops
    registry["deps"] = [_dep("haiku-4-5", "acc3", 10, "global.anthropic.claude-haiku-4-5"),
                        _dep("nova-lite", "acc2", 10, "us.amazon.nova-lite-v1:0")]
    assert ops._pii_usable_models([HAIKU, NOVA]) == {"nova-lite"}
    with pytest.raises(HTTPException) as e:
        asyncio.run(ops._check_pii("x", {"pii_class": "pii"}, [HAIKU, NOVA], ["nova-lite", "haiku-4-5"]))
    assert e.value.status_code == 422 and "haiku-4-5" in e.value.detail and "nova-lite," not in e.value.detail
    asyncio.run(ops._check_pii("x", {"pii_class": "pii"}, [HAIKU, NOVA], ["nova-lite", None]))
    asyncio.run(ops._check_pii("x", {"pii_class": "content"}, [HAIKU, NOVA], ["haiku-4-5"]))   # content: no check
    asyncio.run(ops._check_pii("x", {}, [HAIKU, NOVA], ["haiku-4-5"]))                         # before 373


def test_route_control_names_the_pii_problem(registry):
    from api.services import llm_route_control as rc
    registry["deps"] = [_dep("haiku-4-5", "acc3", 10, "global.anthropic.claude-haiku-4-5"),
                        _dep("nova-lite", "acc2", 10, "us.amazon.nova-lite-v1:0")]
    row = {"stage": "x", "role": "writer", "allowed_scopes": ["aws_only"], "pii_class": "pii"}
    with patch("api.routers.admin_llm_ops._options_for", return_value=[
            {"model_id": "haiku-4-5", "available": True}, {"model_id": "nova-lite", "available": True}]):
        problems = rc.chain_problems(row, ["nova-lite", "haiku-4-5"], [HAIKU, NOVA], {})
        assert [p["rule"] for p in problems] == ["pii_class"] and "haiku-4-5" in problems[0]["message"]
        assert rc.chain_problems(dict(row, pii_class="content"), ["nova-lite", "haiku-4-5"], [HAIKU, NOVA], {}) == []


# ── the route invariant ───────────────────────────────────────────────────────────────────────────────────────────

def test_invariant_counts_only_us_deployments_for_a_pii_stage():
    conns = [{"connection_key": "acc3", "kind": "bedrock", "enabled": True, "data_scope": "aws_only",
              "region": "us-west-1", "failure_domain": "aws:acc3"},
             {"connection_key": "acc2", "kind": "bedrock", "enabled": True, "data_scope": "aws_only",
              "region": "us-west-1", "failure_domain": "aws:acc2"}]
    deps = [{"model_key": "haiku-4-5", "connection_key": "acc3", "priority": 10, "enabled": True,
             "wire_id": "global.anthropic.claude-haiku-4-5"},
            {"model_key": "haiku-4-5", "connection_key": "acc2", "priority": 20, "enabled": True,
             "wire_id": "us.anthropic.claude-haiku-4-5"}]
    route = {"stage": "x", "role": "writer", "model_id": "haiku-4-5", "fallback_model_ids": [],
             "allowed_scopes": ["aws_only"], "is_active": True}
    content = invariant.check([dict(route, pii_class="content")], [], deps, conns)["stages"][0]
    pii = invariant.check([dict(route, pii_class="pii")], [], deps, conns)["stages"][0]
    assert content["primary"].startswith("acc3") and pii["primary"].startswith("acc2")
    assert content["ok"] and not pii["ok"]                     # one US domain left: no warm path elsewhere


def test_invariant_reads_the_class_and_falls_back_before_migration_373():
    class Conn:
        def __init__(self):
            self.sql = []

        async def fetch(self, sql, *args):
            self.sql.append(sql)
            if "pii_class" in sql:
                raise asyncpg.exceptions.UndefinedColumnError('column "pii_class" does not exist')
            return []
    conn = Conn()
    report = asyncio.run(invariant.read(conn, blocked={}))
    assert report["ready"] is True
    assert conn.sql[:2] == [invariant.ROUTES_V4_SQL, invariant.ROUTES_V3_SQL]
    assert "served_roles" in invariant.CONNECTIONS_V2_SQL and "region" in invariant.CONNECTIONS_V2_SQL
    assert "wire_id" in invariant.DEPLOYMENTS_SQL


def test_stage_pii_class_reads_the_binding_first():
    """The class comes from the stage row bound for the call (one role_config read per call, like the scopes)."""
    from shared.llm_client.client import _STAGE_SCOPES
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="x")
    token = _STAGE_SCOPES.set(("x", AWS, "writer", "pii"))
    try:
        with patch("shared.llm_client.client.get_stage_config_sync") as read:
            assert stage_pii_class(req) == "pii"
            assert not read.called
    finally:
        _STAGE_SCOPES.reset(token)
