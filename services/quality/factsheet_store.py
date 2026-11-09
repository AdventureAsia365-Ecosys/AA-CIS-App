"""AA-1047 (Quality W1, root ADR 0020) — persistence of the FactSheet (acp_shared.fact_sheet, CIS 386) and the fs_id
stamps on the objects written from it.

* ``save(conn, sheet, tenant_id=, workflow=)`` stores a sheet once per (tenant, workflow, fs_sha) and returns its
  fs_id; the same inputs give the same sha, so a retry, a resumed job or a second piece on the same seed reuses the
  row (INSERT … ON CONFLICT DO NOTHING, then a SELECT: cis_app holds no UPDATE on the table).
* ``stamp_generated_content`` / ``stamp_tenant_version`` set ``fs_id`` on an S1 Master version / a T2 version.
  A T9 piece carries its fs_id in ``content_piece.writer_input`` (``service.writer_input_snapshot``).

Best-effort, like the gateway cassette (CIS 385): a FactSheet record must never fail or slow a write. Every call
runs in its own savepoint (``conn.transaction()`` nests), logs and returns None on any error; a missing table or
column (the code deployed before CIS 386 was applied) is remembered for ``_SKIP_SECONDS`` so the writers stop trying.
``FACTSHEET_PERSIST=off`` turns every write off (default on).
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

import structlog

from services.quality.factsheet import (BUILDER_VERSION, SCHEMA, FactSheet, build_tour_factsheet,
                                        raw_source_tour)

logger = structlog.get_logger()

WORKFLOWS = ("s1", "t2", "t9")
_SKIP_SECONDS = 300.0
_skip_until = 0.0

_INSERT = """
    INSERT INTO acp_shared.fact_sheet
        (tenant_id, workflow, builder, schema_version, builder_version, fs_sha, tour_id, line_count, excluded_count,
         pointer_failure_count, lines, excluded, pointer_failures, meta, render_text)
    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7::uuid, $8, $9, $10, $11::jsonb, $12::jsonb, $13::jsonb, $14::jsonb, $15)
    ON CONFLICT (tenant_id, workflow, fs_sha) DO NOTHING
    RETURNING fs_id
"""
_FIND = """
    SELECT fs_id FROM acp_shared.fact_sheet WHERE tenant_id = $1::uuid AND workflow = $2 AND fs_sha = $3
"""


def enabled() -> bool:
    return os.environ.get("FACTSHEET_PERSIST", "on").strip().lower() not in ("off", "0", "false", "no")


def _missing_schema(exc: BaseException) -> bool:
    return type(exc).__name__ in ("UndefinedTableError", "UndefinedColumnError")


def _skipping() -> bool:
    return time.monotonic() < _skip_until


def _remember_failure(exc: BaseException, what: str, **log) -> None:
    global _skip_until
    if _missing_schema(exc):
        _skip_until = time.monotonic() + _SKIP_SECONDS
    logger.warning("factsheet_store_failed", what=what, error_type=type(exc).__name__, error=str(exc)[:300], **log)


def row_args(sheet: FactSheet, *, tenant_id: str, workflow: str, tour_id: Optional[str] = None) -> tuple:
    """The INSERT arguments of one sheet (pure; the unit test pins the column order)."""
    if workflow not in WORKFLOWS:
        raise ValueError(f"unknown workflow {workflow!r}")
    rec = sheet.record()
    dumps = lambda v: json.dumps(v, ensure_ascii=False, default=str)  # noqa: E731
    return (str(tenant_id), workflow, sheet.builder, SCHEMA, BUILDER_VERSION, sheet.sha(),
            str(tour_id) if tour_id else None, len(rec["lines"]), len(rec["excluded"]), len(rec["pointer_failures"]),
            dumps(rec["lines"]), dumps(rec["excluded"]), dumps(rec["pointer_failures"]), dumps(rec["meta"]),
            sheet.render())


async def save(conn, sheet: Optional[FactSheet], *, tenant_id, workflow: str,
               tour_id: Optional[str] = None) -> Optional[str]:
    """fs_id (str) of the stored sheet, or None (switched off, no sheet, no tenant, or the write failed)."""
    if sheet is None or not tenant_id or not enabled() or _skipping():
        return None
    try:
        args = row_args(sheet, tenant_id=str(tenant_id), workflow=workflow, tour_id=tour_id)
        async with conn.transaction():
            fs_id = await conn.fetchval(_INSERT, *args)
            if fs_id is None:
                fs_id = await conn.fetchval(_FIND, args[0], workflow, args[5])
        return str(fs_id) if fs_id is not None else None
    except Exception as exc:  # noqa: BLE001 — best-effort: the write it describes goes on
        _remember_failure(exc, "save", workflow=workflow, tenant_id=str(tenant_id))
        return None


async def save_tour_sheet(conn, row, *, tenant_id, workflow: str, tour_id=None,
                          meta: Optional[dict] = None) -> Optional[str]:
    """Build the tour FactSheet of a row carrying the raw_tours source columns (the S1 run_tour row, the T2 _PT_SQL
    row) and store it. None when switched off, when the row has no raw source (a T2 Master whose raw row is gone) or
    on any error."""
    if not enabled() or _skipping() or row is None:
        return None
    try:
        tour, agency = raw_source_tour(row)
        if not (tour.get("name") or "").strip():
            return None
        sheet = build_tour_factsheet(tour, agency=agency, meta=meta)
    except Exception as exc:  # noqa: BLE001
        _remember_failure(exc, "build_tour_sheet", workflow=workflow)
        return None
    return await save(conn, sheet, tenant_id=tenant_id, workflow=workflow, tour_id=tour_id)


async def _stamp(conn, sql: str, fs_id: Optional[str], object_id, what: str) -> bool:
    if not fs_id or not object_id or not enabled() or _skipping():
        return False
    try:
        async with conn.transaction():
            await conn.execute(sql, fs_id, str(object_id))
        return True
    except Exception as exc:  # noqa: BLE001
        _remember_failure(exc, what, object_id=str(object_id))
        return False


async def stamp_generated_content(conn, version_id, fs_id: Optional[str]) -> bool:
    return await _stamp(conn, "UPDATE silver_aa_internal.generated_content SET fs_id = $1::uuid "
                              "WHERE id = $2::uuid AND fs_id IS DISTINCT FROM $1::uuid",
                        fs_id, version_id, "stamp_generated_content")


async def stamp_tenant_version(conn, version_id, fs_id: Optional[str]) -> bool:
    return await _stamp(conn, "UPDATE gold_aa_internal.tenant_tour_versions SET fs_id = $1::uuid "
                              "WHERE id = $2::uuid AND fs_id IS DISTINCT FROM $1::uuid",
                        fs_id, version_id, "stamp_tenant_version")


async def load(conn, fs_id) -> Optional[dict]:
    """The stored record of one sheet (admin / replay reads), or None."""
    row = await conn.fetchrow(
        "SELECT fs_id, tenant_id, workflow, builder, schema_version, builder_version, fs_sha, tour_id, lines, "
        "excluded, pointer_failures, meta, render_text, created_at FROM acp_shared.fact_sheet WHERE fs_id = $1::uuid",
        str(fs_id))
    if row is None:
        return None
    out = dict(row)
    for k in ("lines", "excluded", "pointer_failures", "meta"):
        if isinstance(out.get(k), str):
            out[k] = json.loads(out[k])
    return out


__all__ = ["WORKFLOWS", "enabled", "load", "row_args", "save", "save_tour_sheet", "stamp_generated_content",
           "stamp_tenant_version"]
