#!/usr/bin/env python3
"""AA-1079 — the brand-label step on an AA-729 golden fixture (format: services/quality/brand_labels.py). Local, $0
(the panel is Claude Code agents reading the pack, not a gateway call), no DB.

  python3 -m scripts.aa729.brand_labels export-pack fixture-x.y.z.json --out brand-pack.json [--relabel]
  python3 -m scripts.aa729.brand_labels apply fixture-x.y.z.json brand-panel.json --approved-by <who> \\
      --out fixture-x.(y+1).0.json [--include-disputed] [--source tech_lead]

The pack holds, per T2 / T9 golden item without a brand label, the brand pack and the text, never the pipeline's own
verdict, scores, gate ledger or flags. The panel returns {"<sample_id>": {"brand_fit": "fits|partly|off",
"core_idea": "present|absent|unclear", "reason": "...", "status": "agreed|disputed", "votes": [...]}}. `apply` writes
`label.brand` with who approved it, bumps the fixture's minor version and keeps a `label_history` entry; the content
label is untouched. Import the new fixture with `python -m services.golden_set.importer` (DRY RUN, then --apply), then
measure with `python -m scripts.quality.measure_aa1079_brand_fit`.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from scripts.aa729.common import bump_minor, dumps, find_pii
from services.quality import brand_labels


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export-pack")
    e.add_argument("fixture")
    e.add_argument("--out", required=True)
    e.add_argument("--relabel", action="store_true", help="every brand item, labelled or not")
    p = sub.add_parser("apply")
    p.add_argument("fixture")
    p.add_argument("panel")
    p.add_argument("--approved-by", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--include-disputed", action="store_true")
    p.add_argument("--source", default="agent_panel", choices=brand_labels.BRAND_SOURCES)
    a = ap.parse_args(argv)
    with open(a.fixture, encoding="utf-8") as f:
        fixture = json.load(f)
    if a.cmd == "export-pack":
        pack = brand_labels.export_brand_pack(fixture, relabel=a.relabel)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(dumps(pack))
        print(json.dumps({"fixture_version": pack["fixture_version"], "items": pack["count"]}))
        return 0
    with open(a.panel, encoding="utf-8") as f:
        panel = json.load(f)
    new, info = brand_labels.apply_brand_labels(fixture, panel, approved_by=a.approved_by,
                                                include_disputed=a.include_disputed, source=a.source,
                                                bump=bump_minor)
    leaks = find_pii(new)
    if leaks:
        print(f"PII check failed: {leaks[:10]}")
        return 2
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(dumps(new))
    print(json.dumps(info, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
