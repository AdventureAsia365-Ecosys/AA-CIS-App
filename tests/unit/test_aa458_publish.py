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
from services.acp_publish.idempotency import Claim
from services.acp_s4_blog.cms.wordpress import WordPressAdapter

TENANT = {"sub": str(uuid.uuid4()), "role": "tenant"}
PIECE_ID = uuid.uuid4()


def _make_request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    req.app.state.redis.get = AsyncMock(return_value="false")  # S241 D-12: explicit OFF
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
    assert "cp.status IN ('approved', 'held')" in sql  # AA-770 — the tenant always receives it
    # AA-802 — NOT EXISTS (not a LEFT JOIN): two published/requested rows can never duplicate a card
    assert "NOT EXISTS" in sql and "pl.status IN ('published', 'requested')" in sql
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
async def test_publish_422_when_piece_not_written_yet():
    """AA-770 (S233) — 'held' is an admin-only quality label now and publishes like approved
    (test_aa770_tenant_receives.py); only a piece with no content yet is refused (422)."""
    piece = {"piece_id": str(PIECE_ID), "content_text": "", "status": "processing",
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
# AA-726: publish() now claims a publish_log row (services/acp_publish/idempotency.claim, covered
# against real Postgres in tests/integration/test_aa726_publish_idempotency.py) and finishes it
# with one UPDATE ... RETURNING (the last fetchrow). These tests stub the claim.

def _claimed(prior_attempt=False, publish_id=None):
    return AsyncMock(return_value=Claim("claimed", publish_id=publish_id or str(uuid.uuid4()),
                                        prior_attempt=prior_attempt))

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

    create = AsyncMock(return_value=fake_result)
    claim = _claimed()
    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://tenant-blog.com", "username": "admin", "app_password": "pw"}), \
         patch.object(mod, "claim", claim), \
         patch.object(WordPressAdapter, "create_post", create):
        result = await mod.publish(PIECE_ID, req, TENANT)

    assert result["success"] is True
    assert result["status"] == "published"
    assert result["external_id"] == "42"
    assert result["external_url"] == "https://tenant-blog.com/p/42"
    assert result["replayed"] is False

    # claimed for channel='blog' with a revision key, then the claimed row was finished
    assert claim.call_args.kwargs["channel"] == "blog"
    assert claim.call_args.kwargs["key"].startswith(f"{PIECE_ID}:blog:")
    finish_call = conn.fetchrow.call_args_list[-1]
    assert "UPDATE acp_shared.publish_log" in finish_call[0][0]
    params = finish_call[0][1:]
    assert params[2] == "published"  # status
    assert params[3] == "42"         # external_id
    # the WordPress slug carries the revision, so a lost post can be found again
    sent = create.call_args[0][0]
    assert sent.slug.startswith("the-chosen-angle-") and len(sent.slug.rsplit("-", 1)[-1]) == 8

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
         patch.object(mod, "claim", _claimed()), \
         patch.object(WordPressAdapter, "create_post",
                      AsyncMock(side_effect=RuntimeError("WP API returned an unexpected response"))):
        result = await mod.publish(PIECE_ID, req, TENANT)

    assert result["success"] is False
    assert result["status"] == "failed"
    assert result["external_id"] is None
    assert result["external_url"] is None

    finish_call = conn.fetchrow.call_args_list[-1]
    params = finish_call[0][1:]
    assert params[2] == "failed"
    assert params[3] is None  # external_id must be None, never fabricated
    assert params[5] is not None  # last_error present


@pytest.mark.asyncio
async def test_publish_retry_after_lost_response_finds_the_post_and_does_not_post_again():
    """AA-726 Done-when: WordPress returned 201 but the response was lost (attempt recorded as
    failed/requested). The retry looks the revision slug up first, finds the post, and records
    it — exactly one post on the site."""
    publish_id = str(uuid.uuid4())
    write_result = {"publish_id": publish_id, "status": "published",
                     "external_id": "99", "external_url": "https://tenant-blog.com/p/99",
                     "published_at": datetime(2026, 8, 25, tzinfo=timezone.utc), "last_error": None}
    pool, conn = _sequential_pool(_piece_row(), _integ_row(), write_result)
    req = _make_request(pool)

    from services.acp_s4_blog.cms.base import CMSPostResult
    existing = CMSPostResult(post_id=99, post_url="https://tenant-blog.com/p/99",
                             status="publish", cms_type="wordpress")
    create = AsyncMock()
    lookup = AsyncMock(return_value=existing)
    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://tenant-blog.com", "username": "admin", "app_password": "pw"}), \
         patch.object(mod, "claim", _claimed(prior_attempt=True, publish_id=publish_id)), \
         patch.object(WordPressAdapter, "find_post_by_slug", lookup), \
         patch.object(WordPressAdapter, "create_post", create):
        result = await mod.publish(PIECE_ID, req, TENANT)

    assert result["success"] is True and result["external_id"] == "99"
    create.assert_not_called()
    assert lookup.call_args[0][0].startswith("the-chosen-angle-")
    finish_call = conn.fetchrow.call_args_list[-1]
    assert finish_call[0][1] == publish_id


@pytest.mark.asyncio
async def test_publish_retry_posts_when_lookup_finds_nothing():
    write_result = {"publish_id": str(uuid.uuid4()), "status": "published", "external_id": "5",
                     "external_url": "https://b.com/5", "published_at": None, "last_error": None}
    pool, _ = _sequential_pool(_piece_row(), _integ_row(), write_result)
    from services.acp_s4_blog.cms.base import CMSPostResult
    create = AsyncMock(return_value=CMSPostResult(post_id=5, post_url="https://b.com/5",
                                                  status="publish", cms_type="wordpress"))
    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://b.com", "username": "admin", "app_password": "pw"}), \
         patch.object(mod, "claim", _claimed(prior_attempt=True)), \
         patch.object(WordPressAdapter, "find_post_by_slug", AsyncMock(return_value=None)), \
         patch.object(WordPressAdapter, "create_post", create):
        result = await mod.publish(PIECE_ID, _make_request(pool), TENANT)
    assert result["success"] is True
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_publish_retry_does_not_post_blind_when_lookup_fails():
    write_result = {"publish_id": str(uuid.uuid4()), "status": "failed", "external_id": None,
                     "external_url": None, "published_at": None, "last_error": "lookup"}
    pool, conn = _sequential_pool(_piece_row(), _integ_row(), write_result)
    create = AsyncMock()
    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://b.com", "username": "admin", "app_password": "pw"}), \
         patch.object(mod, "claim", _claimed(prior_attempt=True)), \
         patch.object(WordPressAdapter, "find_post_by_slug",
                      AsyncMock(side_effect=RuntimeError("WP API lookup 503"))), \
         patch.object(WordPressAdapter, "create_post", create):
        result = await mod.publish(PIECE_ID, _make_request(pool), TENANT)
    assert result["success"] is False
    create.assert_not_called()
    assert conn.fetchrow.call_args_list[-1][0][3] == "failed"


@pytest.mark.asyncio
async def test_publish_same_revision_already_published_is_replayed_without_remote_call():
    row = {"publish_id": str(uuid.uuid4()), "status": "published", "external_id": "7",
           "external_url": "https://b.com/7", "published_at": datetime(2026, 10, 1, tzinfo=timezone.utc),
           "last_error": None, "idempotency_key": "k"}
    pool, _ = _sequential_pool(_piece_row(), _integ_row())
    create = AsyncMock()
    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://b.com", "username": "admin", "app_password": "pw"}), \
         patch.object(mod, "claim",
                      AsyncMock(return_value=Claim("published", publish_id=row["publish_id"], row=row))), \
         patch.object(WordPressAdapter, "create_post", create):
        result = await mod.publish(PIECE_ID, _make_request(pool), TENANT)
    assert result["replayed"] is True and result["external_id"] == "7" and result["success"] is True
    create.assert_not_called()


@pytest.mark.asyncio
async def test_publish_409_while_another_call_is_in_flight():
    pool, _ = _sequential_pool(_piece_row(), _integ_row())
    create = AsyncMock()
    with patch.object(mod, "_get_secret", return_value={
            "wp_url": "https://b.com", "username": "admin", "app_password": "pw"}), \
         patch.object(mod, "claim", AsyncMock(return_value=Claim("in_progress"))), \
         patch.object(WordPressAdapter, "create_post", create):
        with pytest.raises(HTTPException) as exc_info:
            await mod.publish(PIECE_ID, _make_request(pool), TENANT)
    assert exc_info.value.status_code == 409
    create.assert_not_called()


def test_publish_key_changes_with_text_and_channel():
    from services.acp_publish.idempotency import blog_slug, publish_key
    a = publish_key(PIECE_ID, "blog", "text")
    assert a == publish_key(PIECE_ID, "blog", "text")
    assert a != publish_key(PIECE_ID, "blog", "text edited")
    assert a != publish_key(PIECE_ID, "facebook", "text")
    assert blog_slug("Ha Long Bay — 3 days!", a) == f"ha-long-bay-3-days-{a.rsplit(':', 1)[-1][:8]}"
    assert blog_slug("", a).startswith("post-")


# ── WordPressAdapter.find_post_by_slug() (AA-726) ─────────────────────────────

def _mock_get_response(status: int, content_type: str = "application/json", body_text: str = ""):
    factory = _mock_post_response(status, content_type, body_text)
    session = factory.return_value.__aenter__.return_value
    session.get = session.post
    return factory


@pytest.mark.asyncio
async def test_find_post_by_slug_returns_existing_post():
    adapter = WordPressAdapter(wp_url="https://b.com", username="admin", app_password="pw")
    body = json.dumps([{"id": 9, "link": "https://b.com/p/9", "status": "publish"}])
    with patch("aiohttp.ClientSession", _mock_get_response(200, body_text=body)):
        found = await adapter.find_post_by_slug("x-1234abcd")
    assert found.post_id == 9 and found.post_url == "https://b.com/p/9"


@pytest.mark.asyncio
async def test_find_post_by_slug_none_when_empty_list():
    adapter = WordPressAdapter(wp_url="https://b.com", username="admin", app_password="pw")
    with patch("aiohttp.ClientSession", _mock_get_response(200, body_text="[]")):
        assert await adapter.find_post_by_slug("x-1234abcd") is None


@pytest.mark.asyncio
async def test_find_post_by_slug_raises_on_html_challenge_page():
    adapter = WordPressAdapter(wp_url="https://b.com", username="admin", app_password="pw")
    with patch("aiohttp.ClientSession", _mock_get_response(200, content_type="text/html", body_text="<html>")):
        with pytest.raises(RuntimeError, match="unexpected response"):
            await adapter.find_post_by_slug("x-1234abcd")


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
