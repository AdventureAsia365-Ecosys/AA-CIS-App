"""
services/eval/regression.py — AA-289 Part B: on-demand prompt-regression eval gate.

Runs the REAL S1-old pipeline (v1_pipeline._rewrite_tour) against a fixed tour set, tags the
result with prompt_version, and compares against the most recent PRIOR shared.prompt_eval_runs
row for that pipeline with a DIFFERENT prompt_version (its baseline). Writes its own result row
either way — the first-ever run for a pipeline has nothing to compare against yet and just
establishes a baseline (not a failure).

(AA-611: the parallel S1-from-atom pipeline was removed — that writer was dead code with no
real callers, so its eval branch and fixture tour set are gone with it.)

ON-DEMAND ONLY (AA-289 explicit constraint): this module has no scheduler of its own. It is
invoked either directly (python -m services.eval.regression) via ECS exec, or by
.github/workflows/eval-regression.yml, which is workflow_dispatch-only — no `schedule:`
trigger. Real LLM cost every run (Bedrock Haiku/GPT-4.1 for S1-old) — do not wire this into
any push/PR trigger without AA-287 Budgets alarms in place first (Done as of this PR, but
"done" != "wire this to run automatically" per the issue's own explicit warning).

Fixture sourcing (AA-289 STEP 0 findings):
- S1-old: CIS_Golden_Tours_20_v1.xlsx, uploaded to
  s3://aa-cis-bronze-005097885195/fixtures/ (the repo's data/ dir is gitignored — the file
  is not committed, so a GitHub-Actions-triggered run has no local copy to read).
"""
import argparse
import asyncio
import io
import json
import os
import sys

import asyncpg
import boto3
import openpyxl
import structlog

logger = structlog.get_logger()

GOLDEN_TOURS_S3_BUCKET = "aa-cis-bronze-005097885195"
GOLDEN_TOURS_S3_KEY = "fixtures/CIS_Golden_Tours_20_v1.xlsx"
AWS_REGION = os.environ.get("AWS_REGION", "us-west-1")

# AA-289: absolute-point drop on a 0-10 scale. Same order of magnitude as graph.py's own
# MISSING_FIELD_CAP (4.0) / a single hard-block deduction — chosen so the gate fires on a
# real, single-issue-class regression (e.g. AA-231/AA-195's precedent bugs), not on the
# ordinary run-to-run noise of an LLM call.
S1_OLD_REGRESSION_THRESHOLD = 1.0

# AA-353: >=9 days is AA-339's own "dài-dày" (long-dense) threshold, quoted directly from that
# issue's Lane A description ("Tour dài (>9-10 ngày, chi tiết)"). AA-339 also used a human
# categorical medium/thin split on a dedicated 30-tour set that is not in this repo and can't be
# reconstructed from a formula — its own word-count-based "rich" heuristic
# (words>=400 OR (days>=4 AND words_per_day>=35)) was flagged in that same issue as suspect and
# never confirmed. Rather than invent a new boundary, this uses 2 buckets only: "long"
# (AA-339-sourced) and "other" — see docs/implementation-notes/AA-353.md "Should know".
ITINERARY_LONG_TOUR_DAY_THRESHOLD = 9

ITINERARY_BASELINE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "baselines", "itinerary_compression_baseline.json",
)


def _download_golden_tours() -> list[dict]:
    s3 = boto3.client("s3", region_name=AWS_REGION)
    obj = s3.get_object(Bucket=GOLDEN_TOURS_S3_BUCKET, Key=GOLDEN_TOURS_S3_KEY)
    wb = openpyxl.load_workbook(io.BytesIO(obj["Body"].read()), data_only=True)
    ws = wb["Golden Tours"]
    header = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    tours = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is None:
            continue
        rec = dict(zip(header, row))
        highlights = [h.strip() for h in (rec.get("highlights") or "").split("\n") if h.strip()]
        tours.append({
            "name":        rec.get("name") or "",
            "subtitle":    rec.get("subtitle") or "",
            "summary":     rec.get("summary") or "",
            "description": "",  # not present in the golden fixture — no equivalent field
            "highlights":  highlights,
            "itineraries": rec.get("itinerary_summary") or "",
            "country":     rec.get("country") or "",
            "duration":    rec.get("duration") or "",
            "price":       rec.get("price_usd") or "",
            "inclusions":  rec.get("inclusions") or "",
            "exclusions":  "",
            "_tour_id":    rec.get("tour_id"),  # golden-set id, not a real raw_tours UUID
        })
    return tours


def _itinerary_compression_summary(results: list[dict]) -> dict:
    """AA-353: worst (most-compressed) per-day actual/source word-count ratio across a run,
    grouped by ITINERARY_LONG_TOUR_DAY_THRESHOLD. "Worst" = min ratio, matching AA-339/AA-346's
    own framing (the failure mode this guards is COMPRESSION, not padding — over-length days are
    not what AA-353 was written to catch)."""
    buckets = {"long": [], "other": []}
    total_days = 0
    nudged_days = 0
    for r in results:
        day_ratios = r.get("itinerary_day_ratios") or []
        if not day_ratios:
            continue
        bucket = "long" if len(day_ratios) >= ITINERARY_LONG_TOUR_DAY_THRESHOLD else "other"
        ratios = [d["ratio"] for d in day_ratios if d.get("ratio") is not None]
        if ratios:
            buckets[bucket].append(min(ratios))
        total_days += len(day_ratios)
        nudged_days += sum(1 for d in day_ratios if d.get("nudged"))

    return {
        "long_worst_ratio":  round(min(buckets["long"]), 3) if buckets["long"] else None,
        "long_tour_count":   len(buckets["long"]),
        "other_worst_ratio": round(min(buckets["other"]), 3) if buckets["other"] else None,
        "other_tour_count":  len(buckets["other"]),
        "total_day_count":   total_days,
        "nudged_day_count":  nudged_days,
        "nudge_rate":        round(nudged_days / total_days, 3) if total_days else None,
    }


def _load_itinerary_baseline():
    if not os.path.exists(ITINERARY_BASELINE_PATH):
        return None
    with open(ITINERARY_BASELINE_PATH) as f:
        return json.load(f)


def _write_itinerary_baseline(summary: dict) -> None:
    os.makedirs(os.path.dirname(ITINERARY_BASELINE_PATH), exist_ok=True)
    with open(ITINERARY_BASELINE_PATH, "w") as f:
        json.dump(summary, f, indent=2, sort_keys=True)
        f.write("\n")


def _detect_itinerary_regression(current_summary: dict) -> bool:
    """AA-353: current run's worst-day-ratio (per bucket) must not be LOWER (more compressed)
    than the committed baseline file — AA-353's whole point is to IMPROVE this number, not just
    hold it steady, per the issue's own explicit instruction. No baseline captured yet, or a
    bucket that's empty in both runs (None vs None), is never a regression."""
    baseline = _load_itinerary_baseline()
    if baseline is None:
        return False
    for bucket in ("long", "other"):
        cur = current_summary.get(f"{bucket}_worst_ratio")
        base = baseline.get(f"{bucket}_worst_ratio")
        if cur is None or base is None:
            continue
        if cur < base:
            return True
    return False


async def run_s1_old_eval() -> dict:
    """Runs every Golden Tour through the REAL old-S1 LangGraph pipeline (generate -> validate
    -> llm_judge -> brand_audit -> flag_fix -> revalidate), no brand_rules (default AA voice —
    judge_node skips its GPT-4.1 call with no differentiation profile; brand_audit_node still
    runs its own GPT-4.1 call for legacy/no-profile brands, per its own docstring), seo={}
    (skip DataForSEO — an eval run scores writing quality, not live keyword data)."""
    from api.routers.v1_pipeline import _rewrite_tour

    tours = _download_golden_tours()
    results = []
    for idx, tour in enumerate(tours):
        result = await _rewrite_tour(
            tour, idx=idx, total=len(tours), brand_rules={}, seo={}, model_tier="haiku",
        )
        results.append(result)
        logger.info("eval_s1_old_tour_done", idx=idx, name=tour["name"],
                    quality_score=result.get("quality_score"), status=result.get("status"))

    scored = [r for r in results if r.get("status") == "success"]
    prompt_versions = {r.get("prompt_version") for r in scored if r.get("prompt_version")}
    if len(prompt_versions) > 1:
        logger.warning("eval_s1_old_prompt_version_mismatch", versions=list(prompt_versions))

    avg_score = round(sum(r["quality_score"] for r in scored) / len(scored), 3) if scored else None
    total_cost = round(sum(r.get("cost_usd", 0.0) for r in results), 4)
    return {
        "pipeline": "s1_old",
        "prompt_version": next(iter(prompt_versions), None) or "",
        "tour_count": len(tours),
        "avg_quality_score": avg_score,
        "cost_usd": total_cost,
        "details": {
            "scored_count": len(scored),
            "failed_count": len(results) - len(scored),
            "per_tour": [
                {"name": r.get("src_name"), "quality_score": r.get("quality_score"),
                 "status": r.get("status"),
                 "itinerary_day_count": len(r.get("itinerary_day_ratios") or [])}
                for r in results
            ],
            # AA-353: worst-day-ratio regression signal — see _itinerary_compression_summary and
            # _detect_itinerary_regression (compared against ITINERARY_BASELINE_PATH, separate
            # from this table's own prompt_version-based regression check above).
            "itinerary_compression": _itinerary_compression_summary(results),
        },
    }


async def _get_baseline(conn, pipeline: str, current_prompt_version: str):
    """Returns a plain dict (not an asyncpg.Record) so avg_quality_score can be normalized to a
    Python float right here, at the read point — Postgres NUMERIC comes back as decimal.Decimal,
    while every place that computes a CURRENT avg_quality_score (run_s1_old_eval, this module's
    own round(sum(...)/len(...), 3)) produces a plain float. Mixing the two in an arithmetic
    comparison (_detect_regression's s1_old branch) raises TypeError — fixed at the source instead
    of only at that one call site, since any other future consumer of this baseline would hit the
    same mismatch otherwise."""
    row = await conn.fetchrow(
        """
        SELECT prompt_version, avg_quality_score, avg_words_per_citation, gate_pass_count
        FROM shared.prompt_eval_runs
        WHERE pipeline = $1 AND prompt_version != $2
        ORDER BY created_at DESC LIMIT 1
        """,
        pipeline, current_prompt_version,
    )
    if row is None:
        return None
    baseline = dict(row)
    if baseline["avg_quality_score"] is not None:
        baseline["avg_quality_score"] = float(baseline["avg_quality_score"])
    if baseline["avg_words_per_citation"] is not None:
        baseline["avg_words_per_citation"] = float(baseline["avg_words_per_citation"])
    return baseline


def _detect_regression(pipeline: str, current: dict, baseline) -> bool:
    if baseline is None:
        return False  # first-ever run for this pipeline — nothing to regress against
    # s1_old: an absolute-point drop in avg_quality_score beyond the threshold is a regression.
    if current["avg_quality_score"] is None or baseline["avg_quality_score"] is None:
        return False
    return (baseline["avg_quality_score"] - current["avg_quality_score"]) > S1_OLD_REGRESSION_THRESHOLD


async def _write_eval_run(conn, result: dict, baseline, regression: bool, triggered_by: str) -> None:
    await conn.execute(
        """
        INSERT INTO shared.prompt_eval_runs (
            pipeline, prompt_version, tour_count, avg_quality_score, avg_words_per_citation,
            gate_pass_count, gate_fail_count, cost_usd, regression_detected,
            baseline_prompt_version, baseline_avg_quality_score, details, triggered_by
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13)
        """,
        result["pipeline"], result["prompt_version"], result["tour_count"],
        result.get("avg_quality_score"), result.get("avg_words_per_citation"),
        result.get("gate_pass_count"), result.get("gate_fail_count"), result.get("cost_usd"),
        regression, baseline["prompt_version"] if baseline else None,
        baseline["avg_quality_score"] if baseline else None,
        json.dumps(result.get("details", {}), default=str), triggered_by,
    )


async def run_eval(pipeline: str, triggered_by: str = "manual",
                    capture_itinerary_baseline: bool = False) -> dict:
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=2)
    try:
        if pipeline == "s1_old":
            result = await run_s1_old_eval()
        else:
            raise ValueError(f"Unknown pipeline: {pipeline!r}")

        async with pool.acquire() as conn:
            baseline = await _get_baseline(conn, pipeline, result["prompt_version"])
            regression = _detect_regression(pipeline, result, baseline)
            await _write_eval_run(conn, result, baseline, regression, triggered_by)

        result["baseline"] = dict(baseline) if baseline else None
        result["regression_detected"] = regression

        # AA-353: separate from the prompt_version-keyed regression check above — a file-committed
        # baseline (not a DB row), only meaningful for s1_old (only pipeline with itineraries).
        if pipeline == "s1_old":
            itin_summary = result["details"]["itinerary_compression"]
            if capture_itinerary_baseline:
                _write_itinerary_baseline(itin_summary)
                result["itinerary_baseline_captured"] = True
                result["itinerary_regression_detected"] = False
            else:
                result["itinerary_regression_detected"] = _detect_itinerary_regression(itin_summary)
        return result
    finally:
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="AA-289 on-demand prompt regression eval gate")
    parser.add_argument("--pipeline", choices=["s1_old"], default="s1_old")
    parser.add_argument("--triggered-by", default="manual")
    parser.add_argument(
        "--capture-itinerary-baseline", action="store_true",
        help="AA-353: write this run's worst-day-ratio numbers as the new committed baseline "
             "(eval/baselines/itinerary_compression_baseline.json) instead of comparing against "
             "it. Only applies to --pipeline s1_old/both. Use for an intentional, reviewed "
             "re-baseline — not routine runs.",
    )
    args = parser.parse_args()

    result = asyncio.run(run_eval(
        args.pipeline, triggered_by=args.triggered_by,
        capture_itinerary_baseline=args.capture_itinerary_baseline,
    ))
    print(json.dumps(result, indent=2, default=str))
    any_regression = result["regression_detected"]
    any_regression = any_regression or result.get("itinerary_regression_detected", False)

    return 1 if any_regression else 0


if __name__ == "__main__":
    sys.exit(main())
