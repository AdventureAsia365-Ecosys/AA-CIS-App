"""AA-900 W0 — the $0 replay script (scripts/quality/replay_aa900_w0.py) on invented pieces."""
from __future__ import annotations

import json

from scripts.quality import replay_aa900_w0 as rp
from services.quality import registry as qr

PROMISE = ('audit flagged: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "You will ride a camel at dawn." '
           "(claims: camel ride; source: silent)")
BACKGROUND = ('audit flagged: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): "The old fort was built by a river '
              'king." (claims: founder; source: silent)')


def _sample(sid, violations, blocking=True):
    f9 = {"gate": qr.F9_CTA_FACT, "passed": False, "blocking": blocking, "repairable": True, "violations": violations}
    return {"kind": "t9", "sample_id": sid, "channel": "blog",
            "input": {"context": {"atom_text": "Location: Old Fort\nSource itinerary: fort visit."}},
            "output": {"gate_ledger": [f9] if blocking else [], "flags": [] if blocking else [f9]},
            "pipeline_verdict": {"status": "held" if blocking else "approved"}}


def test_replay_shows_the_declared_diff(tmp_path):
    golden = tmp_path / "golden.json"
    golden.write_text(json.dumps({"samples": [
        _sample("a" * 8, [PROMISE]), _sample("b" * 8, [BACKGROUND]),
        _sample("c" * 8, ["judge unavailable: timeout — not checked"], blocking=False)]}))
    pieces = tmp_path / "pieces.json"
    pieces.write_text(json.dumps([{"piece_id": "d" * 8, "channel": "blog", "status": "approved", "attempt_number": 3,
                                   "gate_ledger": [], "flags": [],
                                   "repair_log": [{"attempt": 2, "repair_mode": "truth_cut"}]}]))
    out = tmp_path / "out.json"
    md = tmp_path / "report.md"
    assert rp.main(["--golden", str(golden), "--pieces", str(pieces), "--out", str(out), "--report", str(md)]) == 0
    s = json.loads(out.read_text())["summary"]
    assert s["pieces"] == 4
    assert s["f9_blocks"] == {"legacy": 2, "interim": 1}
    assert s["held"] == {"legacy": 2, "interim": 1} and s["status_flips"]["held->approved"] == 1
    assert s["f9_infrastructure"] == 1 and s["f9_fidelity"] == 2
    assert s["paid_attempt_3_saved"] == 1 and s["code_cuts_stopped"] == 1
    assert "Interim (W0)" in md.read_text()
