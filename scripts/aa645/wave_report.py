#!/usr/bin/env python3
"""AA-645 step 0 (d13) — the per-wave quality report of an S1 rerun wave: did quality clearly improve, per country?

READ-ONLY and $0: SELECTs only, no model call, no write. Nghiep's decision d13 (S248): every wave is analysed for a
clear improvement, before vs after, per country; the stop conditions of the platform-impact memo section 6 are
checked on the same numbers.

For every tour with an A1 version (silver_aa_internal.generated_content) created inside the wave window:

* ``after`` = the newest version created in the window; ``before`` = the newest approved version created before the
  window (the page that was live when the wave started; none for a tour that was never published);
* outcome: ``published`` (published_tours points at ``after``), ``stayed_v1`` (the tour keeps ``before`` live: the
  new version is hitl / its review row dismissed — the silent "dismissed" of memo C1), ``not_published`` (no live
  page at all); ``fact_check_holds`` counts new versions held on FACT_CHECK (the elephant-riding rule, memo C8),
  reported apart, not as a writer failure;
* code checks on both pages, the same ones the A/B used ($0, ``services.quality.s1_checks.run_checks`` against the raw
  source + ``scripts/aa645/gates.py``): pages with a gate code (the serious-error proxy), gate codes by bucket,
  report-only codes, figure coverage;
* why a tour stayed on v1: the new version's brand-audit status and codes (``stayed_v1_by_brand_audit``), and the
  AA voice-rubric codes the audit recorded report-only on a v2 draft (d02, ``brand_audit_report_only_codes_after``);
* the stored verdicts of each version: validate failure codes (latest quality_scores row), grounding found /
  still-unsupported (metadata.grounding), brand fit (metadata.judge — report-only on a v2 version), writer version,
  cost (metadata.llm_cost_usd);
* atoms after the wave, for published tours: atomized (day fingerprints for the new version), live atoms on a day the
  new itinerary does not have (memo C6, must be 0), "published, not atomized" (memo T5).

Output: one JSON document (``--out``, default stdout) with ``countries`` (one row per country + ``ALL``), ``tours``
(per-tour rows, no page text) and ``stop_conditions`` (memo section 6, each true = stop and report). ``--markdown``
also prints a table for the wave report.

Stop flags are enforced (AA-1024 G17; root lessons log, wave family): a wave went on past an approved stop threshold
because the report always exited 0, and a flag read while jobs were still queued was once taken for a stop.
``stop_state`` gives each flag as ``true`` / ``false``, or ``pending`` while shared.job still has queued or running
jobs for the wave's tours (``active_jobs``: the numbers can still move). Exit code: 3 = a flag is true (stop the
wave and report; the next wave starts only with the tech lead's written approval quoted in the wave report),
5 = pending (run the report again after the jobs drain), 0 = no flag.

Runs on the deployed image with the container DATABASE_URL (cis_app reads every CIS schema; ssl=require), shipped
through S3 like the other aa645 scripts:

  cd /app && python3 -m scripts.aa645.wave_report --since 2026-10-24T02:00:00Z --markdown --out /tmp/wave1.json

Thresholds (memo section 6, Nghiep's d13 "as proposed"): stayed_v1 > 10 % of the wave, any live atom on a day past
the new day count, A1 cost per published tour > $0.12, any published tour not atomized, any tour on the legacy
(no "Day N —") itinerary path. "Clearly better" per country = the after pages' gate-page rate is at least 10 points
below the before pages' and the grounding still-unsupported count did not rise.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.getcwd())

# AA-1024 G17: the computation lives in services/waves/quality.py (the API's wave start reads the same flags); the
# names stay importable from here for the other aa645 scripts and the tests.
from services.waves.quality import (  # noqa: E402,F401
    ACTIVE_JOBS_SQL,
    ATOMS_SQL,
    CLEARLY_BETTER_POINTS,
    COST_PER_TOUR_MAX,
    EXIT_PENDING,
    EXIT_STOP,
    PAGES_SQL,
    STAYED_V1_MAX,
    V2_AUDIT_DETECTOR,
    VERSIONS_SQL,
    _fact_check,
    _json,
    _mean,
    _rate,
    aggregate,
    collect,
    evaluate,
    exit_code,
    load_active_jobs,
    outcome,
    page_days,
    page_of,
    source_tour,
    stop_conditions,
    stop_state,
    version_facts,
)


async def load(args) -> tuple[list[dict], list[dict]]:
    import asyncpg

    ids = None
    if args.tour_ids_file:
        with open(args.tour_ids_file, encoding="utf-8") as f:
            ids = [x.strip() for x in json.load(f) if str(x).strip()]
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        return await collect(conn, args.since, args.until, ids, args.country)
    finally:
        await conn.close()


def markdown(countries: dict, stops: dict, state: Optional[dict] = None,
             active_jobs: Optional[list] = None) -> str:
    head = ("| Country | Tours | Published | Stayed v1 | Gate pages before → after | Unsupported before → after |"
            " Figure cov. before → after | Brand fit before → after (report-only) | $ / tour | Clearly better |")
    lines = [head, "|" + "---|" * 10]
    for c, r in countries.items():
        lines.append(
            f"| {c} | {r['tours']} | {r['outcomes'].get('published', 0)} | {r['outcomes'].get('stayed_v1', 0)} | "
            f"{r['gate_pages_before']}/{r['paired']} → {r['gate_pages_after']}/{r['paired']} | "
            f"{r['grounding_unsupported_before']} → {r['grounding_unsupported_after']} | "
            f"{r['figure_coverage_before']} → {r['figure_coverage_after']} | "
            f"{r['brand_fit_before']} → {r['brand_fit_after_report_only']} | {r['cost_per_tour_after']} | "
            f"{'yes' if r['clearly_better'] else 'no'} |")
    lines.append("")
    all_row = countries.get("ALL") or {}
    if all_row.get("outcomes", {}).get("stayed_v1"):
        lines.append("Stayed v1 by the new version's brand audit (ALL): "
                     + json.dumps(all_row.get("stayed_v1_by_brand_audit") or {}) + ", codes "
                     + json.dumps(all_row.get("stayed_v1_brand_audit_codes") or {}))
    if all_row.get("brand_audit_report_only_codes_after"):
        lines.append("Brand-audit voice codes recorded report-only on v2 (ALL): "
                     + json.dumps(all_row["brand_audit_report_only_codes_after"]))
    state = state or {k: "true" if v else "false" for k, v in stops.items()}
    lines.append("Stop conditions (true = stop and report): "
                 + ", ".join(f"{k}={'TRUE' if state[k] == 'true' else state[k]}" for k in stops))
    if "pending" in state.values():
        lines.append("PENDING: the wave's tours still have queued or running jobs ("
                     + ", ".join(f"{j['kind']} {j['status']} {j['n']}" for j in active_jobs or [])
                     + "); the flags can still change. Run the report again after they drain.")
    elif "true" in state.values():
        lines.append("STOP: a stop condition is true. Stop the wave and report; the next wave starts only with the "
                     "tech lead's written approval, quoted in the wave report.")
    return "\n".join(lines)


def _ts(value: str) -> dt.datetime:
    v = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return v if v.tzinfo else v.replace(tzinfo=dt.timezone.utc)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", required=True, type=_ts, help="wave start (ISO 8601, UTC if no offset)")
    ap.add_argument("--until", type=_ts, default=None, help="wave end (default: now)")
    ap.add_argument("--country", default=None)
    ap.add_argument("--tour-ids-file", default=None, help="JSON list of tour ids (the wave's expected_set)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args(argv)
    args.until = args.until or dt.datetime.now(dt.timezone.utc)
    return args


def build(tours: list[dict], args, active_jobs: Optional[list] = None) -> dict[str, Any]:
    ev = evaluate(tours, active_jobs)
    return {"window": {"since": args.since.isoformat(), "until": args.until.isoformat(), "country": args.country},
            **ev, "tours": tours}


def main(argv=None) -> int:
    args = parse_args(argv)
    tours, active = asyncio.run(load(args))
    report = build(tours, args, active)
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    if args.markdown:
        print(markdown(report["countries"], report["stop_conditions"], report["stop_state"], report["active_jobs"]))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
