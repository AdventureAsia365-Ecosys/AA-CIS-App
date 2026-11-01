"""AA-1026 D5 — job kind ``embedding_reembed``: re-embed one vector column into another embedding space
(``services/embeddings/reembed.py``; operator steps: root runbook ``docs/ops/runbooks/embedding-re-embed.md``).

Payload: {"mode": "dry_run" | "fill" | "probe", "column": "<schema.table.column>" (dry_run / fill),
"space_key": "<target space>" (dry_run before the shadow is prepared, probe), "cost_cap_usd": number (fill,
0 < cap <= 25, for the whole job: what earlier attempts spent counts against it), "batch_size"?, "max_rows"?,
"requested_by"}. Queued by POST /admin/job-runner/embedding-reembed/run
(DRY RUN by default).

Paid calls: dry_run none; probe one embedding of a few words; fill one embedding call per page (Cohere: up to 96 texts
per call; Titan: one request per text), stopped before a page that would cross the cap. The cap is per job: its retries
(max_attempts 3) and releases on deploy share it (``shared.job.cost_usd``); a new job is a new cap (an operator's
decision). A fill is resumable: it only reads rows whose shadow is still empty, so a retry, a release or a re-queue
continues where it stopped. concurrency 1 (one re-embed at a time; they share the account's embedding quota),
background (never takes the worker's last free slots).
"""
from __future__ import annotations

from typing import Optional

from services.embeddings import reembed
from shared.jobs.registry import JobContext, NonRetryable, enqueue, job_kind

KIND = "embedding_reembed"


def check_payload(p: dict) -> dict:
    """The normalised payload, or ValueError (the admin route answers 422 with it)."""
    mode = p.get("mode") or "dry_run"
    if mode not in reembed.MODES:
        raise ValueError(f"mode must be one of {reembed.MODES}")
    out = {"mode": mode, "requested_by": p.get("requested_by")}
    if mode in ("dry_run", "fill"):
        if not isinstance(p.get("column"), str) or not p["column"]:
            raise ValueError("column (schema.table.column) is required")
        out["column"] = p["column"]
    if mode == "probe" and not p.get("space_key"):
        raise ValueError("space_key is required for a probe")
    if p.get("space_key"):
        out["space_key"] = str(p["space_key"])
    if mode == "fill":
        cap = p.get("cost_cap_usd")
        if not isinstance(cap, (int, float)) or isinstance(cap, bool) or not 0 < cap <= reembed.MAX_COST_CAP_USD:
            raise ValueError(f"cost_cap_usd must be > 0 and <= {reembed.MAX_COST_CAP_USD} for a fill")
        out["cost_cap_usd"] = float(cap)
        for key, hi in (("batch_size", 96), ("max_rows", 10_000_000)):
            if p.get(key) is not None:
                v = p[key]
                if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= hi:
                    raise ValueError(f"{key} must be an integer 1..{hi}")
                out[key] = v
    return out


@job_kind(KIND, concurrency=1, max_attempts=3, expected_seconds=1800, background=True)
async def run(ctx: JobContext) -> Optional[dict]:
    try:
        p = check_payload(ctx.payload or {})
    except ValueError as e:
        raise NonRetryable(str(e))
    try:
        if p["mode"] == "dry_run":
            result = await reembed.dry_run(ctx.pool, p["column"], p.get("space_key"))
        elif p["mode"] == "probe":
            result = await reembed.probe(ctx.pool, p["space_key"])
        else:
            spent_before = float(ctx.cost_usd or 0.0)     # earlier attempts of this job (retry, release on deploy)
            result = await reembed.fill(ctx.pool, p["column"], cost_cap_usd=p["cost_cap_usd"],
                                        batch_size=p.get("batch_size"), max_rows=p.get("max_rows"),
                                        progress=ctx.progress, add_cost=ctx.add_cost,
                                        already_spent=spent_before)
    except reembed.ReembedRefused as e:
        raise NonRetryable(str(e))
    await ctx.progress(step="done")
    return result


async def enqueue_manual(pool, payload: dict, *, actor: str) -> tuple[str, bool]:
    """One run from the admin route (``check_payload`` first)."""
    p = check_payload({**payload, "requested_by": actor})
    return await enqueue(pool, KIND, p, created_by=actor)
