#!/usr/bin/env python3
"""AA-983 — backfill gold_aa_internal.published_tours.quality_score_id where a revalidation left it NULL.

Before AA-983 a revalidation DELETEd the version's quality_scores rows and INSERTed a new one; since CIS 268 the
FK's ON DELETE SET NULL then left the published tour with quality_score_id = NULL although its version has a
score row. This script points those rows at the version's ACCEPTED score row: the latest one of the published
version (generated_content_id = published_tours.generated_content_id, ORDER BY evaluated_at DESC, id DESC) —
the same rule as CIS 268 step 1, the revalidation re-point and the export. Rows only, no schema change. The
published `quality_score` number is not touched (same rule as 268).

Default = DRY RUN: SELECT only. Prints a JSON summary:
  - `counts.to_fix`: published rows with NULL quality_score_id whose version has a score row (what --apply
    changes), listed in `to_fix` with each row's target score id; split into live and soft-deleted rows,
  - `counts.null_without_score`: NULL pointer and no score row for the version (left NULL, nothing to point at),
  - `counts.stale_pointer`: pointer set to an older score row OF THE SAME VERSION (not the latest one); listed in
    `stale` (`pointer_same_version: true`, pointer evaluated_at / score vs the target's). Changed only with
    --include-stale,
  - `counts.stale_other_version`: pointer set to a score row of ANOTHER version while this version has its own
    score row; listed in `stale` (`pointer_same_version: false`), never changed: a different defect (no current
    code path writes it), report it for a follow-up issue,
  - `counts.stale_no_target`: pointer set but the version has no score row (pointer names another version's
    row); listed in `stale`, never changed (nothing to point at),
  - `revalidate_candidate`: one live published tour whose pointer is already the latest row, with the admin
    revalidate path, for the live check of the fix (step 5 of the apply order).

--apply --expect N [--include-stale]: in ONE transaction, UPDATE exactly the rows the dry run lists (to_fix, plus
with --include-stale the `stale` rows counted in `stale_pointer`; each only while its pointer is still what the
dry run saw), refuse and ROLLBACK unless exactly N rows changed and 0 such rows remain, then COMMIT and re-count on
a NEW connection. The restore list (published id + previous pointer) is printed and, with --restore-file, written there
before the UPDATE.

--tour <tour_id>: the live check after a revalidation (SELECT only). Prints the published row's pointer, the
version's latest score row, `pointer_is_latest`, and the version's latest `revalidate` job (status,
result.quality_score_id, result.published_repointed); `ok` is true when the pointer is non-NULL, equals the latest
row and (when a revalidate job exists) equals the job's quality_score_id with published_repointed >= 1.

Runs inside the CIS api container with its DATABASE_URL (role cis_app: SELECT/UPDATE on both tables, SELECT on
shared.job, policies USING (true)); ssl=require. ECS exec + S3 pattern, one line per command
(skill/aa-cis-schema/references/ecs-exec.md):
  BRONZE=aa-cis-bronze-$(aws sts get-caller-identity --profile aa365-admin --query Account --output text)
  aws s3 cp scripts/aa983_backfill_quality_score_id.py s3://$BRONZE/scripts/aa983_backfill.py --profile aa365-admin --region us-west-1
  URL=$(aws s3 presign s3://$BRONZE/scripts/aa983_backfill.py --profile aa365-admin --region us-west-1 --expires-in 300)
  TASK_ARN=$(aws ecs list-tasks --cluster aa-cis-dev-cluster --service-name aa-cis-dev-api --profile aa365-admin --region us-west-1 --query 'taskArns[0]' --output text)
  dry run: aws ecs execute-command --cluster aa-cis-dev-cluster --task $TASK_ARN --container api --interactive --command "sh -c 'curl -s \"$URL\" -o /tmp/b.py && python3 /tmp/b.py'" --profile aa365-admin --region us-west-1
  apply:   same command with 'python3 /tmp/b.py --apply --expect <N from the dry run> --restore-file /tmp/aa983_restore.json'
  check:   same command with 'python3 /tmp/b.py --tour <tour_id>'
Output: the JSON summary between the lines AA983-JSON-BEGIN and AA983-JSON-END, so it can be cut out of an ECS exec
session transcript (session-manager banners, CR line ends) mechanically.
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
           (SELECT q.generated_content_id FROM silver_aa_internal.quality_scores q
            WHERE q.id = pt.quality_score_id) AS pointer_version,
           (SELECT q.id FROM silver_aa_internal.quality_scores q
            WHERE q.generated_content_id = pt.generated_content_id
            ORDER BY q.evaluated_at DESC, q.id DESC LIMIT 1) AS target_score_id,
           (SELECT count(*) FROM silver_aa_internal.quality_scores q
            WHERE q.generated_content_id = pt.generated_content_id) AS version_score_rows
    FROM gold_aa_internal.published_tours pt
"""

SQL_TO_FIX = f"""
    SELECT t.*, q.score_overall AS target_score, q.evaluated_at AS target_evaluated_at
    FROM ({_TARGET} WHERE pt.quality_score_id IS NULL) t
    JOIN silver_aa_internal.quality_scores q ON q.id = t.target_score_id
    ORDER BY t.tour_id
"""

SQL_STALE = f"""
    SELECT t.*,
           cur.generated_content_id = t.generated_content_id AS pointer_same_version,
           cur.score_overall AS pointer_score, cur.evaluated_at AS pointer_evaluated_at,
           q.score_overall AS target_score, q.evaluated_at AS target_evaluated_at
    FROM ({_TARGET} WHERE pt.quality_score_id IS NOT NULL) t
    LEFT JOIN silver_aa_internal.quality_scores cur ON cur.id = t.quality_score_id
    LEFT JOIN silver_aa_internal.quality_scores q ON q.id = t.target_score_id
    WHERE t.quality_score_id IS DISTINCT FROM t.target_score_id
    ORDER BY t.tour_id
"""

SQL_COUNTS = f"""
    SELECT count(*) FILTER (WHERE quality_score_id IS NULL AND target_score_id IS NOT NULL) AS to_fix,
           count(*) FILTER (WHERE quality_score_id IS NULL AND target_score_id IS NULL)     AS null_without_score,
           count(*) FILTER (WHERE quality_score_id IS NOT NULL AND target_score_id IS NOT NULL
                              AND quality_score_id <> target_score_id
                              AND pointer_version = generated_content_id)                   AS stale_pointer,
           count(*) FILTER (WHERE quality_score_id IS NOT NULL AND target_score_id IS NOT NULL
                              AND pointer_version IS DISTINCT FROM generated_content_id)    AS stale_other_version,
           count(*) FILTER (WHERE quality_score_id IS NOT NULL AND target_score_id IS NULL)  AS stale_no_target,
           count(*)                                                                          AS published_total
    FROM ({_TARGET}) t
"""

# $1 = published ids the dry run listed, $2 = their pointer as the dry run saw it (NULL for to_fix rows).
SQL_APPLY = f"""
    UPDATE gold_aa_internal.published_tours pt
    SET quality_score_id = t.target_score_id
    FROM ({_TARGET}) t,
         unnest($1::uuid[], $2::uuid[]) AS seen(published_id, quality_score_id)
    WHERE pt.id = t.published_id
      AND seen.published_id = pt.id
      AND t.target_score_id IS NOT NULL
      AND pt.quality_score_id IS NOT DISTINCT FROM seen.quality_score_id
    RETURNING pt.id::text AS published_id, pt.tour_id::text AS tour_id, t.target_score_id::text AS score_id
"""

SQL_CANDIDATE = f"""
    SELECT t.tour_id::text, t.generated_content_id::text, t.quality_score_id::text
    FROM ({_TARGET} WHERE pt.deleted_at IS NULL) t
    WHERE t.quality_score_id IS NOT NULL AND t.quality_score_id = t.target_score_id
    ORDER BY t.tour_id
    LIMIT 1
"""

SQL_TOUR = """
    SELECT pt.id AS published_id, pt.tour_id, pt.generated_content_id,
           pt.deleted_at IS NOT NULL AS soft_deleted, pt.quality_score AS published_score,
           pt.quality_score_id, latest.id AS latest_score_id, latest.evaluated_at AS latest_evaluated_at,
           latest.score_overall AS latest_score,
           (SELECT count(*) FROM silver_aa_internal.quality_scores q
            WHERE q.generated_content_id = pt.generated_content_id) AS version_score_rows
    FROM gold_aa_internal.published_tours pt
    LEFT JOIN LATERAL (
        SELECT q.id, q.evaluated_at, q.score_overall FROM silver_aa_internal.quality_scores q
        WHERE q.generated_content_id = pt.generated_content_id
        ORDER BY q.evaluated_at DESC, q.id DESC LIMIT 1
    ) latest ON true
    WHERE pt.tour_id = $1::uuid
"""

SQL_TOUR_JOB = """
    SELECT id, status, created_at, finished_at, error,
           result->>'quality_score_id' AS quality_score_id,
           result->>'published_repointed' AS published_repointed,
           result->>'revalidate_passed' AS revalidate_passed
    FROM shared.job
    WHERE kind = 'revalidate' AND payload->>'content_id' = $1
    ORDER BY created_at DESC LIMIT 1
"""


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (v if isinstance(v, (int, float, bool, type(None))) else str(v)) for k, v in row.items()}


async def _counts(conn: asyncpg.Connection) -> dict[str, int]:
    return {k: int(v) for k, v in dict(await conn.fetchrow(SQL_COUNTS)).items()}


async def _planned(conn: asyncpg.Connection, include_stale: bool) -> list[dict[str, Any]]:
    rows = [_jsonable(dict(r)) for r in await conn.fetch(SQL_TO_FIX)]
    if include_stale:
        # Same-version stale rows only: a pointer at another version's row is a different defect (report, no fix).
        rows += [_jsonable(dict(r)) for r in await conn.fetch(SQL_STALE)
                 if r["target_score_id"] is not None and r["pointer_same_version"]]
    return rows


async def dry_run(conn: asyncpg.Connection) -> dict[str, Any]:
    counts = await _counts(conn)
    rows = [_jsonable(dict(r)) for r in await conn.fetch(SQL_TO_FIX)]
    stale = [_jsonable(dict(r)) for r in await conn.fetch(SQL_STALE)]
    cand = await conn.fetchrow(SQL_CANDIDATE)
    return {
        "mode": "dry_run",
        "counts": counts,
        "to_fix_live": sum(1 for r in rows if not r["soft_deleted"]),
        "to_fix_soft_deleted": sum(1 for r in rows if r["soft_deleted"]),
        "to_fix": rows,
        "stale": stale,
        "revalidate_candidate": None if cand is None else {
            "tour_id": cand[0], "content_id": cand[1], "quality_score_id": cand[2],
            "endpoint": f"POST /admin/tours/{cand[0]}/generated/{cand[1]}/revalidate",
        },
    }


async def apply(conn: asyncpg.Connection, expect: int, restore_file: str | None,
                include_stale: bool = False) -> dict[str, Any]:
    before = await _planned(conn, include_stale)
    if len(before) != expect:
        raise SystemExit(f"refused: {len(before)} rows to change now, --expect {expect} (re-run the dry run)")
    restore = [{"published_id": r["published_id"], "previous_quality_score_id": r["quality_score_id"]}
               for r in before]
    if restore_file:
        with open(restore_file, "w", encoding="utf-8") as fh:
            json.dump({"issue": "AA-983",
                       "restore": "UPDATE gold_aa_internal.published_tours pt SET quality_score_id = "
                                  "r.previous_quality_score_id FROM <rows> r WHERE pt.id = r.published_id",
                       "published_ids": [r["published_id"] for r in restore], "rows": restore}, fh, indent=1)
    ids = [r["published_id"] for r in before]
    seen = [r["quality_score_id"] for r in before]
    async with conn.transaction():
        changed = await conn.fetch(SQL_APPLY, ids, seen)
        after = await _counts(conn)
        left = after["to_fix"] + (after["stale_pointer"] if include_stale else 0)
        if len(changed) != expect or left != 0:
            raise SystemExit(f"refused, rolled back: changed {len(changed)} (expected {expect}), {left} left")
    return {"mode": "apply", "include_stale": include_stale, "changed": len(changed),
            "rows": [dict(r) for r in changed],
            "restore_published_ids": [r["published_id"] for r in restore], "restore_rows": restore}


async def check_tour(conn: asyncpg.Connection, tour_id: str) -> dict[str, Any]:
    row = await conn.fetchrow(SQL_TOUR, tour_id)
    if row is None:
        return {"mode": "check_tour", "tour_id": tour_id, "published": None, "ok": False}
    pub = _jsonable(dict(row))
    job = await conn.fetchrow(SQL_TOUR_JOB, pub["generated_content_id"])
    job = None if job is None else _jsonable(dict(job))
    pointer_is_latest = pub["quality_score_id"] is not None and pub["quality_score_id"] == pub["latest_score_id"]
    job_ok = None
    if job is not None:
        job_ok = (job["status"] == "succeeded" and job["quality_score_id"] == pub["quality_score_id"]
                  and int(job["published_repointed"] or 0) >= 1)
    return {"mode": "check_tour", "tour_id": tour_id, "published": pub, "pointer_is_latest": pointer_is_latest,
            "last_revalidate_job": job, "job_matches_pointer": job_ok,
            "ok": pointer_is_latest and job_ok is not False}


MARK_BEGIN, MARK_END = "AA983-JSON-BEGIN", "AA983-JSON-END"


def render(out: dict[str, Any]) -> str:
    """The printed summary: JSON between two marker lines (cut out of an ECS exec transcript by extract())."""
    return f"{MARK_BEGIN}\n{json.dumps(out, indent=1, default=str)}\n{MARK_END}"


def extract(transcript: str) -> dict[str, Any]:
    """The JSON summary inside an ECS exec transcript (banners around it, CR line ends); ValueError if absent."""
    lines = transcript.replace("\r", "").split("\n")
    try:
        start, end = lines.index(MARK_BEGIN), lines.index(MARK_END)
    except ValueError:
        raise ValueError("no AA983-JSON block in the transcript") from None
    return json.loads("\n".join(lines[start + 1:end]))


async def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true", help="write (default: DRY RUN, SELECT only)")
    ap.add_argument("--expect", type=int, help="with --apply: the dry-run count of rows to change")
    ap.add_argument("--include-stale", action="store_true",
                    help="with --apply: also re-point `stale` rows that have a target "
                         "(expect = to_fix + stale_pointer)")
    ap.add_argument("--restore-file", help="with --apply: write the restore list here before the UPDATE")
    ap.add_argument("--tour", help="check one tour after a revalidation (SELECT only)")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL"), help="default: $DATABASE_URL")
    ap.add_argument("--no-ssl", action="store_true", help="local test databases only")
    args = ap.parse_args(argv)
    if not args.dsn:
        raise SystemExit("DATABASE_URL is not set")
    if args.apply and args.expect is None:
        raise SystemExit("--apply needs --expect <dry-run count>")
    if args.apply and args.tour:
        raise SystemExit("--tour is read-only; do not combine it with --apply")
    ssl = None if args.no_ssl else "require"

    conn = await asyncpg.connect(args.dsn, ssl=ssl)
    try:
        if args.tour:
            return await check_tour(conn, args.tour)
        out = await (apply(conn, args.expect, args.restore_file, args.include_stale) if args.apply
                     else dry_run(conn))
    finally:
        await conn.close()
    if args.apply:  # independent verify on a NEW connection
        conn = await asyncpg.connect(args.dsn, ssl=ssl)
        try:
            out["verify_after"] = await _counts(conn)
        finally:
            await conn.close()
    return out


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--extract":  # local: cut the JSON out of a saved transcript
        with open(sys.argv[2], encoding="utf-8") as fh:
            print(json.dumps(extract(fh.read()), indent=1))
    else:
        print(render(asyncio.run(main(sys.argv[1:]))))
