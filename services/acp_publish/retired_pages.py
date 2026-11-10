"""AA-1039 follow-up (tech lead decision S276, root ADR 0019 d20 amended): the agency's live posts about a tour Adventure
Asia no longer offers, and its one-click "Move to draft".

By default a retirement changes nothing on the agency's site: the fan-out writes a ``notice_only`` flag per live post
(services/tour_lifecycle/fanout.py), the bell lists the WordPress pages (``tour.pages_to_review``) and the e-mail says
what the agency can do. The Content studio lists the same posts (GET ``/v1/publish-log/retired-pages``) and offers
"Move to draft" on each WordPress page (POST ``/v1/publish-log/{publish_id}/move-to-draft``, api/routers/v1_publish.py),
which runs :func:`services.tour_lifecycle.fanout.move_to_draft` with the tenant as the actor: the same re-checks and the
same WordPress call as the automatic draft of an opted-in agency, the same record (``publish_log`` unpublished by
``system:tour_retirement``, flag resolved ``drafted``), so the restore notice and "Put back online" work on it as on an
automatic draft.

All reads run on the main pool (``content_compliance_flag`` has no tenant-pool grant, CIS 360) with an explicit tenant
condition in every statement, like the "Put back online" reads.
"""
from __future__ import annotations

import uuid
from typing import Optional

from services.tour_lifecycle.fanout import ACTOR as RETIREMENT_ACTOR, FLAG_KIND, WORDPRESS_CHANNELS

# The tenant's ($1) live posts with an open tour_retired flag: one row per post (its latest open flag). Facebook posts
# are listed too (no draft action: the agency edits or removes them on Facebook).
RETIRED_PAGES_SQL = f"""
    SELECT DISTINCT ON (pl.publish_id)
           pl.publish_id::text AS publish_id, pl.piece_id::text AS piece_id, pl.channel, pl.external_url,
           f.flag_id::text AS flag_id, f.action, f.draft_due_at, f.created_at AS flagged_at,
           f.detail ->> 'tour_name' AS tour_name, ago.name AS angle_name
      FROM acp_shared.content_compliance_flag f
      JOIN acp_shared.publish_log pl ON pl.publish_id = f.publish_id AND pl.tenant_id = $1::uuid
      JOIN acp_shared.content_piece cp ON cp.piece_id = pl.piece_id AND cp.tenant_id = pl.tenant_id
      LEFT JOIN acp_shared.angle_gate_option ago
          ON ago.option_id = cp.angle_gate_option_id
          OR (cp.angle_gate_option_id IS NULL AND ago.request_id = cp.angle_gate_request_id AND ago.chosen = true)
     WHERE f.tenant_id = $1::uuid AND f.kind = '{FLAG_KIND}' AND f.state = 'open'
       AND pl.status = 'published' AND pl.unpublished_at IS NULL
     ORDER BY pl.publish_id, f.created_at DESC, f.flag_id, ago.option_id
"""

# The open tour_retired flag of one of the tenant's ($1) posts ($2): the click's target. None = nothing to move.
OPEN_FLAG_OF_PAGE_SQL = f"""
    SELECT f.flag_id::text AS flag_id, pl.channel
      FROM acp_shared.content_compliance_flag f
      JOIN acp_shared.publish_log pl ON pl.publish_id = f.publish_id AND pl.tenant_id = $1::uuid
     WHERE f.tenant_id = $1::uuid AND f.publish_id = $2::uuid AND f.kind = '{FLAG_KIND}' AND f.state = 'open'
     ORDER BY f.created_at DESC, f.flag_id
     LIMIT 1
"""

# A repeat click: the post's latest tour_retired flag is resolved 'drafted' and its row is still the retirement's
# draft. Answered as "already in draft" (replayed), no WordPress call.
DRAFTED_PAGE_SQL = f"""
    SELECT f.flag_id::text AS flag_id, pl.external_url
      FROM acp_shared.content_compliance_flag f
      JOIN acp_shared.publish_log pl ON pl.publish_id = f.publish_id AND pl.tenant_id = $1::uuid
     WHERE f.tenant_id = $1::uuid AND f.publish_id = $2::uuid AND f.kind = '{FLAG_KIND}'
       AND f.resolution = 'drafted' AND pl.status = 'unpublished' AND pl.unpublished_by = $3
     ORDER BY f.drafted_at DESC NULLS LAST, f.created_at DESC
     LIMIT 1
"""


def can_move_to_draft(channel: Optional[str]) -> bool:
    return channel in WORDPRESS_CHANNELS


def actor_of(tenant: dict, tenant_id: str) -> str:
    """Who clicked: their Content studio membership (``mid``), never an e-mail; else the tenant (<= 128 characters,
    the flag's resolved_by CHECK)."""
    mid = tenant.get("mid")
    try:
        return f"membership:{uuid.UUID(str(mid))}" if mid else f"tenant:{tenant_id}"
    except ValueError:
        return f"tenant:{tenant_id}"


def page_row(r) -> dict:
    return {
        "publish_id": r["publish_id"], "piece_id": r["piece_id"], "title": r["angle_name"] or "Untitled",
        "channel": r["channel"], "external_url": r["external_url"], "tour_name": r["tour_name"],
        "can_move_to_draft": can_move_to_draft(r["channel"]),
        "draft_due_at": r["draft_due_at"].isoformat() if r["action"] == "draft_after_notice" and r["draft_due_at"]
        else None,
        "flagged_at": r["flagged_at"].isoformat() if r["flagged_at"] else None,
    }


__all__ = ["DRAFTED_PAGE_SQL", "OPEN_FLAG_OF_PAGE_SQL", "RETIRED_PAGES_SQL", "RETIREMENT_ACTOR", "actor_of",
           "can_move_to_draft", "page_row"]
