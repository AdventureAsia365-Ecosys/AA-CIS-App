"""AA-1051 S285 — the plain-judge T9 proof template the runner / power / prereg mechanics tests build on.

Until S285 those tests read prereg/w5-t9.json, the T9 draft with a plain catalog judge and an `improve` rule on
item_fail_rate. S285 re-drafted that file on the pinned claim instrument (non_inferior on fail_claims_per_item), so the
tests that exercise the plain judge's catalog price, the improve rule and its effect keep the old shape here. Invented
values only; not a pre-registration anyone runs."""
from __future__ import annotations

import copy

T9_PLAIN_IMPROVE = {
    "format": 1, "id": "w5-t9", "workflow": "t9", "status": "draft", "registered_by": None, "registered_on": None,
    "golden_set": {"name": "aa729"}, "eval_split": "gate", "sample_ids": [], "noise_sample_ids": [],
    "release_id": None, "judge": {"model_key": "gpt-6-luna-openai", "connection": "openai-direct"},
    "metrics": [
        {"name": "item_fail_rate", "kind": "computed", "direction": "lower", "primary": True},
        {"name": "first_pass_yield", "kind": "computed", "direction": "higher", "target": {"op": ">=", "value": 0.3}},
        {"name": "requests_per_object", "kind": "computed", "direction": "lower", "target": {"op": "<=", "value": 5}},
        {"name": "cost_per_object_usd", "kind": "computed", "direction": "lower",
         "target": {"op": "<=", "value": 0.25}},
        {"name": "contract_violations", "kind": "external", "direction": "lower", "target": {"op": "<=", "value": 0}},
    ],
    "decision": {"rule": "improve", "metric": "item_fail_rate", "margin_abs": 0.1, "alpha": 0.05, "noise_se_k": 2.0},
    "requirements": {"paired_all": True, "max_mixed_judges": 0, "max_hard_stop_1": 0},
    "power": {"min_power": 0.8, "writer_sd": None, "claims_per_item": 60, "effect_abs": 0.3},
    "cap_usd": 15.0,
}


def t9_plain_improve() -> dict:
    """A fresh deep copy of the template (tests mutate it)."""
    return copy.deepcopy(T9_PLAIN_IMPROVE)
