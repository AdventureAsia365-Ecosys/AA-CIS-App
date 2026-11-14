"""AA-1048 (S280) — claim-instrument prompt v3 (services/ground_truth/prompts.py: the restatement test for paraphrase)
and the evaluation tools it needs (services/ground_truth/instrument_eval.py): an out-of-sample subset (--exclude), a
$0 replay of a finished run (score) and a plan priced from a measured run (--measured). Every model call is a fake;
every text, place and hash is invented."""
import json

import pytest

from services.ground_truth import instrument_eval as ev
from services.ground_truth import prompts as pr
from services.ground_truth import r1
from tests.unit.test_aa1048_instrument_v2 import LIM, _h, _item, _pool, _resp

GPT = ("gpt-6-luna-openai", "openai-direct")


def _inst(prompt):
    return r1.instruments([GPT], ["entailment"], prompt=prompt)[0]


# ── prompt versions ───────────────────────────────────────────────────────────────────────────────────────────────


def test_earlier_versions_keep_the_hash_their_records_name():
    # The S279 evaluation (run 3018008d) recorded aa1048-r1-v2@ddaab8c279ef in shared.llm_call_log; a change to the v1
    # or v2 texts would make those records point at a prompt nobody can rebuild.
    assert pr.get("v2").tag == "aa1048-r1-v2@ddaab8c279ef"
    assert pr.get("v1").tag == "aa1048-r1-v1@bf496005d329"
    assert pr.DEFAULT == "v1"


def test_v3_is_selectable_with_its_own_id_and_v2_user_prompts():
    v3, v2 = _inst("v3"), _inst("v2")
    assert v3.id == "gpt-6-luna-openai@openai-direct:entailment#v3"
    assert v3.prompt_version.tag.startswith("aa1048-r1-v3@")
    item = _item(1, "Reach the summit of Mount Selk.")
    assert r1.calls([item], v3)[0][0] == r1.calls([item], v2)[0][0]                           # same user prompt
    assert pr.get("v3").sha != pr.get("v2").sha
    assert pr.get("v3").system("entailment") == pr.ENTAILMENT_SYSTEM_V3
    assert pr.get("v3").system("extraction") == pr.EXTRACTION_SYSTEM_V3


def test_v3_carries_the_restatement_test_and_keeps_b3_b4_and_every_b5_pattern():
    for system in (pr.ENTAILMENT_SYSTEM_V3, pr.EXTRACTION_SYSTEM_V3):
        assert "three steps" in system and "Set aside wording that adds no checkable fact" in system          # P1
        assert "If you cannot name one, the sentence is supported." in system                              # P1
        assert "is NOT style: it is a fact" in system                                 # P1 keeps the promises
        assert "whole trip or the whole route" in system                                                   # P2
        assert "craft, food or custom" in system                                                           # P2b
        assert "A label made only of source words" in system and "A day heading" in system                 # P3
        assert pr._V2_RULES[2] in system and pr._V2_RULES[3] in system                         # B3, B4 unchanged
        for pattern in ("character of a meal", "qualities of the guide", "group or social experience",
                        "arrival or crowd atmosphere", "an experience at a stop", "contradicts the source"):
            assert pattern in system                                                      # every v2 B5 item
        assert "an order, sequence or schedule the source does not give" in system          # B5 + unstated order
        assert "bound to the wrong thing" in system and "named just BEFORE" in system       # B5 + misbinding
        assert "name the fact that the source does not give" in system
    assert "paraphrase" not in pr.ENTAILMENT_SYSTEM_V2.split("Still unsupported")[1]        # v2 untouched


def test_run_sends_the_v3_system_and_records_its_tag():
    seen = []
    r1.run([_item(1, "Reach the summit of Mount Selk.")], [_inst("v3")], cap=2.0, votes=1, run_id="zz",
           caller=lambda inst, system, user, schema: (seen.append(system),
                                                      _resp(json.dumps({"verdict": "supported", "quote": "",
                                                                        "why": ""})))[1],
           log=lambda resp, sig, parsed, err: seen.append(sig))
    assert seen[0] == pr.ENTAILMENT_SYSTEM_V3 and seen[1]["prompt_version"] == pr.get("v3").tag


# ── out-of-sample subset ──────────────────────────────────────────────────────────────────────────────────────────


def test_prefixes_from_a_report_or_a_list_and_their_guards():
    assert ev.prefixes_from(json.dumps({"votes": {_h(1)[:16]: [True], _h(2)[:16]: [False]}})) == {_h(1)[:16],
                                                                                                  _h(2)[:16]}
    assert ev.prefixes_from(json.dumps([_h(3)[:12].upper()])) == {_h(3)[:12]}
    for bad, why in (("[]", "non-empty"), ('{"votes": {}}', "non-empty"), ('["abc"]', ">= 8 hex"),
                     ('["zzzzzzzzzz"]', ">= 8 hex"), ("not json", "run report or a JSON list")):
        with pytest.raises(ev.EvalRefused, match=why):
            ev.prefixes_from(bad)


def test_exclude_drops_the_earlier_subset_but_keeps_the_relabels():
    pool = _pool()
    first = ev.select(pool, 50)
    prefixes = {p.item.claim_hash[:16] for p in first}
    kept, rep = ev.exclude(pool, prefixes)
    assert rep["by_group"] == {"fp_background": 4, "fp_paraphrase": 17, "fp_verbatim": 4, "tp": 20}
    assert rep["claims"] == 45 and rep["prefixes"] == 50
    assert {p.item.claim_hash for p in kept if p.group == "tp_relabel"} == {
        p.item.claim_hash for p in pool if p.group == "tp_relabel"}
    second = ev.select(kept, 50)
    overlap = {p.item.claim_hash for p in second} & {p.item.claim_hash for p in first}
    assert {p.group for p in second if p.item.claim_hash in overlap} == {"tp_relabel"}       # fresh otherwise
    assert ev.composition(second)["by_group"]["tp"] == 10                         # what is left of the 30 stored
    assert ev.exclude(pool, {"ffffffffffffffff"})[1]["claims"] == 0


# ── replay ($0) ───────────────────────────────────────────────────────────────────────────────────────────────────


def _report(subset, votes_for, **kw):
    votes = {p.item.claim_hash[:16]: votes_for(p) for p in subset}
    return {"mode": "live", "run_id": "0123456789ab", "instrument": "gpt-6-luna-openai@openai-direct:entailment#v2",
            "prompt_version": pr.get("v2").tag, "calls": 3 * len(subset), "spent_usd": 0.0003 * len(subset),
            "stopped": None, "votes": votes, **kw}


def test_replay_reproduces_a_stored_verdict_without_a_call():
    subset = ev.select(_pool(), 50)
    rep = _report(subset, lambda p: [p.group.startswith("tp")] * 3)
    rep["score"] = ev.score(subset, {p.item.claim_hash: rep["votes"][p.item.claim_hash[:16]] for p in subset}, LIM)
    out = ev.replay(_pool(), rep, LIM)
    assert out["matches_stored"] and out["score"]["verdict"] == "go" == out["stored_verdict"]
    assert out["subset"] == ev.composition(subset) and out["replayed_run"] == "01234567"
    with pytest.raises(ev.EvalRefused, match="no votes"):
        ev.replay(_pool(), {**rep, "votes": {}}, LIM)
    with pytest.raises(ev.EvalRefused, match="not in the pool"):
        ev.replay(_pool(), {**rep, "votes": {**rep["votes"], "ffffffffffffffff": [True]}}, LIM)


def test_score_command_replays_a_report(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ev, "limits", lambda: LIM)
    subset = ev.select(_pool(), 20)                              # 10 false positives, 5 relabels, 5 stored
    pool_file, report_file = tmp_path / "pool.jsonl", tmp_path / "report.json"
    pool_file.write_text("".join(json.dumps(x.line()) + "\n" for x in _pool()), encoding="utf-8")
    report_file.write_text(json.dumps(_report(subset, lambda p: [True] * 3)), encoding="utf-8")
    rc = ev.main(["score", "--pool", str(pool_file), "--report", str(report_file)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] and out["score"]["false_positives"]["dropped_share"] == 0.0
    assert out["score"]["verdict"] == "no_go" and out["stored_verdict"] is None and not out["matches_stored"]


# ── measured cost ─────────────────────────────────────────────────────────────────────────────────────────────────


def test_measured_expectation_scales_the_measured_cost_per_call():
    pool = _pool()
    old_subset = ev.select(pool, 10)
    rep = _report(old_subset, lambda p: [True] * 3)                         # 0.0003 USD per claim = 0.0001 per call
    fn, info = ev.measured_expectation(pool, rep, _inst("v3"))
    assert info["measured_usd_per_call"] == 0.0001 and info["calls"] == 30 and info["run"] == "01234567"
    v2 = _inst("v2")
    old_users = [u for u, _ in r1.calls([p.item for p in old_subset], v2)]
    mean_old = sum(r1.call_cost(v2, u, 0) for u in old_users) / len(old_users)
    user = old_users[0]
    assert fn(_inst("v3"), user) == pytest.approx(0.0001 * r1.call_cost(_inst("v3"), user, 0) / mean_old)
    assert fn(_inst("v3"), user) > fn(v2, user)                          # v3's longer system text costs more


@pytest.mark.parametrize("change, why", [
    ({"mode": "dry_run"}, "finished live run"),
    ({"stopped": "cap 0.1 USD"}, "finished live run"),
    ({"calls": 0}, "finished live run"),
    ({"spent_usd": 0}, "positive spent_usd"),
    ({"instrument": "llama4-maverick@acc2:entailment#v2"}, "measured 'llama4"),
    ({"prompt_version": "aa1048-r1-v2@abcdefabcdef"}, "texts changed"),
    ({"prompt_version": "aa1048-r1-v9@ddaab8c279ef"}, "unknown here"),
    ({"votes": {"ffffffffffffffff": [True]}}, "not in the pool"),
])
def test_measured_expectation_refuses_a_report_it_cannot_trust(change, why):
    pool = _pool()
    rep = {**_report(ev.select(pool, 10), lambda p: [True] * 3), **change}
    with pytest.raises(ev.EvalRefused, match=why):
        ev.measured_expectation(pool, rep, _inst("v3"))


def test_plan_with_a_measured_expectation_and_the_worst_call():
    items = [p.item for p in ev.select(_pool(), 10)]
    model = r1.plan(items, [_inst("v3")], cap=2.0)
    measured = r1.plan(items, [_inst("v3")], cap=2.0, expected_of=lambda inst, user: 0.0001)
    assert model["instruments"][0]["expected_from"] == "model"
    assert measured["instruments"][0]["expected_from"] == "measured"
    assert measured["expected_total_usd"] == pytest.approx(0.0001 * 30, abs=1e-4)
    assert measured["worst_case_total_usd"] == model["worst_case_total_usd"]          # the gate is unchanged
    row = model["instruments"][0]
    assert 0 < row["worst_call_usd_max"] <= row["worst_case_usd"]


def test_cli_dry_run_with_exclude_and_measured(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ev, "limits", lambda: LIM)
    monkeypatch.setattr(ev, "_caller", lambda *a: pytest.fail("no call may be made"))
    from services.golden_set import llm_guard

    monkeypatch.setattr(llm_guard, "install", lambda tag: None)
    pool = _pool()
    first = ev.select(pool, 50)
    pool_file, report_file = tmp_path / "pool.jsonl", tmp_path / "report.json"
    pool_file.write_text("".join(json.dumps(x.line()) + "\n" for x in pool), encoding="utf-8")
    report_file.write_text(json.dumps(_report(first, lambda p: [True] * 3)), encoding="utf-8")
    args = ["run", "--pool", str(pool_file), "--prompt", "v3", "--limit", "50", "--exclude", str(report_file)]
    rc = ev.main(args + ["--cap", "0.1"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 3 and out["refused"] == "the plan does not fit the cap"            # the model estimate refuses
    rc = ev.main(args + ["--cap", "0.1", "--measured", str(report_file)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ok"] and out["instrument"].endswith("#v3")
    assert out["excluded"]["claims"] == 45 and out["measured"]["run"] == "01234567"
    assert out["plan"]["instruments"][0]["expected_from"] == "measured" and out["plan"]["fits_cap"]
    assert out["subset"]["by_group"]["tp_relabel"] == 5
