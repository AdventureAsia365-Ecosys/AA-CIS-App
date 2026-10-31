"""AA-1017 (registry v2; migrations 320 / 322) — families and capabilities as data, the provider kinds kept out of the
LLM dispatch, the per-kind secret / host guard, the deployment api_style, the truncated class, the read-only secret,
endpoint and price, and the provider settings (Jev URL / timeout / retries, low-balance thresholds, the Geoapify daily
credit limit) read from the connection rows with the env / constant fallback.

Every new value is empty / NULL on the live rows at 320 / 322, so each "unset" case below pins today's behaviour; the
golden snapshot of the gateway's provider requests is tests/unit/test_aa1002_no_behaviour_change.py.
"""
import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from api.core import readiness
from api.services import ai_gateway as gw
from api.services import llm_connections
from shared import jev_credit
from shared.llm_client import call_log, catalog, connections, decide, errors
from shared.llm_client import client as client_mod
from shared.llm_client.adapters import anthropic_messages as am
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.connections import ProviderConnection
from shared.llm_client.deployments import ModelDeployment, _row_to_deployment, fetch_deployments
from shared.llm_client.models import LLMRequest
from shared.llm_client.vendor_route import VendorDeployment
from tests.unit.test_aa1002_s2_admin_switch import FakeDB
from tests.unit.test_aa1002_s2_admin_switch import _row as _conn_row

RATES = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
MIGRATIONS = Path(__file__).resolve().parents[2] / "api" / "migrations"
M320 = MIGRATIONS / "320_llm_registry_v2_families_params.sql"
M322 = MIGRATIONS / "322_provider_connection_v2.sql"


def _model(**kw):
    base = dict(model_key="sonnet-5-5", label="s", vendor="anthropic", provider="bedrock", api_style="converse",
                bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5-5"}, callable_via=("llm_client",),
                supports_temperature=False, price_in_per_mtok=2.0, price_out_per_mtok=10.0, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


@pytest.fixture(autouse=True)
def _fresh():
    client_mod._FORCED_TOOL_REJECTED.clear()
    client_mod._CACHE_POINT_REJECTED.clear()
    yield
    client_mod._FORCED_TOOL_REJECTED.clear()
    client_mod._CACHE_POINT_REJECTED.clear()


# ── catalog: family and capabilities ─────────────────────────────────────────────────────────────────────────────

def _catalog_row(**kw):
    row = dict(model_key="nova-x", label="Nova", vendor="amazon", provider="bedrock", api_style="converse",
               bedrock_profile_ids='{"acc2": "us.amazon.nova-x"}', wire_model=None, callable_via=["llm_client"],
               supports_temperature=True, max_output_tokens=None, price_in_per_mtok=1, price_out_per_mtok=2,
               price_cache_read_per_mtok=None, price_cache_write_per_mtok=None, enabled=False, blocked_reason=None)
    row.update(kw)
    return row


def test_the_catalog_row_carries_family_and_capabilities():
    m = catalog._row_to_model(_catalog_row(family="amazon", capabilities='{"forced_tool_choice": false}'))
    assert m.family == "amazon" and m.family_key == "amazon"
    assert m.capabilities == {"forced_tool_choice": False}
    assert catalog.capability(m, "forced_tool_choice") is False
    assert catalog.capability(m, "prompt_cache", "unset") == "unset"
    assert catalog.has_capability(m, "forced_tool_choice") and not catalog.has_capability(m, "prompt_cache")


def test_a_row_before_320_reads_its_vendor_as_family():
    m = catalog._row_to_model(_catalog_row())                 # no family, no capabilities column
    assert m.family is None and m.family_key == "amazon" and m.capabilities == {}
    assert catalog.capability(object(), "prompt_cache", 1) == 1     # a test double without capabilities


def test_the_catalog_read_falls_back_before_320():
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[asyncpg.exceptions.UndefinedColumnError("family"), [_catalog_row()]])
    rows = asyncio.run(catalog._fetch_rows(conn, catalog._SELECT_V2_SQL, catalog._SELECT_SQL))
    assert rows == [_catalog_row()]
    assert "capabilities, family" in conn.fetch.call_args_list[0].args[0]
    assert conn.fetch.call_args_list[1].args[0] == catalog._SELECT_SQL


def test_catalog_prices_are_read_only():
    async def connect(*a, **k):
        raise AssertionError("a refused price edit opens no connection")
    with patch.object(catalog.asyncpg, "connect", connect):
        for fields in ({"price_in_per_mtok": 1.0}, {"price_source": "x"}, {"enabled": True, "price_out_per_mtok": 2}):
            with pytest.raises(catalog.CatalogPriceReadOnly, match="migration only"):
                asyncio.run(catalog.set_catalog_price("haiku", fields=fields, updated_by="t", audit_actor="t"))
    assert catalog._CATALOG_EDITABLE_FIELDS == ("enabled",)


def test_the_catalog_route_answers_422_for_a_price():
    from fastapi import HTTPException
    from api.routers import admin_llm_ops as ops

    with patch.object(ops, "verify_admin_secret"):
        with pytest.raises(HTTPException) as e:
            asyncio.run(ops.patch_llm_catalog("haiku", ops.LlmCatalogPatch(price_in_per_mtok=1.0), "s", "u"))
    assert e.value.status_code == 422


# ── capability-driven decisions (fallback = the markers) ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,profile,expected", [
    ("sonnet-5-5", "global.anthropic.claude-sonnet-5-5", False),       # a marker model
    ("haiku-5-5", "global.anthropic.claude-haiku-5-5", True),
    ("gpt-6-luna", "global.openai.gpt-6-luna", True),
])
def test_forced_tool_choice_unset_capability_keeps_the_markers(key, profile, expected):
    assert client_mod._forced_tool_choice_ok(_model(model_key=key), profile) is expected


def test_forced_tool_choice_capability_wins_over_the_markers():
    assert client_mod._forced_tool_choice_ok(
        _model(capabilities={"forced_tool_choice": True}), "global.anthropic.claude-sonnet-5-5") is True
    assert client_mod._forced_tool_choice_ok(
        _model(model_key="kimi", capabilities={"forced_tool_choice": False}), "us.moonshot.kimi") is False
    client_mod._FORCED_TOOL_REJECTED.add("kimi")                 # a runtime rejection still wins
    assert client_mod._forced_tool_choice_ok(
        _model(model_key="kimi", capabilities={"forced_tool_choice": True}), "us.moonshot.kimi") is False


def test_cache_point_capabilities():
    long_prompt = "x" * 4 * 5000
    claude = "global.anthropic.claude-sonnet-5-5"
    assert client_mod._converse_cache_point(_model(), claude, long_prompt) is True               # today's rule
    assert client_mod._converse_cache_point(_model(), "global.openai.gpt-6-luna", long_prompt) is False
    assert client_mod._converse_cache_point(_model(capabilities={"prompt_cache": False}), claude, long_prompt) is False
    assert client_mod._converse_cache_point(_model(model_key="nova", capabilities={"prompt_cache": True}),
                                            "us.amazon.nova-x", long_prompt) is True
    small = "x" * 4 * 600                                       # 600 tokens: above 512 (5.5 family), below 1000
    assert client_mod._converse_cache_point(_model(), claude, small) is True
    assert client_mod._converse_cache_point(_model(capabilities={"cache_min_tokens": 1000}), claude, small) is False


def test_effort_default_capability_replaces_the_family_table():
    assert am.resolve_effort("claude-sonnet-5-5", "s", {}) == "high"                     # the family table
    assert am.resolve_effort("claude-sonnet-5-5", "s", {}, {"effort_default": "low"}) == "low"
    assert am.resolve_effort("claude-sonnet-5-5", "s", {"effort": "max"}, {"effort_default": None}) is None
    assert am.resolve_effort("claude-new-6", "s", {}, {"effort_default": "medium"}) == "medium"
    assert am.resolve_effort("claude-sonnet-5-5", "s", {}, {"effort_default": "weird"}) == "high"   # invalid: ignored
    params = am.build_params(wire_id="claude-new-6", system="s", user="u", max_tokens=10, stage="x",
                             capabilities={"effort_default": "low"})
    assert params["output_config"] == {"effort": "low"}


def test_a_model_without_capabilities_sends_the_same_anthropic_call():
    model = _model()
    conn = ProviderConnection(connection_key="anthropic-direct", kind="anthropic", auth_mode="api_key", label="a",
                              data_scope="vendor_direct", enabled=True, secret_name="aa-llm/dev/anthropic-direct")
    dep = ModelDeployment("sonnet-5-5", "anthropic-direct", "claude-sonnet-5-5", 100, True, "seed",
                          request_defaults={"effort": "medium"})
    result = am.MessagesResult(text="ok", provider_model="m", stop_reason="end_turn", stop_details=None,
                               input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_write_tokens=0,
                               structured=False, effort="medium")
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    with patch.object(am, "send", return_value=result) as send, \
            patch("shared.llm_client.pricing._catalog_rates", return_value=RATES):
        c._call_anthropic_messages(LLMRequest(system_prompt="s", user_prompt="u"), model,
                                   VendorDeployment(dep, conn))
    kwargs = send.call_args.kwargs
    assert "capabilities" not in kwargs and kwargs["request_defaults"] == {"effort": "medium"}
    with patch.object(am, "send", return_value=result) as send, \
            patch("shared.llm_client.pricing._catalog_rates", return_value=RATES):
        c._call_anthropic_messages(LLMRequest(system_prompt="s", user_prompt="u"),
                                   _model(capabilities={"effort_default": "low"}), VendorDeployment(dep, conn))
    assert send.call_args.kwargs["capabilities"] == {"effort_default": "low"}


# ── deployment api_style ─────────────────────────────────────────────────────────────────────────────────────────

def _dep_row(**kw):
    row = dict(model_key="m", connection_key="acc3", wire_id="w", priority=10, enabled=True, origin="seed",
               price_in_override=None, price_out_override=None, price_cache_read_override=None,
               price_cache_write_override=None, price_cache_write_1h_override=None, batch_capable=False,
               request_defaults="{}", notes=None)
    row.update(kw)
    return row


def test_deployment_api_style_is_read_and_null_before_320():
    assert _row_to_deployment(_dep_row(api_style="converse")).api_style == "converse"
    assert _row_to_deployment(_dep_row()).api_style is None
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[asyncpg.exceptions.UndefinedColumnError("api_style"), [_dep_row()]])
    assert [d.api_style for d in asyncio.run(fetch_deployments(conn))] == [None]


def test_dispatch_uses_the_deployment_api_style_over_the_catalog():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    acc3 = ProviderConnection(connection_key="acc3", kind="bedrock", auth_mode="assume_role", label="x",
                              data_scope="aws_only", enabled=True)
    model = _model(api_style="anthropic_native")          # the catalog says one thing ...
    dep = ModelDeployment("sonnet-5-5", "acc3", "global.anthropic.claude-sonnet-5-5", 10, True, "seed",
                          api_style="converse")             # ... the deployment another
    with patch.object(c, "_call_bedrock_converse", return_value="converse") as conv:
        assert c._dispatch_target(LLMRequest(system_prompt="s", user_prompt="u"), model,
                                  VendorDeployment(dep, acc3)) == "converse"
    conv.assert_called_once()
    with pytest.raises(RuntimeError, match="api_style='anthropic_native'"):   # NULL = the catalog's (no native key)
        c._dispatch_target(LLMRequest(system_prompt="s", user_prompt="u"), model,
                           VendorDeployment(ModelDeployment("sonnet-5-5", "acc3", "w", 10, True, "seed"), acc3))


# ── the truncated class ──────────────────────────────────────────────────────────────────────────────────────────

def test_truncated_is_a_recorded_class_not_a_failure():
    assert errors.TRUNCATED == "truncated"
    assert errors.reply_class("max_tokens") == errors.TRUNCATED and errors.reply_class("length") == "truncated"
    assert errors.reply_class("end_turn") is None and errors.reply_class(None) is None
    assert call_log.TRUNCATED_STOP_REASONS is errors.TRUNCATED_STOP_REASONS
    values = dict(zip(call_log.V2_COLUMNS, call_log._v2_values(stop_reason="max_tokens")))
    assert (values["status"], values["error_class"]) == ("truncated", "truncated")
    assert dict(zip(call_log.V2_COLUMNS, call_log._v2_values(stop_reason="end_turn")))["status"] == "ok"
    assert errors.TRUNCATED not in errors.ACCOUNT_LEVEL


# ── connections: kinds, per-kind guard, LLM dispatch kept apart ──────────────────────────────────────────────────

def test_the_provider_kinds_are_not_llm_kinds():
    assert set(connections.KINDS) == set(connections.LLM_KINDS) | set(connections.PROVIDER_KINDS)
    assert not set(connections.LLM_KINDS) & set(connections.PROVIDER_KINDS)
    assert set(connections.KIND_ROLE) == set(connections.PROVIDER_KINDS)
    for row in connections.BUILTIN_PROVIDERS.values():
        assert not row.is_llm and row.policy_problems() == [], row.connection_key
    assert not set(connections.BUILTIN_PROVIDERS) & set(connections.BUILTIN_CONNECTIONS)


@pytest.mark.parametrize("kind,name,ok", [
    ("typesafe", "aa-cis/dev/typesafe", True), ("typesafe", "aa-cis/prod/typesafe", True),
    ("typesafe", "aa-cis/dev/dataforseo", False), ("typesafe", "aa-llm/dev/typesafe", False),
    ("typesafe", "aa-cis/dev/rds", False), ("dataforseo", "aa-cis/dev/dataforseo", True),
    ("geoapify", "aa-cis/dev/geoapify", True), ("bedrock_guardrails", "aa-cis/dev/typesafe", False),
    ("anthropic", "aa-cis/dev/typesafe", False), ("anthropic", "aa-llm/dev/anthropic-direct", True),
    (None, "aa-llm/dev/x", True), (None, "aa-cis/dev/typesafe", False),
])
def test_secret_guard_per_kind(kind, name, ok):
    if ok:
        connections.check_secret_name(name, kind)
    else:
        with pytest.raises(connections.ConnectionPolicyError):
            connections.check_secret_name(name, kind)


@pytest.mark.parametrize("kind,url,ok", [
    ("typesafe", "https://api.typesafe.ai", True), ("typesafe", "https://api.anthropic.com", False),
    ("dataforseo", "https://api.dataforseo.com/v3", True), ("geoapify", "https://api.geoapify.com/v1", True),
    ("geoapify", "https://api.geoapify.com.evil.io", False), ("bedrock_guardrails", "https://x.amazonaws.com", False),
])
def test_host_guard_per_kind(kind, url, ok):
    if ok:
        connections.check_base_url(kind, url)
    else:
        with pytest.raises(connections.ConnectionPolicyError):
            connections.check_base_url(kind, url)


def test_a_provider_row_is_never_an_llm_key_source():
    jev = connections.BUILTIN_PROVIDERS["typesafe"]
    with patch.object(connections, "_fetch_secret") as secret:
        with pytest.raises(connections.ConnectionCredentialError, match="not an LLM connection"):
            connections.resolve_api_key(jev)
    secret.assert_not_called()


def _registry_records():
    rows = []
    for c in (connections.BUILTIN_CONNECTIONS["acc3"], connections.BUILTIN_PROVIDERS["typesafe"]):
        r = {k: getattr(c, k) for k in connections._COLUMNS}
        r.update({k: getattr(c, k) for k in connections.V2_COLUMNS})
        r["served_roles"] = list(c.served_roles) if c.served_roles else None
        r["updated_at"] = None
        rows.append(r)
    return rows


def test_fetch_connections_keeps_the_provider_kinds_out_by_default():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=_registry_records())
    assert set(asyncio.run(connections.fetch_connections(conn))) == {"acc3"}
    everything = asyncio.run(connections.fetch_connections(conn, llm_only=False))
    assert set(everything) == {"acc3", "typesafe"}
    assert everything["typesafe"].served_roles == ("decide",) and everything["typesafe"].low_balance_usd == 3.0
    assert "failure_domain" in conn.fetch.call_args.args[0]


def test_fetch_connections_reads_the_304_columns_before_322():
    rows = [{k: v for k, v in r.items() if k in connections._COLUMNS} for r in _registry_records()]
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[asyncpg.exceptions.UndefinedColumnError("failure_domain"), rows])
    out = asyncio.run(connections.fetch_connections(conn, llm_only=False))
    assert out["typesafe"].access_state == "unknown" and out["typesafe"].low_balance_usd is None


class _Conn:
    """An asyncpg connection stand-in (no ``acquire``: not a pool)."""

    def __init__(self, fetch):
        self.fetch = fetch


def test_provider_connection_never_raises_and_checks_the_key():
    conn = _Conn(AsyncMock(return_value=[_registry_records()[1]]))
    assert asyncio.run(connections.provider_connection(conn, "typesafe")).base_url == "https://api.typesafe.ai"
    assert asyncio.run(connections.provider_connection(conn, "geoapify")) is None          # another row: no
    conn.fetch = AsyncMock(side_effect=OSError("db down"))
    assert asyncio.run(connections.provider_connection(conn, "typesafe")) is None
    assert asyncio.run(connections.provider_connection(MagicMock(), "typesafe")) is None   # a bare test double


def test_provider_value_precedence(monkeypatch):
    row = ProviderConnection(connection_key="geoapify", kind="geoapify", auth_mode="api_key", label="g",
                             data_scope="vendor_direct", daily_credit_limit=1200)
    monkeypatch.setenv("ZZ_LIMIT", "900")
    assert connections.provider_value(row, "daily_credit_limit", "ZZ_LIMIT", 2500) == 1200
    assert connections.provider_value(None, "daily_credit_limit", "ZZ_LIMIT", 2500) == 900
    monkeypatch.setenv("ZZ_LIMIT", "lots")
    assert connections.provider_value(None, "daily_credit_limit", "ZZ_LIMIT", 2500) == 2500


# ── migration 322 agrees with the code ───────────────────────────────────────────────────────────────────────────

def _m322_check(name: str) -> str:
    sql = M322.read_text(encoding="utf-8")
    m = re.search(rf"ADD CONSTRAINT {name} CHECK \((.*?)\) NOT VALID", sql, re.S)
    assert m, name
    return m.group(1)


def test_322_kind_and_auth_mode_lists_are_the_code_lists():
    assert set(re.findall(r"'([^']*)'", _m322_check("llm_provider_connection_kind_check"))) == set(connections.KINDS)
    body = _m322_check("llm_provider_connection_auth_mode_check")
    for kind, modes in connections.AUTH_MODES.items():
        clause = next(c for c in body.split("OR (") if f"'{kind}'" in c)
        assert set(modes) <= set(re.findall(r"'([^']*)'", clause)), kind


def test_322_secret_patterns_are_the_code_patterns():
    body = _m322_check("llm_provider_connection_secret_name_check")
    assert connections.SECRET_NAME_RE.pattern in body
    for kind, pattern in connections.PROVIDER_SECRET_RE.items():
        assert f"kind = '{kind}' AND secret_name ~ '{pattern.pattern}'" in body


def test_322_hosts_are_the_code_hosts():
    body = _m322_check("llm_provider_connection_base_url_check")
    for kind in ("anthropic", "openai", "typesafe", "dataforseo", "geoapify"):
        host = connections.BASE_URL_ALLOWLIST[kind][0]
        assert f"kind = '{kind}' AND base_url ~ '^https://{re.escape(host)}(/|$)'" in body.replace("\\.", "\\."), kind


def test_322_seeds_are_the_builtin_providers():
    sql = M322.read_text(encoding="utf-8")
    seeds = re.search(r"INSERT INTO shared\.llm_provider_connection.*?ON CONFLICT", sql, re.S).group(0)
    for key, row in connections.BUILTIN_PROVIDERS.items():
        start = seeds.index(f"('{key}', '{row.kind}', '{row.auth_mode}', '{row.label}'")
        chunk = seeds[start:start + 900]
        for value in (row.secret_name, row.base_url, row.failure_domain, row.balance_source):
            if value is not None:
                assert f"'{value}'" in chunk, (key, value)
        for role in row.served_roles:
            assert f"ARRAY['{role}']" in chunk, (key, role)
        assert ("true" if row.enabled else "false") in chunk
    assert re.search(r"'https://api\.typesafe\.ai', 'vendor_direct', 3, 2, true", seeds)
    assert "'daily_credit_window', 2500" in seeds and "'provider_api', NULL, 10.00" in seeds
    assert "'topups_minus_spend', NULL, 3.00" in seeds


def test_322_revokes_the_secret_and_endpoint_and_grants_no_identity_column():
    sql = M322.read_text(encoding="utf-8")
    assert ("REVOKE UPDATE (secret_name, base_url, credential_env) ON shared.llm_provider_connection "
            "FROM cis_app;") in sql
    granted = re.search(r"GRANT UPDATE \((.+?)\)\s+ON shared\.llm_provider_connection TO cis_app;", sql).group(1)
    assert {c.strip() for c in granted.split(",")} == {"access_state", "last_probe_at", "daily_credit_limit",
                                                        "low_balance_usd"}


# ── admin edits ──────────────────────────────────────────────────────────────────────────────────────────────────

def _update(db, key, changes):
    return asyncio.run(llm_connections.update_connection(db, key, changes, actor="admin:u-1"))


def test_secret_name_and_base_url_left_the_editable_list():
    assert "secret_name" not in llm_connections.EDITABLE and "base_url" not in llm_connections.EDITABLE
    assert {"daily_credit_limit", "low_balance_usd"} <= set(llm_connections.EDITABLE)


def test_a_provider_row_takes_its_limits_but_no_switch():
    jev = _conn_row(connection_key="typesafe", kind="typesafe", auth_mode="api_key", label="Jev",
                    secret_name="aa-cis/dev/typesafe", base_url="https://api.typesafe.ai", enabled=True)
    db = FakeDB(jev)
    out = _update(db, "typesafe", {"low_balance_usd": 5, "timeout_s": 4})
    assert out["changed_fields"] == ["timeout_s", "low_balance_usd"]
    (upd, args), _audit = db.executed
    assert "low_balance_usd = $3" in upd and str(args[2]) == "5"
    for changes in ({"enabled": False}, {"rpm_limit": 10}, {"monthly_budget_usd": 1}):
        with pytest.raises(llm_connections.ConnectionToggleRefused, match="provider"):
            _update(FakeDB(jev), "typesafe", changes)


def test_the_listing_shows_the_322_fields():
    acc3 = connections.BUILTIN_CONNECTIONS["acc3"]
    out = llm_connections.build({"acc3": acc3}, [])
    row = out["connections"][0]
    assert {"failure_domain", "served_roles", "access_state", "balance_source", "daily_credit_limit",
            "low_balance_usd", "editable_fields"} <= set(row)
    assert "secret_name" not in row["editable_fields"]


# ── provider settings from the rows ──────────────────────────────────────────────────────────────────────────────

def test_jev_endpoint_from_its_row_else_the_constants(monkeypatch):
    monkeypatch.setattr(decide, "_provider", {})
    assert decide.jev_endpoint() == (decide.JEV_URL, decide._TIMEOUT_S, decide._MAX_ATTEMPTS)
    monkeypatch.setattr(decide, "_provider", {"base_url": "https://api.typesafe.ai/", "timeout_s": 5,
                                              "max_retries": 0})
    assert decide.jev_endpoint() == ("https://api.typesafe.ai/v1/systemone", 5.0, 1)
    monkeypatch.setattr(decide, "_provider", {"base_url": "http://plain.example", "timeout_s": None,
                                              "max_retries": None})
    assert decide.jev_endpoint() == (decide.JEV_URL, decide._TIMEOUT_S, decide._MAX_ATTEMPTS)


def test_the_seeded_jev_row_gives_todays_endpoint(monkeypatch):
    row = connections.BUILTIN_PROVIDERS["typesafe"]
    monkeypatch.setattr(decide, "_provider", {"base_url": row.base_url, "timeout_s": row.timeout_s,
                                              "max_retries": row.max_retries})
    monkeypatch.delenv("TYPESAFE_BASE_URL", raising=False)
    assert decide.jev_endpoint() == ("https://api.typesafe.ai/v1/systemone", 3.0, 3)


def test_jev_provider_load_is_cached_and_never_raises(monkeypatch):
    monkeypatch.setattr(decide, "_provider", {})
    monkeypatch.setattr(decide, "_provider_loaded_at", 0.0)
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"base_url": "https://api.typesafe.ai", "timeout_s": 3, "max_retries": 2})
    asyncio.run(decide._load_provider(conn))
    asyncio.run(decide._load_provider(conn))                  # within the TTL: one read
    assert conn.fetchrow.await_count == 1 and decide._provider["timeout_s"] == 3
    monkeypatch.setattr(decide, "_provider_loaded_at", 0.0)
    conn.fetchrow = AsyncMock(side_effect=OSError("no table"))
    asyncio.run(decide._load_provider(conn))
    assert decide._provider["timeout_s"] == 3                 # the last good row stays


def test_jev_low_credit_threshold_from_its_row():
    with patch.object(connections, "provider_connection", AsyncMock(return_value=None)):
        assert asyncio.run(jev_credit.low_credit_threshold(object())) == jev_credit.LOW_CREDIT_THRESHOLD_USD == 3.0
    row = ProviderConnection(connection_key="typesafe", kind="typesafe", auth_mode="api_key", label="j",
                             data_scope="vendor_direct", low_balance_usd=7.5)
    with patch.object(connections, "provider_connection", AsyncMock(return_value=row)):
        assert asyncio.run(jev_credit.low_credit_threshold(object())) == 7.5


def test_dfs_threshold_from_its_row(monkeypatch):
    from api.routers import admin_llm_ops as ops

    with patch.object(connections, "provider_connection", AsyncMock(return_value=None)):
        assert asyncio.run(ops.dfs_balance_threshold(object())) == ops._DFS_BALANCE_ALERT_THRESHOLD_USD
    row = ProviderConnection(connection_key="dataforseo", kind="dataforseo", auth_mode="basic_auth", label="d",
                             data_scope="vendor_direct", low_balance_usd=25.0)
    with patch.object(connections, "provider_connection", AsyncMock(return_value=row)):
        assert asyncio.run(ops.dfs_balance_threshold(object())) == 25.0


def test_geoapify_daily_limit_row_then_env_then_constant(monkeypatch):
    from services.places import geocoder

    monkeypatch.setattr(geocoder, "_registry_daily_limit", None)
    monkeypatch.delenv("GEOAPIFY_DAILY_CREDIT_LIMIT", raising=False)
    assert geocoder.daily_credit_limit() == geocoder.DEFAULT_DAILY_CREDITS == 2500
    monkeypatch.setenv("GEOAPIFY_DAILY_CREDIT_LIMIT", "1000")
    assert geocoder.daily_credit_limit() == 1000
    row = ProviderConnection(connection_key="geoapify", kind="geoapify", auth_mode="api_key", label="g",
                             data_scope="vendor_direct", daily_credit_limit=1800)
    with patch.object(connections, "provider_connection", AsyncMock(return_value=row)):
        assert asyncio.run(geocoder.refresh_daily_credit_limit(object())) == 1800
    assert geocoder.get_geocoder("geoapify").daily_credit_limit == 1800
    with patch.object(connections, "provider_connection", AsyncMock(return_value=None)):   # no row: keep the last
        assert asyncio.run(geocoder.refresh_daily_credit_limit(object())) == 1800
    monkeypatch.setattr(geocoder, "_registry_daily_limit", None)


# ── the Providers page reads the registry rows ───────────────────────────────────────────────────────────────────

def test_provider_rows_take_the_registry_facts():
    jev = connections.BUILTIN_PROVIDERS["typesafe"]
    base = {"key": "jev", "label": "Jev (TypeSafe decisions)", "enabled": True, "status": "ok", "connection_key": None}
    out = gw._with_registry(base, jev)
    assert out["connection_key"] == "typesafe" and out["from_registry"] is True
    assert out["failure_domain"] == "typesafe:typesafe:vendor_direct" and out["balance_source"] == "topups_minus_spend"
    assert gw._with_registry(base, None)["from_registry"] is False
    off = ProviderConnection(connection_key="typesafe", kind="typesafe", auth_mode="api_key", label="Jev",
                             data_scope="vendor_direct", enabled=False)
    assert gw._with_registry(base, off)["status"] == "disabled"
    geo = {"key": "geoapify", "enabled": False, "status": "disabled"}                  # not the configured geocoder
    assert gw._with_registry(geo, connections.BUILTIN_PROVIDERS["geoapify"])["enabled"] is False


# ── readiness ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_readiness_needs_322():
    assert readiness.REQUIRED_CIS_SCHEMA == 322 and {320, 321, 322} <= set(readiness.REQUIRED_CIS_KEYS)
    assert M320.exists() and (MIGRATIONS / "321_tenant_ai_budget_prompt_registry.sql").exists() and M322.exists()
