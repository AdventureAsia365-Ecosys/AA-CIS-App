# api/routers/admin_pipeline/drafts.py — AA-1146 (S285): a person's draft version of a tour's wording, its diff
# against the live version, its publish through the one gate, the Master's hand-edit lock and the source tour's history.
"""Routes (prefix /admin, POLICY in api/core/staff_authz.py):

  GET    /tours/{tour_id}/draft          the open draft (if any), the live version, the per-field diff, the lock and
                                         whether two-person publishing is on (content.read)
  PUT    /tours/{tour_id}/draft          create the draft (a copy of the live version, or of base_version_num) or save
                                         fields into the open one; a reason is required (content.write)
  DELETE /tours/{tour_id}/draft          discard the open draft (status rejected, kept for history) (content.write)
  POST   /tours/{tour_id}/draft/publish  publish it: the re-check gate (a passing re-validation), the two-person rule,
                                         then process_export (exit human_publish), which locks the Master
                                         (content.publish | review.act)
  POST   /tours/{tour_id}/unlock         lift the hand-edit lock so automatic reruns may replace the Master again
                                         (master.admin)
  GET    /tours/{tour_id}/raw-revisions  the source tour's edit and Excel-merge history (content.read)

One open draft per tour (status 'draft', origin 'human'); every save is audited (acp_shared.audit_log, before / after of
the fields) and recorded as a writing signal of the field (AA-916 WM-05), like the inline edit it replaces. Before CIS
457 is applied every route answers 503 ``versions_not_ready``.
"""
import json
from typing import Optional

import asyncpg
from fastapi import Body, Header, HTTPException, Request
from pydantic import BaseModel, Field

from api.routers.admin import verify_admin_secret
from api.routers.admin_pipeline._publish import enforce_publish_gate, request_actor
from api.routers.admin_pipeline._shared import logger, router
from api.routers.admin_pipeline.tour_detail import _record_field_edit_signals
from api.routers.admin_pipeline.uploads import _lifecycle_http
from api.services import content_publish_gate as gate
from services.acp_shared.audit_log import write_audit_log
from services.content_generation import human_lock
from services.content_generation import tour_draft as td
from services.quality import final_contract_store as _fc_store
from shared.tour_liveness import SOURCE_NOT_LIVE, SourceNotLiveError, source_not_live_message, source_state

MASTER_TENANT = "00000000-0000-0000-0000-000000000001"
NOT_READY = {"code": "versions_not_ready",
             "message": "Tour versions are not available yet: the database update (CIS 457) is not applied"}
RESOURCE = "tour_version"


class DraftSave(BaseModel):
    fields: dict = Field(default_factory=dict)
    reason: str = Field(min_length=td.REASON_MIN, max_length=td.REASON_MAX)
    base_version_num: Optional[int] = Field(None, ge=1)


class UnlockBody(BaseModel):
    reason: str = Field(min_length=td.REASON_MIN, max_length=td.REASON_MAX)


_COLS = ", ".join(f"gc.{f}" for f in td.DRAFT_FIELDS)
_DRAFT_SQL = f"""
    SELECT gc.id::text AS id, gc.version_num, gc.status::text AS status, gc.tenant_id, gc.origin,
           gc.parent_version_num, gc.edit_reason, gc.author_id, gc.edit_diff, gc.revalidate_passed, gc.human_edited,
           gc.created_at, gc.edited_at, {_COLS}
      FROM silver_aa_internal.generated_content gc
     WHERE gc.tour_id = $1::uuid AND gc.status = 'draft' AND gc.origin = 'human'
     ORDER BY gc.version_num DESC LIMIT 1
"""
_LIVE_SQL = f"""
    SELECT gc.id::text AS id, gc.version_num, p.human_lock_at, {_COLS}
      FROM gold_aa_internal.published_tours p
      JOIN silver_aa_internal.generated_content gc ON gc.id = p.generated_content_id
     WHERE p.tour_id = $1::uuid
"""


def _iso(v):
    return v.isoformat() if v is not None else None


def _draft_out(d) -> Optional[dict]:
    if d is None:
        return None
    return {"id": d["id"], "version_num": d["version_num"], "base_version_num": d["parent_version_num"],
            "reason": d["edit_reason"], "author": d["author_id"],
            "editors": td.editors_of(d["edit_diff"], d["author_id"]),
            "checks_passed": d["revalidate_passed"], "created_at": _iso(d["created_at"]),
            "edited_at": _iso(d["edited_at"]), "fields": td.fields_of(d)}


async def _read(conn, tour_id: str):
    """(draft row, live row) of the tour; 503 before CIS 457."""
    try:
        draft = await conn.fetchrow(_DRAFT_SQL, tour_id)
    except asyncpg.exceptions.UndefinedColumnError:
        raise HTTPException(status_code=503, detail=NOT_READY)
    live = await conn.fetchrow(_LIVE_SQL, tour_id)
    return draft, live


async def _state(conn, tour_id: str) -> dict:
    draft, live = await _read(conn, tour_id)
    live_fields = td.fields_of(live) if live else None
    return {
        "tour_id": tour_id,
        "live": ({"id": live["id"], "version_num": live["version_num"], "fields": live_fields} if live else None),
        "draft": _draft_out(draft),
        "diff": td.diff_fields(live_fields, td.fields_of(draft)) if draft else [],
        "lock": await human_lock.lock_of(conn, tour_id),
        "two_person_publish": await gate.switch_on(conn),
    }


async def _tour_exists(conn, tour_id: str) -> None:
    if not await conn.fetchval("SELECT 1 AS ok FROM silver_aa_internal.raw_tours WHERE tour_id = $1::uuid", tour_id):
        raise HTTPException(status_code=404, detail=f"Tour {tour_id} not found")


@router.get("/tours/{tour_id}/draft")
async def get_tour_draft(tour_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    async with request.app.state.pool.acquire() as conn:
        await _tour_exists(conn, tour_id)
        return await _state(conn, tour_id)


_INSERT_DRAFT_SQL = """
    INSERT INTO silver_aa_internal.generated_content (
        tour_id, tenant_id, version_num,
        aa_name, aa_subtitle, aa_summary, aa_description, aa_highlights, aa_itineraries,
        mobile_card_text, seo_title, seo_meta, seo_keywords_used, og_tags,
        model_editorial, model_schema, prompt_version, brand_rules_version,
        status, retry_count, metadata, fallback_used, human_edited, edited_at, edit_diff, revalidate_passed,
        reviewed_by, satellite_used, fs_id, origin, parent_version_num, edit_reason, author_id
    )
    SELECT g.tour_id, g.tenant_id,
           (SELECT MAX(x.version_num) + 1 FROM silver_aa_internal.generated_content x WHERE x.tour_id = g.tour_id),
           g.aa_name, g.aa_subtitle, g.aa_summary, g.aa_description, g.aa_highlights, g.aa_itineraries,
           g.mobile_card_text, g.seo_title, g.seo_meta, g.seo_keywords_used, g.og_tags,
           g.model_editorial, g.model_schema, g.prompt_version, g.brand_rules_version,
           'draft', 0, g.metadata - 'final_contract' - 'final_contract_log' - 's1_job_id' - 'recompose_of',
           false, true, now(), $3::jsonb, NULL,
           $4::uuid, false, g.fs_id, 'human', g.version_num, $5, $6
      FROM silver_aa_internal.generated_content g
     WHERE g.id = $2::uuid
    RETURNING id::text AS id
"""


async def _base_version(conn, tour_id: str, base_version_num: Optional[int]):
    if base_version_num is not None:
        row = await conn.fetchrow(
            "SELECT id::text AS id FROM silver_aa_internal.generated_content WHERE tour_id = $1::uuid "
            "AND version_num = $2 AND status <> 'draft'", tour_id, base_version_num)
        if row is None:
            raise HTTPException(status_code=404, detail=f"v{base_version_num} of this tour not found")
        return row["id"]
    live = await conn.fetchval("SELECT generated_content_id::text AS id FROM gold_aa_internal.published_tours "
                               "WHERE tour_id = $1::uuid", tour_id)
    if live:
        return live
    newest = await conn.fetchval(
        "SELECT id::text AS id FROM silver_aa_internal.generated_content WHERE tour_id = $1::uuid "
        "AND status <> 'draft' ORDER BY version_num DESC LIMIT 1", tour_id)
    if not newest:
        raise HTTPException(status_code=409, detail="This tour has no written version yet: run the rewrite first")
    return newest


def _reviewer_uuid(actor: str) -> Optional[str]:
    return actor.split(":", 1)[1] if actor.startswith("admin:") else None


@router.put("/tours/{tour_id}/draft")
async def put_tour_draft(tour_id: str, request: Request, body: DraftSave = Body(...),
                         x_admin_secret: str = Header(None)):
    """Create the tour's draft or save fields into it. The draft is a new version (never an overwrite of the live one);
    every save resets the re-check (a publish needs a passing re-validation of what is saved now)."""
    verify_admin_secret(x_admin_secret)
    invalid = set(body.fields) - set(td.DRAFT_FIELDS)
    if invalid:
        raise HTTPException(status_code=400, detail=f"Fields not allowed: {sorted(invalid)}")
    reason = td.clean_reason(body.reason)
    if reason is None:
        raise HTTPException(status_code=422, detail="Say why you edit this tour (3 to 500 characters)")
    actor = request_actor(request)
    fields = list(body.fields)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        await _tour_exists(conn, tour_id)
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 1146))", tour_id)
            draft, _live = await _read(conn, tour_id)
            created = draft is None
            if created:
                base_id = await _base_version(conn, tour_id, body.base_version_num)
                draft_id = await conn.fetchval(
                    _INSERT_DRAFT_SQL, tour_id, base_id, json.dumps(td.with_editor({}, actor, fields, reason)),
                    _reviewer_uuid(actor), reason, actor)
                draft, _live = await _read(conn, tour_id)
            else:
                draft_id = draft["id"]
            before = td.fields_of(draft)
            if fields:
                cols = ", ".join(
                    f"{f} = ${i + 1}::jsonb" if f in td.JSONB_FIELDS else f"{f} = ${i + 1}"
                    for i, f in enumerate(fields))
                n = len(fields)
                await conn.execute(
                    f"UPDATE silver_aa_internal.generated_content SET {cols}, edit_reason = ${n + 1},"
                    " edited_at = now(),"
                    f" edit_diff = ${n + 2}::jsonb, revalidate_passed = NULL, human_edited = true"
                    f" WHERE id = ${n + 3}::uuid AND status = 'draft'",
                    *[td.db_value(f, body.fields[f]) for f in fields], reason,
                    json.dumps(td.with_editor(draft["edit_diff"], actor, fields, reason)), draft_id)
                # AA-916 WM-05: one writing signal per changed writer-text field (same transaction as the save)
                sig_before = {"tenant_id": draft["tenant_id"],
                              **{f: td.db_value(f, before[f]) for f in fields}}
                await _record_field_edit_signals(conn, draft_id, sig_before, body.fields, fields, actor)
            await write_audit_log(
                conn, tenant_id=None, actor=actor, action="tour_version.draft_saved", resource_type=RESOURCE,
                resource_id=draft_id, details={
                    "tour_id": tour_id, "version_num": draft["version_num"], "created": created, "reason": reason,
                    "base_version_num": draft["parent_version_num"],
                    "before": {f: before[f] for f in fields}, "after": {f: body.fields[f] for f in fields}})
    # AA-1047 W1: the saved text through the REPORT-mode final contract (recorded, never blocks); after the commit.
    async with pool.acquire() as conn:
        await _fc_store.record_s1_exit(conn, draft_id, "human_edit")
        state = await _state(conn, tour_id)
    logger.info("tour_draft_saved", tour_id=tour_id, version_id=draft_id, created=created, fields=fields)
    return {**state, "created": created, "checks_required": True}


@router.delete("/tours/{tour_id}/draft")
async def discard_tour_draft(tour_id: str, request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    actor = request_actor(request)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            draft, _live = await _read(conn, tour_id)
            if draft is None:
                raise HTTPException(status_code=404, detail="This tour has no open draft")
            await conn.execute("UPDATE silver_aa_internal.generated_content SET status = 'rejected' "
                               "WHERE id = $1::uuid AND status = 'draft'", draft["id"])
            await write_audit_log(conn, tenant_id=None, actor=actor, action="tour_version.draft_discarded",
                                  resource_type=RESOURCE, resource_id=draft["id"],
                                  details={"tour_id": tour_id, "version_num": draft["version_num"]})
    return {"tour_id": tour_id, "discarded": draft["version_num"]}


@router.post("/tours/{tour_id}/draft/publish")
async def publish_tour_draft(tour_id: str, request: Request, x_admin_secret: str = Header(None)):
    """Make the open draft the tour's live version. The one gate: a passing re-validation of the saved text (409
    ``revalidate_required``), the two-person rule while cis.content.publish_two_person is on (403
    ``two_person_required`` / ``same_person``), a live source (409 ``source_not_live``); then process_export with exit
    ``human_publish`` (atomize, segment, source fan-out) locks the Master against automatic reruns."""
    verify_admin_secret(x_admin_secret)
    actor = request_actor(request)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            draft, live = await _read(conn, tour_id)
            if draft is None:
                raise HTTPException(status_code=404, detail="This tour has no open draft")
            await conn.execute(
                "SELECT 1 AS ok FROM silver_aa_internal.generated_content WHERE id = $1::uuid FOR UPDATE", draft["id"])
            await enforce_publish_gate(conn, request, editors=td.editors_of(draft["edit_diff"], draft["author_id"]))
            if draft["revalidate_passed"] is not True:
                raise HTTPException(
                    status_code=409, headers={"X-Error-Code": "revalidate_required"},
                    detail={"code": "revalidate_required",
                            "message": "Run the checks on this draft and let them pass before you publish it"})
            src = await source_state(conn, tour_id)
            if not src.live:
                raise HTTPException(status_code=409, detail=source_not_live_message(src, "publish this draft"),
                                    headers={"X-Error-Code": SOURCE_NOT_LIVE})
            await conn.execute("UPDATE silver_aa_internal.generated_content SET status = 'approved' "
                               "WHERE id = $1::uuid AND status = 'draft'", draft["id"])
            diff = td.diff_fields(td.fields_of(live) if live else None, td.fields_of(draft))
            await write_audit_log(
                conn, tenant_id=None, actor=actor, action="tour_version.published", resource_type=RESOURCE,
                resource_id=draft["id"], details={
                    "tour_id": tour_id, "version_num": draft["version_num"],
                    "replaced_version_num": live["version_num"] if live else None, "reason": draft["edit_reason"],
                    "fields": [d["field"] for d in diff],
                    "before": {d["field"]: d["before"] for d in diff}, "after": {d["field"]: d["after"] for d in diff}})

    from services.export.handler import process_export
    from services.tour_lifecycle import LifecycleError
    from services.tour_lifecycle.policy import POLICY_REVIEW_REQUIRED, PolicyReviewRequiredError
    try:
        exported = await process_export(draft["id"], contract_exit="human_publish",
                                        atomize_created_by="admin:publish_draft", lock_by=actor,
                                        lock_reason=draft["edit_reason"])
    except SourceNotLiveError as e:
        raise HTTPException(status_code=409, detail=source_not_live_message(e.state, "publish this draft"),
                            headers={"X-Error-Code": SOURCE_NOT_LIVE})
    except PolicyReviewRequiredError as e:   # AA-1049: elephant riding, the version is now in the Review Queue
        raise HTTPException(status_code=409, detail=f"{e} (v{draft['version_num']} is now in the Review Queue)",
                            headers={"X-Error-Code": POLICY_REVIEW_REQUIRED})
    except LifecycleError as e:
        raise _lifecycle_http(e)
    logger.info("tour_draft_published", tour_id=tour_id, version_id=draft["id"], version_num=draft["version_num"],
                atomize_job_id=exported.get("atomize_job_id"))
    return {"published": True, "tour_id": tour_id, "version_num": draft["version_num"], "version_id": draft["id"],
            "published_tour_id": exported.get("catalog_id"), "atomize_job_id": exported.get("atomize_job_id"),
            "locked": True}


@router.post("/tours/{tour_id}/unlock")
async def unlock_tour(tour_id: str, request: Request, body: UnlockBody = Body(...),
                      x_admin_secret: str = Header(None)):
    """Lift the hand-edit lock: the next wave or S1 run may replace this Master again."""
    verify_admin_secret(x_admin_secret)
    reason = td.clean_reason(body.reason)
    if reason is None:
        raise HTTPException(status_code=422, detail="Say why you unlock this tour (3 to 500 characters)")
    actor = request_actor(request)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            old = await human_lock.clear_lock(conn, tour_id)
            if old is None:
                raise HTTPException(status_code=409, detail="This tour is not locked")
            await write_audit_log(conn, tenant_id=None, actor=actor, action="tour_version.unlocked",
                                  resource_type=RESOURCE, resource_id=tour_id,
                                  details={"tour_id": tour_id, "before": old, "after": None, "reason": reason})
    return {"tour_id": tour_id, "unlocked": True, "was": old}


@router.get("/tours/{tour_id}/raw-revisions")
async def list_raw_revisions(tour_id: str, request: Request, x_admin_secret: str = Header(None),
                             limit: int = 50):
    verify_admin_secret(x_admin_secret)
    limit = max(1, min(int(limit), 200))
    async with request.app.state.pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                "SELECT revision_num, source, fields, before::text AS before, after::text AS after, actor, reason, "
                "resolutions::text AS resolutions, created_at FROM silver_aa_internal.raw_tour_revision "
                "WHERE tour_id = $1::uuid ORDER BY revision_num DESC LIMIT $2", tour_id, limit)
        except asyncpg.exceptions.UndefinedTableError:
            raise HTTPException(status_code=503, detail=NOT_READY)
    return {"tour_id": tour_id, "items": [
        {"revision_num": r["revision_num"], "source": r["source"], "fields": list(r["fields"]),
         "before": json.loads(r["before"]), "after": json.loads(r["after"]), "actor": r["actor"],
         "reason": r["reason"], "resolutions": json.loads(r["resolutions"]) if r["resolutions"] else None,
         "created_at": _iso(r["created_at"])} for r in rows]}
