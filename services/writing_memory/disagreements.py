"""AA-922 WM-31 — the weekly judge / human disagreement queue (app ADR 0010, D15: <= 30 minutes a week; CIS 314).

Each weekly KPI run (kpi.run_weekly, apply) adds up to QUEUE_MAX new items of its week, newest first, from:
  golden       an R2 / reverify run of the week (writing_memory, finished) scored a labelled control item
               (arm control) pass where the human label is fail, or the reverse (eval_item_result.verdict vs
               eval_golden_item.label_verdict): the judge disagrees with the frozen label;
  piece_label  a T9 piece labelled in the week (content_piece_label) that the gates passed (approved, no failed
               blocking gate) but a human labelled heavy / reject, or that the gates held but a human labelled as_is /
               light: the gates disagree with the human.
An item is never added twice (unique source + ref). The reviewer resolves it in one click: judge_right, human_right
or unclear (+ an optional note). Ids and verdicts only; the texts stay in their own tables. Resolutions are the
labelled evidence the judge calibration and the gate thresholds are tuned on later; this queue changes no rule,
release or gate by itself.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

QUEUE_MAX = 10
QUEUE_MIN = 5
RESOLUTIONS = ("judge_right", "human_right", "unclear")

_GOLDEN_SQL = """
    SELECT r.result_id AS ref, r.writer, r.scope, r.tenant_id::text AS tenant_id, r.verdict AS machine,
           i.label_verdict AS human, r.created_at
      FROM shared.eval_item_result r
      JOIN shared.eval_golden_item i ON i.item_id = r.item_id
      JOIN shared.prompt_eval_runs p ON p.id = r.run_id
     WHERE p.pipeline = 'writing_memory' AND p.run_kind IN ('r2', 'reverify') AND p.finished_at IS NOT NULL
       AND p.finished_at >= $1 AND p.finished_at < $2 AND r.arm = 'control' AND r.verdict IN ('pass', 'fail')
       AND r.verdict <> i.label_verdict
     ORDER BY r.created_at DESC
     LIMIT $3
"""
_PIECE_SQL = """
    SELECT cp.piece_id AS ref,
           CASE WHEN cp.writer_input->>'channel' = 'blog' THEN 't9_blog' ELSE 't9_social' END AS writer,
           'tenant' AS scope, cp.tenant_id::text AS tenant_id,
           CASE WHEN cp.status = 'held' THEN 'held' ELSE 'passed' END AS machine, l.label AS human,
           l.labelled_at AS created_at
      FROM acp_shared.content_piece_label l
      JOIN acp_shared.content_piece cp ON cp.piece_id = l.piece_id
     WHERE l.labelled_at >= $1 AND l.labelled_at < $2
       AND ((cp.status = 'held' AND l.label IN ('as_is', 'light'))
            OR (cp.status = 'approved' AND l.label IN ('heavy', 'reject')
                AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(cp.gate_ledger) g
                                 WHERE (g->>'passed')::boolean = false AND coalesce((g->>'blocking')::boolean, true))))
     ORDER BY l.labelled_at DESC
     LIMIT $3
"""
_INSERT_SQL = """
    INSERT INTO shared.writing_memory_disagreement (source, source_ref, writer, scope, tenant_id, machine_verdict,
                                                    human_label, week_start)
    VALUES ($1, $2::uuid, $3, $4, $5::uuid, $6, $7, $8)
    ON CONFLICT (source, source_ref) DO NOTHING
    RETURNING item_id::text AS item_id
"""
_COLUMNS = ("item_id::text AS item_id, source, source_ref::text AS source_ref, writer, scope, tenant_id::text AS "
            "tenant_id, machine_verdict, human_label, week_start, status, resolution, note, resolved_by, resolved_at, "
            "created_at")


class DisagreementError(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(status, code, detail)
        self.status, self.code, self.detail = status, code, detail


async def _rows(conn, sql: str, *args) -> list[dict]:
    try:
        async with conn.transaction():
            return [dict(r) for r in await conn.fetch(sql, *args)]
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
        return []


async def candidates(conn, week: date) -> list[tuple[str, dict]]:
    """(source, row) of the week's disagreements, newest first, golden before piece labels at equal time."""
    start = datetime(week.year, week.month, week.day, tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    out = [("golden", r) for r in await _rows(conn, _GOLDEN_SQL, start, end, QUEUE_MAX * 3)]
    out += [("piece_label", r) for r in await _rows(conn, _PIECE_SQL, start, end, QUEUE_MAX * 3)]
    return sorted(out, key=lambda x: (x[1]["created_at"], x[0] == "golden"), reverse=True)


async def fill_queue(conn, week: date, *, apply: bool) -> dict:
    """Add up to QUEUE_MAX new items of `week` (DRY RUN: count them). {found, added, below_min}."""
    found = await candidates(conn, week)
    added = 0
    for source, r in found:
        if added >= QUEUE_MAX:
            break
        scope = "platform" if r["tenant_id"] is None else "tenant"
        if not apply:
            exists = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM shared.writing_memory_disagreement "
                                         "WHERE source = $1 AND source_ref = $2::uuid)", source, str(r["ref"]))
            added += 0 if exists else 1
            continue
        if await conn.fetchval(_INSERT_SQL, source, str(r["ref"]), r["writer"], scope, r["tenant_id"],
                               str(r["machine"]), str(r["human"]), week):
            added += 1
    return {"found": len(found), "added": added, "below_min": added < QUEUE_MIN, "dry_run": not apply}


async def list_items(conn, *, status: Optional[str] = "open", limit: int = 50) -> list[dict]:
    where, args = "true", []
    if status:
        where, args = "status = $1", [status]
    rows = await conn.fetch(f"SELECT {_COLUMNS} FROM shared.writing_memory_disagreement WHERE {where} "
                            f"ORDER BY week_start DESC, created_at DESC LIMIT {int(limit)}", *args)
    return [serialise(dict(r)) for r in rows]


async def resolve(conn, item_id: str, *, resolution: str, actor: str, note: Optional[str] = None) -> dict:
    """The one-click resolution of an open item (audited, ids only)."""
    from services.acp_shared.audit_log import write_audit_log
    from services.writing_memory.releases import PLATFORM_TENANT_ID
    if resolution not in RESOLUTIONS:
        raise DisagreementError(422, "invalid", f"resolution must be one of {list(RESOLUTIONS)}")
    note = " ".join((note or "").split())[:500] or None
    row = await conn.fetchrow(
        f"""UPDATE shared.writing_memory_disagreement
               SET status = 'resolved', resolution = $2, note = $3, resolved_by = $4, resolved_at = now()
             WHERE item_id = $1::uuid AND status = 'open' RETURNING {_COLUMNS}""",
        str(item_id), resolution, note, actor[:128])
    if row is None:
        exists = await conn.fetchval("SELECT status FROM shared.writing_memory_disagreement WHERE item_id = $1::uuid",
                                     str(item_id))
        raise DisagreementError(404 if exists is None else 409, "not_found" if exists is None else "resolved",
                                "Unknown item" if exists is None else "the item is already resolved")
    out = serialise(dict(row))
    await write_audit_log(conn, tenant_id=out.get("tenant_id") or PLATFORM_TENANT_ID, actor=actor[:128],
                          action="writing_memory.disagreement_resolved", resource_type="writing_memory_disagreement",
                          resource_id=out["item_id"],
                          details={"source": out["source"], "writer": out["writer"], "resolution": resolution})
    return out


def serialise(row: dict) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


__all__ = ["QUEUE_MAX", "QUEUE_MIN", "RESOLUTIONS", "DisagreementError", "candidates", "fill_queue", "list_items",
           "resolve", "serialise"]
