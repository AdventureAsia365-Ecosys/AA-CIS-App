"""AA-1127 (C2, S283 UI audit) — spend truth: one provider-family mapping and one account naming everywhere.

- The Anthropic API (provider "anthropic") is a family of its own: the Spend forecast row "Anthropic API" with its
  Budgets-tab budget, the AI spend by provider and the FI-02 weekly lines all read platform_cost.provider_family; it was
  "Other LLM providers" with no budget (28.90 USD month to date, the largest line).
- Cost Explorer rows of a live Bedrock satellite of the registry (the account serving Bedrock since AA-1095) are keyed
  like accounts.account_name keys it, not "retired", so the AWS bill tab and the Overview name it alike.
- That satellite is its own AWS row in the Spend forecast (it used to fall out once it was no longer "retired") and its
  own line in the FI-02 weekly report, counted in the week total (money.source_of follows cost_ops.ledger_account).
  The AI gateway's LLM month is platform_cost.ai_spend_by_provider (tests/unit/test_aa804_ai_gateway_api.py).
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


# ── a live Bedrock satellite: its own row in the forecast and in the weekly report ─────────────────────────────────

SAT_KEY, SAT_ID, ACC2_ID, ACC3_ID = "payer-bedrock", "1" * 12, "2" * 12, "3" * 12


@pytest.fixture
def satellite():
    from shared.aws_client import accounts
    from shared.aws_client import cost_explorer as ce_mod
    with patch.object(ce_mod, "acc2_account_id", return_value=ACC2_ID), \
            patch.object(ce_mod, "ACC3_ACCOUNT_ID", ACC3_ID), \
            patch.object(accounts, "satellite_connections", return_value={SAT_KEY: _Conn(SAT_ID)}):
        yield accounts


@pytest.mark.asyncio
async def test_forecast_shows_the_satellite_cost_explorer_rows_as_their_own_account(satellite):
    now = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)
    fetched = datetime(2026, 11, 18, 6, tzinfo=timezone.utc)
    ce_rows = [{"account_id": acc, "day": date(2026, 11, d), "amount_usd": usd, "fetched_at": fetched}
               for d in range(1, 17) for acc, usd in ((ACC2_ID, 3.0), (SAT_ID, 1.5))]
    ai = [{"day": date(2026, 11, d), "provider": "bedrock", "account": acc, "cost_usd": 1.0}
          for d in range(1, 17) for acc in ("acc3", SAT_KEY, "acc2")]
    pool = _Pool({cost_ops.CE_DAILY_SQL: ce_rows, cost_ops.AI_DAILY_SQL: ai})
    out = await cost_ops.cost_forecast(pool, None, now=now)
    aws = {a["account"]: a for a in out["aws_accounts"]}
    assert list(aws) == ["acc2", "acc3", SAT_KEY]            # no "retired" row: the satellite is not history
    assert aws[SAT_KEY]["label"] == satellite.account_label(SAT_KEY) and aws[SAT_KEY]["mtd_usd"] == pytest.approx(24.0)
    bedrock = next(p for p in out["providers"] if p["key"] == "bedrock")
    assert [a["account"] for a in bedrock["accounts"]] == ["acc3", SAT_KEY, "acc2"]


@pytest.mark.asyncio
async def test_forecast_hides_a_satellite_without_cost_explorer_rows(satellite):
    out = await cost_ops.cost_forecast(_Pool({}), None, now=datetime(2026, 11, 18, 12, tzinfo=timezone.utc))
    assert [a["account"] for a in out["aws_accounts"]] == ["acc2", "acc3"]


def test_the_weekly_report_splits_bedrock_like_the_forecast(satellite):
    assert money.source_of("bedrock", SAT_KEY) == "bedrock_satellite" == money.source_of("bedrock-satellite", SAT_KEY)
    assert cost_ops.ledger_account("bedrock", SAT_KEY) == SAT_KEY
    assert money.source_of("bedrock", "acc3") == "bedrock_acc3" and money.source_of("bedrock", None) == "bedrock_acc2"
    assert money.source_of("bedrock", "acc9") == "bedrock_retired"


def test_the_weekly_total_counts_the_satellite_bedrock(satellite):
    first, last = date(2026, 11, 9), date(2026, 11, 15)
    llm = [{"day": date(2026, 11, d), "provider": p, "account": acc, "cost_usd": usd, "calls": 1, "app": "cis",
            "stage": "write"}
           for d in range(9, 16) for p, acc, usd in (("bedrock", SAT_KEY, 2.0), ("anthropic", "anthropic-direct", 1.0),
                                                     ("bedrock", "acc9", 5.0))]
    out = money.week_compute({"llm": llm, "aws": [], "dfs": [], "geo": []}, first, last)
    lines = {x["key"]: x for x in out["lines"]}
    assert lines["bedrock_satellite"]["usd"] == pytest.approx(14.0)
    assert lines["bedrock_satellite"]["label"] == "Bedrock on the serving account (gateway estimate)"
    assert out["total_usd"] == pytest.approx(14.0 + 7.0)      # the retired account's history stays out of the total
