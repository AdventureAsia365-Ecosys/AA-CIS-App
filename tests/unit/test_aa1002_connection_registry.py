"""AA-1002 (gateway slice S1) — connection registry: credential policy, fail-closed resolver, read side, migration 304
parity with the code constants, and the admin listing (no secret values)."""
import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from api.core import staff_authz
from api.services import llm_connections
from shared.llm_client import bedrock_satellite, connections, deployments, pricing
from shared.llm_client.connections import (
    ConnectionCredentialError,
    ConnectionPolicyError,
    ProviderConnection,
    check_base_url,
    check_secret_name,
    resolve_api_key,
)
from shared.llm_client.deployments import ModelDeployment

MIGRATION = Path(__file__).resolve().parents[2] / "api" / "migrations" / "304_llm_connection_deployment.sql"


def _conn(key="anthropic-direct", **kw):
    base = dict(connection_key=key, kind="anthropic", auth_mode="api_key", label=key, data_scope="vendor_direct",
                enabled=True, secret_name="aa-llm/dev/anthropic-direct", base_url="https://api.anthropic.com")
    base.update(kw)
    return ProviderConnection(**base)


@pytest.fixture(autouse=True)
def _clear_caches():
    def clear():
        connections._credentials.clear()
        connections._connections = {}
        deployments._by_model = {}
        connections.invalidate()
        deployments.invalidate()
    clear()
    yield
    clear()


# ── policy: secret prefix ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["aa-llm/dev/anthropic-direct", "aa-llm/prod/openai-direct", "aa-llm/dev/x1"])
def test_secret_name_inside_aa_llm_is_accepted(name):
    check_secret_name(name)


@pytest.mark.parametrize("name", [
    "aa-cis/dev/rds", "aa-cis/dev/openai-key", "aa-cis/dev/typesafe", "booking/dev/database-url",
    "aa-llm/", "aa-llm/dev/", "aa-llm/dev", "aa-llm/dev/../rds", "aa-llm/dev/a/b", "aa-llm//x",
    "AA-LLM/dev/x", " aa-llm/dev/x", "aa-llm/dev/x\n",
    "arn:aws:secretsmanager:us-west-1:<account>:secret:aa-llm/dev/x",
])
def test_secret_name_outside_aa_llm_is_refused(name):
    with pytest.raises(ConnectionPolicyError):
        check_secret_name(name)


# ── policy: base_url allowlist per kind ────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind,url", [
    ("anthropic", "https://api.anthropic.com"), ("anthropic", "https://api.anthropic.com/"),
    ("openai", "https://api.openai.com/v1"), ("azure_openai", "https://aa-dev.openai.azure.com/"),
    ("bedrock", None), ("openai", None),
])
def test_base_url_on_the_allowlist_is_accepted(kind, url):
    check_base_url(kind, url)


@pytest.mark.parametrize("kind,url", [
    ("anthropic", "http://api.anthropic.com"),                     # not https
    ("anthropic", "https://api.openai.com/v1"),                    # another kind's host
    ("openai", "https://api.openai.com.evil.example/v1"),          # suffix trick
    ("openai", "https://evil.example/api.openai.com"),
    ("openai", "https://api.openai.com@evil.example/v1"),          # userinfo
    ("openai", "https://user:pw@api.openai.com/v1"),
    ("openai", "https://api.openai.com:8443/v1"),                  # other port
    ("openai", "https://api.openai.com/v1?x=1"),
    ("azure_openai", "https://openai.azure.com/"),                 # no resource label
    ("azure_openai", "https://a.b.openai.azure.com/"),             # nested label
    ("bedrock", "https://bedrock-runtime.us-west-1.amazonaws.com"),  # bedrock takes no base_url
    ("mock", "https://api.openai.com/v1"),
])
def test_base_url_off_the_allowlist_is_refused(kind, url):
    with pytest.raises(ConnectionPolicyError):
        check_base_url(kind, url)


def test_policy_problems_lists_every_reason():
    c = _conn(secret_name="aa-cis/dev/rds", base_url="https://evil.example", credential_env="AWS_SECRET_ACCESS_KEY",
              auth_mode="assume_role")
    problems = " | ".join(c.policy_problems())
    for needle in ("auth_mode", "secret_name", "base_url", "credential_env"):
        assert needle in problems


def test_assume_role_needs_role_external_id_region_and_account():
    c = ProviderConnection(connection_key="bedrock-llm-org", kind="bedrock", auth_mode="assume_role", label="x",
                           data_scope="aws_only", role_name="AA-LLM-Invoker", region="us-west-1")
    assert any("external_id" in p and "account_id" in p for p in c.policy_problems())


# ── account / role ARN from the allowlisted env only ───────────────────────────────────────────────────────────────

def test_role_arn_uses_the_allowlisted_account_env(monkeypatch):
    monkeypatch.setenv("BEDROCK_ACCT_PRIMARY", "123456789012")
    c = ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="assume_role", label="x",
                           data_scope="aws_only", account_id_env="BEDROCK_ACCT_PRIMARY",
                           role_name="AA3-Bedrock-Invoker",
                           external_id="e", region="us-west-1", enabled=True)
    assert c.role_arn() == "arn:aws:iam::123456789012:role/AA3-Bedrock-Invoker"
    assert c.policy_problems() == []
    assert c.credential_source() == "sts"


def test_account_env_outside_the_allowlist_is_never_read(monkeypatch):
    monkeypatch.setenv("SOME_OTHER_ACCOUNT", "123456789012")
    c = ProviderConnection(connection_key="x1", kind="bedrock", auth_mode="assume_role", label="x",
                           data_scope="aws_only", account_id_env="SOME_OTHER_ACCOUNT", role_name="r",
                           external_id="e", region="us-west-1")
    assert c.resolved_account_id() is None and c.role_arn() is None
    assert any("account_id_env" in p for p in c.policy_problems())


# ── resolver fails closed ──────────────────────────────────────────────────────────────────────────────────────────

def test_resolver_reads_the_aa_llm_secret_and_caches_it():
    with patch.object(connections, "_fetch_secret", return_value='{"api_key": " sk-1 "}') as fetch:
        assert resolve_api_key(_conn()) == "sk-1"
        assert resolve_api_key(_conn()) == "sk-1"
    fetch.assert_called_once_with("aa-llm/dev/anthropic-direct")


def test_invalidate_credential_reads_the_secret_again():
    with patch.object(connections, "_fetch_secret", side_effect=["k1", "k2"]) as fetch:
        assert resolve_api_key(_conn()) == "k1"
        connections.invalidate_credential("anthropic-direct")
        assert resolve_api_key(_conn()) == "k2"
    assert fetch.call_count == 2


@pytest.mark.parametrize("conn", [
    _conn(enabled=False),
    _conn(secret_name="aa-cis/dev/rds"),                       # another app's secret: never fetched
    _conn(base_url="https://evil.example"),                     # key would leave for an arbitrary host
    _conn(secret_name=None),                                    # no credential named
    _conn(secret_name=None, credential_env="AWS_SECRET_ACCESS_KEY"),
    ProviderConnection(connection_key="acc2", kind="bedrock", auth_mode="task_role", label="x",
                       data_scope="aws_only", region="us-west-1", enabled=True),
])
def test_resolver_refuses_before_reading_anything(conn, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    with patch.object(connections, "_fetch_secret") as fetch, pytest.raises(ConnectionCredentialError):
        resolve_api_key(conn)
    fetch.assert_not_called()


@pytest.mark.parametrize("effect", [RuntimeError("AccessDeniedException"), ["   "], ['{"api_key": ""}'], ["{oops"]])
def test_resolver_fails_closed_on_an_unreadable_or_empty_secret(effect):
    with patch.object(connections, "_fetch_secret", side_effect=effect), \
            pytest.raises(ConnectionCredentialError):
        resolve_api_key(_conn())
    assert connections._credentials == {}


def test_resolver_env_fallback_only_for_the_allowlisted_env(monkeypatch):
    c = _conn("openai-direct", kind="openai", secret_name=None, credential_env="OPENAI_API_KEY",
              base_url="https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    assert resolve_api_key(c) == "sk-env"
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(ConnectionCredentialError):
        resolve_api_key(c)


# ── read side never raises ─────────────────────────────────────────────────────────────────────────────────────────

def test_connection_read_failure_is_swallowed_and_negative_cached():
    with patch.object(connections, "run_sync", side_effect=OSError("relation does not exist")) as rs:
        assert connections.get_connection_sync("acc3") is None
        assert connections.get_connection_sync("acc3") is None
    assert rs.call_count == 1


def test_deployments_sorted_by_priority_and_filtered():
    rows = [ModelDeployment("haiku", "acc2", "us.x", 20, False, "legacy_constant"),
            ModelDeployment("haiku", "acc3", "global.x", 10, True, "legacy_constant"),
            ModelDeployment("haiku", "anthropic-direct", "claude-haiku-4-5", 10, True, "seed")]
    with patch.object(connections, "run_sync", return_value=rows):
        assert [d.connection_key for d in deployments.deployments_for_sync("haiku")] == ["acc3", "anthropic-direct"]
        assert [d.connection_key for d in deployments.deployments_for_sync("haiku", enabled_only=False)] == \
            ["acc3", "anthropic-direct", "acc2"]
        assert deployments.deployments_for_sync("nope") == []


def test_deployment_read_failure_is_swallowed():
    with patch.object(connections, "run_sync", side_effect=OSError("db down")):
        assert deployments.deployments_for_sync("haiku") == []


# ── migration 304 agrees with the code ─────────────────────────────────────────────────────────────────────────────

def _sql():
    return MIGRATION.read_text(encoding="utf-8")


def _check_values(name):
    m = re.search(rf"CONSTRAINT {name}\s+CHECK \((.*?)\),\n", _sql(), re.S)
    assert m, name
    return set(re.findall(r"'([^']*)'", m.group(1)))


def test_migration_secret_pattern_is_the_code_pattern():
    m = re.search(r"secret_name ~ '([^']+)'", _sql())
    assert m and m.group(1) == connections.SECRET_NAME_RE.pattern


def test_migration_closed_lists_are_the_code_lists():
    assert _check_values("llm_provider_connection_kind_check") == set(connections.KINDS)
    assert _check_values("llm_provider_connection_data_scope_check") == set(connections.DATA_SCOPES)
    assert _check_values("llm_provider_connection_account_id_env_check") == set(connections.ACCOUNT_ID_ENVS)
    assert _check_values("llm_provider_connection_credential_env_check") == set(connections.CREDENTIAL_ENVS)
    pairs = _check_values("llm_provider_connection_auth_mode_check")
    assert pairs == set(connections.KINDS) | {m for modes in connections.AUTH_MODES.values() for m in modes}


def test_migration_legacy_constants_are_the_code_constants():
    sql = _sql()
    for model_key, account, wire in (
            ("haiku", "acc3", bedrock_satellite._PROFILE_ID_HAIKU),
            ("sonnet", "acc3", bedrock_satellite._PROFILE_ID_SONNET),
            ("haiku", "acc2", pricing.BEDROCK_HAIKU),
            ("sonnet", "acc2", pricing.BEDROCK_SONNET)):
        assert re.search(rf"\('{model_key}',\s+'{account}', '{re.escape(wire)}'", sql), (model_key, account, wire)


def test_migration_acc3_seed_is_the_satellite_config():
    sql = _sql()
    assert f"'{bedrock_satellite.ACC3_EXTERNAL_ID}'" in sql
    assert "'AA3-Bedrock-Invoker'" in sql and bedrock_satellite.ACC3_ROLE_ARN.endswith(":role/AA3-Bedrock-Invoker")
    assert f"'{bedrock_satellite.ACC3_REGION}'" in sql


def test_migration_seeded_secrets_and_urls_pass_the_code_policy():
    sql = _sql()
    seeds = re.search(r"INSERT INTO shared\.llm_provider_connection.*?ON CONFLICT", sql, re.S).group(0)
    for name in re.findall(r"'(aa-llm/[^']*)'", seeds):
        check_secret_name(name)
    for kind, url in (("openai", "https://api.openai.com/v1"), ("anthropic", "https://api.anthropic.com")):
        assert f"'{url}'" in seeds
        check_base_url(kind, url)


def test_migration_carries_no_account_id():
    assert not re.search(r"\b\d{12}\b", _sql())


# ── admin listing ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_listing_is_admin_only():
    assert staff_authz.POLICY[("GET", "/admin/llm-connections")] == staff_authz.AI_ADMIN
    for role in ("reviewer", "content", "viewer"):
        assert staff_authz.AI_ADMIN not in staff_authz.ROLE_PERMS[role]


def test_listing_shows_sources_and_problems_never_a_value(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    monkeypatch.setenv("BEDROCK_ACCT_PRIMARY", "123456789012")
    conns = {
        "acc3": ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="assume_role", label="L",
                                   data_scope="aws_only", account_id_env="BEDROCK_ACCT_PRIMARY",
                                   role_name="AA3-Bedrock-Invoker", external_id="ext", region="us-west-1",
                                   enabled=True),
        "openai-direct": _conn("openai-direct", kind="openai", secret_name=None, credential_env="OPENAI_API_KEY",
                               base_url="https://api.openai.com/v1"),
        "bad": _conn("bad", secret_name="aa-cis/dev/rds"),
    }
    deps = [ModelDeployment("sonnet", "acc3", "global.anthropic.claude-sonnet-4-6", 10, True, "legacy_constant")]
    with patch.object(connections, "_fetch_secret") as fetch:
        out = llm_connections.build(conns, deps)
    fetch.assert_not_called()
    rows = {r["connection_key"]: r for r in out["connections"]}
    assert out["schema_ready"] is True and out["routing_uses_registry"] is False
    assert rows["acc3"]["account_id"] == "123456789012" and rows["acc3"]["credential_source"] == "sts"
    assert rows["acc3"]["deployments"][0]["wire_id"] == "global.anthropic.claude-sonnet-4-6"
    assert rows["openai-direct"]["credential_source"] == "env"
    assert rows["openai-direct"]["credential_env_set"] is True
    assert rows["bad"]["policy_problems"]
    assert "sk-secret-value" not in repr(out) and "ext" not in [v for r in out["connections"] for v in r.values()]


def test_listing_before_migration_304_is_not_an_error():
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=asyncpg.exceptions.UndefinedTableError("relation does not exist"))
    out = asyncio.run(llm_connections.read(conn))
    assert out == {"schema_ready": False, "routing_uses_registry": False, "connections": []}
