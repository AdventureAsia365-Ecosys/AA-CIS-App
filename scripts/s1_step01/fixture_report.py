#!/usr/bin/env python3
"""S1-first step 1 (M2a, S248) — fact tables + code checks over a set of written S1 pages. Offline, $0, no LLM,
no DB.

Input: the AA-729 golden fixture (`--fixture`, its `s1` samples: input.tour = the raw source the writer read,
output = the stored page) or any JSON list of {sample_id, tour, output} (`--pages`, e.g. an A/B arm in step 3).
Output (`--out`, JSON): per sample the fact table, `verify_pointers` violations and every code-check finding;
plus totals per code. With `--ground-truth` (the root audit's ground-truth.json: class → sample ids) it adds a
per-class confusion table (sample level) and the TPR / TNR with Wilson 95% bounds.

The fixture and the results hold tour text, so both stay in the private root repo; this public repo carries the
code only.

  python3 -m scripts.s1_step01.fixture_report --fixture <root>/docs/eval/aa729/fixture-1.1.0.json \
      --ground-truth <audit>/ground-truth.json --out <audit>/results.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from services.quality.s1_checks import run_checks  # noqa: E402
from services.quality.s1_facts import build_fact_table, summary_counts, verify_pointers  # noqa: E402

# The code-check class each ground-truth class is caught by (semantic_other has none on purpose).
CLASS_OF_CODE_PREFIX = {
    "day_structure": ("DAY_", "SOURCE_DAY_CONTRADICTION"),
    "meta_cut": ("META_CUT",),
    "truncated_text": ("TEXT_TRUNCATED",),
    "number": ("NUMBER_",),
    "name": ("NAME_NOT_IN_SOURCE",),
    "inclusion": ("INCLUSION_CONTRADICTED",),
}
# A source contradiction is the source's fault: it is reported, never counted as a writer hit.
NOT_A_WRITER_HIT = frozenset({"SOURCE_DAY_CONTRADICTION"})


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round((c - r) / d, 3), round((c + r) / d, 3))


def load_pages(args) -> list[dict]:
    if args.fixture:
        fx = json.load(open(args.fixture))
        return [{"sample_id": s["sample_id"], "tour": s["input"]["tour"], "output": s["output"],
                 "label": {k: s["label"].get(k) for k in ("verdict", "grade")}}
                for s in fx["samples"] if s.get("kind") == "s1"]
    return json.load(open(args.pages))


def run(pages: list[dict]) -> dict:
    rows = []
    totals: Counter = Counter()
    for p in pages:
        facts = build_fact_table(p["tour"])
        findings = run_checks(p["tour"], p["output"], facts)
        totals.update({f["code"] for f in findings})
        rows.append({"sample_id": p["sample_id"], "label": p.get("label"), "fact_summary": summary_counts(facts),
                     "pointer_violations": verify_pointers(facts, p["tour"]), "findings": findings,
                     "facts": facts})
    return {"n": len(rows), "samples_with_code": dict(sorted(totals.items())), "rows": rows}


def confusion(result: dict, gt: dict) -> dict:
    ids = [r["sample_id"] for r in result["rows"]]
    by_id = {r["sample_id"]: r for r in result["rows"]}
    passes = {r["sample_id"] for r in result["rows"] if (r.get("label") or {}).get("verdict") == "pass"}
    out = {}
    for cls, prefixes in CLASS_OF_CODE_PREFIX.items():
        pos = set(gt["samples"].get(cls, []))

        def hit(sid, prefixes=prefixes):
            return any(f["code"].startswith(prefixes) and f["code"] not in NOT_A_WRITER_HIT
                       for f in by_id[sid]["findings"])
        tp = sorted(s for s in ids if s in pos and hit(s))
        fn = sorted(s for s in ids if s in pos and not hit(s))
        fp = sorted(s for s in ids if s not in pos and hit(s))
        tn = [s for s in ids if s not in pos and not hit(s)]
        out[cls] = {
            "positives": len(pos), "tp": len(tp), "fn": len(fn), "fp": len(fp), "tn": len(tn),
            "tpr": round(len(tp) / len(pos), 3) if pos else None, "tpr_wilson95": wilson(len(tp), len(pos)),
            "tnr": round(len(tn) / (len(tn) + len(fp)), 3) if (tn or fp) else None,
            "tnr_wilson95": wilson(len(tn), len(tn) + len(fp)),
            "fires_on_label_pass": sorted(s for s in passes if hit(s)),
            "tp_ids": tp, "fn_ids": fn, "fp_ids": fp,
        }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--fixture")
    src.add_argument("--pages")
    ap.add_argument("--ground-truth")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    result = run(load_pages(args))
    if args.ground_truth:
        result["confusion"] = confusion(result, json.load(open(args.ground_truth)))
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=1, ensure_ascii=False, default=str)
    viol = sum(len(r["pointer_violations"]) for r in result["rows"])
    print(f"samples={result['n']} pointer_violations={viol}")
    print("samples with code:", json.dumps(result["samples_with_code"]))
    for cls, c in (result.get("confusion") or {}).items():
        print(f"{cls:15s} pos={c['positives']:2d} tp={c['tp']:2d} fn={c['fn']:2d} fp={c['fp']:2d} tn={c['tn']:2d} "
              f"tpr={c['tpr']} tnr={c['tnr']} on_pass={c['fires_on_label_pass']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
