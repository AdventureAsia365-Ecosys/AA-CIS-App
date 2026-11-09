"""S271 W0 — the offline replay script (scripts/aa900/w0_r2_replay.py) on invented rows: pairing old vs new, hard
stop 1 under the old and the W0 rule, the ledger estimate. No DB, no model, no real text (public repo)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def replay():
    spec = importlib.util.spec_from_file_location("w0_r2_replay", ROOT / "scripts" / "aa900" / "w0_r2_replay.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HARD = {"fact.inclusion", "fact.number"}


def _row(arm, sample, verdict, codes=(), *, label="pass", trap=False, t=0, cost=0.0, paired=False):
    return {"run_id": "run-1", "item_id": f"i-{sample}", "sample_id": sample, "arm": arm,
            "mode": "rejudge" if arm == "control" else "regenerate", "verdict": verdict,
            "failure_codes": list(codes), "label_verdict": label, "trap": trap, "split": "trap" if trap else "tune",
            "post_edit": False, "cost_usd": cost, "created_at": f"2026-01-01 00:00:{t:02d}",
            "details": {"calibration": "only"} if arm == "control" else {"paired": paired}}


def _export():
    rows = [
        # controls (the stored calibration verdicts): zz-a fail, zz-b pass, zz-c pass
        _row("control", "zz-a", "fail", ["fact.number"], label="pass"),
        _row("control", "zz-b", "pass", label="pass"),
        _row("control", "zz-c", "pass", label="fail"),
        # candidates, in order: a trap, a fail on a fail control with a new class (old rule stops here),
        # a pass with a new class (noise), then a fail on a pass control with a new class (the W0 stop)
        _row("candidate", "zz-trap", "fail", ["fact.inclusion"], trap=True, t=1, cost=0.1),
        _row("candidate", "zz-a", "fail", ["fact.inclusion"], t=2, cost=0.1),
        _row("candidate", "zz-c", "pass", ["fact.inclusion"], t=3, cost=0.12),
        _row("candidate", "zz-b", "fail", ["fact.inclusion"], t=4, cost=0.14),
    ]
    run = {"kind": "run", "id": "run-1", "run_kind": "r2", "cost_usd": "0.5"}
    return ["not json", json.dumps(run)] + [json.dumps(r) for r in rows]


def test_replay_pairing_and_the_two_hard_stop_rules(replay):
    runs, rows = replay.load(_export())
    assert list(runs) == ["run-1"] and len(rows) == 7
    rep = replay.replay_run("run-1", rows, hard=HARD, run=runs["run-1"])
    assert (rep["paired_old"], rep["paired_new"], rep["planned_non_trap"], rep["traps"]) == (0, 3, 3, 1)
    assert rep["hard_stop_old"]["sample_id"] == "zz-a" and rep["hard_stop_old"]["nth_candidate"] == 2
    assert rep["hard_stop_new"]["sample_id"] == "zz-b" and rep["hard_stop_new"]["nth_candidate"] == 4
    assert rep["new_hard_on_pass"] == ["zz-c"] and rep["new_hard_on_fail_control"] == ["zz-a"]
    assert rep["estimate"]["source"] == "config"               # 4 candidate costs < R2_MIN_COST_SAMPLES


def test_replay_what_if_controls_from_labels_and_counts_off(replay):
    runs, rows = replay.load(_export())
    rep = replay.replay_run("run-1", rows, hard=HARD, control_from_label=True)
    assert rep["hard_stop_new"]["sample_id"] == "zz-a"         # its label is pass: a fail with a new class stops
    off = replay.replay_run("run-1", rows, hard=HARD, counts=False)
    assert off["hard_stop_old"] is None and off["hard_stop_new"] is None and off["paired_new"] == 3


def test_replay_main_prints_one_line_per_r2_run(replay, tmp_path, capsys):
    p = tmp_path / "export.jsonl"
    p.write_text("\n".join(_export()), encoding="utf-8")
    assert replay.main([str(p)]) == 0
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()]
    assert len(lines) == 1 and lines[0]["run_id"] == "run-1" and lines[0]["paired_new"] == 3
