"""
services.acp_publish.dispatch — T11 channel dispatch (AA-765, moved out of api/routers/v1_publish.py).

The one place that knows, per publish channel, which tenant integration holds its credentials, which adapter
posts to it, how a post is built from a content piece, how an earlier attempt's post is found again, and how a
post is taken down. The router (`/v1/publish-log`) keeps HTTP, auth, the tenant pool, Secrets Manager and the
publish_log bookkeeping (idempotency.claim / finish, daily_limit) and calls these functions with the
credentials it read.

Channels: blog (WordPress, AA-456/458) and facebook (AA-462 — of the 6 non-blog channels it had real
approved pieces first). tiktok / instagram / linkedin / email / ads are not built; the router answers 404
"not yet supported" for them before anything here runs.

Every adapter validates its own response (the AA-460 lesson: never trust a bare 2xx) and raises on failure, so
a function here either returns a real post or raises.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from services.acp_content_writing import ai_marking
from services.acp_content_writing.export import render_content_text_to_fragment
from services.acp_publish.base import SocialPost
from services.acp_publish.facebook import FacebookAdapter
from services.acp_publish.idempotency import blog_slug
from services.acp_s4_blog.cms.base import BlogContent
from services.acp_s4_blog.cms.wordpress import WordPressAdapter

SUPPORTED_CHANNELS = ("blog", "facebook")
# AA-815 — shared.tenant_integrations.integration_type per channel (v1_integrations.py writes
# 'wordpress' / 'facebook'; the blog channel's connection is the WordPress one).
INTEGRATION_TYPE = {"blog": "wordpress", "facebook": "facebook"}


def channel_display_name(channel: str) -> str:
    """The name a tenant-facing "Connect X to publish" message uses: 'blog' -> 'WordPress' (the original AA-458
    copy), 'facebook' -> 'Facebook'; str.title() for any future channel."""
    return {"blog": "WordPress", "facebook": "Facebook"}.get(channel, channel.title())


def _wordpress(creds: dict) -> WordPressAdapter:
    return WordPressAdapter(wp_url=creds["wp_url"], username=creds["username"], app_password=creds["app_password"])


def _facebook(creds: dict) -> FacebookAdapter:
    return FacebookAdapter(page_id=creds["page_id"], page_access_token=creds["page_access_token"])


def piece_ai_mark(piece) -> dict:
    """AA-1040 — the AI mark of a piece row read by the publish route (`created_at`, `ai_provenance`); a row
    without them (an older caller) still gets `ai_generated`."""
    return ai_marking.piece_mark(piece.get("created_at"), piece.get("ai_provenance"))


def _slug(piece, key: str) -> str:
    return blog_slug(piece.get("slug") or piece.get("angle_name") or "", key)


async def find_remote(channel: str, creds: dict, piece, key: str) -> Optional[tuple]:
    """AA-726 — (external_id, external_url) of a post an earlier attempt already created, or None (WordPress by
    the revision slug, Facebook by the message). A lookup that fails raises, so the caller never posts blind."""
    if channel == "blog":
        result = await _wordpress(creds).find_post_by_slug(_slug(piece, key))
    else:
        result = await _facebook(creds).find_recent_post(piece["content_text"])
    return (str(result.post_id), result.post_url) if result else None


async def publish_to_channel(channel: str, creds: dict, piece, key: str) -> tuple:
    """Post `piece` to `channel`; returns (external_id, external_url), raises on any failure. One branch per
    supported channel, each building its own adapter from that channel's creds shape."""
    if channel == "blog":
        # AA-768 (S233) — the T9 blog's own SEO title/meta (fitted to the 45-60 / 140-155 band)
        # instead of the internal angle name and an empty meta.
        title = piece.get("seo_title") or piece["angle_name"] or "Untitled"
        # AA-613 — content_text is markdown (T9's blog prompt writes `## ` H2s, `**bold**`, FAQ
        # pairs). Publishing it raw into content_html made WordPress show literal `##`/`**`.
        # Render the markdown to a bare <article> fragment first (the CMS supplies the page
        # chrome; the standalone-document mode is for tenant download, not publish).
        # AA-1040 — the fragment carries the machine-readable AI mark (data-ai-* on the <article>, then the
        # <meta name="ai-*"> tags), so the published page says the text is AI-generated.
        content = BlogContent(
            title=title, content_html=render_content_text_to_fragment(piece["content_text"], ai=piece_ai_mark(piece)),
            slug=_slug(piece, key), seo_title=title, seo_meta=piece.get("meta_description") or "",
            status="publish",
        )
        result = await _wordpress(creds).create_post(content)
    else:  # channel == "facebook" — the only other channel SUPPORTED_CHANNELS lets through
        # AA-1040 — the Graph API Page feed has no AI label for a text post; the mark stays in the piece
        # (writer_input.ai_provenance) and its publish_log row (root AI Use Register, "Machine-readable marking").
        result = await _facebook(creds).create_post(SocialPost(message=piece["content_text"]))
    return str(result.post_id), result.post_url


async def put_back_remote(creds: dict, external_id: str) -> tuple:
    """AA-1039 (put back online): read the WordPress page the tour retirement drafted and publish it only when it is
    still a draft. Returns (outcome, WordPress status), see services/acp_publish/put_back.py; raises when WordPress
    cannot be read or refuses."""
    from services.acp_publish.put_back import remote_put_back
    return await remote_put_back(_wordpress(creds), external_id)


def _site_host(url: Optional[str]) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def other_account(channel: str, creds: dict, external_id: str, external_url: Optional[str]) -> Optional[str]:
    """AA-815 — the post was published to a different WordPress site / Facebook Page than the one
    connected now (the tenant changed credentials since). WordPress post IDs are per-site
    integers, so deleting `external_id` on the new site would trash an unrelated post. Returns
    where the post lives, or None when it is the connected account (or that cannot be told)."""
    if channel == "blog":
        posted, connected = _site_host(external_url), _site_host(creds.get("wp_url"))
        return posted if posted and connected and posted != connected else None
    page_id = str(creds.get("page_id") or "")
    posted_page = external_id.split("_", 1)[0] if "_" in external_id else ""
    return f"Page {posted_page}" if posted_page and page_id and posted_page != page_id else None


async def delete_remote(channel: str, creds: dict, external_id: str) -> str:
    """AA-815 — remove the live post. Returns the adapter's outcome ("trashed" / "already_gone" /
    "deleted"); raises when the channel did not confirm the removal."""
    if channel == "blog":
        return await _wordpress(creds).delete_post(external_id)
    return await _facebook(creds).delete_post(external_id)


__all__ = ["INTEGRATION_TYPE", "SUPPORTED_CHANNELS", "channel_display_name", "delete_remote", "find_remote",
           "other_account", "piece_ai_mark", "publish_to_channel", "put_back_remote"]
