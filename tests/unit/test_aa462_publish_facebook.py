"""AA-462 — POST /v1/publish-log/{piece_id}/publish, the facebook branch (api/routers/
v1_publish.py::publish() / _call_adapter()). Same call-sequence/mocking shape as
test_aa458_publish.py's own WordPress success/failure tests — piece lookup, integration
lookup, existing-failed check, final write, all via the same sequential-conn pattern.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import v1_publish as mod
from services.acp_publish.base import SocialPostResult
from services.acp_publish.facebook import FacebookAdapter
from services.acp_publish.idempotency import Claim

TENANT = {"sub": str(uuid.uuid4()), "role": "tenant"}
PIECE_ID = uuid.uuid4()


def _make_request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    return req


def _sequential_pool(*fetchrow_returns, fetchval_return=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_returns))
    conn.fetchval = AsyncMock(return_value=fetchval_return)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _fb_piece_row():
    return {"piece_id": str(PIECE_ID), "content_text": "Real facebook post content.",
            "status": "approved", "channel": "facebook", "angle_name": "The Chosen Angle"}


def _fb_integ_row():
    return {"secret_key": "acp/social/tenant-x"}


@pytest.fixture(autouse=True)
def _claimed():
    """AA-726 — publish() claims a publish_log row first; stubbed here (real-DB coverage in
    tests/integration/test_aa726_publish_idempotency.py). The last fetchrow is finish()."""
    with patch.object(mod, "claim", AsyncMock(return_value=Claim("claimed", publish_id=str(uuid.uuid4())))):
        yield


@pytest.mark.asyncio
class TestPublishFacebook:
    async def test_publish_422_when_facebook_not_connected(self):
        from fastapi import HTTPException
        pool, _ = _sequential_pool(_fb_piece_row(), None)
        req = _make_request(pool)
        with pytest.raises(HTTPException) as exc_info:
            await mod.publish(PIECE_ID, req, TENANT)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail == "Connect Facebook to publish"

    async def test_publish_success_writes_published_row_facebook(self):
        write_result = {"publish_id": str(uuid.uuid4()), "status": "published",
                         "external_id": "1_2", "external_url": "https://www.facebook.com/1_2",
                         "published_at": datetime(2026, 9, 4, tzinfo=timezone.utc), "last_error": None}
        pool, conn = _sequential_pool(_fb_piece_row(), _fb_integ_row(), write_result)
        req = _make_request(pool)

        fake_result = SocialPostResult(post_id="1_2", post_url="https://www.facebook.com/1_2", platform="facebook")

        with patch.object(mod, "_get_secret", return_value={"page_id": "1", "page_access_token": "tok"}), \
             patch.object(FacebookAdapter, "create_post", AsyncMock(return_value=fake_result)):
            result = await mod.publish(PIECE_ID, req, TENANT)

        assert result["success"] is True
        assert result["status"] == "published"
        assert result["external_id"] == "1_2"
        assert result["external_url"] == "https://www.facebook.com/1_2"

        finish_call = conn.fetchrow.call_args_list[-1]
        params = finish_call[0][1:]
        assert params[2] == "published"
        assert mod.claim.call_args.kwargs["channel"] == "facebook"

        # integration lookup queried integration_type = 'facebook', not 'wordpress'
        integ_call = conn.fetchrow.call_args_list[1]
        assert integ_call[0][2] == "facebook"

    async def test_publish_failure_records_failed_row_facebook(self):
        write_result = {"publish_id": str(uuid.uuid4()), "status": "failed",
                         "external_id": None, "external_url": None,
                         "published_at": None, "last_error": "Facebook Graph API error (190): bad token"}
        pool, conn = _sequential_pool(_fb_piece_row(), _fb_integ_row(), write_result)
        req = _make_request(pool)

        with patch.object(mod, "_get_secret", return_value={"page_id": "1", "page_access_token": "bad"}), \
             patch.object(FacebookAdapter, "create_post",
                          AsyncMock(side_effect=RuntimeError("Facebook Graph API error (190): bad token"))):
            result = await mod.publish(PIECE_ID, req, TENANT)

        assert result["success"] is False
        assert result["status"] == "failed"
        assert "bad token" in result["last_error"]

    async def test_list_pending_includes_facebook_pieces(self):
        rows = [{
            "piece_id": str(PIECE_ID), "content_text": "fb content",
            "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "channel": "facebook", "angle_name": "FB Angle",
            "route_hub_name": None, "route_segment_count": None,
        }]
        pool, conn = _sequential_pool()
        conn.fetch = AsyncMock(return_value=rows)
        req = _make_request(pool)

        result = await mod.list_pending(req, TENANT)
        assert result["total"] == 1
        assert result["data"][0]["channel"] == "facebook"
