"""AA-900 — read-only report of one eval run (shared.prompt_eval_runs) and its logged spend, for the PR of a prompt,
model or route change (docs/llm/eval-runbook.md).

Runs inside the api / worker container through the S3-mediated ECS exec pattern (it is not part of the image, so it
depends on the standard library and asyncpg only). Connects with the container's DATABASE_URL (cis_app, SSL), reads
two tables, writes nothing and calls no model:

    python3 eval_run_report.py <run-id>                    # one run (preferred: the id from the job result)
    python3 eval_run_report.py --latest t9_blog            # the newest FINISHED adhoc_aa900 run of a writer
    python3 eval_run_report.py --latest a1 --stage writing_memory_eval

Prints one JSON object between the markers `--- AA900 EVAL RUN ---` / `--- END ---`: the run row (verdict, stop reason,
cost and cap, kappa, golden set, log stage) and the llm_call_log spend of the run, grouped by stage, so the PR can quote
the run ID and its real logged cost.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

RUN_SQL = """
    SELECT id::text AS run_id, run_kind, writer, prompt_version, verdict, stop_reason, tour_count, cost_usd,
           cost_cap_usd, judge_kappa, judge_counts, triggered_by, created_at, finished_at,
           details->>'golden_set' AS golden_set, coalesce(details->>'log_stage', 'writing_memory_eval') AS log_stage,
           details->>'goal' AS goal, details->'planned' AS planned
      FROM shared.prompt_eval_runs
     WHERE id = $1::uuid AND pipeline = 'writing_memory'
"""
# --latest: only a finished run of the asked label (a run still in progress, or a Releases tab run while a change eval
# is being reported, must not be picked).
LATEST_SQL = """
    SELECT id::text FROM shared.prompt_eval_runs
     WHERE pipeline = 'writing_memory' AND run_kind IN ('r2', 'reverify') AND writer = $1
       AND coalesce(details->>'log_stage', 'writing_memory_eval') = $2 AND finished_at IS NOT NULL
     ORDER BY created_at DESC LIMIT 1
"""
# The run id filter is exact; the created_at bound only lets the (stage, created_at) index narrow the scan. The
# ::timestamptz cast is required: without it Postgres types $3 from `$3 - interval` as an interval and the query fails
# (UndefinedFunctionError, timestamptz >= interval).
SPEND_SQL = """
    SELECT stage, quality_signal->>'real_stage' AS real_stage, role, count(*)::int AS calls,
           coalesce(sum(cost_usd), 0)::float AS cost_usd
      FROM shared.llm_call_log
     WHERE stage = ANY($2::text[]) AND created_at >= $3::timestamptz - interval '10 minutes'
       AND quality_signal->>'eval_run' = $1
     GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
"""
EVAL_STAGES = ["writing_memory_eval", "adhoc_aa900"]
WRITERS = ("a1", "t2", "t9_blog", "t9_social")


def _parse(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="AA-900 read-only eval run report")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("run_id", nargs="?")
    g.add_argument("--latest", choices=WRITERS, help="the newest finished r2 / reverify run of this writer")
    ap.add_argument("--stage", default="adhoc_aa900", choices=EVAL_STAGES,
                    help="with --latest: the run's log_stage (default adhoc_aa900, a change eval)")
    a = ap.parse_args(argv)
    if a.run_id:
        uuid.UUID(a.run_id)             # refuse anything but a UUID before connecting
    return a


def _jsonable(row) -> dict:
    out = {}
    for k, v in dict(row).items():
        if isinstance(v, str) and k == "planned":
            v = json.loads(v)
        out[k] = v
    return out


async def report(conn, run_id: str) -> dict:
    run = await conn.fetchrow(RUN_SQL, run_id)
    if run is None:
        return {"run_id": run_id, "error": "no writing_memory eval run with this id"}
    rows = await conn.fetch(SPEND_SQL, run_id, EVAL_STAGES, run["created_at"])
    spend = [dict(r) for r in rows]
    return {"run": _jsonable(run), "logged_spend": spend,
            "logged_cost_usd": round(sum(r["cost_usd"] for r in spend), 6),
            "logged_calls": sum(r["calls"] for r in spend)}


async def main(argv=None) -> int:
    import asyncpg

    a = _parse(argv)
    dsn = (os.environ.get("DATABASE_URL") or "").strip()
    if not dsn:
        print("DATABASE_URL is not set (run inside the api / worker container)", file=sys.stderr)
        return 2
    conn = await asyncpg.connect(dsn, ssl="require")
    try:
        await conn.execute("SET default_transaction_read_only = on")
        run_id = a.run_id or await conn.fetchval(LATEST_SQL, a.latest, a.stage)
        if not run_id:
            out = {"error": f"no finished {a.stage} r2 run for writer {a.latest}"}
        else:
            out = await report(conn, run_id)
    finally:
        await conn.close()
    print("--- AA900 EVAL RUN ---")
    print(json.dumps(out, indent=1, default=str))
    print("--- END ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
