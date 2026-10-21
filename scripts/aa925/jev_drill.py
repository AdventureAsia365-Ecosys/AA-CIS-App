#!/usr/bin/env python3
"""AA-925 (Q-2) Done-when 3 — a forced Jev failure holds the A1 output and increments ``degraded``, on Dev.

Runs on the DEPLOYED image (after the PR is merged and deployed) as a one-off ECS task, with the container's
DATABASE_URL (role cis_app) and ssl=require:

  cd /app && PYTHONPATH=/tmp/aa925 python3 -m scripts.aa925.jev_drill [--tour-id UUID] [--max-cost-usd 0.5]
      [--regenerate] [--no-upload]

1. ``scripts.aa729.evalpatch.install(run, stage="adhoc_aa925")``: every model call is logged under
   ``adhoc_aa925`` (real stage in quality_signal.aa729_stage), every Jev verdict under ``adhoc_aa925:<stage>``,
   no lesson write, no shadow call, and an in-call cost stop at ``--max-cost-usd`` (default $0.50).
   The generated version is never stored: the drill calls ``_rewrite_tour`` directly, which returns the
   result without a database write (the admin router's persist step is not run).
2. Picks ONE published A1 tour (Set A of day_oracle_report) whose stored output has source day markers,
   ≥ 8 checkable sentences and ZERO deterministic numeric hits under ``check_grounding(use_jev=False)`` — so
   any hold must come from the degraded detector, not from UNSUPPORTED_NUMBER (on the old code such a tour
   publishes).
3. Trips the Jev credit breaker directly (``decide._trip_jev_breaker()``): every a1_claim_supported verdict is
   zone=error. It never fakes a 402 — that path calls ``maybe_alert_jev_credit``, a real alert. The breaker is
   process-local and is reset at the end. The verdict cache is bypassed for the drill (``use_cache=False``):
   the stored sentences were judged before, and a cached verdict skips the breaker — correctly, in
   production; the drill stands for fresh text.
4. Runs the real ``v1_pipeline._rewrite_tour``: by default on the stored output as the batch writer text
   (``batch_text``: the production Bedrock-Batch path, no writer call unless the gate sends it to a retry);
   ``--regenerate`` writes the tour again live (~$0.06–0.12).
5. Asserts: ``degraded`` has ``jev_unavailable`` with count > 0; ``DEGRADED_DETECTOR`` is in failure_codes;
   ``brand_audit_status == "manual_check"`` and ``_is_publishable`` is False; ``grounding_violations == []``.
   The T9 variant (an F9 judge outage holds the piece) is the Q0 case ``t9-f9-outage-held``.

Output: s3://<bronze>/scripts/out/aa925/<run>/jev-drill.json (bucket name built at runtime) + a local copy and a
stdout summary. Exit code 0 only when every assertion holds. The CloudWatch ``quality_degraded`` event of the
run is the second piece of evidence (log group from the task definition's logConfiguration).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.getcwd())

S3_PREFIX = "scripts/out/aa925"
STAGE = "adhoc_aa925"
MIN_SENTENCES = 8
CANDIDATE_SQL = """
    SELECT gc.id::text AS gc_id, gc.tour_id::text AS tour_id, gc.aa_name, gc.aa_subtitle, gc.aa_summary,
           gc.aa_highlights, gc.aa_itineraries, gc.seo_title, gc.seo_meta,
           rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description, rt.src_highlights, rt.src_itineraries,
           rt.country, rt.duration, rt.price_raw, rt.inclusions, rt.exclusions
    FROM gold_aa_internal.published_tours pt
    JOIN silver_aa_internal.generated_content gc ON gc.id = pt.generated_content_id
    JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id AND rt.deleted_at IS NULL
    WHERE pt.deleted_at IS NULL AND ($1::uuid IS NULL OR pt.tour_id = $1::uuid)
"""
_SPEND_SQL = f"""
    SELECT COALESCE(sum(cost_usd), 0)::float FROM shared.llm_call_log
    WHERE stage = '{STAGE}' AND created_at >= $1 AND quality_signal->>'aa729_run' = $2
"""


def writer_json(row: dict) -> dict:
    """The stored version in the batch writer's JSON shape (seed_generated_node parses it)."""
    from services.content_generation.grounding import _json_list

    return {"name": row.get("aa_name") or row.get("src_name"), "subtitle": row.get("aa_subtitle"),
            "summary": row.get("aa_summary"), "highlights": _json_list(row.get("aa_highlights")),
            "itineraries": row.get("aa_itineraries"), "seo_title": row.get("seo_title"),
            "seo_meta": row.get("seo_meta")}


def eligible(row: dict) -> Optional[dict]:
    """The selection facts of one candidate, or None when it does not qualify."""
    from services.content_generation import grounding
    from services.content_generation.day_parse import source_day_markers

    if not source_day_markers(row.get("src_itineraries") or ""):
        return None
    tour, gen = grounding.tour_from_row(row), grounding.generated_from_row(row)
    units = grounding.sentence_units(gen)
    if len(units) < MIN_SENTENCES:
        return None
    res = grounding.check_grounding(gen, tour, use_jev=False)
    if res["violations"]:
        return None
    return {"units": len(units), "numeric_hits": 0}


def assertions(result: dict) -> dict[str, bool]:
    from api.routers.admin_pipeline import _is_publishable
    from services.quality import degraded

    counts = degraded.counts(result.get("degraded"))
    return {
        "degraded_has_jev_unavailable": counts.get(degraded.JEV_UNAVAILABLE, 0) > 0,
        "failure_codes_has_DEGRADED_DETECTOR": degraded.HOLD_CODE in (result.get("failure_codes") or []),
        "brand_audit_status_manual_check": result.get("brand_audit_status") == "manual_check",
        "not_publishable": not _is_publishable(result),
        "no_grounding_violations": not (result.get("grounding_violations") or []),
    }


async def run(args) -> dict:
    import asyncpg

    from api.routers.v1_pipeline import _rewrite_tour
    from scripts.aa729 import evalpatch
    from services.content_generation import grounding
    from shared.llm_client import call_log, decide

    run_id = str(uuid.uuid4())
    started = datetime.now(timezone.utc)
    evalpatch.install(run_id, stage=STAGE)
    evalpatch.BUDGET.configure(args.max_cost_usd, slots=1)

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        rows = [dict(r) for r in await conn.fetch(CANDIDATE_SQL, args.tour_id)]
    finally:
        await conn.close()
    random.Random(925).shuffle(rows)
    picked, facts = None, None
    for row in rows:
        facts = eligible(row)
        if facts:
            picked = row
            break
    if picked is None:
        raise SystemExit("no published A1 tour with day markers, >= 8 sentences and 0 numeric hits")
    tour = grounding.tour_from_row(picked)
    tour["tour_id"] = picked["tour_id"]
    print(f"tour {picked['tour_id']} ({tour.get('country')}, {tour.get('duration')}): {facts}", flush=True)

    real_decide = decide._decide

    async def _decide_uncached(db, stage, subject_key, state, question_keys, tenant_id, use_cache=True):
        return await real_decide(db, stage, subject_key, state, question_keys, tenant_id, use_cache=False)

    decide.reset_decision_memo()
    decide._decide = _decide_uncached   # a cached verdict would skip the breaker (see the docstring)
    decide._trip_jev_breaker()
    job_id = str(uuid.uuid4())
    try:
        with call_log.bind_job(job_id):
            if args.regenerate:
                result = await _rewrite_tour(tour, 0, 1, brand_rules={}, seo={}, model_tier=None)
            else:
                result = await _rewrite_tour(tour, 0, 1, brand_rules={}, seo={}, model_tier=None,
                                             batch_text=json.dumps(writer_json(picked)),
                                             batch_model_used="aa925-drill-stored")
    finally:
        decide.reset_jev_breaker()
        decide._decide = real_decide

    await asyncio.sleep(2.0)   # fire-and-forget log writes
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        spend = float(await conn.fetchval(_SPEND_SQL, started, run_id) or 0.0)
        zones = [dict(r) for r in await conn.fetch(
            "SELECT question_key, zone, count(*)::int AS n FROM shared.decision_log "
            "WHERE created_at >= $1 AND job_id = $2::uuid GROUP BY 1, 2", started, job_id)]
    finally:
        await conn.close()

    checks = assertions(result)
    return {
        "schema": "aa925-jev-drill", "run_id": run_id, "job_id": job_id, "started_at": started.isoformat(),
        "mode": "regenerate" if args.regenerate else "stored_output_batch_path",
        "tour_id": picked["tour_id"], "gc_id": picked["gc_id"], "country": tour.get("country"),
        "selection": facts, "status": result.get("status"),
        "degraded": result.get("degraded") or [], "failure_codes": result.get("failure_codes") or [],
        "brand_audit_status": result.get("brand_audit_status"),
        "grounding_violations": len(result.get("grounding_violations") or []),
        "decision_log_zones": zones, "spend_usd_llm_call_log": round(spend, 6),
        "local_llm_usd": round(evalpatch.SPEND.usd, 6), "cap_usd": args.max_cost_usd,
        "cost_cap_tripped": evalpatch.BUDGET.tripped,
        "assertions": checks, "passed": all(checks.values()),
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tour-id", help="drill this published tour (must still qualify)")
    ap.add_argument("--max-cost-usd", type=float, default=0.5)
    ap.add_argument("--regenerate", action="store_true", help="write the tour again live instead of the stored text")
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args(argv)
    report: dict[str, Any] = asyncio.run(run(a))
    from scripts.aa729.common import save_output

    path = save_output(report, f"{S3_PREFIX}/{report['run_id']}/jev-drill.json", local_dir="/tmp/aa925-out",
                       upload=not a.no_upload)
    print(json.dumps({k: report[k] for k in ("run_id", "tour_id", "mode", "degraded", "failure_codes",
                                             "brand_audit_status", "grounding_violations", "spend_usd_llm_call_log",
                                             "assertions", "passed")}, indent=1, default=str), flush=True)
    print(f"local={path}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
