"""AA-924 PR1c — the offline replay harness (scripts/quality/replay_aa924.py): input loading, the stored-F9 stub,
one replay on the real gates and the base/head diff. $0, no DB."""
import gzip
import json

from scripts.quality import replay_aa924 as rp
from services.quality import registry as qr
from tests.unit import aa791_t9_fault_set as fs

WRITER_INPUT = {
    "seed": fs.SEED, "route_moments": [], "facts": "", "scope": fs.SCOPE, "brand_pack": fs.PACK,
    "brand_rubric": "rubric", "angle": {"name": "First night"}, "goal": "awareness", "channel": "facebook",
    "keyword": None, "cta": fs.CTA, "trip": fs.TRIP, "destination": "Mongolia",
}
F9 = [{"gate": qr.F9_CTA_FACT, "passed": True, "violations": [], "repairable": True, "blocking": True},
      {"gate": qr.F9_BRAND_STYLE, "passed": True, "violations": [], "repairable": False, "blocking": False}]


def _piece(text: str, pid: str = "p1") -> dict:
    return {"piece_id": pid, "channel": "facebook", "status": "approved", "attempt_number": 1,
            "content_text": text, "gate_ledger": F9, "flags": [], "writer_input": WRITER_INPUT}


def test_context_from_writer_input():
    ctx = rp.context_from_writer_input({**WRITER_INPUT, "route_moments": [{"atom_id": "a1", "seed": "S"}]}, None)
    assert ctx["atom_text"] == fs.SEED and ctx["route_segments"] == [("a1", "S")]
    assert ctx["brand_audience"] == fs.PACK and ctx["trip_name"] == fs.TRIP and ctx["channel"] == "facebook"


def test_load_s234_skips_pieces_without_writer_input(tmp_path):
    path = tmp_path / "export.json.gz"
    data = {"pieces": [_piece(fs.CLEAN), {**_piece(fs.CLEAN, "p2"), "writer_input": None}]}
    path.write_bytes(gzip.compress(json.dumps(data).encode()))
    pieces, skipped = rp.load_s234(str(path))
    assert [p["id"] for p in pieces] == ["p1"] and skipped == ["p2"]


def test_stored_f9_and_f10():
    assert rp._stored_f9(F9) == F9 and rp._stored_f9(F9[:1]) is None
    f10 = {"gate": qr.F10_CANNIBALIZATION_CROSS_TENANT, "passed": False, "blocking": True, "repairable": True,
           "violations": ["content is 0.95 cosine-similar to a piece already published by a different tenant "
                          "(piece_id=00000000-0000-0000-0000-000000000001) — above"]}
    assert rp._stored_match([f10]) == {"piece_id": "00000000-0000-0000-0000-000000000001", "tenant_id": "other",
                                       "similarity": 0.95, "writer_missing_brand_rules": False}


def test_replay_one_on_the_real_gates(tmp_path):
    path = tmp_path / "export.json"
    path.write_text(json.dumps({"pieces": [_piece("Sod Camp is a hidden gem. " + fs.CLEAN), _piece(fs.CLEAN, "p2")]}))
    pieces, _ = rp.load_s234(str(path))
    held = rp.replay_one(pieces[0], True)
    assert held["status"] == "held" and held["held_gate"] == qr.F2_LEXICON
    assert held["first_failure"] == qr.F2_BANNED_PATTERNS and held["route"] == "rewrite"
    assert held["findings"] == {f"{qr.F2_LEXICON}|{qr.F2_BANNED_PATTERNS}|block": 1}
    clean = rp.replay_one(pieces[1], False)  # the full writer contexts run (F17 may fire), but no lexicon word
    assert clean["results"] == 21 and not clean["findings"] and clean["first_failure"] != qr.F2_BANNED_PATTERNS


def test_diff_maps_the_held_gate_and_reports_decision_changes():
    row = {"id": "p1", "source": "s234", "mode": "later", "passed": False, "status": "held",
           "held_gate": qr.F2_LEXICON, "first_failure": qr.F2_BANNED_PATTERNS, "hold_failure": qr.F2_BANNED_PATTERNS,
           "route": "rewrite", "style_only_repair": False, "blocking_failures": [qr.F2_BANNED_PATTERNS],
           "flags_legacy": {}}
    base = {"rows": [{**row, "held_gate": qr.F2_BANNED_PATTERNS}]}
    assert rp.diff(base, {"rows": [row]}) == []
    changed = {"rows": [{**row, "status": "approved", "passed": True, "held_gate": None}]}
    fields = {d["field"] for d in rp.diff(base, changed)}
    assert {"passed", "status", "held_gate"} <= fields
    text = rp.report(base, changed, golden_path=None, s234_path="x.json")
    assert "| s234 | later | 1 | 0 | 0 | 0 | 1 | 0 |" in text


def test_dash_fixed_mode_reaches_past_the_em_dash():
    # review round: a stored pre-AA-778 text holds on F2 em-dashes; the _dashfix mode replays it after fix_dashes
    text = "Sod Camp sits opposite a mountain range — a quiet first night. " + fs.CLEAN
    path_piece = {"id": "p1", "source": "s234", "channel": "facebook",
                  "context": rp.context_from_writer_input(WRITER_INPUT, "facebook"),
                  "output": {"content_text": text, "gate_ledger": F9, "flags": []}}
    raw = rp.replay_one(path_piece, True)
    fixed = rp.replay_one(path_piece, True, dash_fix=True)
    assert raw["first_failure"] == qr.F2_BANNED_PATTERNS
    assert fixed["first_failure"] != qr.F2_BANNED_PATTERNS
    assert set(rp.MODES) == {"attempt1", "later", "attempt1_dashfix", "later_dashfix"}


def test_diff_reports_what_the_sentence_repair_is_told():
    row = {"id": "p1", "source": "s234", "mode": "attempt1", "passed": False, "status": "held",
           "route": "repair", "plan_problems": ["F2_brand_forbidden | s | uses 'sitting'",
                                                "F15_voice | s | stock phrase 'worth sitting with'"]}
    head = {**row, "plan_problems": row["plan_problems"][:1]}
    [d] = rp.diff({"rows": [row]}, {"rows": [head]})
    assert d["field"] == "plan_problems" and d["base"] == row["plan_problems"][1:] and d["head"] == []
    assert "plan_problems" in rp.report({"rows": [row]}, {"rows": [head]}, golden_path="g", s234_path=None)
