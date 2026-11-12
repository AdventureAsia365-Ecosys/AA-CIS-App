"""AA-1051 — the R2 runner reads the frozen DEV / CAL / GATE split, a fixed sample and a pre-registration (fake store,
fake writer and judge from the AA-919 runner tests: no DB, no model). Invented text only."""
from __future__ import annotations

import asyncio
import json

import pytest

from services.writing_memory import config as cfg
from services.writing_memory import r2
from tests.unit.test_aa919_r2_runner import FakeStore, golden, make_judge, run

SHA = "a" * 64


def _sided(items, sides):
    """Give each non-trap item a side, cycling through `sides`."""
    k = 0
    for it in items:
        if not it["trap"]:
            it["eval_split"] = sides[k % len(sides)]
            k += 1
    return items


def _fp(**parts):
    """A run fingerprint with a config_fp (invented component digests)."""
    base = {c: f"sha-{c}" for c in ("routes", "stage_params", "catalog", "route_control", "mode_flags", "prompts",
                                    "memory_items", "l0_lexicon", "brand_config", "policy")}
    base.update(parts)
    return {"gate_taxonomy": "qtax-v1", "golden_set": "aa729@1.1.0",
            "writer_route": {"stage": "zz_write", "model_id": "zz-model", "fallbacks": [], "account_route": None},
            "config_fp": {"service": "api", "parts": base}}


class PreregStore(FakeStore):
    def __init__(self, items, prior=(), control_fp=None, now_fp=None, **kw):
        super().__init__(items, **kw)
        self.prior = list(prior)
        self.asked = []
        self.control_fp = _fp() if control_fp is None else control_fp
        self.now_fp = _fp() if now_fp is None else now_fp

    async def prereg_runs(self, prereg_id):
        self.asked.append(prereg_id)
        return list(self.prior)

    async def run_fingerprint(self, run_id):
        return self.control_fp

    async def fingerprint(self, writer):
        return self.now_fp


def _conf(**over):
    kw = dict(writer="a1", cap_usd=5.0)
    kw.update(over)
    return r2.R2Config(**kw)


def _w5(**over):
    kw = dict(eval_split="gate", sample_ids=["s-100"], prereg_id="w5-null-t9-blog", prereg_sha256=SHA,
              prereg_workflow="null", prereg_arm="B", judge_model="zz-judge", log_stage=cfg.W5_STAGE)
    kw.update(over)
    return _conf(**kw)


# ── config ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_w5_config_validates_and_round_trips_through_the_job_payload():
    c = _w5()
    c.validate()
    assert r2.config_from_payload(r2.payload_of(c)) == c


@pytest.mark.parametrize("over,needle", [
    ({"eval_split": "holdout"}, "eval_split"),
    ({"sample_ids": []}, "sample_ids"),
    ({"sample_ids": ["S 1"]}, "sample_ids"),
    ({"sample_ids": ["s-1", "s-1"]}, "sample_ids"),
    ({"prereg_sha256": "nothex"}, "prereg_sha256"),
    ({"prereg_arm": "C"}, "prereg_arm"),
    ({"judge_model": None}, "pinned judge"),
    ({"sample_ids": None}, "fixed sample"),
    ({"log_stage": "adhoc_aa900"}, "adhoc_aa1051"),
    ({"seed_fault": "qa_trace"}, "calibrated r2"),
    ({"require_calibrated": False}, "calibrated r2"),
])
def test_w5_config_refusals(over, needle):
    with pytest.raises(r2.R2Refused, match=needle):
        _w5(**over).validate()


def test_w5_stage_is_reserved_for_pre_registered_runs():
    with pytest.raises(r2.R2Refused, match="pre-registered W5"):
        _conf(log_stage=cfg.W5_STAGE).validate()
    assert cfg.W5_STAGE not in cfg.MEMORY_STAGES and cfg.W5_STAGE in cfg.EVAL_LOG_STAGES


def test_cli_flags():
    conf, _ = r2._parse(["--writer", "a1", "--eval-split", "gate", "--sample-ids", "s-100,s-101"])
    assert conf.eval_split == "gate" and conf.sample_ids == ["s-100", "s-101"]


# ── pure helpers ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_select_items_side_and_sample():
    items = _sided(golden(), ["dev", "cal", "gate"])
    pool, problems = r2.select_items(items, eval_split="gate", sample_ids=None)
    assert not problems and pool
    assert all(it["trap"] or it["eval_split"] == "gate" for it in pool)
    assert sum(1 for it in pool if it["trap"]) == 2                     # traps stay for the probe
    gate_ids = sorted(it["sample_id"] for it in pool if not it["trap"])
    pool2, p2 = r2.select_items(items, eval_split="gate", sample_ids=gate_ids[:2])
    assert not p2 and sorted(it["sample_id"] for it in pool2) == gate_ids[:2]
    dev_id = next(it["sample_id"] for it in items if it.get("eval_split") == "dev")
    _, p3 = r2.select_items(items, eval_split="gate", sample_ids=[dev_id, "s-999"])
    assert any("not on the gate side" in p for p in p3) and any("not in the golden set" in p for p in p3)


def test_select_items_refuses_a_set_without_split_rows():
    _, problems = r2.select_items(golden(), eval_split="cal", sample_ids=None)
    assert problems and "no split rows" in problems[0]
    pool, none = r2.select_items(golden(), eval_split=None, sample_ids=None)    # no side asked: unchanged
    assert not none and len(pool) == len(golden())


def test_calibration_split_counts_only_cal():
    items = _sided(golden(), ["cal", "gate"])
    calib = r2.calibration_items(items)
    planned = [it for it in items if it.get("eval_split") == "gate"]
    judged, counting = r2.calibration_split(calib, planned, "gate")
    assert counting == {it["item_id"] for it in calib if it["eval_split"] == "cal"}
    assert {it["item_id"] for it in judged} >= counting and len(judged) > len(counting)
    same, all_ids = r2.calibration_split(calib, planned, None)
    assert same == calib and all_ids == {it["item_id"] for it in calib}


def test_prereg_refusal():
    c = _w5()
    assert r2.prereg_refusal(c, []) is None
    assert r2.prereg_refusal(c, [{"run_id": "x", "sha256": "b" * 64, "results": 0}]) is None
    assert r2.prereg_refusal(c, [{"run_id": "x", "sha256": SHA, "results": 3}]) is None
    assert "edited after its first result" in r2.prereg_refusal(c, [{"run_id": "x", "sha256": "b" * 64,
                                                                    "results": 1}])


def test_prereg_refusal_refuses_a_rerun_of_an_arm_with_results():
    c = _w5()
    assert "already has a run with results" in r2.prereg_refusal(
        c, [{"run_id": "x", "sha256": SHA, "arm": "B", "results": 3}])
    assert r2.prereg_refusal(c, [{"run_id": "x", "sha256": SHA, "arm": "B", "results": 0}]) is None
    assert r2.prereg_refusal(c, [{"run_id": "x", "sha256": SHA, "arm": "A_prime", "results": 3}]) is None


def test_null_config_problems():
    assert r2.null_config_problems(_fp(), _fp()) == []
    assert "no writer configuration" in r2.null_config_problems({"golden_set": "aa729@1.1.0"}, _fp())[0]
    assert "no writer configuration" in r2.null_config_problems(None, _fp())[0]
    assert r2.null_config_problems(_fp(prompts="sha-other"), _fp()) == ["config_fp prompts differs from the control's"]
    assert any("cannot be compared" in p for p in r2.null_config_problems(_fp(routes="unavailable"), _fp()))
    missing = _fp()
    del missing["config_fp"]["parts"]["policy"]
    assert any("policy cannot be compared" in p for p in r2.null_config_problems(missing, _fp()))
    other = {**_fp(), "golden_set": "aa729@1.0.0"}
    assert r2.null_config_problems(other, _fp()) == ["golden_set differs from the control's"]
    # mode_flags: compared only by the worker against a worker record (an exec session does not know its env)
    w_old, w_new = _fp(mode_flags="a"), _fp(mode_flags="b")
    w_old["config_fp"]["service"] = w_new["config_fp"]["service"] = "worker"
    assert r2.null_config_problems(w_old, w_new) == ["config_fp mode_flags differs from the control's"]
    assert r2.null_config_problems(_fp(mode_flags="a"), _fp(mode_flags="b")) == []


def test_kappa_by_side():
    recs = ([{"label": "pass", "judge": "pass", "eval_split": "cal"}] * 4
            + [{"label": "fail", "judge": "fail", "eval_split": "cal"}] * 4
            + [{"label": "pass", "judge": "fail", "eval_split": "gate"}] * 2
            + [{"label": "fail", "judge": "fail", "eval_split": "gate"}] * 2)
    out = r2.kappa_by_side(recs, "gate")
    assert out["cal"]["kappa"] == 1.0 and out["gate"]["pairs"] == 4 and out["gap"] > 0.1
    assert r2.kappa_by_side(recs, None) is None


# ── the run ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _store_with_sides():
    """CAL holds every labelled-fail holdout item (so calibration counts), the rest alternate gate / dev."""
    items = golden(n_tune=12, n_hold=8, bad_holdout=4)
    for it in items:
        if it["trap"]:
            continue
        n = int(it["sample_id"].split("-")[1])
        it["eval_split"] = "cal" if (200 <= n < 204 or n in (100, 101, 102, 103, 104, 105)) else (
            "gate" if n % 2 == 0 else "dev")
    return PreregStore(items)


def test_run_on_gate_plans_only_gate_items_and_calibrates_on_cal():
    store = _store_with_sides()
    out, writer, judge = run(store, _conf(eval_split="gate"))
    gate = {it["item_id"] for it in store._items if it.get("eval_split") == "gate"}
    cand = {r["item_id"] for r in store.results if r["arm"] == "candidate" and r["mode"] == "regenerate"}
    traps = {it["item_id"] for it in store._items if it["trap"]}
    assert cand and cand <= gate | traps
    details = next(iter(store.runs.values()))["details"]
    assert details["eval_split"] == "gate" and details["calibration_counting_items"] > 0
    assert details["kappa_eval_split"]["cal"]["pairs"] == details["calibration_counting_items"]
    assert any(s["dim"] == "eval_split" for s in details["kappa_strata"])
    assert out.judge_counts


def test_run_refuses_at_zero_cost_when_the_sample_is_not_on_the_side():
    store = _store_with_sides()
    dev_id = next(it["sample_id"] for it in store._items if it.get("eval_split") == "dev")
    with pytest.raises(r2.R2Refused, match="not on the gate side"):
        run(store, _conf(eval_split="gate", sample_ids=[dev_id]))
    assert not store.runs and not store.results


def test_pre_registered_run_records_the_prereg_and_refuses_an_edited_file():
    store = _store_with_sides()
    gate_ids = sorted(it["sample_id"] for it in store._items if it.get("eval_split") == "gate")
    conf = _w5(sample_ids=gate_ids, judge_model="zz-judge")
    run(store, conf)
    assert store.asked == ["w5-null-t9-blog"]
    run_row = next(iter(store.runs.values()))
    assert run_row["details"]["prereg"] == {"id": "w5-null-t9-blog", "sha256": SHA, "workflow": "null", "arm": "B"}
    planned = {r["item_id"] for r in store.results if r["arm"] == "candidate"}
    assert planned and all(any(it["item_id"] == p and it["sample_id"] in gate_ids for it in store._items)
                           for p in planned)
    edited = PreregStore(store._items, prior=[{"run_id": "earlier", "sha256": "b" * 64, "results": 5}])
    with pytest.raises(r2.R2Refused, match="edited after its first result"):
        asyncio.run(r2.run_r2(edited, conf, writer_fn=lambda *a, **k: None, judge_fn=make_judge(),
                              flush=_noop))
    assert not edited.runs


async def _noop():
    return None


def test_item_strata_has_the_side():
    from services.writing_memory import eval_checks as ec
    assert ("eval_split", "gate") in ec.item_strata({"tenant_id": None, "tiers": [], "split": "tune",
                                                     "eval_split": "gate"})
    assert all(d != "eval_split" for d, _ in ec.item_strata({"tenant_id": None, "tiers": []}))
    assert json.dumps(ec.STRATUM_DIMS)


def test_null_run_is_refused_at_zero_cost_when_the_control_config_differs():
    """B == A by construction: a null run against a baseline import (frozen text, no config_fp) or a control written
    under another writer configuration is refused before any run row or spend; another workflow is not checked."""
    items = _store_with_sides()._items
    gate_ids = sorted(it["sample_id"] for it in items if it.get("eval_split") == "gate")
    conf = _w5(sample_ids=gate_ids)
    for store in (PreregStore(items, control_fp={"golden_set": "aa729@1.1.0", "baseline_source": "fixture"}),
                  PreregStore(items, now_fp=_fp(prompts="sha-after-w3"))):
        with pytest.raises(r2.R2Refused, match="not null by construction"):
            asyncio.run(r2.run_r2(store, conf, writer_fn=lambda *a, **k: None, judge_fn=make_judge(), flush=_noop))
        assert not store.runs and not store.results
    t9 = PreregStore(items, now_fp=_fp(prompts="sha-after-w3"))
    run(t9, _w5(sample_ids=gate_ids, prereg_id="w5-t9", prereg_workflow="t9"))
    assert t9.runs
