"""The publish route's piece checks (api/routers/v1_publish.py), kept apart so the router stays within its size budget.

AA-1127 (S1, S283 UI audit): ``REQUEST_LIVE_SQL`` is the one rule the Publish list (``_PENDING_SQL``) and ``publish()``
(``_PUBLISH_PIECE_SQL`` → 409 ``PUBLISH_REQUEST_LIVE``) share, so the list never offers what publish refuses.
"""
from __future__ import annotations

from fastapi import HTTPException

from services.acp_publish.dispatch import SUPPORTED_CHANNELS

# AA-1127 (S1, S283 UI audit): another piece of the same request already has a live (or in-flight) post on this piece's
# channel. My Content shows such a request as published (the S251 rule: the newest text is not publishable and is not
# written again), but the Publish page listed its newest piece and one click put a SECOND post online, because the
# list's filters looked only at the piece's own publish_log rows. Read with `cp` (content_piece) and `agr`
# (angle_gate_request) in scope. A post taken down (status unpublished / failed) does not count: the newest piece may
# then publish. Another channel does not count either (a request's channel can be changed before a re-write).
REQUEST_LIVE_SQL = """EXISTS (
          SELECT 1 FROM acp_shared.content_piece op
          JOIN acp_shared.publish_log other ON other.piece_id = op.piece_id AND other.tenant_id = op.tenant_id
          WHERE op.angle_gate_request_id = cp.angle_gate_request_id AND op.tenant_id = cp.tenant_id
            AND op.piece_id <> cp.piece_id
            AND other.channel = COALESCE(cp.channel, agr.channel)
            AND other.status IN ('published', 'requested')
      )"""
PUBLISH_REQUEST_LIVE = ("This content is already live from an earlier version. Take that post down first, or keep the "
                        "live one.")


def publishable_channel(piece) -> str:
    """The checks between reading the piece and its connection: raises the route's 404 / 422 / 409, returns the
    piece's channel."""
    if not piece:
        raise HTTPException(status_code=404, detail="Content piece not found")
    # AA-613 — publish gate under the 3-tier severity model: only 'approved' pieces publish.
    # A warn-only piece (off-brand voice / framework miss) already finalized as 'approved', so it
    # publishes fine. A 'held' piece = unresolved product-truth after ≤2 retries (grounding,
    # banned, missing CTA, FACT_CHECK, cannibalization) — it is fully visible to the tenant in My
    # Content and editable, but must NOT be published until the tenant edits it (which re-opens it
    # for a fresh write) or an admin clears it. 422 (not 404) here because the tenant DOES own this
    # piece and the reason is actionable — no existence leak (ownership already confirmed above).
    # AA-770 (S233) — the tenant always receives the content: a 'held' piece (a gate verdict, an
    # admin-only quality label) is listed, read, edited and exported like an approved one. Only a
    # piece with no content yet ('processing') or a failed write is refused here. AA-1088 (M3): its
    # PUBLISH needs the account owner's explicit "publish anyway" (publish() below, _held_refusal()).
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
    # AA-1127 (S1): one live post per request and channel; the newest piece of a request already online elsewhere is
    # refused (409) even from a stale Publish page, so one click cannot put a second post of the same content online.
    if piece.get("request_live"):
        raise HTTPException(status_code=409, detail=PUBLISH_REQUEST_LIVE)
    channel = piece["channel"]
    if channel not in SUPPORTED_CHANNELS:
        raise HTTPException(status_code=404, detail=f"Publishing to '{channel}' is not yet supported")
    return channel
