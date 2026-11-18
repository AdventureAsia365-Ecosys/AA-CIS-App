"""AA-1113 (S284 redesign) — GET /admin/ai-gateway/connection-split (api/services/ai_gateway_split.py): the registry
connections with their CIS share, the provider budget setting and the CRM share read through the CRM settings relay
(crm.ops.v1 GET /settings, area ai, the staff token). No database (a fake registry), no network (a fake relay call);
invented connection keys and amounts only."""
from contextlib import asynccontextmanager

import pytest

from api.core.staff_authz import POLICY, SPEND_READ
from api.services import ai_gateway_split, llm_connection_split as split
from shared.llm_client import connections as conn_registry
from shared.llm_client.connections import ProviderConnection


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
             "calls": []}

    async def fetch_connections(conn, **_):
        return state["registry"]

    async def aget(key, tenant_id=None):
        assert key == split.PROVIDER_BUDGET_KEY
        return state["provider"]

    async def crm_ops_call(method, path, authorization, *, json=None, params=None):
        state["calls"].append((method, path, authorization, params))
        return state["crm"]

    monkeypatch.setattr(conn_registry, "fetch_connections", fetch_connections)
    monkeypatch.setattr(ai_gateway_split.settings, "aget", aget)
    monkeypatch.setattr(ai_gateway_split.crm_console, "crm_ops_call", crm_ops_call)
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
