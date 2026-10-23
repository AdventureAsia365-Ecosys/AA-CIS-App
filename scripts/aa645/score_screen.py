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
* code gates (pages with ≥ 1 gate code: DAY_*, META_CUT, META_LENGTH, TEXT_TRUNCATED, NUMBER_NOT_IN_SOURCE,
  INCLUSION_CONTRADICTED — must be ~0 before a win counts), report-only codes, figure coverage;
* calls and $ per tour.

`--baseline <arm|model>` adds the paired comparison every other arm × model makes against it on the same tours: the
median per-tour difference in serious-error rate and the Wilcoxon signed-rank p (normal approximation, zero
differences dropped) — the A/B run 1 / run 2 test. The screen's pick (`--pick`): the lowest pooled serious-error rate
with zero gate pages; a model within 10% of the best rate and cheaper per tour wins the tie (q4-q8: "hoà thì lấy
model rẻ"). 12 tours can pick the wrong model; the report says so.

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

GATE_PREFIXES = ("DAY_", "META_CUT", "META_LENGTH", "TEXT_TRUNCATED", "NUMBER_NOT_IN_SOURCE", "INCLUSION_CONTRADICTED")
REPORT_CODES = ("NUMBER_NOUN_MISMATCH", "NAME_NOT_IN_SOURCE", "DAY_PLACE_MISMATCH")
TIE_BAND = 0.10


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
    """Two-sided Wilcoxon signed-rank p, normal approximation with tie correction; zeros dropped. (n, p)."""
    d = [x for x in diffs if x != 0]
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
            codes = {f["code"] for f in u.get("findings") or [] if f["code"] != "SOURCE_DAY_CONTRADICTION"}
            gates.update({("DAY_*" if c.startswith("DAY_") else c) for c in codes if c.startswith(GATE_PREFIXES)})
            reports.update({c for c in codes if c in REPORT_CODES})
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
            "pages_with_gate_code": sum(1 for u in ok if any(f["code"].startswith(GATE_PREFIXES)
                                                             and f["code"] != "SOURCE_DAY_CONTRADICTION"
                                                             for f in u.get("findings") or [])),
            "gate_codes_pages": dict(sorted(gates.items())), "report_codes_pages": dict(sorted(reports.items())),
            "figure_coverage_mean": round(sum(cov) / len(cov), 3) if cov else None,
            "calls_per_tour": round(sum(u.get("n_calls", 0) for u in paid) / len(paid), 2) if paid else 0,
            "usd_per_tour": round(sum(u.get("cost_usd", 0) for u in paid) / len(paid), 4) if paid else 0,
        }
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


def pick(table: dict, arm: str = "w") -> dict:
    """The screen's W choice among `arm|*` rows: zero gate pages, lowest pooled serious rate; a cheaper model within
    TIE_BAND of the best rate wins."""
    rows = {k: v for k, v in table.items() if k.startswith(arm + "|") and v["serious_per_claim_pooled"] is not None}
    if not rows:
        return {"pick": None, "why": "no panel-scored rows for arm " + arm}
    clean = {k: v for k, v in rows.items() if v["pages_with_gate_code"] == 0} or rows
    best = min(v["serious_per_claim_pooled"] for v in clean.values())
    near = {k: v for k, v in clean.items() if v["serious_per_claim_pooled"] <= best * (1 + TIE_BAND) + 1e-9}
    choice = min(near, key=lambda k: (near[k]["usd_per_tour"], near[k]["serious_per_claim_pooled"]))
    return {"pick": choice, "best_rate": best, "within_tie_band": sorted(near),
            "gate_clean": sorted(k for k, v in rows.items() if v["pages_with_gate_code"] == 0),
            "caveat": "12 tours choose a writer, they do not prove one (q4-q8 §2.2 step 1); confirm in A/B run 1."}


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
        print(f"{am:28s} n={row['units']:2d} err={row['errors']} rate={row['serious_per_claim_pooled']} "
              f"rank={row['rank_mean']} gates={row['pages_with_gate_code']} cov={row['figure_coverage_mean']} "
              f"calls={row['calls_per_tour']} ${row['usd_per_tour']}" + (f" vs={row['vs_baseline']}"
                                                                         if "vs_baseline" in row else ""))
    if args.pick:
        print("pick:", json.dumps(result["pick"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
