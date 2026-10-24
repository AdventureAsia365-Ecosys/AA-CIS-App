"""AA-924 PR1c — offline replay of the T10 gate fold on stored T9 pieces ($0: no LLM, no DB, no network).

Runs `run_quality_gates` on stored final texts with the gate inputs rebuilt the way the writer builds them
(`scripts/aa729/score.py::t9_gate_replay`), once on a BASE tree (AA-926 merged, before AA-924) and once on HEAD,
and diffs the routing decisions. The F9 judge is never called: `gate_brand_voice` returns the F9 entries stored in
each piece's own ledger (the real verdicts), or a pass when none is stored. The cross-tenant similarity lookup is
not run: a stored failing F10 entry is turned back into its match. Every piece is replayed as attempt 1
(`stock_blocking=True`: F15 stock / frames block) and as a later attempt (`stock_blocking=False`: they warn), each on
the stored text and on the stored text after `hygiene.fix_dashes` (the `_dashfix` modes; review round). Most stored
texts predate AA-778 and hold on F2 em-dashes, which makes every later routing step trivially equal; the writer now
fixes dashes before the gates, so the dash-fixed modes are the ones that reach F15, the waive and the style-only paths.

Inputs
  --golden PATH   root docs/eval/aa729/fixture-1.1.0.json: the 30 T9 samples (15 t9_blog + 15 t9_linkedin, frozen
                  writer context, stored final text and ledger).
  --s234 PATH     the S234 r1–r4 pieces exported read-only from acp_shared.content_piece (root
                  docs/audits/2026-10-24-S250-aa924-replay/export_s234_pieces.py; plain or gzipped JSON). A piece
                  without `writer_input` is counted and skipped.

Per piece and mode it records: passed, status (approved / held, as service._hold_or_ship decides), the held gate
(mapped to the stored entry name), first_failure and hold_failure (legacy source names), the sentence-repair route
(repair / rewrite / none), what each sentence repair is told (`plan_problems`: gate, sentence and problem per item),
the style-only stage, the blocking failures of the legacy view, the flags (stored names and legacy view) and the
number of results in the ledger.

Usage (from a CIS checkout; the BASE tree is a worktree at the AA-926-merged main):
    python -m scripts.quality.replay_aa924 all --base-tree /path/to/base-worktree \\
        --golden ../../docs/eval/aa729/fixture-1.1.0.json --s234 /path/aa924_s234.json.gz \\
        --out-dir /tmp/aa924 --report ../../docs/audits/2026-10-24-S250-aa924-replay/README.md
    python -m scripts.quality.replay_aa924 run --golden … --out head.json        # one tree only (the cwd's)
    python -m scripts.quality.replay_aa924 diff base.json head.json --report out.md
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import gzip
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

SCHEMA = "aa924-replay/1"
# mode → (stock_blocking, run hygiene.fix_dashes on the stored text first)
MODES = {"attempt1": (True, False), "later": (False, False), "attempt1_dashfix": (True, True),
         "later_dashfix": (False, True)}
_F10_RE = re.compile(r"content is ([0-9.]+) cosine-similar .*?piece_id=([0-9a-fA-F-]+)", re.DOTALL)


def _rename() -> dict[str, str]:
    """F2_banned_patterns / F2_brand_forbidden → F2_lexicon, F15_voice_warn → F15_voice (read on the head tree)."""
    from services.quality import registry as qr
    return {k: v for k, v in qr.SOURCE_HOME.items() if k != v}


def _read_json(path: str) -> Any:
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


# ------------------------------------------------------------------------------------------- inputs

def load_golden(path: str) -> list[dict]:
    data = _read_json(path)
    out = []
    for s in data.get("samples") or []:
        if s.get("kind") != "t9":
            continue
        out.append({"id": s["sample_id"], "source": "golden", "stratum": s.get("stratum"),
                    "channel": s.get("channel") or s["input"]["context"].get("channel"),
                    "context": s["input"]["context"], "output": s["output"],
                    "stored_status": (s.get("pipeline_verdict") or {}).get("status")})
    return out


def context_from_writer_input(wi: dict, channel: Optional[str]) -> dict:
    """The writer context (run_write_background's dict) rebuilt from a stored `writer_input` snapshot
    (service.writer_input_snapshot): the keys t9_gate_replay reads."""
    angle = wi.get("angle") or {}
    ctx = {
        "atom_text": wi.get("seed") or "",
        "route_segments": [(m.get("atom_id"), m.get("seed")) for m in wi.get("route_moments") or []
                           if isinstance(m, dict)],
        "facts_text": wi.get("facts") or "",
        "scope": wi.get("scope"),
        "brand_audience": wi.get("brand_pack") or {},
        "brand_rubric_text": wi.get("brand_rubric") or "",
        "chosen": angle,
        "goal": {"key": wi.get("goal")},
        "channel": wi.get("channel") or channel,
        "keyword": wi.get("keyword"),
        "cta": wi.get("cta"),
        "trip_name": wi.get("trip"),
        "destination": wi.get("destination"),
        "trip_places": wi.get("trip_places") or [],
    }
    return ctx


def load_s234(path: str) -> tuple[list[dict], list[str]]:
    data = _read_json(path)
    out, skipped = [], []
    for p in data.get("pieces") or []:
        wi = p.get("writer_input")
        if isinstance(wi, str):
            wi = json.loads(wi) if wi else None
        if not wi:
            skipped.append(p.get("piece_id"))
            continue
        ledger, flags = p.get("gate_ledger"), p.get("flags")
        out.append({"id": p["piece_id"], "source": "s234", "stratum": f"s234_{p.get('channel')}",
                    "channel": p.get("channel"), "context": context_from_writer_input(wi, p.get("channel")),
                    "output": {"content_text": p.get("content_text") or "", "seo_title": p.get("seo_title"),
                               "meta_description": p.get("meta_description"), "slug": p.get("slug"),
                               "gate_ledger": json.loads(ledger) if isinstance(ledger, str) else ledger or [],
                               "flags": json.loads(flags) if isinstance(flags, str) else flags or []},
                    "stored_status": p.get("status"), "attempt_number": p.get("attempt_number")})
    return out, skipped


# ------------------------------------------------------------------------------------------- replay

def _stored_f9(ledger: list) -> Optional[list]:
    from services.quality import registry as qr
    by = {g.get("gate"): g for g in ledger or [] if isinstance(g, dict)}
    if qr.F9_CTA_FACT in by and qr.F9_BRAND_STYLE in by:
        return [copy.deepcopy(by[qr.F9_CTA_FACT]), copy.deepcopy(by[qr.F9_BRAND_STYLE])]
    return None


def _stored_match(ledger: list) -> Optional[dict]:
    from services.quality import registry as qr
    for g in ledger or []:
        if isinstance(g, dict) and g.get("gate") == qr.F10_CANNIBALIZATION_CROSS_TENANT and not g.get("passed"):
            m = _F10_RE.search(" ".join(map(str, g.get("violations") or [])))
            if m:
                return {"piece_id": m.group(2), "tenant_id": "other", "similarity": float(m.group(1)),
                        "writer_missing_brand_rules": "no active brand rules" in " ".join(g["violations"])}
    return None


def _route(plan) -> str:
    return "rewrite" if plan is None else "none" if plan == [] else "repair"


def replay_one(piece: dict, stock_blocking: bool, dash_fix: bool = False) -> dict:
    from scripts.aa729 import score
    from services.acp_content_writing import hygiene
    from services.acp_content_writing import quality_gates as qg
    from services.acp_content_writing import sentence_repair
    from services.acp_content_writing import service as svc
    from services.quality import registry as qr

    expand = getattr(qg, "expand_ledger", lambda ledger: list(ledger or []))
    stored = piece["output"].get("gate_ledger") or []
    output = piece["output"]
    if dash_fix:
        output = {**output, "content_text": hygiene.fix_dashes(output.get("content_text") or "")[0]}
    f9 = _stored_f9(stored)
    match = _stored_match(stored)
    real_gates = svc.run_quality_gates

    def voice(*_a, **_k):
        if f9 is not None:
            return copy.deepcopy(f9)
        return [qg._result(qr.F9_CTA_FACT, []), qg._result(qr.F9_BRAND_STYLE, [], blocking=False)]

    def gates(*a, **k):
        return real_gates(*a, **{**k, "stock_blocking": stock_blocking, "cannibalization_match": match})

    with patch.object(qg, "gate_brand_voice", side_effect=voice), patch.object(svc, "run_quality_gates", gates):
        outcome = asyncio.run(score.t9_gate_replay(piece["context"], output))
    ledger, flags = outcome["gate_ledger"], outcome["flags"]
    view = expand(ledger)
    if outcome["passed"]:
        status, held = "approved", None
    else:
        status, reason, _ = svc._hold_or_ship(ledger, list(flags))
        held = reason.split(":", 1)[0] if reason else None
    hold = qg.hold_failure(ledger)
    plan = sentence_repair.plan(output.get("content_text") or "", ledger)
    findings = Counter(f"{g['gate']}|{f.get('source')}|{f.get('severity')}" for g in ledger
                       if isinstance(g.get("findings"), list) for f in g["findings"])
    return {
        "passed": bool(outcome["passed"]), "status": status, "held_gate": held,
        "first_failure": (outcome.get("first_failure") or {}).get("gate"), "hold_failure": (hold or {}).get("gate"),
        "route": _route(plan), "plan_items": len(plan or []), "style_only_repair": svc._style_only_repair(ledger),
        "plan_problems": sorted(f"{i.get('gate')} | {str(i.get('sentence'))[:80]} | {i.get('problem')}"
                                for i in plan or []),
        "blocking_failures": sorted(g["gate"] for g in view if not g["passed"] and g.get("blocking", True)),
        "flags_stored": dict(Counter(g.get("gate") for g in flags if isinstance(g, dict))),
        "flags_legacy": dict(Counter(g.get("gate") for g in expand(flags) if isinstance(g, dict))),
        "results": len(ledger), "findings": dict(findings), "f9_stored": f9 is not None,
    }


def run(pieces: list[dict], skipped: list[str]) -> dict:
    import tests.unit.conftest  # noqa: F401 — the CI unit-test env (dummy values), set at import

    rows = []
    for p in pieces:
        for mode, (sb, dash_fix) in MODES.items():
            try:
                res = replay_one(p, sb, dash_fix)
            except Exception as e:  # noqa: BLE001 — reported, never hidden
                res = {"error": f"{type(e).__name__}: {e}"[:300]}
            rows.append({"id": p["id"], "source": p["source"], "stratum": p.get("stratum"), "mode": mode,
                         "stored_status": p.get("stored_status"), **res})
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"schema": SCHEMA, "tree": os.getcwd(), "commit": head, "rows": rows, "skipped_no_writer_input": skipped}


# ------------------------------------------------------------------------------------------- diff + report

_COMPARED = ("passed", "status", "first_failure", "hold_failure", "route", "style_only_repair", "blocking_failures",
             "flags_legacy", "plan_problems")


def diff(base: dict, head: dict) -> list[dict]:
    rename = _rename()
    b = {(r["id"], r["mode"]): r for r in base["rows"]}
    out = []
    for r in head["rows"]:
        o = b.get((r["id"], r["mode"]))
        if o is None:
            out.append({"id": r["id"], "mode": r["mode"], "field": "missing in base", "base": None, "head": None})
            continue
        for f in _COMPARED + ("held_gate", "error"):
            ov, nv = o.get(f), r.get(f)
            if f == "held_gate" and ov is not None:
                ov = rename.get(ov, ov)
            if ov != nv:
                if f == "plan_problems":  # only what changed: the items one side has and the other has not
                    ov, nv = sorted(set(ov or []) - set(nv or [])), sorted(set(nv or []) - set(ov or []))
                out.append({"id": r["id"], "source": r["source"], "mode": r["mode"], "field": f, "base": ov,
                            "head": nv})
    return out


def _cell(v: Any) -> str:
    return str(v).replace("|", "/").replace("\n", " ")[:400]


def _confusion(base: dict, head: dict, source: str, mode: str) -> dict:
    b = {(r["id"], r["mode"]): r for r in base["rows"]}
    c: Counter = Counter()
    for r in head["rows"]:
        if r["source"] != source or r["mode"] != mode or "error" in r:
            continue
        o = b.get((r["id"], mode)) or {}
        c[(o.get("status"), r.get("status"))] += 1
    return c


def report(base: dict, head: dict, *, golden_path: Optional[str], s234_path: Optional[str]) -> str:
    diffs = diff(base, head)
    sources = sorted({r["source"] for r in head["rows"]})
    lines = ["# AA-924 replay — T10 gate fold (F2_lexicon, F15_voice) on stored T9 pieces", "",
             f"Base tree commit `{base.get('commit')}`, head commit `{head.get('commit')}`. $0: no LLM call (F9 "
             "verdicts replayed from each piece's stored ledger), no DB, no network. Each piece is replayed as "
             "attempt 1 (`stock_blocking=True`) and as a later attempt (`stock_blocking=False`), on the stored text "
             "and (`_dashfix`) on the stored text after `hygiene.fix_dashes`.", "",
             "Inputs:", ""]
    if golden_path:
        lines.append(f"- golden: `{golden_path}` (the AA-729 T9 samples)")
    if s234_path:
        lines.append(f"- S234: `{s234_path}`; skipped without `writer_input`: "
                     f"{len(head.get('skipped_no_writer_input') or [])}")
    lines += ["", "## Decisions, base vs head", "",
              "| source | mode | pieces | approved→approved | held→held | approved→held | held→approved | errors |",
              "|---|---|---|---|---|---|---|---|"]
    for src in sources:
        for mode in MODES:
            c = _confusion(base, head, src, mode)
            n = sum(1 for r in head["rows"] if r["source"] == src and r["mode"] == mode)
            errs = sum(1 for r in head["rows"] if r["source"] == src and r["mode"] == mode and "error" in r)
            lines.append(f"| {src} | {mode} | {n} | {c[('approved', 'approved')]} | {c[('held', 'held')]} | "
                         f"{c[('approved', 'held')]} | {c[('held', 'approved')]} | {errs} |")
    decision = [d for d in diffs if d["field"] in ("passed", "status", "hold_failure", "first_failure", "route",
                                                   "style_only_repair", "error", "missing in base")]
    lines += ["", f"## Differences in routing decisions ({len(decision)})", ""]
    if decision:
        lines += ["| piece | source | mode | field | base | head |", "|---|---|---|---|---|---|"]
        lines += [f"| {d['id']} | {d.get('source')} | {d['mode']} | {d['field']} | {d['base']} | {d['head']} |"
                  for d in decision]
    else:
        lines.append("None: passed / status / first failure / hold / repair route / stage are identical.")
    other = [d for d in diffs if d not in decision]
    lines += ["", f"## Other differences ({len(other)})", "",
              "Blocking failures of the legacy view, legacy-view flags, what each sentence repair is told "
              "(`plan_problems`: only the items one side has) and the held gate (base name mapped through the fold). "
              "Expected: none, except a word that was judged in two gates (now judged once).", ""]
    if other:
        lines += ["| piece | source | mode | field | base | head |", "|---|---|---|---|---|---|"]
        lines += [f"| {d['id']} | {d.get('source')} | {d['mode']} | {d['field']} | {_cell(d['base'])} | "
                  f"{_cell(d['head'])} |" for d in other]
    else:
        lines.append("None.")
    fb, fh = Counter(), Counter()
    for r in base["rows"]:
        fb.update(r.get("flags_stored") or {})
    for r in head["rows"]:
        fh.update(r.get("flags_stored") or {})
    lines += ["", "## Warn flags by stored name (all rows)", "", "| flag | base | head |", "|---|---|---|"]
    lines += [f"| {k} | {fb.get(k, 0)} | {fh.get(k, 0)} |" for k in sorted(set(fb) | set(fh))]
    sev = Counter()
    for r in head["rows"]:
        sev.update(r.get("findings") or {})
    lines += ["", "## Head: findings of the merged entries (gate / source / severity)", "", "| finding | count |",
              "|---|---|"]
    lines += [f"| {k.replace('|', ' / ')} | {v} |" for k, v in sorted(sev.items())]
    counts = Counter((r["source"], r.get("results")) for r in head["rows"] if "results" in r)
    bcounts = Counter((r["source"], r.get("results")) for r in base["rows"] if "results" in r)
    lines += ["", "## Results per ledger", "", "| source | base results (rows) | head results (rows) |",
              "|---|---|---|"]
    for src in sources:
        lines.append(f"| {src} | {dict(sorted((k[1], v) for k, v in bcounts.items() if k[0] == src))} | "
                     f"{dict(sorted((k[1], v) for k, v in counts.items() if k[0] == src))} |")
    agree = Counter((r["source"], r.get("stored_status") == r.get("status")) for r in head["rows"]
                    if r["mode"] == "later" and r.get("stored_status") in ("approved", "held"))
    reach = Counter((r["source"], r["mode"], r.get("first_failure") or "none") for r in head["rows"]
                    if "error" not in r)
    lines += ["", "## Head: first failure (legacy source) per mode", "",
              "How much of the routing the replay exercises: a first failure on F2_banned_patterns stops every later "
              "step, so the `_dashfix` modes are the ones that reach F15, the waive and the style-only paths.", "",
              "| source | mode | first failure | rows |", "|---|---|---|---|"]
    lines += [f"| {k[0]} | {k[1]} | {k[2]} | {v} |" for k, v in sorted(reach.items())]
    lines += ["", "## Context: head (later-attempt mode) vs the stored status", "",
              "Not a test of AA-924 (the stored status came from older code and the full repair loop); shown so a "
              "reader can see how close the replay inputs are to production.", "", "| source | same | different |",
              "|---|---|---|"]
    for src in sources:
        lines.append(f"| {src} | {agree[(src, True)]} | {agree[(src, False)]} |")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------------------- CLI

def _load_inputs(args) -> tuple[list[dict], list[str]]:
    pieces, skipped = [], []
    if args.golden:
        pieces += load_golden(args.golden)
    if args.s234:
        s, sk = load_s234(args.s234)
        pieces += s
        skipped += sk
    if not pieces:
        raise SystemExit("no input: pass --golden and/or --s234")
    return pieces, skipped


def _run_in(tree: str, args, out: str) -> None:
    cmd = [sys.executable, str(Path(__file__).resolve()), "run", "--out", out]
    for flag in ("golden", "s234"):
        if getattr(args, flag):
            cmd += [f"--{flag}", str(Path(getattr(args, flag)).resolve())]
    env = {**os.environ, "PYTHONPATH": tree, "AWS_EC2_METADATA_DISABLED": "true",
           "AWS_PROFILE": os.environ.get("AWS_PROFILE_REPLAY", "replay-dummy")}
    subprocess.run(cmd, cwd=tree, env=env, check=True)


def main() -> None:  # pragma: no cover — manual
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="replay on the code tree in the current directory")
    r.add_argument("--golden")
    r.add_argument("--s234")
    r.add_argument("--out", required=True)
    d = sub.add_parser("diff", help="diff two run outputs")
    d.add_argument("base")
    d.add_argument("head")
    d.add_argument("--report", required=True)
    d.add_argument("--golden")
    d.add_argument("--s234")
    a = sub.add_parser("all", help="run on --base-tree and on this tree, then diff")
    a.add_argument("--base-tree", required=True)
    a.add_argument("--golden")
    a.add_argument("--s234")
    a.add_argument("--out-dir", required=True)
    a.add_argument("--report", required=True)
    args = ap.parse_args()
    if args.cmd == "run":
        sys.path.insert(0, os.getcwd())
        pieces, skipped = _load_inputs(args)
        Path(args.out).write_text(json.dumps(run(pieces, skipped), indent=1, default=str), encoding="utf-8")
        print(f"wrote {args.out}: {len(pieces)} pieces, {len(skipped)} skipped")
        return
    if args.cmd == "all":
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        _run_in(str(Path(args.base_tree).resolve()), args, str((out / "base.json").resolve()))
        _run_in(os.getcwd(), args, str((out / "head.json").resolve()))
        args.base, args.head = str(out / "base.json"), str(out / "head.json")
    base, head = json.loads(Path(args.base).read_text()), json.loads(Path(args.head).read_text())
    Path(args.report).write_text(report(base, head, golden_path=args.golden, s234_path=args.s234), encoding="utf-8")
    print(f"wrote {args.report}: {len(diff(base, head))} differences")


if __name__ == "__main__":  # pragma: no cover
    main()
