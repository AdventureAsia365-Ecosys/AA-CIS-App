"""AA-1048 (S281 lane l8; S282 lanes l4-v6 and l9-v7: the default step set is now v7) — the two-step claim instrument
(services/ground_truth/two_step.py, prompts v5c / v5j, v6c / v6j and v7c / v7j): the
step prompt versions, the reply parsing (step-1 classification, step-2 support), the routing rule, the live run on a
fake caller (one budget, ledger signal per step, the specifics passed on), the score with its step bars, the
pre-registration check, the out-of-sample gate and the CLI's DRY RUN / live refusals. Every model call is a fake;
every text, place and hash is invented."""
import dataclasses
import hashlib
import json
import types

import pytest

from services.ground_truth import instrument_eval as ev
from services.ground_truth import prompts as pr
from services.ground_truth import r1
from services.ground_truth import two_step as ts
from shared import settings

SOURCE = ("Day 1: Arrive in Pellow. Dinner at the inn.\nDay 2: A boat trip on Quarne lake or a cycle along its shore.\n"
          "Day 3: Drive back to Pellow.")
FIELD = "Three days around Quarne lake.\nA boat trip on Quarne lake at sunrise.\nBack to Pellow on day 3."
CAND = "gpt-6-luna-openai@openai-direct"
LIM = ev.Limits(cap_usd=1.5, max_claims=50, trial_claims=3, fp_drop_min=0.6, tp_keep_min=0.9)
SLIM = ts.StepLimits(route_tp_min=0.95, judge_tp_keep_min=0.95, judge_fp_drop_min=0.4)


def _h(n: int) -> str:
    return hashlib.sha256(f"invented-two-step-{n}".encode()).hexdigest()


def _item(n, claim="A boat trip on Quarne lake at sunrise.", label="ok", writer="t2", piece="b"):
    return r1.Item(_h(n), writer, "fact.embellish", label, "representative", piece * 64, "itineraries", claim, FIELD,
                   SOURCE, (), "train")


def _pi(n, group, **kw):
    return ev.PoolItem(_item(n, label="violation" if group == "tp" else "ok", **kw), group)


def _resp(payload, model="gpt-6-luna-openai", conn="openai-direct", cost=0.0002):
    return types.SimpleNamespace(content=json.dumps(payload), model_used=model, connection_key=conn,
                                 satellite_account=None, cost_usd=cost, input_tokens=900, output_tokens=40,
                                 provider="openai", stop_reason="stop", fallback_used=False)


def _insts(steps="v7"):
    return ts.instruments(CAND, steps)


CLASSIFY = ("v5c", "v6c", "v7c")


# ── prompt versions ───────────────────────────────────────────────────────────────────────────────────────────────


def test_step_prompts_are_registered_with_pinned_tags_and_old_tags_are_unchanged():
    assert pr.get("v5c").tag == "aa1048-r1-v5c@50704a977e52"
    assert pr.get("v5j").tag == "aa1048-r1-v5j@0c3529d50a55"
    # the one-prompt versions keep the tags their ledger rows carry
    assert pr.get("v2").tag == "aa1048-r1-v2@ddaab8c279ef" and pr.get("v4").tag == "aa1048-r1-v4@5cd34821af82"
    assert pr.get("v5c").step == "classify" and pr.get("v5j").step == "judge" and pr.get("v4").step == ""
    assert set(pr.get("v5c").shaped_on) == {"3018008d", "33835b87", "2977dd78"} == set(pr.get("v5j").shaped_on)
    # the S279 pool shaped both steps (through v2); the field is not part of the tag
    assert pr.get("v5c").shaped_on_pools == (pr.S279_POOL,) == pr.get("v5j").shaped_on_pools
    assert pr.get("v4").shaped_on_pools == ()


def test_step_prompts_are_entailment_only_and_never_run_as_one_prompt():
    with pytest.raises(pr.UnknownPromptVersion, match="entailment only"):
        pr.get("v5c").system("extraction")
    for v in ("v5c", "v5j"):
        with pytest.raises(r1.R1Refused, match="two-step"):
            r1.instruments([("gpt-6-luna-openai", "openai-direct")], ["entailment"], prompt=v)
    insts = _insts("v5")
    assert insts["classify"].id == "gpt-6-luna-openai@openai-direct:entailment#v5c"
    assert insts["judge"].id == "gpt-6-luna-openai@openai-direct:entailment#v5j"
    assert _insts()["classify"].id.endswith("#v7c") and _insts()["judge"].id.endswith("#v7j")
    with pytest.raises(ts.TwoStepRefused, match="no price"):
        ts.instruments("no-such-model@x")
    with pytest.raises(ts.TwoStepRefused, match="<model>@<connection>"):
        ts.instruments("gpt-6-luna-openai")


def test_step_texts_split_the_question_along_the_source_fidelity_policy():
    c, j = pr.V5C_SYSTEM, pr.V5J_SYSTEM
    assert "restatement" in c and "new_claim" in c and "new_specifics" in c and "answer new_claim" in c
    for word in ("background about a place", "one obvious step", "a sentence about the text itself"):
        assert word in c
    for word in ("pace, effort", "a view or sighting", "one side of an 'or' made firm"):
        assert word in c
    assert "SPECIFICS" in j and "Place background" in j and "joining two source facts" in j
    for invented in ("Morvane", "Quell", "Tasska", "Ilbern"):                 # invented example names only
        assert invented in c


def test_step2_user_prompt_lists_the_specifics_or_asks_for_the_whole_sentence():
    item = _item(1)
    user = ts.step2_user(item, ["at sunrise", "boat trip"])
    assert SOURCE in user and "SENTENCE: A boat trip on Quarne lake at sunrise." in user
    assert user.endswith("SPECIFICS TO CHECK:\n- at sunrise\n- boat trip")
    assert ts.step2_user(item).endswith(f"- {pr.WHOLE_SENTENCE}")
    assert ts.step1_user(item) == pr.v2_entailment_user(item)


# ── replies and routing ───────────────────────────────────────────────────────────────────────────────────────────


def test_reply_parsing_is_strict():
    assert ts.classify_vote({"kind": "new_claim"}) is True and ts.classify_vote({"kind": "restatement"}) is False
    assert ts.classify_vote({"kind": "maybe"}) is None and ts.classify_vote({}) is None
    assert ts.judge_vote({"verdict": "unsupported"}) is True and ts.judge_vote({"verdict": "supported"}) is False
    assert ts.judge_vote({"verdict": "unclear"}) is None
    assert ts.specifics_of({"new_specifics": [" at sunrise ", "", 3]}) == ["at sunrise", "3"]
    assert ts.specifics_of({"new_specifics": "at sunrise"}) == []
    assert ts.merge_specifics([["At sunrise", "boat"], ["at  sunrise", "lake view"]]) == ["At sunrise", "boat",
                                                                                         "lake view"]


def test_a_restatement_majority_clears_and_anything_else_goes_to_step_two():
    a, b, c, d = (_h(n) for n in range(4))
    step1 = {a: [False, False, True], b: [True, True, False], c: [True, False, None], d: [True, True, True]}
    step2 = {b: [True, True, False], c: [False, False, True]}
    assert ts.routed(step1[a]) is False and ts.routed(step1[c]) is True               # unverified step 1 never clears
    items = [types.SimpleNamespace(claim_hash=h) for h in (a, b, c, d)]
    assert ts.final_votes(items, step1, step2) == {a: [False], b: [True], c: [False], d: [None]}


# ── live run (fake caller) ────────────────────────────────────────────────────────────────────────────────────────


def _fake(step1, step2, calls):
    """step1 / step2: claim text -> reply payload; records (step prompt id, user)."""
    def caller(inst, system, user, schema):
        calls.append((inst.prompt, user, schema["name"]))
        claim = next(t for t in step1 if f"SENTENCE: {t}\n" in user)
        return _resp(step1[claim] if inst.prompt in CLASSIFY else step2[claim])
    return caller


def test_run_sends_only_new_claims_to_step_two_and_passes_their_specifics():
    restate, promise, fine = "Three days around Quarne lake.", "A boat trip on Quarne lake at sunrise.", \
        "Back to Pellow on day 3."
    items = [_item(1, restate), _item(2, promise, label="violation"), _item(3, fine)]
    step1 = {restate: {"kind": "restatement", "new_specifics": [], "why": "x"},
             promise: {"kind": "new_claim", "new_specifics": ["at sunrise"], "why": "x"},
             fine: {"kind": "new_claim", "new_specifics": [], "why": "x"}}
    step2 = {promise: {"verdict": "unsupported", "quote": "", "why": "sunrise"},
             fine: {"verdict": "supported", "quote": "Day 3", "why": "x"}}
    calls, logged = [], []
    res = ts.run(items, _insts(), cap=1.0, votes=3, run_id="r", caller=_fake(step1, step2, calls),
                 log=lambda resp, sig, parsed, err: logged.append(sig))
    assert res["calls"] == 15 and not res["stopped"]                                  # 3 x 3 + 2 x 3
    assert res["votes"]["classify"][_h(1)] == [False] * 3 and _h(1) not in res["votes"]["judge"]
    assert res["votes"]["judge"][_h(2)] == [True] * 3 and res["votes"]["judge"][_h(3)] == [False] * 3
    judged = [u for p, u, _ in calls if p == "v7j"]
    assert all(u.endswith("- at sunrise") for u in judged if f"SENTENCE: {promise}\n" in u)
    assert all(u.endswith(f"- {pr.WHOLE_SENTENCE}") for u in judged if f"SENTENCE: {fine}\n" in u)
    assert {s for _, _, s in calls} == {"claim_restatement_v7", "claim_entailment_v7"}
    assert len(logged) == 15 and {s["step"] for s in logged} == {"classify", "judge"}
    assert {s["prompt_version"] for s in logged if s["step"] == "judge"} == {pr.get("v7j").tag}
    assert res["by_step"]["classify"]["calls"] == 9 and res["by_step"]["judge"]["calls"] == 6
    assert res["by_step"]["judge"]["charged_usd"] == pytest.approx(0.0012)
    assert res["specifics"] == {_h(2): ["at sunrise"], _h(3): []}


def test_run_logs_each_reply_through_the_billed_ledger_writer(monkeypatch):
    rows = []
    from shared.llm_client import call_log

    monkeypatch.setattr(call_log, "record_call_sync", lambda **kw: rows.append(kw))
    item = _item(1, "Three days around Quarne lake.")
    ts.run([item], _insts(), cap=1.0, votes=1, run_id="r",
           caller=lambda *a: _resp({"kind": "restatement", "new_specifics": [], "why": "x"}))
    assert len(rows) == 1 and rows[0]["stage"] == r1.STAGE and rows[0]["role"] == "judge"
    assert rows[0]["prompt_version"] == pr.get("v7c").tag and rows[0]["quality_signal"]["step"] == "classify"


def test_the_cap_and_a_caller_error_stop_the_run_cleanly():
    item = _item(1)
    res = ts.run([item], _insts(), cap=0.001, votes=3, run_id="r",
                 caller=lambda *a: pytest.fail("no call may start: the first worst case passes the cap"))
    assert res["calls"] == 0 and res["stopped"].startswith("cap 0.001 USD")
    assert res["votes"]["classify"][_h(1)] == [None, None, None]

    def boom(*a):
        raise RuntimeError("route exhausted")
    res = ts.run([item], _insts(), cap=1.0, votes=3, run_id="r", caller=boom, log=lambda *a: None)
    assert res["calls"] == 1 and "caller error" in res["stopped"] and res["caller_errors"][0]["step"] == "classify"
    assert res["votes"]["judge"] == {_h(1): [None, None, None]}                       # routed, never called


def test_a_reply_from_another_connection_is_no_vote():
    res = ts.run([_item(1)], _insts(), cap=1.0, votes=1, run_id="r", log=lambda *a: None,
                 caller=lambda *a: _resp({"kind": "restatement", "new_specifics": [], "why": ""}, conn="acc1"))
    assert res["votes"]["classify"][_h(1)] == [None] and res["identity_mismatches"]


# ── score and step bars ───────────────────────────────────────────────────────────────────────────────────────────


def _subset():
    return [_pi(1, "tp"), _pi(2, "tp"), _pi(3, "fp_paraphrase"), _pi(4, "fp_background"), _pi(5, "fp_verbatim")]


def test_go_needs_the_overall_bars_and_every_step_bar():
    sub = _subset()
    s1 = {_h(1): [True] * 3, _h(2): [True] * 3, _h(3): [False] * 3, _h(4): [True] * 3, _h(5): [False] * 3}
    s2 = {_h(1): [True] * 3, _h(2): [True] * 3, _h(4): [False] * 3}
    sc = ts.score(sub, s1, s2, LIM, SLIM)
    assert sc["verdict"] == "go" and sc["false_positives"]["dropped_share"] == 1.0
    st = sc["steps"]
    assert st["route_tp_share"] == 1.0 and st["judge_tp_kept_share"] == 1.0 and st["judge_fp_dropped_share"] == 1.0
    assert st["step1"]["fp_paraphrase"] == {"restated": 1} and st["step2"]["fp_background"] == {"cleared": 1}
    # step 2 keeps the violations but its false-positive bar fails: the overall go turns into no_go
    strict = ts.StepLimits(0.95, 0.95, 1.0)
    s2b = {**s2, _h(4): [True] * 3}
    lim = ev.Limits(1.5, 50, 3, 0.6, 0.9)
    sc = ts.score(sub, s1, s2b, lim, strict)
    assert sc["overall_verdict"] == "go" and sc["steps"]["passes"]["judge_fp_dropped"] is False
    assert sc["verdict"] == "no_go"


def test_step_one_losing_a_violation_fails_the_route_bar_and_no_false_positive_at_step_two_passes():
    sub = _subset()
    s1 = {_h(1): [False] * 3, _h(2): [True] * 3, _h(3): [False] * 3, _h(4): [False] * 3, _h(5): [False] * 3}
    s2 = {_h(2): [True] * 3}
    sc = ts.score(sub, s1, s2, LIM, SLIM)
    assert sc["steps"]["route_tp_share"] == 0.5 and sc["steps"]["passes"]["route_tp"] is False
    assert sc["steps"]["judge_fp_dropped_share"] is None and sc["steps"]["passes"]["judge_fp_dropped"] is True
    assert sc["verdict"] == "no_go"
    assert ts.score(sub, s1, s2, LIM, SLIM, trial=True)["verdict"] == "trial"
    gold = {_h(1)[:16]: "new_claim", _h(3)[:16]: "restatement"}
    assert ts.score(sub, s1, s2, LIM, SLIM, gold=gold)["steps"]["step1_vs_panel"] == {
        "new_claim->restatement": 1, "restatement->restatement": 1}


# ── plan, measured cost, pre-registration, out of sample ──────────────────────────────────────────────────────────


def test_plan_prices_step_two_as_if_every_claim_reached_it():
    items = [_item(n) for n in range(4)]
    p = ts.plan(items, _insts(), votes=3, cap=1.5)
    assert [r["calls_max"] for r in p["steps"]] == [12, 12] and p["items"] == 4
    assert p["expected_total_usd_max"] == pytest.approx(sum(r["expected_usd_max"] for r in p["steps"]), abs=1e-4)
    assert p["fits_cap"] is (p["expected_total_usd_max"] * r1.SAFETY <= 1.5)
    with pytest.raises(ts.TwoStepRefused, match="cap must be"):
        ts.plan(items, _insts(), cap=5.0)
    with pytest.raises(ts.TwoStepRefused, match="no claims"):
        ts.plan([], _insts(), cap=1.0)


def test_measured_cost_scales_each_steps_cost_per_call():
    insts = _insts()
    item = _item(1)
    chars = {s: len(insts[s].prompt_version.system("entailment")) + len(u)
             for s, u in (("classify", ts.step1_user(item)), ("judge", ts.step2_user(item)))}
    rep = {"mode": "live", "stopped": None, "calls": 15, "run_id": "abcdef0123",
           "by_step": {s: {"instrument": insts[s].id, "prompt_version": insts[s].prompt_version.tag, "calls": 3,
                           "charged_usd": 0.0006, "mean_prompt_chars": chars[s]} for s in ts.STEPS}}
    expected, info = ts.measured_expectation(rep, insts)
    assert expected("classify", insts["classify"], ts.step1_user(item)) == pytest.approx(0.0002)
    assert info["usd_per_call"] == {"classify": 0.0002, "judge": 0.0002}
    p = ts.plan([item], insts, votes=3, cap=1.0, expected_of=expected)
    assert p["expected_total_usd_max"] == pytest.approx(0.0012, abs=1e-4)
    assert p["steps"][0]["expected_from"] == "measured"
    with pytest.raises(ts.TwoStepRefused, match="another instrument"):
        ts.measured_expectation({**rep, "by_step": {**rep["by_step"], "judge": {**rep["by_step"]["judge"],
                                                                                 "prompt_version": "old"}}}, insts)
    with pytest.raises(ts.TwoStepRefused, match="finished live"):
        ts.measured_expectation({**rep, "stopped": "cap"}, insts)


def test_the_preregistration_names_the_code_prompts_and_the_settings_defaults():
    prereg = ts.load_preregistration()
    insts = _insts()
    assert prereg["prompt_versions"] == {s: insts[s].prompt_version.tag for s in ts.STEPS}
    assert sorted(prereg["shaped_on"]) == ts.shaped_on(insts) and prereg["candidate"] == CAND
    assert prereg["shaped_on_pools"] == ts.shaped_on_pools(insts) == [pr.S279_POOL, pr.FRESH_POOL]
    # the settings' defaults are the registered bars (CIS 417 + 423), so a fresh Dev matches the pre-registration
    for name, key in prereg["bars_settings"].items():
        assert float(settings.definition(key).default_value) == prereg["bars"][name]
    chk = ts.preregistration_check(prereg, LIM, SLIM, insts, 3)
    assert chk["matches"] and chk["differences"] == []


def test_a_changed_bar_prompt_or_vote_count_is_not_the_preregistered_run():
    prereg = ts.load_preregistration()
    insts = _insts()
    chk = ts.preregistration_check(prereg, ev.Limits(1.5, 50, 3, 0.55, 0.9), SLIM, insts, 3)
    assert not chk["matches"] and chk["differences"] == ["bar fp_dropped_min_pct: registered 60.0, settings 55.0"]
    other = {**prereg, "prompt_versions": {"classify": "aa1048-r1-v5c@feedfacecafe", "judge":
                                           prereg["prompt_versions"]["judge"]}}
    assert any("classify prompt" in d for d in ts.preregistration_check(other, LIM, SLIM, insts, 3)["differences"])
    assert any(d.startswith("votes") for d in ts.preregistration_check(prereg, LIM, SLIM, insts, 5)["differences"])
    no_pool = {**prereg, "shaped_on_pools": []}
    assert any(d.startswith("shaped_on_pools") for d in
               ts.preregistration_check(no_pool, LIM, SLIM, insts, 3)["differences"])
    fewer = {**prereg, "shaped_on": ["3018008d"]}
    assert any(d.startswith("shaped_on:") for d in ts.preregistration_check(fewer, LIM, SLIM, insts, 3)["differences"])


def test_fresh_check_enforces_the_pool_file_the_shaping_runs_and_the_excluded_pieces():
    pool = [_pi(1, "tp", piece="c"), _pi(2, "fp_paraphrase", piece="d")]
    text = "pool text"
    shaped = ["2977dd78", "3018008d", "33835b87"]
    good = {"pool_sha256": ts.pool_sha256(text), "excluded_runs": shaped, "excluded_pieces": ["e" * 16]}
    oos = ts.fresh_check(good, text, pool, shaped)
    assert oos["missing"] == [] and oos["pool_sha_matches"] and ts.oos_refusal(oos) is None
    assert "sha256" in ts.oos_refusal(ts.fresh_check(good, "another pool", pool, shaped))
    assert "2977dd78" in ts.oos_refusal(ts.fresh_check({**good, "excluded_runs": shaped[1:]}, text, pool, shaped))
    hit = ts.fresh_check({**good, "excluded_pieces": ["c" * 16]}, text, pool, shaped)
    assert hit["pieces_on_excluded"] == 1 and "excluded as in-sample" in ts.oos_refusal(hit)


POOLS = ["487bf4847b6136fe"]


@pytest.mark.parametrize("extra,split_of_first,side", [
    ({"excluded_pools": POOLS, "split": "held_out"}, "held_out", "excluded"),     # the shaping pool named
    ({"excluded_pools": [], "split": "train"}, "train", "train_split"),           # every claim train
    ({"excluded_pools": [], "split": "held_out"}, "held_out", None),              # held-out, pool not excluded
    ({"excluded_pools": [], "split": "all"}, "train", None),                      # a mixed split is not train
    ({"excluded_pools": [], "split": "train"}, "held_out", None),                 # a held-out claim in a "train" pool
    ({"excluded_pools": ["f" * 16], "split": "held_out"}, "held_out", None),      # another pool excluded
])
def test_fresh_check_keeps_the_pools_the_steps_were_shaped_on_out(extra, split_of_first, side):
    first = ev.PoolItem(r1.Item(_h(1), "t2", "fact.embellish", "violation", "representative", "c" * 64,
                                "itineraries", "A claim.", FIELD, SOURCE, (), split_of_first), "tp")
    pool = [first, _pi(2, "fp_paraphrase", piece="d")]
    text = "pool text"
    shaped = ["2977dd78", "3018008d", "33835b87"]
    manifest = {"pool_sha256": ts.pool_sha256(text), "excluded_runs": shaped, "excluded_pieces": [], **extra}
    oos = ts.fresh_check(manifest, text, pool, shaped, POOLS)
    assert oos["pool_side"] == side
    why = ts.oos_refusal(oos)
    assert (why is None) if side else ("487bf4847b6136fe" in why and "train-split" in why)


def test_the_pinned_shaping_pools_are_the_s279_and_the_s281_fresh_digests():
    assert POOLS == [pr.S279_POOL] == ts.shaped_on_pools(_insts("v6")) == ts.shaped_on_pools(_insts("v5"))
    assert ts.shaped_on_pools(_insts()) == sorted([pr.S279_POOL, pr.FRESH_POOL])                    # v7


def test_an_exclude_only_oos_never_passes_a_live_run():
    oos = {"kind": "exclude", "shaped_on": ["3018008d"], "excluded_runs": ["3018008d"], "missing": []}
    assert "needs --fresh" in ts.oos_refusal(oos)


# ── stop rule ─────────────────────────────────────────────────────────────────────────────────────────────────────

V5C = "aa1048-r1-v5c@50704a977e52"


def _prior(run, kind, prereg="aa1048-two-step-v5", pool="a" * 16, pv=V5C):
    return {"run_id": run * 8 + "-x", "prereg": prereg, "pool": pool, "run_kind": kind, "prompt_version": pv,
            "first_at": "2026-11-15T10:00:00+00:00", "calls": 3}


def _stop(prior, kind="full", pool="a" * 16):
    return ts.stop_rule(prior, kind=kind, prereg_id="aa1048-two-step-v5", pool=pool, classify_tag=V5C)


def test_stop_rule_allows_the_first_gated_run_and_any_trial():
    assert _stop([])["refused"] is None
    trials = [_prior("1", "trial"), _prior("2", "trial")]
    rec = _stop(trials)
    assert rec["refused"] is None and rec["prior_trials"] == 2 and rec["prior_gated_runs"] == []
    assert _stop([_prior("3", "full")], kind="trial")["refused"] is None
    assert _stop(None, kind="trial")["refused"] is None and _stop(None, kind="trial")["ledger_read"] is None


def test_stop_rule_refuses_a_second_gated_run_of_the_preregistration_on_any_pool():
    rec = _stop([_prior("1", "trial"), _prior("4", "full", pool="b" * 16)])
    assert "one gated run" in rec["refused"] and "44444444" in rec["refused"]
    assert rec["prior_gated_runs"][0]["pool"] == "b" * 16


def test_stop_rule_refuses_a_reroll_under_a_new_preregistration_id_on_the_same_pool():
    assert _stop([_prior("5", "full", prereg="aa1048-two-step-v5-again")])["refused"]
    # another step-1 prompt on another pool under another id is another evaluation
    other = _prior("6", "full", prereg="another", pool="b" * 16, pv="aa1048-r1-v6c@feedfacecafe")
    assert _stop([other])["refused"] is None


def test_stop_rule_refuses_a_gated_run_when_the_ledger_cannot_be_read():
    rec = _stop(None)
    assert rec["ledger_read"] is False and "cannot be read" in rec["refused"]


# ── CLI ───────────────────────────────────────────────────────────────────────────────────────────────────────────


def _pool_file(tmp_path, n_tp=3, n_fp=3):
    rows = [_pi(n, "tp", piece="c") for n in range(n_tp)] + [
        _pi(100 + n, "fp_paraphrase", piece="d") for n in range(n_fp)]
    path = tmp_path / "pool.jsonl"
    path.write_text("".join(json.dumps(p.line()) + "\n" for p in rows), encoding="utf-8")
    manifest = tmp_path / "fresh.json"
    manifest.write_text(json.dumps({"pool_sha256": ts.pool_sha256(path.read_text(encoding="utf-8")),
                                    # another fresh pool: it keeps v7's shaping runs and pools out at piece level
                                    "excluded_runs": ["3018008d", "33835b87", "2977dd78", "9b0d46f8", "c5983e03"],
                                    "excluded_pieces": ["e" * 16], "excluded_pools": [pr.S279_POOL, pr.FRESH_POOL],
                                    "split": "train", "step1_gold": {}}), encoding="utf-8")
    return str(path), str(manifest)


def test_dry_run_reports_the_plan_and_the_checks_without_a_call(tmp_path, monkeypatch, capsys):
    pool, manifest = _pool_file(tmp_path)
    monkeypatch.setattr(ts, "_caller", lambda *a: pytest.fail("a DRY RUN makes no call"))
    monkeypatch.setattr("services.golden_set.llm_guard.install", lambda *a: None)
    assert ts.main(["run", "--pool", pool, "--fresh", manifest, "--limit", "6"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["mode"] == "dry_run" and rep["preregistration"]["matches"] is True
    assert rep["out_of_sample"]["missing"] == [] and rep["subset"]["claims"] == 6
    assert [s["step"] for s in rep["plan"]["steps"]] == ["classify", "judge"]


def test_live_refuses_without_out_of_sample_or_with_another_preregistration(tmp_path, monkeypatch, capsys):
    pool, manifest = _pool_file(tmp_path)
    monkeypatch.setattr(ev, "refresh_settings", lambda: False)
    monkeypatch.setattr(ts, "_caller", lambda *a: pytest.fail("a refused run makes no call"))
    base = ["run", "--pool", pool, "--trial", "2", "--live", "--confirm-spend"]
    assert ts.main(base) == 2 and "needs --fresh" in json.loads(capsys.readouterr().out)["refused"]
    # --exclude of every shaping run report on an older pool is still not out of sample for the pool the panel read
    reports = []
    for run in ("3018008d", "33835b87", "2977dd78"):
        rp = tmp_path / f"{run}.json"
        rp.write_text(json.dumps({"run_id": run + "-0000", "votes": {"f" * 16: [True]}}), encoding="utf-8")
        reports += ["--exclude", str(rp)]
    assert ts.main(base + reports) == 2 and "needs --fresh" in json.loads(capsys.readouterr().out)["refused"]
    other = tmp_path / "prereg.json"
    other.write_text(json.dumps({**ts.load_preregistration(), "votes": 5}), encoding="utf-8")
    assert ts.main(base + ["--fresh", manifest, "--preregistration", str(other)]) == 2
    assert "committed pre-registration only" in json.loads(capsys.readouterr().out)["refused"]
    monkeypatch.setitem(ts.STEP_SETS, "v7", dataclasses.replace(ts.STEP_SETS["v7"], preregistration=other,
                                                                prereg_sha256=ts.file_sha256(other)))
    assert ts.main(base + ["--fresh", manifest]) == 2
    assert "not the pre-registered one" in json.loads(capsys.readouterr().out)["refused"]
    monkeypatch.undo()
    monkeypatch.setattr(ev, "refresh_settings", lambda: False)
    monkeypatch.setattr(ts, "_caller", lambda *a: pytest.fail("a refused run makes no call"))
    held = tmp_path / "held.json"
    held.write_text(json.dumps({**json.loads(open(manifest).read()), "excluded_pools": [], "split": "held_out"}),
                    encoding="utf-8")
    assert ts.main(base + ["--fresh", str(held)]) == 2
    assert "shaped on pool(s)" in json.loads(capsys.readouterr().out)["refused"]
    assert ts.main(["run", "--pool", pool, "--fresh", manifest, "--live", "--confirm-spend"]) == 2
    assert "--trial N or --limit N" in json.loads(capsys.readouterr().out)["refused"]
    assert ts.main(base + ["--fresh", manifest, "--trial", "9"]) == 2                 # over the trial setting


def test_live_trial_runs_both_steps_and_replays_at_zero_cost(tmp_path, monkeypatch, capsys):
    pool, manifest = _pool_file(tmp_path)
    monkeypatch.setattr(ev, "refresh_settings", lambda: False)
    monkeypatch.setattr(ts, "_probe", lambda since: (lambda: 0.0))
    monkeypatch.setattr(ts, "_prior_runs", lambda: pytest.fail("a trial does not read the stop-rule ledger"))

    def caller(inst, system, user, schema):
        if inst.prompt in CLASSIFY:
            return _resp({"set_aside": [], "kind": "new_claim", "new_specifics": ["at sunrise"], "why": "x"})
        return _resp({"verdict": "unsupported", "quote": "", "why": "x"})
    monkeypatch.setattr(ts, "_caller", caller)
    from shared.llm_client import call_log

    rows = []
    monkeypatch.setattr(call_log, "record_call_sync", lambda **kw: rows.append(kw))
    out = tmp_path / "report.json"
    rc = ts.main(["run", "--pool", pool, "--fresh", manifest, "--trial", "3", "--live", "--confirm-spend",
                  "--out", str(out)])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and rep["mode"] == "live" and rep["calls"] == 18 and rep["score"]["verdict"] == "trial"
    assert set(rep["votes"]) == {"classify", "judge"} and "reasons" not in rep and len(rows) == 18
    digest = ts.pool_sha256(open(pool, encoding="utf-8").read())[:16]
    assert rep["run_kind"] == "trial" and rep["pool_sha256"] == digest and rep["stop_rule"]["ledger_read"] is None
    assert {(r["quality_signal"]["prereg"], r["quality_signal"]["run_kind"], r["quality_signal"]["pool"])
            for r in rows} == {("aa1048-two-step-v7", "trial", digest)}
    assert ts.main(["score", "--pool", pool, "--report", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["matches_stored"] is True


def _gated(tmp_path, monkeypatch, prior):
    pool, manifest = _pool_file(tmp_path)
    monkeypatch.setattr(ev, "refresh_settings", lambda: False)
    monkeypatch.setattr(ts, "_probe", lambda since: (lambda: 0.0))
    monkeypatch.setattr(ts, "_prior_runs", lambda: prior)
    calls = []

    def caller(inst, system, user, schema):
        calls.append(inst.prompt)
        return _resp({"kind": "restatement", "new_specifics": [], "why": "x"})
    monkeypatch.setattr(ts, "_caller", caller)
    from shared.llm_client import call_log

    rows = []
    monkeypatch.setattr(call_log, "record_call_sync", lambda **kw: rows.append(kw))
    rc = ts.main(["run", "--pool", pool, "--fresh", manifest, "--limit", "6", "--live", "--confirm-spend"])
    return rc, calls, rows, pool


def test_a_gated_run_refuses_when_the_ledger_holds_one_of_this_preregistration(tmp_path, monkeypatch, capsys):
    rc, calls, rows, _ = _gated(tmp_path, monkeypatch, [_prior("7", "full", prereg="aa1048-two-step-v7",
                                                             pool="c" * 16, pv=pr.get("v7c").tag)])
    assert rc == 2 and calls == [] and rows == []
    assert "one gated run" in json.loads(capsys.readouterr().out)["refused"]


def test_a_gated_run_refuses_when_the_ledger_cannot_be_read(tmp_path, monkeypatch, capsys):
    rc, calls, _, _ = _gated(tmp_path, monkeypatch, None)
    assert rc == 2 and calls == [] and "cannot be read" in json.loads(capsys.readouterr().out)["refused"]


def test_the_first_gated_run_runs_and_marks_every_ledger_row_full(tmp_path, monkeypatch, capsys):
    rc, calls, rows, pool = _gated(tmp_path, monkeypatch, [_prior("8", "trial", prereg="aa1048-two-step-v7")])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and rep["run_kind"] == "full" and rep["stop_rule"]["prior_trials"] == 1
    assert calls == ["v7c"] * 18 and len(rows) == 18                                 # all restated: no step 2
    digest = ts.pool_sha256(open(pool, encoding="utf-8").read())[:16]
    assert {(r["quality_signal"]["run_kind"], r["quality_signal"]["pool"]) for r in rows} == {("full", digest)}
