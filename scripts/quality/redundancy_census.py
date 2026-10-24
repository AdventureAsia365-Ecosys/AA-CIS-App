"""AA-926 S4 — redundant-detector census from the quality registry (read only, $0, no DB, no LLM).

For every detector a class lists in mode enforce, it asks: is each class this detector maps to already
enforced, on the same writer, by ANOTHER detector? A detector whose every (class, writer) pair is covered by
another deterministic (rule / oracle) enforcing detector is a removal candidate; one covered only by an LLM or
Jev detector is listed separately (removing it would trade a code check for a model call). Shadow and warn
detectors (the day oracle and S1-first code checks ship in shadow) never count as cover.

The overlap is per CLASS, so it is an upper bound: two detectors of one class can catch different defects
(F10 similarity vs F11 brand name, both safety.tenant_isolation), and two candidates that cover only each other
cannot both go. Read every candidate against what its detector actually matches before proposing a removal.

The output is input for AA-924 (gate removals); this script removes nothing.

Usage (from the repo root):  python -m scripts.quality.redundancy_census [--json]
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict

from services.quality import registry

_DETERMINISTIC = ("rule", "oracle")


def census() -> dict:
    # (class, writer) -> [(ref, kind)] of enforcing detectors
    enforce: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for cid, c in registry.classes().items():
        if c.get("kind") == "meta":
            continue
        for d in c.get("detectors") or []:
            if d.get("mode") == "enforce":
                for w in d.get("writers") or []:
                    enforce[(cid, w)].append((d["ref"], d["kind"]))
    by_ref: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for (cid, w), dets in enforce.items():
        for ref, _ in dets:
            by_ref[ref].append((cid, w))
    rows = []
    for ref, pairs in sorted(by_ref.items()):
        det_cover, any_cover, uncovered = [], [], []
        covering: set[str] = set()
        for cid, w in sorted(set(pairs)):
            others = [(r, k) for r, k in enforce[(cid, w)] if r != ref]
            covering |= {r for r, k in others if k in _DETERMINISTIC}
            if any(k in _DETERMINISTIC for _, k in others):
                det_cover.append((cid, w))
            elif others:
                any_cover.append((cid, w))
            else:
                uncovered.append((cid, w))
        verdict = ("candidate" if not uncovered and not any_cover
                   else "model_only_cover" if not uncovered else "sole_owner")
        rows.append({"detector": ref, "verdict": verdict,
                     "kind": "t10_gate" if registry.gate(ref) else
                     "a1_code" if ref in registry.a1_codes() else
                     "jev" if ref in registry.jev_questions() else "code_check",
                     "covering_code_detectors": sorted(covering), "covered_by_code": det_cover,
                     "covered_by_model_only": any_cover, "sole": uncovered})
    return {"taxonomy": registry.TAXONOMY_VERSION, "rows": rows}


def render(data: dict) -> str:
    out = [f"Redundancy census ({data['taxonomy']}): enforcing detectors whose classes another detector enforces",
           ""]
    for verdict, title in (("candidate", "Removal candidates (every class/writer has another code check)"),
                           ("model_only_cover", "Covered only by an LLM / Jev detector"),
                           ("sole_owner", "Sole owner of at least one class/writer")):
        rows = [r for r in data["rows"] if r["verdict"] == verdict]
        out.append(f"## {title}: {len(rows)}")
        for r in rows:
            sole = ", ".join(f"{c}@{w}" for c, w in r["sole"])
            by = ", ".join(r["covering_code_detectors"])
            out.append(f"- {r['detector']} ({r['kind']})" + (f" — sole for {sole}" if sole else "")
                       + (f" — class cover from {by}" if by and verdict == "candidate" else ""))
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    data = census()
    print(json.dumps(data, indent=1) if args.json else render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
