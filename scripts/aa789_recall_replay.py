#!/usr/bin/env python3
"""AA-789 — $0 offline replay of the deterministic T10 checks this issue tunes, on stored T9 pieces.

No database, no LLM, no network. Input: the decoded AA-783 export (`aa783_export.py --set s234`, the 40 pieces the
S234 panel scored, with the panel key). The export holds real tenant copy: keep it in the gitignored scratch folder,
never in the repo.

What it runs per piece, with the code on PYTHONPATH (so the same script replays `origin/main` and the branch):
  F15  quality_gates.gate_voice (stock, contrast density, template / stock lines; LinkedIn spec on LinkedIn)
  F17 / F18  trip_facts.find_trip_fact_issues
  F13  hygiene.find_issues: leak, repeat
  F16  quality_gates.faq_answer_problem on every FAQ pair (blogs)
It prints NO copy: each finding is reduced to its kind (the problem text before the first quote).

Usage:
  PYTHONPATH=<checkout> python3 scripts/aa789_recall_replay.py run EXPORT.json > out.json
  python3 scripts/aa789_recall_replay.py compare BASE.json NEW.json [--panel-clean-linkedin]

`compare` lists, per piece, the gates that fail on NEW but not on BASE and the finding kinds NEW adds;
`--panel-clean-linkedin` keeps the LinkedIn pieces whose panel majority is light or better (the issue's Done-when:
no new fire on them).
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter

_KIND_RE = re.compile(r"^([^'(:]+)")


def _kind(violation: str) -> str:
    m = _KIND_RE.match(violation or "")
    return " ".join((m.group(1) if m else violation or "").split()).rstrip(" —-")[:80]


def _scope(source: str) -> str | None:
    first = (source or "").splitlines()[0] if source else ""
    return first if first.startswith("SCOPE:") else None


def _audience(brand_text: str) -> str | None:
    m = re.search(r"^Audience:\s*(.+(?:\n(?![A-Z][\w ]+:).+)*)", brand_text or "", re.M)
    return m.group(1) if m else None


def _brand(piece: dict) -> str | None:
    m = re.match(r"Brand:\s*(.+?)\s*(?:\(|$)", piece.get("brand_text") or "")
    return m.group(1) if m else piece.get("tenant")


def run(export_path: str) -> dict:
    from services.acp_content_writing import hygiene as h
    from services.acp_content_writing import quality_gates as qg
    from services.acp_content_writing import trip_facts as tf
    from services.acp_shared.channel_spec import channel_spec

    data = json.load(open(export_path, encoding="utf-8"))
    key = data.get("key") or {}
    out = {}
    for p in data["pieces"]:
        text, seed, cta, channel = p.get("text") or "", p.get("source_text") or "", p.get("cta"), p.get("channel")
        brand = _brand(p)
        spec = channel_spec(channel, {"brand_name": brand})
        hctx = h.HygieneContext(seed=seed, scope=_scope(seed), brand_name=brand, cta=cta,
                                audience=_audience(p.get("brand_text") or ""))
        tctx = tf.TripFactContext(seed=seed, audience=hctx.audience, cta=cta, channel=channel, brand_name=brand)
        found: dict[str, list[str]] = {}
        voice = qg.gate_voice(text, spec=spec, exclude_texts=[t for t in (hctx.lexicon.own_text, cta) if t],
                              brand_name=brand, keyword=p.get("keyword"))
        found["F15_voice"] = voice["violations"]
        trip = tf.find_trip_fact_issues(text, tctx, seo_title=p.get("title"), meta_description=p.get("meta"))
        found["F17_trip_fact"], found["F18_rarity"] = trip["trip_fact"], trip["rarity"]
        issues = h.find_issues(text, hctx)
        found["F13_leak"], found["F13_repeat"] = issues["leak"], issues["repeat"]
        if channel == "blog":
            context = f"{text.partition('## FAQ')[0]}\n{seed}"
            found["F16_faq_answer"] = [f"{why}: 'pair {k}'" for k, (q, a) in enumerate(qg.faq_pairs(text))
                                       if (why := qg.faq_answer_problem(q, a, context))]
        panel = (key.get(p["piece_id"]) or {}).get("panel") or {}
        out[str(p["n"])] = {"channel": channel, "tenant": p.get("tenant"), "panel": panel.get("majority"),
                            "gates": {g: sorted(Counter(_kind(v) for v in vs).items()) for g, vs in found.items()
                                      if vs}}
    return out


def compare(base_path: str, new_path: str, clean_linkedin: bool) -> int:
    base, new = json.load(open(base_path)), json.load(open(new_path))
    added_pieces = 0
    for n, row in new.items():
        if clean_linkedin and not (row["channel"] == "linkedin" and row["panel"] in ("light", "as_is")):
            continue
        old = base.get(n, {}).get("gates", {})
        new_gates = sorted(set(row["gates"]) - set(old))
        kinds = {g: sorted({k for k, _ in v} - {k for k, _ in old.get(g, [])}) for g, v in row["gates"].items()}
        kinds = {g: k for g, k in kinds.items() if k}
        if new_gates or kinds:
            added_pieces += 1
        print(json.dumps({"n": n, "channel": row["channel"], "panel": row["panel"], "new_failing_gates": new_gates,
                          "new_kinds": kinds}))
    print(json.dumps({"pieces_with_new_findings": added_pieces}))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "run":
        print(json.dumps(run(sys.argv[2]), indent=1, sort_keys=True))
    elif len(sys.argv) >= 4 and sys.argv[1] == "compare":
        sys.exit(compare(sys.argv[2], sys.argv[3], "--panel-clean-linkedin" in sys.argv[4:]))
    else:
        print(__doc__)
        sys.exit(2)
