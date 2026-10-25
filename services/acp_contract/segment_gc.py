"""AA-988 — garbage collection of orphan Segments (recompute stage `segment_gc`) and the lock it shares with the
segment stage.

Why: `atom_segment` is UPSERT-only (migration 129) and a re-atomize soft-deletes the replaced atoms, so every rewrite
that changes a place / action leaves Segments whose only members are dead atoms (8,578 after the S1 v2 rewrite, AA-991
probe). The segment stage now drops the member rows of its own tour's dead atoms (`run_segment_matching`); this stage
removes the Segments nothing refers to any more, platform-wide, at the end of the debounced platform `recompute`
(after Score and Route are current).

Deleted (decision S252 §7.2: zero-reference orphans only) = an `atom_segment` row with
  - no member row on a live atom (`tour_atoms` not deleted and not an empty-day marker),
  - no `atom_ranking` row (the score stage deletes superseded rows, so this is the current set),
  - no `atom_segment_alias` row naming it, old or canonical (both are FKs to it),
  - no `acp_shared.subject` row (FK) and no `acp_shared.debate_brand_fit_cache` row,
  - not in `ordered_segment_ids` of a current route, nor of any route a subject names (superseded routes are kept for
    `subject.route_id`), nor in any `acp_shared.angle_gate_request.route_segment_ids`.
Its member rows on dead atoms go first, in the same transaction. An orphan by the probe's wider rule (no active member,
no current ranking, no current route) that some reference keeps is reported per reference class, never deleted.
A deleted Segment that comes back is re-minted by the segment stage (ids are deterministic per place / verb).

Mode: env SEGMENT_GC_MODE = `report` (default: counts and samples in the job result, nothing written) | `apply` |
`off`. `apply` refuses (status `held_volume`, nothing written) when the delete set is larger than SEGMENT_GC_MAX
(default 10,000): a predicate bug must not empty the table.

Lock: key `acp:segment_write`. Every segment run holds it SHARED from its candidate read to the end of its write
(`holds_segment_lock_shared`), the GC takes it EXCLUSIVE for its transaction. A segment run reads existing Segments
as pseudo atoms before it writes; deleting one of them in between would break its alias / member re-point writes
(FKs). Segment runs do not block each other (shared); the GC waits for runs in flight, and gives up after
LOCK_TIMEOUT with status `skipped` (the next platform pass collects).
"""
from __future__ import annotations

import functools
import os
from contextlib import asynccontextmanager
from typing import Optional

import structlog

logger = structlog.get_logger()

SEGMENT_WRITE_LOCK_KEY = "acp:segment_write"
MODE_ENV = "SEGMENT_GC_MODE"
MAX_ENV = "SEGMENT_GC_MAX"
MODES = ("report", "apply", "off")
DEFAULT_MODE = "report"
DEFAULT_MAX = 10_000
LOCK_TIMEOUT = "30s"
SAMPLE = 5

# Reference classes reported for a kept orphan (column names of ORPHANS_SQL).
REFERENCE_CLASSES = ("live_member", "ranking", "alias", "subject", "brand_fit", "subject_route", "angle_request")

# Every Segment that is an orphan by the AA-991 probe rule (no active member, no current ranking, not in a current
# route), with one boolean per reference class that keeps it. Sets first, joined once: no correlated subplan per
# Segment (≈19k Segments, ≈11k routes).
ORPHANS_SQL = """
WITH active_m AS (
    SELECT DISTINCT m.segment_id FROM acp_contract.atom_segment_member m
      JOIN acp_contract.v_active_tour_atoms a ON a.atom_id = m.atom_id),
live_m AS (
    SELECT DISTINCT m.segment_id FROM acp_contract.atom_segment_member m
      JOIN acp_contract.tour_atoms t ON t.atom_id = m.atom_id
     WHERE NOT t.deleted AND NOT t.is_empty_marker),
cur_rank AS (SELECT DISTINCT segment_id FROM acp_contract.atom_ranking WHERE superseded_at IS NULL),
any_rank AS (SELECT DISTINCT segment_id FROM acp_contract.atom_ranking),
cur_route AS (
    SELECT DISTINCT e.id AS segment_id FROM acp_contract.route r
     CROSS JOIN LATERAL jsonb_array_elements_text(r.ordered_segment_ids) e(id)
     WHERE r.superseded_at IS NULL AND jsonb_typeof(r.ordered_segment_ids) = 'array'),
subj_route AS (
    SELECT DISTINCT e.id AS segment_id FROM acp_contract.route r
      JOIN acp_shared.subject sj ON sj.route_id = r.route_id
     CROSS JOIN LATERAL jsonb_array_elements_text(r.ordered_segment_ids) e(id)
     WHERE jsonb_typeof(r.ordered_segment_ids) = 'array'),
alias AS (
    SELECT segment_id_old AS segment_id FROM acp_contract.atom_segment_alias
    UNION SELECT segment_id_canonical FROM acp_contract.atom_segment_alias),
subj AS (SELECT DISTINCT segment_id FROM acp_shared.subject WHERE segment_id IS NOT NULL),
bfit AS (SELECT DISTINCT segment_id FROM acp_shared.debate_brand_fit_cache WHERE segment_id IS NOT NULL),
agr AS (
    SELECT DISTINCT e.id AS segment_id FROM acp_shared.angle_gate_request g
     CROSS JOIN LATERAL jsonb_array_elements_text(g.route_segment_ids) e(id)
     WHERE jsonb_typeof(g.route_segment_ids) = 'array')
SELECT s.segment_id,
       lm.segment_id IS NOT NULL AS live_member,
       ar.segment_id IS NOT NULL AS ranking,
       al.segment_id IS NOT NULL AS alias,
       sj.segment_id IS NOT NULL AS subject,
       bf.segment_id IS NOT NULL AS brand_fit,
       sr.segment_id IS NOT NULL AS subject_route,
       ag.segment_id IS NOT NULL AS angle_request
  FROM acp_contract.atom_segment s
  LEFT JOIN active_m am ON am.segment_id = s.segment_id
  LEFT JOIN cur_rank cr ON cr.segment_id = s.segment_id
  LEFT JOIN cur_route ct ON ct.segment_id = s.segment_id
  LEFT JOIN live_m lm ON lm.segment_id = s.segment_id
  LEFT JOIN any_rank ar ON ar.segment_id = s.segment_id
  LEFT JOIN alias al ON al.segment_id = s.segment_id
  LEFT JOIN subj sj ON sj.segment_id = s.segment_id
  LEFT JOIN bfit bf ON bf.segment_id = s.segment_id
  LEFT JOIN subj_route sr ON sr.segment_id = s.segment_id
  LEFT JOIN agr ag ON ag.segment_id = s.segment_id
 WHERE am.segment_id IS NULL AND cr.segment_id IS NULL AND ct.segment_id IS NULL
 ORDER BY s.segment_id
"""

# Member rows of dead atoms (deleted or empty-day marker) of the Segments about to be deleted.
DEAD_MEMBERS_COUNT_SQL = """
    SELECT count(*)::int FROM acp_contract.atom_segment_member m
      JOIN acp_contract.tour_atoms t ON t.atom_id = m.atom_id
     WHERE m.segment_id = ANY($1::text[]) AND (t.deleted OR t.is_empty_marker)
"""
DEAD_MEMBERS_DELETE_SQL = """
    DELETE FROM acp_contract.atom_segment_member m
     USING acp_contract.tour_atoms t
     WHERE t.atom_id = m.atom_id AND m.segment_id = ANY($1::text[]) AND (t.deleted OR t.is_empty_marker)
"""
# Re-checked at delete time: a Segment that has any member row left is never deleted (the FK would refuse anyway).
SEGMENTS_DELETE_SQL = """
    DELETE FROM acp_contract.atom_segment s
     WHERE s.segment_id = ANY($1::text[])
       AND NOT EXISTS (SELECT 1 FROM acp_contract.atom_segment_member m WHERE m.segment_id = s.segment_id)
"""


# ── lock ────────────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def segment_writes_shared(pool):
    """Hold `acp:segment_write` SHARED (session level) on one pool connection for the body. Released explicitly;
    asyncpg's connection reset (pg_advisory_unlock_all) also releases it when the connection goes back to the pool."""
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock_shared(hashtext($1))", SEGMENT_WRITE_LOCK_KEY)
        try:
            yield
        finally:
            try:
                await conn.execute("SELECT pg_advisory_unlock_shared(hashtext($1))", SEGMENT_WRITE_LOCK_KEY)
            except Exception as e:  # noqa: BLE001 — a broken connection drops its locks with it
                logger.warning("segment_lock_release_failed", error=str(e)[:200])


def holds_segment_lock_shared(fn):
    """Decorator for a segment run `fn(tour_id, pool, ...)`: the whole run holds the shared segment-write lock."""
    @functools.wraps(fn)
    async def wrapper(tour_id, pool, *args, **kwargs):
        async with segment_writes_shared(pool):
            return await fn(tour_id, pool, *args, **kwargs)
    return wrapper


async def lock_segment_writes_exclusive(conn) -> None:
    """Inside a transaction: wait (up to LOCK_TIMEOUT) until no segment run is in flight, then keep them out until
    the transaction ends."""
    await conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", SEGMENT_WRITE_LOCK_KEY)


# ── plan ────────────────────────────────────────────────────────────────────────────────────

def gc_mode(value: Optional[str] = None) -> str:
    v = (os.environ.get(MODE_ENV, "") if value is None else value).strip().lower()
    return v if v in MODES else DEFAULT_MODE


def gc_max() -> int:
    try:
        return max(0, int(os.environ.get(MAX_ENV, "") or DEFAULT_MAX))
    except ValueError:
        return DEFAULT_MAX


def classify(rows: list[dict]) -> dict:
    """Pure: ORPHANS_SQL rows -> {"orphans", "delete_ids", "kept", "kept_ids"}. `kept` counts each reference class
    (a Segment kept for two reasons counts in both)."""
    delete_ids, kept_ids = [], []
    kept = {c: 0 for c in REFERENCE_CLASSES}
    for r in rows:
        refs = [c for c in REFERENCE_CLASSES if r.get(c)]
        if refs:
            kept_ids.append(r["segment_id"])
            for c in refs:
                kept[c] += 1
        else:
            delete_ids.append(r["segment_id"])
    return {"orphans": len(rows), "delete_ids": delete_ids, "kept": kept, "kept_ids": kept_ids}


def _count(status) -> int:
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


async def run_segment_gc(pool, mode: Optional[str] = None, max_delete: Optional[int] = None,
                         scope_ids: Optional[list[str]] = None) -> dict:
    """One GC pass. Report mode reads under the same lock (a consistent count), writes nothing. `scope_ids` limits
    the pass to those Segment ids (a trial batch of the backfill, tests); None = every Segment (the stage)."""
    mode = gc_mode(mode)
    if mode == "off":
        return {"mode": mode, "status": "off"}
    cap = gc_max() if max_delete is None else max_delete
    out: dict = {"mode": mode}
    import asyncpg

    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await lock_segment_writes_exclusive(conn)
                rows = [dict(r) for r in await conn.fetch(ORPHANS_SQL)]
                if scope_ids is not None:
                    wanted = set(scope_ids)
                    rows = [r for r in rows if r["segment_id"] in wanted]
                    out["scope_ids"] = len(wanted)
                plan = classify(rows)
                ids = plan["delete_ids"]
                dead_members = int(await conn.fetchval(DEAD_MEMBERS_COUNT_SQL, ids) or 0) if ids else 0
                out.update(orphans=plan["orphans"], deletable=len(ids), dead_members=dead_members,
                           kept=len(plan["kept_ids"]), kept_by=plan["kept"],
                           sample_deletable=ids[:SAMPLE], sample_kept=plan["kept_ids"][:SAMPLE])
                if mode == "report":
                    out["status"] = "report"
                elif len(ids) > cap:
                    out.update(status="held_volume", max_delete=cap)
                    logger.warning("segment_gc_held_volume", deletable=len(ids), max_delete=cap)
                elif ids:
                    out["deleted_members"] = _count(await conn.execute(DEAD_MEMBERS_DELETE_SQL, ids))
                    out["deleted_segments"] = _count(await conn.execute(SEGMENTS_DELETE_SQL, ids))
                    out["kept_on_recheck"] = len(ids) - out["deleted_segments"]
                    out["status"] = "applied"
                else:
                    out.update(status="applied", deleted_members=0, deleted_segments=0, kept_on_recheck=0)
        except asyncpg.exceptions.LockNotAvailableError:
            out.update(status="skipped", reason="lock_busy")
            logger.warning("segment_gc_lock_busy", timeout=LOCK_TIMEOUT)
    logger.info("segment_gc_done", **{k: v for k, v in out.items() if not k.startswith("sample")})
    return out
