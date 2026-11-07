"""AA-1047 W1 — the DB side of the REPORT-mode final contract (``services/quality/final_contract.py``).

Where a record lives (admin-only, never a tenant read):

* S1  ``silver_aa_internal.generated_content.metadata.final_contract`` (the latest exit) and
  ``metadata.final_contract_log`` (one compact entry per exit: done / hitl / revalidation / human_edit / auto_publish
  / approve / promote ...).
* T2  ``gold_aa_internal.tenant_tour_versions.judge_breakdown.final_contract`` (written with the version by the job;
  an admin sentence apply replaces it).
* T9  ``acp_shared.content_piece.writer_input.final_contract`` (written with the piece by ``_finalize_piece``; an
  admin sentence apply replaces it).

``record_s1_exit`` reads the stored version through ``S1_ROW_SQL`` (the same read the report script uses), runs the
contract and appends the record. Every function here is best-effort: report mode never fails the exit it records,
so an error is logged (``final_contract_record_failed``) and swallowed.
"""
from __future__ import annotations

import datetime
import json
from typing import Any, Optional

import structlog

from services.quality import final_contract as fc

logger = structlog.get_logger()

LOG_MAX = 20   # compact exit entries kept per version

# One stored A1 version with what its contract reads: the page text, the latest score row (failure codes, brand
# audit), the metadata (grounding, degraded, writer_version) and the raw source. Table / column names from
# docs/architecture/db-schema-reference.md.
S1_ROW_COLUMNS = """
    gc.id::text AS generated_content_id, gc.tour_id::text AS tour_id, gc.status::text AS status, gc.version_num,
    gc.metadata, gc.fix_pass_applied, COALESCE(gc.human_edited, false) AS human_edited,
    gc.aa_name, gc.aa_subtitle, gc.aa_summary, gc.aa_description, gc.aa_highlights::text AS aa_highlights,
    gc.aa_itineraries, gc.seo_title, gc.seo_meta,
    qs.score_overall::float AS score_overall, qs.failure_codes, qs.brand_audit_status, qs.brand_audit_codes,
    rt.country, rt.duration, rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description,
    rt.src_highlights::text AS src_highlights, rt.src_itineraries, rt.inclusions,
    rt.activities::text AS activities, rt.price_raw
"""
S1_ROW_FROM = """
    FROM silver_aa_internal.generated_content gc
    LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id
    LEFT JOIN LATERAL (
        SELECT q.score_overall, q.failure_codes, q.brand_audit_status, q.brand_audit_codes
        FROM silver_aa_internal.quality_scores q
        WHERE q.generated_content_id = gc.id
        ORDER BY q.evaluated_at DESC, q.id DESC
        LIMIT 1
    ) qs ON true
"""
S1_ROW_SQL = f"SELECT {S1_ROW_COLUMNS} {S1_ROW_FROM} WHERE gc.id = $1::uuid"

_S1_APPEND_SQL = """
    UPDATE silver_aa_internal.generated_content
    SET metadata = jsonb_set(jsonb_set(COALESCE(metadata, '{}'::jsonb), '{final_contract}', $2::jsonb),
                             '{final_contract_log}', $3::jsonb)
    WHERE id = $1::uuid
"""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _json(value: Any, default):
    if value is None:
        return default
    if isinstance(value, type(default)):
        return value
    try:
        out = json.loads(value)
    except (TypeError, ValueError):
        return default
    return out if isinstance(out, type(default)) else default


def _log_of(metadata: Any) -> list:
    log = _json(metadata, {}).get("final_contract_log")
    return list(log) if isinstance(log, list) else []


def s1_record_from_row(row: Any, exit: str, *, actual: Optional[str] = None) -> dict:
    """The contract record of one ``S1_ROW_SQL`` row (pure; the report script runs this too)."""
    status = actual if actual is not None else fc._get(row, "status")
    return fc.check_s1(fc.s1_input_from_row(row), exit=exit, actual=status).record()


async def append_s1(conn, version_id: str, record: dict, metadata: Any = None) -> None:
    """Write ``record`` as the version's latest contract and append its compact form to the exit log."""
    if metadata is None:
        metadata = await conn.fetchval(
            "SELECT metadata FROM silver_aa_internal.generated_content WHERE id = $1::uuid", version_id)
    log = (_log_of(metadata) + [fc.compact(record, _now())])[-LOG_MAX:]
    await conn.execute(_S1_APPEND_SQL, version_id, json.dumps(record, default=str), json.dumps(log, default=str))


async def record_s1_exit(conn, version_id: str, exit: str, *, actual: Optional[str] = None) -> Optional[dict]:
    """Run the S1 contract on the stored version and record it (report mode: never raises)."""
    try:
        row = await conn.fetchrow(S1_ROW_SQL, str(version_id))
        if row is None:
            return None
        record = fc.stamp_stored(s1_record_from_row(row, exit, actual=actual),
                                 fc.tour_text_sha(fc.s1_input_from_row(row).content))
        await append_s1(conn, str(version_id), record, row["metadata"])
        fc.log_record(record, version_id=str(version_id))
        return record
    except Exception as exc:  # noqa: BLE001 — report mode never fails the exit it records
        logger.warning("final_contract_record_failed", workflow=fc.S1, exit=exit, version_id=str(version_id),
                       error_type=type(exc).__name__, error=str(exc)[:300])
        return None


async def attach_s1(conn, version_id: str, record: Optional[dict]) -> None:
    """Store a record the graph already computed (a re-validation) on its version. Never raises."""
    if not isinstance(record, dict):
        return
    try:
        await append_s1(conn, str(version_id), record)
        fc.log_record(record, version_id=str(version_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("final_contract_record_failed", workflow=fc.S1, exit=record.get("exit"),
                       version_id=str(version_id), error_type=type(exc).__name__, error=str(exc)[:300])


__all__ = ["S1_ROW_COLUMNS", "S1_ROW_FROM", "S1_ROW_SQL", "LOG_MAX", "s1_record_from_row", "append_s1",
           "record_s1_exit", "attach_s1"]
