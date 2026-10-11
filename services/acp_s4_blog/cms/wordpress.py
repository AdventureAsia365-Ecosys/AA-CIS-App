"""
WordPress REST API v2 adapter — PRD v1.0 Q7.
Auth: Application Password (WP 5.6+, base64 Basic auth).

AA-458: `content.status` (BlogContent's own field, previously declared but never read — this
adapter used to hardcode the literal string "draft" regardless) now controls the real WordPress
post status. Existing callers (services/acp_s4_blog/cms/publisher.py) never set `status`
explicitly, so they keep getting BlogContent's own default ("draft") — zero behavior change for
that pipeline. api/routers/v1_publish.py (AA-458's real publish endpoint) is the first caller to
pass `status="publish"`.
"""
import base64
import json
import logging
from typing import Optional

import aiohttp

from .base import CMSAdapter, BlogContent, CMSPostResult

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)


class WordPressAdapter(CMSAdapter):
    def __init__(self, wp_url: str, username: str, app_password: str):
        self.api_base = wp_url.rstrip("/") + "/wp-json/wp/v2"
        credentials = base64.b64encode(f"{username}:{app_password}".encode()).decode()
        self._auth_header = f"Basic {credentials}"

    def _headers(self, content_type: str = "application/json") -> dict:
        return {"Authorization": self._auth_header, "Content-Type": content_type}

    async def create_post(self, content: BlogContent) -> CMSPostResult:
        payload = {
            "title": content.seo_title or content.title,
            "content": content.content_html,
            "slug": content.slug,
            "status": content.status,
            "meta": {
                "_yoast_wpseo_title": content.seo_title,
                "_yoast_wpseo_metadesc": content.seo_meta,
            },
        }

        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(
                f"{self.api_base}/posts",
                json=payload,
                headers=self._headers(),
            ) as resp:
                status_code = resp.status
                content_type = resp.headers.get("content-type", "")
                body_text = await resp.text()

        if status_code not in (200, 201):
            raise RuntimeError(f"WP API {status_code}: {body_text[:300]}")

        # AA-460 lesson, applied here for the same reason: a 200/201 status alone doesn't mean
        # this is a real WordPress post response — a WAF/anti-bot challenge page, a maintenance
        # page, or a misconfigured catch-all route can all return a 2xx at this exact path.
        # Require content-type + real WordPress post shape (both "id" and "link") before trusting
        # it — anything else raises, so the caller (v1_publish.py) records a real 'failed'
        # publish_log row instead of a false 'published' one with fabricated external_id/url.
        parsed_body = None
        if content_type.startswith("application/json"):
            try:
                parsed_body = json.loads(body_text)
            except (json.JSONDecodeError, ValueError):
                parsed_body = None

        if not (isinstance(parsed_body, dict) and "id" in parsed_body and "link" in parsed_body):
            raise RuntimeError(
                f"WP API returned an unexpected response (not a real WordPress post): {body_text[:300]}"
            )

        return CMSPostResult(
            post_id=parsed_body["id"],
            post_url=parsed_body["link"],
            status=parsed_body.get("status", content.status),
            cms_type="wordpress",
        )

    async def find_post_by_slug(self, slug: str) -> Optional[CMSPostResult]:
        """AA-726 read-before-retry: the post a previous attempt created, if any. Publish sets a
        slug unique to the piece revision, so a match means the earlier call reached WordPress
        even though its response was lost. Raises on anything that is not a real WordPress list
        response — the caller must not post again when it cannot tell."""
        params = {"slug": slug, "status": "publish,future,draft,pending,private", "context": "edit"}
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.get(f"{self.api_base}/posts", params=params, headers=self._headers()) as resp:
                status_code = resp.status
                content_type = resp.headers.get("content-type", "")
                body_text = await resp.text()

        parsed_body = None
        if status_code == 200 and content_type.startswith("application/json"):
            try:
                parsed_body = json.loads(body_text)
            except (json.JSONDecodeError, ValueError):
                parsed_body = None
        if not isinstance(parsed_body, list):
            raise RuntimeError(f"WP API lookup {status_code} returned an unexpected response: {body_text[:300]}")

        for post in parsed_body:
            if isinstance(post, dict) and "id" in post and "link" in post:
                return CMSPostResult(post_id=post["id"], post_url=post["link"],
                                     status=post.get("status", ""), cms_type="wordpress")
        return None
