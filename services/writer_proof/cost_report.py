"""AA-1051 — cost and requests per object of the W5 proof runs, from the ledger (shared.llm_call_log), $0 and read-only.

Every model call of a proof run is a ledger row under stage adhoc_aa1051 with quality_signal.eval_run = the R2 run id
(call_log.bind_eval_stage; the real stage in quality_signal.real_stage, the role in its column). The R2 runner works
one item at a time and waits for the ledger writes (flush) before it stores the item's result row, so a ledger row
belongs to the first result row of its run stored at or after it (`attribute`): the object's own writer and judge
calls. Rows after the last result (none expected) are reported as unattributed, never dropped.

Report per run: ledger total, rows, cost per object two ways (all-in = run total / candidate objects; attributed =
median / mean / max over the candidate rows), requests and writer requests per object (median / max), plus the W5
totals against the D12 caps.

    python -m services.writer_proof.cost_report --prereg w5-null-t9-blog      # the runs of one pre-registration
    python -m services.writer_proof.cost_report --run <uuid> [--run <uuid>]   # given runs
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from statistics import mean, median
from typing import Any, Optional

from services.writer_proof.spend import W5_STAGE

LEDGER_SQL = """
    SELECT l.id::text AS id, l.created_at, coalesce(l.cost_usd, 0)::float AS cost_usd, l.role,
           l.quality_signal->>'real_stage' AS real_stage, l.quality_signal->>'eval_run' AS eval_run, l.status
      FROM shared.llm_call_log l
     WHERE l.stage = $1 AND l.quality_signal->>'eval_run' = ANY($2::text[])
     ORDER BY l.created_at, l.id
"""
RESULTS_SQL = """
    SELECT r.result_id::text AS result_id, r.run_id::text AS run_id, g.sample_id, r.arm, r.mode, r.verdict,
           r.created_at, r.cost_usd::float AS cost_usd, r.failure_codes, r.hard_stop, r.details
      FROM shared.eval_item_result r
      JOIN shared.eval_golden_item g ON g.item_id = r.item_id
     WHERE r.run_id = ANY($1::uuid[])
     ORDER BY r.created_at, r.result_id
"""
RUNS_SQL = """
    SELECT p.id::text AS run_id, p.writer, p.verdict, p.stop_reason, p.cost_usd::float AS cost_usd,
           p.details->'prereg' AS prereg, p.created_at
      FROM shared.prompt_eval_runs p
     WHERE p.pipeline = 'writing_memory' AND (p.details->'prereg'->>'id' = $1 OR p.id::text = ANY($2::text[]))
     ORDER BY p.created_at
"""


def attribute(ledger: list[dict], results: list[dict]) -> dict:
    """{per_result: {result_id: {cost_usd, requests, writer_requests}}, unattributed: {run_id: {...}}}. Each ledger
    row goes to the first result row of its run whose created_at >= the ledger row's. Pure."""
    by_run: dict[str, list[dict]] = {}
    for r in sorted(results, key=lambda r: (r["created_at"], r["result_id"])):
        by_run.setdefault(str(r["run_id"]), []).append(r)
    per: dict[str, dict] = {str(r["result_id"]): {"cost_usd": 0.0, "requests": 0, "writer_requests": 0}
                            for r in results}
    orphan: dict[str, dict] = {}
    for row in sorted(ledger, key=lambda x: (x["created_at"], x["id"])):
        run = str(row.get("eval_run"))
        target = next((r for r in by_run.get(run, []) if r["created_at"] >= row["created_at"]), None)
        slot = per[str(target["result_id"])] if target else orphan.setdefault(
            run, {"cost_usd": 0.0, "requests": 0, "writer_requests": 0})
        slot["cost_usd"] += float(row.get("cost_usd") or 0.0)
        slot["requests"] += 1
        slot["writer_requests"] += row.get("role") == "writer"
    for d in list(per.values()) + list(orphan.values()):
        d["cost_usd"] = round(d["cost_usd"], 6)
    return {"per_result": per, "unattributed": orphan}


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "median": None, "mean": None, "max": None}
    return {"n": len(xs), "median": round(median(xs), 6), "mean": round(mean(xs), 6), "max": round(max(xs), 6)}


def report(runs: list[dict], ledger: list[dict], results: list[dict]) -> dict:
    """Per run and in total: ledger cost and rows, cost / requests per candidate object. Pure."""
    att = attribute(ledger, results)
    out_runs = []
    for run in runs:
        rid = run["run_id"]
        led = [x for x in ledger if x.get("eval_run") == rid]
        cand = [r for r in results if str(r["run_id"]) == rid and r["arm"] == "candidate"
                and r["mode"] == "regenerate"]
        objs = [att["per_result"][str(r["result_id"])] for r in cand]
        total = round(sum(float(x.get("cost_usd") or 0.0) for x in led), 6)
        by_stage: dict[str, float] = {}
        for x in led:
            k = x.get("real_stage") or "?"
            by_stage[k] = round(by_stage.get(k, 0.0) + float(x.get("cost_usd") or 0.0), 6)
        out_runs.append({
            "run_id": rid, "writer": run.get("writer"), "verdict": run.get("verdict"),
            "stop_reason": run.get("stop_reason"), "prereg": run.get("prereg"),
            "ledger_usd": total, "ledger_rows": len(led), "by_real_stage_usd": by_stage,
            "objects": len(cand),
            "all_in_cost_per_object_usd": round(total / len(cand), 6) if cand else None,
            "attributed_cost_per_object_usd": _stats([o["cost_usd"] for o in objs]),
            "requests_per_object": _stats([float(o["requests"]) for o in objs]),
            "writer_requests_per_object": _stats([float(o["writer_requests"]) for o in objs]),
            "unattributed": att["unattributed"].get(rid)})
    return {"stage": W5_STAGE, "runs": out_runs,
            "total_usd": round(sum(r["ledger_usd"] for r in out_runs), 6),
            "per_result": att["per_result"]}


async def load(conn, *, prereg_id: Optional[str], run_ids: list[str]) -> tuple[list[dict], list[dict], list[dict]]:
    runs = [dict(r) for r in await conn.fetch(RUNS_SQL, prereg_id or "", run_ids)]
    for r in runs:
        if isinstance(r.get("prereg"), str):
            r["prereg"] = json.loads(r["prereg"])
    ids = [r["run_id"] for r in runs]
    ledger = [dict(r) for r in await conn.fetch(LEDGER_SQL, W5_STAGE, ids)] if ids else []
    results = [dict(r) for r in await conn.fetch(RESULTS_SQL, ids)] if ids else []
    for r in results:
        if isinstance(r.get("details"), str):
            r["details"] = json.loads(r["details"])
    return runs, ledger, results


def main(argv: Optional[list[str]] = None) -> int:
    from services.writer_proof.splits import _connect
    ap = argparse.ArgumentParser(prog="python -m services.writer_proof.cost_report", description=__doc__.split("\n")[0])
    ap.add_argument("--prereg", help="the runs of this pre-registration id")
    ap.add_argument("--run", action="append", default=[], help="a run id (repeat)")
    a = ap.parse_args(argv)
    if not a.prereg and not a.run:
        ap.error("--prereg or --run is required")

    async def _go() -> dict:
        conn = await _connect()
        try:
            async with conn.transaction(readonly=True):
                runs, ledger, results = await load(conn, prereg_id=a.prereg, run_ids=a.run)
        finally:
            await conn.close()
        rep: dict[str, Any] = report(runs, ledger, results)
        rep.pop("per_result")
        return rep

    print(json.dumps(asyncio.run(_go()), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
