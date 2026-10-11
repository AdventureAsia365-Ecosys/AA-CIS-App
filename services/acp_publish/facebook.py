"""Facebook Graph API adapter — Page feed post (AA-462).

AA-462 STEP0 (real DB query, not guessed): of the 6 non-blog channels named in this issue
(facebook/tiktok/instagram/linkedin/email/ads), facebook has the most real content_piece data
today (3 approved pieces, vs 0 for every other channel) — the one built here.

Auth: a Page Access Token (long-lived, tenant-generated via Meta Business Suite /
developers.facebook.com, saved through POST /v1/integrations/facebook). This adapter does not
handle the OAuth exchange itself — same division of concerns as WordPressAdapter, which also
takes a pre-obtained Application Password rather than performing WordPress's own login flow.

API: POST https://graph.facebook.com/{version}/{page_id}/feed  (message[, link], access_token)
https://developers.facebook.com/docs/graph-api/reference/page/feed/

Same AA-460 lesson WordPressAdapter already applies: a bare 200 status is not proof of a real
Graph API response — validates real JSON content-type + a genuine `{"id": ...}` post-id shape
(or surfaces Graph API's own `{"error": {...}}` body when present) before ever reporting
success, so a caller records a real 'failed' publish_log row instead of a fabricated
'published' one.

**Known limitation, disclosed (not fixed here)**: no real Facebook Developer App / test Page /
Page Access Token exists for this environment (checked — no facebook/meta secret in Secrets
Manager, confirmed via `aws secretsmanager list-secrets`). Provisioning one requires a live,
human-driven Meta Business/Developer Console flow this session cannot perform — the same class
of external-credential gap AA-458/460 already hit and disclosed for WordPress's own real test
site (`aa-wordpress.rf.gd`, anti-bot-blocked). Positive-path proof here is unit-test-only
(realistic mocked Graph API response); real E2E live-verify needs Nghiệp to provide a real
Page + Page Access Token.
"""
import json
from typing import Optional

import aiohttp

from .base import SocialAdapter, SocialPost, SocialPostResult

_TIMEOUT = aiohttp.ClientTimeout(total=30)
_GRAPH_VERSION = "v21.0"


class FacebookAdapter(SocialAdapter):
    def __init__(self, page_id: str, page_access_token: str):
        self.page_id = page_id
        self.access_token = page_access_token
        self.api_base = f"https://graph.facebook.com/{_GRAPH_VERSION}"

    async def create_post(self, post: SocialPost) -> SocialPostResult:
        payload = {"message": post.message, "access_token": self.access_token}
        if post.link:
            payload["link"] = post.link

        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(f"{self.api_base}/{self.page_id}/feed", data=payload) as resp:
                status_code = resp.status
                content_type = resp.headers.get("content-type", "")
                body_text = await resp.text()

        parsed_body = None
        if content_type.startswith("application/json"):
            try:
                parsed_body = json.loads(body_text)
            except (json.JSONDecodeError, ValueError):
                parsed_body = None

        if isinstance(parsed_body, dict) and "error" in parsed_body:
            err = parsed_body["error"]
            raise RuntimeError(
                f"Facebook Graph API error ({err.get('code', '?')}): {err.get('message', body_text[:200])}"
            )

        if status_code != 200 or not (isinstance(parsed_body, dict) and "id" in parsed_body):
            raise RuntimeError(f"Facebook API {status_code}: {body_text[:300]}")

        post_id = parsed_body["id"]  # Graph API shape: "{page_id}_{post_id}"
        return SocialPostResult(
            post_id=post_id,
            post_url=f"https://www.facebook.com/{post_id}",
            platform="facebook",
        )

    async def find_recent_post(self, message: str) -> Optional[SocialPostResult]:
        """AA-726 read-before-retry: a Page feed post carrying exactly this message, created by a
        previous attempt whose response was lost. Graph API has no client idempotency key, so
        the last 25 feed posts are matched on the full message text. Raises when the feed
        cannot be read — the caller must not post again when it cannot tell."""
        params = {"fields": "id,message", "limit": "25", "access_token": self.access_token}
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(f"{self.api_base}/{self.page_id}/feed", params=params) as resp:
                status_code = resp.status
                content_type = resp.headers.get("content-type", "")
                body_text = await resp.text()

        parsed_body = None
        if content_type.startswith("application/json"):
            try:
                parsed_body = json.loads(body_text)
            except (json.JSONDecodeError, ValueError):
                parsed_body = None
        if status_code != 200 or not (isinstance(parsed_body, dict) and isinstance(parsed_body.get("data"), list)):
            raise RuntimeError(f"Facebook feed lookup {status_code}: {body_text[:300]}")

        wanted = message.strip()
        for post in parsed_body["data"]:
            if isinstance(post, dict) and "id" in post and (post.get("message") or "").strip() == wanted:
                return SocialPostResult(post_id=post["id"], post_url=f"https://www.facebook.com/{post['id']}",
                                        platform="facebook")
        return None
