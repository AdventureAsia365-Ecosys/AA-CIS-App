#!/usr/bin/env python3
"""AA-645 (S249) — read the brand-fit re-measure (brandfit_measure.py) against the panel; decide the pass criterion.

Per rubric and arm: n, mean / median brand fit, retry % (< 7.0), retry outside the tie band (< 6.5), "generic"
feedback %, capped % (v2), unsupported specifics per page (v2), the panel's serious errors per claim, and the paired
difference vs the baseline arm per tour (lower / higher / tie counts, exact sign test). Page level: Spearman between
brand fit and the panel's serious-error rate (negative = the judge rewards fidelity).

PASS CRITERION (fixed before the run, run sheet `.tmp-session/s249/aa645/RUN.md`), on rubric v2:
    retry % of `w|sonnet-5-5` <= retry % of `aplus|haiku` + 5 points
AND page-level Spearman(brand fit, serious errors per claim) <= 0.
PASS -> rubric v2 may gate writer-v2 drafts again (A1_V2_BRAND_FIT_GATE=enforce, Nghiep's call); FAIL -> stays
report-only. The same numbers are printed for v1 (the S248 finding) as the contrast.

Stdlib only (runs on a laptop):
  python3 -I brandfit_analyze.py --result result.json --score score.json --pass-a pass-a.json --pass-b pass-b.json \\
      --key panel/key.json [--v1-result S248/brandfit/result.json] [--json analysis.json]
`--v1-result` adds the S248 v1 run (single rubric, no `rubric` field) as rubric "v1_s248" for an A/A view of v1.
"""
from __future__ import annotations

import argparse
import json
import math
from statistics import mean, median
from typing import Optional

MIN_QUALITY = 7.0
TIE = 0.5
BASELINE = "aplus|haiku"
CANDIDATE = "w|sonnet-5-5"
RETRY_MARGIN_POINTS = 5.0
SPEARMAN_MAX = 0.0


def rank(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(xs, ys) -> Optional[float]:
    if len(xs) < 3:
        return None
    rx, ry = rank(xs), rank(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return round(num / den, 3) if den else None


def sign_test(diffs) -> dict:
    """Two-sided exact sign test on non-zero paired differences."""
    pos, neg = sum(d > 0 for d in diffs), sum(d < 0 for d in diffs)
    n = pos + neg
    if not n:
        return {"lower": neg, "higher": pos, "ties": len(diffs), "p": None}
    k = min(pos, neg)
    p = min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)
    return {"lower": neg, "higher": pos, "ties": len(diffs) - n, "p": round(p, 4)}


def panel_by_key(passes, key_map) -> dict:
    """page key -> {serious, claims, rate} averaged over the panel passes."""
    acc: dict[str, dict] = {}
    for ps in passes:
        for p in ps["pages"]:
            k = key_map.get(p["page_id"], {}).get("key")
            if not k:
                continue
            a = acc.setdefault(k, {"serious": [], "claims": []})
            a["serious"].append(sum(1 for e in p.get("errors", []) if e.get("severity") == "serious"))
            a["claims"].append(p.get("checkable_claims") or 0)
    out = {}
    for k, a in acc.items():
        s, c = mean(a["serious"]), mean(a["claims"])
        out[k] = {"serious": s, "claims": c, "rate": (s / c) if c else None}
    return out


def rows_of(result: dict, rubric_override: Optional[str] = None) -> list[dict]:
    """ok rows with `rubric` and `page_key` filled (the S248 v1 result has neither)."""
    out = []
    for r in result.get("rows", []):
        if not r.get("ok"):
            continue
        r = dict(r)
        r["rubric"] = rubric_override or r.get("rubric") or "v1"
        r["page_key"] = r.get("page_key") or r["key"]
        out.append(r)
    return out


def arm_table(rows: list[dict], score: dict, baseline: str = BASELINE) -> dict:
    arms = sorted({r["arm_model"] for r in rows}, key=lambda a: (a != baseline, a))
    base = {r["sample_id"]: r for r in rows if r["arm_model"] == baseline}
    out = {}
    for arm in arms:
        rs = [r for r in rows if r["arm_model"] == arm]
        fits = [r["brand_fit"] for r in rs]
        sc = score.get(arm, {})
        paired = [r["brand_fit"] - base[r["sample_id"]]["brand_fit"] for r in rs
                  if arm != baseline and r["sample_id"] in base]
        out[arm] = {
            "n": len(rs), "brand_fit_mean": round(mean(fits), 2), "brand_fit_median": median(fits),
            "retry_pct": round(100 * sum(f < MIN_QUALITY for f in fits) / len(fits), 1),
            "retry_outside_tie_pct": round(100 * sum(f < MIN_QUALITY - TIE for f in fits) / len(fits), 1),
            "generic_feedback_pct": round(100 * sum(bool(r.get("feedback_says_generic")) for r in rs) / len(rs), 1),
            "capped_pct": round(100 * sum(1 for r in rs if r.get("caps")) / len(rs), 1),
            "unsupported_per_page": (round(mean(r.get("unsupported", 0) for r in rs), 2)
                                     if any("unsupported" in r for r in rs) else None),
            "panel_serious_per_claim": sc.get("serious_per_claim_pooled"),
            "panel_serious_per_page": sc.get("serious_per_page"),
            "vs_baseline": ({"paired": len(paired), "mean_diff": round(mean(paired), 2), **sign_test(paired)}
                            if paired else None),
        }
    return out


def page_spearman(rows: list[dict], panel: dict) -> dict:
    pts = [(r["brand_fit"], panel[r["page_key"]]["rate"]) for r in rows
           if r["page_key"] in panel and panel[r["page_key"]]["rate"] is not None]
    return {"pages_joined": len(pts), "spearman_brand_fit_vs_serious_rate": spearman([a for a, _ in pts],
                                                                                      [b for _, b in pts])}


def criterion(table: dict, sp: dict, candidate: str = CANDIDATE, baseline: str = BASELINE) -> dict:
    """The pre-registered pass criterion on one rubric's numbers."""
    c, b = table.get(candidate), table.get(baseline)
    rho = sp.get("spearman_brand_fit_vs_serious_rate")
    if not c or not b or rho is None:
        return {"pass": None, "reason": "missing arm or panel join", "candidate": candidate, "baseline": baseline}
    retry_ok = c["retry_pct"] <= b["retry_pct"] + RETRY_MARGIN_POINTS
    rho_ok = rho <= SPEARMAN_MAX
    return {"pass": bool(retry_ok and rho_ok), "retry_ok": retry_ok, "spearman_ok": rho_ok,
            "candidate_retry_pct": c["retry_pct"], "baseline_retry_pct": b["retry_pct"],
            "retry_margin_points": RETRY_MARGIN_POINTS, "spearman": rho, "spearman_max": SPEARMAN_MAX,
            "candidate": candidate, "baseline": baseline}


def analyse(result: dict, score: dict, panel: dict, v1_result: Optional[dict] = None,
            baseline: str = BASELINE) -> dict:
    rows = rows_of(result)
    if v1_result:
        rows += rows_of(v1_result, "v1_s248")
    out = {"judge_model": result.get("judge_model"), "judge_mode": result.get("judge_mode"),
           "code": result.get("code"), "rubric_v2_version": result.get("rubric_v2_version"),
           "spend_usd": result.get("spend_usd"), "calls": result.get("calls"),
           "skipped_cost_cap": len(result.get("skipped_cost_cap") or []), "rubrics": {}}
    for rubric in sorted({r["rubric"] for r in rows}):
        rr = [r for r in rows if r["rubric"] == rubric]
        table = arm_table(rr, score, baseline)
        sp = page_spearman(rr, panel)
        arm_pts = [(v["brand_fit_mean"], v["panel_serious_per_claim"]) for v in table.values()
                   if v["panel_serious_per_claim"] is not None]
        out["rubrics"][rubric] = {"arms": table, "page_level": sp,
                                  "arm_level_spearman": spearman([a for a, _ in arm_pts], [b for _, b in arm_pts]),
                                  "criterion": criterion(table, sp, baseline=baseline)}
    v2 = out["rubrics"].get("v2")
    out["verdict"] = {"rubric_v2_passes": v2["criterion"]["pass"] if v2 else None,
                      "next": ("rubric v2 may gate writer-v2 drafts again: A1_V2_BRAND_FIT_GATE=enforce on the "
                               "worker (Nghiep's call)" if v2 and v2["criterion"]["pass"]
                               else "keep brand fit report-only for writer v2 (A1_V2_BRAND_FIT_GATE unset)")}
    return out


def table_text(analysis: dict) -> str:
    lines = []
    hdr = (f"{'rubric':<8}{'arm':<18}{'n':>4}{'fit':>6}{'med':>5}{'retry%':>8}{'<6.5%':>7}{'gen%':>6}{'cap%':>6}"
           f"{'uns/p':>7}{'ser/claim':>10}{'Δfit':>7}{'lo/hi/tie':>11}{'p':>7}")
    lines.append(hdr)
    for rubric, block in analysis["rubrics"].items():
        for arm, r in block["arms"].items():
            vb = r["vs_baseline"] or {}
            lht = f"{vb.get('lower')}/{vb.get('higher')}/{vb.get('ties')}" if vb else "-"
            unsup = "-" if r["unsupported_per_page"] is None else str(r["unsupported_per_page"])
            lines.append(f"{rubric:<8}{arm:<18}{r['n']:>4}{r['brand_fit_mean']:>6}{r['brand_fit_median']:>5}"
                         f"{r['retry_pct']:>8}{r['retry_outside_tie_pct']:>7}{r['generic_feedback_pct']:>6}"
                         f"{r['capped_pct']:>6}{unsup:>7}"
                         f"{str(r['panel_serious_per_claim']):>10}{str(vb.get('mean_diff', '-')):>7}{lht:>11}"
                         f"{str(vb.get('p', '-')):>7}")
        c = block["criterion"]
        lines.append(f"{rubric:<8}spearman(page)={block['page_level']['spearman_brand_fit_vs_serious_rate']} "
                     f"(n={block['page_level']['pages_joined']}) arm-level={block['arm_level_spearman']} "
                     f"criterion pass={c.get('pass')} (retry {c.get('candidate_retry_pct')} <= "
                     f"{c.get('baseline_retry_pct')} + {c.get('retry_margin_points')}: {c.get('retry_ok')}; "
                     f"spearman <= 0: {c.get('spearman_ok')})")
    lines.append(f"VERDICT rubric_v2_passes={analysis['verdict']['rubric_v2_passes']} -> {analysis['verdict']['next']}")
    lines.append(f"judge={analysis['judge_model']} mode={analysis['judge_mode']} code={analysis.get('code')} "
                 f"rubric={analysis['rubric_v2_version']} spend=${analysis['spend_usd']} calls={analysis['calls']} "
                 f"skipped_cap={analysis['skipped_cost_cap']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", required=True)
    ap.add_argument("--score", required=True)
    ap.add_argument("--pass-a", required=True)
    ap.add_argument("--pass-b", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--v1-result", help="the S248 v1 run (brandfit/result.json), shown as rubric v1_s248")
    ap.add_argument("--baseline", default=BASELINE)
    ap.add_argument("--json", help="also write the analysis JSON here")
    args = ap.parse_args(argv)

    def load(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    res = load(args.result)
    if not res.get("rows"):
        print(json.dumps({k: res.get(k) for k in ("verdict", "brand_has_signals", "judge_model", "dry_run")}, indent=1))
        return 0
    panel = panel_by_key([load(args.pass_a), load(args.pass_b)], load(args.key))
    analysis = analyse(res, load(args.score)["by_arm_model"], panel,
                       load(args.v1_result) if args.v1_result else None, args.baseline)
    print(table_text(analysis))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(analysis, f, indent=1, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
