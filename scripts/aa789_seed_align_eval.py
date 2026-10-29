#!/usr/bin/env python3
"""AA-789 tier 1 — $0 offline harness: seed alignment (and main's deterministic checks) on the eval set or on stored
pieces. No database, no LLM, no network.

The eval set and the stored-piece export hold real tenant copy: keep them in the gitignored scratch folder, never in
the repo. This script prints no copy except in `predict` output, which is written to the path you give (keep it in the
scratch folder too).

  predict EVAL.jsonl OUT.jsonl [--baseline]
      One prediction per eval item ({"id", "flagged", "class"}), for the eval set's score.py
      (`score.py OUT.jsonl --strict-class`). Each item's `seed_excerpt` is its seed; brief lines in it ("writer
      instruction, not copy") are read as the brief. --baseline runs main's checks instead (hygiene.find_issues leak +
      repeat, trip_facts.find_trip_fact_issues, quality_gates.faq_answer_problem): the row tier 1 is compared with.
  census EXPORT.json
      Tier 1 over every piece of a decoded AA-783 export (`aa783_export.py`): fire counts per class and per channel,
      and the run time. Kinds and counts only, no copy.

Run with the repo on PYTHONPATH and spaCy + en_core_web_sm installed (requirements.txt).
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter

_FAQ_RE = re.compile(r"\*\*Q:\s*(.+?)\*\*\s*\n+A:\s*(.+)", re.S)


def _seed_align(text: str, seed: str, briefs: list[str]) -> list:
    from services.acp_content_writing.seed_align import checks, seed_graph

    g = seed_graph.build(seed, briefs)
    found = checks.find_candidates(text, g, names=set(g.place_names))
    if found is None:
        sys.exit("spaCy or en_core_web_sm is not installed")
    return found


def _baseline(text: str, seed: str) -> bool:
    from services.acp_content_writing import hygiene as h
    from services.acp_content_writing import quality_gates as qg
    from services.acp_content_writing import trip_facts as tf

    trip = tf.find_trip_fact_issues(text, tf.TripFactContext(seed=seed, channel="blog"))
    issues = h.find_issues(text, h.HygieneContext(seed=seed, brand_name="Pretend Brand", cta=None))
    faq = [m for m in _FAQ_RE.finditer(text) if qg.faq_answer_problem(m.group(1), m.group(2), seed)]
    return bool(trip["trip_fact"] or trip["rarity"] or issues["leak"] or issues["repeat"] or faq)


def predict(eval_path: str, out_path: str, baseline: bool) -> int:
    started, n = time.monotonic(), 0
    with open(eval_path, encoding="utf-8") as f, open(out_path, "w", encoding="utf-8") as out:
        for line in f:
            if not line.strip():
                continue
            item = json.loads(line)
            n += 1
            if baseline:
                row = {"id": item["id"], "flagged": _baseline(item["text"], item["seed_excerpt"])}
            else:
                found = _seed_align(item["text"], item["seed_excerpt"], [])
                row = {"id": item["id"], "flagged": bool(found)}
                if found:
                    row["class"] = found[0].cls
            out.write(json.dumps(row) + "\n")
    print(json.dumps({"items": n, "seconds": round(time.monotonic() - started, 1)}), file=sys.stderr)
    return 0


def census(export_path: str) -> int:
    data = json.load(open(export_path, encoding="utf-8"))
    by_class: Counter = Counter()
    by_channel: Counter = Counter()
    pieces_with_fire = 0
    started = time.monotonic()
    for p in data["pieces"]:
        m = re.search(r"^Audience:\s*(.+)$", p.get("brand_text") or "", re.M)
        briefs = [f"Audience: {m.group(1)}"] if m else []
        angle = p.get("angle")
        if isinstance(angle, dict):
            briefs += [str(v) for k, v in angle.items() if k != "name" and isinstance(v, str)]
        found = _seed_align(p.get("text") or "", p.get("source_text") or "", briefs)
        by_class.update(c.cls for c in found)
        by_channel.update(p.get("channel") or "?" for _ in found)
        pieces_with_fire += bool(found)
    print(json.dumps({"pieces": len(data["pieces"]), "pieces_with_a_candidate": pieces_with_fire,
                      "by_class": dict(sorted(by_class.items())), "by_channel": dict(sorted(by_channel.items())),
                      "seconds": round(time.monotonic() - started, 1)}, indent=1))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "predict":
        sys.exit(predict(sys.argv[2], sys.argv[3], "--baseline" in sys.argv[4:]))
    if len(sys.argv) >= 3 and sys.argv[1] == "census":
        sys.exit(census(sys.argv[2]))
    print(__doc__)
    sys.exit(2)
