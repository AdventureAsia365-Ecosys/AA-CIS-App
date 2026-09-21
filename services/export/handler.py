import asyncio
import asyncpg
import json
import os
import structlog
from shared.secrets import get_database_url
from shared.repository.published_catalog_repository import PublishedCatalogRepository

logger = structlog.get_logger()

# AA-526 — strong refs for the A3-triggered atomize background task, same GC-safety pattern
# api/routers/v1_tours.py::trigger_rewrite() / v1_content_writing.py::write() already use — a
# bare asyncio.create_task() with no reference can be garbage-collected mid-flight. Module-level
# here (not per-call) for the same reason those 2 call sites keep theirs at module level.
_background_tasks: set = set()


class _SingleConnAsPool:
    """AA-526 — process_export() (and this class's other user, _run_a3_atomize_background()
    below) each own exactly ONE asyncpg.Connection, Lambda-handler style — no asyncpg.Pool in
    scope the way every other real caller of services.acp_produce.tenant_pipeline.run_t5_atomize()
    has (T5's tenant-facing endpoint, api/routers/v1_tours.py, always runs inside a FastAPI
    request with request.app.state.pool). run_t5_atomize()/atom_extraction.py are reused
    UNCHANGED (AA-526's own instruction) rather than reworked to accept a bare Connection — this
    thin adapter exposes the one `.acquire()` async-context-manager shape they call, yielding the
    SAME connection every time. Safe here specifically because every real call path into
    run_t5_atomize() (_atomize_whole_tour_legacy/_atomize_per_day) acquires-and-releases
    sequentially, never concurrently (that module's own docstring: "Days are read SEQUENTIALLY,
    not concurrently") — a real pool with >1 physical connection is never required."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


async def recompute_segment_score_route(tour_id: str, pool, *, log_tour_id: str | None = None) -> dict:
    """AA-564 3.1 — extracted out of `_run_a3_atomize_background()` below (which still calls this
    right after atomize, unchanged) so it can ALSO be fired on its own, from
    `api/routers/admin_atoms.py::patch_atom()`, whenever an atom's `deleted` flag changes. Before
    AA-564, curating an atom (star or soft-delete) never recomputed Segment/Score/Route at all —
    AA-563's investigation confirmed this was a real staleness gap, not a misunderstanding: only
    `deleted` actually affects Segment eligibility (`WHERE NOT ta.deleted`, segment_matching.py),
    `starred` never does, so this is deliberately NOT wired to the star action too.

    Platform-wide (AA-545) — `run_route_detection()` recomputes ALL tours' Routes, not just this
    one, same as `_run_a3_atomize_background()` already did; `run_segment_matching(tour_id, ...)`
    itself is incremental and does stay scoped to this one tour."""
    from services.acp_contract.segment_matching import run_segment_matching
    segment_result = await run_segment_matching(tour_id, pool)
    logger.info("segment_matching_done", tour_id=log_tour_id or tour_id, result=segment_result)

    from services.acp_contract.atom_ranking import precompute_question_landings, run_atom_ranking
    from services.seo_intelligence.seed_builder import DFS_LOCATION_MAP
    # AA-610 (Sub 2 redesign) — PAA landing does not vary by market (a question landing on a
    # Segment's atom has nothing to do with which finite buyer market is being scored), but used
    # to be recomputed inside run_atom_ranking() itself, once per market — 6x real embedding-
    # matching work per atomize run for no reason. Computed exactly ONCE here, passed into every
    # run_atom_ranking() call below (precompute_question_landings()'s own docstring has the full
    # story — this is what a live re-atomize test found never completing within several minutes
    # before this fix).
    question_counts = await precompute_question_landings(pool)
    ranking_results = {}
    for market_code in DFS_LOCATION_MAP:
        ranking_results[market_code] = await run_atom_ranking(market_code, pool, question_counts)
    logger.info("ranking_done", tour_id=log_tour_id or tour_id, result=ranking_results)

    from services.acp_contract.route_detection import run_route_detection
    route_result = await run_route_detection(pool)
    logger.info("route_detection_done", tour_id=log_tour_id or tour_id, result=route_result)
    return {"segment": segment_result, "ranking": ranking_results, "route": route_result}


async def _run_a3_atomize_background(tour_id: str, rewritten: dict, country: str, version_id: str) -> None:
    """AA-526 — the actual A3 atomize call, launched fire-and-forget from process_export() so a
    slow multi-day LLM atomize run (services.acp_produce.tenant_pipeline.run_t5_atomize(), up to
    one invoke_claude() call per itinerary day) never adds latency to — or risks an API Gateway
    504 on — the admin action that triggers publish (api/routers/admin_pipeline.py /
    v1_pipeline.py, both `await process_export(...)` directly in their own request handler).
    Opens its OWN connection (process_export()'s own `conn` is closed in its `finally` block
    before this task would otherwise still be running) — completely independent lifecycle,
    mirroring how services/acp_content_writing/service.py::run_write_background() is launched
    with its own already-open pool rather than reusing the request's.

    owner_scope="platform" (not a tenant UUID) — atoms produced here are a shared backend
    resource, per AA-525/526's architecture decision (Nghiệp, 04/09/2026): tenants never create
    or see atoms directly anymore, curation moves to AA-admin (AA-527)."""
    conn = await asyncpg.connect(get_database_url(), ssl="require")
    try:
        from services.acp_produce.tenant_pipeline import run_t5_atomize
        pool = _SingleConnAsPool(conn)
        result = await run_t5_atomize(
            "platform", tour_id, rewritten, pool,
            country=country, version_id=version_id,
        )
        logger.info("a3_atomize_done", tour_id=tour_id, result=result)

        # AA-545 — Segment/Score/Route now DO run here, platform-wide, right after atomize.
        # AA-526's own note (kept below, historical) explains why this was blocked at the time:
        # `atom_segment.tenant_id NOT NULL` (migration 129) would have failed the FK/UUID cast
        # outright, and `atom_ranking.py` read Segments `WHERE tenant_id = $1::uuid` for one
        # tenant specifically. AA-545 (migration 146) removed both blockers — Segment/Score/Route
        # are genuinely platform-wide now (ADR-0001/0003), so this is their real, permanent
        # trigger point, not a workaround.
        #
        # AA-526's original note, for history: "Segment-matching is DELIBERATELY NOT run here...
        # Segment/Route/Subject stay PER-TENANT products... not a single global Segment set." —
        # superseded by AA-545; Segment/Score/Route are that single global set now, Slate/Subject
        # (T7) remain the per-tenant layer on top (unchanged, out of AA-545's scope).
        try:
            await recompute_segment_score_route(tour_id, pool, log_tour_id=tour_id)
        except Exception:
            # Best-effort, same precedent as every other step in this function — a Segment/Score/
            # Route failure must never be mistaken for atomize (already logged done above) or the
            # publish itself having failed.
            logger.warning("a3_segment_score_route_failed", tour_id=tour_id, exc_info=True)
    except Exception as exc:
        # Best-effort, same precedent as this file's own ACP-S1 manifest fanout (process_export()
        # below) — atomize failing must never be mistaken for the publish itself having failed;
        # A3 (gold_aa_internal.published_tours + pipeline_status='published') is already committed
        # by the time this task is launched.
        logger.error("a3_atomize_failed", tour_id=tour_id, error=str(exc))
    finally:
        await conn.close()

# AA-476: terminal raw_tours.pipeline_status values that mean "this tour will never publish,
# stop waiting on it" — anything else is still in flight. Before this fix the completion check
# only recognized 'published', so a rejected/failed tour (which never got any pipeline_status
# update at all — see mark_tour_rejected below) kept its batch's pipeline_runs.status stuck at
# 'ingesting' forever even after every other tour in the batch finished.
_TERMINAL_TOUR_STATUSES = ("published", "hitl_rejected", "failed")


async def sync_batch_completion(conn, batch_id, silver: str = "silver_aa_internal") -> tuple[int, bool]:
    """Recompute tours_passed + flip pipeline_runs.status to 'completed' once every tour in
    the batch has reached a terminal outcome. Returns (pending_count, just_completed).
    Shared by process_export() (tour → published) and mark_tour_rejected() (tour → rejected) —
    this is the ONE place pipeline_runs.status ever advances, deliberately not duplicated.

    AA-483: the status flip used to be a separate SELECT COUNT(*) (this function) followed by a
    conditional UPDATE gated on that count in Python — two round-trips with a gap between them,
    no lock. Now a SINGLE atomic UPDATE ... WHERE NOT EXISTS(...) does the check-and-flip in one
    statement: Postgres evaluates the WHERE clause (including the NOT EXISTS subquery) and
    performs the UPDATE under one MVCC snapshot with the target row locked, so two concurrent
    callers for the same batch can no longer both read "still pending" moments before the other
    commits the tour that would have made it complete — whichever call's UPDATE actually runs
    second re-evaluates WHERE against the first one's already-committed result and correctly
    no-ops. `just_completed` (True only for whichever single call's UPDATE actually matched a
    row) is now the sole trigger for process_export()'s one-time ACP-S1 manifest/EventBridge
    fanout — using the old `pending == 0` read for that decision had the identical race (two
    concurrent calls could each independently observe pending == 0 and both fire the fanout);
    `pending` itself is kept only as an informational/logging count, no longer a completion
    signal for any caller."""
    await conn.execute("""
        UPDATE shared.pipeline_runs
        SET tours_passed = (
            SELECT COUNT(*) FROM silver_aa_internal.raw_tours
            WHERE batch_id = $1::uuid AND pipeline_status = 'published'
        )
        WHERE batch_id = $1::uuid
    """, batch_id)

    pending = await conn.fetchval(f"""
        SELECT COUNT(*) FROM {silver}.raw_tours
        WHERE batch_id = $1::uuid
          AND pipeline_status NOT IN {_TERMINAL_TOUR_STATUSES}
    """, batch_id)

    flipped = await conn.fetchval(f"""
        UPDATE shared.pipeline_runs
        SET status = 'completed', completed_at = NOW()
        WHERE batch_id = $1::uuid
          AND status = 'ingesting'
          AND NOT EXISTS (
              SELECT 1 FROM {silver}.raw_tours
              WHERE batch_id = $1::uuid
                AND pipeline_status NOT IN {_TERMINAL_TOUR_STATUSES}
          )
        RETURNING 1
    """, batch_id)
    just_completed = flipped is not None

    if just_completed:
        logger.info("batch_completed", batch_id=str(batch_id))

    return pending, just_completed


async def mark_tour_rejected(conn, tour_id: str) -> None:
    """AA-476: reject_review() (api/routers/v1_pipeline.py) used to only flip
    review_queue.review_status + generated_content.status — raw_tours.pipeline_status was
    never touched, so a rejected tour stayed 'ingested' indefinitely and sync_batch_completion
    counted it as still-pending forever, even once every other tour in the batch was done."""
    row = await conn.fetchrow("""
        UPDATE silver_aa_internal.raw_tours
        SET pipeline_status = 'hitl_rejected'
        WHERE tour_id = $1::uuid
        RETURNING batch_id
    """, tour_id)
    if row and row["batch_id"]:
        await sync_batch_completion(conn, row["batch_id"])


async def process_export(version_id: str) -> dict:
    conn = await asyncpg.connect(get_database_url())
    tenant_slug = os.environ.get("TENANT_SLUG", "aa_internal")
    silver = f"silver_{tenant_slug}"
    try:
        # 1. Fetch generated content + tour info
        row = await conn.fetchrow(f"""
            SELECT gc.*, rt.country, rt.duration, rt.batch_id,
                   qs.id            AS quality_score_id,
                   qs.score_overall AS quality_score
            FROM {silver}.generated_content gc
            JOIN {silver}.raw_tours rt ON rt.tour_id = gc.tour_id
            LEFT JOIN {silver}.quality_scores qs ON qs.generated_content_id = gc.id
            WHERE gc.id = $1::uuid
              AND gc.status = 'approved'
        """, version_id)

        if not row:
            raise ValueError(f"Version not approved or not found: {version_id}")

        row = dict(row)
        batch_id = row["batch_id"]
        tour_id = row["tour_id"]

        # 2. Insert into published catalog (gold)
        repo = PublishedCatalogRepository(conn, tenant_slug)
        catalog_id = await repo.insert({
            "tour_id":              tour_id,
            "generated_content_id": row["id"],
            "tenant_id":            row["tenant_id"],
            "aa_name":              row.get("aa_name"),
            "aa_subtitle":          row.get("aa_subtitle"),
            "aa_summary":           row.get("aa_summary"),
            "aa_description":       row.get("aa_description"),
            # AA-314: gc.aa_highlights/seo_keywords_used/og_tags come back from asyncpg as
            # already-JSON-encoded str (no jsonb codec registered anywhere in this app — see
            # AA-293/AA-314 audit). json.dumps()'ing them again here double-encoded all three
            # columns for every export (47/48 published_tours rows, confirmed live). Pass the
            # existing JSON string straight through — PublishedCatalogRepository.insert() does
            # not re-serialize either, it hands the value to asyncpg's default jsonb codec as-is.
            "aa_highlights":        row.get("aa_highlights") or "[]",
            "aa_itineraries":       row.get("aa_itineraries"),
            "mobile_card_text":     row.get("mobile_card_text"),
            "seo_title":            row.get("seo_title"),
            "seo_meta":             row.get("seo_meta"),
            "seo_keywords_used":    row.get("seo_keywords_used") or "[]",
            "og_tags":              row.get("og_tags") or "{}",
            "quality_score":        row.get("quality_score"),
            "quality_score_id": (
                str(row["quality_score_id"]) if row.get("quality_score_id") else None
            ),
            "s3_gold_path":         None,
            "approved_by":          "pipeline",
        })
        logger.info("export_done", catalog_id=catalog_id, version_id=version_id)

        # 3. Mark tour as exported
        await conn.execute(f"""
            UPDATE {silver}.raw_tours
            SET pipeline_status = 'published'
            WHERE tour_id = $1::uuid
        """, tour_id)

        # 3b. AA-526 — this tour has now genuinely entered A3 (Master Content Pool, real QA
        # already passed via the gate at the top of this function, gc.status = 'approved') — the
        # correct, deliberate trigger point for atomize per the 04/09/2026 architecture decision
        # (was previously tied to tenant-rewritten-tour content, api/routers/v1_tours.py's now-
        # removed atomize_version() endpoint; see docs/implementation-notes/AA-526.md). Launched
        # fire-and-forget (own connection, own lifecycle — see _run_a3_atomize_background()'s own
        # docstring for why) so a slow multi-day atomize run never adds latency to this function's
        # own caller (an admin approve/publish action, awaited synchronously).
        _atomize_task = asyncio.create_task(_run_a3_atomize_background(
            tour_id=str(tour_id),
            rewritten={
                "name": row.get("aa_name"), "summary": row.get("aa_summary"),
                "highlights": row.get("aa_highlights"), "itineraries": row.get("aa_itineraries"),
            },
            country=row.get("country") or "",
            version_id=str(row["id"]),  # generated_content.id — this tour's real content version
        ))
        _background_tasks.add(_atomize_task)
        _atomize_task.add_done_callback(_background_tasks.discard)

        # 4. Update tours_passed to exact published count (always, not just at end)
        # AA-492: this used to also gate a one-time "ACP-S1 manifest.json + EventBridge"
        # fanout on just_completed (AA-483's own atomic-race fix). That fanout was removed
        # entirely — STEP0 confirmed via real ECS-exec DB check that both tables it touched
        # (acp_shared.acp_run_context, acp_shared.acp_runs) were already dropped by migration
        # 121 (AA-477), so it had crashed on every real invocation since, silently swallowed by
        # its own try/except, 0 rows ever durably written to shared.acp_runs either. The
        # EventBridge bus it published to (aa-cis-dev-acp-events) had 0 rules/0 targets for its
        # entire lifetime — see docs/implementation-notes/AA-492.md for the full trace.
        if batch_id:
            await sync_batch_completion(conn, batch_id, silver)

        return {
            "status":     "exported",
            "catalog_id": catalog_id,
            "version_id": version_id,
        }
    finally:
        await conn.close()


def lambda_handler(event: dict, context) -> dict:
    # Pattern 1: SF direct invoke — version_id inside validation_result.Payload
    if "validation_result" in event:
        payload = event["validation_result"].get("Payload", {})
        version_id = payload.get("version_id")
        if not version_id:
            logger.warning("no_version_id_in_validation_result", keys=str(event.keys()))
            return {"status": "failed", "error": "missing version_id"}
        try:
            result = asyncio.run(process_export(version_id))
            return result
        except Exception as e:
            logger.error("export_failed", error=str(e))
            return {"status": "failed", "error": str(e)}

    # Pattern 2: SQS trigger (Phase 2)
    elif "Records" in event:
        results = []
        for record in event["Records"]:
            try:
                body = json.loads(record["body"])
                version_id = body.get("version_id")
                if not version_id:
                    logger.warning("missing_version_id")
                    continue
                result = asyncio.run(process_export(version_id))
                results.append(result)
            except Exception as e:
                logger.error("export_failed", error=str(e))
                results.append({"status": "failed", "error": str(e)})
        return {"processed": len(results), "results": results}

    else:
        logger.warning("unknown_event_format", keys=str(event.keys()))
        return {"status": "failed", "error": "unknown event format"}
