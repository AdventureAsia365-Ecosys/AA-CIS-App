import asyncio
import asyncpg
import boto3
import json
import os
import structlog
from shared.secrets import get_database_url
from shared.validators.rules import validate_content

logger = structlog.get_logger()


def _validation_route(score: float, retry_count: int) -> tuple[str, str]:
    """(new_status, route) for a validated version. AA-926: pure seam so the pass line is pinned by
    the gate-outcome snapshot."""
    if score >= 7.0:
        return "approved", "export"
    if retry_count < 2:
        return "pending", "regenerate"
    return "pending", "hitl"


async def process_validation(version_id: str) -> dict:
    conn = await asyncpg.connect(get_database_url())
    tenant_slug = os.environ.get("TENANT_SLUG", "aa_internal")
    try:
        row = await conn.fetchrow(
            f"SELECT * FROM silver_{tenant_slug}.generated_content WHERE id = $1::uuid",
            version_id
        )
        if not row:
            raise ValueError(f"Version not found: {version_id}")

        content = {
            "name":       row["aa_name"],
            "subtitle":   row["aa_subtitle"],
            "summary":    row["aa_summary"],
            "highlights": row["aa_highlights"] or [],
            "seo_title":  row["seo_title"],
            "seo_meta":   row["seo_meta"],
            "trip_type":  "cultural",
        }

        result = validate_content(content)
        score  = result["score"]

        retry_count = row.get("retry_count", 0) or 0
        new_status, route = _validation_route(score, retry_count)

        await conn.execute(
            f"""UPDATE silver_{tenant_slug}.generated_content
                SET status = $2::content_status_enum WHERE id = $1::uuid""",
            version_id, new_status,
        )

        if route == "hitl":
            await conn.execute(f"""
                INSERT INTO silver_{tenant_slug}.review_queue (
                    tour_id, generated_content_id, tenant_id,
                    score_overall, failure_summary, review_status
                )
                SELECT gc.tour_id, gc.id, gc.tenant_id, $2, $3, 'pending'
                FROM silver_{tenant_slug}.generated_content gc
                WHERE gc.id = $1::uuid
                ON CONFLICT DO NOTHING
            """, version_id, float(score), json.dumps(result["issues"]))
            logger.info("hitl_queued", version_id=version_id, score=score)

        logger.info("validation_complete",
                    version_id=version_id, score=score, route=route)

        return {
            "version_id":   version_id,
            "score":        score,
            "route":        route,
            "failed_rules": result["issues"],
        }
    finally:
        await conn.close()


async def process_hitl_notify(
    tour_id: str,
    tenant_id: str,
    task_token: str,
    score: float,
    failed_rules: list,
) -> dict:
    """Store SF task_token in review_queue so UI can send_task_success later."""
    conn = await asyncpg.connect(get_database_url())
    tenant_slug = os.environ.get("TENANT_SLUG", "aa_internal")
    try:
        # Find latest generated_content for this tour
        gc_row = await conn.fetchrow(f"""
            SELECT id FROM silver_{tenant_slug}.generated_content
            WHERE tour_id = $1::uuid AND tenant_id = $2::uuid
            ORDER BY version_num DESC LIMIT 1
        """, tour_id, tenant_id)

        if not gc_row:
            raise ValueError(f"No generated_content for tour_id={tour_id}")

        generated_content_id = str(gc_row["id"])

        # Upsert review_queue with task_token
        await conn.execute(f"""
            INSERT INTO silver_{tenant_slug}.review_queue (
                tour_id, generated_content_id, tenant_id,
                score_overall, failure_summary,
                step_fn_task_token, review_status
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid,
                $4, $5, $6, 'pending'
            )
            ON CONFLICT (generated_content_id)
            DO UPDATE SET
                step_fn_task_token = EXCLUDED.step_fn_task_token,
                review_status = 'pending',
                reviewed_at = NULL
        """,
            tour_id, generated_content_id, tenant_id,
            float(score), json.dumps(failed_rules), task_token,
        )

        logger.info("hitl_notify_stored",
                    tour_id=tour_id, score=score,
                    token_len=len(task_token))
        return {"status": "hitl_queued", "tour_id": tour_id}
    finally:
        await conn.close()


def lambda_handler(event: dict, context) -> dict:
    # Pattern 0: HITL notify — store task_token (called by SF waitForTaskToken)
    if event.get("action") == "hitl_notify":
        try:
            result = asyncio.run(process_hitl_notify(
                tour_id=event["tour_id"],
                tenant_id=event["tenant_id"],
                task_token=event["task_token"],
                score=float(event.get("score", 0)),
                failed_rules=event.get("failed_rules", []),
            ))
            return result
        except Exception as e:
            logger.error("hitl_notify_failed", error=str(e))
            return {"status": "failed", "error": str(e)}

    # Pattern 1: SF direct invoke — version_id inside content_result.Payload
    elif "content_result" in event:
        payload    = event["content_result"].get("Payload", {})
        version_id = payload.get("version_id")
        if not version_id:
            logger.warning("no_version_id_in_content_result")
            return {"processed": 0, "error": "missing version_id"}
        try:
            return asyncio.run(process_validation(version_id))
        except Exception as e:
            logger.error("validation_failed", error=str(e))
            return {"status": "failed", "error": str(e)}

    # Pattern 2: SQS trigger
    elif "Records" in event:
        results = []
        for record in event["Records"]:
            try:
                body       = json.loads(record["body"])
                version_id = body.get("version_id")
                if not version_id:
                    continue
                results.append(asyncio.run(process_validation(version_id)))
            except Exception as e:
                logger.error("validation_failed", error=str(e))
                results.append({"status": "failed", "error": str(e)})
        return {"processed": len(results), "results": results}

    else:
        logger.warning("unknown_event_format", keys=str(event.keys()))
        return {"processed": 0, "error": "unknown event format"}
