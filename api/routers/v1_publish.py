"""
/v1/publish-log — T11 Publish. Tenant self-unpublish (AA-455 bước 1) + the real publish layer
(AA-458, T11 PR2): list content approved-and-not-yet-published, and publish one piece to the
tenant's connected WordPress site.

STEP0 (docs/claude_audit/AA-455-01-step0-a4-force-unpublish.md §6) flagged tenant self-unpublish
as undecided by ADR-2026-038 §0.2. Nghiep's decision (Linear AA-455 update, 24/08/2026): Option 2
— tenant self-unpublish IS built, consistent with the same self-service philosophy already
applied to T3/T6/T7/T8 (tenant operates, AA only monitors post-hoc). Separate module rather than
folded into v1_content_writing.py — `publish_log` is its own resource (T11's, not T9/T10's), same
reasoning migration 115 itself used for NOT denormalizing content_piece's parent fields onto
angle_gate_option: a distinct resource gets its own place, callers join back when they need
context. Reuses `get_tenant` from v1_tours.py unchanged (same shared dependency
v1_content_writing.py/v1_angle_gate.py already use — no new auth mechanism).

Ownership checks throughout follow v1_competitors.py:187's exact precedent: `WHERE id = $1 AND
tenant_id = $2`, 404 (not 403) when the row isn't the caller's — doesn't distinguish "not found"
from "not yours" in the response, same anti-IDOR shape AA-445-02/AA-431 already established.

AA-458's publish() applies the AA-460 lesson (see api/routers/v1_integrations.py::test_wordpress
for the original fix this mirrors): WordPressAdapter.create_post() (services/acp_s4_blog/cms/
wordpress.py) now does its own content-type/JSON/shape validation before returning a result —
this router never trusts a bare 200/201 status either, it just relies on create_post() to have
already raised if the response wasn't real WordPress, and records 'failed' + the real error
message rather than a fabricated 'published' row.

AA-462 — adds `facebook` (STEP0: of the 6 non-blog channels named in that issue, facebook has
the most real content_piece data today — 3 approved pieces vs 0 everywhere else, real DB
query). `_publish_to_channel()` dispatches on `channel`; the WordPress branch is the exact
pre-AA-462 logic unchanged, the Facebook branch mirrors its shape 1:1 (own integration lookup,
own adapter, same AA-460-lesson validation inside FacebookAdapter.create_post()). The other 5
channels (tiktok/instagram/linkedin/email/ads) remain unbuilt — 404 with a clear "not yet
supported" message rather than a generic "not found", so a future caller hitting one doesn't
read it as a content/ownership problem.

AA-815 — unpublish now takes the post off the channel (WordPress trash / Graph DELETE) before the
row flips; a remote failure leaves the row 'published' and returns the reason. GET "" lists the
tenant's published/unpublished rows for the portal's Unpublish action. The integration lookup maps
channel -> integration_type ('blog' -> 'wordpress'): v1_integrations.py stores WordPress under
'wordpress', so the AA-462 `integration_type = channel` lookup never found a blog connection.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional
from urllib.parse import urlparse
from uuid import UUID

import boto3
import structlog
from botocore.exceptions import ClientError
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from api.core.aa544_tenant_pool import (
    PUBLISH_LOG_LIST_FLAG, STAGE1_PUBLISH_PENDING_FLAG, UNPUBLISH_FLAG, acquire_scoped_conn,
)
from api.routers.v1_integrations import is_tombstoned_secret, reconnect_detail
from api.routers.v1_tours import get_tenant
from services.acp_content_writing.export import render_content_text_to_fragment
from services.acp_shared.audit_log import TenantAuditAction, write_audit_log
from services.acp_publish import daily_limit
from services.acp_publish.base import SocialPost
from services.acp_publish.facebook import FacebookAdapter
from services.acp_publish.idempotency import blog_slug, claim, finish, publish_key
from services.acp_s4_blog.cms.base import BlogContent
from services.acp_s4_blog.cms.wordpress import WordPressAdapter

logger = structlog.get_logger()
router = APIRouter(prefix="/v1/publish-log", tags=["publish-log"])

_SM_REGION = "us-west-1"
# T11 scope: blog (AA-456/458) + facebook (AA-462). 5 channels remain unbuilt — see this
# file's own header for why facebook was picked first (real STEP0 data, not a guess).
_SUPPORTED_CHANNELS = ("blog", "facebook")
# AA-815 — shared.tenant_integrations.integration_type per channel (v1_integrations.py writes
# 'wordpress' / 'facebook'; the blog channel's connection is the WordPress one).
_INTEGRATION_TYPE = {"blog": "wordpress", "facebook": "facebook"}


def _get_secret(secret_key: str) -> dict:
    """Same shape as v1_integrations.py's own _get_secret() — arbitrary secret_key at call time,
    no caching. Not imported from there to avoid a cross-router import for one small function;
    matches this codebase's own precedent of small, duplicated-on-purpose secret-fetch helpers
    (v1_integrations.py itself duplicated services/acp_s4_blog/cms/publisher.py::_get_cms_creds()
    for the same reason).

    AA-859: a fresh Session per call, as in v1_integrations._sm_client: publish and unpublish both run
    this in an asyncio.to_thread worker (the Session build is ~50 ms and must stay off the event loop),
    and boto3.client() on the shared default Session is not thread-safe."""
    client = boto3.session.Session().client("secretsmanager", region_name=_SM_REGION)
    return json.loads(client.get_secret_value(SecretId=secret_key)["SecretString"])


@router.get("")
async def list_publish_log(request: Request, tenant=Depends(get_tenant),
                           limit: int = Query(50, ge=1, le=200)):
    """AA-815 — the tenant's own published and unpublished rows, newest first, for the portal's
    "Published" list (the Unpublish action needs a publish_id). Tenant-safe projection: no
    last_error, idempotency key or actor — a removal shows as 'unpublished' whoever did it.
    Runs through acquire_scoped_conn behind its own flag (AA-544 rollout), fail closed (AA-817 PR-A):
    flag on without a tenant pool or tenant connection is a 503, never the admin pool. S241 D-12: a missing
    key is ON; rollback = SET <key> false (see api/core/aa544_tenant_pool.py)."""
    tenant_id = tenant["sub"]
    async with acquire_scoped_conn(
        request, tenant_id, PUBLISH_LOG_LIST_FLAG, fail_closed=True,
    ) as (conn, used_tenant_pool):
        rows = await conn.fetch(
            """
            SELECT pl.publish_id::text, pl.piece_id::text, pl.channel, pl.status, pl.external_url,
                   pl.published_at, pl.unpublished_at, ago.name AS angle_name
            FROM acp_shared.publish_log pl
            JOIN acp_shared.content_piece cp ON cp.piece_id = pl.piece_id
            JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
            LEFT JOIN acp_shared.angle_gate_option ago
                ON ago.option_id = cp.angle_gate_option_id
                OR (cp.angle_gate_option_id IS NULL AND ago.request_id = agr.request_id
                    AND ago.chosen = true)
            WHERE pl.tenant_id = $1::uuid AND pl.status IN ('published', 'unpublished')
            ORDER BY COALESCE(pl.unpublished_at, pl.published_at, pl.created_at) DESC
            LIMIT $2
            """,
            tenant_id, limit,
        )
    logger.info("publish_log_listed", tenant_id=tenant_id, count=len(rows), used_tenant_pool=used_tenant_pool)
    return {
        "data": [
            {
                "publish_id": r["publish_id"],
                "piece_id": r["piece_id"],
                "title": r["angle_name"] or "Untitled",
                "channel": r["channel"],
                "status": r["status"],
                "external_url": r["external_url"],
                "published_at": r["published_at"].isoformat() if r["published_at"] else None,
                "unpublished_at": r["unpublished_at"].isoformat() if r["unpublished_at"] else None,
            }
            for r in rows
        ],
        "total": len(rows),
    }


def _site_host(url: Optional[str]) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _other_account(channel: str, creds: dict, external_id: str, external_url: Optional[str]) -> Optional[str]:
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


async def _delete_remote(channel: str, creds: dict, external_id: str) -> str:
    """AA-815 — remove the live post. Returns the adapter's outcome ("trashed" / "already_gone" /
    "deleted"); raises when the channel did not confirm the removal."""
    if channel == "blog":
        return await _wordpress(creds).delete_post(external_id)
    return await _facebook(creds).delete_post(external_id)


# AA-859 W4 — unpublish's three statements, module-level so tests can mutate them (forgotten-WHERE checks).
# Each runs in its own short block; nothing holds a connection across Secrets Manager or the remote delete.
_UNPUBLISH_SELECT_SQL = """
    SELECT publish_id::text, piece_id::text, channel, external_id, external_url
    FROM acp_shared.publish_log
    WHERE publish_id = $1 AND tenant_id = $2::uuid AND status = 'published'
"""
_UNPUBLISH_INTEGRATION_SQL = (
    "SELECT secret_key FROM shared.tenant_integrations "
    "WHERE tenant_id = $1::uuid AND integration_type = $2"
)
# `AND status = 'published'`: a concurrent unpublish (double click) that flipped the row while this request
# was on the remote delete makes this 0 rows -> 404, never a second flip.
_UNPUBLISH_UPDATE_SQL = """
    UPDATE acp_shared.publish_log
    SET status = 'unpublished', unpublished_at = now(), unpublished_by = $3
    WHERE publish_id = $1 AND tenant_id = $2::uuid AND status = 'published'
    RETURNING publish_id::text, channel, status, unpublished_at
"""


@router.delete("/{publish_id}")
async def unpublish(publish_id: UUID, request: Request, tenant=Depends(get_tenant)):
    """Tenant self-unpublish. Only a `status='published'` row the caller's own tenant_id owns —
    another tenant's row, or one already unpublished/failed, 404s identically (no cross-tenant
    existence leak, AA-445-02 lesson). `unpublished_by` records "tenant:<tenant_id>",
    distinguishing this from A4's "admin:<id>" force-unpublish on the same table.

    AA-815 — the post is removed from the channel FIRST (WordPress: moved to trash; Facebook:
    Graph DELETE), using publish_log.external_id. Only a confirmed removal flips the row; a
    missing connection (422), unreadable credentials or a channel error (502) leave it
    'published', so the portal never tells a tenant a post is gone while it is still public.
    A post published to another WordPress site / Facebook Page than the one connected now is
    refused (422) before any remote call — a WordPress post ID only means something on its site.

    AA-859 W4 — behind UNPUBLISH_FLAG (fail closed): three short acquire_scoped_conn blocks (publish_log
    SELECT, tenant_integrations SELECT, the status-guarded UPDATE), so with the flag on each runs as
    aa_app_user under RLS (229 publish_log, 237 tenant_integrations). The Secrets Manager read
    (asyncio.to_thread) and the remote delete run with no connection held. Flag off: the same three
    statements on the admin pool, as before. One `aa859_unpublish` line per request carries
    used_tenant_pool (of the first block)."""
    pool = request.app.state.pool
    tenant_id = tenant["sub"]

    # 1. the caller's published row
    async with acquire_scoped_conn(request, tenant_id, UNPUBLISH_FLAG, fail_closed=True) as (conn, used_tenant_pool):
        logger.info("aa859_unpublish", publish_id=str(publish_id), tenant_id=tenant_id,
                    used_tenant_pool=used_tenant_pool)
        row = await conn.fetchrow(_UNPUBLISH_SELECT_SQL, publish_id, tenant_id)
    if not row:
        raise HTTPException(status_code=404, detail="publish_log row not found or already unpublished")

    channel = row["channel"]
    name = _connect_action_verb(channel)
    if channel not in _SUPPORTED_CHANNELS or not row["external_id"]:
        raise HTTPException(status_code=422,
                            detail=f"This post has no {name} post ID on record — remove it on {name} directly.")

    # 2. the channel's connection
    async with acquire_scoped_conn(request, tenant_id, UNPUBLISH_FLAG, fail_closed=True) as (conn, _):
        integ = await conn.fetchrow(_UNPUBLISH_INTEGRATION_SQL, tenant_id, _INTEGRATION_TYPE[channel])
    if not integ or not integ["secret_key"]:
        raise HTTPException(status_code=422,
                            detail=f"Connect {name} to unpublish — the post is still live on {name}.")
    try:  # no connection held: sync boto3 off the event loop
        creds = await asyncio.to_thread(_get_secret, integ["secret_key"])
    except ClientError as exc:
        logger.error("unpublish_secret_read_failed", tenant_id=tenant_id, channel=channel, error=str(exc))
        raise HTTPException(status_code=502, detail="Could not read saved credentials — try reconnecting")
    if is_tombstoned_secret(creds):  # AA-859: a disconnect tombstoned them; the post is still live
        raise HTTPException(status_code=422, detail=reconnect_detail(name))

    elsewhere = _other_account(channel, creds, row["external_id"], row["external_url"])
    if elsewhere:
        raise HTTPException(status_code=422,
                            detail=f"This post was published to {elsewhere}, not the {name} account connected "
                                   f"now — remove it there directly.")

    try:  # no connection held across the channel's HTTP call
        remote = await _delete_remote(channel, creds, row["external_id"])
    except Exception as exc:  # noqa: BLE001 — the adapter raises only when removal is unconfirmed
        logger.warning("tenant_unpublish_remote_failed", tenant_id=tenant_id, publish_id=str(publish_id),
                       channel=channel, error=str(exc))
        raise HTTPException(status_code=502,
                            detail=f"Could not remove the post from {name}: {str(exc)[:300]}")

    # 3. flip the row, only if it is still 'published'
    async with acquire_scoped_conn(request, tenant_id, UNPUBLISH_FLAG, fail_closed=True) as (conn, used_for_update):
        updated = await conn.fetchrow(_UNPUBLISH_UPDATE_SQL, publish_id, tenant_id, f"tenant:{tenant_id}")
    if not updated:  # a concurrent unpublish flipped it between the read and here
        raise HTTPException(status_code=404, detail="publish_log row not found or already unpublished")

    logger.info("tenant_self_unpublish", publish_id=str(publish_id), tenant_id=tenant_id,
                channel=channel, remote=remote, used_tenant_pool=used_for_update)
    # AA-859 MAIN exception: audit_log has no RLS and no aa_app_user grant; written on the admin pool after
    # the tenant transaction committed.
    try:  # best-effort, same as publish(): a lost audit row must not turn a removal into an error
        await write_audit_log(
            pool, tenant_id=str(tenant_id), actor=f"tenant:{tenant_id}",
            action=TenantAuditAction.CONTENT_UNPUBLISHED, resource_type="content_piece",
            resource_id=row["piece_id"],
            details={"channel": channel, "publish_id": updated["publish_id"],
                     "external_id": row["external_id"], "remote": remote},
        )
    except Exception as exc:
        logger.warning("unpublish_audit_log_failed", tenant_id=tenant_id, publish_id=str(publish_id), error=str(exc))

    return {
        "publish_id": updated["publish_id"],
        "channel": updated["channel"],
        "status": updated["status"],
        "unpublished_at": updated["unpublished_at"].isoformat() if updated["unpublished_at"] else None,
        "unpublished_by": f"tenant:{tenant_id}",
        "remote": remote,
    }


# AA-802 (S238 scope) — the latest-piece rule My Content already applies (AA-497,
# services/acp_content_writing/service.py::_TENANT_REVIEWS_QUERY): a request can hold several
# content_piece rows (reopen / re-write / earlier attempts), and only the LATEST one by created_at
# is the tenant's content. `latest` picks it over EVERY status first; the status, channel and
# publish_log filters run after, so a superseded approved piece never resurfaces when the newest
# piece is still processing / failed, or is already published or in flight.
_PENDING_SQL = """
    SELECT cp.piece_id::text, cp.content_text, cp.created_at,
           COALESCE(cp.channel, agr.channel) AS channel, ago.name AS angle_name,
           cp.route_hub_name, cp.route_segment_count
    FROM (
        SELECT DISTINCT ON (angle_gate_request_id) piece_id
        FROM acp_shared.content_piece
        WHERE tenant_id = $1::uuid
        ORDER BY angle_gate_request_id, created_at DESC
    ) latest
    JOIN acp_shared.content_piece cp ON cp.piece_id = latest.piece_id
    JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
    LEFT JOIN acp_shared.angle_gate_option ago
        ON ago.option_id = cp.angle_gate_option_id
        OR (cp.angle_gate_option_id IS NULL AND ago.request_id = agr.request_id
            AND ago.chosen = true)
    WHERE cp.tenant_id = $1::uuid
      AND cp.status IN ('approved', 'held')  -- AA-770: the tenant always receives the content
      AND COALESCE(cp.channel, agr.channel) = ANY($2::text[])
      AND NOT EXISTS (
          SELECT 1 FROM acp_shared.publish_log pl
          WHERE pl.piece_id = cp.piece_id AND pl.status IN ('published', 'requested')
      )
    ORDER BY cp.created_at DESC
"""


@router.get("/pending")
async def list_pending(request: Request, tenant=Depends(get_tenant)):
    """AA-458 — the list endpoint AA-456's own STEP0 §5 flagged as missing: a tenant's approved
    content_piece rows for the blog channel that have no successful publish yet. A piece with a
    prior FAILED attempt (or none at all, or a since-unpublished one) still shows up here — only
    a row with a currently-'published' publish_log entry is excluded, so a failed/unpublished
    piece stays retryable rather than disappearing.

    AA-544 Stage 1 canary — this is the first route wired to the aa_app_user pool (real RLS)
    behind its own Redis flag (see api/core/aa544_tenant_pool.py). S241 D-12: a missing key is ON
    (the default); rollback = SET <key> false, which gives the pre-AA-544 behavior, admin pool, no RLS.
    Flag on: runs through aa_app_user with app.tenant_id set LOCAL to this request's transaction. Response shape is
    unchanged either way — the WHERE cp.tenant_id = $1 clause below already scoped this query
    correctly before RLS existed; RLS is defense-in-depth here, not the only thing standing
    between tenants for this specific query. AA-817 PR-A: fail closed — flag on without a tenant
    pool or tenant connection is a 503, never the admin pool.

    AA-497 — angle_name now joins via cp.angle_gate_option_id (denormalized at write time,
    migration 124/AA-497) rather than ago.chosen=true, which is MUTABLE (AA-497's reopen action
    can re-point it to a different option after this piece was already written) — falls back to
    the old chosen=true join only for pre-AA-497 rows where angle_gate_option_id is NULL.

    AA-469 Việc 4 (flow-order fix) — filters/returns cp.channel (content_piece), not agr.channel
    (angle_gate_request), for the exact same reason: angle_gate_request.channel is now settable
    more than once (services/acp_angle_gate/service.py::set_channel(), step 8, callable
    repeatedly while still 'approved') — a piece's OWN channel must stay pinned to whatever it
    was actually written with, not whatever the request's channel later becomes for its next
    write. Falls back to agr.channel only for pre-this-session rows where cp.channel is NULL
    (0 such rows existed live at the time of this change, confirmed — this fallback is pure
    defense, not a real backfill gap).

    AA-802 (S238) — one card per request at most: only the request's latest piece (see
    _PENDING_SQL). Before this, every older approved/held piece of the same request was its own
    card (atlas-hearth: 46 cards for 3 titles). A request whose latest piece is published or
    'requested' shows nothing."""
    tenant_id = tenant["sub"]

    async with acquire_scoped_conn(
        request, tenant_id, STAGE1_PUBLISH_PENDING_FLAG, fail_closed=True,
    ) as (conn, used_tenant_pool):
        rows = await conn.fetch(_PENDING_SQL, tenant_id, list(_SUPPORTED_CHANNELS))
    logger.info("aa544_stage1_list_pending", tenant_id=tenant_id, used_tenant_pool=used_tenant_pool)

    return {
        "data": [
            {
                "piece_id": r["piece_id"],
                "title": r["angle_name"] or "Untitled",
                "content_preview": r["content_text"][:280],
                "channel": r["channel"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                # AA-519 Việc 4 — NULL/None for every non-Route piece (Segment pick or pre-Slate
                # atom-picker request), same convention as T10's fetch_review()/fetch_review_list().
                "route_hub_name": r["route_hub_name"],
                "route_segment_count": r["route_segment_count"],
            }
            for r in rows
        ],
        "total": len(rows),
    }


def _connect_action_verb(channel: str) -> str:
    """A 422 "connect X to publish" message is only useful with a real display name filled in
    — 'wordpress' -> 'WordPress' (matches the original AA-458 copy exactly, "Connect WordPress
    to publish"), 'facebook' -> 'Facebook'. Falls back to str.title() for any future channel."""
    return {"blog": "WordPress", "facebook": "Facebook"}.get(channel, channel.title())


def _wordpress(creds: dict) -> WordPressAdapter:
    return WordPressAdapter(wp_url=creds["wp_url"], username=creds["username"], app_password=creds["app_password"])


def _facebook(creds: dict) -> FacebookAdapter:
    return FacebookAdapter(page_id=creds["page_id"], page_access_token=creds["page_access_token"])


def _slug(piece, key: str) -> str:
    return blog_slug(piece.get("slug") or piece.get("angle_name") or "", key)


async def _find_remote(channel: str, creds: dict, piece, key: str) -> Optional[tuple]:
    """AA-726 — (external_id, external_url) of a post an earlier attempt already created, or None."""
    if channel == "blog":
        result = await _wordpress(creds).find_post_by_slug(_slug(piece, key))
    else:
        result = await _facebook(creds).find_recent_post(piece["content_text"])
    return (str(result.post_id), result.post_url) if result else None


async def _call_adapter(channel: str, creds: dict, piece, key: str) -> tuple:
    """Returns (external_id, external_url) on success, raises on any failure — one branch per
    supported channel, each building its own adapter from that channel's own creds shape and
    relying on the adapter's own AA-460-lesson validation (never trusts a bare success status
    itself). Unknown channels never reach here — publish() already 404s before calling this."""
    if channel == "blog":
        # AA-768 (S233) — the T9 blog's own SEO title/meta (fitted to the 45-60 / 140-155 band)
        # instead of the internal angle name and an empty meta.
        title = piece.get("seo_title") or piece["angle_name"] or "Untitled"
        # AA-613 — content_text is markdown (T9's blog prompt writes `## ` H2s, `**bold**`, FAQ
        # pairs). Publishing it raw into content_html made WordPress show literal `##`/`**`.
        # Render the markdown to a bare <article> fragment first (the CMS supplies the page
        # chrome; the standalone-document mode is for tenant download, not publish).
        content = BlogContent(
            title=title, content_html=render_content_text_to_fragment(piece["content_text"]),
            slug=_slug(piece, key), seo_title=title, seo_meta=piece.get("meta_description") or "",
            status="publish",
        )
        result = await _wordpress(creds).create_post(content)
    else:  # channel == "facebook" — the only 2 branches _SUPPORTED_CHANNELS allows through
        result = await _facebook(creds).create_post(SocialPost(message=piece["content_text"]))
    return str(result.post_id), result.post_url


@router.post("/{piece_id}/publish")
async def publish(piece_id: UUID, request: Request, tenant=Depends(get_tenant)):
    """AA-458 — the real publish endpoint. No AA pre-gate (ADR-2026-038 §0.2, same principle
    AA-455's force-unpublish exists to police after the fact, not before). Ownership + approval
    verified in one query; 404 covers both "not yours" and "not approved yet" identically (no
    existence leak). A retryable prior 'failed' publish_log row for this piece is updated in
    place rather than accumulating duplicate failure rows; a 'published' or 'unpublished' row is
    left untouched and a fresh attempt gets its own new row (preserves that row's real
    unpublished_at/unpublished_by audit trail — AA-455's whole reason for those columns).

    AA-497 — same angle_gate_option_id-first join as list_pending() above, same reason.

    AA-469 Việc 4 — same cp.channel-first (COALESCE onto agr.channel) read as list_pending()
    above, same reason (angle_gate_request.channel is no longer stable after a piece is
    written — see set_channel()).

    AA-726 — idempotent: a 'requested' row is written before the remote call (one per piece
    revision + channel, services/acp_publish/idempotency.py). The same revision already published
    is replayed from publish_log (no second post); one in flight returns 409; a retry after a
    lost response looks the post up on the remote first (WordPress by revision slug, Facebook by
    message) before posting.

    AA-819 (AUP 2026-11) — per-tenant daily publish limit (services/acp_publish/daily_limit.py): code
    default 20 per UTC day, admin override shared.tenants.daily_publish_limit (migration 230; read only
    when the column exists). A NEW publish over the limit gets 429 with a clear message and a
    Retry-After to 00:00 UTC; replays and 409 in-flight retries never count and are never refused."""
    pool = request.app.state.pool
    tenant_id = tenant["sub"]

    async with pool.acquire() as conn:
        piece = await conn.fetchrow(
            """
            SELECT cp.piece_id, cp.content_text, cp.status, cp.slug, cp.seo_title, cp.meta_description,
                   COALESCE(cp.channel, agr.channel) AS channel, ago.name AS angle_name,
                   EXISTS (SELECT 1 FROM acp_shared.content_piece newer
                           WHERE newer.angle_gate_request_id = cp.angle_gate_request_id
                             AND newer.tenant_id = cp.tenant_id
                             AND newer.created_at > cp.created_at) AS superseded
            FROM acp_shared.content_piece cp
            JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
            LEFT JOIN acp_shared.angle_gate_option ago
                ON ago.option_id = cp.angle_gate_option_id
                OR (cp.angle_gate_option_id IS NULL AND ago.request_id = agr.request_id
                    AND ago.chosen = true)
            WHERE cp.piece_id = $1::uuid AND cp.tenant_id = $2::uuid
            """,
            piece_id, tenant_id,
        )

    if not piece:
        raise HTTPException(status_code=404, detail="Content piece not found")
    # AA-613 — publish gate under the 3-tier severity model: only 'approved' pieces publish.
    # A warn-only piece (off-brand voice / framework miss) already finalized as 'approved', so it
    # publishes fine. A 'held' piece = unresolved product-truth after ≤2 retries (grounding,
    # banned, missing CTA, FACT_CHECK, cannibalization) — it is fully visible to the tenant in My
    # Content and editable, but must NOT be published until the tenant edits it (which re-opens it
    # for a fresh write) or an admin clears it. 422 (not 404) here because the tenant DOES own this
    # piece and the reason is actionable — no existence leak (ownership already confirmed above).
    # AA-770 (S233) — the tenant always receives the content: a 'held' piece (a gate verdict, now
    # an admin-only quality label) publishes like an approved one. Only a piece with no content
    # yet ('processing') or a failed write is refused.
    if piece["status"] not in ("approved", "held"):
        raise HTTPException(
            status_code=422,
            detail="This piece isn't written yet — try again once it shows as Ready.",
        )
    # AA-802 (S238) — only a request's latest piece is publishable (the /pending rule above). An
    # older version (earlier attempt, a re-write after a reopen) is refused even when the caller
    # still holds its piece_id from a stale page. 409: the tenant owns it, a newer version exists.
    if piece.get("superseded"):
        raise HTTPException(
            status_code=409,
            detail="A newer version of this content exists — reload and publish that one.",
        )
    channel = piece["channel"]
    if channel not in _SUPPORTED_CHANNELS:
        raise HTTPException(status_code=404, detail=f"Publishing to '{channel}' is not yet supported")

    async with pool.acquire() as conn:
        integ = await conn.fetchrow(
            "SELECT secret_key FROM shared.tenant_integrations "
            "WHERE tenant_id = $1::uuid AND integration_type = $2",
            tenant_id, _INTEGRATION_TYPE[channel],
        )

    if not integ or not integ["secret_key"]:
        raise HTTPException(status_code=422, detail=f"Connect {_connect_action_verb(channel)} to publish")

    try:  # no connection held: sync boto3 off the event loop (a fresh Session per call costs ~50 ms)
        creds = await asyncio.to_thread(_get_secret, integ["secret_key"])
    except ClientError as exc:
        logger.error("publish_secret_read_failed", tenant_id=tenant_id, channel=channel,
                     secret_key=integ["secret_key"], error=str(exc))
        raise HTTPException(status_code=502, detail="Could not read saved credentials — try reconnecting")
    if is_tombstoned_secret(creds):  # AA-859: refused before claim(), so no publish_log row is written
        raise HTTPException(status_code=422, detail=reconnect_detail(_connect_action_verb(channel)))

    key = publish_key(piece_id, channel, piece["content_text"])
    async with pool.acquire() as conn:
        # AA-819 — the override read runs before claim()'s transaction (a missing column must not abort it)
        limit = await daily_limit.effective_limit(conn, tenant_id)
        claimed = await claim(conn, piece_id=piece_id, tenant_id=tenant_id, channel=channel, key=key,
                              daily_limit=limit)

    if claimed.state == "limited":
        logger.info("publish_daily_limit_reached", tenant_id=tenant_id, piece_id=str(piece_id), channel=channel,
                    used=claimed.used, limit=claimed.limit)
        raise HTTPException(status_code=429, detail=daily_limit.limit_message(claimed.used, claimed.limit),
                            headers={"Retry-After": str(daily_limit.seconds_until_reset())})
    if claimed.state == "in_progress":
        raise HTTPException(status_code=409,
                            detail="This piece is already being published — refresh in a minute.")
    if claimed.state == "published":
        # AA-726 — same revision already on the channel: answer from the stored row, never post twice.
        row = claimed.row
        logger.info("channel_publish_replayed", tenant_id=tenant_id, piece_id=str(piece_id),
                    channel=channel, publish_id=row["publish_id"])
        return _publish_response(row, success=True, replayed=True)

    try:
        found = None
        if claimed.prior_attempt:
            # AA-726 read-before-retry — an earlier attempt may have posted although its
            # response was lost. A lookup that fails raises, so we never post blind.
            found = await _find_remote(channel, creds, piece, key)
        if found:
            external_id, external_url = found
            logger.info("channel_publish_found_remote", tenant_id=tenant_id, piece_id=str(piece_id),
                        channel=channel, external_id=external_id)
        else:
            external_id, external_url = await _call_adapter(channel, creds, piece, key)
        success = True
        error_msg = None
    except Exception as exc:  # noqa: BLE001 — the adapter already validated the response shape
        # (AA-460 lesson); anything reaching here is a real failure (network error, non-2xx, or
        # an unexpected response) with a clear message, not a fabricated success.
        success = False
        external_id = None
        external_url = None
        error_msg = str(exc)[:500]

    async with pool.acquire() as conn:
        row = await finish(conn, publish_id=claimed.publish_id, tenant_id=tenant_id, success=success,
                           external_id=external_id, external_url=external_url, error=error_msg)

    logger.info("channel_publish", tenant_id=tenant_id, piece_id=str(piece_id), channel=channel,
                success=success, external_id=external_id, error=error_msg)

    # AA-613 — audit every publish attempt so admin monitor can count publishes per tenant/
    # channel/time (publish_log already records the row; this is the tenant-activity feed's own
    # semantic event, same as export's CONTENT_EXPORTED). Best-effort — a lost audit row must
    # never turn a real successful publish into an error the tenant sees.
    try:
        await write_audit_log(
            pool, tenant_id=str(tenant_id), actor=f"tenant:{tenant_id}",
            action=TenantAuditAction.CONTENT_PUBLISHED, resource_type="content_piece",
            resource_id=str(piece_id),
            details={"channel": channel, "success": success,
                     "external_url": external_url, "publish_id": row["publish_id"]},
        )
    except Exception as exc:
        logger.warning("publish_audit_log_failed", tenant_id=tenant_id, piece_id=str(piece_id), error=str(exc))

    return _publish_response(row, success=success)


def _publish_response(row: dict, *, success: bool, replayed: bool = False) -> dict:
    return {
        "publish_id": row["publish_id"],
        "success": success,
        "status": row["status"],
        "external_id": row["external_id"],
        "external_url": row["external_url"],
        "published_at": row["published_at"].isoformat() if row["published_at"] else None,
        "last_error": row["last_error"],
        "replayed": replayed,
    }


__all__ = ["router"]
