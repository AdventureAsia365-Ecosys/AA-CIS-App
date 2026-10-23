#!/usr/bin/env python3
"""AA-645 / M2a S1-first — score a `s1_screen.py` run: blind panel passes + code checks + cost, per arm × model.
Offline, $0, no DB, no LLM.

Inputs: the run folder (`pages.json`, `panel/key.json`; downloaded from s3://<bronze>/scripts/out/aa645/<run>/) and
one or more panel pass files written against `panel/packet.json` (rubric v1 + the rank question):

  {"pass": "a", "pages": [{"page_id": "p001", "verdict": "...", "agency_use": "...", "checkable_claims": 21,
                           "errors": [{"class": "...", "severity": "serious", "field": "...", "quote": "..."}]}],
   "ranks": [{"group_id": "g01", "order": [["p002"], ["p001", "p003"]]}]}

Per arm × model (several passes are averaged page by page):

* **primary (q4-q8 §2.2):** serious meaning errors / checkable claims — pooled, and the per-page mean;
* rank: mean normalised rank inside each tour (0 = always first, 1 = always last; ties share the mean position);
* verdict / agency_use counts;
* code gates (pages with ≥ 1 gate code, `scripts/aa645/gates.py`: the day-structure DAY_* codes except the
  report-only DAY_PLACE_MISMATCH, META_CUT, META_LENGTH, TEXT_TRUNCATED, NUMBER_NOT_IN_SOURCE, INCLUSION_CONTRADICTED
  — must be ~0 before a win counts), report-only codes, figure coverage;
* calls and $ per tour.

`--baseline <arm|model>` adds the paired comparison every other arm × model makes against it on the same tours: the
median per-tour difference in serious-error rate and the Wilcoxon signed-rank p (normal approximation, zero
differences dropped) — the A/B run 1 / run 2 test. The screen's pick (`--pick`): only ELIGIBLE rows compete — zero
gate pages, zero writer errors and EVERY sample of the arm completed and panel-scored (a model that errored
everywhere or ran only in a short trial is excluded, with its reason in `rows`, and does not shrink the comparison
set); among them the lowest serious-error rate pooled over the arm's tours (`serious_per_claim_common`); a model
within the tie band (10%, at least 0.005 absolute) of the best rate and cheaper per tour wins the tie (q4-q8: "hoà
thì lấy model rẻ"). No eligible row → no pick (`gate_clean_pick: false`). 12 tours can pick the wrong model; the
report says so.

`--export-examples <prefixes> --arm w --model <key>` writes the chosen pages as reference-example DRAFTS
(`[{"tour_id", "page"}]`) for Nghiệp to edit; the file holds tour text, so it lives in the private root repo.

  python3 -m scripts.aa645.score_screen --run <dir> --passes pass-a.json pass-b.json --pick --out score.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from statistics import median
from typing import Optional

sys.path.insert(0, os.getcwd())

from scripts.aa645.gates import gate_label, is_report, page_gate_codes  # noqa: E402

TIE_BAND = 0.10
TIE_FLOOR = 0.005          # absolute: with a best rate of 0, "within 10%" alone would admit only exact zeros


def arm_model(u: dict) -> str:
    return f"{u['arm']}|{u['model']}"


def panel_by_unit(passes: list[dict], key: dict) -> dict[str, dict]:
    """unit key → {"serious", "minor", "claims", "verdicts", "agency", "ranks"} averaged over passes."""
    acc: dict[str, dict] = defaultdict(lambda: {"serious": [], "minor": [], "claims": [], "verdicts": Counter(),
                                                "agency": Counter(), "ranks": []})
    for p in passes:
        for pg in p.get("pages") or []:
            k = key.get(pg["page_id"])
            if not k:
                continue
            errs = pg.get("errors") or []
            a = acc[k["key"]]
            a["serious"].append(sum(1 for e in errs if e.get("severity") == "serious"))
            a["minor"].append(sum(1 for e in errs if e.get("severity") != "serious"))
            a["claims"].append(int(pg.get("checkable_claims") or 0))
            a["verdicts"][pg.get("verdict") or "?"] += 1
            a["agency"][pg.get("agency_use") or "?"] += 1
        for g in p.get("ranks") or []:
            order = g.get("order") or []
            n = sum(len(tier) for tier in order)
            pos = 0
            for tier in order:
                mean_pos = pos + (len(tier) - 1) / 2
                for pid in tier:
                    k = key.get(pid)
                    if k and n > 1:
                        acc[k["key"]]["ranks"].append(mean_pos / (n - 1))
                pos += len(tier)
    out = {}
    for k, a in acc.items():
        m = len(a["claims"]) or 1
        out[k] = {"serious": sum(a["serious"]) / m, "minor": sum(a["minor"]) / m, "claims": sum(a["claims"]) / m,
                  "verdicts": dict(a["verdicts"]), "agency": dict(a["agency"]),
                  "rank": (sum(a["ranks"]) / len(a["ranks"])) if a["ranks"] else None}
    return out


def rate(p: dict) -> Optional[float]:
    return p["serious"] / p["claims"] if p.get("claims") else None


def wilcoxon(diffs: list[float]) -> tuple[int, Optional[float]]:
    """Two-sided Wilcoxon signed-rank p, normal approximation with tie correction; zeros dropped. (n, p).
    Differences are rounded to 9 digits first, so float noise (0.1 - 0.05 vs 0.15 - 0.1) neither splits a tie nor
    keeps a zero."""
    d = [x for x in (round(v, 9) for v in diffs) if x != 0]
    n = len(d)
    if n == 0:
        return 0, None
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    ties = 0.0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        r = (i + j + 2) / 2
        for k in range(i, j + 1):
            ranks[order[k]] = r
        t = j - i + 1
        ties += t ** 3 - t
        i = j + 1
    w_plus = sum(r for r, x in zip(ranks, d) if x > 0)
    mean = n * (n + 1) / 4
    var = n * (n + 1) * (2 * n + 1) / 24 - ties / 48
    if var <= 0:
        return n, None
    z = (w_plus - mean) / math.sqrt(var)
    return n, round(math.erfc(abs(z) / math.sqrt(2)), 4)


def score(units: list[dict], panel: dict[str, dict], baseline: Optional[str]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for u in units:
        groups[arm_model(u)].append(u)
    table = {}
    for am, us in sorted(groups.items()):
        ok = [u for u in us if not u.get("error")]
        scored = [panel[u["key"]] for u in ok if u["key"] in panel]
        serious = sum(p["serious"] for p in scored)
        claims = sum(p["claims"] for p in scored)
        per_page = [r for r in (rate(p) for p in scored) if r is not None]
        ranks = [p["rank"] for p in scored if p["rank"] is not None]
        gates, reports = Counter(), Counter()
        for u in ok:
            gates.update({gate_label(c) for c in page_gate_codes(u.get("findings"))})
            reports.update({f["code"] for f in u.get("findings") or [] if is_report(f["code"])})
        paid = [u for u in us if u["arm"] != "stored"]
        cov = [u["figure_coverage"] for u in ok if u.get("figure_coverage") is not None]
        verdicts, agency = Counter(), Counter()
        for p in scored:
            verdicts.update(p["verdicts"])
            agency.update(p["agency"])
        table[am] = {
            "units": len(us), "errors": len(us) - len(ok), "panel_scored": len(scored),
            "serious_per_claim_pooled": round(serious / claims, 4) if claims else None,
            "serious_per_claim_page_mean": round(sum(per_page) / len(per_page), 4) if per_page else None,
            "serious_per_page": round(serious / len(scored), 2) if scored else None,
            "rank_mean": round(sum(ranks) / len(ranks), 3) if ranks else None,
            "verdicts": dict(verdicts), "agency_use": dict(agency),
            "pages_with_gate_code": sum(1 for u in ok if page_gate_codes(u.get("findings"))),
            "gate_codes_pages": dict(sorted(gates.items())), "report_codes_pages": dict(sorted(reports.items())),
            "figure_coverage_mean": round(sum(cov) / len(cov), 3) if cov else None,
            "calls_per_tour": round(sum(u.get("n_calls", 0) for u in paid) / len(paid), 2) if paid else 0,
            "usd_per_tour": round(sum(u.get("cost_usd", 0) for u in paid) / len(paid), 4) if paid else 0,
            "retried": sum(1 for u in us if (u.get("attempt") or 1) > 1),
            "retry_usd": round(sum(a.get("cost_usd") or 0 for u in us for a in u.get("prior_attempts") or []), 4),
        }
    _common_rates(groups, panel, table)
    if baseline:
        base = {u["sample_id"]: panel.get(u["key"]) for u in groups.get(baseline, [])}
        for am, us in groups.items():
            if am == baseline:
                continue
            diffs = []
            for u in us:
                b, p = base.get(u["sample_id"]), panel.get(u["key"])
                if b and p and rate(b) is not None and rate(p) is not None:
                    diffs.append(rate(p) - rate(b))
            n, pval = wilcoxon(diffs)
            table[am]["vs_baseline"] = {"baseline": baseline, "paired_tours": len(diffs),
                                        "median_rate_diff": round(median(diffs), 4) if diffs else None,
                                        "wilcoxon_n_nonzero": n, "wilcoxon_p": pval}
    return table


def _common_rates(groups: dict[str, list[dict]], panel: dict[str, dict], table: dict) -> None:
    """Per arm: which rows are ELIGIBLE for the comparison, and each row's serious-error rate pooled over the common
    tours of the eligible rows (so rows are compared on the same pages: a model whose hard tours failed is not
    rewarded with an easier set).

    A row is eligible only with zero gate pages, zero writer errors and EVERY sample of the arm (the union of the
    arm's sample ids over all its rows) completed and panel-scored. The common set is computed over eligible rows
    only, so it is the arm's full sample set; a row that errored everywhere or ran only in a 3-tour trial is excluded
    with its reason (`excluded_reason`) instead of shrinking the set for everyone. No eligible row → common set empty
    and every rate None. An ineligible row that still covers the common set gets its rate there for information."""
    by_arm: dict[str, dict[str, dict[str, dict]]] = defaultdict(dict)
    arm_samples: dict[str, set] = defaultdict(set)
    for am, us in groups.items():
        arm = am.split("|", 1)[0]
        arm_samples[arm].update(u["sample_id"] for u in us)
        by_arm[arm][am] = {u["sample_id"]: panel[u["key"]] for u in us
                           if not u.get("error") and u["key"] in panel}
    for arm, rows in by_arm.items():
        need = arm_samples[arm]
        for am, scored in rows.items():
            row = table[am]
            reasons = []
            if row["errors"]:
                reasons.append(f"{row['errors']} writer error(s)")
            if row["pages_with_gate_code"]:
                reasons.append(f"{row['pages_with_gate_code']} page(s) with a gate code")
            have = len(need & set(scored))
            if have < len(need):
                reasons.append(f"{have}/{len(need)} samples of the arm completed and panel-scored")
            row["eligible"] = not reasons
            row["excluded_reason"] = "; ".join(reasons) or None
        common = set(need) if any(table[am]["eligible"] for am in rows) else set()
        for am, scored in rows.items():
            covers = bool(common) and common <= set(scored)
            serious = sum(scored[s]["serious"] for s in common) if covers else 0
            claims = sum(scored[s]["claims"] for s in common) if covers else 0
            table[am]["common_tours"] = len(common)
            table[am]["serious_per_claim_common"] = round(serious / claims, 4) if claims else None


def pick(table: dict, arm: str = "w") -> dict:
    """The screen's W choice among `arm|*` rows. Only ELIGIBLE rows compete (`_common_rates`: zero gate pages, zero
    writer errors, every sample of the arm completed and panel-scored); among them the lowest serious rate on the
    common tours; a cheaper model within the tie band (TIE_BAND relative, TIE_FLOOR absolute) of the best wins.
    No eligible row → pick None: the screen did not find a usable writer. Every row is listed in `rows`, an
    excluded one with its reason. A row whose units needed `--retry-errors` stays eligible (the retry succeeded)
    but is named in `warnings`, and `pick_retried` shows it for the chosen row: a writer that needs retries is less
    reliable than its final error count of 0 says."""
    caveat = "12 tours choose a writer, they do not prove one (q4-q8 §2.2 step 1); confirm in A/B run 1."
    arm_rows = {k: v for k, v in table.items() if k.startswith(arm + "|")}
    coverage = {k: {"units": v["units"], "errors": v["errors"], "panel_scored": v["panel_scored"],
                    "common_tours": v.get("common_tours"), "pages_with_gate_code": v["pages_with_gate_code"],
                    "eligible": bool(v.get("eligible")), "excluded_reason": v.get("excluded_reason"),
                    "retried": v.get("retried", 0)}
                for k, v in arm_rows.items()}
    warnings = [f"{k}: {v['retried']} unit(s) needed a retry (--retry-errors) — the writer is less reliable than "
                f"its error count shows" for k, v in sorted(coverage.items()) if v["retried"]]
    if not any(v.get("panel_scored") for v in arm_rows.values()):
        return {"pick": None, "gate_clean_pick": False, "why": "no panel-scored rows for arm " + arm,
                "rows": coverage, "warnings": warnings, "caveat": caveat}
    clean = {k: v for k, v in arm_rows.items()
             if v.get("eligible") and v.get("serious_per_claim_common") is not None}
    if not clean:
        return {"pick": None, "gate_clean_pick": False,
                "why": ("no eligible row of arm " + arm + " (zero gate pages, zero writer errors, every sample "
                        "completed and panel-scored); see rows[*].excluded_reason"),
                "rows": coverage, "warnings": warnings, "caveat": caveat}
    best = min(v["serious_per_claim_common"] for v in clean.values())
    limit = max(best * (1 + TIE_BAND), best + TIE_FLOOR) + 1e-9
    near = {k: v for k, v in clean.items() if v["serious_per_claim_common"] <= limit}
    choice = min(near, key=lambda k: (near[k]["usd_per_tour"], near[k]["serious_per_claim_common"]))
    return {"pick": choice, "gate_clean_pick": True, "best_rate": best, "rate": "serious_per_claim_common",
            "within_tie_band": sorted(near), "gate_clean": sorted(clean), "pick_retried": coverage[choice]["retried"],
            "rows": coverage, "warnings": warnings, "caveat": caveat}


def export_examples(units: list[dict], prefixes: list[str], arm: str, model: str) -> list[dict]:
    out = []
    for u in units:
        if u["arm"] == arm and u["model"] == model and not u.get("error") and any(
                str(u["sample_id"]).startswith(p) or str(u.get("tour_id") or "").startswith(p) for p in prefixes):
            page = {k: v for k, v in (u.get("output") or {}).items() if k != "trip_type"}
            out.append({"tour_id": u.get("tour_id") or u["sample_id"], "draft_from": f"{arm}|{model}",
                        "edited_by_nghiep": False, "page": page})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--run", required=True, help="run folder with pages.json and panel/key.json")
    ap.add_argument("--passes", nargs="*", default=[])
    ap.add_argument("--baseline", help="arm|model to pair against (e.g. aplus|haiku)")
    ap.add_argument("--pick", action="store_true")
    ap.add_argument("--export-examples", help="comma list of tour id prefixes")
    ap.add_argument("--arm", default="w")
    ap.add_argument("--model")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    units = json.load(open(os.path.join(args.run, "pages.json")))
    if args.export_examples:
        if not args.model:
            raise SystemExit("--export-examples needs --model")
        ex = export_examples(units, [p.strip() for p in args.export_examples.split(",") if p.strip()],
                             args.arm, args.model)
        json.dump(ex, open(args.out, "w"), indent=1, ensure_ascii=False)
        print(f"{len(ex)} example drafts -> {args.out}")
        return 0
    key = json.load(open(os.path.join(args.run, "panel", "key.json")))
    passes = [json.load(open(p)) for p in args.passes]
    table = score(units, panel_by_unit(passes, key), args.baseline)
    result = {"passes": len(passes), "by_arm_model": table}
    if args.pick:
        result["pick"] = pick(table, args.arm)
    json.dump(result, open(args.out, "w"), indent=1, ensure_ascii=False)
    for am, row in table.items():
        print(f"{am:28s} n={row['units']:2d} err={row['errors']} scored={row['panel_scored']} "
              f"rate={row['serious_per_claim_pooled']} common={row.get('serious_per_claim_common')}"
              f"/{row.get('common_tours')} "
              f"rank={row['rank_mean']} gates={row['pages_with_gate_code']} cov={row['figure_coverage_mean']} "
              f"calls={row['calls_per_tour']} ${row['usd_per_tour']} retried={row['retried']}"
              + (f" vs={row['vs_baseline']}" if "vs_baseline" in row else ""))
    if args.pick:
        print("pick:", json.dumps(result["pick"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
