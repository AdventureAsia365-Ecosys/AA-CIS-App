"""AA-1048 (S279 lane l22) — claim-instrument prompt v2 (services/ground_truth/prompts.py) and its gated evaluation
(services/ground_truth/instrument_eval.py): prompt version selection and recording, the v2 user prompt, the pool and
the B3 check, subset stratification, the go / no-go math and the paid-run gates. Every model call is a fake; every
text, place and hash is invented."""
import hashlib
import json
import types

import pytest

from services.ground_truth import instrument_eval as ev
from services.ground_truth import prompts as pr
from services.ground_truth import r1

SOURCE = ("Day 1: Arrive in Velmora. Dinner at a hotel restaurant.\nDay 2: Climb to the top of Mount Selk.\n"
          "Place background (checked facts about the place itself — you may use these; they say nothing about what "
          "this trip does):\n- The Selk monastery was built in the twelfth century by river traders.\n"
          "- Its library holds painted scrolls.\n\nDay 3: Drive back to Velmora.")
FIELD = ("Why go?\nA round trip from Velmora. Reach the summit of Mount Selk. Eat local at a family table.\n"
         "The Selk monastery was built in the twelfth century by river traders.")


def _h(n: int) -> str:
    """An invented claim hash (distinct prefixes, as real sha256 hashes have)."""
    return hashlib.sha256(f"invented-claim-{n}".encode()).hexdigest()


def _item(n, claim, label="ok", writer="t9_blog", source=SOURCE, field_text=FIELD):
    return r1.Item(_h(n), writer, "fact.inclusion", label, "representative", "a" * 64, "content_text", claim,
                   field_text, source, ("Day 2: Climb to the top of Mount Selk.",), "held_out")


def _resp(content, model="gpt-6-luna-openai", conn="openai-direct", cost=0.0004):
    return types.SimpleNamespace(content=content, model_used=model, connection_key=conn, satellite_account=None,
                                 cost_usd=cost, input_tokens=100, output_tokens=20, provider="openai",
                                 stop_reason="stop", fallback_used=False)


LIM = ev.Limits(cap_usd=1.5, max_claims=50, trial_claims=3, fp_drop_min=0.6, tp_keep_min=0.9)


# ── prompt versions ───────────────────────────────────────────────────────────────────────────────────────────────


def test_v1_stays_the_default_and_keeps_its_ids_and_texts():
    inst = r1.Instrument("gpt-6-luna-openai", "openai-direct", "entailment")
    assert inst.prompt == pr.DEFAULT == "v1" and inst.id == "gpt-6-luna-openai@openai-direct:entailment"
    assert r1.ENTAILMENT_SYSTEM is pr.ENTAILMENT_SYSTEM and "SOURCE LINES" in r1.calls([_item(1, "x y z")], inst)[0][0]
    assert r1.calls([_item(1, "x y z")], inst)[0][0] == r1.entailment_prompt(_item(1, "x y z"))
    assert [i.id for i in r1.instruments()][0] == "gpt-6-luna-openai@openai-direct:entailment"


def test_v2_is_selectable_has_its_own_id_and_reads_the_whole_source_and_context():
    inst = r1.instruments([("gpt-6-luna-openai", "openai-direct")], ["entailment"], prompt="v2")[0]
    assert inst.id == "gpt-6-luna-openai@openai-direct:entailment#v2"
    user = r1.calls([_item(1, "Reach the summit of Mount Selk.")], inst)[0][0]
    assert SOURCE in user and "SENTENCE: Reach the summit of Mount Selk." in user
    assert "BEFORE: A round trip from Velmora." in user and "AFTER: Eat local at a family table." in user
    with pytest.raises(r1.R1Refused, match="unknown prompt version"):
        r1.instruments(prompt="v9")
    # the cost estimate reads the version's own (longer) system text
    v1 = r1.Instrument("gpt-6-luna-openai", "openai-direct", "entailment")
    assert r1.call_cost(inst, "u", 0) > r1.call_cost(v1, "u", 0)


def test_v2_texts_carry_the_panel_changes_b1_to_b5():
    for system in (pr.ENTAILMENT_SYSTEM_V2, pr.EXTRACTION_SYSTEM_V2):
        assert "paraphrase" in system and "one-step inference" in system                     # B1
        assert "nickname" in system and "turns it into a promise" in system                  # B2
        assert "'Place background'" in system and "They support any sentence about that place" in system  # B3
        assert "FAQ questions" in system and "calls to action" in system and "AFTER" in system  # B4
        for pattern in ("character of a meal", "qualities of the guide", "group or social experience",
                        "arrival or crowd atmosphere", "an experience at a stop"):        # B5
            assert pattern in system
    assert "whole" in pr.EXTRACTION_SYSTEM_V2


def test_versions_hash_their_texts_and_tag_every_call():
    v1, v2 = pr.get("v1"), pr.get("v2")
    assert v1.sha != v2.sha and len(v1.sha) == 12 and v2.tag == f"aa1048-r1-v2@{v2.sha}"
    assert pr.get("v2").sha == v2.sha                                                       # stable
    with pytest.raises(pr.UnknownPromptVersion):
        pr.get("v0")


def test_around_finds_the_neighbours_of_a_sentence():
    assert pr.around(FIELD, "Why go?") == ("", "A round trip from Velmora. Reach the summit of Mount Selk. Eat local "
                                                "at a family table.")
    assert pr.around(FIELD, "A round trip from Velmora.")[0] == "Why go?"
    assert pr.around(FIELD, "Eat local at a family table.")[1].startswith("The Selk monastery")
    assert pr.around(FIELD, "Not in the text.") == ("", "")
    assert pr.around(FIELD, "") == ("", "")


def test_run_records_the_prompt_version_in_the_signal_and_the_ledger(monkeypatch):
    inst = r1.instruments([("gpt-6-luna-openai", "openai-direct")], ["entailment"], prompt="v2")[0]
    seen = []
    r1.run([_item(1, "Reach the summit of Mount Selk.")], [inst], cap=2.0, votes=1, run_id="zz",
           caller=lambda *a: _resp(json.dumps({"verdict": "supported", "quote": "", "why": ""})),
           log=lambda resp, sig, parsed, err: seen.append(sig))
    assert seen[0]["prompt_version"] == pr.get("v2").tag and seen[0]["instrument"].endswith("#v2")
    rows = []
    from shared.llm_client import call_log

    monkeypatch.setattr(call_log, "record_call_sync", lambda **kw: rows.append(kw))
    r1._log(_resp("{}"), {"prompt_version": "aa1048-r1-v2@abc"}, {}, None)
    assert rows[0]["prompt_version"] == "aa1048-r1-v2@abc" and rows[0]["stage"] == r1.STAGE


# ── pool and B3 ───────────────────────────────────────────────────────────────────────────────────────────────────


def test_read_votes_from_an_r1_log_or_a_json_object():
    log = ("PLAN {}\nVOTES " + json.dumps({"instrument": "other", "votes": {"aa": "111"}}) + "\nVOTES "
           + json.dumps({"instrument": ev.R1_INSTRUMENT, "votes": {"0000": "1-0"}}) + "\n")
    assert ev.read_votes(log) == {"0000": [True, None, False]}
    assert ev.read_votes(json.dumps({"instrument": "x", "votes": {"ab": "01"}}), "x") == {"ab": [False, True]}
    with pytest.raises(ev.EvalRefused, match="no votes"):
        ev.read_votes("PLAN {}\n")


def _panel(majority, *fires):
    return {"majority": majority, "votes": {f"lens{k}": {"fire": f} for k, f in enumerate(fires)}}


def test_panel_group_maps_the_most_common_fire_reason():
    assert ev.panel_group(_panel("ok", "paraphrase_or_inference", "paraphrase_or_inference", "none")) == "fp_paraphrase"
    assert ev.panel_group(_panel("ok", "none", "none", "none")) == "fp_verbatim"
    assert ev.panel_group(_panel("ok", "background_knowledge", "background_knowledge", "x")) == "fp_background"
    assert ev.panel_group(_panel("ok", "none", "background_knowledge")) == "fp_other"            # a tie
    assert ev.panel_group(_panel("ok", "over_split", "over_split")) == "fp_other"
    assert ev.panel_group(_panel("violation", "real_unsupported")) == "fp_disputed"
    assert ev.panel_group(None) == "fp_other"


def test_finals_relabels_and_prefix_matching():
    lines = [{"claim_hash": _h(1), "label": "ok", "claim_class": "fact.inclusion"},
             {"claim_hash": _h(2), "label": "violation", "claim_class": "fact.number"},
             {"claim_hash": _h(5), "label": "ok", "claim_class": "fact.number", "source": "prefilter"}]
    finals = ev.finals_from(lines, [_h(1)])
    assert finals[_h(1)]["label"] == "violation" and finals[_h(2)]["label"] == "violation"
    assert _h(5) not in finals                                                   # machine-rule labels never count
    with pytest.raises(ev.EvalRefused, match="no label line"):
        ev.finals_from(lines, [_h(3)])
    with pytest.raises(ev.EvalRefused, match="share the prefix"):
        ev.by_prefix(["ab01", "ab02"], {"ab": 1})
    assert ev.by_prefix(["ab01", "cd02"], {"ab": 1}) == {"ab01": 1}
    assert ev.by_prefix(["ab01"], {}) == {}


def test_build_pool_keeps_fired_claims_with_their_group():
    items = [_item(1, "Eat local at a family table.", "violation"), _item(2, "Reach the summit of Mount Selk."),
             _item(3, "A round trip from Velmora."), _item(4, "Why go?", "violation")]
    votes = {_h(1): [True, True, False], _h(2): [True, True, True], _h(3): [False, False, True],
             _h(4): [True, None, True]}
    panel = {_h(2): _panel("ok", "paraphrase_or_inference")}
    pool = ev.build_pool(items, votes, panel, relabelled=[_h(4)])
    assert [(p.item.claim_hash, p.group) for p in pool] == [(_h(1), "tp"), (_h(2), "fp_paraphrase"),
                                                             (_h(4), "tp_relabel")]
    again = ev.read_pool("".join(json.dumps(p.line()) + "\n" for p in pool))
    assert again == pool
    assert ev.pool_counts(pool)["by_group"] == {"fp_paraphrase": 1, "tp": 1, "tp_relabel": 1}
    with pytest.raises(ev.EvalRefused, match="unknown group"):
        ev.read_pool(json.dumps({**pool[0].line(), "group": "nope"}))


def test_place_background_lines_and_best_line():
    pb = ev.place_background_lines(SOURCE)
    assert pb == {"- The Selk monastery was built in the twelfth century by river traders.",
                  "- Its library holds painted scrolls."}
    line, covered = ev.best_line("The Selk monastery was built in the twelfth century by river traders.", SOURCE)
    assert line in pb and covered
    line, covered = ev.best_line("A long night of dancing in Velmora.", SOURCE)
    assert line not in pb and not covered


def test_b3_check_compares_fire_rates_by_where_the_support_sits():
    items = [_item(1, "The Selk monastery was built in the twelfth century by river traders."),
             _item(2, "Its library holds painted scrolls."),
             _item(3, "Climb to the top of Mount Selk."),
             _item(4, "Dinner at a hotel restaurant in Velmora."),
             _item(5, "A claim on a source without background.", source="Day 1: Arrive in Velmora."),
             _item(6, "Eat local at a family table.", "violation")]
    votes = {_h(1): [True] * 3, _h(2): [True] * 3, _h(3): [False] * 3, _h(4): [True, False, False],
             _h(5): [True] * 3, _h(6): [True] * 3}
    pool = [ev.PoolItem(items[0], "fp_verbatim"), ev.PoolItem(items[2], "fp_verbatim")]
    rep = ev.b3_check(items, votes, pool)
    rates = rep["ok_claims_in_sources_with_place_background"]
    assert rates["covered_by_place_background"] == {"n": 2, "fired": 2, "fire_rate": 1.0}
    assert rates["best_other_line"]["n"] == 2 and rates["best_other_line"]["fired"] == 0
    assert rep["fp_verbatim"] == {"n": 2, "best_line_is_place_background": 1}
    assert rep["reading"].startswith("consistent with B3")
    assert ev.b3_check([], {}, [])["reading"] == "not shown by the stored votes"


# ── subset ────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_allocate_is_proportional_and_never_over_a_group():
    assert ev.allocate(25, {"a": 135, "b": 30, "c": 29}) == {"a": 17, "b": 4, "c": 4}
    assert sum(ev.allocate(20, {"t2": 45, "t9_blog": 26}).values()) == 20
    assert ev.allocate(10, {"a": 2, "b": 3}) == {"a": 2, "b": 3}
    assert ev.allocate(0, {"a": 2}) == {"a": 0} and ev.allocate(3, {}) == {}


def _pool():
    out, n = [], 0
    spec = [("fp_paraphrase", 27, "ok"), ("fp_verbatim", 6, "ok"), ("fp_background", 6, "ok"), ("fp_other", 4, "ok"),
            ("fp_disputed", 2, "ok"), ("tp_relabel", 5, "violation"), ("tp", 30, "violation")]
    for group, count, label in spec:
        for k in range(count):
            n += 1
            writer = "t2" if k % 3 else "t9_blog"
            out.append(ev.PoolItem(_item(n, f"Invented claim number {n}.", label, writer=writer), group))
    return out


def test_select_stratifies_half_and_half_with_every_relabel_first():
    subset = ev.select(_pool(), 50)
    comp = ev.composition(subset)
    assert comp["claims"] == 50
    assert comp["by_group"] == {"fp_background": 4, "fp_paraphrase": 17, "fp_verbatim": 4, "tp": 20,
                                "tp_relabel": 5}
    assert len({p.item.claim_hash for p in subset}) == 50
    assert ev.select(_pool(), 50) == subset                                                   # deterministic
    tp = [p for p in subset if p.group == "tp"]
    assert {p.item.writer for p in tp} == {"t2", "t9_blog"}
    trial = ev.select(_pool(), 50)[:3]
    assert [p.group for p in trial] == ["tp_relabel", "fp_paraphrase", "tp"]


def test_select_with_a_small_limit():
    comp = ev.composition(ev.select(_pool(), 4))
    assert comp["claims"] == 4 and comp["by_group"] == {"fp_paraphrase": 2, "tp_relabel": 2}


# ── score ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def _scored(fp_dropped, n_fp, tp_kept, n_tp, **kw):
    subset, votes, n = [], {}, 0
    for k in range(n_fp):
        n += 1
        subset.append(ev.PoolItem(_item(n, "x y z"), "fp_paraphrase"))
        votes[_h(n)] = [False] * 3 if k < fp_dropped else [True] * 3
    for k in range(n_tp):
        n += 1
        subset.append(ev.PoolItem(_item(n, "x y z", "violation"), "tp"))
        votes[_h(n)] = [True] * 3 if k < tp_kept else [False] * 3
    return ev.score(subset, votes, LIM, **kw)


def test_go_needs_both_bars():
    go = _scored(6, 10, 9, 10)
    assert go["verdict"] == "go" and go["false_positives"]["dropped_share"] == 0.6
    assert go["true_positives"]["kept_share"] == 0.9 and go["recall"] == 0.9
    assert go["precision"] == round(9 / (9 + 4), 4)
    assert _scored(5, 10, 10, 10)["verdict"] == "no_go"
    assert _scored(10, 10, 8, 10)["verdict"] == "no_go"
    assert _scored(10, 10, 10, 10, trial=True)["verdict"] == "trial"
    assert _scored(10, 10, 10, 10, stopped="cap")["verdict"] == "incomplete"
    assert _scored(0, 0, 3, 3)["verdict"] == "incomplete"


def test_relabelled_claims_are_reported_apart_and_never_gated():
    # 17 of 20 stored violations kept and 5 of 5 relabels kept: 22/25 = 88% together would clear an 88% bar, but the
    # bar reads the out-of-sample 17/20 = 85%: no_go whatever the in-sample relabels do.
    lim = ev.Limits(cap_usd=1.5, max_claims=50, trial_claims=3, fp_drop_min=0.6, tp_keep_min=0.88)
    subset, votes, n = [], {}, 0
    for group, count, kept in (("fp_paraphrase", 10, 0), ("tp", 20, 17), ("tp_relabel", 5, 5)):
        for k in range(count):
            n += 1
            subset.append(ev.PoolItem(_item(n, "x y z", "ok" if group.startswith("fp") else "violation"), group))
            votes[_h(n)] = [False] * 3 if group.startswith("fp") or k >= kept else [True] * 3
    rep = ev.score(subset, votes, lim)
    assert rep["true_positives"]["n"] == 20 and rep["true_positives"]["kept_share"] == 0.85
    assert rep["true_positives"]["sample"] == "out_of_sample" and rep["bars"]["tp_kept_on"] == ["tp"]
    assert rep["relabelled_in_sample"]["kept_share"] == 1.0 and rep["relabelled_in_sample"]["sample"] == "in_sample"
    assert rep["true_positives_with_relabels"]["n"] == 25 and rep["true_positives_with_relabels"]["kept"] == 22
    assert rep["recall"] == 0.85 and rep["precision"] == 1.0 and rep["verdict"] == "no_go"
    only_relabels = [p for p in subset if p.group != "tp"]
    assert ev.score(only_relabels, votes, lim)["verdict"] == "incomplete"                   # no stored violation


def test_unverified_claims_count_neither_way():
    subset = [ev.PoolItem(_item(1, "x y z"), "fp_verbatim"), ev.PoolItem(_item(2, "x y z", "violation"), "tp")]
    rep = ev.score(subset, {_h(1): [True, False, None], _h(2): [None, None, None]}, LIM)
    assert rep["false_positives"]["unverified"] == 1 and rep["false_positives"]["dropped_share"] == 0.0
    assert rep["true_positives"]["unverified"] == 1 and rep["true_positives"]["kept_share"] == 0.0
    assert rep["precision"] is None and rep["verdict"] == "no_go"


# ── CLI gates ─────────────────────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def pool_file(tmp_path, monkeypatch):
    p = tmp_path / "pool.jsonl"
    p.write_text("".join(json.dumps(x.line()) + "\n" for x in _pool()), encoding="utf-8")
    monkeypatch.setattr(ev, "limits", lambda: LIM)
    monkeypatch.setattr(ev, "refresh_settings", lambda: True)
    return str(p)


def _main(capsys, *args):
    rc = ev.main(list(args))
    return rc, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("args, why", [
    (("--live",), "confirm-spend"),
    (("--live", "--confirm-spend"), "--trial N or --limit N"),
    (("--live", "--confirm-spend", "--limit", "51"), "1..50"),
    (("--live", "--confirm-spend", "--trial", "4"), "1..3"),
    (("--live", "--confirm-spend", "--trial", "0"), "1..3"),
    (("--live", "--confirm-spend", "--limit", "10", "--cap", "1.6"), "cap"),
    (("--trial", "3", "--limit", "10"), "not both"),
    (("--prompt", "v9"), "unknown prompt version"),
    (("--candidate", "no-connection"), "candidate"),
])
def test_paid_runs_are_refused_outside_the_gates(pool_file, capsys, monkeypatch, args, why):
    monkeypatch.setattr(ev, "_caller", lambda *a: pytest.fail("no call may be made"))
    rc, out = _main(capsys, "run", "--pool", pool_file, *args)
    assert rc == 2 and not out["ok"] and why in out["refused"]


def test_dry_run_is_the_default_and_prints_the_plan(pool_file, capsys, monkeypatch):
    monkeypatch.setattr(ev, "_caller", lambda *a: pytest.fail("no call may be made"))
    installed = []
    from services.golden_set import llm_guard

    monkeypatch.setattr(llm_guard, "install", lambda tag: installed.append(tag))
    rc, out = _main(capsys, "run", "--pool", pool_file)
    assert rc == 0 and out["mode"] == "dry_run" and out["subset"]["claims"] == 50 and installed
    assert out["plan"]["stage"] == "adhoc_aa1048" and out["plan"]["role"] == "judge"
    assert out["plan"]["expected_total_usd"] > 0 and out["instrument"].endswith(":entailment#v2")
    assert out["prompt_version"] == pr.get("v2").tag
    # --dry-run wins over --live: never paid by accident
    rc, out = _main(capsys, "run", "--pool", pool_file, "--dry-run", "--live", "--confirm-spend", "--trial", "3")
    assert rc == 0 and out["mode"] == "dry_run"


def test_a_plan_over_the_cap_is_refused(pool_file, capsys, monkeypatch):
    monkeypatch.setattr(ev, "_caller", lambda *a: pytest.fail("no call may be made"))
    rc, out = _main(capsys, "run", "--pool", pool_file, "--limit", "50", "--cap", "0.0001")
    assert rc == 3 and out["refused"] == "the plan does not fit the cap"


def test_live_trial_runs_through_r1_and_scores(pool_file, capsys, monkeypatch, tmp_path):
    calls = []

    def fake(inst, system, user, schema):
        calls.append(system)
        return _resp(json.dumps({"verdict": "unsupported", "quote": "", "why": "x"}))
    monkeypatch.setattr(ev, "_caller", fake)
    monkeypatch.setattr(ev, "_probe", lambda since: (lambda: None))
    rows = []
    from shared.llm_client import call_log

    monkeypatch.setattr(call_log, "record_call_sync", lambda **kw: rows.append(kw))
    out_file = tmp_path / "report.json"
    rc, out = _main(capsys, "run", "--pool", pool_file, "--live", "--confirm-spend", "--trial", "3",
                    "--out", str(out_file))
    assert rc == 0 and out["mode"] == "live" and out["calls"] == 9 and len(calls) == 9
    assert all(s == pr.ENTAILMENT_SYSTEM_V2 for s in calls)
    assert out["score"]["verdict"] == "trial" and out["score"]["true_positives"]["kept"] == 1
    assert out["score"]["relabelled_in_sample"]["kept"] == 1
    assert out["settings_refreshed"] is True and json.loads(out_file.read_text())["run_id"] == out["run_id"]
    assert all(len(k) == 16 for k in out["votes"])                                            # prefixes only
    assert len(rows) == 9 and {r["prompt_version"] for r in rows} == {pr.get("v2").tag}
    assert {r["stage"] for r in rows} == {"adhoc_aa1048"} and {r["role"] for r in rows} == {"judge"}


def test_pool_command_builds_the_pool_and_the_b3_report(tmp_path, capsys):
    items = [_item(1, "Eat local at a family table.", "violation"),
             _item(2, "The Selk monastery was built in the twelfth century by river traders.")]
    export = tmp_path / "export.jsonl"
    export.write_text("".join(json.dumps({"claim_hash": i.claim_hash, "writer": i.writer, "content_sha256": "a" * 64,
                                          "field": i.field, "claim_text": i.claim_text, "field_text": i.field_text,
                                          "source_text": i.source_text, "refs": {"tour_id": _tour("held_out")}})
                              + "\n" for i in items), encoding="utf-8")
    labels = tmp_path / "labels.jsonl"
    labels.write_text("".join(json.dumps({"claim_hash": i.claim_hash, "label": i.label,
                                          "claim_class": "fact.inclusion"}) + "\n" for i in items), encoding="utf-8")
    votes = tmp_path / "r1.log"
    votes.write_text("VOTES " + json.dumps({"instrument": ev.R1_INSTRUMENT,
                                            "votes": {_h(1)[:8]: "111", _h(2)[:8]: "110"}}) + "\n", encoding="utf-8")
    panel = tmp_path / "merged.json"
    panel.write_text(json.dumps([{"id": _h(2)[:12], **_panel("ok", "none", "none", "none")}]), encoding="utf-8")
    out = tmp_path / "pool.jsonl"
    rc, rep = _main(capsys, "pool", "--export", str(export), "--labels", str(labels), "--votes", str(votes),
                    "--panel", str(panel), "--out", str(out))
    assert rc == 0 and rep["pool"]["by_group"] == {"fp_verbatim": 1, "tp": 1}
    assert rep["b3"]["fp_verbatim"] == {"n": 1, "best_line_is_place_background": 1}
    assert len(ev.read_pool(out.read_text())) == 2


def _tour(split):
    from services.ground_truth import claims as cl

    return next(f"tour-{n}" for n in range(1000) if cl.split_of(f"tour_id:tour-{n}") == split)


def test_limits_come_from_the_settings_registry(monkeypatch):
    values = {"cis.spend.instrument_eval_cap_usd": 5, "cis.spend.instrument_eval_max_claims": 40,
              "cis.spend.instrument_eval_trial_claims": 2, "cis.quality.instrument_eval_fp_drop_min_pct": 70,
              "cis.quality.instrument_eval_tp_keep_min_pct": 95}
    monkeypatch.setattr(ev.settings, "get", lambda key, tenant_id=None: values[key])
    lim = ev.limits()
    assert lim == ev.Limits(cap_usd=r1.HARD_CAP_USD, max_claims=40, trial_claims=2, fp_drop_min=0.7, tp_keep_min=0.95)
