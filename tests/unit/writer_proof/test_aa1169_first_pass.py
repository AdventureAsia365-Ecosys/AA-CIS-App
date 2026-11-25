"""AA-1169 — the first-pass diagnosis of a W5 proof (services/writer_proof/first_pass.py): the claim classes, where a
flagged claim sits in the output, the per-arm counts, the projection of a fix, the gate replay summary and the runner's
`first-pass` command. No DB, no model call. Invented places, brands and sentences only."""
from __future__ import annotations

import asyncio
import json

import pytest

from services.writer_proof import first_pass as fp
from services.writer_proof import prereg as pr
from services.writer_proof import runner

SAMPLES = ["zz-fp-01", "zz-fp-02", "zz-fp-03", "zz-fp-04"]


@pytest.mark.parametrize("text, cls", [
    ("The lake holds 70 percent of the valley's water.", "number"),
    ("Day 3 reaches the Orvine ridge camp.", "route_timing"),           # a day reference is not a figure
    ("Day three begins at the foot of Mount Corran.", "route_timing"),
    ("It is one of the last herding villages of its kind.", "rarity"),
    ("The monastery is genuinely rare in the region.", "rarity"),
    ("The morning ride suits younger travellers and has no pressure.", "suitability"),
    ("By evening, the group reaches the Talvi guesthouse.", "route_timing"),
    ("The journey north from the plain moves through forest.", "route_timing"),
    ("The sky takes on a different quality above the ridge.", "scene"),
    ("Our travel designers shape each journey with care.", "operator"),
    ("The ruins speak to a memory the valley still keeps.", "reflection"),
    ("Corran Monastery has a library hall.", "other"),
])
def test_claim_class(text, cls):
    assert fp.claim_class(text) == cls


def test_claim_class_order_first_match_wins():
    assert fp.claim_class("One of the most striking views, 40 metres up.") == "number"
    assert fp.claim_class("") == "other"


OUTPUT = {
    "seo_title": "Orvine Ridge Walk: A Guide to Corran Monastery",
    "meta_description": "Walk up to Corran Monastery above Orvine and see its library hall on a guided day.",
    "slug": "orvine-ridge-corran-monastery",
    "content_text": ("## Corran Monastery\n\nCorran Monastery stands above Orvine. It is one of the last working "
                     "libraries of the valley.\n\nBy evening, the group reaches the Talvi guesthouse.\n\n## FAQ\n\n"
                     "**Q: What is Corran Monastery?**\nA: Corran Monastery is a hilltop library hall above Orvine."),
}


def test_locate_fields_and_reading_order():
    seo = fp.locate("Walk up to Corran Monastery above Orvine and see its library hall on a guided day.", OUTPUT)
    body = fp.locate("It is one of the last working libraries of the valley.", OUTPUT)
    later = fp.locate("By evening, the group reaches the Talvi guesthouse.", OUTPUT)
    faq = fp.locate("A: Corran Monastery is a hilltop library hall above Orvine.", OUTPUT)
    assert [seo[0], body[0], later[0], faq[0]] == ["seo", "body", "body", "faq"]
    assert seo[1] < body[1] < later[1] < faq[1]
    assert fp.locate("A sentence that is not in the output.", OUTPUT) == ("body", 10 ** 9)
    assert fp.locate("Anything at all here.", None)[0] == "body"


def _row(sid, arm, claims, output=None):
    det = {"judge_claims": [{"class": "claim.unsupported", "claim": c, "why": "not in the source"} for c in claims]}
    return {"sample_id": sid, "arm": "candidate" if arm == "B" else "control", "mode": "regenerate",
            "verdict": "fail" if claims else "pass", "details": det, "output": output or OUTPUT}


RARITY = "It is one of the last working libraries of the valley."
TIMING = "By evening, the group reaches the Talvi guesthouse."
NUMBER = "The hall keeps 400 books."

ROWS = [
    _row("zz-fp-01", "A", [RARITY, TIMING]), _row("zz-fp-01", "B", [TIMING, RARITY]),
    _row("zz-fp-02", "A", [NUMBER]), _row("zz-fp-02", "B", [RARITY]),
    _row("zz-fp-03", "A", [RARITY]), _row("zz-fp-03", "B", [NUMBER, TIMING]),
    _row("zz-fp-04", "A", []), _row("zz-fp-04", "B", []),
    {"sample_id": "zz-fp-05", "arm": "candidate", "mode": "regenerate", "verdict": "error", "details": {}},
]


def test_item_record_orders_flags_by_reading_position():
    rec = fp.item_record(ROWS[1])
    assert rec["arm"] == "B" and rec["first"] == "rarity"            # the rarity sentence comes first in the body
    assert [f["class"] for f in rec["flags"]] == ["rarity", "route_timing"]


def test_diagnose_counts_per_arm():
    d = fp.diagnose(ROWS)
    b = d["B"]
    assert b["items"] == 4 and b["passes"] == 1 and b["first_pass_yield"] == 0.25 and b["flags"] == 5
    assert b["first_class"] == {"rarity": 2, "route_timing": 1}        # NUMBER is not in the output: read last
    assert b["items_with_class"] == {"number": 1, "rarity": 2, "route_timing": 2}
    assert b["items_failed_by_class_alone"] == {"rarity": 1}
    assert b["flags_by_field"] == {"body": 5}
    assert d["A"]["items"] == 4 and d["A"]["passes"] == 1
    assert list(b["first_class"]) == ["rarity", "route_timing"]       # CLASS_ORDER, not insertion order


def _doc():
    return {"sample_ids": SAMPLES, "metrics": [{"name": "fail_claims_per_item", "direction": "lower"}],
            "decision": {"rule": "non_inferior", "metric": "fail_claims_per_item", "margin_abs": 4.0, "alpha": 0.05,
                         "noise_se_k": 2.0}}


def test_project_removes_the_fixed_classes_from_arm_b_only():
    p = fp.project(_doc(), ROWS, ["rarity", "route_timing"])
    assert p["fixed"] == ["rarity", "route_timing"]
    assert p["first_pass_yield"] == 0.75 and p["fail_claims_per_item"] == 0.25
    assert p["decision"]["verdict"] == "non_inferior" and p["decision"]["mean_a"] == 1.0
    assert fp.project(_doc(), ROWS, [])["first_pass_yield"] == 0.25
    with pytest.raises(ValueError, match="unknown classes"):
        fp.project(_doc(), ROWS, ["typo"])


def _b_fail(claims, *, hard_stop=False, codes=()):
    row = _row("zz-fp-09", "B", claims)
    return {**row, "verdict": "fail", "hard_stop": hard_stop, "failure_codes": list(codes)}


def test_without_turns_only_a_claims_only_fail_into_a_pass():
    fixed = frozenset({"rarity", "route_timing"})
    assert fp._without(_b_fail([RARITY, TIMING]), fixed)["verdict"] == "pass"
    assert fp._without(_b_fail([RARITY, NUMBER]), fixed)["verdict"] == "fail"           # a number claim is left
    # a fail that does not come from claims alone stays a fail whatever the claims become
    assert fp._without(_b_fail([], hard_stop=True), fixed)["verdict"] == "fail"          # hard stop, no claim
    assert fp._without(_b_fail([RARITY], hard_stop=True), fixed)["verdict"] == "fail"    # hard stop, claims all fixed
    assert fp._without(_b_fail([]), fixed)["verdict"] == "fail"                          # judge fail, no claim
    assert fp._without(_b_fail([RARITY], codes=["qa.artefact"]), fixed)["verdict"] == "fail"
    assert fp._without(_b_fail([RARITY], codes=["claim.unsupported"]), fixed)["verdict"] == "pass"
    # a pass stays a pass, also when it quotes claims of a class that is not fixed
    passed = {**_row("zz-fp-10", "B", [NUMBER]), "verdict": "pass"}
    out = fp._without(passed, fixed)
    assert out["verdict"] == "pass" and len(out["details"]["judge_claims"]) == 1


def test_project_keeps_a_hard_stop_fail():
    rows = [r for r in ROWS if r["sample_id"] != "zz-fp-02"]
    rows += [_row("zz-fp-02", "A", [NUMBER]), {**_b_fail([RARITY], hard_stop=True), "sample_id": "zz-fp-02"}]
    p = fp.project(_doc(), rows, ["rarity", "route_timing"])
    assert p["first_pass_yield"] == 0.5                         # 01, 04 pass; 02 (hard stop) and 03 (number) fail


def test_rows_sql_reads_the_hard_stop_and_failure_codes():
    assert "r.hard_stop" in fp.ROWS_SQL and "r.failure_codes" in fp.ROWS_SQL


def test_gate_summary_counts_and_survives_a_bad_input(monkeypatch):
    calls = iter([{"passed": False, "first_failure": "F4_extreme_length", "blocking": ["F4_extreme_length"]},
                  {"passed": True, "first_failure": None, "blocking": []}])

    def fake(ctx, output):
        if not ctx:
            raise KeyError("atom_text")
        return next(calls)

    monkeypatch.setattr(fp, "gate_replay", fake)
    s = fp.gate_summary([({"a": 1}, {}), ({"a": 1}, {}), ({}, {})])
    assert s == {"drafts": 3, "passed": 1, "errors": 1, "first_failure": {"F4_extreme_length": 1, "none": 1},
                 "any_blocking": {"F4_extreme_length": 1}}


def test_run_without_gates_and_with_projection():
    out = fp.run(_doc(), ROWS, fixed=["rarity"], gates=False)
    assert out["classes"] == list(fp.CLASS_ORDER) and "gate_replay" not in out
    assert out["projection"]["first_pass_yield"] == 0.5


def test_run_replays_regenerate_rows_per_arm(monkeypatch):
    seen = []
    monkeypatch.setattr(fp, "gate_summary", lambda pairs: seen.append(len(list(pairs))) or {"drafts": len(seen)})
    out = fp.run(_doc(), [{**r, "input": {"context": {"channel": "blog"}}} for r in ROWS], gates=True)
    assert set(out["gate_replay"]) == {"A", "B"} and seen == [4, 4]           # the error row kept no output


# ── the runner command ─────────────────────────────────────────────────────────────────────────────────────────────

def test_parse_first_pass():
    a = runner._parse(["first-pass", "--prereg", "w5-t9", "--fixed", "rarity, route_timing", "--no-gates"])
    assert a.cmd == "first-pass" and a.fixed == "rarity, route_timing" and a.no_gates


def _pre() -> pr.Prereg:
    return pr.from_doc(json.loads(pr.path_for("w5-t9").read_text()))


def test_first_pass_report_refuses_an_unknown_class():
    with pytest.raises(runner.RunRefused, match="unknown --fixed"):
        asyncio.run(runner.first_pass_report(None, _pre(), fixed=("typo",)))


def test_first_pass_report_before_a_b_run(monkeypatch):
    async def load(pool, prereg_id):
        return None, None, []
    monkeypatch.setattr(fp, "load", load)
    out = asyncio.run(runner.first_pass_report(None, _pre()))
    assert out == {"prereg": "w5-t9", "ok": False, "refused": "no B run with results yet"}


def test_first_pass_report_counts(monkeypatch):
    async def load(pool, prereg_id):
        return "run-b", "set-1", ROWS

    async def resolved(pool, pre, *, set_id=None):
        assert set_id == "set-1"
        return pre
    monkeypatch.setattr(fp, "load", load)
    monkeypatch.setattr(runner, "resolved", resolved)
    out = asyncio.run(runner.first_pass_report(None, _pre(), fixed=("rarity",), gates=False))
    assert out["ok"] and out["run_b"] == "run-b" and out["rows"] == len(ROWS)
    assert out["diagnosis"]["B"]["first_pass_yield"] == 0.25 and out["projection"]["first_pass_yield"] == 0.5
    assert out["advisory"]                                            # the pinned v7 instrument is advisory


# ── the re-measure draft ───────────────────────────────────────────────────────────────────────────────────────────

def test_the_re_measure_mirrors_w5_t9():
    """w5-t9-r2 (registered S292 after the tech lead's yes) measures the same thing as w5-t9 on the active golden
    version, and the lock pins it."""
    base = json.loads(pr.path_for("w5-t9").read_text())
    r2 = json.loads(pr.path_for("w5-t9-r2").read_text())
    for key in ("workflow", "eval_split", "sample_ids", "noise_sample_ids", "release_id", "judge", "metrics",
                "decision", "requirements", "power"):
        assert r2[key] == base[key], key
    assert r2["status"] == "registered" and r2["golden_set"] == {"name": "aa729", "version": "1.7.0"}
    assert r2["registered_by"] == "tech_lead" and r2["registered_on"] == "2026-11-25"
    assert 0 < r2["cap_usd"] <= 5.0
    assert pr.registration_problems(r2) == []
    assert pr.read_lock()["w5-t9-r2"] == pr.sha256_of(r2)
