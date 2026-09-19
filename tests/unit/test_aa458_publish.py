"""
tests/unit/test_aa458_publish.py — AA-458 [T11 PR2]: GET /v1/publish-log/pending,
POST /v1/publish-log/{piece_id}/publish, and WordPressAdapter.create_post()'s real-response
validation (the AA-460 lesson, applied here).

Mocks asyncpg via pool.acquire() (matches v1_publish.py's own connection style) and aiohttp at
the module level — no live AWS/network calls in unit tests.
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from api.routers import v1_publish as mod
from services.acp_s4_blog.cms.base import BlogContent
from services.acp_s4_blog.cms.wordpress import WordPressAdapter

TENANT = {"sub": str(uuid.uuid4()), "role": "tenant"}
PIECE_ID = uuid.uuid4()


def _make_request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    return req


def _sequential_pool(*fetchrow_returns, fetch_return=None, fetchval_return=None):
    """A pool whose acquire() is called multiple times in one request (piece lookup, then
    integrations lookup, then the final publish_log write) — each acquire() call gets the SAME
    conn mock, but conn.fetchrow is queued to return a different row per call, matching how
    test_aa457_integrations.py's multi-query tests already handle this."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_returns))
    conn.fetch = AsyncMock(return_value=fetch_return or [])
    conn.fetchval = AsyncMock(return_value=fetchval_return)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


# ── list_pending ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_pending_returns_rows_scoped_to_tenant():
    rows = [{
        "piece_id": str(PIECE_ID), "content_text": "## Section\nSome real content here.",
        "created_at": datetime(2026, 8, 25, tzinfo=timezone.utc),
        "channel": "blog", "angle_name": "A Real Angle",
        # AA-519 Việc 4 — now selected by list_pending()'s own query.
        "route_hub_name": None, "route_segment_count": None,
    }]
    pool, conn = _sequential_pool(fetch_return=rows)
    conn.fetch = AsyncMock(return_value=rows)
    req = _make_request(pool)

    result = await mod.list_pending(req, TENANT)

    assert result["total"] == 1
    assert result["data"][0]["piece_id"] == str(PIECE_ID)
    assert result["data"][0]["title"] == "A Real Angle"
    # scoped by the caller's own tenant_id, never a client-controlled param
    sql, tenant_id_param, channels_param = conn.fetch.call_args[0]
    assert tenant_id_param == TENANT["sub"]
    # AA-462 — list_pending() now covers every supported channel (blog + facebook), not just blog
    assert set(channels_param) == {"blog", "facebook"}
    assert "status = 'approved'" in sql
    assert "pl.publish_id IS NULL" in sql
    # AA-497 — angle_name joins via the denormalized FK first (mutable chosen=true is only a
    # fallback for pre-AA-497 rows), so a reopen()+re-choice on this request doesn't retroactively
    # change what an already-written piece's angle_name displays here.
    assert "ago.option_id = cp.angle_gate_option_id" in sql
    assert "cp.angle_gate_option_id IS NULL AND ago.request_id = agr.request_id" in sql
    # AA-469 Việc 4 (flow-order fix) — same reasoning, now for channel: reads cp.channel first
    # (COALESCE onto agr.channel only for pre-this-session rows), because
    # angle_gate_request.channel is no longer stable after a piece is written (set_channel() can
    # be called again for the request's NEXT write).
    assert "COALESCE(cp.channel, agr.channel)" in sql


@pytest.mark.asyncio
async def test_list_pending_untitled_fallback():
    rows = [{
        "piece_id": str(PIECE_ID), "content_text": "text", "created_at": None,
        "channel": "blog", "angle_name": None,
        "route_hub_name": None, "route_segment_count": None,
    }]
    pool, conn = _sequential_pool()
    conn.fetch = AsyncMock(return_value=rows)
    req = _make_request(pool)

    result = await mod.list_pending(req, TENANT)
    assert result["data"][0]["title"] == "Untitled"


# ── publish() — ownership / status / channel gating ──────────────────────────

@pytest.mark.asyncio
async def test_publish_404_when_piece_not_found():
    pool, _ = _sequential_pool(None)
    req = _make_request(pool)
    with pytest.raises(HTTPException) as exc_info:
        await mod.publish(PIECE_ID, req, TENANT)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_publish_422_when_piece_held_product_truth_unresolved():
    """AA-613 — a 'held' piece (unresolved product-truth after retries) is owned by the tenant
    and visible in My Content, but not publishable until edited. 422 (actionable), not 404 —
    ownership is already confirmed by the WHERE clause, so there's no existence leak to protect."""
    piece = {"piece_id": str(PIECE_ID), "content_text": "x", "status": "held",
              "channel": "blog", "angle_name": "A"}
    pool, _ = _sequential_pool(piece)
    req = _make_request(pool)
    with pytest.raises(HTTPException) as exc_info:
        await mod.publish(PIECE_ID, req, TENANT)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_publish_404_when_channel_unsupported():
    """AA-462 added facebook (see test_aa462_publish_facebook.py) — blog and facebook are both
    publishable now; the other 5 (tiktok/instagram/linkedin/email/ads) still aren't."""
    piece = {"piece_id": str(PIECE_ID), "content_text": "x", "status": "approved",
              "channel": "tiktok", "angle_name": "A"}
    pool, _ = _sequential_pool(piece)
    req = _make_request(pool)
    with pytest.raises(HTTPException) as exc_info:
        await mod.publish(PIECE_ID, req, TENANT)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_publish_404_cross_tenant_no_leak():
    """The ownership-scoped SELECT (WHERE tenant_id = $2) simply returns no row for another
    tenant's piece — identical 404 to the not-found case, no existence leak."""
    pool, conn = _sequential_pool(None)
    req = _make_request(pool)
    other_tenant = {"sub": str(uuid.uuid4()), "role": "tenant"}
    with pytest.raises(HTTPException) as exc_info:
        await mod.publish(PIECE_ID, req, other_tenant)
    assert exc_info.value.status_code == 404
    assert conn.fetchrow.call_args_list[0][0][2] == other_tenant["sub"]


@pytest.mark.asyncio
async def test_publish_422_when_wordpress_not_connected():
    piece = {"piece_id": str(PIECE_ID), "content_text": "x", "status": "approved",
              "channel": "blog", "angle_name": "A"}
    pool, _ = _sequential_pool(piece, None)
    req = _make_request(pool)
    with pytest.raises(HTTPException) as exc_info:
        await mod.publish(PIECE_ID, req, TENANT)
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "Connect WordPress to publish"


@pytest.mark.asyncio
async def test_publish_502_when_secret_read_fails():
    piece = {"piece_id": str(PIECE_ID), "content_text": "x", "status": "approved",
              "channel": "blog", "angle_name": "A"}
    integ = {"secret_key": "acp/cms/some-tenant"}
    pool, _ = _sequential_pool(piece, integ)
    req = _make_request(pool)

    error = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "GetSecretValue")
    with patch.object(mod, "_get_secret", side_effect=error):
        with pytest.raises(HTTPException) as exc_info:
            await mod.publish(PIECE_ID, req, TENANT)
    assert exc_info.value.status_code == 502


# ── publish() — real success/failure paths ───────────────────────────────────

def _piece_row():
    return {"piece_id": str(PIECE_ID), "content_text": "## Hello\nReal content.",
            "status": "approved", "channel": "blog", "angle_name": "The Chosen Angle"}


def _integ_row():
    return {"secret_key": "acp/cms/tenant-x"}


@pytest.mark.asyncio
async def test_publish_success_writes_published_row():
    write_result = {"publish_id": str(uuid.uuid4()), "status": "published",
                     "external_id": "42", "external_url": "https://tenant-blog.com/p/42",
                     "published_at": datetime(2026, 8, 25, tzinfo=timezone.utc), "last_error": None}
    # fetchrow call order: piece lookup, integ lookup, insert RETURNING (existing_failed check
    # is a separate fetchval() call, defaults to None => no prior failed row => INSERT path)
    pool, conn = _sequential_pool(_piece_row(), _integ_row(), write_result)
    req = _make_request(pool)

    from services.acp_s4_blog.cms.base import CMSPostResult
    fake_result = CMSPostResult(post_id=42, post_url="https://tenant-blog.com/p/42",
                                 status="publish", cms_type="wordpress")

    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://tenant-blog.com", "username": "admin", "app_password": "pw"}), \
         patch.object(WordPressAdapter, "create_post", AsyncMock(return_value=fake_result)):
        result = await mod.publish(PIECE_ID, req, TENANT)

    assert result["success"] is True
    assert result["status"] == "published"
    assert result["external_id"] == "42"
    assert result["external_url"] == "https://tenant-blog.com/p/42"

    # confirm the INSERT (no prior failed row) carried the real values, channel='blog'
    insert_call = conn.fetchrow.call_args_list[-1]
    insert_sql = insert_call[0][0]
    assert "INSERT INTO acp_shared.publish_log" in insert_sql
    params = insert_call[0][1:]
    assert params[2] == "blog"      # channel
    assert params[3] == "published"  # status
    assert params[4] == "42"         # external_id

    # AA-497 — the piece-lookup SELECT (first fetchrow call) joins angle_name via the
    # denormalized FK first, same fix as list_pending()'s above.
    piece_lookup_sql = conn.fetchrow.call_args_list[0][0][0]
    assert "ago.option_id = cp.angle_gate_option_id" in piece_lookup_sql
    # AA-469 Việc 4 (flow-order fix) — same cp.channel-first read as list_pending()'s above.
    assert "COALESCE(cp.channel, agr.channel)" in piece_lookup_sql


@pytest.mark.asyncio
async def test_publish_failure_writes_failed_row_not_published():
    """AA-460 lesson: create_post() raising (real validation failure) must result in
    status='failed', never a fabricated 'published' row."""
    write_result = {"publish_id": str(uuid.uuid4()), "status": "failed",
                     "external_id": None, "external_url": None,
                     "published_at": None, "last_error": "WP API returned an unexpected response"}
    pool, conn = _sequential_pool(_piece_row(), _integ_row(), write_result)
    req = _make_request(pool)

    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://example.com", "username": "admin", "app_password": "pw"}), \
         patch.object(WordPressAdapter, "create_post",
                      AsyncMock(side_effect=RuntimeError("WP API returned an unexpected response"))):
        result = await mod.publish(PIECE_ID, req, TENANT)

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["external_id"] is None
    assert result["external_url"] is None

    insert_call = conn.fetchrow.call_args_list[-1]
    params = insert_call[0][1:]
    assert params[3] == "failed"
    assert params[4] is None  # external_id must be None, never fabricated
    assert params[6] is not None  # last_error present


@pytest.mark.asyncio
async def test_publish_retry_updates_existing_failed_row_not_duplicate():
    existing_publish_id = str(uuid.uuid4())
    write_result = {"publish_id": existing_publish_id, "status": "published",
                     "external_id": "99", "external_url": "https://tenant-blog.com/p/99",
                     "published_at": datetime(2026, 8, 25, tzinfo=timezone.utc), "last_error": None}
    pool, conn = _sequential_pool(_piece_row(), _integ_row(), write_result,
                                   fetchval_return=existing_publish_id)
    req = _make_request(pool)

    from services.acp_s4_blog.cms.base import CMSPostResult
    fake_result = CMSPostResult(post_id=99, post_url="https://tenant-blog.com/p/99",
                                 status="publish", cms_type="wordpress")

    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://tenant-blog.com", "username": "admin", "app_password": "pw"}), \
         patch.object(WordPressAdapter, "create_post", AsyncMock(return_value=fake_result)):
        result = await mod.publish(PIECE_ID, req, TENANT)

    assert result["success"] is True
    # the retry path UPDATEs the existing failed row rather than inserting a new one
    update_call = conn.fetchrow.call_args_list[-1]
    assert "UPDATE acp_shared.publish_log" in update_call[0][0]
    assert update_call[0][1] == existing_publish_id


# ── WordPressAdapter.create_post() — the AA-460 lesson applied here ──────────

def _mock_post_response(status: int, content_type: str = "application/json", body_text: str = ""):
    resp_ctx = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.headers = {"content-type": content_type}
    mock_resp.text = AsyncMock(return_value=body_text)
    resp_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    resp_ctx.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.post = MagicMock(return_value=resp_ctx)

    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session_ctx)


@pytest.mark.asyncio
async def test_create_post_success_real_wp_response():
    adapter = WordPressAdapter(wp_url="https://tenant-blog.com", username="admin", app_password="pw")
    content = BlogContent(title="T", content_html="body", slug="", seo_title="T", seo_meta="", status="publish")
    real_wp_body = json.dumps({"id": 7, "link": "https://tenant-blog.com/p/7", "status": "publish"})

    with patch("aiohttp.ClientSession", _mock_post_response(201, body_text=real_wp_body)):
        result = await adapter.create_post(content)

    assert result.post_id == 7
    assert result.post_url == "https://tenant-blog.com/p/7"


@pytest.mark.asyncio
async def test_create_post_sends_content_status_not_hardcoded():
    """The whole point of AA-458's adapter change: content.status controls the real payload,
    default 'draft' preserved for existing callers, 'publish' available for the new endpoint."""
    adapter = WordPressAdapter(wp_url="https://tenant-blog.com", username="admin", app_password="pw")
    content = BlogContent(title="T", content_html="body", slug="", seo_title="T", seo_meta="", status="publish")
    real_wp_body = json.dumps({"id": 1, "link": "https://x.com/1"})

    mock_session_factory = _mock_post_response(201, body_text=real_wp_body)
    with patch("aiohttp.ClientSession", mock_session_factory):
        await adapter.create_post(content)

    session_ctx = mock_session_factory.return_value
    session = await session_ctx.__aenter__()
    sent_payload = session.post.call_args.kwargs["json"]
    assert sent_payload["status"] == "publish"


@pytest.mark.asyncio
async def test_create_post_non_2xx_raises():
    adapter = WordPressAdapter(wp_url="https://tenant-blog.com", username="admin", app_password="wrong")
    content = BlogContent(title="T", content_html="body", slug="", seo_title="T", seo_meta="", status="publish")

    with patch("aiohttp.ClientSession", _mock_post_response(401, body_text="Unauthorized")):
        with pytest.raises(RuntimeError, match="WP API 401"):
            await adapter.create_post(content)


@pytest.mark.asyncio
async def test_create_post_200_html_anti_bot_page_raises_not_returns_fake_success():
    """The exact AA-460-class bug this adapter must NOT have: a 2xx HTML challenge page must
    never be treated as a successful post creation."""
    adapter = WordPressAdapter(wp_url="https://aa-wordpress.rf.gd", username="admin", app_password="pw")
    content = BlogContent(title="T", content_html="body", slug="", seo_title="T", seo_meta="", status="publish")
    anti_bot_html = '<html><body><script src="/aes.js"></script></body></html>'

    with patch("aiohttp.ClientSession", _mock_post_response(200, content_type="text/html", body_text=anti_bot_html)):
        with pytest.raises(RuntimeError, match="not a real WordPress post"):
            await adapter.create_post(content)


@pytest.mark.asyncio
async def test_create_post_200_json_missing_id_or_link_raises():
    adapter = WordPressAdapter(wp_url="https://tenant-blog.com", username="admin", app_password="pw")
    content = BlogContent(title="T", content_html="body", slug="", seo_title="T", seo_meta="", status="publish")
    wrong_shape = json.dumps({"status": "ok"})

    with patch("aiohttp.ClientSession", _mock_post_response(200, body_text=wrong_shape)):
        with pytest.raises(RuntimeError, match="not a real WordPress post"):
            await adapter.create_post(content)


@pytest.mark.asyncio
async def test_create_post_default_status_still_draft_for_existing_caller():
    """publisher.py never sets BlogContent.status explicitly — confirms the default is still
    'draft', so that pipeline's behavior is completely unchanged by this fix."""
    adapter = WordPressAdapter(wp_url="https://tenant-blog.com", username="admin", app_password="pw")
    content = BlogContent(title="T", content_html="body", slug="", seo_title="T", seo_meta="")
    real_wp_body = json.dumps({"id": 1, "link": "https://x.com/1"})

    mock_session_factory = _mock_post_response(201, body_text=real_wp_body)
    with patch("aiohttp.ClientSession", mock_session_factory):
        await adapter.create_post(content)

    session_ctx = mock_session_factory.return_value
    session = await session_ctx.__aenter__()
    sent_payload = session.post.call_args.kwargs["json"]
    assert sent_payload["status"] == "draft"
