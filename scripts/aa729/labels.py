#!/usr/bin/env python3
"""AA-729 — the panel-label step for samples no existing decision labels (`label_source = needs_panel`).
Local, $0 (the panel is Claude Code agents reading the pack, not a gateway call), no DB.

  python3 -m scripts.aa729.labels export-pack fixture-1.0.0.json --out pack.json [--md pack.md]
  python3 -m scripts.aa729.labels apply fixture-1.0.0.json panel.json --approved-by nghiep --out fixture-1.1.0.json
  python3 -m scripts.aa729.labels relabel fixture-1.1.0.json review.json --approved-by <who> --review-ref <doc> \
      --out fixture-1.2.0.json      # a calibration review of panel labels (S256, AA-919)
  python3 -m scripts.aa729.labels merge fixture-1.2.0.json addendum-labelled.json --approved-by <who> \
      --out fixture-1.3.0.json      # add a labelled addendum (select_samples --ids) as the next version (S258)

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
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

from scripts.aa729.common import bump_minor, dumps, find_pii, flatten_text, make_label

RUBRIC = {
    "pass": "Publishable as is or after a light edit (minutes, no fact or structure change).",
    "fail": "Needs a heavy edit or a rewrite: an invented or wrong fact, a promise the source does not make, "
            "off-brand voice, broken structure, wrong scope, or anything a customer must not read.",
    "grades": {"as_is": "pass", "light": "pass", "heavy": "fail", "reject": "fail"},
    "post_edit": "When output_post_edit is true the text was already edited by a person: label the text you see.",
    # S256 calibration review (AA-919): "minutes" and "no fact change" pulled panel votes apart on one-phrase claims
    # (the same kind of flaw was labelled heavy on some texts and light on others). These rules settle it; the judge
    # reads the same list (eval_judge.LABEL_RUBRIC, kept equal by a test).
    "rules": (
        "Every customer-facing field counts: body, title, SEO title, meta description and hashtags.",
        "A claim the source does not support is a fact change, so the text fails even when one phrase would fix "
        "it: a wrong or invented fact, a fact the source contradicts, or a promise about the trip (an activity, "
        "access, time, view or service) that the itinerary does not make.",
        "A light edit changes wording only: typos, repetition, stray blank lines, awkward or garbled phrasing that "
        "still says something true, an imprecise but not false paraphrase, or a point of the brief left out.",
        "Reflection that states no fact (what a place means, why its setting matters) is voice, not a fact claim.",
        "The writer's brief and brand pack are part of the source: what they ask for is supported unless the facts "
        "or the itinerary contradict it. Naming another part of the trip that the itinerary does include, because "
        "the brief asked for it, is a light fix; promising one the itinerary does not include is a fail.",
    ),
}
# Label sources a calibration review may relabel: the panel's own labels. Labels made from a person's edit or by hand
# (admin_edit, tenant_edit, human_label) and synthetic traps are evidence of another kind and are never relabelled.
RELABEL_SOURCES = frozenset({"agent_panel"})
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
    """S1 / T2: the whole source tour. T9: every section of the stored writer input in the R2 judge's layout, never cut
    (`eval_judge.panel_source_text`, S258): the scope, angle brief and brand pack the rubric rules count as source are
    in it, so a panel label and the judge read the same fields. Packs exported before S258 showed only the seed,
    facts, CTA and brand name for T9."""
    inp = sample.get("input") or {}
    src = inp.get("tour") or {}
    if not src and inp.get("context"):
        from services.writing_memory.eval_judge import panel_source_text

        channel = sample.get("channel") or inp["context"].get("channel")
        return panel_source_text("t9_blog" if channel == "blog" else "t9_social", inp)
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
             f"- pass: {RUBRIC['pass']}", f"- fail: {RUBRIC['fail']}", f"- {RUBRIC['post_edit']}",
             *[f"- {r}" for r in RUBRIC["rules"]], ""]
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


def relabel(fixture: dict, review: dict, *, approved_by: str, review_ref: str) -> tuple[dict, dict]:
    """A new fixture (minor version bump) with the verdicts of a calibration review applied to panel labels.

    review = {"<sample_id>": {"from": "pass|fail", "verdict": "pass|fail", "grade": "...", "reason": "..."}}.
    `from` must equal the current verdict (a stale review is refused, nothing half-applied). Only RELABEL_SOURCES
    labels change; the old label is kept in the new label's evidence (`previous`) and the change in label_history.
    Labels are immutable per version: the result is a new version to import, never an edit of the imported one."""
    if not approved_by.strip():
        raise ValueError("approved_by is required (the business decision owner approves label changes)")
    if not review_ref.strip():
        raise ValueError("review_ref is required (where the per-sample review is written down)")
    out = copy.deepcopy(fixture)
    by_id = {s["sample_id"]: s for s in out["samples"]}
    problems = []
    for sid, rv in review.items():
        s = by_id.get(sid)
        if s is None:
            problems.append(f"{sid}: unknown sample")
        elif s.get("label_source") not in RELABEL_SOURCES:
            problems.append(f"{sid}: label source {s.get('label_source')!r} is not relabelled")
        elif (s.get("label") or {}).get("verdict") != rv.get("from"):
            problems.append(f"{sid}: current verdict {(s.get('label') or {}).get('verdict')!r}, review says "
                            f"{rv.get('from')!r}")
        elif rv.get("verdict") not in ("pass", "fail") or RUBRIC["grades"].get(rv.get("grade")) != rv.get("verdict"):
            problems.append(f"{sid}: verdict {rv.get('verdict')!r} and grade {rv.get('grade')!r} disagree")
        elif not str(rv.get("reason") or "").strip():
            problems.append(f"{sid}: no reason")
    if problems:
        raise ValueError("; ".join(problems))
    changed = []
    for sid, rv in review.items():
        s = by_id[sid]
        old = s["label"]
        if old.get("verdict") == rv["verdict"] and old.get("grade") == rv["grade"]:
            continue
        previous = {k: old.get(k) for k in ("verdict", "grade", "reason")}
        evidence = list(old.get("evidence") or []) + [{"review": review_ref, "approved_by": approved_by,
                                                        "previous": previous}]
        s["label"] = make_label(rv["verdict"], s["label_source"], grade=rv["grade"], reason=rv["reason"],
                                evidence=evidence)
        changed.append(sid)
    old_version = out["fixture_version"]
    out["fixture_version"] = bump_minor(old_version)
    out.setdefault("label_history", []).append({
        "from_version": old_version, "to_version": out["fixture_version"], "source": "calibration_review",
        "review": review_ref, "approved_by": approved_by, "applied": len(changed), "relabelled": changed,
        "rubric_rules": len(RUBRIC["rules"]), "at": datetime.now(timezone.utc).isoformat()})
    return out, {"relabelled": changed, "reviewed": len(review), "fixture_version": out["fixture_version"]}


# Label sources an addendum sample may carry into a merged fixture: what the WM-17 importer accepts
# (services/golden_set/derive.LABEL_SOURCES, minus synthetic: an addendum holds real outputs only).
MERGE_SOURCES = frozenset({"agent_panel", "admin_edit", "tenant_edit", "human_label"})
# refs keys that identify the output an item freezes; the same output twice would count one text twice in kappa.
_OUTPUT_REFS = ("piece_id", "version_id")


def _ref_keys(sample: dict) -> set[str]:
    refs = sample.get("refs") or {}
    keys = {f"{k}:{refs[k]}" for k in _OUTPUT_REFS if refs.get(k)}
    if sample.get("kind") == "s1" and refs.get("tour_id"):
        keys.add(f"tour_id:{refs['tour_id']}")   # a1 freezes one Master text per tour
    return keys


def _prefix(sample_id: str) -> str:
    return sample_id.rsplit("-", 1)[0]


def _pack_digest(pack) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(pack, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def merge(base: dict, addendum: dict, *, approved_by: str, drop_unlabelled: bool = False) -> tuple[dict, dict]:
    """The next fixture version (minor bump of `base`): base + the labelled samples of an addendum made by
    `select_samples --ids` against that base version. Nothing is half-applied: a problem refuses the merge.

    - the addendum must name this base (`addendum_of`) and share its schema;
    - every addendum sample needs an importable label (MERGE_SOURCES, verdict pass / fail); with `drop_unlabelled`
      a needs_panel / disputed one is left out and reported instead of refusing;
    - an output the base already holds (piece / version id; an S1 tour) is refused;
    - sample ids are renumbered after the base's highest number per prefix (`t9f-001` stays `t9f-001` when the base
      has no Facebook item, `t9l-001` becomes `t9l-016` after `t9l-015`);
    - brand packs: a new key is added; an equal one is shared; a CHANGED S1 pack is stored under
      `<key>@<sha8>` and the new samples point at it (base items keep the pack they were frozen with); a changed
      tenant (t2:) pack is inlined on the new samples themselves (`input.brand_rules`, no ref), because the importer
      inlines a `t2:<slug>` ref on every item of that tenant.
    Labels are immutable per version: the result is a new version to import, never an edit of the imported one."""
    if not approved_by.strip():
        raise ValueError("approved_by is required (the business decision owner approves new labels)")
    problems = []
    if addendum.get("schema") != base.get("schema"):
        problems.append(f"schema {addendum.get('schema')!r} is not the base's {base.get('schema')!r}")
    if addendum.get("addendum_of") != base.get("fixture_version"):
        problems.append(f"the addendum extends {addendum.get('addendum_of')!r}, the base is "
                        f"{base.get('fixture_version')!r}")
    taken = {k for s in base["samples"] for k in _ref_keys(s)}
    keep, dropped = [], []
    for s in addendum.get("samples") or []:
        sid, lab = s.get("sample_id"), s.get("label") or {}
        if s.get("stratum") == "injection" or str(s.get("kind", "")).startswith("injection"):
            problems.append(f"{sid}: an addendum holds real outputs only")
        elif s.get("label_source") not in MERGE_SOURCES or lab.get("verdict") not in ("pass", "fail"):
            if drop_unlabelled:
                dropped.append(sid)
                continue
            problems.append(f"{sid}: label source {s.get('label_source')!r} / verdict {lab.get('verdict')!r} "
                            f"is not importable")
        elif _ref_keys(s) & taken:
            problems.append(f"{sid}: the base already holds {sorted(_ref_keys(s) & taken)}")
        else:
            taken |= _ref_keys(s)
            keep.append(s)
    if problems:
        raise ValueError("; ".join(problems))

    out = copy.deepcopy(base)
    packs = out.setdefault("shared_inputs", {}).setdefault("brand_rules", {})
    add_packs = (addendum.get("shared_inputs") or {}).get("brand_rules") or {}
    renamed, inlined = {}, []
    for key, pack in add_packs.items():
        if key not in packs:
            packs[key] = copy.deepcopy(pack)
        elif _pack_digest(packs[key]) != _pack_digest(pack) and key.startswith("s1:"):
            renamed[key] = f"{key}@{_pack_digest(pack)[:8]}"
            packs[renamed[key]] = copy.deepcopy(pack)
        elif _pack_digest(packs[key]) != _pack_digest(pack):
            renamed[key] = None
    top: dict[str, int] = {}
    for s in out["samples"]:
        head, _, num = s["sample_id"].rpartition("-")
        if num.isdigit():
            top[head] = max(top.get(head, 0), int(num))
    added = []
    for s in keep:
        s = copy.deepcopy(s)
        p = _prefix(s["sample_id"])
        top[p] = top.get(p, 0) + 1
        s["sample_id"] = f"{p}-{top[p]:03d}"
        inp = s.get("input") or {}
        ref = inp.get("brand_rules_ref")
        if ref in renamed and renamed[ref]:
            inp["brand_rules_ref"] = renamed[ref]
        elif ref in renamed:
            inp.pop("brand_rules_ref")
            inp["brand_rules"] = copy.deepcopy(add_packs[ref])
            inlined.append(s["sample_id"])
        out["samples"].append(s)
        added.append(s["sample_id"])
    old_version = out["fixture_version"]
    out["fixture_version"] = bump_minor(old_version)
    out.setdefault("selection", {}).setdefault("addenda", []).append({
        "addendum_created_at": addendum.get("created_at"), "selection": addendum.get("selection"),
        "added": added, "dropped_unlabelled": dropped})
    out.setdefault("label_history", []).append({
        "from_version": old_version, "to_version": out["fixture_version"], "source": "addendum",
        "approved_by": approved_by, "applied": len(added), "added": added,
        "addendum_label_history": addendum.get("label_history") or [],
        "packs_renamed": {k: v for k, v in renamed.items() if v}, "packs_inlined_on": inlined,
        "at": datetime.now(timezone.utc).isoformat()})
    return out, {"added": added, "dropped_unlabelled": dropped, "fixture_version": out["fixture_version"],
                 "by_stratum_label": dict(sorted(Counter(
                     f"{s['stratum']}:{(s.get('label') or {}).get('verdict')}"
                     for s in out["samples"] if s["sample_id"] in set(added)).items()))}


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
    r = sub.add_parser("relabel")
    r.add_argument("fixture")
    r.add_argument("review")
    r.add_argument("--approved-by", required=True)
    r.add_argument("--review-ref", required=True)
    r.add_argument("--out", required=True)
    m = sub.add_parser("merge")
    m.add_argument("fixture")
    m.add_argument("addendum")
    m.add_argument("--approved-by", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--drop-unlabelled", action="store_true")
    a = ap.parse_args(argv)
    with open(a.fixture, encoding="utf-8") as f:
        fixture = json.load(f)
    if a.cmd == "merge":
        with open(a.addendum, encoding="utf-8") as f:
            addendum = json.load(f)
        new, info = merge(fixture, addendum, approved_by=a.approved_by, drop_unlabelled=a.drop_unlabelled)
        leaks = find_pii(new)
        if leaks:
            print(f"PII check failed: {leaks[:10]}")
            return 2
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(dumps(new))
        print(json.dumps(info, indent=1))
        return 0
    if a.cmd == "relabel":
        with open(a.review, encoding="utf-8") as f:
            review = json.load(f)
        new, info = relabel(fixture, review, approved_by=a.approved_by, review_ref=a.review_ref)
        leaks = find_pii(new)
        if leaks:
            print(f"PII check failed: {leaks[:10]}")
            return 2
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(dumps(new))
        print(json.dumps(info, indent=1))
        return 0
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
