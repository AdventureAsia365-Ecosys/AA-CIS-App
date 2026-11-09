"""AA-645 d13 / AA-1024 G17 — the quality read of an S1 rerun wave and its stop flags, used by the wave report script
(`scripts/aa645/wave_report.py`, the CLI, which documents each number and exits 3 on a true flag, 5 while pending).

Not called from an API request: the read runs the S1 code checks on the before and after page of every tour (S272
review: about 16 s for 50 dense tours and 75 s for 250 on a laptop, past the API Gateway's 29 s on the 0.5 vCPU api
task), so a wave-start gate on it was taken out. Follow-up: the CLI stores its stop_state on the wave and
`POST /admin/waves` reads the stored value (docs/implementation-notes/AA-1024-guards-rest.md).

Code-only and $0: SELECTs, the S1 code checks (`services.quality.s1_checks`), no model call, no write. Every query
reads CIS schemas only (cis_app has SELECT on each). Moved from the script (AA-1024 G17) so the API reads the same
thresholds and the same checks: one definition of a stop flag.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections import Counter
from typing import Any, Optional

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
    from services.quality.s1_coverage import figure_coverage
    from services.quality.s1_gates import gate_label, is_report, page_gate_codes
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
        # AA-645 (S249): the rubric behind brand_fit ("v1" = the brand-angle prompt, every version before S249; a v2
        # version string = brand_fit_v2, voice within the source) — the two are different scales.
        "brand_fit_rubric": judge.get("rubric") or "v1",
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
            # AA-645 (S249): which rubric scored the before / after brand fit (a v1 -> v2 change is a scale change,
            # not a quality change)
            "brand_fit_rubrics_before": dict(Counter(t["before"].get("brand_fit_rubric", "v1") for t in paired)),
            "brand_fit_rubrics_after": dict(Counter(t["after"].get("brand_fit_rubric", "v1") for t in paired)),
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


# The wave's own jobs, by every payload shape that names its tours (S272 review): a single `tour_id` (s1_rewrite,
# a3_atomize, ...), a `tour_ids` list (s1_seo_prefetch and s1_batch_ingest run before any s1_rewrite of the wave), and
# t2_rewrite's `published_tour_id`, which is gold_aa_internal.published_tours.id, not the raw tour id.
ACTIVE_JOBS_SQL = """
SELECT j.kind, j.status, count(*) AS n
FROM shared.job j
WHERE j.status IN ('queued', 'running')
  AND (j.payload->>'tour_id' = ANY($1::text[])
       OR (jsonb_typeof(j.payload->'tour_ids') = 'array' AND j.payload->'tour_ids' ?| $1::text[])
       OR j.payload->>'published_tour_id' IN (
              SELECT pt.id::text FROM gold_aa_internal.published_tours pt WHERE pt.tour_id::text = ANY($1::text[])))
GROUP BY 1, 2 ORDER BY 1, 2
"""

# The active tours of a country, when the report is scoped by --country: a wave tour whose prefetch / ingest /
# rewrite is still queued has no A1 version in the window yet, so the window alone cannot name it (S272 review).
COUNTRY_TOURS_SQL = """
SELECT rt.tour_id::text AS tour_id
FROM silver_aa_internal.raw_tours rt
WHERE rt.country = $1 AND rt.source_status = 'active' AND rt.deleted_at IS NULL
"""

EXIT_STOP = 3
EXIT_PENDING = 5
# A synthetic active-jobs row: the requested tours have no A1 version in the window yet (nothing to judge).
NO_VERSION_KIND = "no_a1_version_in_window"


def stop_state(stops: dict, active_jobs: list[dict]) -> dict:
    """Each flag as true / false, or pending while the wave's tours still have queued or running jobs."""
    if any(int(j.get("n") or 0) > 0 for j in active_jobs or []):
        return {k: "pending" for k in stops}
    return {k: "true" if v else "false" for k, v in stops.items()}


def exit_code(state: dict) -> int:
    if "true" in state.values():
        return EXIT_STOP
    if "pending" in state.values():
        return EXIT_PENDING
    return 0


async def load_active_jobs(conn, tour_ids: list) -> list[dict]:
    if not tour_ids:
        return []
    return [dict(r) for r in await conn.fetch(ACTIVE_JOBS_SQL, sorted({str(t) for t in tour_ids}))]


def _compute_tours(rows: list[dict], pages: dict) -> list[dict]:
    """The per-tour facts of the wave (CPU only: the code checks on both versions)."""
    from services.quality.s1_facts import build_fact_table

    tours = []
    for r in rows:
        tour = source_tour(r)
        facts = build_fact_table(tour)
        tours.append({"tour_id": r["tour_id"], "country": r.get("country"), "outcome": outcome(r),
                      "master_status": r.get("master_status"),
                      "after": version_facts(pages.get(r["after_id"]), tour, facts),
                      "before": version_facts(pages.get(r["before_id"]), tour, facts) if r.get("before_id") else None})
    return tours


async def collect(conn, since: dt.datetime, until: dt.datetime, ids: Optional[list[str]] = None,
                  country: Optional[str] = None) -> tuple[list[dict], list[dict]]:
    """(tours, active_jobs) of a wave window: every tour with an A1 version created in [since, until), optionally
    limited to `ids` / `country`. The code checks run in a worker thread (an API request must not block the loop).
    active_jobs are the queued / running jobs of the wave's expected set (ids, the window's tours, the country's
    active tours); with a named set and no version in the window yet, one `no_a1_version_in_window` row (pending)."""
    rows = [dict(r) for r in await conn.fetch(VERSIONS_SQL, since, until, ids, country)]
    vids = sorted({r[k] for r in rows for k in ("after_id", "before_id") if r.get(k)})
    pages = {p["id"]: dict(p) for p in await conn.fetch(PAGES_SQL, vids)} if vids else {}
    tours = await asyncio.to_thread(_compute_tours, rows, pages)
    pub = [t for t in tours if t["outcome"] == "published" and t["after"]]
    if pub:
        arows = await conn.fetch(ATOMS_SQL, [t["tour_id"] for t in pub], [t["after"]["version_id"] for t in pub],
                                 [",".join(str(d) for d in t["after"]["days"]) for t in pub])
        by_tour = {a["tour_id"]: dict(a) for a in arows}
        for t in pub:
            t["atoms"] = by_tour.get(t["tour_id"])
    # The job lookup covers the wave's expected set, not only the tours found in the window (S272 review): the
    # requested ids, the window's tours, and with --country the country's active tours. A tour whose s1_seo_prefetch /
    # s1_batch_ingest / s1_rewrite is still queued has no version yet; reading only the window called the flags final.
    expected = {str(t) for t in ids or []} | {t["tour_id"] for t in tours}
    if country:
        expected |= {r["tour_id"] for r in await conn.fetch(COUNTRY_TOURS_SQL, country)}
    active = await load_active_jobs(conn, sorted(expected))
    requested = {str(t) for t in ids or []}
    if not tours and (requested or country) and not active:
        # nothing ran yet for a named set: report pending (exit 5), never a STOP on an empty table
        active = [{"kind": NO_VERSION_KIND, "status": "expected", "n": len(requested) or 1}]
    return tours, active


def evaluate(tours: list[dict], active_jobs: Optional[list] = None) -> dict[str, Any]:
    """The countries table, the stop flags, their state and the exit code of one wave's tours."""
    countries = aggregate(tours)
    stops = stop_conditions(countries.get("ALL", {}))
    state = stop_state(stops, active_jobs or [])
    return {"countries": countries, "stop_conditions": stops, "stop_state": state,
            "active_jobs": active_jobs or [], "exit_code": exit_code(state)}
