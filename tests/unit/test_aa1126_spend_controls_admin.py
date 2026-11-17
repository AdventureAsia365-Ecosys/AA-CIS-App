"""AA-1126 (S283) — the admin side of the gateway's spend controls: GET /admin/ai-gateway/spend-controls (the settings
in force of every caps_settings key with its Platform › Settings link, one source of truth) and POST
/admin/ai-gateway/anomaly-breaker/{app}/reset (ai.admin, audited, refused to a tighten-only role), the page-access hint
``anomaly_reset``, the connection cap mode on the Budgets read. No database: fakes only; invented values."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.core import staff_authz
from api.routers import admin_ai_gateway as gw
from api.routers import admin_ai_gateway_spend as gws
from api.services import ai_gateway_spend_controls as sc
from shared import settings
from shared.llm_client import anomaly_breaker, app_profile, caps_settings


def test_the_routes_are_in_the_policy_and_the_page_hint():
    assert staff_authz.POLICY[("GET", "/admin/ai-gateway/spend-controls")] == staff_authz.SPEND_READ
    assert staff_authz.POLICY[("POST", "/admin/ai-gateway/anomaly-breaker/{app}/reset")] == staff_authz.AI_ADMIN
    assert gw.PAGE_WRITES["anomaly_reset"] == ("POST", "/admin/ai-gateway/anomaly-breaker/{app}/reset")


def test_the_settings_link_opens_the_spend_area_of_cis():
    assert sc.settings_href() == "/admin/platform/settings?app=cis&area=spend"
    assert sc.settings_href(caps_settings.ANOMALY_MULTIPLE) == \
        "/admin/platform/settings?app=cis&area=spend&key=cis.llm.anomaly_multiple"


def _fake_registry(monkeypatch, known: dict):
    """``known``: key -> (value in force, source). Every other key is not registered."""
    def definition(key):
        if key not in known:
            raise KeyError(key)
        return SimpleNamespace(label=f"label of {key}", description="d", value_type="usd", unit="USD/run",
                               allowed_values=None, min_value=0.05, max_value=20.0, default_value=2)

    def resolve(key):
        value, source = known[key]
        return SimpleNamespace(value=value, source=source, version=1 if source == "platform" else None)
    monkeypatch.setattr(settings, "definition", definition)
    monkeypatch.setattr(settings, "resolve", resolve)


def test_every_spend_control_key_is_listed_with_its_value_source_and_link(monkeypatch):
    _fake_registry(monkeypatch, {k: (3, "platform") for k in caps_settings.ALL})
    rows, missing = sc.setting_rows()
    assert [r["key"] for r in rows] == list(caps_settings.ALL) and missing == []
    for r in rows:
        assert r["value"] == 3 and r["source"] == "platform" and r["href"] == sc.settings_href(r["key"])


def test_a_key_not_registered_yet_is_named_missing(monkeypatch):
    _fake_registry(monkeypatch, {caps_settings.CONNECTION_CAP_MODE: ("hard_stop", "default")})
    rows, missing = sc.setting_rows()
    assert [r["key"] for r in rows] == [caps_settings.CONNECTION_CAP_MODE]
    assert set(missing) == set(caps_settings.ALL) - {caps_settings.CONNECTION_CAP_MODE}


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn=None):
        self.conn = conn or SimpleNamespace()

    def acquire(self):
        return _Acquire(self.conn)


def test_the_read_carries_the_breaker_and_the_cap_mode(monkeypatch):
    _fake_registry(monkeypatch, {})
    monkeypatch.setattr(caps_settings, "_get", lambda key: {caps_settings.CONNECTION_CAP_MODE: "failover_continue"}
                        .get(key))

    async def status(conn, now=None):
        return {"mode": "enforce", "policy": None, "apps": [{"app": "cis", "tripped": True}]}

    async def refresh(force=False, pool=None):
        return True
    monkeypatch.setattr(anomaly_breaker, "status", status)
    monkeypatch.setattr(settings.READER, "refresh", refresh)
    out = asyncio.run(sc.read(_Pool()))
    assert out["connection_cap_mode"] == "failover_continue" and out["anomaly"]["apps"][0]["tripped"] is True
    assert out["settings_href"] == sc.settings_href() and out["errors"] == []


def test_a_breaker_read_error_still_shows_the_settings(monkeypatch):
    _fake_registry(monkeypatch, {k: (1, "default") for k in caps_settings.ALL})

    async def status(conn, now=None):
        raise RuntimeError("ledger unreadable")

    async def refresh(force=False, pool=None):
        return False
    monkeypatch.setattr(anomaly_breaker, "status", status)
    monkeypatch.setattr(settings.READER, "refresh", refresh)
    out = asyncio.run(sc.read(_Pool()))
    assert len(out["settings"]) == len(caps_settings.ALL) and out["errors"][0].startswith("anomaly breaker")


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ResetConn:
    def __init__(self):
        self.executed: list = []

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql, *args):
        assert "SET anomaly_reset_at = now(), anomaly_reset_by = $2" in sql
        return {"row": {"app": args[0], "anomaly_reset_at": "2026-11-17T10:00:00+00:00", "anomaly_reset_by": args[1]}}

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def test_the_reset_writes_the_profile_and_its_audit_row(monkeypatch):
    monkeypatch.setattr(caps_settings, "_get", lambda key: None)          # breaker settings unreadable: no verdict
    conn = _ResetConn()
    out = asyncio.run(sc.reset(_Pool(conn), "crm", actor="admin:zz"))
    assert out["profile"]["anomaly_reset_by"] == "admin:zz" and out["profile"]["app"] == "crm"
    (sql, args), = conn.executed
    assert "acp_shared.audit_log" in sql and args[1] == "llm_anomaly_breaker_reset" and args[2] == "crm"
    assert json.loads(args[3]) == {"cleared": {}}


def test_the_reset_refuses_an_app_without_ai():
    with pytest.raises(LookupError):
        asyncio.run(sc.reset(_Pool(_ResetConn()), "booking", actor="admin:zz"))


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=_Pool(_ResetConn()))))


def test_a_tighten_only_role_may_not_reset(monkeypatch):
    monkeypatch.setattr(gws.settings_registry, "tighten_only", lambda role: role == "ai_ops")
    with pytest.raises(HTTPException) as e:
        asyncio.run(gws.reset_anomaly_breaker(_request(), app="cis", x_admin_user_id="u1", x_admin_role="ai_ops"))
    assert e.value.status_code == 403


def test_the_route_maps_an_unknown_app_and_a_missing_migration(monkeypatch):
    monkeypatch.setattr(gws.settings_registry, "tighten_only", lambda role: False)
    with pytest.raises(HTTPException) as e:
        asyncio.run(gws.reset_anomaly_breaker(_request(), app="booking", x_admin_user_id="u1", x_admin_role="admin"))
    assert e.value.status_code == 404

    async def unavailable(pool, app, *, actor, details=None):
        raise app_profile.AppProfileUnavailable("the anomaly breaker reset needs migration 446")
    monkeypatch.setattr(app_profile, "reset_anomaly", unavailable)
    monkeypatch.setattr(caps_settings, "_get", lambda key: None)
    with pytest.raises(HTTPException) as e:
        asyncio.run(gws.reset_anomaly_breaker(_request(), app="cis", x_admin_user_id="u1", x_admin_role="admin"))
    assert e.value.status_code == 503 and "446" in e.value.detail
