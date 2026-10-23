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
from collections import Counter
from typing import Any, Optional

sys.path.insert(0, os.getcwd())

STAYED_V1_MAX = 0.10
COST_PER_TOUR_MAX = 0.12
CLEARLY_BETTER_POINTS = 0.10
V2_AUDIT_DETECTOR = "s1_writer_v2_brand_audit"   # brand_audit_node.V2_BRAND_AUDIT_DETECTOR (no app import here)

VERSIONS_SQL = """
WITH win AS (
    SELECT DISTINCT ON (gc.tour_id) gc.tour_id, gc.id AS after_id
    FROM silver_aa_internal.generated_content gc
    WHERE gc.created_at >= $1 AND gc.created_at < $2
      AND ($3::text[] IS NULL OR gc.tour_id::text = ANY($3::text[]))
    ORDER BY gc.tour_id, gc.created_at DESC, gc.version_num DESC
), bef AS (
    SELECT DISTINCT ON (gc.tour_id) gc.tour_id, gc.id AS before_id
    FROM silver_aa_internal.generated_content gc
    JOIN win ON win.tour_id = gc.tour_id
    WHERE gc.created_at < $1 AND gc.status = 'approved'
    ORDER BY gc.tour_id, gc.created_at DESC, gc.version_num DESC
)
SELECT win.tour_id::text AS tour_id, win.after_id::text AS after_id, bef.before_id::text AS before_id,
       rt.country, rt.duration, rt.src_name, rt.src_subtitle, rt.src_summary, rt.src_description, rt.src_highlights,
       rt.src_itineraries, rt.price_raw, rt.inclusions, rt.exclusions, rt.group_size, rt.best_time_to_go,
       pt.generated_content_id::text AS live_id, pt.master_status::text AS master_status
FROM win
JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = win.tour_id
LEFT JOIN bef ON bef.tour_id = win.tour_id
LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = win.tour_id AND pt.deleted_at IS NULL
WHERE ($4::text IS NULL OR rt.country = $4)
ORDER BY rt.country, win.tour_id
"""

PAGES_SQL = """
SELECT gc.id::text AS id, gc.status::text AS status, gc.metadata, gc.aa_name, gc.aa_subtitle, gc.aa_summary,
       gc.aa_description, gc.aa_highlights, gc.aa_itineraries, gc.seo_title, gc.seo_meta,
       qs.failure_codes, qs.brand_audit_status, qs.brand_audit_codes, qs.score_overall,
       (SELECT rq.review_status::text FROM silver_aa_internal.review_queue rq
         WHERE rq.generated_content_id = gc.id ORDER BY rq.created_at DESC LIMIT 1) AS review_status
FROM silver_aa_internal.generated_content gc
LEFT JOIN LATERAL (
    SELECT q.failure_codes, q.brand_audit_status, q.brand_audit_codes, q.score_overall
    FROM silver_aa_internal.quality_scores q
    WHERE q.generated_content_id = gc.id
    ORDER BY q.evaluated_at DESC, q.id DESC LIMIT 1
) qs ON true
WHERE gc.id = ANY($1::uuid[])
"""

ATOMS_SQL = """
SELECT t.tour_id::text AS tour_id,
       (SELECT count(*) FROM acp_contract.atomize_day_fingerprint f
         WHERE f.tenant_tour_version_id = t.version_id) AS fingerprint_days,
       (SELECT count(*) FROM acp_contract.tour_atoms a
         WHERE a.tour_id = t.tour_id AND a.owner_scope = 'platform' AND NOT a.deleted AND NOT a.is_empty_marker)
           AS live_atoms,
       (SELECT count(*) FROM acp_contract.tour_atoms a
         WHERE a.tour_id = t.tour_id AND a.owner_scope = 'platform' AND NOT a.deleted AND NOT a.is_empty_marker
           AND (a.itinerary_day IS NULL OR NOT (a.itinerary_day = ANY(t.days)))) AS atoms_off_days
FROM unnest($1::uuid[], $2::uuid[], $3::text[]) AS t0(tour_id, version_id, days_csv)
CROSS JOIN LATERAL (SELECT t0.tour_id, t0.version_id,
                           COALESCE(string_to_array(NULLIF(t0.days_csv, ''), ',')::smallint[], '{}') AS days) t
"""


def _json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def source_tour(r: dict) -> dict:
    """The raw source in the shape the code checks read (s1_screen.sample_from_row, agency fields included)."""
    from services.content_generation.s1_writer_v2 import with_agency_fields

    hl = _json(r.get("src_highlights"))
    tour = {"name": r.get("src_name"), "subtitle": r.get("src_subtitle"), "summary": r.get("src_summary"),
            "description": r.get("src_description"), "highlights": hl if isinstance(hl, list) else (hl or []),
            "itineraries": r.get("src_itineraries"), "country": r.get("country"), "duration": r.get("duration"),
            "price": r.get("price_raw"), "inclusions": r.get("inclusions"), "exclusions": r.get("exclusions")}
    return with_agency_fields(tour, r)


def page_of(p: Optional[dict]) -> Optional[dict]:
    if not p:
        return None
    hl = _json(p.get("aa_highlights"))
    return {"name": p.get("aa_name"), "subtitle": p.get("aa_subtitle"), "summary": p.get("aa_summary"),
            "description": p.get("aa_description") or "", "highlights": hl if isinstance(hl, list) else [],
            "itineraries": p.get("aa_itineraries"), "seo_title": p.get("seo_title"), "seo_meta": p.get("seo_meta")}


def page_days(page: Optional[dict]) -> list[int]:
    from services.content_generation.itinerary_utils import parse_canonical_itinerary_days
    return sorted(parse_canonical_itinerary_days((page or {}).get("itineraries") or ""))


def version_facts(p: Optional[dict], tour: dict, facts: dict) -> Optional[dict]:
    """The measured side of one stored version: code checks + stored verdicts. None when there is no version."""
    from scripts.aa645.gates import gate_label, is_report, page_gate_codes
    from scripts.aa645.s1_screen import figure_coverage
    from services.quality.s1_checks import run_checks

    page = page_of(p)
    if page is None:
        return None
    findings = run_checks(tour, page, facts)
    meta = _json(p.get("metadata")) or {}
    grounding = meta.get("grounding") or {}
    judge = meta.get("judge") or {}
    return {
        "version_id": p["id"], "status": p.get("status"), "review_status": p.get("review_status"),
        "writer_version": meta.get("writer_version") or "v1",
        "gate_codes": sorted({gate_label(c) for c in page_gate_codes(findings)}),
        "report_codes": sorted({f["code"] for f in findings if is_report(f["code"])}),
        "figure_coverage": figure_coverage(facts, page),
        "failure_codes": list(_json(p.get("failure_codes")) or []),
        "brand_audit_status": p.get("brand_audit_status"),
        "brand_audit_codes": list(_json(p.get("brand_audit_codes")) or []),
        # AA-645 d02: the AA voice-rubric codes the brand audit recorded report-only on a v2 draft
        "brand_audit_report_only": sorted({f.get("code") for f in (meta.get("quality_shadow") or [])
                                           if isinstance(f, dict) and f.get("detector") == V2_AUDIT_DETECTOR}),
        "grounding_found": grounding.get("found"),
        "grounding_unsupported": len(grounding.get("violations") or []) if grounding else None,
        "brand_fit": judge.get("brand_fit"),
        "score": float(p["score_overall"]) if p.get("score_overall") is not None else None,
        "cost_usd": meta.get("llm_cost_usd"),
        "days": page_days(page),
        "canonical_days": bool(page_days(page)),
    }


def outcome(row: dict) -> str:
    if row.get("live_id") and row["live_id"] == row["after_id"]:
        return "published"
    if row.get("live_id") and row["live_id"] == row.get("before_id"):
        return "stayed_v1"
    if row.get("live_id"):
        return "other_live_version"
    return "not_published"


def _fact_check(v: dict) -> bool:
    """Held on the elephant-riding rule: brand_audit stores FACT_CHECK_MANUAL_CHECK (an exact `in` on "FACT_CHECK"
    never matched it)."""
    return any(str(c).startswith("FACT_CHECK") for c in v.get("brand_audit_codes") or [])


def _mean(xs: list) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def _rate(n: int, d: int) -> Optional[float]:
    return round(n / d, 3) if d else None


def aggregate(tours: list[dict]) -> dict:
    """Per country (+ "ALL"): counts, before vs after code checks and stored verdicts, atoms, the verdict."""
    groups: dict[str, list[dict]] = {}
    for t in tours:
        groups.setdefault(t["country"] or "(none)", []).append(t)
    groups["ALL"] = list(tours)
    out = {}
    for country, rows in sorted(groups.items(), key=lambda kv: (kv[0] == "ALL", kv[0])):
        outcomes = Counter(t["outcome"] for t in rows)
        paired = [t for t in rows if t["before"] and t["after"]]
        b_gate = sum(1 for t in paired if t["before"]["gate_codes"])
        a_gate = sum(1 for t in paired if t["after"]["gate_codes"])
        b_uns = sum(t["before"]["grounding_unsupported"] or 0 for t in paired)
        a_uns = sum(t["after"]["grounding_unsupported"] or 0 for t in paired)
        published = [t for t in rows if t["outcome"] == "published"]
        stayed = [t for t in rows if t["outcome"] == "stayed_v1"]
        gate_after, gate_before = Counter(), Counter()
        for t in paired:
            gate_before.update(t["before"]["gate_codes"])
            gate_after.update(t["after"]["gate_codes"])
        b_rate, a_rate = _rate(b_gate, len(paired)), _rate(a_gate, len(paired))
        clearly_better = (b_rate is not None and a_rate is not None
                          and a_rate <= b_rate - CLEARLY_BETTER_POINTS and a_uns <= b_uns)
        out[country] = {
            "tours": len(rows), "outcomes": dict(outcomes),
            "stayed_v1_rate": _rate(outcomes.get("stayed_v1", 0), len(rows)),
            "paired": len(paired),
            "gate_pages_before": b_gate, "gate_pages_after": a_gate,
            "gate_page_rate_before": b_rate, "gate_page_rate_after": a_rate,
            "gate_codes_before": dict(sorted(gate_before.items())),
            "gate_codes_after": dict(sorted(gate_after.items())),
            "grounding_unsupported_before": b_uns, "grounding_unsupported_after": a_uns,
            "figure_coverage_before": _mean([t["before"]["figure_coverage"] for t in paired]),
            "figure_coverage_after": _mean([t["after"]["figure_coverage"] for t in paired]),
            "brand_fit_before": _mean([t["before"]["brand_fit"] for t in paired]),
            "brand_fit_after_report_only": _mean([t["after"]["brand_fit"] for t in paired]),
            "writer_versions_after": dict(Counter(t["after"]["writer_version"] for t in rows if t["after"])),
            "cost_per_tour_after": _mean([t["after"]["cost_usd"] for t in rows if t["after"]]),
            "published_not_atomized": sum(1 for t in published if not (t.get("atoms") or {}).get("fingerprint_days")),
            "atoms_off_days": sum((t.get("atoms") or {}).get("atoms_off_days") or 0 for t in published),
            "fact_check_holds": sum(1 for t in rows if t["after"] and _fact_check(t["after"])),
            # AA-645 review round: why a tour stayed on v1 — the new version's brand-audit status and codes (a
            # stayed_v1 driven by the audit, not by validate/judge, shows here before the flag goes wider).
            "stayed_v1_by_brand_audit": dict(sorted(Counter(
                (t["after"].get("brand_audit_status") or "none") for t in stayed if t["after"]).items())),
            "stayed_v1_brand_audit_codes": dict(sorted(Counter(
                c for t in stayed if t["after"] for c in t["after"].get("brand_audit_codes") or []).items())),
            "brand_audit_report_only_codes_after": dict(sorted(Counter(
                c for t in rows if t["after"] for c in t["after"].get("brand_audit_report_only") or []).items())),
            "legacy_itinerary_pages": sum(1 for t in rows if t["after"] and not t["after"]["canonical_days"]),
            "clearly_better": clearly_better,
        }
    return out


def stop_conditions(all_row: dict) -> dict:
    """Memo section 6 (d13 "as proposed"); True = stop the wave and report."""
    cost = all_row.get("cost_per_tour_after")
    return {
        "stayed_v1_over_10pct": (all_row.get("stayed_v1_rate") or 0) > STAYED_V1_MAX,
        "legacy_itinerary_path": all_row.get("legacy_itinerary_pages", 0) > 0,
        "atoms_on_days_past_new_count": all_row.get("atoms_off_days", 0) > 0,
        "cost_per_tour_over_model": cost is not None and cost > COST_PER_TOUR_MAX,
        "published_not_atomized": all_row.get("published_not_atomized", 0) > 0,
        "not_clearly_better": not all_row.get("clearly_better"),
    }


async def load(args) -> list[dict]:
    import asyncpg

    from services.quality.s1_facts import build_fact_table

    ids = None
    if args.tour_ids_file:
        with open(args.tour_ids_file, encoding="utf-8") as f:
            ids = [x.strip() for x in json.load(f) if str(x).strip()]
    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    try:
        rows = [dict(r) for r in await conn.fetch(VERSIONS_SQL, args.since, args.until, ids, args.country)]
        vids = sorted({r[k] for r in rows for k in ("after_id", "before_id") if r.get(k)})
        pages = {p["id"]: dict(p) for p in await conn.fetch(PAGES_SQL, vids)} if vids else {}
        tours = []
        for r in rows:
            tour = source_tour(r)
            facts = build_fact_table(tour)
            t = {"tour_id": r["tour_id"], "country": r.get("country"), "outcome": outcome(r),
                 "master_status": r.get("master_status"),
                 "after": version_facts(pages.get(r["after_id"]), tour, facts),
                 "before": version_facts(pages.get(r["before_id"]), tour, facts) if r.get("before_id") else None}
            tours.append(t)
        pub = [t for t in tours if t["outcome"] == "published" and t["after"]]
        if pub:
            arows = await conn.fetch(ATOMS_SQL, [t["tour_id"] for t in pub], [t["after"]["version_id"] for t in pub],
                                     [",".join(str(d) for d in t["after"]["days"]) for t in pub])
            by_tour = {a["tour_id"]: dict(a) for a in arows}
            for t in pub:
                t["atoms"] = by_tour.get(t["tour_id"])
    finally:
        await conn.close()
    return tours


def markdown(countries: dict, stops: dict) -> str:
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
    lines.append("Stop conditions (true = stop and report): "
                 + ", ".join(f"{k}={'TRUE' if v else 'false'}" for k, v in stops.items()))
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


def build(tours: list[dict], args) -> dict[str, Any]:
    countries = aggregate(tours)
    return {"window": {"since": args.since.isoformat(), "until": args.until.isoformat(), "country": args.country},
            "countries": countries, "stop_conditions": stop_conditions(countries.get("ALL", {})), "tours": tours}


def main(argv=None) -> int:
    args = parse_args(argv)
    report = build(asyncio.run(load(args)), args)
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        print(text)
    if args.markdown:
        print(markdown(report["countries"], report["stop_conditions"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
