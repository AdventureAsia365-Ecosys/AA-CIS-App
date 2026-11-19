# api/routers/admin_pipeline/fact_drafts.py — AA-1146 T1 (S285): the draft an Excel re-import merge becomes, and its
# publish through the gate.
"""Routes (prefix /admin, POLICY in api/core/staff_authz.py):

  GET    /tours/{tour_id}/fact-draft          the open draft of the tour's facts (if any): per field the value it was
                                              merged from, the value publishing writes and the tour's value now
                                              (content.read)
  POST   /tours/{tour_id}/fact-draft/publish  publish it: the two-person rule (cis.content.publish_two_person),
                                              the price rule (D5: price_raw only with reference_price.write), a live
                                              source and a stale check (409 ``fact_draft_stale`` when the tour changed
                                              since the merge); then the facts are written, with one history row
                                              (excel_merge) and one audit row, and the source fan-out marks what was
                                              written from the old facts (content.publish | review.act)
  DELETE /tours/{tour_id}/fact-draft          discard it (kept as history) (content.write)

The draft is written by POST /admin/upload-staging/{id}/decide with decision ``merge`` (uploads.py,
:func:`create_fact_draft`): the tour is not touched until a person publishes. One open draft per tour (CIS 457 partial
unique index); before CIS 457 every route answers 503 ``versions_not_ready``.
"""
import json
from typing import Optional

import asyncpg
from fastapi import Header, HTTPException, Request

from api.routers.admin import verify_admin_secret
from api.routers.admin_pipeline._publish import enforce_publish_gate, may_edit_price, price_read_only, request_actor
from api.routers.admin_pipeline._shared import logger, router
from api.services import content_publish_gate as gate
from services.acp_shared.audit_log import write_audit_log
from services.ingestion import excel_merge as em
from shared.tour_liveness import SOURCE_NOT_LIVE, source_not_live_message, source_state

FACT_DRAFT_NOT_READY = {"code": "versions_not_ready",
                        "message": "Sheet drafts are not available yet: the database update (CIS 457) is not applied"}
FACT_DRAFT_RESOURCE = "raw_tour"
_FD_OPEN_SQL = """
    SELECT id, tour_id::text AS tour_id, staging_id::text AS staging_id, fields, before::text AS before,
           after::text AS after, resolutions::text AS resolutions, author, reason, created_at
      FROM silver_aa_internal.raw_tour_fact_draft
     WHERE tour_id = $1::uuid AND status = 'draft'
"""
_FD_INSERT_SQL = """
    INSERT INTO silver_aa_internal.raw_tour_fact_draft (tour_id, staging_id, fields, before, after, resolutions,
                                                        author, reason)
    VALUES ($1::uuid, $2::uuid, $3::text[], $4::jsonb, $5::jsonb, $6::jsonb, $7, $8)
    RETURNING id
"""
_FD_DECIDE_SQL = """
    UPDATE silver_aa_internal.raw_tour_fact_draft
       SET status = $2, decided_by = $3, decided_at = now(), revision_num = $4
     WHERE id = $1 AND status = 'draft'
"""


def _fd_json(text) -> dict:
    return json.loads(text) if isinstance(text, str) else dict(text or {})


def _fd_value(value):
    """A raw tour value as the API shows it (asyncpg hands jsonb back as JSON text)."""
    if isinstance(value, str) and value[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


async def open_fact_draft(conn, tour_id: str, *, lock: bool = False):
    """The open draft row of the tour, or None; 503 before CIS 457."""
    try:
        return await conn.fetchrow(_FD_OPEN_SQL + (" FOR UPDATE" if lock else ""), tour_id)
    except asyncpg.exceptions.UndefinedTableError:
        raise HTTPException(status_code=503, detail=FACT_DRAFT_NOT_READY)


async def create_fact_draft(conn, *, tour_id: str, staging_id: str, before: dict, after: dict, resolutions: dict,
                            author: str, reason: str) -> int:
    """Hold a merge's changes as the tour's open draft (in the caller's transaction). 409 ``fact_draft_open`` when the
    tour already has one: publish or discard it first."""
    if await open_fact_draft(conn, tour_id) is not None:
        raise _open_draft_conflict()
    try:
        async with conn.transaction():
            return await conn.fetchval(_FD_INSERT_SQL, tour_id, staging_id, sorted(after),
                                       json.dumps(before, default=str), json.dumps(after, default=str),
                                       json.dumps(resolutions), author[:64], reason)
    except asyncpg.exceptions.UniqueViolationError:
        raise _open_draft_conflict()


def _open_draft_conflict() -> HTTPException:
    return HTTPException(status_code=409, headers={"X-Error-Code": "fact_draft_open"},
                         detail={"code": "fact_draft_open",
                                 "message": "This tour already has a sheet draft waiting: publish or discard it in the "
                                            "tour drawer first"})


def _fd_out(d, current: dict) -> dict:
    before, after = _fd_json(d["before"]), _fd_json(d["after"])
    stale = em.stale_fields(current, before)
    return {"id": d["id"], "staging_id": d["staging_id"], "author": d["author"], "reason": d["reason"],
            "created_at": d["created_at"].isoformat() if d["created_at"] else None,
            "resolutions": _fd_json(d["resolutions"]), "stale": stale,
            "fields": [{"field": f, "before": before.get(f), "after": after.get(f), "current": current.get(f),
                        "stale": f in stale} for f in em.MERGE_FIELDS if f in after]}


async def _fd_current(conn, tour_id: str, fields, *, lock: bool = False) -> Optional[dict]:
    cols = ", ".join(f for f in em.MERGE_FIELDS if f in set(fields)) or "tour_id"
    row = await conn.fetchrow(f"SELECT {cols} FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid"
                              + (" FOR UPDATE" if lock else ""), tour_id)
    return None if row is None else {k: _fd_value(v) for k, v in dict(row).items()}


@router.get("/tours/{tour_id}/fact-draft")
async def get_fact_draft(tour_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    async with request.app.state.pool.acquire() as conn:
        d = await open_fact_draft(conn, tour_id)
        current = await _fd_current(conn, tour_id, list(d["fields"]) if d else [])
        if current is None:
            raise HTTPException(status_code=404, detail=f"Tour {tour_id} not found")
        return {"tour_id": tour_id, "draft": _fd_out(d, current) if d else None,
                "two_person_publish": await gate.switch_on(conn), "price_editable": may_edit_price(request)}


async def _fd_publish_checks(conn, request: Request, tour_id: str, d) -> dict:
    """The gate of a fact draft's publish, in the publish transaction; returns the tour's current values."""
    fields = list(d["fields"])
    await enforce_publish_gate(conn, request, editors=[d["author"]])
    if set(fields) & em.PRICE_FIELDS and not may_edit_price(request):
        raise price_read_only()
    src = await source_state(conn, tour_id)
    if not src.live:
        raise HTTPException(status_code=409, detail=source_not_live_message(src, "publish this sheet draft"),
                            headers={"X-Error-Code": SOURCE_NOT_LIVE})
    current = await _fd_current(conn, tour_id, fields, lock=True)
    stale = em.stale_fields(current or {}, _fd_json(d["before"]))
    if stale:
        raise HTTPException(status_code=409, headers={"X-Error-Code": "fact_draft_stale"}, detail={
            "code": "fact_draft_stale", "fields": stale,
            "message": "The tour changed since this sheet was merged: discard the draft and merge the sheet again"})
    return current or {}


@router.post("/tours/{tour_id}/fact-draft/publish")
async def publish_fact_draft(tour_id: str, request: Request, x_admin_secret: str = Header(None)):
    """Write the open sheet draft into the tour through the gate (see the module doc)."""
    from api.routers.admin_pipeline.tour_detail import _source_fanout_raw, record_raw_revision
    verify_admin_secret(x_admin_secret)
    actor = request_actor(request)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            d = await open_fact_draft(conn, tour_id, lock=True)
            if d is None:
                raise HTTPException(status_code=404, detail="This tour has no sheet draft")
            current = await _fd_publish_checks(conn, request, tour_id, d)
            after, before = _fd_json(d["after"]), _fd_json(d["before"])
            fields = [f for f in em.MERGE_FIELDS if f in after]
            cols = ", ".join(f"{f} = ${i + 1}::jsonb" if f in em.JSONB_FIELDS else f"{f} = ${i + 1}"
                             for i, f in enumerate(fields))
            changed_at = await conn.fetchval(
                f"UPDATE silver_aa_internal.raw_tours SET {cols} WHERE tour_id = ${len(fields) + 1}::uuid "
                "RETURNING now() AS changed_at", *[em.db_value(f, after[f]) for f in fields], tour_id)
            # the history row names the person who chose the values (the merge's author); the audit row the publisher
            revision = await record_raw_revision(conn, tour_id, source="excel_merge", before=before, after=after,
                                                 actor=d["author"], reason=d["reason"],
                                                 resolutions=_fd_json(d["resolutions"]))
            await conn.execute(_FD_DECIDE_SQL, d["id"], "published", actor[:64], revision)
            await write_audit_log(conn, tenant_id=None, actor=actor, action="raw_tour.sheet_draft_published",
                                  resource_type=FACT_DRAFT_RESOURCE, resource_id=tour_id, details={
                                      "fact_draft_id": d["id"], "fields": fields, "author": d["author"],
                                      "reason": d["reason"], "before": {f: current.get(f) for f in fields},
                                      "after": after, "revision_num": revision})
    await _source_fanout_raw(pool, tour_id, changed_at, fields)
    logger.info("fact_draft_published", tour_id=tour_id, fact_draft_id=d["id"], fields=fields)
    return {"published": True, "tour_id": tour_id, "fact_draft_id": d["id"], "updated": fields,
            "revision_num": revision}


@router.delete("/tours/{tour_id}/fact-draft")
async def discard_fact_draft(tour_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    actor = request_actor(request)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            d = await open_fact_draft(conn, tour_id, lock=True)
            if d is None:
                raise HTTPException(status_code=404, detail="This tour has no sheet draft")
            await conn.execute(_FD_DECIDE_SQL, d["id"], "discarded", actor[:64], None)
            await write_audit_log(conn, tenant_id=None, actor=actor, action="raw_tour.sheet_draft_discarded",
                                  resource_type=FACT_DRAFT_RESOURCE, resource_id=tour_id,
                                  details={"fact_draft_id": d["id"], "fields": list(d["fields"])})
    return {"tour_id": tour_id, "discarded": d["id"]}
