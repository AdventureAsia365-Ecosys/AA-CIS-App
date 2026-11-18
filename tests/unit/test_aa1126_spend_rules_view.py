"""AA-1126 (S283 review fix): the admin pages that show an app's AI caps say what the code enforces: the spend
environment of each app (CIS / TripPlanner from the CIS registry, the CRM from its own registry through crm.ops.v1),
whether a reached daily budget stops the AI (dev) or only alerts (prod), and the TripPlanner stage caps from the
settings TripPlanner reads. No DB, no network; invented values only."""
import asyncio

import pytest

from api.services import crm_console
from api.services import spend_rules_view as v
from shared import settings


@pytest.mark.parametrize("value, expected", [("prod", "prod"), (" PROD", "prod"), ("dev", "dev"), ("zz", "dev"),
                                             (None, None)])
def test_environment_of(value, expected):
    assert v.environment_of(value) == expected


@pytest.mark.parametrize("app, env, rule", [("tripplanner", "prod", "alert_only"), ("tripplanner", "dev", "stops_ai"),
                                            ("crm", "prod", "alert_only"), ("crm", "dev", "stops_ai"),
                                            ("crm", None, None), ("cis", "prod", None), ("booking", None, None)])
def test_the_budget_rule_follows_the_setting(app, env, rule):
    assert v.cap_rule(app, env) == rule


def test_a_key_not_registered_yet_reads_unknown(monkeypatch):
    def boom(key, tenant_id=None):
        raise KeyError(key)
    monkeypatch.setattr(settings, "get", boom)
    assert v.environment(v.TP_ENV_KEY) is None
    assert v.tp_stage_caps() == {"tp_compose": None, "tp_search_embed": None} and v.tp_near_ratio() is None


def test_the_crm_environment_comes_from_its_registry(monkeypatch):
    calls = []

    async def ops(method, path, authorization, *, json=None, params=None):
        calls.append((method, path, params))
        return 200, {"settings": [{"key": "crm.ai.budget_max_usd", "value": 2},
                                  {"key": "crm.spend.environment", "value": "prod"}]}
    monkeypatch.setattr(crm_console, "crm_ops_call", ops)
    monkeypatch.setattr(settings, "get", lambda key, tenant_id=None: {"cis.spend.environment": "dev",
                                                                      "tripplanner.spend.environment": "prod"}[key])
    envs = asyncio.run(v.app_environments("Bearer zz"))
    assert envs == {"cis": "dev", "tripplanner": "prod", "crm": "prod", "booking": None}
    assert calls == [("GET", "/settings", {"area": "spend"})]


@pytest.mark.parametrize("answer", [(503, {}), (200, {"settings": []}), (200, {})])
def test_the_crm_environment_is_unknown_when_the_crm_does_not_say(monkeypatch, answer):
    async def ops(method, path, authorization, *, json=None, params=None):
        return answer
    monkeypatch.setattr(crm_console, "crm_ops_call", ops)
    assert asyncio.run(v.crm_environment("Bearer zz")) is None
