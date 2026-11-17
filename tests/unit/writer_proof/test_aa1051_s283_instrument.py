"""AA-1051 (S283) — the W5 judge on the pinned AA-1048 claim instrument (R1 fire -> two-step v7, ADVISORY): the pinned
registration, the claims of a labelled output, one output judged through a FAKE caller (no model, no DB), the run cap
and identity guards, R2's instrument path (no kappa block, the whole fixed sample, every flagged claim kept), the
sample rule of a pre-registration, the per-output price, the chain power and the runner's plan. Invented text only."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from services.ground_truth import two_step_sets
from services.writer_proof import instrument as wi
from services.writer_proof import power, price, runner
from services.writer_proof import prereg as pr
from services.writing_memory import config as cfg
from services.writing_memory import eval_judge as ej
from services.writing_memory import r2
from services.writing_memory import r2_w5
from tests.unit.test_aa1051_r2_split_prereg import PreregStore, _noop, _store_with_sides
from tests.unit.writer_proof import test_aa1051_runner as base
from tests.unit.writer_proof.test_aa1051_runner import _Pool

fakes = base.fakes
IID = "aa1048-two-step-v7"
MODEL, CONN = "gpt-6-luna-openai", "openai-direct"
SOURCE = "Day 1: we visit the river market by boat and walk the old quarter."
BAD = "The boat ride includes a free lunch on the island."
OUTPUT = (f"SEO TITLE:\nRiver market day by boat\n\nBODY:\nWe visit the river market by boat. {BAD}\n"
          "Then we walk the old quarter together.")


# ── the pinned instrument ────────────────────────────────────────────────────────────────────────────────────────

def test_pinned_reads_the_v7_registration_and_names_the_whole_chain():
    p = wi.pinned(IID)
    assert (p.model_key, p.connection, p.votes, p.step_set) == (MODEL, CONN, 3, "v7")
    assert p.tags["classify"].startswith("aa1048-r1-v7c@") and p.tags["judge"].startswith("aa1048-r1-v7j@")
    assert p.tags["fire"].startswith("aa1048-r1-v1@")               # the extraction prompt the fresh pools fired with
    assert p.judge_version.startswith(f"{IID}:") and len(p.judge_version) == len(IID) + 13
    assert set(wi.instruments(p)) == {"fire", "classify", "judge"}
    with pytest.raises(wi.InstrumentRefused, match="unknown claim instrument"):
        wi.pinned("aa1048-two-step-v9")


def test_a_closed_step_set_cannot_be_pinned(monkeypatch):
    monkeypatch.setitem(wi.PINNED, "zz-closed", "v6")               # v6 is closed in two_step_sets
    wi.pinned.cache_clear()
    try:
        with pytest.raises(wi.InstrumentRefused, match="closed"):
            wi.pinned("zz-closed")
    finally:
        wi.pinned.cache_clear()
    assert two_step_sets.STEP_SETS["v6"].closed


@pytest.mark.parametrize("judge,needle", [
    ({"model_key": "zz-other", "connection": CONN, "instrument": IID, "advisory": True}, "must name it"),
    ({"model_key": MODEL, "connection": CONN, "instrument": IID}, "advisory only"),
    ({"model_key": MODEL, "connection": CONN, "instrument": "zz-nope", "advisory": True}, "unknown claim"),
    ({"model_key": MODEL, "connection": CONN, "advisory": True}, "only for a judge.instrument"),
])
def test_judge_problems(judge, needle):
    assert any(needle in p for p in wi.judge_problems(judge))


def test_judge_problems_accept_the_pinned_advisory_judge_and_a_plain_one():
    assert wi.judge_problems({"model_key": MODEL, "connection": CONN, "instrument": IID, "advisory": True}) == []
    assert wi.judge_problems({"model_key": MODEL, "connection": CONN}) == []


# ── the claims of one output ─────────────────────────────────────────────────────────────────────────────────────

def test_output_fields_map_the_labels_back_to_the_output_keys():
    text = "SEO TITLE:\nA b c\n\nMETA DESCRIPTION:\nShort lines here\n\nHASHTAGS:\n#river\n\nBODY:\nOne two three."
    assert [f for f, _ in wi.output_fields(text)] == ["seo_title", "meta_description", "hashtags", "content_text"]
    assert wi.output_fields("No label at all here.") == [("content_text", "No label at all here.")]
    assert wi.output_shape("t9_blog", OUTPUT) == {"fields": 2, "claims": 4}


def test_output_claims_are_distinct_sentences_with_the_source():
    items = wi.output_claims("t9_blog", SOURCE, OUTPUT + " " + BAD)
    assert sum(1 for i in items if i.claim_text == BAD) == 1          # the same sentence twice in a field: once
    assert all(i.source_text == SOURCE and i.writer == "t9_blog" for i in items)


# ── one output through a fake caller ─────────────────────────────────────────────────────────────────────────────

def _resp(payload: dict, *, model=MODEL, conn=CONN, cost=0.0005):
    return SimpleNamespace(content=json.dumps(payload), model_used=model, connection_key=conn, cost_usd=cost,
                           input_tokens=1000, output_tokens=100, provider="openai", stop_reason="end_turn")


def _caller(*, model=MODEL, fail_on=None):
    """Fire flags BAD when its field is in the prompt; step 1 calls BAD a new claim, the rest restatements; step 2
    finds BAD unsupported. `fail_on` = a step that raises (a caller error)."""
    calls = []

    def call(inst, system, user, schema):
        step = "fire" if inst.mode == "extraction" else ("classify" if inst.prompt.endswith("c") else "judge")
        calls.append(step)
        if step == fail_on:
            raise RuntimeError("route exhausted")
        if step == "fire":
            return _resp({"claims": [{"quote": BAD, "class": "zz", "why": "x"}] if BAD in user else []}, model=model)
        if step == "classify":
            new = "free lunch" in user
            return _resp({"kind": "new_claim" if new else "restatement", "new_specifics": ["free lunch"] if new else [],
                          "why": "x"}, model=model)
        return _resp({"verdict": "unsupported", "quote": "", "why": "the source names no lunch"}, model=model)
    call.calls = calls
    return call


def _judge(caller, *, cap=1.0, spent=0.0):
    logged = []

    async def spend(pool, run_id, stages):
        return spent

    async def log(resp, inst, signal, parsed, error):
        logged.append((signal, parsed, error))
    v = asyncio.run(wi.judge_output(None, writer="t9_blog", source_text=SOURCE, output_text=OUTPUT, run_id="run-zz",
                                    item_id="item-zz", arm="candidate", instrument=IID, cap_usd=cap,
                                    stages=(cfg.W5_STAGE,), caller=caller, spend=spend, log=log))
    return v, logged


def test_judge_output_fires_then_runs_the_two_steps_on_the_fired_claim_only():
    caller = _caller()
    v, logged = _judge(caller)
    assert v.verdict == "fail" and v.failure_codes == [] and len(v.claims) == 1
    assert v.claims[0]["claim"] == BAD and v.claims[0]["class"] == wi.CLAIM_CLASS
    assert v.counts == {"claims": 4, "fired": 1, "fire_unverified": 0, "routed": 1, "flagged": 1, "unverified": 0,
                        "calls": 12}
    assert caller.calls.count("fire") == 6 and caller.calls.count("classify") == 3 and caller.calls.count("judge") == 3
    assert v.score == 7.5 and v.max_claims == 1 and v.judge_version == wi.pinned(IID).judge_version
    assert (v.model_key, v.connection) == (MODEL, CONN) and ej.identity(v) == (MODEL, CONN)
    assert v.cost_usd == pytest.approx(12 * 0.0005)
    tags = {s["prompt_version"] for s, _p, _e in logged}
    assert tags == set(wi.pinned(IID).tags.values()) and all(s["issue"] == "AA-1051" for s, _p, _e in logged)


def test_a_clean_output_passes():
    def clean(inst, system, user, schema):
        return _resp({"claims": []} if inst.mode == "extraction" else {"kind": "restatement", "new_specifics": [],
                                                                         "why": "x"})
    v, _ = _judge(clean)
    assert v.verdict == "pass" and v.score == 10.0 and v.counts["fired"] == 0 and v.counts["calls"] == 6


def test_another_model_answering_is_an_identity_mismatch():
    with pytest.raises(ej.JudgeIdentityMismatch, match="pinned to gpt-6-luna-openai"):
        _judge(_caller(model="zz-other-model"))


def test_the_run_cap_stops_before_a_call_that_could_pass_it():
    caller = _caller()
    with pytest.raises(wi.InstrumentBudgetStop, match="run cap"):
        _judge(caller, cap=1.0, spent=0.99999)
    assert caller.calls == []                                        # nothing was called


def test_a_caller_error_stops_the_output():
    with pytest.raises(wi.InstrumentBudgetStop, match="caller error"):
        _judge(_caller(fail_on="classify"))


def test_judge_fn_binds_the_instrument_and_refuses_a_run_that_is_not_pre_registered():
    conf = _conf()
    fn = wi.judge_fn(conf)
    assert fn.func is wi.judge_output and fn.keywords["instrument"] == IID and fn.keywords["cap_usd"] == 4.0
    with pytest.raises(r2.R2Refused, match="pre-registered"):
        wi.judge_fn(r2.R2Config(writer="t9_blog", judge_model=MODEL, judge_connection=CONN, judge_instrument=IID))


# ── R2's instrument path ─────────────────────────────────────────────────────────────────────────────────────────

def _conf(**over) -> r2.R2Config:
    kw = dict(writer="t9_blog", cap_usd=4.0, eval_split="gate", sample_ids=["s-100"], prereg_id="w5-null-zz",
              prereg_sha256="a" * 64, prereg_workflow="null", prereg_arm="B", judge_model=MODEL,
              judge_connection=CONN, judge_instrument=IID, log_stage=cfg.W5_STAGE)
    kw.update(over)
    return r2.R2Config(**kw)


@pytest.mark.parametrize("over,needle", [
    ({"judge_model": "zz-judge"}, "runs on gpt-6-luna-openai@openai-direct"),
    ({"judge_instrument": "zz-nope"}, "unknown claim instrument"),
])
def test_r2_config_refuses_a_wrong_instrument_pin(over, needle):
    with pytest.raises(r2.R2Refused, match=needle):
        _conf(**over).validate()


def test_r2_config_with_the_instrument_validates_and_round_trips():
    c = _conf()
    c.validate()
    assert r2.config_from_payload(r2.payload_of(c)) == c
    assert r2.judge_fn_for(c).func is wi.judge_output


def test_r2_helpers_of_an_instrument_run():
    items = [{"item_id": "i1", "trap": False, "label_verdict": "fail", "control_output": {"content_text": "x"}}]
    assert r2.calibration_items(items) and r2.calibration_items(items, IID) == []
    ready = r2.calibration_ready([], {}, IID)
    assert ready["possible"] and ready["instrument"] == IID and not r2.calibration_ready([], {})["possible"]
    assert r2.prompt_version("t9_blog", None, MODEL, IID).startswith(f"t9_blog:{IID}:")
    assert r2.tune_step(_conf(), {}, "non_inferior", False) == r2.PREREG_FULL_SAMPLE
    assert r2.tune_step(r2.R2Config(writer="a1"), {}, "non_inferior", False) == "informative"
    assert r2_w5._max_claims(SimpleNamespace(max_claims=20)) == 20 and r2_w5._max_claims(SimpleNamespace()) == 8


def _instrument_judge(n_claims=10):
    async def judge(pool, *, writer, source_text, output_text, run_id, item_id, arm):
        claims = tuple({"class": wi.CLAIM_CLASS, "claim": f"zz claim {k}", "why": "x"} for k in range(n_claims))
        return wi.InstrumentVerdict(verdict="fail", score=5.0, failure_codes=[], model=MODEL, cost_usd=0.0,
                                    judge_version=wi.pinned(IID).judge_version, claims=claims, model_key=MODEL,
                                    connection=CONN, max_claims=n_claims)
    return judge


def test_an_instrument_run_skips_the_kappa_block_and_judges_the_whole_fixed_sample():
    """No calibration rows, no stop after calibrate or tune (the pre-registered decision reads every item), R2's own
    verdict informative, and every flagged claim kept on the row (fail_claims_per_item counts them all)."""
    store = _store_with_sides()
    gate = sorted(it["sample_id"] for it in store._items if it.get("eval_split") == "gate")
    conf = _conf(writer="a1", sample_ids=gate)
    from tests.unit.test_aa919_r2_runner import FakeWriter
    out = asyncio.run(r2.run_r2(store, conf, writer_fn=FakeWriter(store), judge_fn=_instrument_judge(),
                                flush=_noop))
    assert not out.judge_counts and out.verdict == "informative" and out.stopped_phase == "holdout"
    rows = store.results
    assert not [r for r in rows if (r["details"] or {}).get("calibration") == "only"]
    cand = {r["item_id"] for r in rows if r["arm"] == "candidate" and r["mode"] == "regenerate"}
    planned = {it["item_id"] for it in store._items if it.get("sample_id") in gate}
    assert planned <= cand                                           # tune AND holdout items were written
    assert all(len(r["details"]["judge_claims"]) == 10 for r in rows if r["verdict"] == "fail")
    run_row = next(iter(store.runs.values()))
    assert run_row["details"]["judge"]["instrument"] == IID and IID in run_row["prompt_version"]
    assert run_row["details"]["calibration_items"] == 0


def test_an_instrument_run_still_refuses_a_null_run_that_is_not_null():
    items = _store_with_sides()._items
    gate = sorted(it["sample_id"] for it in items if it.get("eval_split") == "gate")
    store = PreregStore(items, control_fp={"golden_set": "aa729@1.1.0"})
    with pytest.raises(r2.R2Refused, match="not null by construction"):
        asyncio.run(r2.run_r2(store, _conf(writer="a1", sample_ids=gate), writer_fn=lambda *a, **k: None,
                              judge_fn=_instrument_judge(), flush=_noop))


# ── the pre-registration: the shipped v7 null run and the sample rule ────────────────────────────────────────────

def test_the_registered_v7_null_run_loads_from_the_lock():
    pre = pr.load("w5-null-t9-blog-v7")
    assert pre.registered and pr.read_lock()[pre.id] == pre.sha256
    assert pre.doc["judge"] == {"model_key": MODEL, "connection": CONN, "instrument": IID, "advisory": True}
    assert pre.doc["sample"] == {"rule": "seeded_hash", "n": 12, "noise": 0} and pre.doc["sample_ids"] == []
    assert pre.doc["decision"]["metric"] == "fail_claims_per_item" and pre.doc["cap_usd"] <= pr.workflow_cap("null")
    assert pre.doc["power"]["writer_sd"] is not None                  # S283 review: never a judge-only upper bound
    assert runner.CAMPAIGN_ORDER[0] == pre.id


def _rule_doc(**over):
    d = json.loads(pr.path_for("w5-null-t9-blog-v7").read_text())
    d.update(over)
    return d


@pytest.mark.parametrize("over,needle", [
    ({"sample": {"rule": "seeded_hash", "n": 5}}, ">= 12 samples"),
    ({"sample": {"rule": "zz", "n": 12}}, "sample must be"),
    ({"sample": {"rule": "seeded_hash", "n": 0}}, "positive integer"),
    ({"sample": {"rule": "seeded_hash", "n": 12, "noise": 13}}, "sample.noise"),
    ({"sample_ids": ["zz-gate-01"]}, "leaves sample_ids"),
])
def test_sample_rule_problems(over, needle):
    assert any(needle in p for p in pr.problems(_rule_doc(**over)))


def _items(n=20):
    out = [{"sample_id": f"zz-gate-{i:02d}", "writer": "t9_blog", "trap": False, "eval_split": "gate"}
           for i in range(n)]
    return out + [{"sample_id": "zz-dev-01", "writer": "t9_blog", "trap": False, "eval_split": "dev"},
                  {"sample_id": "zz-trap-01", "writer": "t9_blog", "trap": True, "eval_split": "gate"}]


def test_resolve_draws_what_draft_would_and_keeps_the_sha():
    pre = pr.load("w5-null-t9-blog-v7")
    got = pr.resolve(pre, _items())
    assert got.sha256 == pre.sha256 and "sample" not in got.doc and pr.problems(got.doc) == []
    assert got.doc["sample_ids"] == pr.pick_sample([f"zz-gate-{i:02d}" for i in range(20)], 12, seed=pre.id)
    assert got.doc["sample_resolved"] == {"rule": "seeded_hash", "gate_items_available": 20, "asked": 12,
                                          "picked": 12}
    assert pr.resolve(got, _items(3)) is got                         # resolving twice changes nothing
    drafted = pr.draft(pre.doc, items=_items(), golden_version="1.4.0")
    assert drafted["sample_ids"] == got.doc["sample_ids"] and "sample" not in drafted


def test_same_instrument_tells_a_claim_instrument_from_its_bare_model():
    v7 = pr.load("w5-null-t9-blog-v7")
    bare = pr.from_doc({**v7.doc, "judge": {"model_key": MODEL, "connection": CONN}, "status": "draft"})
    assert runner.same_instrument(v7, v7) and not runner.same_instrument(v7, bare)


# ── price ────────────────────────────────────────────────────────────────────────────────────────────────────────

MEASURED_ROWS = [{"tag": t, "calls": 100, "mean_usd": usd} for t, usd in
                 ((wi.pinned(IID).tags["fire"], 0.0008), (wi.pinned(IID).tags["classify"], 0.0005),
                  (wi.pinned(IID).tags["judge"], 0.0003))]


def test_output_cost_arithmetic():
    costs = {"fire": {"usd": 0.001}, "classify": {"usd": 0.0005}, "judge": {"usd": 0.0003}}
    got = price.output_cost({"fields": 3, "claims": 50}, costs, votes=3, fire_share=0.2, margin=1.3)
    assert got == pytest.approx((3 * 3 * 0.001 + 50 * 0.2 * 3 * 0.0008) * 1.3)


def test_step_costs_use_the_ledger_mean_else_the_model_estimate():
    p = wi.pinned(IID)
    insts = wi.instruments(p)
    measured = {r["tag"]: r for r in MEASURED_ROWS}
    led = price.step_costs(p, insts, measured, min_samples=5, sample_user={})
    assert {s: c["source"] for s, c in led.items()} == dict.fromkeys(wi.STEPS, "ledger_mean")
    assert led["fire"]["usd"] == 0.0008
    few = price.step_costs(p, insts, {k: {**v, "calls": 2} for k, v in measured.items()}, min_samples=5,
                           sample_user={"fire": "x" * 3000, "classify": "x" * 3000, "judge": "x" * 3000})
    assert all(c["source"] == "model" and c["usd"] > led[s]["usd"] for s, c in few.items())


def test_instrument_output_is_the_mean_over_the_sample_outputs():
    p = wi.pinned(IID)
    measured = {r["tag"]: r for r in MEASURED_ROWS}
    got = price.instrument_output(p, wi.instruments(p), "t9_blog", [OUTPUT, OUTPUT], measured, min_samples=5,
                                  margin=1.3)
    one = price.output_cost({"fields": 2, "claims": 4}, got["steps"], votes=3, fire_share=0.278, margin=1.3)
    assert got["usd"] == pytest.approx(one) and got["outputs"] == 2 and got["pricing"] == "ledger_mean"
    assert got["fire_share"] == 0.278 and got["fire_share_source"] == wi.FIRE_SHARE_SOURCE
    assert price.instrument_output(p, wi.instruments(p), "a1", [OUTPUT], measured, min_samples=5,
                                   margin=1.3)["fire_share"] == 1.0           # no measurement: every claim planned
    assert price.instrument_output(p, wi.instruments(p), "t9_blog", [], measured, min_samples=5,
                                   margin=1.3)["usd"] is None


# ── power ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _h(i: int) -> str:
    return f"{i:02x}" * 32                                            # an invented claim hash, distinct prefixes


def _fire_votes():
    return {_h(i): ([True, True, True] if i < 10 else [False, False, False]) for i in range(40)}


def _step_votes():
    cls = {_h(i)[:16]: [True, True, True] for i in range(5)}
    jdg = {_h(i)[:16]: ([True, True, True] if i < 3 else [False, False, False]) for i in range(5)}
    return {"classify": cls, "judge": jdg}


def test_chain_rates_multiply_the_fire_and_the_two_step_chances():
    rates = dict((h, m) for h, m, _n in power.chain_rates(_fire_votes(), _step_votes()))
    assert rates[_h(0)] == 1.0 and rates[_h(4)] == 0.0                         # judged: flagged / cleared
    assert rates[_h(7)] == pytest.approx(0.6)                                 # fired, not judged: the mean
    assert rates[_h(30)] == 0.0                                               # the fire cleared it


def test_prereg_power_of_a_claim_instrument_needs_the_two_step_report():
    pre = pr.load("w5-null-t9-blog-v7")
    fire = {"votes": {wi.fire_instrument().id: _fire_votes()}}
    with pytest.raises(power.PowerRefused, match="two-step"):
        power.prereg_power(pre, fire)
    out = power.prereg_power(pre, fire, two_step_report={"votes": _step_votes()})
    assert out["n_items"] == 12 and out["metric"] == "fail_claims_per_item" and out["claims"] == 40
    with pytest.raises(power.PowerRefused, match="no step votes"):
        power.prereg_power(pre, fire, two_step_report={"votes": {}})


def _flat_rates(m: float, n: int = 480):
    return [(_h(i % 250) + f"{i:04d}", m, 3) for i in range(n)]


def test_writer_sd_floor_is_the_independence_spread_of_a_count():
    rates = [(_h(i), (0.0 if i % 2 else 0.5), 3) for i in range(100)]          # Var(m) = 0.0625
    got = power.writer_sd_floor(rates, metric="fail_claims_per_item", claims_per_item=60)
    assert got == pytest.approx((2 * 60 * 0.0625) ** 0.5)
    assert power.writer_sd_floor(rates, metric="item_fail_rate", claims_per_item=60) is None
    assert power.writer_sd_floor(rates[:1], metric="fail_claims_per_item", claims_per_item=60) is None
    assert power.writer_sd_floor(_flat_rates(0.2), metric="fail_claims_per_item", claims_per_item=60) == 0.0


def test_a_stated_writer_sd_below_the_floor_is_never_adequate():
    rates = [(_h(i), (0.0 if i % 2 else 0.5), 3) for i in range(240)]          # floor about 2.74
    kw = dict(n_items=12, metric="fail_claims_per_item", claims_per_item=60, rates=rates)
    low = power.check({}, margin_abs=50.0, writer_sd=1.0, **kw)
    assert low["power"] >= 0.8 and low["writer_sd_below_floor"] and not low["adequate"]
    ok = power.check({}, margin_abs=50.0, writer_sd=3.0, **kw)
    assert ok["adequate"] and not ok["writer_sd_below_floor"] and ok["bound"] == "judge noise + assumed writer_sd"
    draft = power.check({}, margin_abs=50.0, **kw)                              # a draft: the judge-only upper bound
    assert draft["adequate"] and draft["bound"].startswith("upper bound")


def test_the_plan_names_a_writer_spread_below_the_floor_and_an_underpowered_file():
    base = {"checked": True, "writer_sd": 1.0, "writer_sd_floor": 2.7, "writer_sd_below_floor": True, "power": 0.5,
            "min_power": 0.8, "rule": "non_inferior", "true_effect": 0.0, "n_items": 12, "n_needed": 30}
    got = runner._power_blocking(base)
    assert len(got) == 2 and "below the writer spread" in got[0] and got[1].startswith("underpowered")
    assert runner._power_blocking({**base, "writer_sd_below_floor": False, "power": 0.9}) == []
    assert runner._power_blocking({"checked": False, "why": "no R1 report"}) == ["power not checked: no R1 report"]


def test_a_registered_file_states_the_writer_spread():
    d = _rule_doc()
    d["power"] = {**d["power"], "writer_sd": None}
    assert any("states power.writer_sd" in p for p in pr.problems(d))
    assert not any("writer_sd" in p for p in pr.problems({**d, "status": "draft"}))


def test_the_v7_margin_holds_power_with_the_writer_spread():
    """The registered margin is powered with the stated writer spread, not on the re-judge noise alone: at the
    measured chain re-judge variance (about 4.31 claims^2 a blog, v7 fire + gated runs), flat rates reproduce it."""
    pre = pr.load("w5-null-t9-blog-v7")
    m = 0.0786                                                                 # 60 m (1 - m) = 4.345
    out = power.check_prereg(pre, {}, rates=_flat_rates(m))
    assert out["judge_var_per_item"] == pytest.approx(4.3, abs=0.1) and out["writer_sd"] == 4.6
    assert out["adequate"] and out["min_resolvable_margin"] <= pre.doc["decision"]["margin_abs"]
    old = power.check({}, n_items=12, margin_abs=2.5, metric="fail_claims_per_item", claims_per_item=60,
                      writer_sd=4.6, rates=_flat_rates(m))
    assert not old["adequate"]                                                 # the first margin: about 0.48


# ── the runner's plan of the registered v7 null run ──────────────────────────────────────────────────────────────

def test_plan_of_the_v7_null_run_resolves_the_sample_and_prices_the_instrument(fakes, tmp_path, monkeypatch):
    ctl = {"seo_title": "River market day by boat", "content_text": f"We visit the river market by boat. {BAD}"}

    async def items(self, set_id, writer, tenant):
        return [{**it, "input": {}, "control_output": ctl} for it in _items()]
    monkeypatch.setattr(r2.PgStore, "items", items)
    monkeypatch.setattr(ej, "evidence_view", lambda w, i, o, s=None: SimpleNamespace(output=OUTPUT, source=SOURCE))
    pool = _Pool()
    pool.conn.instrument_rows = MEASURED_ROWS
    fire, ts = tmp_path / "fire.json", tmp_path / "ts.json"
    fire.write_text(json.dumps({"votes": {wi.fire_instrument().id: _fire_votes()}}))
    ts.write_text(json.dumps({"votes": _step_votes()}))
    out = asyncio.run(runner.plan(pool, pr.load("w5-null-t9-blog-v7"), r1_report=str(fire),
                                  two_step_report=str(ts)))
    assert len(out["sample_ids"]) == 12 and out["sample_resolved"]["picked"] == 12
    conf = fakes["estimates"][-1]
    assert conf.judge_instrument == IID and conf.sample_ids == out["sample_ids"]
    judge = out["price_check"]["judge"]
    assert judge["instrument"] == IID and judge["call"]["outputs"] == 12 and judge["call"]["pricing"] == "ledger_mean"
    assert out["power"]["checked"] and out["power"]["metric"] == "fail_claims_per_item"
    assert not [b for b in out["blocking"] if "instrument" in b or "sample rule" in b]


def test_log_row_writes_the_billed_reply_and_an_unusable_one(monkeypatch):
    from shared.llm_client import call_log
    seen = []

    async def record(pool, **kw):
        seen.append(("ok", kw))

    async def unusable(resp, **kw):
        seen.append(("unusable", kw))
    monkeypatch.setattr(call_log, "record_call_with_pool", record)
    monkeypatch.setattr(call_log, "record_unusable", unusable)
    inst = wi.instruments(wi.pinned(IID))["classify"]
    signal = {"prompt_version": inst.prompt_version.tag, "issue": "AA-1051"}
    asyncio.run(wi._log_row(None, _resp({"kind": "restatement"}), inst, signal, {"kind": "restatement"}, None))
    asyncio.run(wi._log_row(None, _resp({}), inst, signal, None, ValueError("not json")))
    (kind, ok), (kind2, bad) = seen
    assert kind == "ok" and ok["stage"] == "adhoc_aa1048" and ok["role"] == "judge" and ok["model"] == MODEL
    assert ok["quality_signal"]["json_parsed"] is True and "pricing_mode" in ok          # **billed_fields(resp)
    assert kind2 == "unusable" and bad["quality_signal_extra"] == signal
