"""AA-1017 — the registry v2 code's own SQL against real Postgres with migrations 320 / 321 / 322 applied (conftest).

The catalog read names family + capabilities, the deployment read names api_style, the connection read names the 322
columns and keeps the provider kinds out by default, the provider settings read one row, the stage x model read runs,
and the Providers page connection read splits LLM and provider rows. Every value read equals today's (the seeds).
"""
import asyncio
import os
import sys

import asyncpg
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from shared import jev_credit  # noqa: E402
from shared.llm_client import catalog, connections, deployments, stage_params  # noqa: E402


def _adb(fn):
    async def go():
        conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASS)
        try:
            return await fn(conn)
        finally:
            await conn.close()
    return asyncio.run(go())


@pytest.fixture(autouse=True)
def _migrated():
    async def check(conn):
        return await conn.fetchval("SELECT count(*) AS n FROM shared.schema_versions "
                                   "WHERE version IN ('320', '321', '322')")
    if _adb(check) != 3:
        pytest.skip("migrations 320 / 321 / 322 are not in this DB")


def test_the_catalog_read_names_family_and_capabilities():
    rows = _adb(lambda c: catalog._fetch_rows(c, catalog._SELECT_V2_SQL, catalog._SELECT_SQL))
    models = [catalog._row_to_model(r) for r in rows]
    assert models and all(m.family == m.vendor for m in models)
    assert all(isinstance(m.capabilities, dict) for m in models)
    full = _adb(lambda c: catalog._fetch_rows(c, catalog._FULL_SELECT_V2_SQL, catalog._FULL_SELECT_SQL))
    assert all(catalog._row_to_dict(r)["family"] for r in full)


def test_the_deployment_read_names_api_style():
    deps = _adb(deployments.fetch_deployments)
    assert deps and all(d.api_style is None for d in deps)


def test_the_connection_read_keeps_the_provider_kinds_out():
    llm = _adb(connections.fetch_connections)
    every = _adb(lambda c: connections.fetch_connections(c, llm_only=False))
    assert {"typesafe", "dataforseo", "geoapify", "bedrock-guardrails"} <= set(every) - set(llm)
    assert all(c.is_llm for c in llm.values())
    for key, builtin in connections.BUILTIN_PROVIDERS.items():
        row = every[key]
        for field in ("kind", "auth_mode", "label", "secret_name", "base_url", "data_scope", "enabled", "timeout_s",
                      "max_retries", "failure_domain", "served_roles", "balance_source", "daily_credit_limit",
                      "low_balance_usd", "region"):
            assert getattr(row, field) == getattr(builtin, field), (key, field)
        assert row.policy_problems() == [], key
    assert llm["acc2"].served_roles == ("embed", "judge", "validate")


def test_provider_settings_read_their_rows():
    jev = _adb(lambda c: connections.provider_connection(c, "typesafe"))
    assert (jev.base_url, jev.timeout_s, jev.max_retries) == ("https://api.typesafe.ai", 3, 2)
    assert _adb(jev_credit.low_credit_threshold) == 3.0
    assert _adb(lambda c: connections.provider_connection(c, "nope")) is None


def test_the_jev_provider_query_runs():
    from shared.llm_client import decide

    row = _adb(lambda c: c.fetchrow(decide._PROVIDER_SQL, decide.JEV_CONNECTION_KEY))
    assert dict(row) == {"base_url": "https://api.typesafe.ai", "timeout_s": 3, "max_retries": 2}


def test_the_stage_params_read_runs_on_the_empty_table():
    assert _adb(stage_params.fetch_params) == {}


def test_the_geoapify_limit_comes_from_its_row(monkeypatch):
    from services.places import geocoder

    monkeypatch.setattr(geocoder, "_registry_daily_limit", None)
    monkeypatch.setenv("GEOAPIFY_DAILY_CREDIT_LIMIT", "10")             # the row wins over the env
    assert _adb(geocoder.refresh_daily_credit_limit) == 2500
    monkeypatch.setattr(geocoder, "_registry_daily_limit", None)
