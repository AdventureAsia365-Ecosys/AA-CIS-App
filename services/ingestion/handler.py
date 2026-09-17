import boto3
import asyncio
import asyncpg
import json
import os
import tempfile
import uuid
import hashlib
import structlog
from urllib.parse import unquote_plus

from .excel_parser import ExcelParser
from shared.repository.raw_tour_repository import RawTourRepository
from shared.repository.raw_source_repository import RawSourceRepository
from shared.secrets import get_database_url

logger = structlog.get_logger()

_AWS_REGION = os.environ.get("AWS_REGION", "us-west-1")
_s3_client = None
_sfn_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=_AWS_REGION)
    return _s3_client


def _sfn():
    global _sfn_client
    if _sfn_client is None:
        _sfn_client = boto3.client("stepfunctions", region_name=_AWS_REGION)
    return _sfn_client


def compute_file_hash(file_bytes: bytes) -> str:
    """SHA256 hash of raw file — dedup key."""
    return hashlib.sha256(file_bytes).hexdigest()


def normalize_group_key(src_name: str, provider: str | None) -> tuple[str, str]:
    """Return (normalized_name, normalized_provider) for dedup comparison."""
    return (src_name.lower().strip(), (provider or "").lower().strip())


def _summarize_drops(insert_failures: list[dict]) -> list[dict]:
    """Group insert_batch failures by reason for pipeline_runs.ingest_details.drops.
    sample_ids capped at 5/group — enough to trace back to the source file without
    bloating the JSONB column for a large bad batch."""
    groups: dict[str, dict] = {}
    for f in insert_failures:
        g = groups.setdefault(f["reason"], {"reason": f["reason"], "count": 0, "sample_ids": []})
        g["count"] += 1
        if len(g["sample_ids"]) < 5:
            g["sample_ids"].append(f["identifier"])
    return list(groups.values())


async def _write_ingest_details(conn, batch_id: str, rows_parsed: int, rows_landed: int, drops: list[dict]):
    """Ingest-level landed/dropped diagnostics — deliberately NOT written into
    tours_passed/tours_failed (those carry different, already-live semantics downstream:
    tours_passed is recomputed to published-count on export, tours_failed is incremented for
    S1 quality failures — see migration 091)."""
    details = {
        "rows_parsed":  rows_parsed,
        "rows_landed":  rows_landed,
        "rows_dropped": sum(d["count"] for d in drops),
        "drops":        drops,
    }
    await conn.execute("""
        UPDATE shared.pipeline_runs
        SET ingest_details = $2::jsonb
        WHERE batch_id = $1::uuid
    """, batch_id, json.dumps(details, default=str))


async def process_file(s3_bucket: str, s3_key: str, seo_mode: str = "standard") -> dict:
    """Download Excel từ S3, parse, insert bronze.raw_sources → bronze.raw_tours."""

    # Download về /tmp
    filename = s3_key.split("/")[-1]
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        _s3().download_fileobj(s3_bucket, s3_key, tmp)
        tmp_path = tmp.name

    # Read file bytes for hash computation
    with open(tmp_path, "rb") as fh:
        file_bytes = fh.read()
    file_hash = compute_file_hash(file_bytes)
    logger.info("file_downloaded", s3_key=s3_key, file_hash=file_hash[:12], seo_mode=seo_mode)

    # TD-2: Dedup check — skip if same file already processed
    conn_check = await asyncpg.connect(get_database_url())
    try:
        existing = await conn_check.fetchrow(
            "SELECT id, filename FROM silver_aa_internal.raw_sources WHERE file_hash = $1",
            file_hash,
        )
        if existing:
            logger.info(
                "dedup_skip",
                file_hash=file_hash[:12],
                existing_id=str(existing["id"]),
                existing_filename=existing["filename"],
            )
            return {
                "status": "skipped_duplicate",
                "file_hash": file_hash[:12],
                "existing_source_id": str(existing["id"]),
            }
    finally:
        await conn_check.close()

    # Parse Excel
    parser = ExcelParser(tmp_path, source_file=s3_key)
    records = parser.parse()
    logger.info("excel_parsed", total_rows=len(records))

    conn = await asyncpg.connect(get_database_url())
    tenant_slug = "aa_internal"  # Phase 1: single tenant
    tenant_uuid = "00000000-0000-0000-0000-000000000001"

    source_repo = RawSourceRepository(conn, tenant_slug)
    tour_repo   = RawTourRepository(conn, tenant_uuid)

    try:
        # 1. Insert raw_source → lấy source_id
        batch_id_new = str(uuid.uuid4())

        # Insert pipeline_runs first (raw_sources.batch_id FK → pipeline_runs)
        await conn.execute("""
            INSERT INTO shared.pipeline_runs
                (batch_id, tenant_id, s3_source_path, status, tours_total)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5)
            ON CONFLICT (batch_id) DO NOTHING
        """,
            batch_id_new,
            "00000000-0000-0000-0000-000000000001",
            s3_key,
            "ingesting",
            len(records),
        )

        # Get file size from S3 object
        try:
            s3_meta = _s3().head_object(Bucket=s3_bucket, Key=s3_key)
            file_size_kb = round(s3_meta["ContentLength"] / 1024, 1)
        except Exception:
            file_size_kb = None

        source_id = await source_repo.insert({
            "tenant_id":    "00000000-0000-0000-0000-000000000001",
            "batch_id":     batch_id_new,
            "filename":     filename,
            "s3_path":      s3_key,
            "row_count":    len(records),
            "file_hash":    file_hash,
            "file_size_kb": file_size_kb,
        })
        logger.info("source_created", source_id=source_id)

        if not records:
            await source_repo.update_status(source_id, "skipped", row_count=0)
            await _write_ingest_details(conn, batch_id_new, rows_parsed=0, rows_landed=0, drops=[])
            return {"status": "skipped", "rows": 0, "source_id": str(source_id)}

        # 2. Split records: new vs duplicate (normalized dedup against raw_tours)
        new_records: list[dict] = []
        staged_ids: list[str]  = []
        in_file_drops: list[dict] = []
        # AA-488 Gap 1: seen_keys tracks (src_name, provider) already assigned to new_records
        # THIS run — without it, two rows in the same file with an identical key both miss the
        # DB-existence query below (neither is inserted yet when the second row is checked) and
        # both land in new_records, creating duplicate raw_tours rows from one upload.
        seen_keys: set[tuple[str, str]] = set()

        for r in records:
            r["source_id"] = source_id
            r["batch_id"] = batch_id_new
            nname, nprov = normalize_group_key(r.get("src_name", ""), r.get("provider"))

            if (nname, nprov) in seen_keys:
                in_file_drops.append({
                    "identifier": r.get("src_name") or "unknown",
                    "reason": "duplicate_in_file",
                })
                continue

            # AA-604: skip rows with no itinerary body — they cannot be rewritten by S1
            # (S1 get_all_tours + acp_contract.v_trip_registry require a non-empty
            # itinerary), so committing them as 'ingested' only made S0 over-count vs S1
            # (793 vs 763). Mirrors the dry_run preview's empty_itinerary block so preview
            # and commit report the identical outcome. Covers POI/attraction rows and
            # source files lacking itinerary content. Recorded in ingest_details, not a
            # hard failure.
            if not (r.get("src_itineraries") or "").strip():
                in_file_drops.append({
                    "identifier": r.get("src_name") or "unknown",
                    "reason": "empty_itinerary",
                })
                continue

            existing = await conn.fetchrow("""
                SELECT tour_id, source_group_id
                FROM silver_aa_internal.raw_tours
                WHERE tenant_id = $1
                  AND lower(trim(src_name)) = $2
                  AND lower(trim(coalesce(provider, ''))) = $3
                ORDER BY ingest_at DESC LIMIT 1
            """, tenant_uuid, nname, nprov)

            if existing:
                staging_id = await conn.fetchval("""
                    INSERT INTO silver_aa_internal.upload_staging
                        (batch_id, tenant_id, parsed_payload,
                         matched_tour_id, matched_source_group_id, decision)
                    VALUES ($1::uuid, $2::uuid, $3::jsonb, $4::uuid, $5, 'pending')
                    RETURNING id::text
                """,
                    batch_id_new,
                    tenant_uuid,
                    json.dumps(r, default=str),
                    str(existing["tour_id"]),
                    str(existing["source_group_id"]) if existing["source_group_id"] else None,
                )
                staged_ids.append(staging_id)
            else:
                seen_keys.add((nname, nprov))
                r["source_group_id"] = str(uuid.uuid4())
                r["source_version"]  = 1
                r["source_status"]   = "active"
                new_records.append(r)

        ids, insert_failures = await tour_repo.insert_batch(new_records)
        await source_repo.update_status(source_id, "done", row_count=len(ids) + len(staged_ids))

        drops = _summarize_drops(insert_failures + in_file_drops)
        if drops:
            logger.warning("ingest_rows_dropped", batch_id=batch_id_new, source_file=s3_key,
                            rows_dropped=len(insert_failures) + len(in_file_drops), drops=drops)
        await _write_ingest_details(conn, batch_id_new,
                                     rows_parsed=len(records), rows_landed=len(ids), drops=drops)

        logger.info("inserted", count=len(ids), staged=len(staged_ids), source_id=source_id)

        batch_id = batch_id_new

        return {
            "status":        "done",
            "rows":          len(ids),
            "tours_written": len(ids),
            "tours_staged":  len(staged_ids),
            "staged_ids":    staged_ids,
            "source_id":     batch_id,
            "rows_dropped":  len(insert_failures) + len(in_file_drops),
        }

    except Exception as e:
        if "source_id" in dir():
            await source_repo.update_status(source_id, "failed", error=str(e))
        logger.error("processing_failed", error=str(e))
        if "batch_id_new" in dir():
            try:
                await conn.execute("""
                    UPDATE shared.pipeline_runs
                    SET status = 'ingest_failed',
                        completed_at = NOW(),
                        error_message = $2
                    WHERE batch_id = $1::uuid
                """, batch_id_new, str(e)[:2000])
            except Exception as mark_err:
                logger.error("pipeline_runs_fail_mark_failed", batch_id=batch_id_new, error=str(mark_err))
        raise

    finally:
        await conn.close()


def _extract_supplier(s3_key: str) -> str | None:
    """Extract supplier name từ path: raw-inbox/SupplierName/file.xlsx."""
    parts = s3_key.split("/")
    return parts[1] if len(parts) >= 3 else None


# UNUSED as of AA-311 (20/07/2026) — retired call in process_file(), see AA-182 for SM bypass context.
# Kept for reference, do not re-wire without re-reading AA-182 decision.
def _start_pipeline(
    sfn_arn: str,
    batch_id: str,
    tour_ids: list[str],
    tenant_id: str,
    s3_key: str,
    seo_mode: str = "standard",
) -> None:
    """
    Start Step Functions execution with per-tour input.

    Step Functions Map state expects $.tours = list of tour objects.
    Each tour object: {tour_id, batch_id, tenant_id, retry_count, validation_feedback}
    """
    tours_input = [
        {
            "tour_id":             tid,
            "batch_id":            batch_id,
            "tenant_id":           tenant_id,
            "retry_count":         0,
            "validation_feedback": [],
            "seo_mode":            seo_mode,
        }
        for tid in tour_ids
    ]

    execution_name = f"batch-{batch_id[:8]}-{uuid.uuid4().hex[:8]}"

    payload = {
        "batch_id":  batch_id,
        "s3_key":    s3_key,
        "tenant_id": tenant_id,
        "seo_mode":  seo_mode,
        "tours":     tours_input,
    }

    response = _sfn().start_execution(
        stateMachineArn=sfn_arn,
        name=execution_name,
        input=json.dumps(payload),
    )

    logger.info(
        "sfn_started",
        execution_arn=response["executionArn"],
        batch_id=batch_id,
        tour_count=len(tour_ids),
    )


def lambda_handler(event: dict, context) -> dict:
    """AWS Lambda entry point — triggered by S3 PutObject on Bronze bucket raw-inbox/."""
    records = event.get("Records", [])
    results = []

    for record in records:
        s3_bucket = record["s3"]["bucket"]["name"]
        s3_key    = unquote_plus(record["s3"]["object"]["key"])

        if not s3_key.startswith("raw-inbox/"):
            logger.info("skipped_non_inbox", s3_key=s3_key)
            continue

        if not s3_key.endswith((".xlsx", ".xls")):
            logger.info("skipped_non_excel", s3_key=s3_key)
            continue

        logger.info("processing", s3_bucket=s3_bucket, s3_key=s3_key)

        try:
            # Read seo_mode from S3 object metadata (set by upload-url endpoint)
            seo_mode = "standard"
            try:
                meta = _s3().head_object(Bucket=s3_bucket, Key=s3_key)
                seo_mode = meta.get("Metadata", {}).get("seo-mode", "standard")
            except Exception:
                pass
            result = asyncio.run(process_file(s3_bucket, s3_key, seo_mode=seo_mode))
            results.append({"s3_key": s3_key, **result})
        except Exception as e:
            logger.error("lambda_record_failed", s3_key=s3_key, error=str(e))
            results.append({"s3_key": s3_key, "status": "failed", "error": str(e)})

    return {"processed": len(results), "results": results}
