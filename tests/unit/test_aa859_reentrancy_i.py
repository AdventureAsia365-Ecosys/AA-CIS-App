"""AA-859 W9 (PR-I) — reentrancy stub entries for integration status / connect / test-connection.

Same check as test_aa859_tenant_pool_reentrancy.py (its stub pool raises on a nested tenant acquire by the
task that already holds one), plus: no tenant connection is held while Secrets Manager or the remote HTTP
call runs (connect: the secret is written before the one block; test: GetSecretValue and the GET run
between block 1 and block 2).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import v1_integrations as integ_mod
from tests.unit.test_aa859_tenant_pool_reentrancy import TENANT_ID, _ReentrancyGuardPool, _conn, _request

_ROW = {"config": {"site_url": "https://blog.example.com", "page_id": "123"}, "secret_key": "acp/x",
        "connected_at": None, "last_verified_at": None, "last_verify_error": None}
_WP_CREDS = {"wp_url": "https://blog.example.com", "username": "u", "app_password": "p"}
_FB_CREDS = {"page_id": "123", "page_access_token": "t"}


def _http(check=None):
    """aiohttp.ClientSession() stand-in; `check()` runs when the remote GET is entered."""
    resp = MagicMock()
    resp.status = 200
    resp.headers = {"content-type": "application/json"}
    resp.text = AsyncMock(return_value='{"id": "1"}')

    async def enter(*_a):
        if check:
            check()
        return resp

    resp_ctx = MagicMock()
    resp_ctx.__aenter__ = AsyncMock(side_effect=enter)
    resp_ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get = MagicMock(return_value=resp_ctx)
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session_ctx)


def _no_connection_held(request):
    """Records the tenant-pool holders at each Secrets Manager / HTTP call into `pool.held_seen`.

    It records instead of raising: the /test routes catch every Exception around the remote GET (a
    raised AssertionError would become a failed verify and the request would still return normally),
    so the check is read after the handler returns, outside the route's exception handling."""
    pool = request.app.state.tenant_pool
    pool.held_seen = getattr(pool, "held_seen", [])

    def check(*_a, **_k):
        pool.held_seen.append(set(pool.holders))
    return check


async def _status_wp(request):
    return await integ_mod.get_wordpress_status(request, tenant={"sub": TENANT_ID})


async def _status_fb(request):
    return await integ_mod.get_facebook_status(request, tenant={"sub": TENANT_ID})


async def _connect_wp(request):
    held = _no_connection_held(request)
    with patch.object(integ_mod, "_put_secret", side_effect=held):
        return await integ_mod.save_wordpress(
            integ_mod.SaveWordPressRequest(wp_url="https://blog.example.com", username="u", app_password="p"),
            request, tenant={"sub": TENANT_ID})


async def _connect_fb(request):
    held = _no_connection_held(request)
    with patch.object(integ_mod, "_put_secret", side_effect=held):
        return await integ_mod.save_facebook(integ_mod.SaveFacebookRequest(page_id="123", page_access_token="t"),
                                             request, tenant={"sub": TENANT_ID})


async def _test_wp(request):
    held = _no_connection_held(request)

    def secret(_key):
        held()
        return dict(_WP_CREDS)
    with patch.object(integ_mod, "_get_secret", side_effect=secret), \
            patch.object(integ_mod.aiohttp, "ClientSession", _http(held)):
        return await integ_mod.test_wordpress(request, tenant={"sub": TENANT_ID})


async def _test_fb(request):
    held = _no_connection_held(request)

    def secret(_key):
        held()
        return dict(_FB_CREDS)
    with patch.object(integ_mod, "_get_secret", side_effect=secret), \
            patch.object(integ_mod.aiohttp, "ClientSession", _http(held)):
        return await integ_mod.test_facebook(request, tenant={"sub": TENANT_ID})


# (handler, row its queries return, tenant-pool acquires one request makes,
#  Secrets Manager / HTTP calls the no-connection-held check sees)
HANDLERS = [
    pytest.param(_status_wp, _ROW, 1, 0, id="GET /v1/integrations/wordpress"),
    pytest.param(_status_fb, _ROW, 1, 0, id="GET /v1/integrations/facebook"),
    pytest.param(_connect_wp, _ROW, 1, 1, id="POST /v1/integrations/wordpress"),
    pytest.param(_connect_fb, _ROW, 1, 1, id="POST /v1/integrations/facebook"),
    pytest.param(_test_wp, _ROW, 2, 2, id="POST /v1/integrations/wordpress/test"),
    pytest.param(_test_fb, _ROW, 2, 2, id="POST /v1/integrations/facebook/test"),
]


@pytest.mark.parametrize("handler,row,expected_acquires,expected_calls", HANDLERS)
@pytest.mark.asyncio
async def test_handler_never_nests_tenant_pool_acquires(handler, row, expected_acquires, expected_calls):
    pool = _ReentrancyGuardPool(_conn(row))
    result = await handler(_request(pool))
    assert pool.violations == [], "nested tenant-pool acquire (self-deadlock at max_size 1)"
    assert pool.acquires == expected_acquires
    assert pool.holders == set(), "every tenant connection was released"
    seen = getattr(pool, "held_seen", [])
    assert all(h == set() for h in seen), "a tenant connection is held across Secrets Manager / HTTP"
    if expected_calls:
        assert len(seen) == expected_calls, "the Secrets Manager / HTTP stand-ins were not all reached"
    if "success" in result:
        assert result["success"] is True, "the mocked verify must succeed (a swallowed exception fails it)"


@pytest.mark.asyncio
async def test_held_check_catches_a_connection_held_across_the_secret_call():
    """Mutation check: the no-connection-held record really sees a holder inside a tenant block."""
    pool = _ReentrancyGuardPool(_conn(_ROW))
    request = _request(pool)
    held = _no_connection_held(request)
    async with integ_mod.acquire_scoped_conn(request, TENANT_ID, integ_mod.INTEGRATION_CONNECT_FLAG,
                                             fail_closed=True):
        held()
    assert pool.held_seen and pool.held_seen[0] != set()


@pytest.mark.parametrize("handler", [_test_wp, _test_fb], ids=["wordpress", "facebook"])
@pytest.mark.asyncio
async def test_held_check_catches_a_connection_held_across_the_remote_get(handler, monkeypatch):
    """Mutation check through the route itself: block 2 opened before the remote GET (acquire -> GET ->
    UPDATE; still 2 acquires, nothing nested, all released). The route swallows any exception around the
    GET, so only the recorded holders show it."""
    real_session = None

    def session_inside_a_tenant_block(*a, **k):
        ctx = real_session(*a, **k)
        outer = ctx.__aenter__

        async def enter():
            enter.cm = integ_mod.acquire_scoped_conn(request, TENANT_ID, integ_mod.INTEGRATION_CONNECT_FLAG,
                                                     fail_closed=True)
            await enter.cm.__aenter__()
            return await outer()

        async def leave(*exc):
            await enter.cm.__aexit__(None, None, None)
            return False
        ctx.__aenter__ = AsyncMock(side_effect=enter)
        ctx.__aexit__ = AsyncMock(side_effect=leave)
        return ctx

    pool = _ReentrancyGuardPool(_conn(_ROW))
    request = _request(pool)
    held = _no_connection_held(request)
    real_session = _http(held)
    secret = _WP_CREDS if handler is _test_wp else _FB_CREDS
    with patch.object(integ_mod, "_get_secret", return_value=dict(secret)), \
            patch.object(integ_mod.aiohttp, "ClientSession", side_effect=session_inside_a_tenant_block):
        await (integ_mod.test_wordpress if handler is _test_wp else integ_mod.test_facebook)(
            request, tenant={"sub": TENANT_ID})
    assert pool.violations == [] and pool.holders == set()
    assert any(h != set() for h in pool.held_seen), "the record must see the connection held across the GET"
