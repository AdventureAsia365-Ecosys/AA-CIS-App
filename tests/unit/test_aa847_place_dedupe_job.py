"""AA-847 Problem 2 — the place_dedupe job wiring (no DB, no paid call): the kind's registration, the stage defaults
(two vendors, validate), the model wrappers (budget, call log, vendor check) and the held-country alert e-mail. The
SQL (merge / undo / repoint / allow-list) is tested on Postgres in tests/integration/test_aa847_place_merge.py."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.places import dedupe as dd
from shared.llm_client.role_config import SAFE_DEFAULTS


def test_kind_is_a_background_platform_kind_cap_one():
    from shared.jobs.registry import kinds
    from shared.jobs.worker import load_kinds
    load_kinds()
    k = kinds()["place_dedupe"]
    assert k.background and k.concurrency == 1


def test_stage_defaults_are_two_vendors_and_validate():
    p, c = SAFE_DEFAULTS[dd.STAGE], SAFE_DEFAULTS[dd.CHECK_STAGE]
    assert p.role == c.role == "validate"
    assert (p.provider, p.model_id) == ("openai", "gpt-6-luna") and p.account_route == "acc3"
    assert (c.provider, c.model_id) == ("claude", "haiku") and c.account_route == "acc3"
    assert p.provider != c.provider


def test_migration_257_seeds_the_same_routes():
    from pathlib import Path
    sql = (Path(__file__).parents[2] / "api" / "migrations" / "257_geo_place_dedupe.sql").read_text()
    assert "('a3_place_dedupe',       'validate', 'openai', 'gpt-6-luna', 'acc3'" in sql
    assert "('a3_place_dedupe_check', 'validate', 'claude', 'haiku',      'acc3'" in sql
    assert "('bedrock', 'job:place_dedupe', 0.50, NULL, true" in sql


class _Budget:
    def __init__(self, cap):
        self.cap, self.spent, self.checked = cap, 0.0, []

    def check(self, est):
        from shared.cost_guard import BudgetExceeded
        self.checked.append(est)
        if self.spent + est > self.cap:
            raise BudgetExceeded("cap", "bedrock", "per_run")

    def charge(self, usd):
        self.spent += usd


def _resp(verdict, model, conf=0.9, cost=0.001):
    return SimpleNamespace(content=json.dumps({"verdict": verdict, "confidence": conf, "reason": "x"}),
                           model_used=model, cost_usd=cost, input_tokens=900, output_tokens=80)


def _models(answers, budget=None):
    from services.jobs import place_dedupe_job as job
    seq = list(answers)
    calls = []

    def gen(stage, system, user):
        calls.append((stage, json.loads(user)))
        verdict, model = seq.pop(0)
        return _resp(verdict, model)
    return job.make_models(object(), budget, ["a3_place_dedupe:gpt-6-luna", "a3_place_dedupe_check:haiku"],
                           generate_sync=gen), calls


INP = {"country": "Laos", "distance_km": 0.1, "shared": [], "a": {"name": "A"}, "b": {"name": "B"}}


def test_two_vendors_agree_and_every_call_is_logged():
    models, calls = _models([("same", "gpt-6-luna"), ("same", "gpt-6-luna"), ("same", "haiku")])
    with patch("shared.llm_client.call_log.record_call_with_pool", new=AsyncMock()) as log:
        out = asyncio.run(dd.adjudicate(INP, models.primary, models.check))
    assert out["verdict"] == "same" and models.calls == 3 and round(models.cost_usd, 6) == 0.003
    assert [c[0] for c in calls] == [dd.STAGE, dd.STAGE, dd.CHECK_STAGE]
    assert calls[1][1]["a"]["name"] == "B"                                  # the swapped order
    assert [k.kwargs["stage"] for k in log.await_args_list] == [dd.STAGE, dd.STAGE, dd.CHECK_STAGE]
    assert all(k.kwargs["role"] == "validate" for k in log.await_args_list)


def test_a_check_answered_by_the_primarys_vendor_is_unsure():
    # the check stage fell back to an OpenAI model: no two-vendor agreement
    models, _ = _models([("same", "gpt-6-luna"), ("same", "gpt-6-luna"), ("same", "gpt-6-luna-openai")])
    with patch("shared.llm_client.call_log.record_call_with_pool", new=AsyncMock()):
        out = asyncio.run(dd.adjudicate(INP, models.primary, models.check))
    assert out["verdict"] == "unsure"


def test_the_budget_is_checked_before_each_call():
    from shared.cost_guard import BudgetExceeded
    budget = _Budget(cap=0.0025)
    models, calls = _models([("same", "gpt-6-luna"), ("same", "gpt-6-luna"), ("same", "haiku")], budget)
    with patch("shared.llm_client.call_log.record_call_with_pool", new=AsyncMock()):
        with pytest.raises(BudgetExceeded):
            asyncio.run(dd.adjudicate(INP, models.primary, models.check))
    assert len(calls) == 2 and budget.checked == [0.001, 0.001, 0.003]


@pytest.mark.parametrize("model,vendor", [("gpt-6-luna", "openai"), ("gpt-6-luna-openai", "openai"),
                                          ("haiku", "anthropic"), ("us.anthropic.claude-haiku-4-5", "anthropic"),
                                          ("sonnet", "anthropic")])
def test_vendor_of(model, vendor):
    from services.jobs.place_dedupe_job import vendor_of
    assert vendor_of(model) == vendor


def test_held_country_email_renders_and_is_registered():
    from services.jobs import ops_alert_email_job
    from shared import ops_alerts
    assert ops_alert_email_job._RENDERERS[ops_alerts.TEMPLATE_PLACE_DEDUPE_HELD] is ops_alerts.render_place_dedupe_held
    msg = ops_alerts.render_place_dedupe_held({"country": "laos", "outside": 12, "limit": 5, "job_id": "j1",
                                               "period": "2026-10-21"}, to=("ops@example.com",),
                                              base_url="https://admin.example.com", environment="dev")
    assert "laos" in msg.subject and "12" in msg.text and "https://admin.example.com/admin/places" in msg.text


def test_new_write_routes_have_a_policy():
    from api.core.staff_authz import OPS_WRITE, POLICY
    for route in ("/admin/places/merges/{merge_id}/undo", "/admin/places/dedupe/countries",
                  "/admin/places/dedupe/run"):
        assert POLICY[("POST", route)] == OPS_WRITE
