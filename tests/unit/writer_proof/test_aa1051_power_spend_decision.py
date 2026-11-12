"""AA-1051 — the power check (power.py), the spend gate (spend.py), the pre-registered decision (decision.py) and the
cost per object from the ledger (cost_report.py). Pure: no DB, no model. Invented ids only."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from services.writer_proof import cost_report, decision, power, spend
from services.writer_proof import prereg as pr


# ── power ────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_majority_prob():
    assert power.majority_prob(0.0, 3) == 0.0 and power.majority_prob(1.0, 3) == 1.0
    assert math.isclose(power.majority_prob(0.5, 3), 0.5)
    assert math.isclose(power.majority_prob(1 / 3, 3), 7 / 27)


def _votes(n_claims: int, flaky_every: int) -> dict:
    """Unanimous votes, every `flaky_every`-th claim split 2-1."""
    return {f"{i:064x}": ([True, True, False] if i % flaky_every == 0 else [False, False, False])
            for i in range(n_claims)}


def test_unanimous_votes_have_no_judge_noise():
    out = power.check(_votes(120, 10 ** 9 + 1) | {}, n_items=12, margin_abs=0.15, metric="item_fail_rate",
                      claims_per_item=10)
    # claim 0 is the only split one (0 % big == 0): almost no noise -> adequate
    assert out["adequate"] and out["claims"] == 120 and out["pseudo_items"] == 12


def test_noisy_votes_make_a_small_sample_underpowered():
    out = power.check(_votes(600, 3), n_items=12, margin_abs=0.5, metric="fail_claims_per_item", claims_per_item=60)
    assert not out["adequate"] and out["n_needed"] > 12
    assert out["min_resolvable_margin"] > 0.5 and out["bound"].startswith("upper bound")
    # a binary item verdict over 60 claims with 20 flaky ones is (almost) always fail: no re-judge spread left
    sat = power.check(_votes(600, 3), n_items=12, margin_abs=0.05, metric="item_fail_rate", claims_per_item=60)
    assert sat["judge_var_per_item"] < 1e-6


def test_writer_sd_lowers_power():
    v = _votes(600, 7)
    a = power.check(v, n_items=20, margin_abs=1.0, metric="fail_claims_per_item", claims_per_item=60)
    b = power.check(v, n_items=20, margin_abs=1.0, metric="fail_claims_per_item", claims_per_item=60, writer_sd=1.5)
    assert b["power_at_zero"] < a["power_at_zero"] and b["var_diff"] > a["var_diff"]


def test_power_refuses_too_few_claims_and_bad_inputs():
    with pytest.raises(power.PowerRefused):
        power.check({"a" * 64: [True, None, None]} | _votes(5, 2), n_items=12, margin_abs=0.1,
                    metric="item_fail_rate")
    with pytest.raises(power.PowerRefused):
        power.check(_votes(50, 2), n_items=0, margin_abs=0.1, metric="item_fail_rate")


def test_fewer_claims_than_one_item_scales():
    out = power.check(_votes(30, 2), n_items=12, margin_abs=0.2, metric="fail_claims_per_item", claims_per_item=60)
    assert out["scaled"] and out["pseudo_items"] == 0


def test_check_prereg_and_votes_of():
    d = json.loads(pr.path_for("w5-null-t9-blog").read_text())
    d.update(sample_ids=sorted(f"zz-{i:02d}" for i in range(12)))
    pre = pr.from_doc(d)
    rep = {"recommended": "m@c:extraction", "votes": {"m@c:extraction": _votes(120, 4)}}
    out = power.check_prereg(pre, power.votes_of(rep, None))
    assert out["n_items"] == 12 and out["margin_abs"] == d["decision"]["margin_abs"]
    with pytest.raises(power.PowerRefused):
        power.votes_of(rep, "other@c:entailment")
    with pytest.raises(power.PowerRefused):
        power.votes_of({}, None)


# ── spend gate ───────────────────────────────────────────────────────────────────────────────────────────────────

def _gate(**over):
    kw = dict(workflow="null", prereg_cap=5.0, cap_usd=5.0, confirm="CONFIRM", spent_workflow=0.0, spent_total=0.0,
              estimate_usd=3.0)
    kw.update(over)
    return spend.refusals(**kw)


def test_gate_passes_with_confirm_and_a_cap():
    assert _gate() == []


@pytest.mark.parametrize("over,needle", [
    ({"confirm": None}, "confirmation"), ({"confirm": "yes"}, "confirmation"),
    ({"cap_usd": None}, "no cap"), ({"cap_usd": 0}, "no cap"),
    ({"cap_usd": 6.0}, "above the pre-registered"),
    ({"prereg_cap": 4.0}, "above the pre-registered"),
    ({"spent_workflow": 0.5}, "D12 cap 5.0"),
    ({"spent_total": 29.0}, "D12 total"),
    ({"spent_total": float("nan")}, "fail closed"),
    ({"estimate_usd": None}, "no cost estimate"),
    ({"estimate_usd": 5.5}, "above the cap"),
])
def test_gate_refuses(over, needle):
    assert any(needle in r for r in _gate(**over))


def test_t9_cap_is_15_and_split_cap_never_passes_it():
    assert _gate(workflow="t9", prereg_cap=15.0, cap_usd=15.0, estimate_usd=12) == []
    caps = spend.split_cap(15.0, {"B": 9.0, "A_prime": 4.5})
    assert sum(caps.values()) <= 15.0 and caps["B"] > caps["A_prime"]
    assert spend.split_cap(5.0, {"B": 0.0}) == {"B": 5.0}


# ── decision ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _doc(rule="non_inferior", margin=0.15, noise=()):
    d = json.loads(pr.path_for("w5-null-t9-blog").read_text())
    d.update(sample_ids=sorted(f"zz-{i:02d}" for i in range(12)), noise_sample_ids=sorted(noise))
    d["decision"] = {**d["decision"], "rule": rule, "margin_abs": margin}
    return d


def _rows(fails_a=(), fails_b=(), missing=(), claims_b=0):
    rows = []
    for i in range(12):
        sid = f"zz-{i:02d}"
        rows.append({"sample_id": sid, "arm": "control", "mode": "rejudge",
                     "verdict": "fail" if i in fails_a else "pass", "failure_codes": [], "details": {}})
        if i in missing:
            continue
        rows.append({"sample_id": sid, "arm": "candidate", "mode": "regenerate", "result_id": f"res-{i}",
                     "verdict": "fail" if i in fails_b else "pass",
                     "failure_codes": ["fact.inclusion"] * (i in fails_b),
                     "details": {"judge_claims": [{"class": "fact.inclusion"}] * claims_b}, "hard_stop": False})
    return rows


def test_null_run_identical_arms_is_non_inferior():
    out = decision.decide(_doc(), _rows(fails_a=(1, 5), fails_b=(1, 5)),
                          run_details={"judge_identities": [{"model": "m"}], "counts": {"hard_stops": {}}})
    assert out["verdict"] == "non_inferior" and out["paired"] == 12 and out["mean_diff"] == 0


def test_more_failures_is_inferior():
    out = decision.decide(_doc(), _rows(fails_b=(0, 1, 2, 3, 4, 5)))
    assert out["verdict"] == "inferior" and out["upper_bound"] > 0.15


def test_requirements_make_it_invalid():
    assert decision.decide(_doc(), _rows(missing=(3,)))["verdict"] == "invalid"
    mixed = {"judge_identities": [{"model": "a"}, {"model": "b"}]}
    assert "mixed" in decision.decide(_doc(), _rows(), run_details=mixed)["problems"][0]
    hs = {"counts": {"hard_stops": {"1": 1}}}
    assert decision.decide(_doc(), _rows(), run_details=hs)["verdict"] == "invalid"
    assert decision.decide(_doc(), [])["verdict"] == "invalid"


def test_improve_rule_and_noise_arm():
    d = _doc(rule="improve", margin=0.10, noise=["zz-00", "zz-01", "zz-02"])
    b = _rows(fails_a=tuple(range(8)), fails_b=())
    a_prime = [r for r in _rows() if r["sample_id"] in d["noise_sample_ids"]]
    out = decision.decide(d, b, a_prime_rows=a_prime)
    assert out["verdict"] == "improve" and out["noise_items"] == 3
    assert decision.decide(d, b)["verdict"] == "invalid"                 # the noise arm is pre-registered: needed
    flat = decision.decide(_doc(rule="improve", margin=0.10), _rows())
    assert flat["verdict"] == "not_improved"


def test_higher_is_better_metric_flips_the_sign():
    d = _doc()
    d["metrics"] = [{"name": "item_fail_rate", "kind": "computed", "direction": "higher", "primary": True}]
    out = decision.decide(d, _rows(fails_b=(0, 1, 2, 3, 4, 5)))
    assert out["mean_diff"] < 0 and out["verdict"] == "non_inferior"


def test_targets_and_computed_metrics():
    rows = _rows(fails_b=(0, 1, 2), claims_b=2)
    per = {f"res-{i}": {"cost_usd": 0.1 + i / 100, "requests": 4 + (i % 2), "writer_requests": 1} for i in range(12)}
    comp = decision.computed_metrics(rows, per)
    assert comp["item_fail_rate"] == 0.25 and comp["first_pass_yield"] == 0.75
    assert comp["fail_claims_per_item"] == 2.0 and comp["requests_per_object"] == 4.5
    doc = json.loads(pr.path_for("w5-t9").read_text())
    t = {x["metric"]: x["status"] for x in decision.targets(doc, comp)}
    assert t["first_pass_yield"] == "met" and t["requests_per_object"] == "met"
    assert t["contract_violations"] == "pending" and t["cost_per_object_usd"] == "met"


# ── cost per object ──────────────────────────────────────────────────────────────────────────────────────────────

T0 = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


def _t(s: int) -> datetime:
    return T0 + timedelta(seconds=s)


def test_attribute_assigns_each_ledger_row_to_the_next_result_of_its_run():
    results = [{"result_id": "a", "run_id": "run1", "created_at": _t(10), "arm": "control", "mode": "rejudge"},
               {"result_id": "b", "run_id": "run1", "created_at": _t(20), "arm": "candidate", "mode": "regenerate"},
               {"result_id": "c", "run_id": "run2", "created_at": _t(15), "arm": "candidate", "mode": "regenerate"}]
    ledger = [{"id": "1", "eval_run": "run1", "created_at": _t(5), "cost_usd": 0.01, "role": "judge"},
              {"id": "2", "eval_run": "run1", "created_at": _t(12), "cost_usd": 0.05, "role": "writer"},
              {"id": "3", "eval_run": "run1", "created_at": _t(19), "cost_usd": 0.02, "role": "judge"},
              {"id": "4", "eval_run": "run2", "created_at": _t(14), "cost_usd": 0.07, "role": "writer"},
              {"id": "5", "eval_run": "run1", "created_at": _t(30), "cost_usd": 0.003, "role": "judge"}]
    att = cost_report.attribute(ledger, results)
    assert att["per_result"]["a"] == {"cost_usd": 0.01, "requests": 1, "writer_requests": 0}
    assert att["per_result"]["b"] == {"cost_usd": 0.07, "requests": 2, "writer_requests": 1}
    assert att["per_result"]["c"]["writer_requests"] == 1
    assert att["unattributed"]["run1"]["requests"] == 1                   # after the last result: kept, flagged
    rep = cost_report.report([{"run_id": "run1", "writer": "t9_blog"}, {"run_id": "run2", "writer": "t9_blog"}],
                             ledger, results)
    r1 = rep["runs"][0]
    assert r1["ledger_usd"] == 0.083 and r1["objects"] == 1 and r1["all_in_cost_per_object_usd"] == 0.083
    assert r1["attributed_cost_per_object_usd"]["median"] == 0.07 and r1["requests_per_object"]["max"] == 2.0
    assert rep["total_usd"] == 0.153 and rep["stage"] == spend.W5_STAGE
