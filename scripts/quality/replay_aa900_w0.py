"""AA-900 W0 (root ADR 0020) — $0 offline replay of the interim F9 policy and the stop-and-label attempt 3 over stored
T9 pieces (no LLM, no DB, no network).

For every stored piece it reads the F9_cta_fact entries the piece already has (the real judge verdicts: the final
ledger, the flags and, when present, each repair_log attempt's ledger snapshot) and routes them twice: the legacy
policy (what main does today) and the interim policy (`f9_policy.apply_policy`, the same call `run_quality_gates`
makes). The other gates are taken as stored. Per piece it records:

- the F9 classes of the final entry (fidelity vs judge_unavailable vs text_integrity — critic F15's split);
- legacy vs interim: does F9 block, the hold gate (`quality_gates.hold_failure`), held vs approved;
- the typed reason codes a held piece gets (`gate_eval.hold_reason_codes`);
- attempt 3 (when the repair_log is stored): did the piece pay for attempt 3, and did attempt 3 cut a sentence in
  code — both stop under W0.

Inputs (any mix; plain or gzipped JSON):
  --golden PATH   root docs/eval/aa729/fixture-<v>.json (the T9 samples: final text, ledger, flags, context)
  --pieces PATH   a read-only export of acp_shared.content_piece rows (a list of objects with gate_ledger, flags,
                  repair_log, status, held_reason, attempt_number, channel, writer_input); optional

Usage (from a CIS checkout):
    python -m scripts.quality.replay_aa900_w0 --golden ../../docs/eval/aa729/fixture-1.2.0.json \\
        --out /tmp/aa900_w0.json --report /tmp/aa900_w0.md
"""
from __future__ import annotations

import argparse
import gzip
import json
from collections import Counter
from pathlib import Path
from typing import Any, Optional

SCHEMA = "aa900-w0-replay/1"
_CUT_MODES = frozenset({"truth_cut", "truth_cut+sentences", "truth_cut+sentences_cut", "sentences_cut",
                        "f9_truth_cut"})


def _read_json(path: str) -> Any:
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _json(v: Any) -> Any:
    return json.loads(v) if isinstance(v, str) else v


def load_golden(path: str) -> list[dict]:
    out = []
    for s in _read_json(path).get("samples") or []:
        if s.get("kind") != "t9":
            continue
        ctx = (s.get("input") or {}).get("context") or {}
        verdict = s.get("pipeline_verdict") or {}
        out.append({"id": s["sample_id"], "source": "golden", "channel": s.get("channel") or ctx.get("channel"),
                    "gate_ledger": _json(s["output"].get("gate_ledger")) or [],
                    "flags": _json(s["output"].get("flags")) or [], "repair_log": None,
                    "status": verdict.get("status"), "attempt_number": None, "context": ctx})
    return out


def piece_context(writer_input) -> dict:
    """The lexicon context of a stored piece. AA-1047 (W0 review #1356): writer_input stores the seed as `seed` and
    the trip as `trip` (service.writer_input_snapshot), while the lexicon reads `atom_text` / `trip_name` (the job's
    context names): read both, so a --pieces replay builds the same scope lexicon the write did."""
    wi = writer_input if isinstance(writer_input, dict) else {}
    return {**wi, "atom_text": wi.get("atom_text") or wi.get("seed"), "trip_name": wi.get("trip_name") or wi.get("trip")}


def load_pieces(path: str) -> list[dict]:
    out = []
    for r in _read_json(path) or []:
        wi = _json(r.get("writer_input")) or {}
        out.append({"id": str(r.get("piece_id")), "source": "pieces", "channel": r.get("channel"),
                    "gate_ledger": _json(r.get("gate_ledger")) or [], "flags": _json(r.get("flags")) or [],
                    "repair_log": _json(r.get("repair_log")), "status": r.get("status"),
                    "attempt_number": r.get("attempt_number"), "context": piece_context(wi)})
    return out


def _lexicon(ctx: dict):
    from services.acp_content_writing.hygiene import build_scope_lexicon
    if not ctx.get("atom_text"):
        return None
    return build_scope_lexicon(ctx.get("atom_text"), ctx.get("scope"), trip_name=ctx.get("trip_name"),
                               trip_places=tuple(ctx.get("trip_places") or ()))


def _routed(ledger: list[dict], mode: str, lexicon) -> tuple[list[dict], Optional[dict]]:
    from services.acp_content_writing import f9_policy
    from services.acp_content_writing.quality_gates import hold_failure
    from services.quality import registry as qr
    out = [f9_policy.apply_policy(g, lexicon, mode=mode) if g.get("gate") == qr.F9_CTA_FACT else g for g in ledger]
    return out, hold_failure(out)


def replay_piece(p: dict) -> dict:
    from services.acp_content_writing import f9_policy, gate_eval
    from services.quality import registry as qr
    lexicon = _lexicon(p["context"])
    # the F9 entry may sit in the ledger (blocking) or only in the flags (a warn: outage, reasonless, title / meta)
    ledger = list(p["gate_ledger"])
    if not any(g.get("gate") == qr.F9_CTA_FACT for g in ledger):
        ledger += [f for f in p["flags"] if isinstance(f, dict) and f.get("gate") == qr.F9_CTA_FACT]
    f9 = next((g for g in ledger if g.get("gate") == qr.F9_CTA_FACT), None)
    rows: dict[str, Any] = {}
    for mode in (f9_policy.LEGACY_MODE, f9_policy.INTERIM_MODE):
        routed, hold = _routed(ledger, mode, lexicon)
        f9_now = next((g for g in routed if g.get("gate") == qr.F9_CTA_FACT), None)
        rows[mode] = {
            "f9_blocks": bool(f9_now and not f9_now.get("passed") and f9_now.get("blocking", True)),
            "hold_gate": hold.get("gate") if hold else None,
            "status": "held" if hold else "approved",
            "reason_codes": gate_eval.hold_reason_codes(routed) if hold else [],
        }
    log = p.get("repair_log")
    attempt3 = None
    if isinstance(log, list):
        modes = [e.get("repair_mode") for e in log if isinstance(e, dict)]
        attempt3 = {"paid_attempt_3": (p.get("attempt_number") or 0) >= 3,
                    "code_cut": any(m in _CUT_MODES for m in modes)}
    return {"id": p["id"], "source": p["source"], "channel": p["channel"], "stored_status": p["status"],
            "f9_classes": f9_policy.entry_classes(f9, lexicon) if f9 else {},
            "f9_kinds": sorted({k for v in (f9 or {}).get("violations") or []
                                for it in f9_policy.violation_items(v, lexicon) for k in it.get("kinds") or []}),
            "legacy": rows[f9_policy.LEGACY_MODE], "interim": rows[f9_policy.INTERIM_MODE], "attempt3": attempt3}


def summarise(results: list[dict]) -> dict:
    from services.acp_content_writing import f9_policy
    classes: Counter = Counter()
    for r in results:
        classes.update(r["f9_classes"])
    flips = Counter((r["legacy"]["status"], r["interim"]["status"]) for r in results)
    hold_gates = {m: Counter(r[m]["hold_gate"] or "-" for r in results) for m in ("legacy", "interim")}
    a3 = [r["attempt3"] for r in results if r["attempt3"] is not None]
    return {
        "schema": SCHEMA, "pieces": len(results),
        "f9_classes": dict(classes.most_common()),
        "f9_fidelity": sum(n for c, n in classes.items() if c in f9_policy.FIDELITY_CLASSES),
        "f9_infrastructure": sum(n for c, n in classes.items() if c in f9_policy.INFRASTRUCTURE_CLASSES),
        "f9_text_integrity": classes.get(f9_policy.TEXT_INTEGRITY, 0),
        "f9_blocks": {m: sum(r[m]["f9_blocks"] for r in results) for m in ("legacy", "interim")},
        "held": {m: sum(r[m]["status"] == "held" for r in results) for m in ("legacy", "interim")},
        "status_flips": {f"{a}->{b}": n for (a, b), n in sorted(flips.items())},
        "hold_gates": {m: dict(c.most_common()) for m, c in hold_gates.items()},
        "with_repair_log": len(a3),
        "paid_attempt_3_saved": sum(x["paid_attempt_3"] for x in a3),
        "code_cuts_stopped": sum(x["code_cut"] for x in a3),
    }


def report(summary: dict, results: list[dict]) -> str:
    s = summary
    lines = [
        "# AA-900 W0 replay — interim F9 policy and stop-and-label ($0)", "",
        f"Pieces: {s['pieces']} (with a stored repair_log: {s['with_repair_log']}).", "",
        "| Measure | Legacy (main today) | Interim (W0) |", "|---|---|---|",
        f"| F9_cta_fact blocks | {s['f9_blocks']['legacy']} | {s['f9_blocks']['interim']} |",
        f"| Held (stored gates, F9 re-routed) | {s['held']['legacy']} | {s['held']['interim']} |", "",
        f"F9 classes (final entry): {json.dumps(s['f9_classes'])}.",
        f"Fidelity {s['f9_fidelity']}, judge unavailable {s['f9_infrastructure']}, "
        f"TEXT_INTEGRITY {s['f9_text_integrity']}.", "",
        f"Status flips: {json.dumps(s['status_flips'])}.",
        f"Hold gates legacy: {json.dumps(s['hold_gates']['legacy'])}.",
        f"Hold gates interim: {json.dumps(s['hold_gates']['interim'])}.", "",
        f"Attempt 3: {s['paid_attempt_3_saved']} paid attempt-3 runs stop at $0; {s['code_cuts_stopped']} code cuts "
        "stop.", "",
        "| Piece | Channel | F9 classes | Kinds | Legacy | Interim |", "|---|---|---|---|---|---|",
    ]
    for r in results:
        if r["legacy"] == r["interim"] and not r["f9_classes"]:
            continue
        lines.append(f"| {r['id'][:8]} | {r['channel']} | {json.dumps(r['f9_classes'])} | {','.join(r['f9_kinds'])} "
                     f"| {r['legacy']['status']} ({r['legacy']['hold_gate'] or '-'}) "
                     f"| {r['interim']['status']} ({r['interim']['hold_gate'] or '-'}) |")
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--golden", action="append", default=[])
    ap.add_argument("--pieces", action="append", default=[])
    ap.add_argument("--out")
    ap.add_argument("--report")
    args = ap.parse_args(argv)
    pieces = [p for path in args.golden for p in load_golden(path)] + \
        [p for path in args.pieces for p in load_pieces(path)]
    results = [replay_piece(p) for p in pieces]
    summary = summarise(results)
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "pieces": results}, indent=1, default=str))
    if args.report:
        Path(args.report).write_text(report(summary, results))
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
