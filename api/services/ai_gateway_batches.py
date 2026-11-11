"""AA-1076 — the Batches view of the AI providers page: every gateway batch (``llm_batch`` job), its status and its
spend; and the two writes, send a wave through the batch lane and cancel a batch.

Reads only shared.job (the batch's state lives in its row, services/jobs/llm_batch_job.py) and shared.llm_call_log
(the batch's rows, one per request: ``quality_signal.batch_id``). Spend is what the ledger holds for the batch;
``standard_usd`` is what the same requests would have cost on demand (the batch rate is 50% of every token), and
``saved_usd`` the difference. $0: no provider is called by a read.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog

from shared.llm_client import batch_lane

logger = structlog.get_logger()

KIND = "llm_batch"
LIST_LIMIT_MAX = 100
API_CONSUMERS = ("s1_attempt1", "a3_atomize")     # ad hoc batches are sent by scripts, not from the page

_LIST_SQL = """
SELECT id, status, payload, progress, result, error, created_at, started_at, finished_at, run_after, created_by
  FROM shared.job WHERE kind = $1 ORDER BY created_at DESC LIMIT $2
"""

_ONE_SQL = """
SELECT id, status, payload, progress, result, error, created_at, started_at, finished_at, run_after, created_by
  FROM shared.job WHERE kind = $1 AND id = $2::uuid
"""


def _obj(v) -> dict:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return {}
    return v if isinstance(v, dict) else {}


def _iso(v) -> Optional[str]:
    return v.isoformat() if isinstance(v, datetime) else (str(v) if v else None)


def _ts(v) -> Optional[datetime]:
    if isinstance(v, datetime):
        return v
    try:
        out = datetime.fromisoformat(str(v))
        return out if out.tzinfo else out.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def shape(row: dict, spend: Optional[dict]) -> dict:
    """One batch as the page shows it. Pure."""
    payload, prog, result = _obj(row.get("payload")), _obj(row.get("progress")), _obj(row.get("result"))
    target = prog.get("target") or result.get("target") or {}
    usd = float((spend or {}).get("usd") or 0.0)
    handoff = result.get("handoff") or {}
    return {
        "job_id": str(row["id"]), "batch_id": payload.get("batch_id"), "consumer": payload.get("consumer"),
        "dry_run": bool(payload.get("dry_run")), "job_status": row.get("status"),
        "phase": prog.get("phase") or ("queued" if row.get("status") == "queued" else None),
        "stage": target.get("ledger_stage") or target.get("stage"), "model_key": target.get("model_key"),
        "provider": target.get("provider"), "connection_key": target.get("connection_key"),
        "provider_batch_id": prog.get("provider_batch_id"), "provider_status": prog.get("provider_status"),
        "counts": prog.get("counts") or {}, "requests": prog.get("requests") or result.get("requests"),
        "tours": len(payload.get("tour_ids") or []), "outcomes": result.get("outcomes") or prog.get("outcomes") or {},
        "cap_usd": prog.get("cap_usd", payload.get("cap_usd")), "estimate_usd": prog.get("estimate_usd"),
        # how the estimate was made: ledger_p95 (measured) or worst_case, and the worst case itself
        "estimate_basis": (prog.get("estimate") or {}).get("basis"),
        "worst_case_usd": (prog.get("estimate") or {}).get("worst_case_usd"),
        "spend_usd": round(usd, 6), "standard_usd": round(usd / batch_lane.pricing.BATCH_DISCOUNT, 6),
        "saved_usd": round(usd / batch_lane.pricing.BATCH_DISCOUNT - usd, 6),
        "ledger_rows": int((spend or {}).get("rows") or 0),
        "handoff": {k: handoff.get(k) for k in ("kind", "enqueued", "seeded", "on_demand", "seeded_days",
                                                 "drill_withheld") if k in handoff},
        "polls": prog.get("polls"), "reason": prog.get("reason"), "error": row.get("error"),
        "created_at": _iso(row.get("created_at")), "submitted_at": prog.get("submitted_at"),
        "finished_at": _iso(row.get("finished_at")),
        "next_check_at": _iso(row.get("run_after")) if row.get("status") == "queued" else None,
        "created_by": row.get("created_by"),
    }


async def _spend(conn, row: dict) -> Optional[dict]:
    payload, prog, result = _obj(row.get("payload")), _obj(row.get("progress")), _obj(row.get("result"))
    target = prog.get("target") or result.get("target") or {}
    stage = target.get("ledger_stage") or target.get("stage")
    since = _ts(prog.get("submitted_at"))
    if not (stage and since and payload.get("batch_id")):
        return None
    return await batch_lane.ledgered(conn, stage, since, payload["batch_id"])


async def list_batches(pool, limit: int = 30) -> dict:
    limit = max(1, min(int(limit), LIST_LIMIT_MAX))
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(_LIST_SQL, KIND, limit)]
        out = [shape(r, await _spend(conn, r)) for r in rows]
    totals = {"batches": len(out), "spend_usd": round(sum(b["spend_usd"] for b in out), 6),
              "standard_usd": round(sum(b["standard_usd"] for b in out), 6),
              "saved_usd": round(sum(b["saved_usd"] for b in out), 6),
              "open": sum(1 for b in out if b["job_status"] in ("queued", "running"))}
    return {"batches": out, "totals": totals, "lane_closed_reason": batch_lane.closed_reason()}


async def get_batch(pool, job_id: str) -> Optional[dict]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_ONE_SQL, KIND, job_id)
        if row is None:
            return None
        row = dict(row)
        return shape(row, await _spend(conn, row))


def payload_for(*, consumer: str, tour_ids: list[str], cap_usd: float, dry_run: bool, run_batch_id: Optional[str],
                seo_mode: str, cache_system: Optional[bool], drill_withhold: int) -> dict:
    """The ``llm_batch`` job payload of a page send (validated by the router's body model)."""
    if consumer not in API_CONSUMERS:
        raise ValueError(f"consumer must be one of {API_CONSUMERS}")
    args: dict = {"seo_mode": seo_mode}
    if run_batch_id:
        args["batch_id"] = run_batch_id
    if drill_withhold:
        args["drill_withhold"] = int(drill_withhold)
    return {"batch_id": str(uuid.uuid4()), "consumer": consumer, "cap_usd": float(cap_usd), "dry_run": bool(dry_run),
            "cache_system": cache_system, "tour_ids": [str(t) for t in tour_ids], "args": args}


async def cancel_batch(pool, job_id: str) -> dict:
    """Ask the provider to stop a sent batch; the job's next poll (moved to now) ingests what finished and the rest
    goes on demand. A batch not sent yet is cancelled as a job instead."""
    from shared.jobs import queue
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_ONE_SQL, KIND, job_id)
    if row is None:
        raise LookupError("no such batch")
    row = dict(row)
    prog = _obj(row.get("progress"))
    if row["status"] not in ("queued", "running"):
        raise ValueError(f"the batch job is {row['status']}: nothing to cancel")
    pbid = prog.get("provider_batch_id")
    if not pbid:
        status = await queue.request_cancel(pool, job_id)
        return {"cancelled": "job", "job_id": job_id, "job_status": status}
    target = batch_lane.BatchTarget.from_dict(prog["target"])
    await asyncio.to_thread(batch_lane.cancel, target, pbid)
    await pool.execute(
        "UPDATE shared.job SET run_after = now(), progress = progress || $2::jsonb, updated_at = now() "
        "WHERE id = $1::uuid AND status = 'queued'",
        job_id, json.dumps({"cancel_sent_at": datetime.now(timezone.utc).isoformat()}))
    return {"cancelled": "provider_batch", "job_id": job_id, "provider_batch_id": pbid}
