# api/routers/admin_pipeline/uploads.py — S0 upload routes: upload URL, ingest, staging decisions, raw tour
# lifecycle, upload history.
# Moved verbatim out of the single admin_pipeline.py module (AA-1097); see __init__.py.

import json
import os
from uuid import UUID
import asyncpg
import boto3 as _boto3
from fastapi import Header, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from api.routers.admin import verify_admin_secret
from shared.aws_client.bronze import bronze_bucket
from shared.tour_liveness import SOURCE_NOT_LIVE, source_not_live_message, source_state

from api.routers.admin_pipeline._shared import IngestS3Request, UploadUrlRequest, UploadUrlResponse, logger, router


# ── POST /admin/upload-url ────────────────────────────────────────────────────

@router.post("/upload-url", response_model=UploadUrlResponse)
async def get_upload_url(body: UploadUrlRequest, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    import uuid as _uuid
    import re as _re
    tenant_id = "00000000-0000-0000-0000-000000000001"
    bucket    = bronze_bucket()
    safe_filename = _re.sub(r'[^a-zA-Z0-9._-]', '_', body.filename)
    s3_key    = f"raw-inbox/{tenant_id}/{_uuid.uuid4()}_{safe_filename}"
    s3 = _boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1"))
    upload_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": s3_key, "ContentType": body.content_type,
                "Metadata": {"seo-mode": body.seo_mode}},
        ExpiresIn=300,
    )
    return UploadUrlResponse(upload_url=upload_url, s3_key=s3_key, bucket=bucket)


# ── POST /admin/ingest-s3 ─────────────────────────────────────────────────────

@router.post("/ingest-s3")
async def ingest_s3(
    req: IngestS3Request,
    request: Request,
    x_admin_secret: str = Header(None),
):
    verify_admin_secret(x_admin_secret)

    _bucket = req.bucket or bronze_bucket()

    # ── dry_run=True: parse-only preview, no DB write ─────────────────────────
    if req.dry_run:
        import tempfile as _tempfile
        import hashlib as _hashlib
        from services.ingestion.excel_parser import ExcelParser as _ExcelParser
        import uuid as _uuid

        s3_client = _boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1"))
        with _tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            s3_client.download_fileobj(_bucket, req.s3_key, tmp)
            tmp_path = tmp.name

        with open(tmp_path, "rb") as fh:
            file_bytes = fh.read()
        file_hash = _hashlib.sha256(file_bytes).hexdigest()

        import re as _re

        def _clean_filename(path: str) -> str:
            """Strip UUID prefix and path prefix from s3 key or filename."""
            basename = path.split("/")[-1]
            return _re.sub(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}_', '', basename)

        pool = request.app.state.pool
        async with pool.acquire() as conn:
            existing_hash = await conn.fetchrow(
                "SELECT id, filename FROM silver_aa_internal.raw_sources WHERE file_hash = $1",
                file_hash,
            )
            if existing_hash and existing_hash["filename"]:
                incoming_name = _clean_filename(req.s3_key)
                stored_name   = _clean_filename(existing_hash["filename"])
                if incoming_name == stored_name:
                    return {
                        "status":  "blocked",
                        "reason":  "duplicate_file_hash",
                        "dry_run": True,
                        "message": "This file was already uploaded before — content and filename are an exact match.",
                    }

            parser = _ExcelParser(tmp_path, source_file=req.s3_key)
            records = parser.parse()

            all_names = [r["src_name"] for r in records if r.get("src_name")]
            duplicate_names: set = set()
            deleted_names: dict = {}   # AA-986 L2 (Q6): name -> date its tour was deleted (no live version left)
            if all_names:
                norm_names = [n.lower().strip() for n in all_names]
                dup_rows = await conn.fetch(
                    "SELECT lower(trim(rt.src_name)) AS n, "
                    "       bool_or(EXISTS (SELECT 1 FROM silver_aa_internal.raw_tours k "
                    "                        WHERE COALESCE(k.source_group_id, k.tour_id) "
                    "                              = COALESCE(rt.source_group_id, rt.tour_id) "
                    "                          AND k.source_status = 'active' AND k.deleted_at IS NULL)) AS live, "
                    "       max(rt.deleted_at) AS deleted_at "
                    "FROM silver_aa_internal.raw_tours rt "
                    "WHERE lower(trim(rt.src_name)) = ANY($1::text[]) "
                    "GROUP BY lower(trim(rt.src_name))",
                    norm_names,
                )
                duplicate_names = {r["n"] for r in dup_rows}
                deleted_names = {r["n"]: r["deleted_at"] for r in dup_rows
                                 if not r["live"] and r["deleted_at"] is not None}

            # AA-490 (follow-up AA-488 Gap 1): the real Commit path (process_file() ->
            # services/ingestion/handler.py) already drops in-file duplicates (two rows in the
            # SAME uploaded file sharing normalize_group_key(src_name, provider), neither yet
            # in the DB) via a seen_keys set -- correct behavior, but this preview branch never
            # ran that check, so the preview showed BOTH rows as "ready" and the admin only
            # discovered the drop after clicking Commit, with no explanation at preview time.
            # Mirrors handler.py's exact check order (in-file dedup BEFORE the DB-existence
            # check) so preview and commit report the identical outcome for the identical file.
            from services.ingestion.handler import normalize_group_key as _normalize_group_key

            # AA-690 — Jev ingest gates (same function the Commit path uses). Asked only for rows
            # with itinerary text; in shadow mode they only add notes, nothing is dropped.
            from services.ingestion.jev_gates import DROP_MESSAGES as _JEV_DROP, assess_rows as _jev_assess
            candidates = records[:req.max_tours]
            jev_idx = [i for i, r in enumerate(candidates) if (r.get("src_itineraries") or "").strip()]
            jev_list = await _jev_assess([candidates[i] for i in jev_idx], pool, _clean_filename(req.s3_key))
            jev_by_row = dict(zip(jev_idx, jev_list))
            # AA-690 A0-2 — the same near-duplicate rule the Commit path applies.
            from services.ingestion.near_duplicate import Checker as _NearDupChecker
            near_dups = _NearDupChecker(conn, "00000000-0000-0000-0000-000000000001")

            ready_tours = []
            blocked_tours = []
            seen_keys: set = set()
            for idx, r in enumerate(candidates):
                src_name = r.get("src_name") or "(no name)"
                jev = jev_by_row.get(idx)
                if jev is not None and jev.country and not r.get("country"):
                    # AA-990 (AA-490 parity): Commit never writes Jev's guess (services/ingestion/handler.py), so the
                    # preview does not either; the row reports its missing country as Commit would store it.
                    jev.notes.append(f"Jev suggests {jev.country}; not written: the source row and file name state "
                                     "no country")
                missing = [f for f in ["src_name", "country", "duration", "price_raw"] if not r.get(f)]
                nname, nprov = _normalize_group_key(src_name, r.get("provider"))
                if (nname, nprov) in seen_keys:
                    blocked_tours.append({
                        "src_name": src_name, "country": r.get("country"),
                        "reason": "duplicate_in_file",
                        "message": "Another row earlier in this file has the same name + "
                                   "provider — this row will be skipped on Commit.",
                    })
                elif src_name.lower().strip() in duplicate_names:
                    deleted_on = deleted_names.get(src_name.lower().strip())
                    blocked_tours.append({
                        "src_name": src_name, "country": r.get("country"),
                        "reason": "duplicate_tour",
                        "message": (f"This tour was deleted on {deleted_on.date().isoformat()} — Duplicate Review "
                                    "will offer Restore or Create a new tour." if deleted_on
                                    else "This tour already exists in the system."),
                        "deleted_at": deleted_on.isoformat() if deleted_on else None,
                    })
                elif (r.get("src_itineraries") or "").strip() and \
                        (near := await near_dups.check(r)).duplicate is not None:
                    blocked_tours.append({
                        "src_name": src_name, "country": r.get("country"),
                        "reason": "duplicate_of_existing", "message": near.message(),
                    })
                elif missing:
                    blocked_tours.append({
                        "src_name": src_name, "country": r.get("country"),
                        "reason": "missing_fields", "missing_fields": missing,
                        "message": f"Missing fields: {', '.join(missing)}",
                        "jev_notes": list(jev.notes) if jev is not None else [],
                    })
                elif not (r.get("src_itineraries") or "").strip():
                    # AA-604: a row with no itinerary body cannot be rewritten by S1
                    # (S1 get_all_tours + acp_contract.v_trip_registry require a non-empty
                    # itinerary). Surface it here at preview time instead of silently
                    # committing it as "ingested" and having it vanish between S0 and S1.
                    # Covers POI/attraction rows (museums, rentals, single activities) and
                    # source files that simply lack itinerary content.
                    blocked_tours.append({
                        "src_name": src_name, "country": r.get("country"),
                        "reason": "empty_itinerary",
                        "message": "No itinerary content — this looks like a point of interest "
                                   "or activity, not a multi-day tour. It cannot be rewritten and "
                                   "will be skipped on Commit.",
                    })
                elif jev is not None and jev.drop_reason:
                    blocked_tours.append({
                        "src_name": src_name, "country": r.get("country"),
                        "reason": jev.drop_reason,
                        "message": _JEV_DROP[jev.drop_reason].format(
                            kind=(jev.kind or "").replace("_", " ")),
                    })
                else:
                    seen_keys.add((nname, nprov))
                    near = await near_dups.check(r)
                    near_dups.accept(r)
                    notes = (jev.notes if jev is not None else []) + ([near.note()] if near.note() else [])
                    ready_tours.append({
                        "jev_notes":       notes,
                        "tour_id":         str(_uuid.uuid4()),
                        "src_name":        src_name,
                        "country":         r.get("country"),
                        "duration":        r.get("duration"),
                        "price_raw":       r.get("price_raw"),
                        "group_size":      r.get("group_size"),
                        "period":          r.get("period"),
                        "pipeline_status": "preview",
                        "ingest_at":       "",
                        "src_subtitle":    r.get("src_subtitle"),
                        "src_summary":     r.get("src_summary"),
                        "src_highlights":  r.get("src_highlights"),
                        "src_itineraries": r.get("src_itineraries"),
                        "provider":        r.get("provider"),
                        "activities":      r.get("activities"),
                        "inclusions":      r.get("inclusions"),
                        "exclusions":      r.get("exclusions"),
                        "sku":             r.get("sku"),
                        "src_description": r.get("src_description"),
                        "links":           r.get("links"),
                        "feature":         r.get("feature"),
                        "best_time_to_go": r.get("best_time_to_go"),
                    })

            sources = await conn.fetch("""
                SELECT filename, s3_path, row_count, parsed_at, parse_errors, file_hash
                FROM silver_aa_internal.raw_sources
                WHERE tenant_id = $1::uuid
                ORDER BY parsed_at DESC LIMIT 20
            """, UUID("00000000-0000-0000-0000-000000000001"))

            return {
                "status":         "parsed",
                "dry_run":        True,
                "batch_id":       None,
                "ready_count":    len(ready_tours),
                "blocked_count":  len(blocked_tours),
                "tours":          ready_tours,
                "blocked_tours":  blocked_tours,
                "upload_history": [dict(s) for s in sources],
            }

    # ── dry_run=False: insert raw_sources + raw_tours, no pipeline trigger ────
    from services.ingestion.handler import process_file
    result = await process_file(_bucket, req.s3_key, seo_mode=req.seo_mode)
    if result.get("status") == "skipped_duplicate":
        return {"status": "duplicate", "batch_id": None, "tour_count": 0}

    batch_id     = result.get("source_id")
    tours_staged = result.get("tours_staged", 0)
    staged_ids   = result.get("staged_ids", [])

    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tour_id FROM silver_aa_internal.raw_tours WHERE batch_id = $1::uuid",
            batch_id,
        )

    return {
        "status":        "done",
        "batch_id":      str(batch_id),
        "tour_count":    len(rows),
        "tours_written": len(rows),
        "tours_staged":  tours_staged,
        "staged_ids":    staged_ids,
    }


# ── GET /admin/upload-staging/{batch_id} — list staged duplicates ────────────
# NOTE: these routes use /upload-staging/ and /raw-tours/ prefixes (not /tours/)
# so there is no FastAPI greedy-matching conflict with /tours/{tour_id}/...

class DecideRequest(BaseModel):
    decision: str  # bypass | replace | update | keep_both | restore | create_new | merge (AA-1146 T1)
    # merge only: {field: "incoming" | "current"} for every hand-edited field that differs, and why (3..500 characters)
    resolutions: Optional[dict] = None
    reason: Optional[str] = None


def _merge_cols() -> str:
    from services.ingestion import excel_merge as em
    return ", ".join(em.MERGE_FIELDS)


async def _merge_inputs(conn, staging_id: str) -> tuple:
    """(staging row, matched raw tour row, hand-edited fields, plan) of a staged re-import. 404 / 409 when it cannot
    merge: no pending row, no matched tour, the matched tour is not live (deleted: Restore or Create new) or it is a
    hand-made tour (409 ``fact_draft_hand_made``, AA-1181: Bypass or Create new)."""
    from api.routers.admin_pipeline.fact_drafts import refuse_hand_made
    from services.ingestion import excel_merge as em
    staging = await conn.fetchrow("""
        SELECT id::text AS id, parsed_payload, matched_tour_id::text AS matched_tour_id
          FROM silver_aa_internal.upload_staging WHERE id = $1::uuid AND decision = 'pending'
    """, staging_id)
    if staging is None:
        raise HTTPException(status_code=404, detail="Staging row not found or already decided")
    if not staging["matched_tour_id"]:
        raise HTTPException(status_code=409, detail="This row matches no existing tour: nothing to merge into")
    tour_id = staging["matched_tour_id"]
    src = await source_state(conn, tour_id)
    if not src.live:
        raise HTTPException(status_code=409, detail=source_not_live_message(src, "merge this sheet into it"),
                            headers={"X-Error-Code": SOURCE_NOT_LIVE})
    await refuse_hand_made(conn, tour_id, "merge")     # AA-1181: a supplier sheet never merges into a hand-made tour
    current = await conn.fetchrow(
        f"SELECT tour_id::text AS tour_id, {_merge_cols()} FROM silver_aa_internal.raw_tours "
        "WHERE tour_id = $1::uuid FOR UPDATE", tour_id)
    try:
        revisions = await conn.fetch(
            "SELECT revision_num, source, fields, resolutions::text AS resolutions "
            "FROM silver_aa_internal.raw_tour_revision WHERE tour_id = $1::uuid", tour_id)
    except asyncpg.exceptions.UndefinedTableError:
        raise HTTPException(status_code=503, detail={
            "code": "versions_not_ready",
            "message": "The merge is not available yet: the database update (CIS 457) is not applied"})
    payload = staging["parsed_payload"]
    incoming = json.loads(payload) if isinstance(payload, str) else dict(payload or {})
    if incoming.get("country"):
        from shared import countries
        c = countries.resolve(incoming["country"] if isinstance(incoming["country"], str) else None)
        if c is None:
            incoming.pop("country")          # an unknown country never overwrites a valid one
        else:
            incoming["country"] = c.name
    owned = em.hand_edited_fields([dict(r) for r in revisions])
    return staging, dict(current), owned, em.plan(dict(current), incoming, owned)


@router.get("/upload-staging/{staging_id}/merge-preview")
async def merge_preview(staging_id: str, request: Request, x_admin_secret: str = Header(None)):
    """AA-1146 T1: what a merge of this staged re-import into its tour would do, field by field: ``auto`` (nobody
    edited it by hand: the sheet's value is taken), ``conflicts`` (a person edited it: choose incoming or current),
    ``unchanged``. ``read_only``: fields the caller may not change (D5: the price text without reference_price.write);
    ``open_draft``: the tour already has a sheet draft waiting (a merge is refused until it is published or discarded).
    Nothing is written."""
    from services.ingestion import excel_merge as em
    from api.routers.admin_pipeline._publish import may_edit_price
    from api.routers.admin_pipeline.fact_drafts import open_fact_draft
    verify_admin_secret(x_admin_secret)
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            staging, current, owned, merge_plan = await _merge_inputs(conn, staging_id)
            open_draft = await open_fact_draft(conn, current["tour_id"])
    return {"staging_id": staging_id, "tour_id": current["tour_id"], **merge_plan,
            "hand_edited": sorted(owned), "read_only": [] if may_edit_price(request) else sorted(em.PRICE_FIELDS),
            "open_draft": open_draft["id"] if open_draft else None}


def _merge_changes(request: Request, merge_plan: dict, resolutions_in) -> tuple:
    """(changes, resolutions, withheld) of a merge: the person's choices applied, then D5: without reference_price.write
    choosing the sheet's price is refused (403 price_read_only) and an untouched price change is held back (the tour
    keeps its price; the result names the sheet's value for the pricing team)."""
    from services.ingestion import excel_merge as em
    from api.routers.admin_pipeline._publish import may_edit_price, price_read_only
    try:
        changes, resolutions = em.resolve(merge_plan, resolutions_in)
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"code": "merge_unresolved", "message": str(e),
                                                     "conflicts": merge_plan["conflicts"]})
    if may_edit_price(request):
        return changes, resolutions, {}
    if any(resolutions.get(f) == em.INCOMING for f in em.PRICE_FIELDS):
        raise price_read_only()
    changes, withheld = em.withhold(changes, em.PRICE_FIELDS)
    return changes, resolutions, withheld


async def _decide_merge(request: Request, staging_id: str, body: "DecideRequest", actor: str) -> dict:
    """AA-1146 T1: merge a staged re-import into its tour (same tour id), the hand edits winning unless the person
    takes the sheet's value. The result is a DRAFT of the tour's facts (silver_aa_internal.raw_tour_fact_draft): the
    tour is not touched; it changes only when a person publishes the draft through the gate (POST
    /admin/tours/{id}/fact-draft/publish, fact_drafts.py). One audit row, the staging row removed, in one
    transaction."""
    from services.ingestion import excel_merge as em
    from api.routers.admin_pipeline.fact_drafts import create_fact_draft
    from api.routers.admin_pipeline.tour_detail import _raw_json
    from services.acp_shared.audit_log import write_audit_log
    reason = (body.reason or "").strip()
    if not 3 <= len(reason) <= 500:
        raise HTTPException(status_code=422, detail="Say why you merge this sheet (3 to 500 characters)")
    async with request.app.state.pool.acquire() as conn:
        async with conn.transaction():
            staging, current, owned, merge_plan = await _merge_inputs(conn, staging_id)
            changes, resolutions, withheld = _merge_changes(request, merge_plan, body.resolutions)
            tour_id = current["tour_id"]
            before = {f: _raw_json(current[f]) for f in changes}
            draft_id = None
            if changes:
                draft_id = await create_fact_draft(conn, tour_id=tour_id, staging_id=staging_id, before=before,
                                                   after=changes, resolutions=resolutions, author=actor, reason=reason)
            await write_audit_log(conn, tenant_id=None, actor=actor, action="raw_tour.excel_merge_drafted",
                                  resource_type="raw_tour", resource_id=tour_id, details={
                                      "staging_id": staging_id, "fields": sorted(changes), "reason": reason,
                                      "resolutions": resolutions, "auto": [a["field"] for a in merge_plan["auto"]],
                                      "withheld": withheld, "before": before, "after": changes,
                                      "fact_draft_id": draft_id})
            await conn.execute("DELETE FROM silver_aa_internal.upload_staging WHERE id = $1::uuid", staging_id)
    kept = sorted(f for f, c in resolutions.items() if c == em.CURRENT)
    logger.info("staging_merged_as_draft", staging_id=staging_id, tour_id=tour_id, fields=sorted(changes), kept=kept,
                withheld=sorted(withheld), fact_draft_id=draft_id)
    return {"staging_id": staging_id, "decision": "merge", "committed": True, "tour_id": tour_id,
            "fact_draft_id": draft_id, "pending": sorted(changes), "kept": kept, "withheld": withheld,
            "published": False}


# Q6 (ADR 0015): the deleted state of the matched key, per staging row. A key is deleted when none of its versions is
# live and one was trashed; replace / update / keep_both are refused then (no silent revival), restore / create_new
# are offered instead.
_STAGING_KEY_DELETED_SQL = """
    LEFT JOIN LATERAL (
        SELECT bool_or(k.source_status = 'active' AND k.deleted_at IS NULL) AS has_live,
               max(k.deleted_at) FILTER (WHERE k.source_status = 'trashed' OR k.deleted_at IS NOT NULL)
                   AS deleted_at
          FROM silver_aa_internal.raw_tours k
         WHERE COALESCE(k.source_group_id, k.tour_id) = COALESCE(us.matched_source_group_id, us.matched_tour_id)
    ) kd ON true
"""
# AA-1181: the matched key has a version made by hand in the content workspace (raw_tours.entry_origin 'manual', CIS
# 478; read through to_jsonb, so false before it): a supplier sheet never merges into, replaces or joins it, so the UI
# offers Bypass and Create a new tour only (services/tour_lifecycle supersede refuses the rest, 409 source_hand_made).
_STAGING_KEY_HAND_MADE_SQL = """
    LEFT JOIN LATERAL (
        SELECT bool_or(to_jsonb(h) ->> 'entry_origin' = 'manual') AS hand_made
          FROM silver_aa_internal.raw_tours h
         WHERE COALESCE(h.source_group_id, h.tour_id) = COALESCE(us.matched_source_group_id, us.matched_tour_id)
    ) hm ON true
"""


@router.get("/upload-staging/{batch_id}")
async def list_upload_staging(
    batch_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
):
    """Return staging rows + existing tour fields for side-by-side comparison. AA-986 L2 (Q6): `existing` carries the
    matched row's source_status / deleted_at, and `matched_deleted` / `matched_deleted_at` say the matched tour was
    deleted at S0/A0 (the UI then offers Restore or Create a new tour). AA-1181: `matched_hand_made` says the matched
    tour was made by hand in the content workspace (the UI then offers Bypass or Create a new tour)."""
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(f"""
            SELECT
                us.id::text         AS staging_id,
                us.parsed_payload,
                us.matched_tour_id::text,
                us.matched_source_group_id::text,
                us.decision,
                us.created_at,
                rt.src_name, rt.src_summary, rt.country, rt.price_raw,
                rt.provider, rt.ingest_at,
                rt.source_status::text AS source_status, rt.deleted_at,
                (kd.deleted_at IS NOT NULL AND NOT COALESCE(kd.has_live, false)) AS matched_deleted,
                kd.deleted_at AS key_deleted_at,
                COALESCE(hm.hand_made, false) AS matched_hand_made
            FROM silver_aa_internal.upload_staging us
            LEFT JOIN silver_aa_internal.raw_tours rt
                ON rt.tour_id = us.matched_tour_id
            {_STAGING_KEY_DELETED_SQL}
            {_STAGING_KEY_HAND_MADE_SQL}
            WHERE us.batch_id = $1::uuid
              AND us.decision = 'pending'
            ORDER BY us.created_at
        """, batch_id)

    return {
        "batch_id": batch_id,
        "items": [
            {
                "staging_id":              r["staging_id"],
                "matched_tour_id":         r["matched_tour_id"],
                "matched_source_group_id": r["matched_source_group_id"],
                "decision":                r["decision"],
                "created_at":              r["created_at"].isoformat() if r["created_at"] else None,
                "matched_deleted":         bool(r["matched_deleted"]),
                "matched_deleted_at":      (r["key_deleted_at"].isoformat()
                                            if r["matched_deleted"] and r["key_deleted_at"] else None),
                "matched_hand_made":       bool(r["matched_hand_made"]),
                "incoming": (
                    json.loads(r["parsed_payload"])
                    if isinstance(r["parsed_payload"], str)
                    else dict(r["parsed_payload"] or {})
                ),
                "existing": {
                    "src_name":  r["src_name"],
                    "src_summary": r["src_summary"],
                    "country":   r["country"],
                    "price_raw": r["price_raw"],
                    "provider":  r["provider"],
                    "ingest_at": r["ingest_at"].isoformat() if r["ingest_at"] else None,
                    "source_status": r["source_status"],
                    "deleted_at": r["deleted_at"].isoformat() if r["deleted_at"] else None,
                },
            }
            for r in rows
        ],
        "total": len(rows),
    }


def _lifecycle_http(e) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.detail, headers={"X-Error-Code": e.code})


@router.post("/upload-staging/{staging_id}/decide")
async def decide_staging(
    staging_id: str,
    body: DecideRequest,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
):
    """Apply decision for one staged duplicate row.

    bypass     → discard incoming; delete staging row
    replace    → incoming INSERT as active; old active → superseded (its Master switched off); delete staging
    update     → same as replace (alias)
    keep_both  → incoming INSERT as superseded; active unchanged; delete staging
                 (replace / update / keep_both / merge on a hand-made match → 409, AA-1181: Bypass or Create new)
    restore    → (matched tour deleted at S0/A0 only) restore the deleted tour, discard incoming (Q6)
    create_new → incoming INSERT as a new tour (new key), the matched one untouched (Q6)

    AA-986 L2: every write goes through services/tour_lifecycle; replace / update / keep_both on a deleted tour → 409
    source_deleted (never a silent revival).
    """
    # S241: the header was declared but never checked, so this write was public on the API domain.
    verify_admin_secret(x_admin_secret)
    VALID = {"bypass", "replace", "update", "keep_both", "restore", "create_new", "merge"}
    if body.decision not in VALID:
        raise HTTPException(status_code=400, detail=f"decision must be one of {VALID}")
    if body.decision == "merge":   # AA-1146 T1: field by field into the existing tour, hand edits win
        from api.routers.admin_pipeline._publish import request_actor
        return await _decide_merge(request, staging_id, body, request_actor(request))
    from services.tour_lifecycle import LifecycleError, deleted_key_info, key_of, restore_source, run_after_commit
    from services.tour_lifecycle import supersede as _supersede
    actor = x_admin_user_id[:64] if isinstance(x_admin_user_id, str) and x_admin_user_id else "admin"

    pool = request.app.state.pool
    outcome = None
    async with pool.acquire() as conn:
        staging_row = await conn.fetchrow("""
            SELECT id, batch_id, tenant_id, parsed_payload,
                   matched_tour_id, matched_source_group_id
            FROM silver_aa_internal.upload_staging
            WHERE id = $1::uuid AND decision = 'pending'
        """, staging_id)

        if not staging_row:
            raise HTTPException(status_code=404, detail="Staging row not found or already decided")

        tenant_id = str(staging_row["tenant_id"])
        matched_tour_id = str(staging_row["matched_tour_id"]) if staging_row["matched_tour_id"] else None
        _msg = staging_row["matched_source_group_id"]
        matched_source_group_id = str(_msg) if _msg else None
        payload = (
            json.loads(staging_row["parsed_payload"])
            if isinstance(staging_row["parsed_payload"], str)
            else dict(staging_row["parsed_payload"] or {})
        )

        try:
            async with conn.transaction():
                if body.decision == "bypass":
                    await conn.execute(
                        "DELETE FROM silver_aa_internal.upload_staging WHERE id = $1::uuid",
                        staging_id,
                    )
                    return {"staging_id": staging_id, "decision": "bypass", "committed": False}

                new_tour_id = None
                if body.decision == "restore":
                    info = await deleted_key_info(conn, await key_of(conn, matched_tour_id)) \
                        if matched_tour_id else {"deleted": False}
                    if not info["deleted"]:
                        raise HTTPException(status_code=409, detail="not_deleted: the matched tour is not deleted; "
                                                                    "choose Replace, Keep both or Bypass",
                                            headers={"X-Error-Code": "not_deleted"})
                    outcome = await restore_source(conn, info["trashed_tour_id"], actor=actor)
                    restored_tour_id = outcome.tour_id
                else:
                    outcome = await _supersede(
                        conn, decision=body.decision, tenant_id=tenant_id, batch_id=str(staging_row["batch_id"]),
                        payload=payload, matched_tour_id=matched_tour_id, matched_group_id=matched_source_group_id,
                        actor=actor)
                    new_tour_id = outcome.tour_id
                    restored_tour_id = None

                await conn.execute(
                    "DELETE FROM silver_aa_internal.upload_staging WHERE id = $1::uuid",
                    staging_id,
                )
        except LifecycleError as e:
            raise _lifecycle_http(e)

    await run_after_commit(pool, outcome)
    logger.info("staging_decided", staging_id=staging_id, decision=body.decision,
                new_tour_id=new_tour_id, restored_tour_id=restored_tour_id)
    return {
        "staging_id": staging_id,
        "decision":   body.decision,
        "committed":  True,
        "new_tour_id": new_tour_id,
        "restored_tour_id": restored_tour_id,
        "lifecycle": outcome.summary(),
    }


@router.post("/raw-tours/{tour_id}/set-active")
async def set_tour_active(
    tour_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
):
    """Make this version the live one of its source group; the others are superseded (their Masters switched off).
    AA-986 L2: through services/tour_lifecycle; a trashed version → 409 source_trashed (restore it instead)."""
    verify_admin_secret(x_admin_secret)
    from services.tour_lifecycle import LifecycleError, run_after_commit, set_active
    actor = x_admin_user_id[:64] if isinstance(x_admin_user_id, str) and x_admin_user_id else "admin"
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            outcome = await set_active(conn, tour_id, actor=actor)
    except LifecycleError as e:
        raise _lifecycle_http(e)
    logger.info("tour_set_active", tour_id=tour_id, source_id=outcome.source_id)
    await run_after_commit(pool, outcome)
    return {"tour_id": outcome.tour_id, "source_status": "active", "lifecycle": outcome.summary()}


@router.delete("/raw-tours/{tour_id}")
async def soft_delete_raw_tour(
    tour_id: str,
    request: Request,
    x_admin_secret: str = Header(None),
    x_deleted_by: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
    x_admin_user_id: Optional[str] = Header(None),
):
    """Soft-delete raw_tours row (source_status='trashed'). AA-986 L2: the same retire as PATCH
    /admin/tours/{id}/trash (services/tour_lifecycle: Master switched off, reviews dismissed, queued tenant jobs
    cancelled, tenants warned). A published tour needs Master Content rights (Q8); before L2 it answered 409."""
    verify_admin_secret(x_admin_secret)
    from api.routers.admin import _require_master_admin_if_published
    from services.tour_lifecycle import LifecycleError, retire_source, run_after_commit
    await _require_master_admin_if_published(request, tour_id, x_admin_role)
    actor = x_admin_user_id[:64] if isinstance(x_admin_user_id, str) and x_admin_user_id else "admin"
    pool = request.app.state.pool
    try:
        async with pool.acquire() as conn:
            outcome = await retire_source(conn, tour_id, actor=actor, deleted_by=x_deleted_by or "admin")
    except LifecycleError as e:
        raise _lifecycle_http(e)
    await run_after_commit(pool, outcome)
    logger.info("tour_soft_deleted", tour_id=tour_id, deleted_by=x_deleted_by)
    return {"tour_id": outcome.tour_id, "source_status": "trashed", "lifecycle": outcome.summary()}


# ── GET /admin/upload-history ─────────────────────────────────────────────────

def _count_parse_errors(parse_errors) -> int:
    if not parse_errors:
        return 0
    if isinstance(parse_errors, list):
        return len(parse_errors)
    if isinstance(parse_errors, dict):
        return sum(len(v) if isinstance(v, list) else 1 for v in parse_errors.values())
    try:
        import json as _j
        parsed = _j.loads(parse_errors)
        return len(parsed) if isinstance(parsed, list) else 0
    except Exception:
        return 0


@router.get("/upload-history")
async def get_upload_history(request: Request, x_admin_secret: str = Header(None)):
    verify_admin_secret(x_admin_secret)
    pool = request.app.state.pool
    async with pool.acquire() as conn:
        # AA-344: LEFT JOIN shared.pipeline_runs on batch_id to surface the real
        # landed/dropped counts (migration 091, AA-343 Part C) alongside row_count (the
        # PARSED count only — the number that misleadingly read "30" while only 16 rows
        # actually landed, AA-343 P1.3). LEFT JOIN (not JOIN): any source ingested before
        # migration 091, or whose batch_id has no matching pipeline_runs row, must still
        # show up in Upload History with rows_landed/rows_dropped simply NULL — degrade
        # gracefully, per the issue's own proposal, not drop the row.
        sources = await conn.fetch("""
            SELECT rs.id::text, rs.filename, rs.file_size_kb, rs.row_count, rs.parsed_at,
                   rs.parse_errors, rs.batch_id::text,
                   pr.ingest_details->>'rows_landed'  AS rows_landed,
                   pr.ingest_details->>'rows_dropped' AS rows_dropped
            FROM silver_aa_internal.raw_sources rs
            LEFT JOIN shared.pipeline_runs pr ON pr.batch_id = rs.batch_id
            WHERE rs.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
            ORDER BY rs.parsed_at DESC LIMIT 20
        """)
    return {
        "sources": [
            {
                "id":                s["id"],
                "filename":          s["filename"],
                "file_size_kb":      float(s["file_size_kb"]) if s["file_size_kb"] else None,
                "row_count":         s["row_count"],
                "rows_landed":       int(s["rows_landed"]) if s["rows_landed"] is not None else None,
                "rows_dropped":      int(s["rows_dropped"]) if s["rows_dropped"] is not None else None,
                "parsed_at":         str(s["parsed_at"]) if s["parsed_at"] else None,
                "parse_error_count": _count_parse_errors(s["parse_errors"]),
                "batch_id":          s["batch_id"],
            }
            for s in sources
        ],
    }
