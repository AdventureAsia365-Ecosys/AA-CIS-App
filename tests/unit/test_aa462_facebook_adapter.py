"""AA-462 — services/acp_publish/facebook.py::FacebookAdapter.create_post().

Mirrors test_aa458_publish.py's own create_post() coverage for WordPressAdapter — same AA-460
lesson (never trust a bare 200/success status, validate real response shape), applied here to
the Facebook Graph API's own error/success shapes.
"""
import json as _json
from unittest.mock import AsyncMock, patch

import pytest

from services.acp_publish.base import SocialPost
from services.acp_publish.facebook import FacebookAdapter
from tests.unit.safe_http_fakes import TARGET, response


def _mock_response(status: int, json_body: dict = None, content_type: str = "application/json", body_text=None):
    if body_text is None:
        body_text = _json.dumps(json_body) if json_body is not None else ""
    return response(status, content_type, body_text)


def _mock_session(resp):
    """AA-875: the adapter calls shared.safe_http.request(); this stands in for it."""
    return AsyncMock(return_value=resp)


@pytest.mark.asyncio
class TestFacebookAdapter:
    async def test_create_post_success_real_graph_response(self):
        resp = _mock_response(200, {"id": "1234567890_998877"})
        adapter = FacebookAdapter(page_id="1234567890", page_access_token="tok")
        with patch(TARGET, _mock_session(resp)):
            result = await adapter.create_post(SocialPost(message="Hello world"))
        assert result.post_id == "1234567890_998877"
        assert result.post_url == "https://www.facebook.com/1234567890_998877"
        assert result.platform == "facebook"

    async def test_create_post_graph_error_shape_raises_with_message(self):
        """Graph API's own error envelope — {"error": {"code":..., "message":...}} — must be
        surfaced clearly, not treated as an ambiguous non-2xx."""
        resp = _mock_response(400, {"error": {"code": 190, "message": "Invalid OAuth access token"}})
        adapter = FacebookAdapter(page_id="123", page_access_token="bad-tok")
        with patch(TARGET, _mock_session(resp)):
            with pytest.raises(RuntimeError, match="Invalid OAuth access token"):
                await adapter.create_post(SocialPost(message="x"))

    async def test_create_post_200_but_error_body_still_raises(self):
        """Same AA-460 lesson as WordPressAdapter: don't trust the HTTP status code alone. Graph
        API can return a 200 with an error envelope in some edge cases (e.g. rate limiting) —
        must not be read as success just because status == 200."""
        resp = _mock_response(200, {"error": {"code": 4, "message": "Application request limit reached"}})
        adapter = FacebookAdapter(page_id="123", page_access_token="tok")
        with patch(TARGET, _mock_session(resp)):
            with pytest.raises(RuntimeError, match="Application request limit reached"):
                await adapter.create_post(SocialPost(message="x"))

    async def test_create_post_non_json_response_raises(self):
        """An HTML page (e.g. a captive portal, misconfigured proxy) at this URL must not be
        misread as success — same class of defense AA-460 added for WordPress's anti-bot page."""
        resp = _mock_response(200, content_type="text/html", body_text="<html>not graph api</html>")
        adapter = FacebookAdapter(page_id="123", page_access_token="tok")
        with patch(TARGET, _mock_session(resp)):
            with pytest.raises(RuntimeError, match="Facebook API 200"):
                await adapter.create_post(SocialPost(message="x"))

    async def test_create_post_missing_id_field_raises(self):
        resp = _mock_response(200, {"unexpected": "shape"})
        adapter = FacebookAdapter(page_id="123", page_access_token="tok")
        with patch(TARGET, _mock_session(resp)):
            with pytest.raises(RuntimeError):
                await adapter.create_post(SocialPost(message="x"))

    async def test_create_post_includes_link_when_provided(self):
        resp = _mock_response(200, {"id": "1_2"})
        adapter = FacebookAdapter(page_id="1", page_access_token="tok")
        fake = _mock_session(resp)
        with patch(TARGET, fake):
            await adapter.create_post(SocialPost(message="msg", link="https://example.com/tour"))
        assert fake.await_args.args == ("POST", "https://graph.facebook.com/v21.0/1/feed")
        sent_payload = fake.await_args.kwargs["data"]
        assert sent_payload["link"] == "https://example.com/tour"
        assert sent_payload["message"] == "msg"


@pytest.mark.asyncio
class TestFacebookFindRecentPost:
    """AA-726 read-before-retry — the Page feed is searched for the exact message."""

    async def test_finds_post_with_same_message(self):
        resp = _mock_response(200, {"data": [{"id": "1_1", "message": "Other"},
                                             {"id": "1_2", "message": "Hello world\n"}]})
        adapter = FacebookAdapter(page_id="1", page_access_token="tok")
        with patch(TARGET, _mock_session(resp)):
            found = await adapter.find_recent_post("Hello world")
        assert found.post_id == "1_2"

    async def test_none_when_not_in_feed(self):
        resp = _mock_response(200, {"data": [{"id": "1_1", "message": "Other"}, {"id": "1_3"}]})
        adapter = FacebookAdapter(page_id="1", page_access_token="tok")
        with patch(TARGET, _mock_session(resp)):
            assert await adapter.find_recent_post("Hello world") is None

    async def test_raises_when_feed_unreadable(self):
        resp = _mock_response(400, {"error": {"code": 190, "message": "bad token"}})
        adapter = FacebookAdapter(page_id="1", page_access_token="tok")
        with patch(TARGET, _mock_session(resp)):
            with pytest.raises(RuntimeError, match="feed lookup 400"):
                await adapter.find_recent_post("Hello world")


@pytest.mark.asyncio
async def test_page_id_is_one_encoded_path_segment():
    """AA-875: a tenant-entered page_id cannot add path segments or a query to the Graph URL."""
    fake = _mock_session(_mock_response(200, {"id": "x_1"}))
    adapter = FacebookAdapter(page_id="123/../../me?x=1", page_access_token="tok")
    with patch(TARGET, fake):
        await adapter.create_post(SocialPost(message="m"))
    assert fake.await_args.args[1] == "https://graph.facebook.com/v21.0/123%2F..%2F..%2Fme%3Fx%3D1/feed"
