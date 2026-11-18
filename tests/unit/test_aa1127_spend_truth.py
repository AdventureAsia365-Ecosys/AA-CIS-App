"""AA-1127 (C2, S283 UI audit) — spend truth: one provider-family mapping and one account naming everywhere.

- The Anthropic API (provider "anthropic") is a family of its own: the Spend forecast row "Anthropic API" with its
  Budgets-tab budget, the AI spend by provider and the FI-02 weekly lines all read platform_cost.provider_family; it was
  "Other LLM providers" with no budget (28.90 USD month to date, the largest line).
- Cost Explorer rows of a live Bedrock satellite of the registry (the account serving Bedrock since AA-1095) are keyed
  like accounts.account_name keys it, not "retired", so the AWS bill tab and the Overview name it alike.
No DB, no AWS."""
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from api.services import cost_ops
from api.services import platform_cost as pc
from services.reports import money
from shared import cost_guard


def test_every_budget_provider_has_a_forecast_row_and_a_label():
    for p in cost_guard.PROVIDERS:
        if p in ("dfs", "geocoder"):
            continue                       # not LLM ledger providers (dfs has its own row; geocoder its own page)
        assert pc.provider_family(p) in cost_ops.FORECAST_PROVIDERS, p
    assert set(cost_ops.FORECAST_PROVIDERS) <= set(cost_ops.PROVIDER_LABELS)
    assert cost_ops.PROVIDER_LABELS["anthropic"] == "Anthropic API"
    assert pc.provider_family("anthropic") == "anthropic"


def test_the_weekly_report_reads_the_same_family():
    assert money.source_of("anthropic", "anthropic-direct") == "anthropic"
    assert "anthropic" in {k for k, _label, _src in money.PROVIDERS}


class _Pool:
    def __init__(self, by_sql):
        self.by_sql = by_sql

    async def fetch(self, sql, *args):
        return self.by_sql.get(sql, [])


@pytest.mark.asyncio
async def test_forecast_shows_anthropic_with_its_budget():
    now = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    ai = [{"day": date(2026, 11, d), "provider": "anthropic", "account": "anthropic-direct", "cost_usd": 2.0}
          for d in range(1, 18)]
    pool = _Pool({cost_ops.AI_DAILY_SQL: ai,
                  cost_ops.BUDGETS_SQL: [{"provider": "anthropic", "per_day_usd": 5.0, "alert_pct": 80}]})
    out = await cost_ops.cost_forecast(pool, None, now=now)
    by = {p["key"]: p for p in out["providers"]}
    assert "other" not in by
    row = by["anthropic"]
    assert row["label"] == "Anthropic API" and row["mtd_usd"] == pytest.approx(34.0)
    assert row["budget_usd"] == 150.0 and row["status"] != "no_budget"


@pytest.mark.asyncio
async def test_forecast_hides_anthropic_with_no_spend_and_no_budget():
    out = await cost_ops.cost_forecast(_Pool({}), None, now=datetime(2026, 11, 18, 12, tzinfo=timezone.utc))
    assert [p["key"] for p in out["providers"]] == ["bedrock", "openai", "jev", "dfs"]


class _Conn:
    def __init__(self, account_id):
        self._id = account_id

    def resolved_account_id(self):
        return self._id


def test_cost_explorer_names_a_live_satellite_like_the_account_directory():
    from shared.aws_client import accounts
    from shared.aws_client import cost_explorer as ce_mod
    with patch.object(ce_mod, "acc2_account_id", return_value="2" * 12), \
            patch.object(ce_mod, "ACC3_ACCOUNT_ID", "3" * 12), \
            patch.object(accounts, "satellite_connections", return_value={"payer-bedrock": _Conn("1" * 12)}):
        assert ce_mod.account_key("1" * 12) == "payer-bedrock"
        assert ce_mod.account_key("4" * 12) == ce_mod.RETIRED_ACCOUNT_KEY
        assert ce_mod.account_key("2" * 12) == "acc2" and ce_mod.account_key("3" * 12) == "acc3"


def test_cost_explorer_falls_back_to_retired_when_the_registry_cannot_be_read():
    from shared.aws_client import accounts
    from shared.aws_client import cost_explorer as ce_mod
    with patch.object(ce_mod, "acc2_account_id", return_value="2" * 12), \
            patch.object(accounts, "satellite_connections", side_effect=RuntimeError("db down")):
        assert ce_mod.account_key("1" * 12) == ce_mod.RETIRED_ACCOUNT_KEY
