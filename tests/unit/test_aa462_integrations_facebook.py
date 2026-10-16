"""tests/unit/test_aa462_integrations_facebook.py — AA-462: POST/GET /v1/integrations/facebook,
POST /v1/integrations/facebook/test. Mirrors test_aa457_integrations.py's own WordPress
coverage exactly — same mock shapes, same AA-460 don't-trust-bare-200 lesson applied to the
Graph API's own response/error shapes.
"""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from api.routers import v1_integrations as mod

TENANT = {"sub": "6fbaf284-e3cd-4b4b-b53b-c9a04e8fae8e", "role": "tenant"}


def _make_pool(fetchrow=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _make_request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    return req


def _mock_aiohttp_get_session(status, content_type="application/json", body_text=""):
    resp_ctx = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.headers = {"content-type": content_type}
    mock_resp.text = AsyncMock(return_value=body_text)
    resp_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    resp_ctx.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=resp_ctx)
    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session_ctx)


# ── save_facebook ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_facebook_rejects_blank_fields():
    body = mod.SaveFacebookRequest(page_id="  ", page_access_token="tok")
    req = _make_request(_make_pool()[0])
    with pytest.raises(HTTPException) as exc_info:
        await mod.save_facebook(body, req, TENANT)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_save_facebook_success_writes_secret_and_upserts_row():
    row = {
        "config": json.dumps({"page_id": "1234567890"}),
        "connected_at": datetime(2026, 9, 4, tzinfo=timezone.utc),
        "last_verified_at": None, "last_verify_error": None,
    }
    pool, conn = _make_pool(fetchrow=row)
    req = _make_request(pool)
    body = mod.SaveFacebookRequest(page_id="1234567890", page_access_token="EAAtoken123")

    with patch.object(mod, "_put_secret") as mock_put:
        result = await mod.save_facebook(body, req, TENANT)

    mock_put.assert_called_once()
    secret_key_arg, value_arg = mock_put.call_args[0]
    assert secret_key_arg == f"acp/social/{TENANT['sub']}"
    assert value_arg["page_access_token"] == "EAAtoken123"
    assert value_arg["page_id"] == "1234567890"

    # AA-859 W9: integration_type is a parameter ($2) of the shared connect UPSERT
    sql, tenant_id_param, type_param, config_param, secret_key_param = conn.fetchrow.call_args[0]
    assert tenant_id_param == TENANT["sub"]
    assert secret_key_param == f"acp/social/{TENANT['sub']}"
    assert sql == mod._CONNECT_UPSERT_SQL and type_param == "facebook"
    assert result["connected"] is True
    assert result["page_id"] == "1234567890"


# ── get_facebook_status ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_facebook_status_not_connected():
    pool, _ = _make_pool(fetchrow=None)
    req = _make_request(pool)
    result = await mod.get_facebook_status(req, TENANT)
    assert result["connected"] is False


@pytest.mark.asyncio
async def test_get_facebook_status_connected():
    row = {
        "config": json.dumps({"page_id": "1234567890"}),
        "connected_at": datetime(2026, 9, 4, tzinfo=timezone.utc),
        "last_verified_at": datetime(2026, 9, 4, tzinfo=timezone.utc), "last_verify_error": None,
    }
    pool, conn = _make_pool(fetchrow=row)
    req = _make_request(pool)
    result = await mod.get_facebook_status(req, TENANT)
    assert result["connected"] is True
    assert result["page_id"] == "1234567890"
    assert conn.fetchrow.call_args[0][1] == TENANT["sub"]


# ── test_facebook (connection test) ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_test_facebook_404_when_not_connected():
    pool, _ = _make_pool(fetchrow=None)
    req = _make_request(pool)
    with pytest.raises(HTTPException) as exc_info:
        await mod.test_facebook(req, TENANT)
    assert exc_info.value.status_code == 404


def _fb_row():
    return {"config": json.dumps({"page_id": "1234567890"}), "secret_key": f"acp/social/{TENANT['sub']}"}


@pytest.mark.asyncio
async def test_test_facebook_success_sets_last_verified_at():
    updated_row = {
        "config": json.dumps({"page_id": "1234567890"}),
        "connected_at": datetime(2026, 9, 4, tzinfo=timezone.utc),
        "last_verified_at": datetime(2026, 9, 4, tzinfo=timezone.utc), "last_verify_error": None,
    }
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_fb_row(), updated_row])
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    req = _make_request(pool)

    page_json = json.dumps({"id": "1234567890", "name": "Test Page"})
    with patch.object(mod, "_get_secret", return_value={
            "page_id": "1234567890", "page_access_token": "EAAtoken"}), \
         patch("aiohttp.ClientSession", _mock_aiohttp_get_session(200, body_text=page_json)):
        result = await mod.test_facebook(req, TENANT)

    assert result["success"] is True
    assert result["last_verify_error"] is None
    update_sql = conn.fetchrow.call_args_list[1][0][0]
    assert "last_verified_at = now()" in update_sql


@pytest.mark.asyncio
async def test_test_facebook_expired_token_classified():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_fb_row(), {
        "config": json.dumps({"page_id": "1234567890"}),
        "connected_at": datetime(2026, 9, 4, tzinfo=timezone.utc),
        "last_verified_at": None, "last_verify_error": "Invalid or expired Page Access Token",
    }])
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    req = _make_request(pool)

    error_json = json.dumps({"error": {"code": 190, "message": "Error validating access token"}})
    with patch.object(mod, "_get_secret", return_value={
            "page_id": "1234567890", "page_access_token": "expired"}), \
         patch("aiohttp.ClientSession", _mock_aiohttp_get_session(400, body_text=error_json)):
        result = await mod.test_facebook(req, TENANT)

    assert result["success"] is False
    assert "expired" in result["last_verify_error"].lower()


@pytest.mark.asyncio
async def test_test_facebook_secret_read_failure_502():
    pool, _ = _make_pool(fetchrow=_fb_row())
    req = _make_request(pool)
    error = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "GetSecretValue")
    with patch.object(mod, "_get_secret", side_effect=error):
        with pytest.raises(HTTPException) as exc_info:
            await mod.test_facebook(req, TENANT)
    assert exc_info.value.status_code == 502
