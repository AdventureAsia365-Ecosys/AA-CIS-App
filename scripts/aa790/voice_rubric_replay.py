#!/usr/bin/env python3
"""AA-790 (slice B) — replay the offline tenant-voice rubric over labelled T9 pieces. No LLM, no writes, $0.

Scores every piece with ``services.acp_content_writing.voice_rubric`` (profile compiled from the tenant's brand
pack, ``custom_validators.voice_rubric`` override when the row has one) and reports against the labels:

* a confusion table per tenant × channel: would_block vs the label (light-or-better = as_is / light);
* the Trail Pulse voice-miss set: the S234 label-pack Trail Pulse pieces (all approved by the gates; the panel's
  deciding defects name a voice miss on each) — how many the rubric would block (bar: ≥ 4 of 6);
* fires on pieces labelled light: Trail Pulse (bar: ≤ 1) and the other tenants (bar: 0).

One label per piece: the 4-lens panel majority of the label pack when the piece is in it, else the AA-729 fixture
label (pass → light, fail → heavy), else the stored ``content_piece_label`` (``--db``). Pieces are de-duplicated
by piece_id (the fixture and the label pack share 7).

  python -m scripts.aa790.voice_rubric_replay --golden <root>/docs/eval/aa729/fixture-1.1.0.json \\
      --pack <label-pack>/S234-label-40.xlsx --panel-key <label-pack>/agreement/panel_key.json [--out x.json]

``--out`` quotes the sentences behind each miss: keep it in the private root audit folder, not in this repo.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.getcwd())

from scripts.aa790 import corpus  # noqa: E402
from services.acp_content_writing import voice_rubric as vr  # noqa: E402

TRAIL = "Trail Pulse"
BARS = {"trail_caught_min": 4, "trail_light_fires_max": 1, "other_light_fires_max": 0}


def _dedupe(rows: list[dict]) -> list[dict]:
    """One row per piece; a label-pack row (panel majority) wins over a fixture row of the same piece."""
    rank = {"pack": 0, "db": 1, "golden": 2, "test": 3}
    out: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: rank.get(r["source"], 9)):
        key = r.get("piece_id") or r["id"]
        if key not in out:
            out[key] = r
        elif not out[key].get("label") and r.get("label"):
            out[key] = {**out[key], "label": r["label"]}
    return list(out.values())


def replay(rows: list[dict]) -> dict:
    rows = _dedupe(rows)
    cells: dict = defaultdict(lambda: {"block_light": 0, "block_heavy": 0, "pass_light": 0, "pass_heavy": 0,
                                       "unlabelled": 0, "profile": None})
    pieces = []
    for r in rows:
        rubric = vr.compile_profile(r.get("brand_pack"), vr.override_from_validators(r.get("custom_validators")))
        res = vr.score(r["text"], rubric, channel=r.get("channel"), cta=r.get("cta"))
        light = r.get("label") in corpus.LIGHT
        cell = cells[f"{r.get('tenant')} / {r.get('channel')}"]
        cell["profile"] = rubric.profile
        if not r.get("label"):
            cell["unlabelled"] += 1
        else:
            cell[("block_" if res["would_block"] else "pass_") + ("light" if light else "heavy")] += 1
        pieces.append({"id": r["id"], "piece_id": r.get("piece_id"), "tenant": r.get("tenant"),
                       "channel": r.get("channel"), "status": r.get("status"), "label": r.get("label"),
                       "source": r["source"], "profile": rubric.profile, "would_block": res["would_block"],
                       "violations": res["violations"],
                       "features": {k: (len(v) if isinstance(v, list) else v) for k, v in res["features"].items()}})
    trail_set = [p for p in pieces if p["tenant"] == TRAIL and p["source"] == "pack" and p["status"] == "approved"]
    trail_light = [p for p in pieces if p["tenant"] == TRAIL and p["label"] in corpus.LIGHT]
    other_light = [p for p in pieces if p["tenant"] != TRAIL and p["label"] in corpus.LIGHT]
    summary = {
        "pieces": len(pieces),
        "trail_voice_miss_set": len(trail_set),
        "trail_caught": sum(p["would_block"] for p in trail_set),
        "trail_light": len(trail_light),
        "trail_light_fires": sum(p["would_block"] for p in trail_light),
        "other_light": len(other_light),
        "other_light_fires": sum(p["would_block"] for p in other_light),
        "any_miss_other_light": sum(bool(p["violations"]) for p in other_light),
    }
    summary["bars"] = {
        "trail_caught": summary["trail_caught"] >= BARS["trail_caught_min"],
        "trail_light_fires": summary["trail_light_fires"] <= BARS["trail_light_fires_max"],
        "other_light_fires": summary["other_light_fires"] <= BARS["other_light_fires_max"],
    }
    return {"rubric": vr.RUBRIC_VERSION, "summary": summary, "cells": dict(cells), "pieces": pieces}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    corpus.add_args(ap)
    ap.add_argument("--out", help="write the full JSON here (quotes piece sentences: private root folder only)")
    args = ap.parse_args()
    rows = asyncio.run(corpus.load(args))
    if not rows:
        ap.error("no input: pass --golden, --pack or --db")
    res = replay(rows)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1, ensure_ascii=False)
    s = res["summary"]
    print(f"{res['rubric']}: {s['pieces']} pieces")
    print(f"Trail Pulse voice-miss set: {s['trail_caught']}/{s['trail_voice_miss_set']} would block "
          f"(bar ≥ {BARS['trail_caught_min']}: {'met' if s['bars']['trail_caught'] else 'NOT met'})")
    print(f"Trail Pulse labelled light: {s['trail_light_fires']}/{s['trail_light']} would block "
          f"(bar ≤ {BARS['trail_light_fires_max']}: {'met' if s['bars']['trail_light_fires'] else 'NOT met'})")
    print(f"Other tenants labelled light: {s['other_light_fires']}/{s['other_light']} would block "
          f"(bar 0: {'met' if s['bars']['other_light_fires'] else 'NOT met'}); "
          f"{s['any_miss_other_light']} with a single (style-note) miss")
    print("\n| tenant / channel | profile | block+light | block+heavy | pass+light | pass+heavy | unlabelled |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for k, c in sorted(res["cells"].items()):
        print(f"| {k} | {c['profile']} | {c['block_light']} | {c['block_heavy']} | {c['pass_light']} | "
              f"{c['pass_heavy']} | {c['unlabelled']} |")
    print()
    for p in sorted(res["pieces"], key=lambda p: (p["tenant"] or "", p["channel"] or "", p["id"])):
        if p["violations"]:
            print(f"- {p['tenant']} {p['channel']} {p['id']} label={p['label']} would_block={p['would_block']}: "
                  + "; ".join(v[:90] for v in p["violations"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
