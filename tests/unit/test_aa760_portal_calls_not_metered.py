"""AA-760 — portal page loads must not count as API calls.

`shared.tenant_api_usage` is the "API calls" figure on the tenant's Billing page and Dashboard
(`shared.v_tenant_monthly_usage.api_calls_used`). Every portal page fires several `/v1/*` calls through the
portal BFF with the tenant's session token, so a tenant who only used the portal saw hundreds of "API calls".

The fix: the portal's sign-in asks `POST /auth/tenant-login` for a token with `client="portal"`; the rate limiter
still rate-limits a portal session but does not meter it. A token minted without the claim (every machine caller,
every token minted before AA-760) is metered exactly as before. Each portal call is still logged
(`request_completed` carries `tenant_id` + `client`).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import api.main as main
from api.middleware import rate_limit
from api.middleware.request_id import RequestIdMiddleware
from api.routers.auth import (
    JWT_ALG, JWT_SECRET, PORTAL_CLIENT, TenantLoginRequest, _create_jwt, is_portal_session, verify_tenant_jwt,
)
from tests.unit.test_aa871_request_id_json_logs import json_lines  # noqa: F401  (pytest fixture)

REPO = Path(__file__).resolve().parents[2]
TENANT_ID = "9fb0a3db-59aa-468a-a082-ded01ac50bee"


def _decode(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])


# --- token minting ---------------------------------------------------------------------------------------------------


def test_portal_token_carries_the_client_claim():
    payload = _decode(_create_jwt(TENANT_ID, "WanderLux", "starter", client="portal"))
    assert payload["client"] == PORTAL_CLIENT == "portal"
    assert payload["role"] == "tenant"
    assert is_portal_session(payload)
    # still an ordinary tenant token for every route
    assert verify_tenant_jwt(_create_jwt(TENANT_ID, "WanderLux", "starter", client="portal"))["sub"] == TENANT_ID


@pytest.mark.parametrize("client", [None, "", "api", "Portal", "portal "])
def test_any_other_token_is_a_metered_api_token(client):
    payload = _decode(_create_jwt(TENANT_ID, "WanderLux", "starter", client=client))
    assert "client" not in payload
    assert not is_portal_session(payload)


def test_login_request_accepts_only_the_portal_client():
    assert TenantLoginRequest(api_key="cis_" + "x" * 20).client is None
    assert TenantLoginRequest(api_key="cis_" + "x" * 20, client="portal").client == "portal"
    with pytest.raises(ValidationError):
        TenantLoginRequest(api_key="cis_" + "x" * 20, client="api")


def test_client_field_is_not_in_the_public_schema():
    assert "client" not in TenantLoginRequest.model_json_schema()["properties"]


def _login_conn():
    conn = AsyncMock()
    conn.fetchrow.return_value = {"tenant_id": TENANT_ID, "name": "WanderLux", "plan_tier": "starter"}
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _no_redis_request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=None)), headers={}, client=None)


@pytest.mark.asyncio
async def test_portal_login_mints_a_portal_session_and_says_so_in_the_audit_log():
    with patch.object(main, "write_audit_log", new=AsyncMock()) as audit:
        body = TenantLoginRequest(api_key="cis_" + "x" * 20, client="portal")
        result = await main.tenant_login(_no_redis_request(), body, db=_login_conn())
    assert is_portal_session(_decode(result.token))
    assert audit.call_args.kwargs["details"] == {"login_method": "api_key", "client": "portal"}


@pytest.mark.asyncio
async def test_machine_login_is_unchanged():
    with patch.object(main, "write_audit_log", new=AsyncMock()) as audit:
        body = TenantLoginRequest(api_key="cis_" + "x" * 20)
        result = await main.tenant_login(_no_redis_request(), body, db=_login_conn())
    assert not is_portal_session(_decode(result.token))
    assert audit.call_args.kwargs["details"] == {"login_method": "api_key"}


# --- metering --------------------------------------------------------------------------------------------------------


def _request(count: int = 1):
    request = MagicMock()
    request.url.path = "/v1/tours/pool"
    request.method = "GET"
    request.headers = {"Authorization": "Bearer t"}
    request.app.state.redis = AsyncMock()
    request.app.state.redis.get = AsyncMock(return_value=json.dumps({"rate_limit_rpm": 60, "is_active": True}))
    request.app.state.redis.incr = AsyncMock(return_value=count)
    request.app.state.redis.expire = AsyncMock()
    request.app.state.pool = MagicMock()
    return request


def _payload(portal: bool) -> dict:
    p = {"sub": TENANT_ID, "plan_tier": "starter", "role": "tenant"}
    if portal:
        p["client"] = "portal"
    return p


@pytest.mark.asyncio
@pytest.mark.parametrize("portal, metered", [(True, False), (False, True)])
async def test_only_machine_calls_are_metered(portal, metered):
    request = _request()
    call_next = AsyncMock(return_value=MagicMock(status_code=200, headers={}))
    with patch.object(rate_limit, "verify_jwt", return_value=_payload(portal)), \
         patch.object(rate_limit, "track_api_call", new=AsyncMock()) as track:
        resp = await rate_limit.rate_limit_middleware(request, call_next)
    assert resp.status_code == 200
    call_next.assert_awaited_once()
    assert track.await_count == (1 if metered else 0)
    assert request.state.tenant_client == ("portal" if portal else "api")
    # the rate-limit headers are set either way
    assert resp.headers["X-RateLimit-Limit"] == "60"


@pytest.mark.asyncio
@pytest.mark.parametrize("portal, metered", [(True, False), (False, True)])
async def test_portal_session_is_still_rate_limited_but_its_429_is_not_metered(portal, metered):
    request = _request(count=61)
    call_next = AsyncMock()
    with patch.object(rate_limit, "verify_jwt", return_value=_payload(portal)), \
         patch.object(rate_limit, "track_api_call", new=AsyncMock()) as track:
        resp = await rate_limit.rate_limit_middleware(request, call_next)
    assert resp.status_code == 429
    call_next.assert_not_awaited()
    assert track.await_count == (1 if metered else 0)


@pytest.mark.parametrize("portal, client", [(True, "portal"), (False, "api")])
def test_every_portal_call_is_still_logged_with_its_client(monkeypatch, json_lines, portal, client):  # noqa: F811
    monkeypatch.setattr(rate_limit, "verify_jwt", lambda _t: _payload(portal))
    monkeypatch.setattr(rate_limit, "_get_tenant_meta", AsyncMock(return_value={"rate_limit_rpm": 60,
                                                                                 "is_active": True}))
    track = AsyncMock()
    monkeypatch.setattr(rate_limit, "track_api_call", track)
    app = FastAPI()

    @app.get("/v1/_who")
    async def _who() -> dict:
        return {"ok": True}

    app.middleware("http")(rate_limit.rate_limit_middleware)
    app.add_middleware(RequestIdMiddleware)
    app.state.redis = MagicMock(incr=AsyncMock(return_value=1), expire=AsyncMock())
    app.state.pool = MagicMock()
    with TestClient(app) as tc:
        assert tc.get("/v1/_who", headers={"Authorization": "Bearer t"}).status_code == 200
    done = next(x for x in json_lines() if x["event"] == "request_completed")
    assert done["tenant_id"] == TENANT_ID and done["client"] == client
    assert track.await_count == (0 if portal else 1)
    structlog.contextvars.clear_contextvars()


# --- the portal asks for a portal session ----------------------------------------------------------------------------


def test_portal_sign_in_bff_requests_a_portal_session():
    src = (REPO / "portal/app/api/auth/tenant-login/route.ts").read_text()
    assert 'JSON.stringify({ api_key: apiKey, client: "portal" })' in src


def test_billing_page_says_what_api_calls_count():
    src = (REPO / "portal/app/portal/_components/PlaceholderTabs.tsx").read_text()
    assert "Using this" in src and "portal, including the API playground, is not counted" in src
