"""AA-815 — tenant self-unpublish removes the live post, the publish-log list, the blog
integration lookup fix, and DELETE /v1/integrations/{wordpress|facebook}.

- WordPressAdapter.delete_post / FacebookAdapter.delete_post: AA-460 shape validation (a 2xx alone,
  or a non-WordPress 404 page, is never read as "removed").
- v1_publish.unpublish: remote delete first; a remote failure / missing connection leaves the row
  'published' (no UPDATE issued).
- v1_publish.publish: the blog channel looks up integration_type 'wordpress' (was 'blog').
- v1_integrations disconnect: one transaction that deletes the row and then tombstones the secret
  (PutSecretValue, never DeleteSecret); a tombstone failure rolls the DELETE back (AA-817 C3).
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from api.routers import v1_integrations as integ_mod
from api.routers import v1_publish as pub_mod
from services.acp_publish.facebook import FacebookAdapter
from services.acp_publish.idempotency import Claim
from services.acp_s4_blog.cms.wordpress import WordPressAdapter

TENANT_ID = str(uuid.uuid4())
TENANT = {"sub": TENANT_ID, "role": "tenant"}
PUBLISH_ID = uuid.uuid4()
PIECE_ID = str(uuid.uuid4())
NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


# ── helpers ────────────────────────────────────────────────────────────────────

def _http_response(status: int, body=None, content_type: str = "application/json"):
    resp = MagicMock()
    resp.status = status
    resp.headers = {"content-type": content_type}
    text = body if isinstance(body, str) else (json.dumps(body) if body is not None else "")
    resp.text = AsyncMock(return_value=text)
    return resp


def _session_ctx(resp, method: str = "delete"):
    session = MagicMock()
    call = MagicMock()
    call.return_value.__aenter__ = AsyncMock(return_value=resp)
    call.return_value.__aexit__ = AsyncMock(return_value=False)
    setattr(session, method, call)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, call


def _sequential_pool(*fetchrow_returns):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_returns))
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock(return_value="DELETE 1")
    tx = MagicMock()  # conn.transaction() (AA-817: _disconnect runs in one transaction)
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    conn.tx = tx
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    pool.execute = AsyncMock(return_value="INSERT 0 1")  # write_audit_log(pool, ...)
    return pool, conn


def _request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    req.app.state.tenant_pool = None  # acquire_scoped_conn -> admin pool
    return req


def _published_row(channel="blog", external_id="42", external_url=None):
    if external_url is None:
        external_url = ("https://www.tenant-blog.com/post-42/" if channel == "blog"
                        else f"https://www.facebook.com/{external_id}")
    return {"publish_id": str(PUBLISH_ID), "piece_id": PIECE_ID, "channel": channel,
            "external_id": external_id, "external_url": external_url}


def _updated_row(channel="blog"):
    return {"publish_id": str(PUBLISH_ID), "channel": channel, "status": "unpublished",
            "unpublished_at": NOW}


WP_CREDS = {"wp_url": "https://tenant-blog.com", "username": "admin", "app_password": "pw"}
FB_CREDS = {"page_id": "123", "page_access_token": "tok"}


# ── WordPressAdapter.delete_post ───────────────────────────────────────────────

@pytest.mark.asyncio
class TestWordPressDelete:
    adapter = WordPressAdapter(wp_url="https://tenant-blog.com", username="u", app_password="p")

    async def test_trash_response_is_trashed_and_hits_posts_endpoint_without_force(self):
        ctx, call = _session_ctx(_http_response(200, {"id": 42, "status": "trash", "link": "x"}))
        with patch("aiohttp.ClientSession", return_value=ctx):
            assert await self.adapter.delete_post(42) == "trashed"
        url = call.call_args[0][0]
        assert url == "https://tenant-blog.com/wp-json/wp/v2/posts/42"
        assert "force" not in url and "params" not in call.call_args.kwargs

    async def test_already_trashed_410_is_already_gone(self):
        body = {"code": "rest_already_trashed", "message": "The post has already been deleted."}
        ctx, _ = _session_ctx(_http_response(410, body))
        with patch("aiohttp.ClientSession", return_value=ctx):
            assert await self.adapter.delete_post(42) == "already_gone"

    async def test_invalid_id_404_is_already_gone(self):
        body = {"code": "rest_post_invalid_id", "message": "Invalid post ID."}
        ctx, _ = _session_ctx(_http_response(404, body))
        with patch("aiohttp.ClientSession", return_value=ctx):
            assert await self.adapter.delete_post(42) == "already_gone"

    async def test_html_404_from_a_waf_is_not_read_as_gone(self):
        ctx, _ = _session_ctx(_http_response(404, "<html>Not Found</html>", content_type="text/html"))
        with patch("aiohttp.ClientSession", return_value=ctx):
            with pytest.raises(RuntimeError, match="unexpected response"):
                await self.adapter.delete_post(42)

    async def test_200_html_anti_bot_page_raises(self):
        ctx, _ = _session_ctx(_http_response(200, "<html>checking your browser</html>", content_type="text/html"))
        with patch("aiohttp.ClientSession", return_value=ctx):
            with pytest.raises(RuntimeError):
                await self.adapter.delete_post(42)

    async def test_200_post_not_in_trash_raises(self):
        ctx, _ = _session_ctx(_http_response(200, {"id": 42, "status": "publish"}))
        with patch("aiohttp.ClientSession", return_value=ctx):
            with pytest.raises(RuntimeError):
                await self.adapter.delete_post(42)

    async def test_cannot_delete_401_raises_permission_message(self):
        body = {"code": "rest_cannot_delete", "message": "Sorry, you are not allowed to delete this post."}
        ctx, _ = _session_ctx(_http_response(401, body))
        with patch("aiohttp.ClientSession", return_value=ctx):
            with pytest.raises(RuntimeError, match="cannot delete posts"):
                await self.adapter.delete_post(42)

    async def test_trash_disabled_501_raises_clear_message_no_force_delete(self):
        body = {"code": "rest_trash_not_supported", "message": "The post does not support trashing."}
        ctx, call = _session_ctx(_http_response(501, body))
        with patch("aiohttp.ClientSession", return_value=ctx):
            with pytest.raises(RuntimeError, match="trash turned off"):
                await self.adapter.delete_post(42)
        assert call.call_count == 1  # never retried with force=true


# ── FacebookAdapter.delete_post ────────────────────────────────────────────────

@pytest.mark.asyncio
class TestFacebookDelete:
    adapter = FacebookAdapter(page_id="123", page_access_token="tok")

    async def test_success_true_is_deleted(self):
        ctx, call = _session_ctx(_http_response(200, {"success": True}))
        with patch("aiohttp.ClientSession", return_value=ctx):
            assert await self.adapter.delete_post("123_456") == "deleted"
        assert call.call_args[0][0].endswith("/123_456")
        assert call.call_args.kwargs["params"] == {"access_token": "tok"}

    async def test_graph_error_raises_with_message(self):
        body = {"error": {"code": 100, "error_subcode": 33, "message": "Object with ID '123_456' does not exist"}}
        ctx, _ = _session_ctx(_http_response(400, body))
        with patch("aiohttp.ClientSession", return_value=ctx):
            with pytest.raises(RuntimeError, match="does not exist"):
                await self.adapter.delete_post("123_456")

    async def test_non_json_200_raises(self):
        ctx, _ = _session_ctx(_http_response(200, "<html/>", content_type="text/html"))
        with patch("aiohttp.ClientSession", return_value=ctx):
            with pytest.raises(RuntimeError):
                await self.adapter.delete_post("123_456")

    async def test_success_false_raises(self):
        ctx, _ = _session_ctx(_http_response(200, {"success": False}))
        with patch("aiohttp.ClientSession", return_value=ctx):
            with pytest.raises(RuntimeError):
                await self.adapter.delete_post("123_456")


# ── v1_publish.unpublish ───────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestTenantUnpublish:
    async def test_blog_success_trashes_remote_then_flips_row_and_audits(self):
        pool, conn = _sequential_pool(_published_row(), {"secret_key": "acp/cms/x"}, _updated_row())
        delete = AsyncMock(return_value="trashed")
        with patch.object(pub_mod, "_get_secret", return_value=WP_CREDS), \
             patch.object(WordPressAdapter, "delete_post", delete):
            result = await pub_mod.unpublish(PUBLISH_ID, _request(pool), tenant=TENANT)

        delete.assert_awaited_once_with("42")
        assert result["status"] == "unpublished"
        assert result["remote"] == "trashed"
        assert result["unpublished_by"] == f"tenant:{TENANT_ID}"
        # the blog channel's credentials are the 'wordpress' integration row
        integ_call = conn.fetchrow.call_args_list[1]
        assert integ_call[0][1:] == (TENANT_ID, "wordpress")
        # the UPDATE is still ownership- and status-guarded
        update_sql, *update_params = conn.fetchrow.call_args_list[2][0]
        assert "status = 'published'" in update_sql
        assert update_params == [PUBLISH_ID, TENANT_ID, f"tenant:{TENANT_ID}"]
        audit_args = pool.execute.call_args[0]
        assert audit_args[3] == "content_piece.unpublished" and audit_args[5] == PIECE_ID

    async def test_facebook_success_deletes_graph_post(self):
        pool, conn = _sequential_pool(_published_row("facebook", "123_456"), {"secret_key": "acp/social/x"},
                                      _updated_row("facebook"))
        delete = AsyncMock(return_value="deleted")
        with patch.object(pub_mod, "_get_secret", return_value=FB_CREDS), \
             patch.object(FacebookAdapter, "delete_post", delete):
            result = await pub_mod.unpublish(PUBLISH_ID, _request(pool), tenant=TENANT)
        delete.assert_awaited_once_with("123_456")
        assert result["remote"] == "deleted" and result["channel"] == "facebook"
        assert conn.fetchrow.call_args_list[1][0][2] == "facebook"

    async def test_remote_failure_returns_502_and_leaves_row_published(self):
        pool, conn = _sequential_pool(_published_row(), {"secret_key": "acp/cms/x"})
        delete = AsyncMock(side_effect=RuntimeError("WP API delete 500 returned an unexpected response"))
        with patch.object(pub_mod, "_get_secret", return_value=WP_CREDS), \
             patch.object(WordPressAdapter, "delete_post", delete):
            with pytest.raises(HTTPException) as exc_info:
                await pub_mod.unpublish(PUBLISH_ID, _request(pool), tenant=TENANT)
        assert exc_info.value.status_code == 502
        assert "Could not remove the post from WordPress" in exc_info.value.detail
        assert conn.fetchrow.await_count == 2  # select + integration lookup, no UPDATE
        pool.execute.assert_not_awaited()

    async def test_not_connected_returns_422_without_remote_call(self):
        pool, conn = _sequential_pool(_published_row("facebook", "1_2"), None)
        delete = AsyncMock()
        with patch.object(FacebookAdapter, "delete_post", delete):
            with pytest.raises(HTTPException) as exc_info:
                await pub_mod.unpublish(PUBLISH_ID, _request(pool), tenant=TENANT)
        assert exc_info.value.status_code == 422
        assert "Connect Facebook to unpublish" in exc_info.value.detail
        delete.assert_not_awaited()
        assert conn.fetchrow.await_count == 2

    async def test_row_without_external_id_returns_422(self):
        pool, conn = _sequential_pool(_published_row(external_id=None))
        with pytest.raises(HTTPException) as exc_info:
            await pub_mod.unpublish(PUBLISH_ID, _request(pool), tenant=TENANT)
        assert exc_info.value.status_code == 422
        assert conn.fetchrow.await_count == 1

    async def test_secret_read_failure_returns_502(self):
        pool, _ = _sequential_pool(_published_row(), {"secret_key": "acp/cms/x"})
        err = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetSecretValue")
        with patch.object(pub_mod, "_get_secret", side_effect=err):
            with pytest.raises(HTTPException) as exc_info:
                await pub_mod.unpublish(PUBLISH_ID, _request(pool), tenant=TENANT)
        assert exc_info.value.status_code == 502

    async def test_post_on_a_previously_connected_wordpress_site_is_refused(self):
        # WordPress post IDs are per site: post 42 on the newly connected site is another post.
        pool, conn = _sequential_pool(_published_row(external_url="https://old-blog.com/post-42/"),
                                      {"secret_key": "acp/cms/x"})
        delete = AsyncMock()
        with patch.object(pub_mod, "_get_secret", return_value=WP_CREDS), \
             patch.object(WordPressAdapter, "delete_post", delete):
            with pytest.raises(HTTPException) as exc_info:
                await pub_mod.unpublish(PUBLISH_ID, _request(pool), tenant=TENANT)
        assert exc_info.value.status_code == 422
        assert "old-blog.com" in exc_info.value.detail
        delete.assert_not_awaited()
        assert conn.fetchrow.await_count == 2  # no UPDATE

    async def test_post_on_a_previously_connected_facebook_page_is_refused(self):
        pool, _ = _sequential_pool(_published_row("facebook", "999_456"), {"secret_key": "acp/social/x"})
        delete = AsyncMock()
        with patch.object(pub_mod, "_get_secret", return_value=FB_CREDS), \
             patch.object(FacebookAdapter, "delete_post", delete):
            with pytest.raises(HTTPException) as exc_info:
                await pub_mod.unpublish(PUBLISH_ID, _request(pool), tenant=TENANT)
        assert exc_info.value.status_code == 422
        assert "Page 999" in exc_info.value.detail
        delete.assert_not_awaited()

    async def test_concurrent_flip_after_remote_delete_is_404(self):
        pool, _ = _sequential_pool(_published_row(), {"secret_key": "acp/cms/x"}, None)
        with patch.object(pub_mod, "_get_secret", return_value=WP_CREDS), \
             patch.object(WordPressAdapter, "delete_post", AsyncMock(return_value="already_gone")):
            with pytest.raises(HTTPException) as exc_info:
                await pub_mod.unpublish(PUBLISH_ID, _request(pool), tenant=TENANT)
        assert exc_info.value.status_code == 404

    async def test_audit_failure_does_not_break_a_done_unpublish(self):
        pool, _ = _sequential_pool(_published_row(), {"secret_key": "acp/cms/x"}, _updated_row())
        pool.execute = AsyncMock(side_effect=RuntimeError("audit down"))
        with patch.object(pub_mod, "_get_secret", return_value=WP_CREDS), \
             patch.object(WordPressAdapter, "delete_post", AsyncMock(return_value="trashed")):
            result = await pub_mod.unpublish(PUBLISH_ID, _request(pool), tenant=TENANT)
        assert result["status"] == "unpublished"


# ── v1_publish.publish — blog integration lookup ──────────────────────────────

@pytest.mark.asyncio
async def test_publish_blog_looks_up_wordpress_integration():
    """Regression: AA-462 made the lookup `integration_type = channel`, i.e. 'blog', but
    v1_integrations.py stores the WordPress connection as 'wordpress' — every blog publish
    answered "Connect WordPress to publish"."""
    piece = {"piece_id": PIECE_ID, "content_text": "Body", "status": "approved", "channel": "blog",
             "angle_name": "A", "slug": None, "seo_title": None, "meta_description": None}
    pool, conn = _sequential_pool(piece, None)
    with patch.object(pub_mod, "claim", AsyncMock(return_value=Claim("claimed", publish_id="x"))):
        with pytest.raises(HTTPException) as exc_info:
            await pub_mod.publish(uuid.UUID(PIECE_ID), _request(pool), TENANT)
    assert exc_info.value.status_code == 422
    assert conn.fetchrow.call_args_list[1][0][2] == "wordpress"


# ── v1_publish.list_publish_log ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_publish_log_is_tenant_scoped_and_tenant_safe():
    pool, conn = _sequential_pool()
    conn.fetch = AsyncMock(return_value=[{
        "publish_id": str(PUBLISH_ID), "piece_id": PIECE_ID, "channel": "blog", "status": "published",
        "external_url": "https://tenant-blog.com/p/42", "published_at": NOW, "unpublished_at": None,
        "angle_name": None,
    }])
    req = _request(pool)
    result = await pub_mod.list_publish_log(req, tenant=TENANT, limit=50)

    sql, tenant_param, limit_param = conn.fetch.call_args[0]
    assert "pl.tenant_id = $1::uuid" in sql and "'published', 'unpublished'" in sql
    assert tenant_param == TENANT_ID and limit_param == 50
    row = result["data"][0]
    assert row["title"] == "Untitled" and row["status"] == "published"
    assert set(row) == {"publish_id", "piece_id", "title", "channel", "status", "external_url",
                        "published_at", "unpublished_at"}  # no last_error / actor / key


# ── v1_integrations disconnect ─────────────────────────────────────────────────

@pytest.mark.asyncio
class TestDisconnect:
    async def test_wordpress_tombstones_secret_then_deletes_row(self):
        pool, conn = _sequential_pool({"secret_key": f"acp/cms/{TENANT_ID}"})
        sm = MagicMock()
        with patch.object(integ_mod, "_sm_client", return_value=sm):
            result = await integ_mod.disconnect_wordpress(_request(pool), tenant=TENANT)

        sm.put_secret_value.assert_called_once()
        kwargs = sm.put_secret_value.call_args.kwargs
        assert kwargs["SecretId"] == f"acp/cms/{TENANT_ID}"
        assert json.loads(kwargs["SecretString"]) == {"disconnected": True}
        assert not sm.delete_secret.called
        delete_sql, *params = conn.execute.call_args[0]
        assert delete_sql.startswith("DELETE FROM shared.tenant_integrations")
        assert params == [TENANT_ID, "wordpress"]
        assert result["connected"] is False and result["disconnected"] is True
        assert pool.execute.call_args[0][3] == "integration.disconnected"

    async def test_facebook_disconnect_targets_facebook_row(self):
        pool, conn = _sequential_pool({"secret_key": f"acp/social/{TENANT_ID}"})
        sm = MagicMock()
        with patch.object(integ_mod, "_sm_client", return_value=sm):
            result = await integ_mod.disconnect_facebook(_request(pool), tenant=TENANT)
        assert sm.put_secret_value.call_args.kwargs["SecretId"] == f"acp/social/{TENANT_ID}"
        assert conn.execute.call_args[0][2] == "facebook"
        assert result == {"connected": False, "page_id": None, "connected_at": None,
                          "last_verified_at": None, "last_verify_error": None, "disconnected": True}

    async def test_tombstone_failure_keeps_the_row(self):
        """AA-817 C3: the DELETE runs first, inside the transaction; a tombstone failure raises
        through that transaction, which rolls the DELETE back (real-Postgres proof:
        tests/integration/test_aa817_tenant_pool_routes.py)."""
        pool, conn = _sequential_pool({"secret_key": f"acp/cms/{TENANT_ID}"})
        sm = MagicMock()
        sm.put_secret_value.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "PutSecretValue")
        with patch.object(integ_mod, "_sm_client", return_value=sm):
            with pytest.raises(HTTPException) as exc_info:
                await integ_mod.disconnect_wordpress(_request(pool), tenant=TENANT)
        assert exc_info.value.status_code == 502
        conn.transaction.assert_called_once()
        exc_type = conn.tx.__aexit__.call_args[0][0]
        assert exc_type is HTTPException, "the transaction must exit with the error (rollback)"
        assert not pool.execute.called, "no audit row for a disconnect that did not happen"

    async def test_missing_secret_still_disconnects(self):
        pool, conn = _sequential_pool({"secret_key": f"acp/cms/{TENANT_ID}"})
        sm = MagicMock()
        sm.put_secret_value.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "PutSecretValue")
        with patch.object(integ_mod, "_sm_client", return_value=sm):
            result = await integ_mod.disconnect_wordpress(_request(pool), tenant=TENANT)
        assert result["disconnected"] is True
        conn.execute.assert_awaited_once()

    async def test_not_connected_is_idempotent(self):
        pool, conn = _sequential_pool(None)
        sm = MagicMock()
        with patch.object(integ_mod, "_sm_client", return_value=sm):
            result = await integ_mod.disconnect_facebook(_request(pool), tenant=TENANT)
        assert result["connected"] is False and result["disconnected"] is False
        assert not sm.put_secret_value.called
        conn.execute.assert_not_awaited()
