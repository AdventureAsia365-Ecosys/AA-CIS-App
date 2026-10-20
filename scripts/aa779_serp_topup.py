#!/usr/bin/env python3
"""AA-779 (B) — admin-only SERP top-up of real search questions for blog places. Hard cap $3 across all runs.

What it does (services/acp_shared/question_topup.py): for every place of blog work (Subjects a tenant
can still write + blog angle-gate requests) it counts the real questions the chain already has
(Segment matches, place PAA, earlier top-up); a place with fewer than 3 gets ONE Google SERP for
'<place> <country>' (US, English) with `people_also_ask_click_depth=2`, and the questions are stored in
`acp_contract.search_demand` (keyword '<place> <country>' folded, market 'US-PAA2' — its own, apart from
the Segment research cache). A place that returned no question gets an empty row, so a rerun skips it.
Blogs then read them as the `serp_topup` tier.

Default = DRY RUN: SELECTs only, prints the place list and the estimated cost, buys nothing, writes
nothing; it also prints what earlier runs spent (`prior_spent_usd`) and what is left of the $3. `--run`
buys, under a budget of min(--cap, $3 − earlier spend) (a cap above $3 is refused; nothing left = refused)
checked before every call; each call is logged in shared.dfs_call_log as endpoint 'serp_advanced_paa_deep'.
Never wired to a tenant action, a router or a job (AA-646).

Run inside the CIS api container (ECS exec + S3 pattern, one line per command; see
skill/aa-cis-schema/references/ecs-exec.md). The image must carry this change:
  BRONZE=aa-cis-bronze-$(aws sts get-caller-identity --profile aa365-admin --query Account --output text)
  aws s3 cp scripts/aa779_serp_topup.py s3://$BRONZE/scripts/aa779_topup.py --profile aa365-admin --region us-west-1
  URL=$(aws s3 presign s3://$BRONZE/scripts/aa779_topup.py --profile aa365-admin --region us-west-1 --expires-in 300)
  TASK_ARN=$(aws ecs list-tasks --cluster aa-cis-dev-cluster --service-name aa-cis-dev-api --profile aa365-admin --region us-west-1 --query 'taskArns[0]' --output text)
  dry run:  aws ecs execute-command --cluster aa-cis-dev-cluster --task $TASK_ARN --container api --interactive --command "sh -c 'curl -s \"$URL\" -o /tmp/t.py && cd /app && python3 /tmp/t.py'" --profile aa365-admin --region us-west-1
  real run: same command with 'python3 /tmp/t.py --run --cap 3'
  aws s3 cp s3://$BRONZE/scripts/aa779-serp-topup.json .tmp-session/aa779-serp-topup.json --profile aa365-admin --region us-west-1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.getcwd())

REGION = os.environ.get("AWS_REGION", "us-west-1")


async def _pool():
    import asyncpg
    from urllib.parse import urlparse

    from shared.secrets import get_database_url

    u = urlparse(get_database_url().strip())
    return await asyncpg.create_pool(host=u.hostname, port=u.port or 5432, user=u.username, password=u.password,
                                     database=u.path.lstrip("/"), ssl="require", min_size=1, max_size=4)


async def main(run: bool, cap: float) -> dict[str, Any]:
    from services.acp_shared.question_topup import run_topup

    pool = await _pool()
    try:
        return await run_topup(pool, dry_run=not run, cap_usd=cap)
    finally:
        await pool.close()


def _write(result: dict[str, Any], name: str = "aa779-serp-topup.json") -> None:
    body = json.dumps(result, indent=2, default=str, ensure_ascii=False)
    summary = {k: v for k, v in result.items() if k not in ("targets", "bought", "left_out_by_cap")}
    summary["places"] = [f"{t['query']}  (real now {t['real_now']}, used by {t['used_by']})"
                         for t in result.get("targets", [])]
    print(json.dumps(summary, indent=2, default=str, ensure_ascii=False), flush=True)
    path = f"/tmp/{name}"
    with open(path, "w") as f:
        f.write(body)
    try:
        import boto3

        bucket = os.environ.get("BRONZE_BUCKET", "").strip() or (
            "aa-cis-bronze-" + boto3.client("sts", region_name=REGION).get_caller_identity()["Account"])
        boto3.client("s3", region_name=REGION).upload_file(path, bucket, f"scripts/{name}")
        print(f"uploaded s3://{bucket}/scripts/{name}", flush=True)
    except Exception as e:  # noqa: BLE001 — a local run without S3 access still prints the summary
        print(f"S3 upload skipped: {e}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--run", action="store_true", help="buy the SERPs (default: dry run, $0)")
    parser.add_argument("--cap", type=float, default=3.0, help="spend cap in USD for this run (at most 3)")
    args = parser.parse_args()
    if args.cap > 3.0 or args.cap <= 0:
        parser.error("--cap must be > 0 and at most 3 (AA-779 hard cap)")
    _write(asyncio.run(main(args.run, args.cap)))
