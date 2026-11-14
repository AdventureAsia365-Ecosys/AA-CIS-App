"""AA-1048 (S280) — claim-instrument prompt v4 (services/ground_truth/prompts.py: v3's recall losses and remaining
false fires) and what its evaluation needs (services/ground_truth/instrument_eval.py): a live run refuses unless it
leaves out every run the version was shaped on, and --with-reasons keeps each reply's `why` for a $0 reading of the
misses. Every model call is a fake; every text, place and hash is invented."""
import json

import pytest

from services.ground_truth import instrument_eval as ev
from services.ground_truth import prompts as pr
from services.ground_truth import r1
from tests.unit.test_aa1048_instrument_v2 import LIM, _item, _pool, _resp

GPT = ("gpt-6-luna-openai", "openai-direct")
V2_RUN, V3_RUN = "3018008d", "33835b87"


def _inst(prompt):
    return r1.instruments([GPT], ["entailment"], prompt=prompt)[0]


# ── prompt versions ───────────────────────────────────────────────────────────────────────────────────────────────


def test_earlier_versions_keep_the_hash_their_records_name():
    # S279 (run 3018008d) recorded v2, S280 (run 33835b87) recorded v3 in shared.llm_call_log: their texts are frozen.
    assert pr.get("v1").tag == "aa1048-r1-v1@bf496005d329"
    assert pr.get("v2").tag == "aa1048-r1-v2@ddaab8c279ef"
    assert pr.get("v3").tag == "aa1048-r1-v3@7e4923a09780"
    # v4's evaluation run records this tag: an edit to its texts must come with a new version, not a silent new hash
    assert pr.get("v4").tag == "aa1048-r1-v4@5cd34821af82"
    assert pr.DEFAULT == "v1" and ev.DEFAULT_PROMPT == "v2"


def test_v4_is_selectable_with_its_own_id_tag_and_v3_user_prompts():
    v4, v3 = _inst("v4"), _inst("v3")
    assert v4.id == "gpt-6-luna-openai@openai-direct:entailment#v4"
    assert v4.prompt_version.tag == f"aa1048-r1-v4@{pr.get('v4').sha}"
    assert len({pr.get(v).sha for v in ("v1", "v2", "v3", "v4")}) == 4
    item = _item(1, "Reach the summit of Mount Selk.")
    assert r1.calls([item], v4)[0][0] == r1.calls([item], v3)[0][0]                           # same user prompt
    assert pr.get("v4").templates == pr.get("v3").templates
    assert pr.get("v4").system("entailment") == pr.ENTAILMENT_SYSTEM_V4
    assert pr.get("v4").system("extraction") == pr.EXTRACTION_SYSTEM_V4


def test_v4_keeps_the_restatement_test_and_v3s_unchanged_rules():
    for system in (pr.ENTAILMENT_SYSTEM_V4, pr.EXTRACTION_SYSTEM_V4):
        assert "three steps" in system and "Set aside wording that adds no checkable fact" in system          # P1
        assert "If you cannot name one, the sentence is supported." in system
        for v3_example in ("paraphrase with emphasis", "(point of view)", "the writer's reading; the fort",
                           "no option added", "a framing verb; no sighting promised"):
            assert v3_example in system                                                   # v3's P1 examples stay
        for kept in (pr._V3_RULES[1], pr._V3_RULES[3], pr._V3_RULES[4]):                 # P2, B3, B4 byte for byte
            assert kept in system
        assert "A label made only of source words" in system and "the Karsk Royal Lodge" in system         # P3
        for pattern in ("character of a meal", "qualities of the guide", "group or social experience",
                        "arrival or crowd atmosphere", "an experience at a stop",
                        "an order, sequence or schedule the source does not give", "bound to the wrong thing",
                        "named just BEFORE", "contradicts the source"):
            assert pattern in system                                                      # every v3 B5 item
        assert "name the fact that the source does not give" in system


def test_v4_adds_the_rules_drawn_from_v3s_misses():
    for system in (pr.ENTAILMENT_SYSTEM_V4, pr.EXTRACTION_SYSTEM_V4):
        # recall: pace and suitability are facts (two lost persona promises), and a restatement may add no specific
        assert "crowds, pace, effort, comfort, safety or who it suits" in system and "is NOT style" in system
        assert "pace, effort, comfort, safety or who the trip suits" in system                         # B5 item
        assert "do not\nby themselves" not in system and "do not by themselves make a trip slow or easy" in system
        assert "A restatement must not add a new specific about this trip: a number, name, time, service" in system
        assert "is not such a specific" in system                       # the place rule is the guard's exception
        assert "Joining two source facts into one new specific adds one" in system
        assert "firming one side of an 'or'" in system
        # false fires: a purpose, the day's order, lasting features, sentences, text about the text
        assert "a purpose given to a fact the source states" in system
        assert "the afternoon follows from the source's order" in system and "a clock time would be new" in system
        assert "the last thing the source gives for a day ends that day" in system
        assert "visited place's own lasting features" in system and "a sight beyond the place" in system
        assert "access the source does not give" in system                                # still a promise
        assert "A day heading or a sentence that names where the trip or a day begins" in system           # P3
        assert "A sentence about the text itself" in system and "still unsupported" in system              # P4
    assert pr.ENTAILMENT_SYSTEM_V4.count("\n- ") == len(pr._V4_RULES) == 8


def test_v4_numbers_follow_one_rule_for_place_background():
    # P2b lets a visited place's size and age stand as background; B5 must not call the same number unsupported.
    # Only a number about the trip, or one the source gives for another place, stays a promise.
    for system in (pr.ENTAILMENT_SYSTEM_V4, pr.EXTRACTION_SYSTEM_V4):
        assert "the size of what stands there" in system and "age, builder" in system                    # P2b
        assert "for another place, or not at all" not in system
        assert "a size, height, date or count the source gives for another place is unsupported" in system
        assert "a number about this trip (a distance, duration, time, price or group size)" in system
        assert "age or date that describes a place the source names is background" in system
    assert "for another place, or not at all" in pr.ENTAILMENT_SYSTEM_V3                       # v3 stays frozen


def test_v4_examples_use_invented_places_only():
    # the golden-set text guard: v4's own example names are invented (the pool check itself runs in .tmp-session)
    for name in ("Velm", "Tarro", "Karsk", "Ostra", "Brellin"):
        assert name in pr.ENTAILMENT_SYSTEM_V4
    for real in ("Jaipur", "Lahore", "Kathmandu", "Colombo", "Agra", "Sigiriya"):
        assert real not in pr.ENTAILMENT_SYSTEM_V4


def test_run_sends_the_v4_system_and_records_its_tag():
    seen = []
    r1.run([_item(1, "Reach the summit of Mount Selk.")], [_inst("v4")], cap=2.0, votes=1, run_id="zz",
           caller=lambda inst, system, user, schema: (seen.append(system),
                                                      _resp(json.dumps({"verdict": "supported", "quote": "",
                                                                        "why": ""})))[1],
           log=lambda resp, sig, parsed, err: seen.append(sig))
    assert seen[0] == pr.ENTAILMENT_SYSTEM_V4 and seen[1]["prompt_version"] == pr.get("v4").tag


# ── out of sample: the runs a version was shaped on ───────────────────────────────────────────────────────────────


def test_versions_name_the_runs_that_shaped_them():
    assert pr.get("v1").shaped_on == () == pr.get("v2").shaped_on
    assert pr.get("v3").shaped_on == (V2_RUN,)
    assert pr.get("v4").shaped_on == (V2_RUN, V3_RUN)


def test_run_of_reads_a_report_run_id_and_ignores_a_prefix_list():
    assert ev.run_of(json.dumps({"run_id": "33835B87-964b", "votes": {"a" * 16: [True]}})) == V3_RUN
    assert ev.run_of(json.dumps(["a" * 16])) is None
    assert ev.run_of(json.dumps({"run_id": "short"})) is None
    assert ev.run_of("not json") is None


def test_out_of_sample_lists_the_missing_runs():
    assert ev.out_of_sample(pr.get("v4"), [V2_RUN]) == {"shaped_on": [V2_RUN, V3_RUN], "excluded_runs": [V2_RUN],
                                                        "missing": [V3_RUN]}
    assert ev.out_of_sample(pr.get("v4"), [V3_RUN, V2_RUN, "ffffffff"])["missing"] == []
    assert ev.out_of_sample(pr.get("v2"), [])["missing"] == []


def _report(subset, run_id):
    return {"mode": "live", "run_id": run_id, "instrument": "gpt-6-luna-openai@openai-direct:entailment#v3",
            "prompt_version": pr.get("v3").tag, "calls": 3 * len(subset), "spent_usd": 0.0003 * len(subset),
            "stopped": None, "votes": {p.item.claim_hash[:16]: [True] * 3 for p in subset}}


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "limits", lambda: LIM)
    monkeypatch.setattr(ev, "refresh_settings", lambda: True)
    monkeypatch.setattr(ev, "_probe", lambda since: (lambda: None))
    from services.golden_set import llm_guard
    from shared.llm_client import call_log

    monkeypatch.setattr(llm_guard, "install", lambda tag: None)
    monkeypatch.setattr(call_log, "record_call_sync", lambda **kw: None)
    pool = _pool()
    first = ev.select(pool, 20)                                       # the "v2 run": 10 fp, 5 relabels, 5 stored
    kept, _ = ev.exclude(pool, {p.item.claim_hash[:16] for p in first})
    second = ev.select(kept, 20)                                      # the "v3 run", fresh but for the relabels
    out = {"pool": tmp_path / "pool.jsonl", "v2": tmp_path / "v2.json", "v3": tmp_path / "v3.json",
           "list": tmp_path / "v2-prefixes.json"}
    out["pool"].write_text("".join(json.dumps(x.line()) + "\n" for x in pool), encoding="utf-8")
    out["v2"].write_text(json.dumps(_report(first, V2_RUN + "-0000")), encoding="utf-8")
    out["v3"].write_text(json.dumps(_report(second, V3_RUN + "-0000")), encoding="utf-8")
    out["list"].write_text(json.dumps([p.item.claim_hash[:16] for p in first]), encoding="utf-8")
    return {k: str(v) for k, v in out.items()}


def _main(capsys, *args):
    rc = ev.main(list(args))
    return rc, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("excludes, missing", [
    ((), "3018008d, 33835b87"),
    (("v2",), "33835b87"),
    (("list", "v3"), "3018008d"),                                    # a prefix list carries no run id
])
def test_a_live_v4_run_refuses_unless_both_shaping_runs_are_excluded(files, capsys, monkeypatch, excludes, missing):
    monkeypatch.setattr(ev, "_caller", lambda *a: pytest.fail("no call may be made"))
    args = ["run", "--pool", files["pool"], "--prompt", "v4", "--trial", "3", "--live", "--confirm-spend"]
    for e in excludes:
        args += ["--exclude", files[e]]
    rc, out = _main(capsys, *args)
    assert rc == 2 and not out["ok"] and f"shaped on run(s) {missing}" in out["refused"]


def test_a_dry_run_reports_the_gap_without_refusing(files, capsys, monkeypatch):
    monkeypatch.setattr(ev, "_caller", lambda *a: pytest.fail("no call may be made"))
    rc, out = _main(capsys, "run", "--pool", files["pool"], "--prompt", "v4", "--limit", "20",
                    "--exclude", files["v2"])
    assert rc == 0 and out["mode"] == "dry_run" and out["out_of_sample"]["missing"] == [V3_RUN]
    rc, out = _main(capsys, "run", "--pool", files["pool"], "--prompt", "v4", "--limit", "20",
                    "--exclude", files["v2"], "--exclude", files["v3"])
    assert rc == 0 and out["out_of_sample"] == {"shaped_on": [V2_RUN, V3_RUN], "excluded_runs": [V2_RUN, V3_RUN],
                                                "missing": []}
    assert out["excluded"]["claims"] == 30 and out["subset"]["by_group"]["tp_relabel"] == 5
    assert out["instrument"].endswith(":entailment#v4") and out["prompt_version"] == pr.get("v4").tag


def test_a_live_v4_trial_with_both_runs_excluded_keeps_the_reasons(files, capsys, monkeypatch):
    calls = []

    def fake(inst, system, user, schema):
        calls.append(system)
        return _resp(json.dumps({"verdict": "unsupported", "quote": "", "why": "the source gives no pace " * 40}))
    monkeypatch.setattr(ev, "_caller", fake)
    base = ["run", "--pool", files["pool"], "--prompt", "v4", "--trial", "3", "--live", "--confirm-spend",
            "--exclude", files["v2"], "--exclude", files["v3"]]
    rc, out = _main(capsys, *base, "--with-reasons")
    assert rc == 0 and out["calls"] == 9 and all(s == pr.ENTAILMENT_SYSTEM_V4 for s in calls)
    assert out["score"]["verdict"] == "trial" and not out["out_of_sample"]["missing"]
    assert set(out["reasons"]) == set(out["votes"]) and all(len(k) == 16 for k in out["reasons"])
    assert all(len(w) == 3 and all(len(x) == r1.REASON_CHARS for x in w) for w in out["reasons"].values())
    rc, out = _main(capsys, *base)
    assert rc == 0 and "reasons" not in out                                       # counts and prefixes only


def test_run_keeps_an_empty_reason_for_every_missing_vote():
    items = [_item(1, "Reach the summit of Mount Selk."), _item(2, "Walk the Selk ridge.")]
    replies = iter([_resp(json.dumps({"verdict": "supported", "quote": "q", "why": "stated"})), _resp("not json")])
    res = r1.run(items, [_inst("v4")], cap=2.0, votes=1, run_id="zz", caller=lambda *a: next(replies),
                 log=lambda *a: None)
    inst = _inst("v4").id
    assert res["votes"][inst] == {items[0].claim_hash: [False], items[1].claim_hash: [None]}
    assert res["reasons"][inst] == {items[0].claim_hash: ["stated"], items[1].claim_hash: [""]}
    ext = r1.instruments([GPT], ["extraction"], prompt="v4")[0]
    assert r1.run(items, [ext], cap=2.0, votes=1, run_id="zz", log=lambda *a: None,
                  caller=lambda *a: _resp(json.dumps({"claims": []})))["reasons"] == {}
