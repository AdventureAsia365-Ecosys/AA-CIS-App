"""AA-919 WM-20 — Release 0: turn the Dev replay report into the R0 evidence and the release-0 rows ($0, no model call).

Input: the post-phase report of `scripts/writing_memory/wm20_release0_replay.py` (ids and sha256 digests only). This
module does not trust the report's own summary: it re-derives every writer's verdict from the per-input rows.

  python -m services.writing_memory.release0 --report s3:scripts/out/wm20/post/<stamp>.json            # DRY RUN
  python -m services.writing_memory.release0 --report s3:… --apply [--actor agent:<session>]
        [--a1-v2-on-worker] [--allow-no-pre] [--approve --approval-ref <decision link>] [--activate] [--memory-on]

Verdict per writer (pass needs every point):
  * inputs >= --min (20) replayed without error;
  * 0 byte differences: memory off vs the release-0 view (both captured on the post image), and the post image vs
    the pre-deploy image (the `pre` report, same input ids) — the independent reference (AA-919 critic 1); a report
    without a pre phase passes only with --allow-no-pre, recorded in the run;
  * where the writer stores a prompt hash (A1 metadata.prompt_version, T9 writer_input.writer_prompt_version), at
    least one stored production prompt is reproduced exactly (the replay path is the production path); older hashes
    that differ are reported, not counted (the prompt changed since they were written);
  * A1: the S1 writer v2 is on where the A1 rewrites run (the worker task): read from the report, or confirmed with
    --a1-v2-on-worker (critic 29).

--apply, one transaction: one `r0_replay` run per writer in shared.prompt_eval_runs (pipeline writing_memory,
cost 0, details {inputs, byte_diffs, byte_diffs_none_vs_r0, byte_diffs_vs_pre, compared_with_pre, errors,
stored_hashes, stored_hash_match, prompt_sha256, report_key, git_sha}); for each passing writer the platform release
0 (content class empty, train kind baseline) is drafted if missing and gets its evidence: that r0 run + the writer's
AA-729 `baseline_import` run of the active golden set (WM-20 scope 3: the baseline numbers are release 0's eval).
Release 0 starts with `reverify = true` (prompt_version_mismatch): the AA-729 frozen outputs come from earlier
prompts (A1 before the S250 v2 writer, T9 before AA-790), so a fresh control run (WM-19, approved separately) is due.

Approval (ADR 0010 decision 1: one human click). The admin approves and activates on the Releases routes
(/admin/writing-memory/releases/{id}/approve, /activate). --approve / --activate / --memory-on are the release-0
exception of the ADR 0010 note: allowed only for the empty class, with `--approval-ref` naming the human decision;
approved_by is the agent (`agent:<session>`), never a person's staff id the agent did not hold.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from typing import Any, Optional

from services.writing_memory import releases as rel

SCHEMA = "wm20-release0-replay/1"
WRITERS = rel.WRITERS
STORED_HASH_WRITERS = ("a1", "t9_blog", "t9_social")

_BASELINE_RUN_SQL = """
    SELECT p.id::text FROM shared.prompt_eval_runs p
      JOIN shared.eval_golden_set s ON s.set_id = p.golden_set_id
     WHERE p.pipeline = 'writing_memory' AND p.run_kind = 'baseline_import' AND p.writer = $1
       AND p.verdict = 'baseline' AND p.finished_at IS NOT NULL AND s.name = $2 AND s.status = 'active'
     ORDER BY p.created_at DESC LIMIT 1
"""
_INSERT_RUN_SQL = """
    INSERT INTO shared.prompt_eval_runs (pipeline, prompt_version, tour_count, cost_usd, regression_detected, details,
                                         triggered_by, run_kind, writer, stop_reason, verdict, fingerprint, finished_at)
    VALUES ('writing_memory', $1, $2, 0, false, $3::jsonb, $4, 'r0_replay', $5, 'completed', $6, $7::jsonb, now())
    RETURNING id::text
"""


def verdicts(report: dict, *, min_inputs: int = rel.MIN_R0_INPUTS, allow_no_pre: bool = False,
             a1_v2_on_worker: bool = False) -> dict[str, dict]:
    """Per writer: {verdict pass | fail, reasons, details} from the report's per-input rows (never its summary)."""
    if report.get("schema") != SCHEMA or report.get("phase") != "post":
        raise ValueError(f"not a {SCHEMA} post report")
    rows = report.get("rows") or []
    pre = report.get("pre_key")
    out: dict[str, dict] = {}
    for w in WRITERS:
        mine = [r for r in rows if r.get("writer") == w]
        ok = [r for r in mine if "error" not in r]
        none_vs_r0 = sum(1 for r in ok if r.get("none_vs_r0_equal") is not True)
        compared = [r for r in ok if r.get("pre_system_sha256") is not None]
        vs_pre = sum(1 for r in compared if (r["system_sha256"], r["user_sha256"])
                     != (r["pre_system_sha256"], r["pre_user_sha256"]))
        stored = [r for r in ok if r.get("stored_hash")]
        stored_match = sum(1 for r in stored if r.get("stored_hash_match"))
        systems = Counter(r["system_sha256"] for r in ok)
        details: dict[str, Any] = {
            "inputs": len(ok), "errors": len(mine) - len(ok), "byte_diffs": none_vs_r0 + vs_pre,
            "byte_diffs_none_vs_r0": none_vs_r0, "byte_diffs_vs_pre": vs_pre if pre else None,
            "compared_with_pre": len(compared), "stored_hashes": len(stored), "stored_hash_match": stored_match,
            "prompt_sha256": systems.most_common(1)[0][0] if systems else None,
            "distinct_system_prompts": len(systems), "pre_key": pre, "git_sha": report.get("git_sha"),
            "flags": sorted({f for r in ok for f in (r.get("flags") or [])}),
            "sources": dict(Counter(r.get("source") for r in ok)), "cost_usd": 0,
        }
        reasons = []
        if len(ok) < min_inputs:
            reasons.append(f"{len(ok)} inputs replayed (< {min_inputs})")
        if details["byte_diffs"]:
            reasons.append(f"{details['byte_diffs']} byte differences")
        if not pre:
            if not allow_no_pre:
                reasons.append("no pre-deploy reference (run the pre phase on the old image, or --allow-no-pre)")
        elif len(compared) < min_inputs:
            reasons.append(f"only {len(compared)} inputs compared with the pre-deploy reference")
        if w in STORED_HASH_WRITERS and stored and stored_match == 0:
            reasons.append("no stored production prompt hash is reproduced")
        if w == "a1" and not (report.get("a1_v2_enabled_here") or a1_v2_on_worker):
            reasons.append("the S1 writer v2 is not confirmed on for A1 (--a1-v2-on-worker)")
        out[w] = {"verdict": "fail" if reasons else "pass", "reasons": reasons, "details": details}
    return out


def merge_pre(post: dict, pre: Optional[dict]) -> dict:
    """Copy the pre digests onto the post rows (same input id) so the verdict reads one document."""
    if not pre:
        return post
    by_id = {r["input_id"]: r for r in pre.get("rows") or [] if "error" not in r}
    for r in post.get("rows") or []:
        p = by_id.get(r.get("input_id"))
        if p and r.get("writer") == p.get("writer"):
            r["pre_system_sha256"], r["pre_user_sha256"] = p["system_sha256"], p["user_sha256"]
    return post


async def apply(conn, report: dict, results: dict[str, dict], *, actor: str, report_key: Optional[str],
                approve: bool = False, approval_ref: Optional[str] = None, activate: bool = False,
                memory_on: bool = False) -> dict:
    """Write the r0 runs and release 0 per passing writer, in the caller's transaction."""
    out: dict[str, Any] = {}
    for w in WRITERS:
        res = results[w]
        details = {**res["details"], "reasons": res["reasons"], "report_key": report_key}
        fp = await rel.current_fingerprint(conn, w, prompt_sha256=details["prompt_sha256"])
        run_id = await conn.fetchval(_INSERT_RUN_SQL, (details["prompt_sha256"] or "none")[:8], details["inputs"],
                                     json.dumps(details), actor[:128], w, res["verdict"], json.dumps(fp))
        entry: dict[str, Any] = {"r0_run_id": run_id, "verdict": res["verdict"], "reasons": res["reasons"]}
        out[w] = entry
        if res["verdict"] != "pass":
            continue
        baseline = await conn.fetchval(_BASELINE_RUN_SQL, w, rel.GOLDEN_SET)
        if baseline is None:
            entry["release"] = "no baseline_import run of the active golden set (run the WM-17 importer first)"
            continue
        r0 = await conn.fetchrow("SELECT release_id::text, status FROM shared.writing_memory_release "
                                 "WHERE writer = $1 AND scope = 'platform' AND release_no = 0", w)
        if r0 is None:
            draft = await rel.create_draft(conn, writer=w, created_by=actor, prompt_sha256=details["prompt_sha256"],
                                           notes="WM-20 release 0 (empty memory)")
            await conn.execute("UPDATE shared.writing_memory_release SET reverify = true, reverify_at = now(), "
                               "reverify_reasons = ARRAY['prompt_version_mismatch'] WHERE release_id = $1::uuid",
                               draft["release_id"])
            r0 = {"release_id": draft["release_id"], "status": draft["status"]}
        entry["release_id"] = r0["release_id"]
        if r0["status"] in ("draft", "evaluated"):
            row = await rel.attach_evidence(conn, r0["release_id"], r0_run_id=run_id, eval_run_id=baseline,
                                            actor=actor)
            r0 = {"release_id": row["release_id"], "status": row["status"]}
        if approve and r0["status"] == "evaluated":
            row = await rel.approve(conn, r0["release_id"], approved_by=actor, approval_ref=approval_ref,
                                    allow_agent=True)
            r0["status"] = row["status"]
        if activate and r0["status"] == "approved":
            row = await rel.activate(conn, r0["release_id"], actor=actor)
            r0["status"] = row["status"]
        if memory_on and r0["status"] == "active":
            await rel.set_memory_enabled(conn, w, True, actor=actor)
            entry["memory_enabled"] = True
        entry["status"] = r0["status"]
    return out


def _read(ref: str) -> dict:
    if ref.startswith("s3://"):
        raise SystemExit("give the bronze key as s3:<key>; the bucket comes from the environment (BRONZE_BUCKET)")
    if ref.startswith("s3:"):
        import boto3

        from shared.aws_client.bronze import bronze_bucket
        body = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1")).get_object(
            Bucket=bronze_bucket(), Key=ref[3:].lstrip("/"))["Body"].read()
        return json.loads(body)
    with open(ref) as f:
        return json.load(f)


async def _main(args) -> dict:
    import asyncpg

    from shared.secrets import get_database_url

    report = _read(args.report)
    if report.get("pre_key"):
        report = merge_pre(report, _read(report["pre_key"]))
    results = verdicts(report, min_inputs=args.min, allow_no_pre=args.allow_no_pre,
                       a1_v2_on_worker=args.a1_v2_on_worker)
    out: dict[str, Any] = {"mode": "apply" if args.apply else "dry_run", "report": args.report,
                           "verdicts": {w: {"verdict": r["verdict"], "reasons": r["reasons"],
                                            **{k: r["details"][k] for k in ("inputs", "byte_diffs",
                                                                            "compared_with_pre", "stored_hashes",
                                                                            "stored_hash_match")}}
                                        for w, r in results.items()}}
    if not args.apply:
        return out
    ssl: Any = None if os.environ.get("WM20_DB_SSL", "require") == "disable" else "require"
    conn = await asyncpg.connect(get_database_url().strip(), ssl=ssl)
    try:
        async with conn.transaction():
            out["applied"] = await apply(conn, report, results, actor=args.actor, report_key=args.report,
                                         approve=args.approve, approval_ref=args.approval_ref,
                                         activate=args.activate, memory_on=args.memory_on)
    finally:
        await conn.close()
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="AA-919 WM-20 release 0 from the replay report ($0)")
    ap.add_argument("--report", required=True, help="post report: path or s3:<key>")
    ap.add_argument("--min", type=int, default=rel.MIN_R0_INPUTS)
    ap.add_argument("--apply", action="store_true", help="write the r0 runs and release 0 (default: dry run)")
    ap.add_argument("--actor", default="agent:wm20-release0", help="agent:<session> (never a person's staff id)")
    ap.add_argument("--allow-no-pre", action="store_true")
    ap.add_argument("--a1-v2-on-worker", action="store_true",
                    help="S1_WRITER_V2 is on in the worker task definition (checked by the operator)")
    ap.add_argument("--approve", action="store_true", help="ADR 0010 note: release 0 only, needs --approval-ref")
    ap.add_argument("--approval-ref", help="the link of the human decision that approves release 0")
    ap.add_argument("--activate", action="store_true")
    ap.add_argument("--memory-on", action="store_true")
    args = ap.parse_args(argv)
    if not args.actor.startswith("agent:"):
        raise SystemExit("--actor must be agent:<session>: the CLI never signs as a person")
    if args.approve and not args.approval_ref:
        raise SystemExit("--approve needs --approval-ref (the human decision)")
    sys.path.insert(0, "/app")
    from services.golden_set import llm_guard
    llm_guard.install("wm20_release0")
    print(json.dumps(asyncio.run(_main(args)), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
