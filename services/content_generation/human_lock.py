"""AA-1146 (S285) — the hand-edit lock of a Master (gold_aa_internal.published_tours.human_lock_*, CIS 457).

A person who publishes a hand-edited version locks the tour's Master: the automatic exits (an S1 run's auto publish,
a wave rerun, the recompose path, the legacy Lambda) then leave it as it is and log why; only a person lifts the lock
(POST /admin/tours/{id}/unlock, master.admin) or publishes another version by hand (Review Queue approve, promote, a
draft's publish). The S1 job skips a locked tour before any paid call (services/jobs/s1_rewrite_job.py) and
``process_export`` refuses an automatic publish over it (services/export/handler.py), so a wave never pays for a
rewrite it may not publish and never overwrites one that slipped through.

Feature-detected: before CIS 457 is applied the columns do not exist (SQLSTATE 42703) and nothing is locked (the
behaviour of today).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

import asyncpg
import structlog

logger = structlog.get_logger()

# Exits that publish without a person deciding this version (final_contract.EXITS names): refused over a lock.
AUTOMATIC_EXITS = frozenset({"auto_publish", "recompose", "lambda"})
# Exits where a person decided: they set the lock (hand-edited version) or lift it (a writer's version).
HUMAN_EXITS = frozenset({"approve", "promote", "human_publish"})
SKIP_REASON = "human_locked"

_LOCK_SQL = """
    SELECT tour_id::text AS tour_id, human_lock_at, human_lock_by, human_lock_reason
      FROM gold_aa_internal.published_tours
     WHERE tour_id = ANY($1::uuid[]) AND human_lock_at IS NOT NULL
"""


class HumanLockedError(Exception):
    """An automatic exit met a Master a person locked."""

    def __init__(self, tour_id: str, lock: Optional[dict] = None) -> None:
        super().__init__(tour_id, lock)
        self.tour_id, self.lock = str(tour_id), dict(lock or {})

    def __str__(self) -> str:
        return f"tour {self.tour_id} is locked by a hand edit; unlock it before an automatic rewrite"


def _out(row: Any) -> dict:
    at = row["human_lock_at"]
    return {"locked": True, "at": at.isoformat() if at else None, "by": row["human_lock_by"],
            "reason": row["human_lock_reason"]}


async def locks_of(conn: Any, tour_ids: Iterable[str]) -> dict[str, dict]:
    """{tour_id: lock} of the locked tours among ``tour_ids`` ({} before CIS 457)."""
    ids = [str(t) for t in tour_ids if t]
    if not ids:
        return {}
    try:
        rows = await conn.fetch(_LOCK_SQL, ids)
    except (asyncpg.exceptions.UndefinedColumnError, asyncpg.exceptions.UndefinedTableError):
        return {}
    return {r["tour_id"]: _out(r) for r in rows}


async def lock_of(conn: Any, tour_id: str) -> Optional[dict]:
    """The lock of one tour, or None (not locked, no Master, or CIS 457 not applied)."""
    return (await locks_of(conn, [tour_id])).get(str(tour_id))


async def set_lock(conn: Any, tour_id: str, *, by: str, reason: Optional[str]) -> bool:
    """Lock the Master of ``tour_id`` (a person published a hand edit). False before CIS 457 or without a Master."""
    try:
        status = await conn.execute(
            """UPDATE gold_aa_internal.published_tours
                  SET human_lock_at = now(), human_lock_by = $2, human_lock_reason = $3
                WHERE tour_id = $1::uuid""", str(tour_id), (by or "admin")[:64], (reason or None) and reason[:500])
    except asyncpg.exceptions.UndefinedColumnError:
        return False
    return str(status).endswith(" 1")


async def clear_lock(conn: Any, tour_id: str) -> Optional[dict]:
    """Lift the lock; returns the lock that was in force (None when there was none or before CIS 457)."""
    try:
        row = await conn.fetchrow(
            """UPDATE gold_aa_internal.published_tours p
                  SET human_lock_at = NULL, human_lock_by = NULL, human_lock_reason = NULL
                 FROM (SELECT tour_id, human_lock_at, human_lock_by, human_lock_reason
                         FROM gold_aa_internal.published_tours WHERE tour_id = $1::uuid FOR UPDATE) old
                WHERE p.tour_id = old.tour_id AND old.human_lock_at IS NOT NULL
            RETURNING old.tour_id::text AS tour_id, old.human_lock_at, old.human_lock_by, old.human_lock_reason""",
            str(tour_id))
    except asyncpg.exceptions.UndefinedColumnError:
        return None
    return _out(row) if row else None


def is_human_version(row: Any) -> bool:
    """Is a generated_content row a person's text (a draft version, or an edited one)?"""
    get = row.get if hasattr(row, "get") else (lambda k, d=None: row[k] if k in row else d)
    return get("origin") == "human" or bool(get("human_edited"))


__all__ = ["AUTOMATIC_EXITS", "HUMAN_EXITS", "HumanLockedError", "SKIP_REASON", "clear_lock", "is_human_version",
           "lock_of", "locks_of", "set_lock"]
