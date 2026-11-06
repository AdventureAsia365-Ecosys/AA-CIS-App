#!/usr/bin/env python3
"""AA-900 S271 W0 — OFFLINE replay of stored R2 rows: pairing, hard stop 1 and the ledger estimate. $0: no DB, no
model call, no write.

Input: a JSONL export of R2 runs and their result rows (the shape the S271 judge root-cause export wrote, one object
per line): run rows `{"kind": "run", "id", "run_kind", "cost_usd", "tour_count", ...}` and result rows
`{"run_id", "item_id", "sample_id", "arm", "mode", "phase", "split", "trap", "post_edit", "label_verdict",
"verdict", "failure_codes", "cost_usd", "created_at", ...}`. Lines that are not JSON objects are skipped. Only ids,
verdicts, codes and costs are read (never text), so the export stays outside the repo.

For each r2 run it recomputes, with the code of this checkout:
- pairing: OLD = the stored `paired` flags (item_id pairing on the active release's eval run), NEW = what the W0
  pairing gives on the run's own golden version: a candidate is paired when the run holds a control row of the same
  sample (release 0 -> this version's baseline, so the control verdict is the calibration verdict of that sample);
- hard stop 1 under the old rule (any new hard fact class vs the control's classes) and under the W0 rule
  (`eval_checks.compare_item`: only on a candidate fail against a control pass), in candidate order: which item
  would have stopped the run, and how many candidates were written before it;
- the per-item estimate `r2.item_estimate` from the run's candidate costs and what it predicts for the run
  (calibration + candidates) against the logged run cost.

  python3 scripts/aa900/w0_r2_replay.py <export.jsonl> [--run <id-prefix> ...] [--counts-off]

`--counts-off` replays as if the judge did not count (hard stop 1 never fires); the default assumes it counts, which
is the case the W0 rule is meant for. `--control-from-label` is a what-if for a calibrated judge: each control's
verdict is its golden label (its stored classes are kept), so the stop rule is seen against pass controls too.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Iterable, Optional

for _p in (os.getcwd(), os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))):
    if os.path.isdir(os.path.join(_p, "services")) and _p not in sys.path:
        sys.path.insert(0, _p)

from services.writing_memory import config as cfg  # noqa: E402
from services.writing_memory import eval_checks as ec  # noqa: E402


def load(lines: Iterable[str]) -> tuple[dict[str, dict], list[dict]]:
    runs, rows = {}, []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("kind") == "run":
            runs[obj["id"]] = obj
        elif obj.get("run_id") and obj.get("arm"):
            rows.append(obj)
    return runs, rows


def _codes(row: dict) -> list[str]:
    v = row.get("failure_codes") or []
    return list(v) if isinstance(v, list) else []


def _details(row: dict) -> dict:
    d = row.get("details")
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except ValueError:
            d = {}
    return d if isinstance(d, dict) else {}


def _control_verdict(row: dict) -> dict:
    """The control's verdict as the W0 pairing would see it: the calibration verdict of the frozen text when the
    row carries one apart ('only' / same_row = the row's own verdict; a dict = the calibration of the frozen text)."""
    cal = _details(row).get("calibration")
    if isinstance(cal, dict):
        return {"verdict": cal.get("verdict"), "failure_codes": list(cal.get("failure_codes") or [])}
    return {"verdict": row.get("verdict"), "failure_codes": _codes(row)}


def old_hard_stop(control: Optional[dict], cand: dict, hard: set[str], counts: bool) -> bool:
    """The rule before W0: any NEW hard fact class vs the control's classes, whatever the verdicts."""
    if not counts or control is None or control.get("verdict") not in ("pass", "fail"):
        return False
    return bool((set(_codes(cand)) & hard) - set(control.get("failure_codes") or []))


def replay_run(run_id: str, rows: list[dict], *, hard: set[str], counts: bool = True,
               run: Optional[dict] = None, control_from_label: bool = False) -> dict:
    mine = [r for r in rows if r["run_id"] == run_id]
    controls = {r["sample_id"]: _control_verdict(r) for r in mine if r["arm"] == "control"}
    if control_from_label:
        labels = {r["sample_id"]: r.get("label_verdict") for r in mine if r["arm"] == "control"}
        controls = {k: {**v, "verdict": labels.get(k) or v["verdict"]} for k, v in controls.items()}
    cands = sorted((r for r in mine if r["arm"] == "candidate"), key=lambda r: str(r.get("created_at") or ""))
    out = {"run_id": run_id, "control_from_label": control_from_label, "candidates": len(cands),
           "traps": sum(1 for r in cands if r.get("trap")),
           "paired_old": sum(1 for r in cands if _details(r).get("paired")), "paired_new": 0,
           "planned_non_trap": sum(1 for r in cands if not r.get("trap")),
           "hard_stop_old": None, "hard_stop_new": None, "new_hard_on_pass": [], "new_hard_on_fail_control": []}
    for n, c in enumerate(cands, start=1):
        if c.get("trap"):
            continue
        ctl = controls.get(c["sample_id"])
        cand = {"verdict": c.get("verdict"), "failure_codes": _codes(c), "qa_artefacts": [],
                "isolation": {"leak": False}, "injection": []}
        cmp = ec.compare_item(control=ctl, candidate=cand, counts=counts)
        if cmp["paired"]:
            out["paired_new"] += 1
        if cmp["new_hard"] and c.get("verdict") == "pass":
            out["new_hard_on_pass"].append(c["sample_id"])
        if cmp["new_hard"] and (ctl or {}).get("verdict") == "fail":
            out["new_hard_on_fail_control"].append(c["sample_id"])
        if out["hard_stop_old"] is None and old_hard_stop(ctl, c, hard, counts):
            out["hard_stop_old"] = {"sample_id": c["sample_id"], "nth_candidate": n, "verdict": c.get("verdict"),
                                    "control": (ctl or {}).get("verdict")}
        if out["hard_stop_new"] is None and cmp["hard_stop"] == 1:
            out["hard_stop_new"] = {"sample_id": c["sample_id"], "nth_candidate": n, "verdict": c.get("verdict"),
                                    "control": (ctl or {}).get("verdict"), "classes": cmp["new_hard"]}
    costs = [float(r.get("cost_usd") or 0) for r in cands if r.get("verdict") != "error"]
    from services.writing_memory.r2 import item_estimate
    est = item_estimate("t9_blog" if any(str(r.get("sample_id", "")).startswith("t9b") for r in mine)
                        else "a1", costs)
    n_ctl = sum(1 for r in mine if r["arm"] == "control")
    predicted = n_ctl * cfg.JUDGE_CALL_ESTIMATE_USD + len(cands) * (est["item_usd"] + cfg.JUDGE_CALL_ESTIMATE_USD)
    logged = float((run or {}).get("cost_usd") or 0) or None
    out["estimate"] = {**est, "predicted_run_usd": round(predicted, 4), "logged_run_usd": logged,
                       "ratio": round(predicted / logged, 3) if logged else None}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("export")
    ap.add_argument("--run", action="append", default=[], help="run id prefix (default: every r2 run)")
    ap.add_argument("--counts-off", action="store_true")
    ap.add_argument("--control-from-label", action="store_true")
    a = ap.parse_args(argv)
    from services.quality.registry import hard_fact_classes
    with open(a.export, encoding="utf-8") as f:
        runs, rows = load(f)
    ids = [rid for rid, r in runs.items() if r.get("run_kind") == "r2"
           and (not a.run or any(rid.startswith(p) for p in a.run))]
    ids += [rid for rid in {r["run_id"] for r in rows} if rid not in runs and a.run
            and any(rid.startswith(p) for p in a.run)]
    for rid in ids:
        rep = replay_run(rid, rows, hard=set(hard_fact_classes()), counts=not a.counts_off, run=runs.get(rid),
                         control_from_label=a.control_from_label)
        print(json.dumps(rep, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
