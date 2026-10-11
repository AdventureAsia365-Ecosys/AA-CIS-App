"""Idempotent T11 publish (AA-726) — the publish_log state machine around one remote call.

    claim()  ── published row for this key ─────────▶ "published"   (no remote call, replay it)
             ── fresh requested row for this key ───▶ "in_progress" (409, another call owns it)
             ── stale requested / failed row ───────▶ "claimed", prior_attempt=True
             ── nothing ────────────────────────────▶ "claimed", prior_attempt=False
    remote   ── prior_attempt: look the post up first (it may exist although we never heard back)
    finish() ── requested → published | failed

The key is one piece revision on one channel: "<piece_id>:<channel>:<sha256(text)[:16]>".
Editing the text gives a new key, so a deliberate re-publish of new text still works.
A partial unique index (migration 215) allows one requested/published row per key; that is what
stops two concurrent clicks, the row lock below only orders them.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional

import asyncpg

# A requested row older than this is treated as an attempt that died mid-call (the adapters time
# out after 30 s). Retrying it looks the post up remotely before posting again.
STALE_AFTER_SECONDS = 120

_ROW_COLS = ("publish_id::text, status, idempotency_key, external_id, external_url, published_at, "
             "last_error")


def publish_key(piece_id, channel: str, content_text: str) -> str:
    digest = hashlib.sha256(content_text.encode("utf-8")).hexdigest()[:16]
    return f"{piece_id}:{channel}:{digest}"


def blog_slug(base: str, key: str) -> str:
    """WordPress slug unique to the piece revision, so a lost post can be found again by slug."""
    stem = re.sub(r"[^a-z0-9]+", "-", (base or "").lower()).strip("-")[:60].strip("-") or "post"
    return f"{stem}-{key.rsplit(':', 1)[-1][:8]}"


@dataclass
class Claim:
    state: str                      # "claimed" | "published" | "in_progress"
    publish_id: Optional[str] = None
    prior_attempt: bool = False
    row: Optional[dict] = None      # the published row, for state == "published"


async def claim(conn, *, piece_id, tenant_id, channel: str, key: str) -> Claim:
    try:
        async with conn.transaction():
            rows = await conn.fetch(
                f"""
                SELECT {_ROW_COLS},
                       requested_at < now() - make_interval(secs => $3) AS stale
                FROM acp_shared.publish_log
                WHERE piece_id = $1::uuid AND tenant_id = $2::uuid
                ORDER BY created_at DESC
                FOR UPDATE
                """,
                piece_id, tenant_id, STALE_AFTER_SECONDS,
            )
            same = [r for r in rows if r["idempotency_key"] == key]
            published = next((r for r in same if r["status"] == "published"), None)
            if published:
                return Claim("published", publish_id=published["publish_id"], row=dict(published))

            requested = next((r for r in same if r["status"] == "requested"), None)
            if requested:
                if not requested["stale"]:
                    return Claim("in_progress", publish_id=requested["publish_id"])
                await conn.execute(
                    "UPDATE acp_shared.publish_log SET requested_at = now() WHERE publish_id = $1::uuid",
                    requested["publish_id"],
                )
                return Claim("claimed", publish_id=requested["publish_id"], prior_attempt=True)

            # Reuse a failed row instead of piling up failures (pre-AA-726 behaviour). A failed
            # row of this revision — or a pre-215 row with no key — may have posted before its
            # response was lost, so it counts as a prior attempt.
            failed = next((r for r in rows if r["status"] == "failed"), None)
            if failed:
                await conn.execute(
                    """
                    UPDATE acp_shared.publish_log
                    SET status = 'requested', idempotency_key = $2, requested_at = now(),
                        channel = $3, last_error = NULL
                    WHERE publish_id = $1::uuid
                    """,
                    failed["publish_id"], key, channel,
                )
                prior = failed["idempotency_key"] in (key, None)
                return Claim("claimed", publish_id=failed["publish_id"], prior_attempt=prior)

            publish_id = await conn.fetchval(
                """
                INSERT INTO acp_shared.publish_log
                    (piece_id, tenant_id, channel, status, idempotency_key, requested_at)
                VALUES ($1::uuid, $2::uuid, $3, 'requested', $4, now())
                RETURNING publish_id::text
                """,
                piece_id, tenant_id, channel, key,
            )
            return Claim("claimed", publish_id=publish_id)
    except asyncpg.UniqueViolationError:
        # A concurrent call inserted/claimed the same key between our read and write.
        return Claim("in_progress")


async def finish(conn, *, publish_id: str, tenant_id, success: bool, external_id: Optional[str],
                 external_url: Optional[str], error: Optional[str]) -> dict:
    row = await conn.fetchrow(
        f"""
        UPDATE acp_shared.publish_log
        SET status = $3, external_id = $4, external_url = $5, last_error = $6,
            published_at = CASE WHEN $3 = 'published' THEN now() ELSE published_at END
        WHERE publish_id = $1::uuid AND tenant_id = $2::uuid
        RETURNING {_ROW_COLS}
        """,
        publish_id, tenant_id, "published" if success else "failed", external_id, external_url, error,
    )
    return dict(row)
