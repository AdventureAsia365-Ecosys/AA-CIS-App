"""
tests/unit/test_aa457_integrations.py — AA-457 [T11 PR1]: shared.tenant_integrations,
POST/GET /v1/integrations/wordpress, POST /v1/integrations/wordpress/test.

Mocks asyncpg via pool.acquire() (matches v1_publish.py/admin_a4.py's own connection style) and
boto3 at the module level and shared.safe_http.request (AA-875) — no live AWS/network calls in unit tests.
"""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from api.routers import v1_integrations as mod
from tests.unit.safe_http_fakes import TARGET, fake_request


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
    req.app.state.redis.get = AsyncMock(return_value="false")  # S241 D-12: explicit OFF
    return req


TENANT = {"sub": "6fbaf284-e3cd-4b4b-b53b-c9a04e8fae8e", "role": "tenant"}
OTHER_TENANT = {"sub": "48a63db8-731b-45cf-96ac-d4a1be9ba440", "role": "tenant"}


# ── _normalize_wp_url (AA-875: static checks from shared.safe_http.validate_url) ──────────────

def test_normalize_wp_url_accepts_valid_https():
    assert mod._normalize_wp_url("https://example-tenant-blog.com/") == "https://example-tenant-blog.com"


def test_normalize_wp_url_rejects_http():
    with pytest.raises(ValueError, match="https"):
        mod._normalize_wp_url("http://example.com")


def test_normalize_wp_url_rejects_localhost():
    with pytest.raises(ValueError, match="local"):
        mod._normalize_wp_url("https://localhost/")


def test_normalize_wp_url_rejects_loopback_ip():
    with pytest.raises(ValueError, match="private or reserved"):
        mod._normalize_wp_url("https://127.0.0.1/")


def test_normalize_wp_url_rejects_private_range():
    with pytest.raises(ValueError, match="private or reserved"):
        mod._normalize_wp_url("https://192.168.1.5/")


def test_normalize_wp_url_rejects_link_local_metadata_ip():
    """169.254.169.254 — the AWS/GCP cloud metadata endpoint, the classic SSRF target."""
    with pytest.raises(ValueError, match="private or reserved"):
        mod._normalize_wp_url("https://169.254.169.254/")


def test_normalize_wp_url_does_not_resolve_dns():
    """A syntactically valid https URL for a non-existent domain must NOT be rejected at
    validation time — that's the test-connection endpoint's job, not the validator's."""
    assert mod._normalize_wp_url("https://this-domain-does-not-exist-xyz123.com") == \
        "https://this-domain-does-not-exist-xyz123.com"


def test_normalize_wp_url_rejects_ipv6_ula_metadata():
    """AA-875: the EC2 IPv6 metadata address (fd00:ec2::254, inside fd00::/8)."""
    with pytest.raises(ValueError, match="private or reserved"):
        mod._normalize_wp_url("https://[fd00:ec2::254]/")


def test_normalize_wp_url_rejects_userinfo_trick():
    with pytest.raises(ValueError, match="username or password"):
        mod._normalize_wp_url("https://evil.example.com@169.254.169.254/")


# ── save_wordpress ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_save_wordpress_rejects_bad_url():
    pool, _ = _make_pool()
    req = _make_request(pool)
    body = mod.SaveWordPressRequest(wp_url="http://insecure.com", username="u", app_password="p")

    with pytest.raises(HTTPException) as exc_info:
        await mod.save_wordpress(body, req, TENANT)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_save_wordpress_rejects_name_resolving_to_private_address(monkeypatch):
    """AA-875 Done-when: connecting with an internal address -> 4xx, no secret written."""
    import socket

    async def internal_dns(host, port, family=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.3.7", port))]

    monkeypatch.setattr("shared.safe_http._getaddrinfo", internal_dns)
    pool, conn = _make_pool()
    req = _make_request(pool)
    body = mod.SaveWordPressRequest(wp_url="https://wp.internal-looking.example", username="u", app_password="p")

    with patch.object(mod, "_put_secret") as mock_put, pytest.raises(HTTPException) as exc_info:
        await mod.save_wordpress(body, req, TENANT)
    assert exc_info.value.status_code == 422
    assert "private or reserved" in exc_info.value.detail
    mock_put.assert_not_called()
    conn.fetchrow.assert_not_called()


@pytest.mark.asyncio
async def test_save_wordpress_accepts_name_that_does_not_resolve_yet(monkeypatch):
    """Unchanged design: a typo / not-yet-propagated domain is saved; the test connection classifies it."""
    import socket

    async def nxdomain(host, port, family=0):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr("shared.safe_http._getaddrinfo", nxdomain)
    row = {"config": json.dumps({"site_url": "https://not-yet-live.example"}), "connected_at": None,
           "last_verified_at": None, "last_verify_error": None}
    pool, _ = _make_pool(fetchrow=row)
    req = _make_request(pool)
    body = mod.SaveWordPressRequest(wp_url="https://not-yet-live.example", username="u", app_password="p")
    with patch.object(mod, "_put_secret"):
        result = await mod.save_wordpress(body, req, TENANT)
    assert result["site_url"] == "https://not-yet-live.example"


@pytest.mark.asyncio
async def test_save_wordpress_success_writes_secret_and_upserts_row():
    row = {
        "config": json.dumps({"site_url": "https://example-tenant-blog.com"}),
        "connected_at": datetime(2026, 8, 25, tzinfo=timezone.utc),
        "last_verified_at": None, "last_verify_error": None,
    }
    pool, conn = _make_pool(fetchrow=row)
    req = _make_request(pool)
    body = mod.SaveWordPressRequest(
        wp_url="https://example-tenant-blog.com", username="admin", app_password="app pass 1234")

    with patch.object(mod, "_put_secret") as mock_put:
        result = await mod.save_wordpress(body, req, TENANT)

    mock_put.assert_called_once()
    secret_key_arg, value_arg = mock_put.call_args[0]
    assert secret_key_arg == f"acp/cms/{TENANT['sub']}"
    assert value_arg["app_password"] == "app pass 1234"

    # DB row scoped to this tenant, never trusting a body-supplied tenant id
    # AA-859 W9: integration_type is a parameter ($2) of the shared connect UPSERT
    sql, tenant_id_param, type_param, config_param, secret_key_param = conn.fetchrow.call_args[0]
    assert sql == mod._CONNECT_UPSERT_SQL and type_param == "wordpress"
    assert tenant_id_param == TENANT["sub"]
    assert secret_key_param == f"acp/cms/{TENANT['sub']}"
    assert result["connected"] is True
    assert result["site_url"] == "https://example-tenant-blog.com"


@pytest.mark.asyncio
async def test_put_secret_falls_back_to_put_secret_value_when_exists():
    exists_error = ClientError(
        {"Error": {"Code": "ResourceExistsException", "Message": "exists"}}, "CreateSecret")
    mock_client = MagicMock()
    mock_client.create_secret.side_effect = exists_error

    with patch.object(mod, "_sm_client", return_value=mock_client):
        mod._put_secret("acp/cms/some-tenant", {"wp_url": "https://x.com"})

    mock_client.create_secret.assert_called_once()
    mock_client.put_secret_value.assert_called_once()
    assert mock_client.put_secret_value.call_args.kwargs["SecretId"] == "acp/cms/some-tenant"


# ── get_wordpress_status ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_status_not_connected():
    pool, _ = _make_pool(fetchrow=None)
    req = _make_request(pool)
    result = await mod.get_wordpress_status(req, TENANT)
    assert result["connected"] is False


@pytest.mark.asyncio
async def test_get_status_connected():
    row = {
        "config": json.dumps({"site_url": "https://example-tenant-blog.com"}),
        "connected_at": datetime(2026, 8, 25, tzinfo=timezone.utc),
        "last_verified_at": datetime(2026, 8, 25, tzinfo=timezone.utc), "last_verify_error": None,
    }
    pool, conn = _make_pool(fetchrow=row)
    req = _make_request(pool)
    result = await mod.get_wordpress_status(req, TENANT)
    assert result["connected"] is True
    # scoped by caller's own tenant_id, never a param the client controls
    assert conn.fetchrow.call_args[0][1] == TENANT["sub"]


# ── test_wordpress (connection test) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_test_wordpress_404_when_not_connected():
    pool, _ = _make_pool(fetchrow=None)
    req = _make_request(pool)
    with pytest.raises(HTTPException) as exc_info:
        await mod.test_wordpress(req, TENANT)
    assert exc_info.value.status_code == 404


def _wp_row():
    return {"config": json.dumps({"site_url": "https://example-tenant-blog.com"}),
            "secret_key": f"acp/cms/{TENANT['sub']}"}


@pytest.mark.asyncio
async def test_test_wordpress_success_sets_last_verified_at():
    updated_row = {
        "config": json.dumps({"site_url": "https://example-tenant-blog.com"}),
        "connected_at": datetime(2026, 8, 25, tzinfo=timezone.utc),
        "last_verified_at": datetime(2026, 8, 25, tzinfo=timezone.utc), "last_verify_error": None,
    }

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_wp_row(), updated_row])
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    req = _make_request(pool)

    wp_user_json = json.dumps({"id": 1, "name": "admin", "slug": "admin"})
    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://example-tenant-blog.com", "username": "admin", "app_password": "pw"}), \
         patch(TARGET, fake_request(200, body_text=wp_user_json)):
        result = await mod.test_wordpress(req, TENANT)

    assert result["success"] is True
    assert result["last_verify_error"] is None
    update_sql = conn.fetchrow.call_args_list[1][0][0]
    assert "last_verified_at = now()" in update_sql
    assert "last_verify_error = NULL" in update_sql


@pytest.mark.asyncio
async def test_test_wordpress_401_classified_as_wrong_credentials():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_wp_row(), {
        "config": json.dumps({"site_url": "https://example-tenant-blog.com"}),
        "connected_at": None, "last_verified_at": None,
        "last_verify_error": "Wrong username or application password",
    }])
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    req = _make_request(pool)

    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://example-tenant-blog.com", "username": "admin", "app_password": "wrong"}), \
         patch(TARGET, fake_request(401)):
        result = await mod.test_wordpress(req, TENANT)

    assert result["success"] is False
    assert result["last_verify_error"] == "Wrong username or application password"
    # last_verified_at must NOT be assigned on a failed retest (it legitimately still appears
    # in the RETURNING clause, so check the SET clause specifically, not the whole statement)
    update_sql = conn.fetchrow.call_args_list[1][0][0]
    set_clause = update_sql.split("WHERE")[0]
    assert "last_verified_at =" not in set_clause


@pytest.mark.asyncio
async def test_test_wordpress_connection_error_classified_as_unreachable():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_wp_row(), {
        "config": json.dumps({"site_url": "https://this-domain-does-not-exist-xyz123.com"}),
        "connected_at": None, "last_verified_at": None,
        "last_verify_error": "Could not connect to this WordPress site — check the URL",
    }])
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    req = _make_request(pool)

    import aiohttp as real_aiohttp
    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://this-domain-does-not-exist-xyz123.com", "username": "admin", "app_password": "pw"}), \
         patch(TARGET, fake_request(
             None, raise_exc=real_aiohttp.ClientConnectorError(MagicMock(), OSError("DNS fail")))):
        result = await mod.test_wordpress(req, TENANT)

    assert result["success"] is False
    assert "Could not connect" in result["last_verify_error"]


# ── AA-460 — false-positive regression: a 200 that isn't real WordPress JSON ────

@pytest.mark.asyncio
async def test_test_wordpress_200_html_anti_bot_page_is_not_success():
    """The exact bug found live in AA-457-02: InfinityFree's anti-bot challenge page returns
    200/text-html for every request, previously misread as a successful connection."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_wp_row(), {
        "config": json.dumps({"site_url": "https://aa-wordpress.rf.gd"}),
        "connected_at": None, "last_verified_at": None,
        "last_verify_error": "Unexpected response from this URL — verify it's a WordPress site with REST API enabled",
    }])
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    req = _make_request(pool)

    anti_bot_html = '<html><body><script src="/aes.js"></script></body></html>'
    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://aa-wordpress.rf.gd", "username": "admin", "app_password": "correct"}), \
         patch(TARGET, fake_request(200, content_type="text/html", body_text=anti_bot_html)):
        result = await mod.test_wordpress(req, TENANT)

    assert result["success"] is False
    assert "Unexpected response" in result["last_verify_error"]
    # last_verified_at must NOT be assigned — same anti-false-success-on-failure guarantee
    update_sql = conn.fetchrow.call_args_list[1][0][0]
    set_clause = update_sql.split("WHERE")[0]
    assert "last_verified_at =" not in set_clause


@pytest.mark.asyncio
async def test_test_wordpress_200_json_but_missing_id_field_is_not_success():
    """content-type is real JSON but the body doesn't have WordPress's user-object shape —
    still must not be treated as success (e.g. a JSON API that isn't WordPress at all)."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_wp_row(), {
        "config": json.dumps({"site_url": "https://example-tenant-blog.com"}),
        "connected_at": None, "last_verified_at": None, "last_verify_error": "Unexpected response...",
    }])
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    req = _make_request(pool)

    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://example-tenant-blog.com", "username": "admin", "app_password": "pw"}), \
         patch(TARGET, fake_request(
             200, content_type="application/json", body_text=json.dumps({"status": "ok"}))):
        result = await mod.test_wordpress(req, TENANT)

    assert result["success"] is False


@pytest.mark.asyncio
async def test_test_wordpress_200_malformed_json_is_not_success():
    """content-type claims JSON but the body doesn't actually parse — must not crash, must not
    report success."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_wp_row(), {
        "config": json.dumps({"site_url": "https://example-tenant-blog.com"}),
        "connected_at": None, "last_verified_at": None, "last_verify_error": "Unexpected response...",
    }])
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    req = _make_request(pool)

    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://example-tenant-blog.com", "username": "admin", "app_password": "pw"}), \
         patch(TARGET, fake_request(
             200, content_type="application/json", body_text="{not valid json")):
        result = await mod.test_wordpress(req, TENANT)

    assert result["success"] is False


@pytest.mark.asyncio
async def test_test_wordpress_blocked_destination_is_422_and_not_recorded():
    """AA-875: the saved site now resolves (or redirects) to a private address -> safe_http refuses
    the connection; the endpoint answers 422 and does not run the verify UPDATE."""
    from shared.safe_http import UnsafeURLError

    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_wp_row()])
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    req = _make_request(pool)

    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://example-tenant-blog.com", "username": "admin", "app_password": "pw"}), \
         patch(TARGET, fake_request(raise_exc=UnsafeURLError(
             "URL cannot point to a private or reserved IP address"))), \
         pytest.raises(HTTPException) as exc_info:
        await mod.test_wordpress(req, TENANT)

    assert exc_info.value.status_code == 422
    assert conn.fetchrow.await_count == 1  # only the block-1 SELECT


@pytest.mark.asyncio
async def test_test_wordpress_calls_users_me_with_basic_auth_through_safe_http():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=[_wp_row(), {
        "config": json.dumps({"site_url": "https://example-tenant-blog.com"}),
        "connected_at": None, "last_verified_at": None, "last_verify_error": None,
    }])
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    req = _make_request(pool)

    fake = fake_request(200, body_text=json.dumps({"id": 1}))
    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://example-tenant-blog.com/", "username": "admin", "app_password": "pw"}), \
         patch(TARGET, fake):
        await mod.test_wordpress(req, TENANT)

    method, url = fake.await_args.args
    assert (method, url) == ("GET", "https://example-tenant-blog.com/wp-json/wp/v2/users/me")
    assert fake.await_args.kwargs["auth"].login == "admin"


def test_classify_test_failure_200_invalid_body_has_dedicated_message():
    msg = mod._classify_test_failure(200, None, invalid_200_body=True)
    assert "Unexpected response" in msg
    assert "REST API" in msg


def test_classify_test_failure_200_valid_body_flag_never_reached():
    """Sanity: a genuinely successful 200 never reaches _classify_test_failure at all (the
    caller only calls it when success is False) — this just confirms the function itself
    doesn't misclassify a default invalid_200_body=False + status=200 combination, which
    would only happen if a caller misused it."""
    msg = mod._classify_test_failure(200, None, invalid_200_body=False)
    assert msg == "WordPress returned an unexpected response (HTTP 200)"
