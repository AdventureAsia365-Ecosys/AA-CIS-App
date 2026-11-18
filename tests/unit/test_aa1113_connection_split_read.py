"""AA-1113 (S284 redesign) — GET /admin/ai-gateway/connection-split (api/services/ai_gateway_split.py): the registry
connections with their CIS share, the provider budget setting and the CRM share read through the CRM settings relay
(crm.ops.v1 GET /settings, area ai, the staff token). No database (a fake registry), no network (a fake relay call);
invented connection keys and amounts only."""
from contextlib import asynccontextmanager

import pytest

from api.core.staff_authz import POLICY, SPEND_READ
from api.services import ai_gateway_split
from shared import cost_guard
from shared.llm_client import connections as conn_registry
from shared.llm_client.connections import ProviderConnection

# The pure rule ships with the CIS 453 migration PR (MIG-4); this code PR runs after main has it.
split = pytest.importorskip("api.services.llm_connection_split", reason="ships with the CIS 453 migration PR")


class _Pool:
    @asynccontextmanager
    async def acquire(self):
        yield object()


def _conn(key, budget):
    return ProviderConnection(connection_key=key, kind="bedrock", auth_mode="assume_role", label=f"Zz {key}",
                              data_scope="global", enabled=True, monthly_budget_usd=budget)


@pytest.fixture
def world(monkeypatch):
    state = {"registry": {"zz-a": _conn("zz-a", 80), "zz-b": _conn("zz-b", None)},
             "provider": {"zz-a": 100}, "crm": (200, {"settings": [{"key": split.CRM_CAP_KEY, "value": {"zz-a": 30}}]}),
             "policies": {}, "calls": []}

    async def fetch_connections(conn, **_):
        return state["registry"]

    async def aget(key, tenant_id=None):
        assert key == split.PROVIDER_BUDGET_KEY
        return state["provider"]

    async def read_cost_policies(conn, **_):
        if isinstance(state["policies"], Exception):
            raise state["policies"]
        return state["policies"]

    async def crm_ops_call(method, path, authorization, *, json=None, params=None):
        state["calls"].append((method, path, authorization, params))
        return state["crm"]

    monkeypatch.setattr(conn_registry, "fetch_connections", fetch_connections)
    monkeypatch.setattr(ai_gateway_split.settings, "aget", aget)
    monkeypatch.setattr(ai_gateway_split.crm_console, "crm_ops_call", crm_ops_call)
    monkeypatch.setattr(ai_gateway_split.cost_guard, "read_cost_policies", read_cost_policies)
    return state


@pytest.mark.asyncio
async def test_the_split_flags_the_over_allocated_connection(world):
    out = await ai_gateway_split.read_connection_split(_Pool(), "Bearer zz")
    assert world["calls"] == [("GET", "/settings", "Bearer zz", {"area": "ai"})]
    rows = {r["connection_key"]: r for r in out["rows"]}
    assert rows["zz-a"]["over_allocated"] is True and rows["zz-a"]["allocated_usd"] == 110.0
    assert rows["zz-a"]["label"] == "Zz zz-a" and rows["zz-a"]["enabled"] is True
    assert rows["zz-b"]["state"] == split.NO_PROVIDER_BUDGET
    assert out["over_allocated"] == ["zz-a"] and out["crm_error"] is None and out["errors"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,code", [
    ((403, {"detail": "forbidden"}), "forbidden"),
    ((503, {}), "crm_status_503"),
    ((200, {"settings": [{"key": "crm.ai.other", "value": 1}]}), ai_gateway_split.CRM_NOT_PUBLISHED),
    ((200, {"settings": [{"key": split.CRM_CAP_KEY, "value": {"zz-a": -1}}]}), ai_gateway_split.CRM_UNREADABLE),
])
async def test_a_crm_share_that_cannot_be_read_is_named_and_never_read_as_zero(world, answer, code):
    world["crm"] = answer
    out = await ai_gateway_split.read_connection_split(_Pool(), "Bearer zz")
    assert out["crm_error"] == code
    row = next(r for r in out["rows"] if r["connection_key"] == "zz-a")
    assert row["crm_known"] is False and row["state"] == split.CRM_UNKNOWN and row["crm_share_usd"] is None


@pytest.mark.asyncio
async def test_a_broken_provider_budget_is_reported_and_checks_nothing(world):
    world["provider"] = ["not", "an", "object"]
    out = await ai_gateway_split.read_connection_split(_Pool(), None)
    assert out["errors"] == [{"source": "provider_budget", "error": "ValueError"}]
    assert {r["state"] for r in out["rows"]} == {split.NO_PROVIDER_BUDGET}


def test_the_route_is_spend_read():
    assert POLICY[("GET", "/admin/ai-gateway/connection-split")] == SPEND_READ


@pytest.mark.asyncio
async def test_the_route_passes_the_pool_and_the_staff_token(world, monkeypatch):
    from types import SimpleNamespace

    from api.services import platform_apps
    monkeypatch.setattr(platform_apps, "relay_authorization", lambda request: "Bearer zz-staff")
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_Pool())))
    out = await ai_gateway_split.read_for(request)
    assert world["calls"][0][2] == "Bearer zz-staff" and out["crm_cap_key"] == split.CRM_CAP_KEY


@pytest.mark.asyncio
async def test_a_cis_share_whose_policy_is_alert_only_is_not_reported_as_fitting(world):
    # Provider 100, CIS 50 with hard_stop false, CRM 30: the numbers fit, but the CIS gateway keeps calling past 50.
    world["registry"]["zz-a"] = _conn("zz-a", 50)
    world["policies"] = {"zz-a": cost_guard.ConnectionCostPolicy(connection_key="zz-a", hard_stop=False, stored=True)}
    out = await ai_gateway_split.read_connection_split(_Pool(), "Bearer zz")
    row = next(r for r in out["rows"] if r["connection_key"] == "zz-a")
    assert row["state"] != split.OK and row["state"] == split.UNBOUNDED
    assert row["uncapped_apps"] == ["cis"] and row["alert_only_apps"] == ["cis"] and row["cis_hard_stop"] is False


@pytest.mark.asyncio
async def test_a_connection_without_a_policy_row_is_a_hard_stop(world):
    world["registry"]["zz-a"] = _conn("zz-a", 50)
    out = await ai_gateway_split.read_connection_split(_Pool(), "Bearer zz")
    row = next(r for r in out["rows"] if r["connection_key"] == "zz-a")
    assert row["state"] == split.OK and row["cis_hard_stop"] is True


@pytest.mark.asyncio
async def test_an_unreadable_policy_never_reports_a_cis_share_as_a_limit(world):
    world["registry"]["zz-a"] = _conn("zz-a", 50)
    world["policies"] = RuntimeError("zz down")
    out = await ai_gateway_split.read_connection_split(_Pool(), "Bearer zz")
    assert {"source": "cost_policy", "error": "RuntimeError"} in out["errors"]
    row = next(r for r in out["rows"] if r["connection_key"] == "zz-a")
    assert row["state"] == split.UNBOUNDED and row["alert_only_apps"] == ["cis"]
