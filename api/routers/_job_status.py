"""The job status of a tenant's tour versions (moved from api/routers/v1_tours.py, AA-1126 l16).

AA-859 MAIN exception: shared.job has no tenant column (tenant only in `payload`), is worker-hot, and aa_app_user has
no SELECT on it after 241 (plan D2). On the tenant pool the version rows come back with their job_id through RLS; the
statuses are read here on MAIN, after the tenant block, keyed only by those ids. Flag off keeps the pre-AA-859 LEFT
JOIN shared.job instead.

AA-1126 L1: with the job's tenant-facing stop code (``tenant_budget.QUOTA_STOP_CODE`` for a job held by its tenant's AI
budget, else NULL): a code only, never the job error text; a held version also says when the allowance resets.
"""
from __future__ import annotations

from shared.llm_client import tenant_budget

_JOB_STATUS_SQL = (f"SELECT id, status, {tenant_budget.job_stop_code_sql()} AS stop_code "
                   "FROM shared.job WHERE id = ANY($1::uuid[])")
_JOB_JOIN = "LEFT JOIN shared.job j ON j.id = ttv.job_id"
_JOB_STOP_COL = f"{tenant_budget.job_stop_code_sql('j')} AS job_stop_code"


async def _job_statuses(pool, job_ids) -> dict:
    """{job_id text: (status, stop code)} for the given ids (None ignored); no query when there is none."""
    ids = sorted({str(j) for j in job_ids if j is not None})
    if not ids:
        return {}
    async with pool.acquire() as conn:
        rows = await conn.fetch(_JOB_STATUS_SQL, ids)
    return {str(r["id"]): (r["status"], r.get("stop_code")) for r in rows}


def _with_allowance(d: dict) -> dict:
    """A version held by its tenant's AI budget also says when the allowance resets (else None)."""
    d["allowance_resets_at"] = (tenant_budget.allowance_resets_at().isoformat()
                                if d.get("job_stop_code") == tenant_budget.QUOTA_STOP_CODE else None)
    return d


async def _with_job_status(pool, rows, job_id_key: str) -> list:
    """Tenant-pool rows -> dicts with `job_status` and `job_stop_code` filled from shared.job on MAIN (D2 exception,
    see the module docstring). `_job_id` (my-versions' helper column) is dropped; same keys as the joined form."""
    statuses = await _job_statuses(pool, [r[job_id_key] for r in rows])
    out = []
    for r in rows:
        d = dict(r)
        job_id = d[job_id_key]
        if job_id_key == "_job_id":
            del d["_job_id"]  # my-versions' helper column, not part of the response
        status, stop = statuses.get(str(job_id), (None, None)) if job_id is not None else (None, None)
        d["job_status"] = status        # keeps the key's place
        d["job_stop_code"] = stop
        out.append(d)
    return out
