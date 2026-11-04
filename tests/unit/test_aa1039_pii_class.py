"""AA-1039 (R6, CIS 373; root ADR 0019 d12 / TL-12) — the data class of a gateway stage.

``shared.llm_role_config.pii_class`` is ``content`` or ``pii``. A pii stage is held to allowed_scopes = {aws_only} by
a CHECK of migration 373 and the gateway calls it only on a US deployment on our AWS accounts (a ``us.`` profile, or
an in-region id on a ``us-*`` connection; never ``global.``, never a vendor API, never the hard-coded legacy chain).
Every row is ``content`` today, so this file also proves nothing changes for a content stage. No AWS, no vendor, no DB.
"""
import asyncio
import dataclasses
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
    # review S268: only an explicit us. profile or an in-region <provider>.<model> id on a us-* connection is US
    ("cohere.embed-v4:0", "us-west-2", "us"),
    ("arn:aws:bedrock:us-west-2:111122223333:inference-profile/global.anthropic.claude-haiku-4-5", "us-west-2",
     "unknown"),                                                 # a full ARN: its profile is not read here
    ("arn:aws:bedrock:us-west-2::foundation-model/amazon.nova-lite-v1:0", "us-west-2", "unknown"),
    ("zz.anthropic.claude-haiku-4-5", "us-west-1", "unknown"),   # an unknown geography prefix
    ("claude-haiku-4-5", "us-west-1", "unknown"),                # not a Bedrock model id, even on a US connection
    ("x", "us-west-1", "unknown"),
    ("us.", "us-west-1", "unknown"),
])
def test_deployment_geo(wire, region, geo):
    assert data_scope.deployment_geo(wire, region) == geo


def test_pii_allows_only_our_accounts_in_the_us():
    assert data_scope.pii_allows("content", "vendor_direct", "claude-haiku-4-5", None, "anthropic")
    assert data_scope.pii_allows(None, "aws_only", "global.x", "us-west-1", "bedrock")
    assert data_scope.pii_allows("pii", "aws_only", "us.x", "us-west-1", "bedrock")
    assert not data_scope.pii_allows("pii", "aws_only", "global.x", "us-west-1", "bedrock")
    assert not data_scope.pii_allows("pii", "vendor_direct", "us.x", "us-west-1", "bedrock")
    assert not data_scope.pii_allows("pii", "aws_only", "x", "eu-west-1", "bedrock")


def test_pii_allows_refuses_an_arn_or_an_unknown_prefix_on_a_us_connection():
    assert not data_scope.pii_allows("pii", "aws_only", "arn:aws:bedrock:us-west-2:1:inference-profile/us.x",
                                     "us-west-2", "bedrock")
    assert not data_scope.pii_allows("pii", "aws_only", "zz.anthropic.claude-haiku-4-5", "us-west-1", "bedrock")
    assert data_scope.pii_allows("pii", "aws_only", "amazon.nova-lite-v1:0", "us-west-1", "bedrock")


@pytest.mark.parametrize("kind, allowed", [
    ("bedrock", True), ("bedrock_guardrails", True), ("mock", True),
    ("anthropic", False), ("openai", False), ("azure_openai", False), ("typesafe", False), (None, False),
])
def test_pii_allows_needs_an_aws_connection_kind(kind, allowed):
    """Review S268 (item 4): the data scope is a row value; a pii stage also needs a connection kind that runs inside
    our AWS accounts. A content stage does not look at the kind."""
    assert data_scope.pii_allows("pii", "aws_only", "us.anthropic.claude-haiku-4-5", "us-west-1", kind) is allowed
    assert data_scope.pii_allows("content", "aws_only", "us.anthropic.claude-haiku-4-5", "us-west-1", kind)


def test_a_vendor_kind_marked_aws_only_is_refused_everywhere(registry, monkeypatch):
    """A vendor connection whose row says aws_only (an edit or seed slip) with a us. wire id: the plan, the pin, the
    invariant and the admin's usable set all refuse it for a pii stage."""
    from api.routers import admin_llm_ops as ops
    slip = ProviderConnection(connection_key="slip", kind="anthropic", auth_mode="api_key", label="slip",
                              data_scope="aws_only", enabled=True, secret_name="aa-llm/dev/slip",
                              base_url="https://api.anthropic.com", region="us-west-1")
    conns = dict(CONNS, slip=slip)
    monkeypatch.setattr(connections, "registry_rows_sync", lambda: dict(conns))
    monkeypatch.setattr(connections, "get_connection_sync", lambda key: conns.get(key))
    registry["deps"] = [_dep("sonnet-5-5", "slip", 10, "us.anthropic.claude-sonnet-5-5")]
    assert [t.connection.connection_key for t in
            vendor_route.deployment_plan(SONNET_API, AWS, pii_class="content")] == ["slip"]
    assert vendor_route.deployment_plan(SONNET_API, AWS, pii_class="pii") == []
    with pytest.raises(RuntimeError, match="personal-data stage"):
        vendor_route.pinned_deployment(SONNET_API, "slip", allowed_scopes=AWS, pii_class="pii")
    assert ops._pii_usable_models([SONNET_API]) == frozenset()
    route = {"stage": "x", "role": "writer", "model_id": "sonnet-5-5", "fallback_model_ids": [],
             "allowed_scopes": ["aws_only"], "pii_class": "pii"}
    conn_row = {"connection_key": "slip", "kind": "anthropic", "enabled": True, "data_scope": "aws_only",
                "region": "us-west-1", "failure_domain": "vendor:anthropic"}
    dep = {"model_key": "sonnet-5-5", "connection_key": "slip", "priority": 10, "enabled": True,
           "wire_id": "us.anthropic.claude-sonnet-5-5"}
    assert invariant.check_stage(route, {}, {"sonnet-5-5": [dep]}, {"slip": conn_row}, {}).primary is None
    assert invariant.check_stage(dict(route, pii_class="content"), {}, {"sonnet-5-5": [dep]}, {"slip": conn_row},
                                 {}).primary.startswith("slip")


def test_safe_defaults_are_content():
    """Every stage of today is content (migration 373's backfill): a read that succeeds with no row stays content."""
    assert all(cfg.pii_class == "content" for cfg in SAFE_DEFAULTS.values())
    assert role_config._GENERIC_FALLBACK.pii_class == "content"


# ── review S268: a failed read never downgrades a stage to content ───────────────────────────────────────────────

@pytest.fixture
def cold_cache(monkeypatch):
    monkeypatch.setattr(role_config, "_cache", {})
    monkeypatch.setattr(role_config, "_pii_known", {})
    monkeypatch.setattr(role_config, "_watch_route_version", lambda: None)


@pytest.mark.asyncio
async def test_cold_cache_read_failure_fails_closed_as_pii(cold_cache):
    stage = next(iter(SAFE_DEFAULTS))
    with patch.object(role_config, "_fetch_one", AsyncMock(side_effect=OSError("db down"))):
        cfg = await role_config.get_stage_config(stage)
        unknown = await role_config.get_stage_config("adhoc_no_row")
    assert cfg.pii_class == "pii" and unknown.pii_class == "pii"
    assert data_scope.pii_class_unread(cfg.pii_class) and data_scope.pii_class_unread(unknown.pii_class)
    assert cfg.allowed_scopes == unknown.allowed_scopes == ("aws_only",)  # 373's invariant: pii = aws_only
    assert cfg.model_id == SAFE_DEFAULTS[stage].model_id                 # the route is still SAFE_DEFAULTS'
    assert SAFE_DEFAULTS[stage].pii_class == "content"                     # the shared default is not mutated
    assert stage not in role_config._cache                                 # not cached: the next call reads again


def test_cold_cache_sync_read_failure_fails_closed_as_pii(cold_cache):
    stage = next(iter(SAFE_DEFAULTS))
    with patch.object(role_config, "_fetch_one", AsyncMock(side_effect=OSError("db down"))):
        assert role_config.get_stage_config_sync(stage).pii_class == "pii"
    with patch.object(role_config, "get_stage_config", MagicMock(return_value=None)):   # asyncio.run(None) raises
        assert role_config.get_stage_config_sync(stage).pii_class == "pii"


@pytest.mark.asyncio
async def test_stale_cache_keeps_its_class_and_no_row_stays_content(cold_cache, monkeypatch):
    real = StageConfig("x", "writer", "claude", "haiku-4-5", "acc3", pii_class="content")
    role_config._cache["x"] = (real, 0.0)                                  # stale
    with patch.object(role_config, "_fetch_one", AsyncMock(side_effect=OSError("db down"))):
        assert (await role_config.get_stage_config("x")) is real
    with patch.object(role_config, "_fetch_one", AsyncMock(return_value=None)):
        assert (await role_config.get_stage_config("adhoc_no_row")).pii_class == "content"


def test_stage_pii_class_fails_closed_when_the_read_raises():
    with patch("shared.llm_client.client.get_stage_config_sync", side_effect=RuntimeError("boom")):
        assert stage_pii_class(LLMRequest(system_prompt="s", user_prompt="u", stage="x")) == "pii"


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


# ── review S268: a pinned model@connection keeps the stage's data policy ──────────────────────────────────────────

SONNET_API = CatalogModel(model_key="sonnet-5-5", label="Sonnet 5.5", vendor="anthropic", provider="anthropic",
                          api_style="anthropic_messages", bedrock_profile_ids={}, callable_via=("llm_client",),
                          price_in_per_mtok=3.0, price_out_per_mtok=15.0, enabled=True)


@pytest.fixture
def pin_registry(registry):
    registry["deps"].append(_dep("sonnet-5-5", "anthropic-direct", 10, "claude-sonnet-5-5"))
    return registry


@pytest.mark.parametrize("scopes, pii, refused", [
    (None, None, None),                          # a probe with no stage policy: unchanged
    (BOTH, "content", None),
    (AWS, "content", "data scope vendor_direct"),
    (AWS, "pii", "data scope vendor_direct"),
    (None, "pii", "personal-data stage"),
])
def test_pinned_deployment_keeps_the_stage_scopes_and_class(pin_registry, scopes, pii, refused):
    if refused is None:
        found = vendor_route.pinned_deployment(SONNET_API, "anthropic-direct", allowed_scopes=scopes, pii_class=pii)
        assert found.connection.connection_key == "anthropic-direct"
    else:
        with pytest.raises(RuntimeError, match=refused):
            vendor_route.pinned_deployment(SONNET_API, "anthropic-direct", allowed_scopes=scopes, pii_class=pii)


def test_pinned_call_passes_the_stage_policy(pin_registry):
    """The client's pin path reads the stage's scopes and class (like deployment_plan) and never calls the vendor when
    the stage may not use the pinned connection."""
    c = _client()
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="x")
    with patch("shared.llm_client.client.get_model_sync", return_value=SONNET_API), \
            patch("shared.llm_client.client.stage_scopes", return_value=AWS), \
            patch("shared.llm_client.client.stage_pii_class", return_value="pii"), \
            patch.object(c, "_call_anthropic_messages") as call:
        with pytest.raises(RuntimeError, match="not allowed for the stage"):
            c._call_catalog_model(req, "sonnet-5-5@anthropic-direct", "acc3")
    assert not call.called
    with patch("shared.llm_client.client.get_model_sync", return_value=SONNET_API), \
            patch("shared.llm_client.client.stage_scopes", return_value=None), \
            patch("shared.llm_client.client.stage_pii_class", return_value="pii"), \
            patch.object(c, "_call_anthropic_messages") as call:
        with pytest.raises(RuntimeError, match="personal-data stage"):
            c._call_catalog_model(req, "sonnet-5-5@anthropic-direct", "acc3")
    assert not call.called


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


# ── the route preview = the runtime plan, refusal evidence (AA-1042) and the data class together ──────────────────────

class _Refusals:
    """A stage_coverage.RefusalEvidence stand-in: the (model, connection) pairs the account refuses."""

    def __init__(self, *pairs):
        self.pairs = set(pairs)

    def refused(self, model_key, connection_key):
        return "refused (access_denied)" if (model_key, connection_key) in self.pairs else None


def _resolve(cfg, evidence):
    from api.services import llm_route_chains
    models = {"haiku-4-5": HAIKU, "nova-lite": NOVA}
    with patch("shared.llm_client.catalog.get_model_sync", side_effect=models.get):
        return llm_route_chains.resolve_stage(cfg, evidence)


def test_preview_skips_both_the_refused_and_the_non_us_deployments_of_a_pii_stage(registry):
    """haiku-4-5 on acc3 is a global. profile (left out of a pii plan, as the gateway does) and on acc2 the account
    refuses it: the pii stage is served by nova-lite on acc2; the content stage by haiku-4-5 on acc3."""
    ev = _Refusals(("haiku-4-5", "acc2"))
    pii = StageConfig("x", "writer", "claude", "haiku-4-5", "acc3", fallback_model_ids=("sonnet", "nova-lite"),
                      allowed_scopes=AWS, pii_class="pii")
    out = _resolve(pii, ev)
    assert out["pii_class"] == "pii" and out["first"] == "nova-lite@acc2"
    assert out["called_first"] == "haiku-4-5@acc2"
    haiku, sonnet, nova = out["attempts"]
    assert haiku["status"] == "refused" and haiku["deployments"] == ["acc2"]
    assert sonnet["status"] == "skip" and "personal-data" in sonnet["reason"]          # no hard-coded legacy chain
    assert nova["status"] == "call" and nova["connection"] == "acc2"
    assert [(s["model"], s["connection"]) for s in out["skipped"]] == [("haiku-4-5", "acc2"), ("sonnet", None)]
    # the runtime plan of each model is the preview's deployments
    assert _plan(HAIKU, AWS, "pii") == haiku["deployments"] and _plan(NOVA, AWS, "pii") == nova["deployments"]

    content = _resolve(StageConfig("x", "writer", "claude", "haiku-4-5", "acc3", fallback_model_ids=("nova-lite",),
                                   allowed_scopes=AWS, pii_class="content"), ev)
    assert content["first"] == "haiku-4-5@acc3" and content["skipped"] == []
    assert content["attempts"][0]["deployments"] == _plan(HAIKU, AWS, "content") == ["acc3", "acc2"]


# ── review S268 (items 2, 3): the class of a failed read ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failed_read_keeps_the_class_last_read_after_invalidate(cold_cache):
    """invalidate() and the route-version watch drop _cache, not the class: a stage read before keeps its real class
    on a failed read; only a stage never read in this process fails closed (marked unread)."""
    vendor_stage = next(s for s, c in SAFE_DEFAULTS.items() if c.allowed_scopes == data_scope.ALL_SCOPES)
    content_row = dataclasses.replace(SAFE_DEFAULTS[vendor_stage], pii_class="content")
    pii_row = StageConfig("p", "writer", "claude", "haiku-4-5", "acc3", allowed_scopes=AWS, pii_class="pii")
    with patch.object(role_config, "_fetch_one", AsyncMock(side_effect=[content_row, pii_row])):
        await role_config.get_stage_config(vendor_stage)
        await role_config.get_stage_config("p")
    role_config.invalidate()
    with patch.object(role_config, "_fetch_one", AsyncMock(side_effect=OSError("db down"))):
        content = await role_config.get_stage_config(vendor_stage)
        pii = await role_config.get_stage_config("p")
        never = await role_config.get_stage_config("never_read")
    assert content.pii_class == "content" and content.allowed_scopes == data_scope.ALL_SCOPES   # SAFE_DEFAULTS'
    assert pii.pii_class == "pii" and not data_scope.pii_class_unread(pii.pii_class)
    assert pii.allowed_scopes == ("aws_only",)
    assert data_scope.pii_class_unread(never.pii_class) and never.allowed_scopes == ("aws_only",)
    with patch.object(role_config, "_fetch_one", AsyncMock(side_effect=OSError("db down"))):
        assert role_config.get_stage_config_sync(vendor_stage).pii_class == "content"


@pytest.mark.asyncio
async def test_a_read_with_no_row_is_remembered_as_content(cold_cache):
    with patch.object(role_config, "_fetch_one", AsyncMock(return_value=None)):
        await role_config.get_stage_config("adhoc_x")
    role_config.invalidate("adhoc_x")
    with patch.object(role_config, "_fetch_one", AsyncMock(side_effect=OSError("db down"))):
        assert (await role_config.get_stage_config("adhoc_x")).pii_class == "content"


def test_the_unread_class_is_pii_everywhere_and_says_unknown(pin_registry):
    unread = data_scope.PII_UNREAD
    assert unread == "pii" and data_scope.normalize_pii_class(unread) is unread
    assert not data_scope.pii_class_unread("pii") and not data_scope.pii_class_unread("content")
    assert data_scope.pii_reason(unread) == ("stage class unknown (role_config read failed): US AWS deployments "
                                             "only")
    assert "personal-data" in data_scope.pii_reason("pii")
    assert _plan(HAIKU, AWS, unread) == _plan(HAIKU, AWS, "pii") == ["acc2"]
    with pytest.raises(RuntimeError, match=r"stage class unknown \(role_config read failed\)"):
        vendor_route.pinned_deployment(SONNET_API, "anthropic-direct", allowed_scopes=None, pii_class=unread)
    with patch("shared.llm_client.client.get_stage_config_sync", side_effect=RuntimeError("boom")):
        got = stage_pii_class(LLMRequest(system_prompt="s", user_prompt="u", stage="x"))
    assert got == "pii" and data_scope.pii_class_unread(got)


def test_an_unread_class_names_itself_in_the_client_refusal():
    c = _client()
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="x")
    with patch("shared.llm_client.client.legacy_aliases_enabled", return_value=False), \
            patch("shared.llm_client.client.stage_pii_class", return_value=data_scope.PII_UNREAD), \
            patch.object(c, "_legacy_chain") as chain:
        with pytest.raises(RuntimeError, match="stage class unknown"):
            c._legacy_call(req, "haiku", "acc3")
    assert not chain.called


def test_call_openai_refuses_a_pii_stage_whatever_its_scopes():
    """Item 2: the OpenAI adapter checks the class itself; a pii stage with (wrongly) both scopes is still refused
    before any request."""
    from shared.llm_client.client import OpenAIPlatformBlocked
    c = _client()
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="x")
    with patch("shared.llm_client.client.openai_platform_allowed", return_value=True), \
            patch("shared.llm_client.client.stage_scopes", return_value=BOTH), \
            patch("shared.llm_client.client.stage_pii_class", return_value="pii"):
        with pytest.raises(OpenAIPlatformBlocked, match="personal-data stage"):
            c._call_openai(req, model="gpt-4.1")
    assert not c._openai.chat.completions.create.called


def test_legacy_last_resort_never_calls_gpt41_for_a_pii_stage():
    c = _client()
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="x")
    with patch("shared.llm_client.client.openai_platform_allowed", return_value=True), \
            patch("shared.llm_client.client.openai_scope_allowed", return_value=True), \
            patch("shared.llm_client.client.stage_pii_class", return_value="pii"), \
            patch.object(c, "_legacy_tier") as tier:
        with pytest.raises(RuntimeError, match="no OpenAI last resort: a personal-data stage"):
            c._legacy_last_resort(req, "haiku", [RuntimeError("bedrock down")])
    assert not tier.called
    with patch("shared.llm_client.client.openai_platform_allowed", return_value=True), \
            patch("shared.llm_client.client.openai_scope_allowed", return_value=True), \
            patch("shared.llm_client.client.stage_pii_class", return_value="content"), \
            patch.object(c, "_legacy_tier", return_value=MagicMock()) as tier:
        c._legacy_last_resort(req, "haiku", [RuntimeError("bedrock down")])
    assert tier.called                                         # a content stage: unchanged


# ── review S268 (item 5): the preview of a legacy key on a pii stage mirrors _legacy_call ─────────────────────────

def test_preview_plans_the_alias_targets_of_a_legacy_key_on_a_pii_stage(registry):
    pii = StageConfig("x", "writer", "claude", "sonnet", "acc3", allowed_scopes=AWS, pii_class="pii")
    models = {"haiku-4-5": HAIKU, "nova-lite": NOVA}
    with patch("shared.llm_client.client.legacy_aliases_enabled", return_value=True), \
            patch("shared.llm_client.client.legacy_acc2_native_enabled", return_value=False), \
            patch("shared.llm_client.client.alias_targets_sync", return_value=["haiku-4-5", "nova-lite"]), \
            patch("shared.llm_client.client.get_model_sync", side_effect=models.get):
        out = _resolve(pii, _Refusals(("haiku-4-5", "acc2")))
        free = _resolve(pii, None)
    (step,) = out["attempts"]
    assert step["deployments"] == ["haiku-4-5@acc2", "nova-lite@acc2"]          # no global. profile, no vendor API
    assert step["status"] == "call" and out["first"] == "nova-lite@acc2"
    assert out["called_first"] == "haiku-4-5@acc2"
    assert [(s["model"], s["connection"]) for s in out["skipped"]] == [("haiku-4-5", "acc2")]
    assert free["first"] == "haiku-4-5@acc2" and free["skipped"] == []
    with patch("shared.llm_client.client.legacy_aliases_enabled", return_value=False):
        off = _resolve(pii, None)
    assert off["attempts"][0]["status"] == "skip" and off["first"] is None       # the hard-coded chain: refused


def test_route_control_preview_reads_the_pii_models_once_in_a_thread(registry, monkeypatch):
    """Item 5: chain_problems takes the pii-usable set; preview_world computes it once (not per stage)."""
    from api.routers import admin_llm_ops as ops
    from api.services import llm_route_control as rc
    registry["deps"] = [_dep("haiku-4-5", "acc3", 10, "global.anthropic.claude-haiku-4-5"),
                        _dep("nova-lite", "acc2", 10, "us.amazon.nova-lite-v1:0")]
    row = {"stage": "x", "role": "writer", "allowed_scopes": ["aws_only"], "pii_class": "pii"}
    with patch("api.routers.admin_llm_ops._options_for", return_value=[
            {"model_id": "haiku-4-5", "available": True}, {"model_id": "nova-lite", "available": True}]), \
            patch("api.routers.admin_llm_ops._pii_usable_models", side_effect=AssertionError("read again")):
        problems = rc.chain_problems(row, ["nova-lite", "haiku-4-5"], [HAIKU, NOVA], {}, frozenset({"nova-lite"}))
    assert [p["rule"] for p in problems] == ["pii_class"]
    calls = []
    real = ops._pii_usable_models
    monkeypatch.setattr(ops, "_pii_usable_models", lambda models: calls.append(1) or real(models))
    seen = []
    monkeypatch.setattr(rc, "chain_problems", lambda row, chain, models, reach, pii_usable=None: seen.append(
        pii_usable) or [])

    class World:
        live = {"routes": {}, "priorities": {}, "order_mode": "model_first"}
        by_stage = {"x": row, "y": dict(row, stage="y")}
        stages, models, reach, evidence = [], [HAIKU, NOVA], {}, None

    target = {"routes": {"x": {"model_id": "nova-lite", "fallback_model_ids": []},
                         "y": {"model_id": "nova-lite", "fallback_model_ids": []}},
              "priorities": {}, "order_mode": "model_first"}
    monkeypatch.setattr(rc.route_versions, "diff", lambda live, tgt: [{"kind": "route", "stage": "x"},
                                                                      {"kind": "route", "stage": "y"}])
    try:
        asyncio.run(rc.preview_world(World(), target, []))
    except Exception:  # noqa: BLE001 — only the validation part matters here
        pass
    assert calls == [1] and seen == [frozenset({"nova-lite"})] * 2


# ── review S268 (item 6): Bedrock Batch never runs a pii writer stage ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("pii_class", ["pii", data_scope.PII_UNREAD])
async def test_s1_batch_refuses_a_pii_stage_before_reading_any_prompt(pii_class):
    from services.content_generation import s1_batch
    from shared.llm_client.bedrock_batch import BatchUnavailable
    cfg = dataclasses.replace(SAFE_DEFAULTS["s1_generate"], pii_class=pii_class, allowed_scopes=AWS)
    with patch.object(role_config, "get_stage_config", AsyncMock(return_value=cfg)), \
            patch.object(s1_batch.asyncpg, "connect", AsyncMock()) as connect, \
            patch.object(s1_batch.bedrock_batch, "submit_batch_job") as submit:
        with pytest.raises(BatchUnavailable, match="run the tours on demand"):
            await s1_batch.submit_s1_batch(["t1"] * 200)
    assert not connect.called and not submit.called


@pytest.mark.asyncio
async def test_s1_batch_goes_on_for_a_content_stage():
    from services.content_generation import s1_batch
    from shared.llm_client.bedrock_batch import BatchUnavailable
    with patch.object(role_config, "get_stage_config", AsyncMock(return_value=SAFE_DEFAULTS["s1_generate"])), \
            patch.object(s1_batch.asyncpg, "connect", AsyncMock(return_value=AsyncMock())), \
            patch.object(s1_batch, "_load_prompt_state", AsyncMock(return_value=None)):
        with pytest.raises(BatchUnavailable, match="no eligible tours"):      # past the class check
            await s1_batch.submit_s1_batch(["t1"])
