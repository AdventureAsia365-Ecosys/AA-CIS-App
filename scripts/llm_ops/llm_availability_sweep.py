"""AA-1026 (S267) — the LLM availability sweep: queue one ``llm_connection_probe`` job in candidate mode (every
DISABLED deployment the gateway can call, CIS 367's candidates among them), wait for it, print the matrix.

Run in the CIS api container (orchestrator only: it reads the container's DATABASE_URL, cis_app):

    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/llm_ops/llm_availability_sweep.py SWEEP_LIVE=0 SWEEP_CONNECTIONS=acc3
    tools/ecs/ecsx.sh apps/AA-CIS-App/scripts/llm_ops/llm_availability_sweep.py SWEEP_LIVE=1 SWEEP_CONNECTIONS=acc3

Environment:
  SWEEP_LIVE         0 (default): DRY RUN, the job lists what it would call and the estimated cost, no provider call.
                     1: live, one call per planned deployment (16 output tokens; refused = 0 USD), at most MAX_PROBES.
                     A live run needs LLM_DAILY_PROBE=on on the worker (the job refuses otherwise).
  SWEEP_CONNECTIONS  comma-separated connection keys (default: every connection).
  SWEEP_MODELS       comma-separated model keys (default: every disabled text deployment of those connections).
  SWEEP_WAIT_S       how long to wait for the job (default 600).

Prints one JSON document: the job id and status, the plan (dry run) or the matrix {model_key, connection_key, wire_id,
ok, error_class, error}, the cost, and the stage coverage summary after the run. Every call is in shared.llm_call_log
(stage adhoc_conn_test) and acp_shared.audit_log; each outcome is on its llm_model_deployment row.
"""
import asyncio
import json
import os
import sys
import time

ACTOR = "script:llm_availability_sweep"
DONE = ("succeeded", "failed", "cancelled", "stopped_budget")


def _list(name: str) -> list[str]:
    return [x.strip() for x in (os.environ.get(name) or "").split(",") if x.strip()]


async def main() -> int:
    import asyncpg

    from services.jobs import llm_probe_job as probe

    live = (os.environ.get("SWEEP_LIVE") or "0").strip() in ("1", "true", "yes", "on")
    wait_s = float(os.environ.get("SWEEP_WAIT_S") or 600)
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], ssl="require", min_size=1, max_size=2)
    try:
        job_id, _ = await probe.enqueue_manual(pool, dry_run=not live, actor=ACTOR, mode=probe.MODE_CANDIDATES,
                                               connection_keys=_list("SWEEP_CONNECTIONS"),
                                               model_keys=_list("SWEEP_MODELS"))
        deadline = time.monotonic() + wait_s
        row = None
        while time.monotonic() < deadline:
            row = await pool.fetchrow("SELECT status, error, result FROM shared.job WHERE id = $1::uuid", job_id)
            if row is not None and row["status"] in DONE:
                break
            await asyncio.sleep(5)
        status = row["status"] if row else "missing"
        result = row["result"] if row else None
        if isinstance(result, str):
            result = json.loads(result)
        result = result or {}
        out = {"job_id": job_id, "status": status, "error": row["error"] if row else None, "live": live,
               "estimated_usd": result.get("estimated_usd"), "unpriced": result.get("unpriced")}
        if live:
            out.update(cost_usd=result.get("cost_usd"), probed=result.get("probed"), ok=result.get("ok"),
                       failed=result.get("failed"), matrix=result.get("matrix"),
                       stage_coverage=result.get("stage_coverage"))
        else:
            out.update(planned=[{k: p.get(k) for k in ("connection_key", "model_key", "wire_id", "estimated_usd")}
                                for p in result.get("planned") or []])
        out["skipped"] = result.get("skipped")
        print(json.dumps(out, indent=1, default=str))
        sys.stdout.flush()
        return 0 if status == "succeeded" else 1
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
