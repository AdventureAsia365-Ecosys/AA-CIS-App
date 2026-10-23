#!/usr/bin/env python3
"""AA-645 (S249) — the wave table + stop thresholds of a shadow run (scripts/aa645/shadow.py output). $0, stdlib only.

Per country and ALL: tours, errors, publishable (the version would publish; else the tour "stays on v1"), attempts per
run, judge pass rate (brand fit >= 7 on every judged draft and on the final draft, per rubric — report-only on writer
v2), code gate pages after flag_fix on the new page vs the live page (the serious-error proxy of the A/B), figure
coverage, grounding still-unsupported, Jev ran / problem, real $ per tour (llm_call_log by the tour's job id),
"Adventure Asia" / "Design This Journey" leaks, the day path (legacy = no "Day N —" itinerary), day-count changes.

STOP THRESHOLDS (platform-impact memo section 6 "Điều kiện dừng", Nghiep d13 "as proposed", mapped to a shadow run —
no publish, no atomize; each true = stop and report before the next step):
  stayed_v1_over_10pct       > 10 % of the tours would not publish (stay on v1)
  legacy_itinerary_path      any new page without canonical "Day N —" days (atomize would take the legacy path)
  cost_per_tour_over_model   mean real $ per tour > $0.12 (1.5 x the $0.08 model for W x Sonnet)
  bedrock_throttle           any throttling row / error (acc3)
  tour_errors                any tour that crashed or timed out (a cost-cap stop is reported, not counted)
  not_written_by_v2          any tour whose kept draft is not writer v2 (harness / flag problem)
  degraded_hard              any tour where a hard detector failed open (Jev, grounding: the numbers are not valid)
  brand_leak_pages           any new page naming Adventure Asia / Design This Journey the source lacks (added: the
                             white-label rule; the memo lists it as a wave metric)
  not_better_than_live       gate-page rate of the new pages >= that of the live pages (code proxy of "the serious
                             error rate no longer beats A+"; the panel check on panel/packet.json is the real one)
Not a stop in a shadow run: live atoms past the new day count (no atomize runs; `day_count_dropped` is shown) and an
atomize failure (none runs).

  python3 -I shadow_report.py --result shadow.json [--markdown] [--json summary.json]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from statistics import mean
from typing import Optional

MIN_QUALITY = 7.0
STAYED_V1_MAX = 0.10
COST_PER_TOUR_MAX = 0.12


def _rate(n: int, d: int) -> Optional[float]:
    return round(n / d, 3) if d else None


def _mean(xs) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(mean(xs), 4) if xs else None


def _is_cap(t: dict) -> bool:
    return "CostCapReached" in (t.get("error") or "")


def group_row(tours: list[dict]) -> dict:
    done = [t for t in tours if not t.get("error") and t.get("status") == "success"]
    errors = [t for t in tours if t.get("error") and not _is_cap(t)]
    paired = [t for t in done if t.get("new_page") and t.get("live_page")]
    new_gate = sum(1 for t in paired if t["new_page"]["gate_codes"])
    live_gate = sum(1 for t in paired if t["live_page"]["gate_codes"])
    judged = [d for t in done for d in (t.get("judged_drafts") or []) if d.get("brand_fit") is not None]
    rubrics = Counter(d.get("rubric") or "v1" for d in judged)
    final_fit = [t["judge"]["brand_fit"] for t in done if (t.get("judge") or {}).get("brand_fit") is not None]
    costs = [t.get("cost_usd") for t in done]
    stage_usd: dict[str, list[float]] = {}
    for t in done:
        for st, usd in (t.get("usd_by_stage") or {}).items():
            stage_usd.setdefault(st, []).append(usd)
    publishable = sum(1 for t in done if t.get("publishable"))
    return {
        "tours": len(tours), "done": len(done), "errors": len(errors),
        "cost_cap_skipped": sum(1 for t in tours if _is_cap(t)),
        "publishable": publishable, "publishable_rate": _rate(publishable, len(done)),
        "stayed_v1_rate": _rate(len(done) - publishable, len(done)),
        "attempts_mean": _mean([t.get("attempts") for t in done]),
        "attempts_max": max((t.get("attempts") or 0 for t in done), default=None),
        "attempts_dist": dict(sorted(Counter(t.get("attempts") for t in done).items())),
        "judged_drafts": len(judged),
        "judge_pass_rate_drafts": _rate(sum(1 for d in judged if d["brand_fit"] >= MIN_QUALITY), len(judged)),
        "judge_pass_rate_final": _rate(sum(1 for f in final_fit if f >= MIN_QUALITY), len(final_fit)),
        "judge_brand_fit_final_mean": _mean(final_fit),
        "judge_rubrics": dict(rubrics),
        "judge_gates": dict(Counter(d.get("gate") for d in judged)),
        "judge_capped_final": sum(1 for t in done if (t.get("judge") or {}).get("caps")),
        "paired_with_live": len(paired),
        "gate_pages_new": new_gate, "gate_pages_live": live_gate,
        "gate_page_rate_new": _rate(new_gate, len(paired)), "gate_page_rate_live": _rate(live_gate, len(paired)),
        "gate_pages_new_all": sum(1 for t in done if (t.get("new_page") or {}).get("gate_codes")),
        "gate_codes_new": dict(sorted(Counter(c for t in done for c in (t.get("new_page") or {}).get("gate_codes", [])
                                              ).items())),
        "gate_codes_live": dict(sorted(Counter(c for t in paired for c in t["live_page"]["gate_codes"]).items())),
        "figure_coverage_new": _mean([(t.get("new_page") or {}).get("figure_coverage") for t in paired]),
        "figure_coverage_live": _mean([(t.get("live_page") or {}).get("figure_coverage") for t in paired]),
        "validate_codes": dict(sorted(Counter(c for t in done for c in t.get("failure_codes") or []).items())),
        "surviving_hard_codes": dict(sorted(Counter(c for t in done for c in t.get("surviving_hard_codes") or []
                                                    ).items())),
        "brand_audit": dict(Counter((t.get("brand_audit") or {}).get("status") or "none" for t in done)),
        "flag_fix_applied": sum(1 for t in done if (t.get("flag_fix") or {}).get("applied")),
        "revalidate_passed": sum(1 for t in done if (t.get("flag_fix") or {}).get("revalidate_passed")),
        "grounding_found": sum((t.get("grounding") or {}).get("found", 0) for t in done),
        "grounding_still_unsupported": sum((t.get("grounding") or {}).get("still_unsupported", 0) for t in done),
        "jev_ran": sum(1 for t in done if (t.get("jev") or {}).get("ran")),
        "jev_problem": sum(1 for t in done if (t.get("jev") or {}).get("problem")),
        "degraded_hard": sum(1 for t in done if t.get("degraded_hard")),
        "cost_per_tour_mean": _mean(costs), "cost_per_tour_max": max((c for c in costs if c is not None), default=None),
        "cost_total": round(sum(c or 0 for c in (t.get("cost_usd") for t in tours)), 4),
        "cost_per_tour_by_stage": {k: _mean(v) for k, v in sorted(stage_usd.items())},
        "fallback_rows": sum(t.get("fallback_rows") or 0 for t in tours),
        "throttle_rows": sum(t.get("throttle_rows") or 0 for t in tours),
        "throttle_errors": sum(1 for t in tours if "throttl" in (t.get("error") or "").lower()),
        "leak_pages": sum(1 for t in done if t.get("leaks")),
        "legacy_pages": sum(1 for t in done if (t.get("days") or {}).get("legacy_path")),
        "days_match_plan": sum(1 for t in done if (t.get("days") or {}).get("matches_plan")),
        "day_count_changed": sum(1 for t in done if (t.get("days") or {}).get("count_changed")),
        "day_count_dropped": sum(1 for t in done if (t.get("days") or {}).get("count_dropped")),
        "not_v2": sum(1 for t in tours if "not written by writer v2" in (t.get("error") or "")),
        "sonnet_retried": sum(1 for t in done if t.get("sonnet_retried")),
        "writer_models": dict(Counter(t.get("model_used") for t in done)),
    }


def stop_conditions(a: dict) -> dict:
    rate_new, rate_live = a.get("gate_page_rate_new"), a.get("gate_page_rate_live")
    return {
        "stayed_v1_over_10pct": (a.get("stayed_v1_rate") or 0) > STAYED_V1_MAX,
        "legacy_itinerary_path": a.get("legacy_pages", 0) > 0,
        "cost_per_tour_over_model": (a.get("cost_per_tour_mean") is not None
                                     and a["cost_per_tour_mean"] > COST_PER_TOUR_MAX),
        "bedrock_throttle": a.get("throttle_rows", 0) > 0 or a.get("throttle_errors", 0) > 0,
        "tour_errors": a.get("errors", 0) > 0,
        "not_written_by_v2": a.get("not_v2", 0) > 0,
        "degraded_hard": a.get("degraded_hard", 0) > 0,
        "brand_leak_pages": a.get("leak_pages", 0) > 0,
        "not_better_than_live": (rate_new is not None and rate_live is not None
                                 and rate_new >= rate_live and (rate_new > 0 or rate_live > 0)),
    }


def summarize(tours: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for t in tours:
        groups.setdefault(t.get("country") or "(none)", []).append(t)
    countries = {k: group_row(v) for k, v in sorted(groups.items())}
    countries["ALL"] = group_row(list(tours))
    stops = stop_conditions(countries["ALL"])
    return {"countries": countries, "stop_conditions": stops, "stop": any(stops.values()),
            "panel_check": "run the blind panel on panel/packet.json (new page vs live page) for the serious-error "
                           "rate — the memo's last stop condition; the gate-page rate above is its code proxy"}


def markdown(summary: dict) -> str:
    rows = ["| Country | Tours (done/err) | Publishable | Attempts mean/max | Judge pass drafts/final (rubric) | "
            "Gate pages new/live | Figure cov new/live | Grounding unsupported | Jev ran/problem | $/tour mean/max | "
            "Leaks | Legacy | Days ≠ plan |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c, r in summary["countries"].items():
        rub = ",".join(f"{k}:{v}" for k, v in r["judge_rubrics"].items()) or "-"
        rows.append(
            f"| {c} | {r['tours']} ({r['done']}/{r['errors']}) | {r['publishable']} ({r['publishable_rate']}) | "
            f"{r['attempts_mean']}/{r['attempts_max']} | {r['judge_pass_rate_drafts']}/{r['judge_pass_rate_final']} "
            f"({rub}) | {r['gate_pages_new']}/{r['gate_pages_live']} of {r['paired_with_live']} | "
            f"{r['figure_coverage_new']}/{r['figure_coverage_live']} | {r['grounding_still_unsupported']} | "
            f"{r['jev_ran']}/{r['jev_problem']} | {r['cost_per_tour_mean']}/{r['cost_per_tour_max']} | "
            f"{r['leak_pages']} | {r['legacy_pages']} | {r['done'] - r['days_match_plan']} |")
    stops = summary["stop_conditions"]
    rows.append("")
    rows.append("Stop conditions: " + (", ".join(k for k, v in stops.items() if v) or "none") +
                f" → {'STOP and report' if summary['stop'] else 'go on to the next step (after Nghiệp approves)'}")
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True, help="shadow.json (or shadow.partial.json)")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--json", help="write the summary JSON here")
    args = ap.parse_args(argv)
    with open(args.result, encoding="utf-8") as f:
        doc = json.load(f)
    summary = summarize(doc.get("tours") or [])
    a = summary["countries"]["ALL"]
    print(json.dumps({"run_id": doc.get("run_id"), "final": doc.get("final"), "stopped": doc.get("stopped"),
                      "writer_model": doc.get("writer_model"), "ALL": a,
                      "stop_conditions": summary["stop_conditions"]}, indent=1, ensure_ascii=False))
    if args.markdown:
        print(markdown(summary))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
