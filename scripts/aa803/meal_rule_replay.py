#!/usr/bin/env python3
"""AA-803 (S247) — replay the structured meal rule over the stored S1 versions. Read-only, $0, no Jev, no LLM.

Run AFTER the deploy of the meal-rule PRs (the image's own ``brand_audit_node`` / ``meal_claims`` are used; the
script stops if they are missing). Inside the api container, with the container's DATABASE_URL (role cis_app,
SELECT on silver_aa_internal), ssl=require, the session set READ ONLY. The image has no scripts/, so the file is
fetched by a presigned URL (S3-mediated ECS exec, see the orchestrator steps) and run from /app:

    cd /app && python3 /tmp/meal_rule_replay.py [--days 30] [--no-upload]

Set: every master-tenant generated_content version written in the last ``--days`` days whose grounding ran and
that no human edited (the J04 set of S246: 1,413 versions / 914 tours on 21/10/2026), with its newest
quality_scores row.

Per version:
  * ``old_units``  — the meal-claim sentences the pre-AA-803 code asked Jev about (old regex, frozen below, over
    ``grounding.sentence_units``): the Jev meal questions of the old code.
  * ``claims``     — the meal-claim sentences of the new detector (``itinerary_claim_sentences`` +
    ``ITIN_MEAL_INVENTED``), each run through ``meal_claims.claim_sourced``.
  * ``jev_units``  — the claims the rule does not clear and that are askable: the Jev meal questions now.
  * ``waived_by_stored_code`` — the J04 measure: the stored audit has no ITINERARY_MEAL_TIME_INVENTED although the
    itinerary has an old-regex meal hit and every clock time is in the source, on a version flag_fix did not
    rewrite the itinerary of (comparable). Only a Jev accept explains that: a Jev waiver.
  * ``still_needs_jev`` — a waived version whose meal claims the rule does not all clear: the code would fire
    there once a1_claim_supported is OFF.

Output: totals, the waiver share before (tours waived / tours) and after (tours still needing Jev / tours) against
the 5 % bar, Jev meal units removed, not-cleared reasons, and up to 40 samples of each kind (tour id, version,
day, sentence, reason) for the private root audit folder. Written to s3://<bronze>/scripts/out/aa803/<run>.json
(bucket name built at runtime from the task's account) and /tmp/aa803-out/, plus a one-line stdout summary.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.getcwd())

MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"
MEAL_CODE = "ITINERARY_MEAL_TIME_INVENTED"
WAIVER_BAR = 0.05
SAMPLES = 40
S3_PREFIX = "scripts/out/aa803"

# The claim regex of the code before AA-803 (brand_audit_node, origin/main 47ae118f), frozen here so the "old Jev
# meal questions" stay comparable with the S246 J04 run after the new regex is deployed.
OLD_ITIN_MEAL_INVENTED = re.compile(
    r'(?:'
    r'\bmeals?\s*(?:included|provided)?\s*[:\-]'
    r'|\((?:\s*[BLD](?:\s*[,/&]\s*[BLD]){0,2}\s*)\)'
    r'|\b(?:breakfast|lunch|dinner)\s+(?:is\s+)?(?:included|provided|served|arranged)\b'
    r')',
    re.IGNORECASE,
)

VERSIONS_SQL = """
    SELECT gc.id::text AS gc_id, gc.tour_id::text AS tour_id, gc.version_num, gc.created_at,
           gc.fix_pass_applied, gc.fix_pass_fields, gc.aa_subtitle, gc.aa_summary, gc.aa_highlights,
           gc.aa_itineraries,
           rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description, rt.src_highlights,
           rt.src_itineraries, rt.country, rt.duration, rt.price_raw, rt.inclusions, rt.exclusions,
           qs.brand_audit_codes
      FROM silver_aa_internal.generated_content gc
      JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = gc.tour_id
      LEFT JOIN LATERAL (
            SELECT q.brand_audit_codes
              FROM silver_aa_internal.quality_scores q
             WHERE q.generated_content_id = gc.id
             ORDER BY q.evaluated_at DESC
             LIMIT 1) qs ON true
     WHERE gc.tenant_id = $1::uuid
       AND gc.created_at >= now() - make_interval(days => $2)
       AND (gc.metadata ? 'grounding')
       AND NOT gc.human_edited
     ORDER BY gc.created_at, gc.id
"""

FLAG_FIX_COST_SQL = """
    SELECT count(*) AS n, avg(cost_usd)::float AS avg_cost
      FROM shared.llm_call_log
     WHERE stage = 's1_flag_fix' AND created_at >= now() - make_interval(days => $1)
       AND quality_signal -> 'fields_requested' = '["itineraries"]'::jsonb
"""


def _j(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _modules():
    """The deployed detector and rule. Stops when the image predates the meal-rule PRs."""
    from services.content_generation import brand_audit_node as ba
    from services.content_generation import grounding as g

    try:
        from services.content_generation import meal_claims as mc
    except ImportError as e:  # pragma: no cover - only on an old image
        raise SystemExit(f"meal_claims is not in this image ({e}): deploy the AA-803 meal-rule PRs first")
    if not hasattr(ba, "itinerary_claim_sentences"):
        raise SystemExit("brand_audit_node.itinerary_claim_sentences missing: deploy the AA-803 detector PR first")

    def _no_jev(*_a, **_k):
        raise RuntimeError("aa803 meal replay is read-only: Jev must never be called")

    g.judge_units = _no_jev
    g._judge_all = _no_jev
    return ba, g, mc


def classify_version(row: dict, ba, g, mc) -> dict:
    """Everything the report needs about one stored version. Pure (no DB, no Jev)."""
    tour = g.tour_from_row(row)
    itin = str(row.get("aa_itineraries") or "")
    source = g.source_text(tour)
    src_clocks = {ba._norm_clock(m.group(0)) for m in ba.ITIN_CLOCK_TIME.finditer(source)}
    clock_ok = all(ba._norm_clock(m.group(0)) in src_clocks for m in ba.ITIN_CLOCK_TIME.finditer(itin))

    units = g.sentence_units({"itineraries": itin})
    asked_keys = {(u["sentence"], u["day"]) for u in units}
    old_units = [u for u in units if OLD_ITIN_MEAL_INVENTED.search(u["sentence"])]
    old_hit = bool(OLD_ITIN_MEAL_INVENTED.search(itin))

    claims = [u for u in ba.itinerary_claim_sentences(itin) if ba.ITIN_MEAL_INVENTED.search(u["sentence"])]
    verdicts = []
    for u in claims:
        ok, why = mc.claim_sourced(u, tour, itin)
        verdicts.append({"sentence": u["sentence"], "day": u["day"], "cleared": ok, "reason": why,
                         "askable": (u["sentence"], u["day"]) in asked_keys})
    cleared_keys = {(v["sentence"], v["day"]) for v in verdicts if v["cleared"]}
    new_hit = bool(ba.ITIN_MEAL_INVENTED.search(itin))
    rule_clears_all = bool(claims) and all(v["cleared"] for v in verdicts)
    jev_units = [v for v in verdicts if not v["cleared"] and v["askable"]]
    unaskable_left = [v for v in verdicts if not v["cleared"] and not v["askable"]]

    codes = set(_j(row.get("brand_audit_codes")) or [])
    fix_fields = set(_j(row.get("fix_pass_fields")) or [])
    qs_missing = row.get("brand_audit_codes") is None
    stored_code = MEAL_CODE in codes
    comparable = (not qs_missing and "itineraries" not in fix_fields and (old_hit or stored_code))
    waived = comparable and not stored_code and old_hit and clock_ok
    # With Jev OFF, the new code keeps the meal code unless every clock is sourced and the rule clears every claim.
    fires_jev_off = (not clock_ok) or (new_hit and not rule_clears_all)
    return {
        "gc_id": row.get("gc_id"), "tour_id": row.get("tour_id"), "version": row.get("version_num"),
        "clock_ok": clock_ok, "old_hit": old_hit, "new_hit": new_hit,
        "old_units": len(old_units), "claims": len(claims),
        "cleared": sum(v["cleared"] for v in verdicts),
        "askable_cleared": sum(1 for v in verdicts if v["cleared"] and v["askable"]),
        "old_units_cleared": sum(1 for u in old_units if (u["sentence"], u["day"]) in cleared_keys),
        "jev_units": len(jev_units), "unaskable_left": len(unaskable_left),
        "rule_clears_all": rule_clears_all, "comparable": comparable, "stored_code": stored_code,
        "waived_by_stored_code": waived, "still_needs_jev": waived and not rule_clears_all,
        "fires_jev_off": fires_jev_off, "fires_jev_off_old": (not clock_ok) or old_hit,
        "verdicts": verdicts,
    }


def aggregate(results: list[dict], flag_fix_cost: Optional[float] = None) -> dict:
    tours = {r["tour_id"] for r in results}
    waived_tours = {r["tour_id"] for r in results if r["waived_by_stored_code"]}
    still_tours = {r["tour_id"] for r in results if r["still_needs_jev"]}
    reasons = Counter(v["reason"] for r in results for v in r["verdicts"] if not v["cleared"])
    paths = Counter(v["reason"] for r in results for v in r["verdicts"] if v["cleared"])
    n = len(tours)
    before = round(len(waived_tours) / n, 4) if n else None
    after = round(len(still_tours) / n, 4) if n else None
    newly_firing = sum(1 for r in results if r["fires_jev_off"] and not r["fires_jev_off_old"])
    no_longer_firing = sum(1 for r in results if r["fires_jev_off_old"] and not r["fires_jev_off"])
    out = {
        "versions": len(results), "tours": n,
        "versions_old_meal_hit": sum(r["old_hit"] for r in results),
        "versions_new_meal_hit": sum(r["new_hit"] for r in results),
        "jev_meal_units_old": sum(r["old_units"] for r in results),
        "meal_claims_new": sum(r["claims"] for r in results),
        "claims_cleared_by_rule": sum(r["cleared"] for r in results),
        "claims_cleared_by_path": dict(paths),
        "jev_meal_units_removed_vs_old": sum(r["old_units_cleared"] for r in results),
        "jev_meal_units_removed_vs_new_detector": sum(r["askable_cleared"] for r in results),
        "jev_meal_units_now": sum(r["jev_units"] for r in results),
        "unaskable_claims_left": sum(r["unaskable_left"] for r in results),
        "versions_rule_clears_all": sum(r["rule_clears_all"] for r in results),
        "comparable_versions": sum(r["comparable"] for r in results),
        "versions_waived_by_stored_code": sum(r["waived_by_stored_code"] for r in results),
        "tours_waived_by_stored_code": len(waived_tours),
        "versions_still_needing_jev": sum(r["still_needs_jev"] for r in results),
        "tours_still_needing_jev": len(still_tours),
        "waiver_share_before": before, "waiver_share_after": after, "waiver_bar": WAIVER_BAR,
        "waiver_bar_met": (after is not None and after <= WAIVER_BAR),
        "versions_firing_with_jev_off_old_code": sum(r["fires_jev_off_old"] for r in results),
        "versions_firing_with_jev_off_new_code": sum(r["fires_jev_off"] for r in results),
        "versions_newly_firing_with_jev_off": newly_firing,
        "versions_no_longer_firing_with_jev_off": no_longer_firing,
        "not_cleared_reasons": dict(reasons.most_common()),
    }
    if flag_fix_cost is not None:
        out["flag_fix_itinerary_avg_usd"] = round(flag_fix_cost, 5)
        out["extra_flag_fix_usd_if_off_vs_old"] = round(
            max(0, out["versions_firing_with_jev_off_new_code"] - out["versions_firing_with_jev_off_old_code"])
            * flag_fix_cost, 4)
    return out


def samples(results: list[dict]) -> dict:
    def pick(pred, k=SAMPLES):
        out = []
        for r in results:
            for v in r["verdicts"]:
                if pred(r, v):
                    out.append({"tour_id": r["tour_id"], "version": r["version"], "day": v["day"],
                                "sentence": v["sentence"][:220], "reason": v["reason"]})
                    if len(out) >= k:
                        return out
        return out
    return {
        "cleared": pick(lambda r, v: v["cleared"]),
        "still_needing_jev": pick(lambda r, v: r["still_needs_jev"] and not v["cleared"]),
        "not_cleared": pick(lambda r, v: not v["cleared"]),
    }


async def fetch(days: int) -> tuple[list[dict], Optional[float]]:
    import asyncpg

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        await conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        await conn.execute("SET statement_timeout = '300s'")
        assert await conn.fetchval("SHOW default_transaction_read_only") == "on", "session is not read-only"
        rows = [dict(r) for r in await conn.fetch(VERSIONS_SQL, MASTER_TENANT_ID, days)]
        cost_row = await conn.fetchrow(FLAG_FIX_COST_SQL, days)
        cost = float(cost_row["avg_cost"]) if cost_row and cost_row["avg_cost"] is not None else None
        return rows, cost
    finally:
        await conn.close()


def _bronze_bucket() -> str:
    import boto3

    region = os.environ.get("AWS_REGION", "us-west-1")
    return "aa-cis-bronze-" + boto3.client("sts", region_name=region).get_caller_identity()["Account"]


def save(report: dict, key: str, upload: bool) -> str:
    os.makedirs("/tmp/aa803-out", exist_ok=True)
    path = os.path.join("/tmp/aa803-out", key.replace("/", "__"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, default=str)
    if upload:
        try:
            import boto3

            bucket = _bronze_bucket()
            boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-1")).upload_file(path, bucket, key)
            print(f"uploaded s3://{bucket}/{key}", flush=True)
        except Exception as e:  # noqa: BLE001 - a local run without S3 still has the file
            print(f"S3 upload skipped ({key}): {e}", flush=True)
    return path


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--no-upload", action="store_true")
    a = ap.parse_args(argv)
    ba, g, mc = _modules()
    rows, cost = asyncio.run(fetch(a.days))
    results = [classify_version(r, ba, g, mc) for r in rows]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    report = {"schema": "aa803-meal-rule-replay", "run_id": run_id,
              "generated_at": datetime.now(timezone.utc).isoformat(), "days": a.days,
              "total": aggregate(results, cost), "samples": samples(results)}
    path = save(report, f"{S3_PREFIX}/meal-rule-replay-{run_id}.json", upload=not a.no_upload)
    t = report["total"]
    print(json.dumps({k: t[k] for k in (
        "versions", "tours", "jev_meal_units_old", "jev_meal_units_removed_vs_old", "jev_meal_units_now",
        "tours_waived_by_stored_code", "tours_still_needing_jev", "waiver_share_before", "waiver_share_after",
        "waiver_bar_met", "versions_firing_with_jev_off_old_code", "versions_firing_with_jev_off_new_code",
        "claims_cleared_by_path")}, default=str), flush=True)
    print(f"local={path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
