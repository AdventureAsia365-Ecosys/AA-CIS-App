"""S279 thresholds lane L5 (CIS 414) — the TripPlanner settings in the registry are seeded from today's effective
values, and the daily AI cap is not duplicated in the registry (its one source is the app profile row).

The same numbers are pinned on the TripPlanner side (AA-TripPlanner-Web backend/tests/test_s279_settings.py: each key's
fallback, the code default of backend/config.py / plan_chat.py / agent.py, equals the seed), so a change on one side
without the other fails one of the two suites. No DB."""
from pathlib import Path

import pytest

from scripts.settings import gen_compiled_defaults as gen
from shared import settings

M414 = Path(gen.MIGRATIONS) / "414_tripplanner_settings_registry.sql"

TP_SEED = {
    "tripplanner.rate.narrate_per_hour": 10,
    "tripplanner.rate.search_per_minute": 30,
    "tripplanner.rate.signed_in_factor": 2,
    "tripplanner.rate.send_per_hour": 5,
    "tripplanner.rate.send_ip_per_hour": 20,
    "tripplanner.rate.chat_per_hour": 20,
    "tripplanner.rate.chat_ip_per_hour": 40,
    "tripplanner.spend.plan_chat_monthly_cap_usd": 2.0,
    "tripplanner.spend.plan_chat_tenant_monthly_cap_usd": 2.0,
    "tripplanner.leads.tenant_daily_cap": 200,
    "tripplanner.leads.hold_days": 7,
    "tripplanner.ai.narration_cache_ttl_hours": 168,
    "tripplanner.ai.narration_max_tokens": 1200,
    "tripplanner.ai.plan_chat_max_steps": 3,
    "tripplanner.ai.plan_chat_max_tokens": 700,
    "tripplanner.handoff.advisor_demo": True,
    "tripplanner.handoff.require_registration": True,
}


def _tp_rows() -> dict:
    return {r["key"]: r for r in gen.seed_rows()[0] if r["app"] == "tripplanner"}


def test_the_tripplanner_keys_are_exactly_the_lane_l5_list():
    assert sorted(_tp_rows()) == sorted(TP_SEED)


@pytest.mark.parametrize("key,value", sorted(TP_SEED.items()))
def test_seeded_from_todays_effective_value(key, value):
    row = _tp_rows()[key]
    assert row["default_value"] == value and type(row["default_value"]) is type(value)
    assert row["enforced"] is True and row["env_override"] is None


def test_per_tenant_overrides_only_where_the_audit_allows_them():
    tenant = sorted(k for k, r in _tp_rows().items() if r["scope_kind"] == "platform+tenant")
    assert tenant == ["tripplanner.leads.tenant_daily_cap", "tripplanner.spend.plan_chat_tenant_monthly_cap_usd"]


def test_the_lead_hold_keeps_the_retention_promise():
    row = _tp_rows()["tripplanner.leads.hold_days"]
    assert (row["min_value"], row["max_value"]) == (3, 7)          # DM-7.1 tripplanner.outbox: at most 7 days


def test_the_daily_cap_has_one_source_the_app_profile():
    assert not any(k.endswith("daily_cap_usd") or k.endswith("daily_budget_usd") for k in _tp_rows())
    sql = M414.read_text(encoding="utf-8")
    assert "SET default_daily_budget_usd = 5.0" in sql
    assert "WHERE app = 'tripplanner' AND default_daily_budget_usd IS NULL" in sql   # an administrator's value is kept


def test_the_cis_binding_serves_the_tripplanner_defaults():
    assert settings.get("tripplanner.leads.tenant_daily_cap") == 200
    assert settings.resolve("tripplanner.spend.plan_chat_tenant_monthly_cap_usd", "t-1").source == "default"
