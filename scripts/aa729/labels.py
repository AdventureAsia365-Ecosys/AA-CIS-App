#!/usr/bin/env python3
"""AA-729 — the panel-label step for samples no existing decision labels (`label_source = needs_panel`).
Local, $0 (the panel is Claude Code agents reading the pack, not a gateway call), no DB.

  python3 -m scripts.aa729.labels export-pack fixture-1.0.0.json --out pack.json [--md pack.md]
  python3 -m scripts.aa729.labels apply fixture-1.0.0.json panel.json --approved-by nghiep --out fixture-1.1.0.json

The pack holds, per needs_panel sample, the frozen output, the FULL writer source and the rubric — never the
pipeline's own verdict (passed / held / score / failure codes), T9's gate_ledger / flags, nor the selection
tags derived from them (held, known_failure): the panel label is
what the judges are measured against, so it must not be anchored on them (PANEL_HIDDEN_KEYS). The panel
returns {"<sample_id>": {"verdict": "pass|fail", "grade": "as_is|light|heavy|reject", "reason": "...",
"votes": [...], "status": "agreed|disputed"}}. `apply` writes those labels with
`label_source = "agent_panel"` and who approved them, bumps the fixture's minor version and keeps a
`label_history`. Disputed samples stay needs_panel unless `--include-disputed` (after Nghiệp decided them).
A human / audit / synthetic label is never overwritten.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from typing import Optional

from scripts.aa729.common import bump_minor, dumps, find_pii, flatten_text, make_label

RUBRIC = {
    "pass": "Publishable as is or after a light edit (minutes, no fact or structure change).",
    "fail": "Needs a heavy edit or a rewrite: an invented or wrong fact, a promise the source does not make, "
            "off-brand voice, broken structure, wrong scope, or anything a customer must not read.",
    "grades": {"as_is": "pass", "light": "pass", "heavy": "fail", "reject": "fail"},
    "post_edit": "When output_post_edit is true the text was already edited by a person: label the text you see.",
}
# The panel reads the whole source: a 2,500-char excerpt (1.0.0) hid the later days of 76/82 sources, and judges
# failed claims that were in the full text (26/82 samples, S245). Panel agents read files, so size is not a limit.
# Output keys that carry the pipeline's own judgement (T9 gate results) — left out of the panel pack.
PANEL_HIDDEN_KEYS = frozenset({"gate_ledger", "flags"})
# Selection tags that restate the pipeline verdict or an existing label — left out too.
PANEL_HIDDEN_TAGS = frozenset({"held", "known_failure"})


def panel_output(output):
    """The frozen output as the panel sees it: the text only, without the pipeline's gate results."""
    if isinstance(output, dict):
        return {k: v for k, v in output.items() if k not in PANEL_HIDDEN_KEYS}
    return output


def _source_text(sample: dict) -> str:
    inp = sample.get("input") or {}
    src = inp.get("tour") or {}
    if not src and inp.get("context"):
        ctx = inp["context"]
        src = {"seed": ctx.get("atom_text"), "facts": ctx.get("facts_text"), "cta": ctx.get("cta"),
               "brand": (ctx.get("brand_audience") or {}).get("brand_name")}
    return flatten_text(src)


def export_pack(fixture: dict) -> dict:
    items = []
    for s in fixture["samples"]:
        if s.get("label_source") != "needs_panel":
            continue
        items.append({"sample_id": s["sample_id"], "stratum": s["stratum"], "channel": s.get("channel"),
                      "country": s.get("country"), "group": s.get("group"),
                      "tags": [t for t in s.get("tags") or [] if t not in PANEL_HIDDEN_TAGS],
                      "output_post_edit": s.get("output_post_edit", False),
                      "source": _source_text(s), "output": panel_output(s.get("output"))})
    return {"fixture_version": fixture["fixture_version"], "rubric": RUBRIC, "count": len(items), "items": items}


def pack_markdown(pack: dict) -> str:
    lines = [f"# AA-729 panel pack — fixture {pack['fixture_version']} ({pack['count']} samples)", "",
             f"- pass: {RUBRIC['pass']}", f"- fail: {RUBRIC['fail']}", f"- {RUBRIC['post_edit']}", ""]
    for it in pack["items"]:
        lines += [f"## {it['sample_id']} · {it['stratum']} · {it.get('group')} · {it.get('country')}", "",
                  f"Tags: {', '.join(it['tags']) or '–'}{' · POST-EDIT' if it['output_post_edit'] else ''}", "",
                  "Source (full):", "", "```", it["source"], "```", "", "Output:", "", "```",
                  flatten_text(panel_output(it["output"])), "```", ""]
    return "\n".join(lines)


def apply_labels(fixture: dict, panel: dict, *, approved_by: str, include_disputed: bool = False) -> tuple[dict, dict]:
    """A new fixture (minor version bump) with the panel's verdicts on needs_panel samples."""
    if not approved_by.strip():
        raise ValueError("approved_by is required (Nghiệp approves panel labels)")
    out = copy.deepcopy(fixture)
    applied, skipped = [], []
    by_id = {s["sample_id"]: s for s in out["samples"]}
    for sid, lab in panel.items():
        s = by_id.get(sid)
        if s is None:
            skipped.append((sid, "unknown sample"))
            continue
        if s.get("label_source") != "needs_panel":
            skipped.append((sid, f"already labelled by {s.get('label_source')}"))
            continue
        if lab.get("status") == "disputed" and not include_disputed:
            skipped.append((sid, "disputed"))
            continue
        label = make_label(lab.get("verdict"), "agent_panel", grade=lab.get("grade"), reason=lab.get("reason", ""),
                           evidence=[{"votes": lab.get("votes") or [], "status": lab.get("status") or "agreed",
                                      "approved_by": approved_by}])
        if label["verdict"] is None:
            skipped.append((sid, "no verdict"))
            continue
        s["label"], s["label_source"] = label, "agent_panel"
        applied.append(sid)
    old = out["fixture_version"]
    out["fixture_version"] = bump_minor(old)
    out.setdefault("label_history", []).append({
        "from_version": old, "to_version": out["fixture_version"], "source": "agent_panel",
        "approved_by": approved_by, "applied": len(applied), "at": datetime.now(timezone.utc).isoformat()})
    return out, {"applied": applied, "skipped": skipped, "fixture_version": out["fixture_version"],
                 "needs_panel_left": sum(1 for s in out["samples"] if s.get("label_source") == "needs_panel")}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export-pack")
    e.add_argument("fixture")
    e.add_argument("--out", required=True)
    e.add_argument("--md")
    p = sub.add_parser("apply")
    p.add_argument("fixture")
    p.add_argument("panel")
    p.add_argument("--approved-by", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--include-disputed", action="store_true")
    a = ap.parse_args(argv)
    with open(a.fixture, encoding="utf-8") as f:
        fixture = json.load(f)
    if a.cmd == "export-pack":
        pack = export_pack(fixture)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(dumps(pack))
        if a.md:
            with open(a.md, "w", encoding="utf-8") as f:
                f.write(pack_markdown(pack))
        print(f"{pack['count']} needs_panel samples -> {a.out}")
        return 0
    with open(a.panel, encoding="utf-8") as f:
        panel = json.load(f)
    new, info = apply_labels(fixture, panel, approved_by=a.approved_by, include_disputed=a.include_disputed)
    leaks = find_pii(new)
    if leaks:
        print(f"PII check failed: {leaks[:10]}")
        return 2
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(dumps(new))
    print(json.dumps(info, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
