"""AA-1051 S285 — the W5 null gate carries a non_inferior null run pinned to an EARLIER version of the same golden set
to a proof on a later version when the judge is the same and every GATE item of the null run's writer is
byte-identical in the later version (services/writer_proof/null_carry.py); `same_instrument` stays strict. Faked
golden reads and reports: no DB, no model call. Invented ids and text only."""
from __future__ import annotations

import asyncio
import hashlib

import pytest

from services.writer_proof import null_carry as nc
from services.writer_proof import prereg as pr
from services.writer_proof import runner
from tests.unit.writer_proof import test_aa1051_runner as base
from tests.unit.writer_proof.test_aa1051_runner import _Pool, _r1_report

fakes = base.fakes

NULL_ID = "w5-null-t9-blog-v7-g150"         # registered: aa729@1.5.0, sample rule n 12, the v7 claim instrument
GATE_IDS = [f"zzb-{i:02d}" for i in range(1, 13)]


def _item(sid, writer="t9_blog", side="gate", **over):
    row = {c: None for c in nc.ITEM_COLUMNS + nc.SPLIT_COLUMNS}
    row.update(sample_id=sid, writer=writer, channel="blog" if writer == "t9_blog" else None, scope="tenant",
               tenant_id="00000000-0000-4000-8000-0000000000aa", split="tune", trap=False, kind="real",
               tiers=["culinary"], input='{"brief": "A quiet lake walk"}', control_output='{"body": "We walk."}',
               post_edit=False, label='{"verdict": "pass"}', label_verdict="pass", label_source="agent_panel",
               refs="{}", input_sha256=hashlib.sha256(sid.encode()).hexdigest(), eval_split=side,
               split_rule="aa1051-split-v1", split_bucket="0.400000")
    row.update(over)
    return row


def _version(version, items, shared='{"brand_rules": {}}'):
    return {"version": version, "shared_inputs": shared, "items": items}


def _old():
    return _version("1.5.0", [_item(s) for s in GATE_IDS] + [_item("zzb-dev-1", side="dev"),
                                                               _item("zzb-cal-1", side="cal")])


def _new(items=None):
    """1.6.0 = 1.5.0's t9_blog items unchanged (the reader returns one writer's items; S1 samples were added)."""
    return _version("1.6.0", _old()["items"] if items is None else items)


def _null() -> pr.Prereg:
    return pr.load(NULL_ID)


def _proof(version="1.6.0", **over) -> pr.Prereg:
    d = {**pr.load("w5-s1").doc, "golden_set": {"name": "aa729", "version": version}}
    d.update(over)
    return pr.from_doc(d)


def _gate(monkeypatch, verdicts, pairs):
    """null_gate with faked reports ({prereg id: verdict}) and golden reads (a list of (old, new) the loader pops)."""
    calls = []

    async def report(pool, n):
        return {"ok": True, "decision": {"verdict": verdicts[n.id]}}

    async def load(null, pre):
        calls.append((nc.golden_of(null)[1], nc.golden_of(pre)[1]))
        return pairs.pop(0)

    monkeypatch.setattr(runner, "report", report)
    return calls, load


def _run(nulls, pre, load):
    return asyncio.run(runner.null_gate(_Pool(), pre, nulls=nulls, golden_items=load))


# ── the gate ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_identical_gate_items_carry_the_null_run_to_the_later_version(monkeypatch):
    calls, load = _gate(monkeypatch, {NULL_ID: "non_inferior"}, [(_old(), _new())])
    out = _run([_null()], _proof(), load)
    assert out["passed_by"] == NULL_ID and out["rule"] == nc.CARRY_RULE and "blocking" not in out
    assert out["golden_carried_from"] == "1.5.0" and out["items_compared"] == 12 and out["sample_items"] == 12
    assert calls == [("1.5.0", "1.6.0")]
    assert out["checked"][0]["carry"]["ok"] and out["checked"][0]["rule"] == nc.CARRY_RULE


@pytest.mark.parametrize("change,needle", [
    ({"label_verdict": "fail"}, "zzb-03 differs in label_verdict"),
    ({"label": '{"verdict": "fail"}'}, "zzb-03 differs in label"),
    ({"control_output": '{"body": "We walk far."}'}, "zzb-03 differs in control_output"),
    ({"input_sha256": "f" * 64}, "zzb-03 differs in input_sha256"),
    ({"split_bucket": "0.410000"}, "zzb-03 differs in split_bucket"),
    ({"eval_split": "dev"}, "zzb-03 moved to dev in 1.6.0"),
    ({"eval_split": None}, "have no stored eval split"),
])
def test_one_changed_item_blocks_and_names_what_differs(monkeypatch, change, needle):
    items = [_item(s, **(change if s == "zzb-03" else {})) for s in GATE_IDS]
    _, load = _gate(monkeypatch, {NULL_ID: "non_inferior"}, [(_old(), _new(items))])
    out = _run([_null()], _proof(), load)
    assert out["passed_by"] is None and needle in out["blocking"]
    assert f"registered null run {NULL_ID} (aa729@1.5.0) is not carried to aa729@1.6.0" in out["blocking"]
    assert out["checked"][0]["carry"]["ok"] is False


def test_a_removed_or_added_gate_item_blocks(monkeypatch):
    removed = [_item(s) for s in GATE_IDS if s != "zzb-07"]
    added = [_item(s) for s in GATE_IDS] + [_item("zzb-13")]
    _, load = _gate(monkeypatch, {NULL_ID: "non_inferior"}, [(_old(), _new(removed)), (_old(), _new(added))])
    out = _run([_null()], _proof(), load)
    assert out["passed_by"] is None and "t9_blog GATE item zzb-07 is missing from 1.6.0" in out["blocking"]
    out = _run([_null()], _proof(), load)
    assert out["passed_by"] is None and "t9_blog GATE item zzb-13 was added in 1.6.0" in out["blocking"]


def test_a_version_that_is_not_stored_blocks(monkeypatch):
    _, load = _gate(monkeypatch, {NULL_ID: "non_inferior"}, [(_old(), None)])
    out = _run([_null()], _proof(), load)
    assert out["passed_by"] is None and "golden aa729@1.6.0 is not stored" in out["blocking"]


def test_another_judge_is_never_carried(monkeypatch):
    calls, load = _gate(monkeypatch, {NULL_ID: "non_inferior"}, [])
    pre = _proof(judge={"model_key": "zz-other-model", "connection": "zz-conn"})
    out = _run([_null()], pre, load)
    assert out["passed_by"] is None and "run the null run first" in out["blocking"]
    assert calls == [] and out["checked"] == []                   # no report, no golden read for another judge


def test_a_null_run_of_a_later_version_than_the_proof_is_not_carried_back(monkeypatch):
    calls, load = _gate(monkeypatch, {NULL_ID: "non_inferior"}, [])
    out = _run([_null()], _proof(version="1.4.0"), load)
    assert out["passed_by"] is None and calls == []
    assert "golden version 1.5.0 is later than the proof's 1.4.0: a null run carries forward only" in out["blocking"]


def test_the_same_version_keeps_the_strict_rule_and_reads_no_items(monkeypatch):
    calls, load = _gate(monkeypatch, {NULL_ID: "non_inferior"}, [])
    out = _run([_null()], _proof(version="1.5.0"), load)
    assert out["passed_by"] == NULL_ID and out["rule"] == nc.SAME_RULE and calls == []
    assert "golden_carried_from" not in out
    _gate(monkeypatch, {NULL_ID: "inferior"}, [])
    out = _run([_null()], _proof(version="1.5.0"), load)
    assert out["passed_by"] is None and calls == []               # an inferior same-version run is not carried


def test_an_inferior_null_run_of_an_earlier_version_is_not_carried(monkeypatch):
    calls, load = _gate(monkeypatch, {NULL_ID: "inferior"}, [(_old(), _new())])
    out = _run([_null()], _proof(), load)
    assert out["passed_by"] is None and calls == [] and out["checked"][0]["verdict"] == "inferior"


def test_the_plan_reports_the_carried_null_run(fakes, tmp_path, monkeypatch):
    fakes["null_gate"] = "real"
    fakes["version"] = "1.6.0"
    _, load = _gate(monkeypatch, {NULL_ID: "non_inferior"}, [(_old(), _new())])
    monkeypatch.setattr(pr, "registered_files", lambda workflow=None: [_null()])
    monkeypatch.setattr(pr, "unloadable_registered", lambda workflow=None: [])

    async def load_pair(pool, null, pre):
        return await load(null, pre)
    monkeypatch.setattr(nc, "load_pair", load_pair)
    out = asyncio.run(runner.plan(_Pool(), _proof(), r1_report=_r1_report(tmp_path)))
    assert out["null_run"]["passed_by"] == NULL_ID and out["null_run"]["golden_carried_from"] == "1.5.0"
    assert not any("null run first" in b for b in out["blocking"])


# ── null_carry (pure) ────────────────────────────────────────────────────────────────────────────────────────────

def test_same_instrument_stays_strict_across_golden_versions():
    assert not runner.same_instrument(_null(), _proof())
    assert nc.carry_problem(_null(), _proof()) is None


def test_semver_orders_numerically():
    assert nc.semver("1.10.0") > nc.semver("1.9.9") and nc.semver("1.6") is None and nc.semver(None) is None


def test_the_shared_inputs_count_for_a_writer_whose_judge_source_reads_them():
    from services.writing_memory import eval_writers as ew
    assert tuple(nc.NO_SHARED_INPUT_WRITERS) == tuple(ew.T9_WRITERS)
    a1 = _proof(version="1.5.0")                                   # an a1 pre-registration as the measured run
    old = _version("1.5.0", [_item(s, writer="a1") for s in a1.doc["sample_ids"]],
                   shared='{"brand_rules": {"s1:a": 1}}')
    same = _version("1.6.0", old["items"], shared=old["shared_inputs"])
    other = _version("1.6.0", old["items"], shared='{"brand_rules": {"s1:a": 2}}')
    assert nc.null_covers(a1, _proof(), old, same)["ok"]
    got = nc.null_covers(a1, _proof(), old, other)
    assert not got["ok"] and any("shared inputs" in d for d in got["differs"])
    t9_other = _version("1.6.0", _old()["items"], shared='{"brand_rules": {"s1:z": 9}}')
    assert nc.null_covers(_null(), _proof(), _old(), t9_other)["ok"]     # the T9 judge source does not read them


def test_a_sample_item_outside_the_gate_side_refuses():
    listed = pr.from_doc({**{k: v for k, v in _null().doc.items() if k != "sample"},
                          "sample_ids": GATE_IDS[:11] + ["zzb-dev-1"]})
    got = nc.null_covers(listed, _proof(), _old(), _new())
    assert not got["ok"] and any("not t9_blog GATE items of 1.5.0" in d for d in got["differs"])


def test_the_blocking_line_lists_at_most_a_few_differences():
    cover = {"golden_carried_from": "1.5.0", "golden_carried_to": "1.6.0",
             "differs": [f"zz difference {i}" for i in range(nc.MAX_LISTED + 3)]}
    line = nc.blocking_line(_null(), _proof(), cover)
    assert "(+3 more)" in line and f"zz difference {nc.MAX_LISTED}" not in line
