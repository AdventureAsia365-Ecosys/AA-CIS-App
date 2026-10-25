#!/usr/bin/env python3
"""AA-790 — census of the post-repair integrity detectors over stored T9 pieces. Read-only, no LLM, $0.

Runs ``text_integrity.structure_findings`` (split paragraphs, open paragraphs, dangling tails — the AA-790
detectors) and, for context, ``text_integrity.fragments`` (lower-case starts, unbalanced brackets) on every piece,
the same functions the repair contract reads. Counts are per finding kind and per pieces with that kind. Bar
(plan AA-790 slice A): the new detectors fire on 0 approved pieces the labels call clean (as_is / light); every
hit is listed so a reviewer can read it.

Local (the S234 label pack + the AA-729 fixture, root repo paths):

  python -m scripts.aa790.integrity_census --golden ../../docs/eval/aa729/fixture-1.1.0.json \\
      --pack ../../docs/audits/2026-10-13-S234-t-series-rounds/label-pack/S234-label-40.xlsx \\
      --panel-key ../../docs/audits/2026-10-13-S234-t-series-rounds/label-pack/agreement/panel_key.json

DB rows: export them in the api container with ``corpus.py --db --export`` (see ``scripts/aa790/corpus.py``), then
``--rows <file>`` here. ``--out`` writes the full JSON (hits quote piece text: keep it in the private root audit
folder, never in this public repo).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.getcwd())

from scripts.aa790 import corpus  # noqa: E402
from services.acp_content_writing.text_integrity import fragments, structure_findings  # noqa: E402


def census(rows: list[dict]) -> dict:
    counts: Counter = Counter()
    hits = []
    clean_approved_hits = 0
    for r in rows:
        found = structure_findings(r["text"], r.get("cta"))
        frag = fragments(r["text"])
        counts[("pieces", r["source"], r.get("status"))] += 1
        for f in found:
            counts[(f["kind"], r["source"], r.get("status"))] += 1
        for kind in {f["kind"] for f in found}:
            counts[(f"pieces_with_{kind}", r["source"], r.get("status"))] += 1
        if frag:
            counts[("fragments", r["source"], r.get("status"))] += 1
        if found:
            clean = r.get("label") in corpus.LIGHT and r.get("status") == "approved"
            clean_approved_hits += clean
            hits.append({"id": r["id"], "piece_id": r.get("piece_id"), "tenant": r.get("tenant"),
                         "channel": r.get("channel"), "status": r.get("status"), "label": r.get("label"),
                         "clean_approved": clean,
                         "findings": [{"kind": f["kind"], "why": f["why"], "text": f["text"][:300]}
                                      for f in found]})
    return {"pieces": len(rows), "counts": {" / ".join(str(x) for x in k): v for k, v in sorted(counts.items(),
                                                                                           key=str)},
            "pieces_with_hits": len(hits), "clean_approved_hits": clean_approved_hits, "hits": hits}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    corpus.add_args(ap)
    ap.add_argument("--out", help="write the full JSON here")
    args = ap.parse_args()
    rows = asyncio.run(corpus.load(args))
    if not rows:
        ap.error("no input: pass --golden, --pack or --db")
    res = census(rows)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1, ensure_ascii=False)
    print(f"pieces: {res['pieces']}  with a hit: {res['pieces_with_hits']}  "
          f"approved + labelled clean with a hit: {res['clean_approved_hits']}")
    for k, v in res["counts"].items():
        print(f"  {k}: {v}")
    for h in res["hits"]:
        print(f"- {h['id']} {h['tenant']} {h['channel']} {h['status']} label={h['label']}")
        for f in h["findings"]:
            print(f"    {f['why']}: {f['text'][:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
