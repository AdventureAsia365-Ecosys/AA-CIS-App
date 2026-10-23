#!/usr/bin/env python3
"""AA-983 — backfill gold_aa_internal.published_tours.quality_score_id where a revalidation left it NULL.

Before AA-983 a revalidation DELETEd the version's quality_scores rows and INSERTed a new one; since CIS 268 the
FK's ON DELETE SET NULL then left the published tour with quality_score_id = NULL although its version has a
score row. This script points those rows at the version's ACCEPTED score row: the latest one of the published
version (generated_content_id = published_tours.generated_content_id, ORDER BY evaluated_at DESC, id DESC) —
the same rule as CIS 268 step 1, the revalidation re-point and the export. Rows only, no schema change. The
published `quality_score` number is not touched (same rule as 268).

Default = DRY RUN: SELECT only. Prints a JSON summary:
  - `to_fix`: published rows with NULL quality_score_id whose version has a score row (what --apply changes),
    with each row's target score id; split into live and soft-deleted (deleted_at set) rows,
  - `null_without_score`: NULL pointer and no score row for the version (left NULL, nothing to point at),
  - `stale_pointer`: pointer set but not to the version's latest row (reported only, never changed here).

--apply --expect N: in ONE transaction, UPDATE the `to_fix` rows (only where the pointer is still NULL), refuse
and ROLLBACK unless exactly N rows changed (N = the dry-run count) and 0 such rows remain, then COMMIT and
re-count on a NEW connection. The restore list (published ids that were NULL) is printed in the output and, with
--restore-file, written there before the UPDATE; restore = set quality_score_id back to NULL for those ids.

Runs inside the CIS api container with its DATABASE_URL (role cis_app: SELECT/UPDATE on both tables, policies
USING (true)); ssl=require. ECS exec + S3 pattern, one line per command (skill/aa-cis-schema/references/ecs-exec.md):
  BRONZE=aa-cis-bronze-$(aws sts get-caller-identity --profile aa365-admin --query Account --output text)
  aws s3 cp scripts/aa983_backfill_quality_score_id.py s3://$BRONZE/scripts/aa983_backfill.py --profile aa365-admin --region us-west-1
  URL=$(aws s3 presign s3://$BRONZE/scripts/aa983_backfill.py --profile aa365-admin --region us-west-1 --expires-in 300)
  TASK_ARN=$(aws ecs list-tasks --cluster aa-cis-dev-cluster --service-name aa-cis-dev-api --profile aa365-admin --region us-west-1 --query 'taskArns[0]' --output text)
  dry run: aws ecs execute-command --cluster aa-cis-dev-cluster --task $TASK_ARN --container api --interactive --command "sh -c 'curl -s \"$URL\" -o /tmp/b.py && python3 /tmp/b.py'" --profile aa365-admin --region us-west-1
  apply:   same command with 'python3 /tmp/b.py --apply --expect <N from the dry run> --restore-file /tmp/aa983_restore.json'
The script needs only asyncpg (no repo import), so it does not depend on the image carrying this change.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

import asyncpg

# Latest score row of a published tour's version (the accepted score).
_TARGET = """
    SELECT pt.id            AS published_id,
           pt.tour_id,
           pt.generated_content_id,
           pt.deleted_at IS NOT NULL AS soft_deleted,
           pt.quality_score AS published_score,
           pt.quality_score_id,
           (SELECT q.id FROM silver_aa_internal.quality_scores q
            WHERE q.generated_content_id = pt.generated_content_id
            ORDER BY q.evaluated_at DESC, q.id DESC LIMIT 1) AS target_score_id
    FROM gold_aa_internal.published_tours pt
"""

SQL_TO_FIX = f"""
    SELECT t.*, q.score_overall AS target_score, q.evaluated_at AS target_evaluated_at
    FROM ({_TARGET} WHERE pt.quality_score_id IS NULL) t
    JOIN silver_aa_internal.quality_scores q ON q.id = t.target_score_id
    ORDER BY t.tour_id
"""

SQL_COUNTS = f"""
    SELECT count(*) FILTER (WHERE quality_score_id IS NULL AND target_score_id IS NOT NULL) AS to_fix,
           count(*) FILTER (WHERE quality_score_id IS NULL AND target_score_id IS NULL)     AS null_without_score,
           count(*) FILTER (WHERE quality_score_id IS NOT NULL
                              AND quality_score_id IS DISTINCT FROM target_score_id)        AS stale_pointer,
           count(*)                                                                          AS published_total
    FROM ({_TARGET}) t
"""

SQL_APPLY = f"""
    UPDATE gold_aa_internal.published_tours pt
    SET quality_score_id = t.target_score_id
    FROM ({_TARGET} WHERE pt.quality_score_id IS NULL) t
    WHERE pt.id = t.published_id
      AND t.target_score_id IS NOT NULL
      AND pt.quality_score_id IS NULL
    RETURNING pt.id::text AS published_id, pt.tour_id::text AS tour_id, t.target_score_id::text AS score_id
"""


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (v if isinstance(v, (int, float, bool, type(None))) else str(v)) for k, v in row.items()}


async def dry_run(conn: asyncpg.Connection) -> dict[str, Any]:
    counts = dict(await conn.fetchrow(SQL_COUNTS))
    rows = [_jsonable(dict(r)) for r in await conn.fetch(SQL_TO_FIX)]
    return {
        "mode": "dry_run",
        "counts": {k: int(v) for k, v in counts.items()},
        "to_fix_live": sum(1 for r in rows if not r["soft_deleted"]),
        "to_fix_soft_deleted": sum(1 for r in rows if r["soft_deleted"]),
        "to_fix": rows,
    }


async def apply(conn: asyncpg.Connection, expect: int, restore_file: str | None) -> dict[str, Any]:
    before = [_jsonable(dict(r)) for r in await conn.fetch(SQL_TO_FIX)]
    if len(before) != expect:
        raise SystemExit(f"refused: {len(before)} rows to fix now, --expect {expect} (re-run the dry run)")
    restore = [r["published_id"] for r in before]
    if restore_file:
        with open(restore_file, "w", encoding="utf-8") as fh:
            json.dump({"issue": "AA-983", "restore": "UPDATE gold_aa_internal.published_tours "
                       "SET quality_score_id = NULL WHERE id = ANY(<published_ids>)",
                       "published_ids": restore}, fh, indent=1)
    async with conn.transaction():
        changed = await conn.fetch(SQL_APPLY)
        left = (await conn.fetchrow(SQL_COUNTS))["to_fix"]
        if len(changed) != expect or left != 0:
            raise SystemExit(f"refused, rolled back: changed {len(changed)} (expected {expect}), {left} left")
    return {"mode": "apply", "changed": len(changed), "rows": [dict(r) for r in changed],
            "restore_published_ids": restore}


async def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="write (default: DRY RUN, SELECT only)")
    ap.add_argument("--expect", type=int, help="with --apply: the dry-run to_fix count; anything else refuses")
    ap.add_argument("--restore-file", help="with --apply: write the restore list here before the UPDATE")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"), help="default: $DATABASE_URL")
    ap.add_argument("--no-ssl", action="store_true", help="local test databases only")
    args = ap.parse_args(argv)
    if not args.dsn:
        raise SystemExit("DATABASE_URL is not set")
    if args.apply and args.expect is None:
        raise SystemExit("--apply needs --expect <dry-run to_fix count>")
    ssl = None if args.no_ssl else "require"

    conn = await asyncpg.connect(args.dsn, ssl=ssl)
    try:
        out = await (apply(conn, args.expect, args.restore_file) if args.apply else dry_run(conn))
    finally:
        await conn.close()
    if args.apply:  # independent verify on a NEW connection
        conn = await asyncpg.connect(args.dsn, ssl=ssl)
        try:
            out["verify_after"] = {k: int(v) for k, v in dict(await conn.fetchrow(SQL_COUNTS)).items()}
        finally:
            await conn.close()
    return out


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(sys.argv[1:])), indent=1, default=str))
