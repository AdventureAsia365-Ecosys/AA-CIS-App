"""AA-1003 R1 — reads and writes of shared.kpi_daily and shared.report_run (CIS 305).

Every function takes an asyncpg pool or connection (anything with fetch / fetchrow / fetchval / execute /
executemany). Rows are never deleted here (data standards Part B §7: kept).
"""
from __future__ import annotations

import json
from datetime import date
from typing import Iterable, Optional

from services.reports.kpis import KpiValue

# ── shared.kpi_daily ────────────────────────────────────────────────────────────────────────────────────────────

UPSERT_KPI_SQL = """
    INSERT INTO shared.kpi_daily (kpi_id, day, scope, kpi_version, value, unit, source, data_as_of, computed_at, job_id)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now(), $9::uuid)
    ON CONFLICT (kpi_id, day, scope) DO UPDATE
       SET kpi_version = EXCLUDED.kpi_version, value = EXCLUDED.value, unit = EXCLUDED.unit,
           source = EXCLUDED.source, data_as_of = EXCLUDED.data_as_of, computed_at = now(),
           job_id = EXCLUDED.job_id
"""

KPI_SERIES_SQL = """
    SELECT kpi_id, day, scope, value, unit, source, data_as_of, computed_at
      FROM shared.kpi_daily
     WHERE kpi_id = ANY($1::text[]) AND day >= $2 AND day <= $3
     ORDER BY kpi_id, scope, day
"""

LATEST_KPI_DAY_SQL = "SELECT max(day) FROM shared.kpi_daily"


async def upsert_kpis(conn, day: date, values: Iterable[KpiValue], *, units: dict[str, str],
                      versions: dict[str, int], job_id: Optional[str]) -> int:
    rows = [(v.kpi_id, day, v.scope, versions.get(v.kpi_id, 1), v.value, units[v.kpi_id], v.source, v.data_as_of,
             job_id) for v in values]
    if rows:
        await conn.executemany(UPSERT_KPI_SQL, rows)
    return len(rows)


async def kpi_series(conn, kpi_ids: list[str], first: date, last: date) -> list[dict]:
    rows = await conn.fetch(KPI_SERIES_SQL, kpi_ids, first, last)
    return [dict(r) for r in rows]


async def latest_kpi_day(conn) -> Optional[date]:
    return await conn.fetchval(LATEST_KPI_DAY_SQL)


# ── shared.report_run ───────────────────────────────────────────────────────────────────────────────────────────

RUN_COLUMNS = ("id, report_id, period_start, period_end, status, trigger, triggered_by, job_id, started_at, "
               "finished_at, summary, s3_key, error, approved_by, approved_at, created_at, updated_at")

CREATE_RUN_SQL = """
    INSERT INTO shared.report_run (report_id, period_start, period_end, status, trigger, triggered_by)
    VALUES ($1, $2, $3, 'queued', $4, $5)
    ON CONFLICT (report_id, period_start, period_end) WHERE trigger = 'schedule' DO NOTHING
    RETURNING id
"""

SET_JOB_SQL = "UPDATE shared.report_run SET job_id = $2::uuid, updated_at = now() WHERE id = $1::uuid"

GET_RUN_SQL = f"SELECT {RUN_COLUMNS}, output, output_markdown FROM shared.report_run WHERE id = $1::uuid"

MARK_RUNNING_SQL = """
    UPDATE shared.report_run
       SET status = 'running', job_id = coalesce(job_id, $2::uuid), started_at = coalesce(started_at, now()),
           error = NULL, finished_at = NULL, updated_at = now()
     WHERE id = $1::uuid
"""

FINISH_OK_SQL = """
    UPDATE shared.report_run
       SET status = 'succeeded', summary = $2, output = $3::jsonb, output_markdown = $4, error = NULL,
           finished_at = now(), updated_at = now()
     WHERE id = $1::uuid
"""

FINISH_FAILED_SQL = """
    UPDATE shared.report_run
       SET status = 'failed', error = left($2, 4000), finished_at = now(), updated_at = now()
     WHERE id = $1::uuid AND status IN ('queued', 'running', 'failed')
"""

LIST_RUNS_SQL = f"""
    SELECT {RUN_COLUMNS} FROM shared.report_run
     WHERE report_id = $1
     ORDER BY created_at DESC, id
     LIMIT $2 OFFSET $3
"""

COUNT_RUNS_SQL = "SELECT count(*) FROM shared.report_run WHERE report_id = $1"

# The latest run of each report, the latest succeeded one (its period says what is covered) and failures in 7 days.
LAST_RUNS_SQL = f"""
    WITH latest AS (
        SELECT DISTINCT ON (report_id) {RUN_COLUMNS} FROM shared.report_run
         ORDER BY report_id, created_at DESC, id
    ), ok AS (
        SELECT report_id, max(period_end) AS covered_to, max(finished_at) AS last_succeeded_at
          FROM shared.report_run WHERE status IN ('succeeded', 'approved', 'posted')
         GROUP BY report_id
    ), failed AS (
        SELECT report_id, count(*) AS failed_7d FROM shared.report_run
         WHERE status = 'failed' AND finished_at >= now() - interval '7 days'
         GROUP BY report_id
    )
    SELECT l.*, ok.covered_to, ok.last_succeeded_at, coalesce(f.failed_7d, 0) AS failed_7d
      FROM latest l
      LEFT JOIN ok ON ok.report_id = l.report_id
      LEFT JOIN failed f ON f.report_id = l.report_id
"""


async def create_run(conn, report_id: str, first: date, last: date, *, trigger: str,
                     triggered_by: str) -> Optional[str]:
    """New queued run; None when a scheduled run of the same report and period exists already."""
    row = await conn.fetchrow(CREATE_RUN_SQL, report_id, first, last, trigger, triggered_by)
    return str(row["id"]) if row else None


async def set_job(conn, run_id: str, job_id: str) -> None:
    await conn.execute(SET_JOB_SQL, run_id, job_id)


async def get_run(conn, run_id: str) -> Optional[dict]:
    row = await conn.fetchrow(GET_RUN_SQL, run_id)
    return dict(row) if row else None


async def mark_running(conn, run_id: str, job_id: str) -> None:
    await conn.execute(MARK_RUNNING_SQL, run_id, job_id)


async def finish_ok(conn, run_id: str, *, summary: str, output: dict, markdown: str) -> None:
    await conn.execute(FINISH_OK_SQL, run_id, summary[:1000], json.dumps(output, default=str), markdown)


async def finish_failed(conn, run_id: str, error: str) -> None:
    await conn.execute(FINISH_FAILED_SQL, run_id, error or "failed")


async def list_runs(conn, report_id: str, *, limit: int, offset: int) -> tuple[list[dict], int]:
    rows = await conn.fetch(LIST_RUNS_SQL, report_id, limit, offset)
    total = await conn.fetchval(COUNT_RUNS_SQL, report_id)
    return [dict(r) for r in rows], int(total or 0)


async def last_runs(conn) -> dict[str, dict]:
    return {r["report_id"]: dict(r) for r in await conn.fetch(LAST_RUNS_SQL)}
