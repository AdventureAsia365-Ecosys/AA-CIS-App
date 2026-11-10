"""AA-1039 follow-up (option A, S275): "Put back online" — the tenant's one click that publishes again a WordPress page
the tour retirement moved to draft, once AA serves the tour again.

A restore never republishes on its own (the S274 review of an automatic republish found that a system writing to a
site the tenant also edits cannot tell a deliberate draft from its own). Instead the restore's notice (bell + mail)
lists each drafted page and the Content studio offers "Put back online" on it
(``POST /v1/publish-log/{publish_id}/put-back``, api/routers/v1_publish.py). The click goes through the checks of the
publish route (ownership, the tour served, the latest piece of its request, the AA-subject gate, the connection, the
site) and one WordPress call:

    read the post (``post_status``)
      publish ─────────────▶ "already_online"        (the tenant published it on WordPress; no call)
      draft ───────────────▶ ``set_published`` ──▶ "published"
      anything else ───────▶ "changed_on_wordpress"  (private, pending, scheduled, trashed or gone: left as is)

The page's own ``publish_log`` row is the idempotency record, the same state machine as a publish (AA-726):
``unpublished`` (by ``system:tour_retirement``) -> ``requested`` (the claim; a concurrent click finds it and gets 409)
-> ``published`` (``unpublished_at`` / ``unpublished_by`` cleared, so a later retirement flags the page again) or
back to ``unpublished`` when the page is left as is or WordPress cannot be reached. The partial unique index on the
revision key (migration 215) refuses the claim when the same revision is live or in flight as another post. A click
on a page already put back answers from the row ("already_online", replayed); a page left as is answers from the
note on its flag.

The outcome is noted on the retirement flag (``detail.put_back``) so the page leaves the list, and written to the
tenant's audit log (``content_piece.put_back_online``). Not counted against the daily publish limit (AA-819): it puts
an existing page back, it is not a new post.

All reads and writes of ``content_compliance_flag`` run on the main pool (the table has no tenant-pool grant) with an
explicit tenant condition; ``publish_log`` runs through the route's scoped connection (RLS when the flag is on).
"""
from __future__ import annotations

import json
from typing import Optional

from services.tour_lifecycle.fanout import ACTOR as RETIREMENT_ACTOR, FLAG_KIND, WORDPRESS_CHANNELS

PUBLISHED = "published"
ALREADY_ONLINE = "already_online"
CHANGED_ON_WORDPRESS = "changed_on_wordpress"
OUTCOMES = (PUBLISHED, ALREADY_ONLINE, CHANGED_ON_WORDPRESS)

# A claim this old without an answer died mid-call (the adapter times out after 30 s): a new click takes it over and
# reads the post again before acting (idempotency.STALE_AFTER_SECONDS).
STALE_AFTER_SECONDS = 120

# The tenant's pages the retirement moved to draft (main pool): per publish_id its latest tour_retired flag resolved
# 'drafted' where WordPress did draft it (not 'already_gone'). $1 tenant, $2 WordPress channels; {one} narrows to one
# publish_id ($3). The caller filters on the row state (listed: still unpublished by the retirement, no put_back note).
_DRAFTED_PAGES_SQL = f"""
    WITH latest AS (
        SELECT DISTINCT ON (f.publish_id) f.flag_id, f.publish_id, f.tour_id, f.drafted_at, f.detail
          FROM acp_shared.content_compliance_flag f
         WHERE f.tenant_id = $1::uuid AND f.kind = '{FLAG_KIND}' AND f.resolution = 'drafted'
               {{one}}
         ORDER BY f.publish_id, f.drafted_at DESC NULLS LAST, f.created_at DESC, f.flag_id)
    SELECT DISTINCT ON (pl.publish_id)
           pl.publish_id::text AS publish_id, pl.piece_id::text AS piece_id, pl.channel, pl.external_url,
           pl.status AS publish_status, pl.unpublished_by, latest.flag_id::text AS flag_id,
           latest.tour_id::text AS tour_id, latest.drafted_at, latest.detail -> 'put_back' AS put_back,
           latest.detail ->> 'tour_name' AS tour_name, cp.angle_gate_request_id::text AS request_id,
           ago.name AS angle_name
      FROM latest
      JOIN acp_shared.publish_log pl ON pl.publish_id = latest.publish_id AND pl.tenant_id = $1::uuid
      JOIN acp_shared.content_piece cp ON cp.piece_id = pl.piece_id AND cp.tenant_id = pl.tenant_id
      LEFT JOIN acp_shared.angle_gate_option ago
          ON ago.option_id = cp.angle_gate_option_id
          OR (cp.angle_gate_option_id IS NULL AND ago.request_id = cp.angle_gate_request_id AND ago.chosen = true)
     WHERE pl.channel = ANY($2::text[])
       AND COALESCE(latest.detail ->> 'wordpress', 'drafted') = 'drafted'
     ORDER BY pl.publish_id, ago.option_id
"""
DRAFTED_PAGES_SQL = _DRAFTED_PAGES_SQL.format(one="")
DRAFTED_PAGE_SQL = _DRAFTED_PAGES_SQL.format(one="AND f.publish_id = $3::uuid")

# The page's publish_log row with its piece (scoped connection): the publish route's checks read the same fields.
# `other_live`: another post of the same request (any piece) is live or in flight, i.e. the tenant published it again
# from the Content studio since: this page stays in draft (no second copy of the content online).
ROW_TENANT_COND = "pl.tenant_id = $2::uuid"
ROW_SQL = """
    SELECT pl.publish_id::text AS publish_id, pl.piece_id::text AS piece_id, pl.channel, pl.status,
           pl.external_id, pl.external_url, pl.published_at, pl.unpublished_by, pl.idempotency_key,
           pl.requested_at < now() - make_interval(secs => $3) AS stale,
           cp.content_text, cp.status AS piece_status, cp.angle_gate_request_id::text AS request_id,
           COALESCE(cp.channel, agr.channel) AS piece_channel,
           EXISTS (SELECT 1 FROM acp_shared.content_piece newer
                    WHERE newer.angle_gate_request_id = cp.angle_gate_request_id
                      AND newer.tenant_id = cp.tenant_id AND newer.created_at > cp.created_at) AS superseded,
           EXISTS (SELECT 1 FROM acp_shared.publish_log other
                     JOIN acp_shared.content_piece op ON op.piece_id = other.piece_id
                    WHERE op.angle_gate_request_id = cp.angle_gate_request_id AND other.tenant_id = pl.tenant_id
                      AND other.publish_id <> pl.publish_id AND other.status IN ('published', 'requested')) AS other_live
      FROM acp_shared.publish_log pl
      JOIN acp_shared.content_piece cp ON cp.piece_id = pl.piece_id AND cp.tenant_id = pl.tenant_id
      JOIN acp_shared.angle_gate_request agr ON agr.request_id = cp.angle_gate_request_id
     WHERE pl.publish_id = $1::uuid AND {tenant_cond}
"""

# The claim: the retirement's unpublished row (or a stale claim of an earlier click) becomes 'requested'. $3 = the
# retirement actor, $4 = the stale window. 0 rows = another click owns it or it changed (re-read by the caller).
CLAIM_SQL = """
    UPDATE acp_shared.publish_log
       SET status = 'requested', requested_at = now(), last_error = NULL
     WHERE publish_id = $1::uuid AND tenant_id = $2::uuid AND unpublished_by = $3
       AND (status = 'unpublished'
            OR (status = 'requested' AND requested_at < now() - make_interval(secs => $4)))
    RETURNING publish_id::text AS publish_id
"""

# Back to the retirement's draft state (the page is left as is, or WordPress could not be reached).
REVERT_SQL = """
    UPDATE acp_shared.publish_log SET status = 'unpublished', last_error = $4
     WHERE publish_id = $1::uuid AND tenant_id = $2::uuid AND status = 'requested' AND unpublished_by = $3
"""

# The page is online again: a live post like any other (a later retirement flags it; Unpublish works on it).
# published_at keeps the first publish date.
FINISH_SQL = """
    UPDATE acp_shared.publish_log
       SET status = 'published', unpublished_at = NULL, unpublished_by = NULL, last_error = NULL
     WHERE publish_id = $1::uuid AND tenant_id = $2::uuid AND status = 'requested' AND unpublished_by = $3
    RETURNING publish_id::text AS publish_id, status, external_id, external_url, published_at
"""

# The outcome on the page's retirement flag (main pool): the page leaves the list; a repeat click reads it back.
NOTE_SQL = f"""
    UPDATE acp_shared.content_compliance_flag
       SET detail = detail || jsonb_build_object('put_back', $3::jsonb), updated_at = now()
     WHERE flag_id = $1::uuid AND tenant_id = $2::uuid AND kind = '{FLAG_KIND}'
"""


def listed(row) -> bool:
    """A drafted page the tenant can still put back: its row is the retirement's draft and no click settled it."""
    return (row["publish_status"] == "unpublished" and row["unpublished_by"] == RETIREMENT_ACTOR
            and not row["put_back"])


def note(raw) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw) or {}
        except ValueError:
            return {}
    return dict(raw)


async def remote_put_back(adapter, post_id) -> tuple[str, Optional[str]]:
    """Read the post, then publish it only when it is still a draft. Returns (outcome, WordPress status). Raises when
    WordPress cannot be read, or refuses the publish and a second read does not show the post live (a lost answer to
    a publish that did go through counts as published)."""
    state = await adapter.post_status(post_id)
    if state.status == "publish":
        return ALREADY_ONLINE, state.status
    if state.status != "draft":
        return CHANGED_ON_WORDPRESS, state.status
    try:
        await adapter.set_published(post_id)
    except Exception:
        try:
            again = await adapter.post_status(post_id)
        except Exception:  # noqa: BLE001 — the publish error is the one to report
            again = None
        if again is not None and again.status == "publish":
            return PUBLISHED, "publish"
        raise
    return PUBLISHED, "publish"


__all__ = ["ALREADY_ONLINE", "CHANGED_ON_WORDPRESS", "CLAIM_SQL", "DRAFTED_PAGES_SQL", "DRAFTED_PAGE_SQL",
           "FINISH_SQL", "NOTE_SQL", "OUTCOMES", "PUBLISHED", "RETIREMENT_ACTOR", "REVERT_SQL", "ROW_SQL",
           "ROW_TENANT_COND", "STALE_AFTER_SECONDS", "WORDPRESS_CHANNELS", "listed", "note", "remote_put_back"]
