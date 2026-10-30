"""AA-1002 (gateway slice S6) — cost parity: ``pricing.quote(model, usage, deployment)`` and where it is used.

- Golden: for every shape ``shared.llm_call_log`` records (``aa1002_s6_cost_capture.py``), ``calc_cost`` gives the
  pre-S6 number, except the documented correction (Claude Haiku 5.5 above 100K prompt tokens: 5x).
- The cases the old formula could not price: 1-hour cache writes (2x), batch (50% off everything), US-only
  inference (1.1x), deployment price overrides, an unknown model (charged high, never 0, flagged ``estimate``).
- The adapters: OpenAI cached prompt tokens at the cache-read rate, the Anthropic API's 1-hour writes and geo, the
  Converse deployment override; the log (``call_log``) and the rollup write the S6 columns only when they exist.
No AWS, no vendor, no DB.
"""
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from shared.llm_client import call_log, pricing
from shared.llm_client.deployments import ModelDeployment
from shared.llm_client.pricing import Quote, Usage, calc_cost, quote
from tests.unit.aa1002_s6_cost_capture import capture, catalog_setter

GOLDEN = Path(__file__).resolve().parent / "snapshots" / "aa1002_s6_pre_s6_costs.json"
MIGRATION = Path(__file__).resolve().parents[2] / "api" / "migrations" / "312_llm_call_log_cache_pricing_mode.sql"
RATES = {  # $ per 1K, the shape pricing._catalog_rates returns
    "sonnet-5-5": {"in": 0.002, "out": 0.010, "cache_read": 0.0002, "cache_write": 0.0025},
    "haiku-5-5": {"in": 0.0001, "out": 0.0005, "cache_read": 0.00001, "cache_write": 0.000125},
    "gpt-6-luna-openai": {"in": 0.0001, "out": 0.0005, "cache_read": 0.00001, "cache_write": 0.000125},
}


@pytest.fixture
def rates(monkeypatch):
    monkeypatch.setattr(pricing, "_catalog_rates", lambda m: RATES.get(pricing._catalog_key(m)))


def _dep(**overrides):
    return ModelDeployment("sonnet-5-5", "acc3", "global.anthropic.claude-sonnet-5-5", 10, True, "admin", **overrides)


# ── golden parity ─────────────────────────────────────────────────────────────────────────────────────────────────

def _tiered(key: str) -> bool:
    label, i, _o, cr, cw = key.split("|")
    return pricing._catalog_key(label) == "haiku-5-5" and int(i) + int(cr) + int(cw) > 100_000


def test_calc_cost_keeps_every_pre_s6_number_but_the_haiku_5_5_tier():
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    got = capture(calc_cost, catalog_setter())
    assert set(got["catalog"]) == set(golden["catalog"]) and len(golden["catalog"]) >= 150
    changed = []
    for state in ("catalog", "no_catalog"):
        for key, old in golden[state].items():
            if state == "catalog" and _tiered(key):
                changed.append(key)
                assert got[state][key] == pytest.approx(old * 5, rel=1e-6), key
            else:
                assert got[state][key] == old, f"{state} {key}: {got[state][key]} != {old}"
    assert len(changed) == 4                     # haiku-5-5 / anthropic-api-haiku-5-5 x the two >100K shapes


# ── quote ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_one_hour_writes_cost_twice_the_input_rate(rates):
    five = quote("sonnet-5-5", Usage(cache_write_tokens=1_000_000))
    hour = quote("sonnet-5-5", Usage(cache_write_tokens=1_000_000, cache_write_1h_tokens=1_000_000))
    mixed = quote("sonnet-5-5", Usage(cache_write_tokens=1_000_000, cache_write_1h_tokens=400_000))
    assert five.cost_usd == pytest.approx(2.5) and hour.cost_usd == pytest.approx(4.0)
    assert mixed.cost_usd == pytest.approx(0.6 * 2.5 + 0.4 * 4.0)
    assert quote("sonnet-5-5", Usage(cache_write_tokens=10, cache_write_1h_tokens=99)).cost_usd == \
        pytest.approx(10 * 0.004 / 1000)                                # the 1h part never exceeds the writes


def test_batch_halves_every_token_including_the_cache(rates):
    u = dict(input_tokens=1000, output_tokens=500, cache_read_tokens=4000, cache_write_tokens=2000)
    std, batch = quote("sonnet-5-5", Usage(**u)), quote("sonnet-5-5", Usage(**u, batch=True))
    assert batch.cost_usd == pytest.approx(std.cost_usd / 2)
    assert (std.pricing_mode, batch.pricing_mode) == ("catalog", "catalog+batch")


def test_us_only_inference_costs_ten_percent_more(rates):
    u = dict(input_tokens=10_000, output_tokens=1000)
    assert quote("sonnet-5-5", Usage(**u, inference_geo="us")).cost_usd == \
        pytest.approx(quote("sonnet-5-5", Usage(**u)).cost_usd * 1.1)
    assert quote("sonnet-5-5", Usage(**u, inference_geo="global")).pricing_mode == "catalog"
    assert quote("sonnet-5-5", Usage(**u, inference_geo="us", batch=True)).pricing_mode == "catalog+batch+geo_us"


@pytest.mark.parametrize("prompt, factor", [(100_000, 1.0), (100_001, 5.0)])
def test_haiku_5_5_long_prompt_tier_counts_the_whole_prompt(rates, prompt, factor):
    u = Usage(input_tokens=prompt - 60_000, cache_read_tokens=60_000, output_tokens=1000)
    base = (u.input_tokens * 0.0001 + 1000 * 0.0005 + 60_000 * 0.00001) / 1000
    assert quote("haiku-5-5", u).cost_usd == pytest.approx(base * factor, abs=1e-6)          # cost rounds to 1e-6
    assert quote("anthropic-api-haiku-5-5", u).cost_usd == pytest.approx(base * factor, abs=1e-6)
    assert quote("sonnet-5-5", Usage(input_tokens=500_000)).cost_usd == pytest.approx(1.0)     # no tier for Sonnet


def test_deployment_overrides_win_and_are_named(rates):
    d = _dep(price_in_override=1.0, price_out_override=5.0, price_cache_write_1h_override=3.0)
    q = quote("sonnet-5-5", Usage(input_tokens=1_000_000, output_tokens=100_000, cache_write_tokens=1000,
                                  cache_write_1h_tokens=1000), d)
    assert q.pricing_mode == "override"
    assert q.cost_usd == pytest.approx(1.0 + 0.5 + 1000 * 0.003 / 1000)
    assert quote("sonnet-5-5", Usage(input_tokens=1000), _dep()).pricing_mode == "catalog"   # no override set


def test_an_unknown_model_is_charged_high_and_flagged_never_zero(rates):
    with patch.object(pricing, "logger") as log:
        q = quote("mystery-model", Usage(input_tokens=1000, output_tokens=100))
    assert q.estimated and q.pricing_mode == "estimate" and q.cost_usd == pytest.approx(0.0045)
    assert log.warning.call_args.args[0] == "llm_price_unknown_model"
    assert quote("us.anthropic.claude-haiku-4-5-20251001-v1:0", Usage(input_tokens=1000)).pricing_mode == "code_table"
    assert pricing.pricing_mode_for("mystery-model") == "estimate"
    assert pricing.pricing_mode_for("sonnet-5-5") == "catalog"


def test_pricing_mode_values_match_migration_312():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "pricing_mode ~ '^(override|catalog|code_table|estimate)(\\+batch)?(\\+geo_us)?$'" in sql
    assert pricing.PRICING_MODE_RE == r"^(override|catalog|code_table|estimate)(\+batch)?(\+geo_us)?$"
    for basis in pricing.PRICING_BASES:
        for tail in ("", "+batch", "+geo_us", "+batch+geo_us"):
            assert re.match(pricing.PRICING_MODE_RE, basis + tail)
    assert not re.search(r"^-- migrate: expand", sql, re.M)          # contract: ACCESS EXCLUSIVE on a >100k table


def test_deployment_for_finds_the_row_and_never_raises(monkeypatch):
    from shared.llm_client import deployments
    rows = [_dep(), ModelDeployment("sonnet-5-5", "anthropic-direct", "claude-sonnet-5-5", 100, True, "seed")]
    monkeypatch.setattr(deployments, "deployments_for_sync", lambda key, enabled_only=True: rows)
    assert pricing.deployment_for("satellite-sonnet-5-5", "anthropic-direct").connection_key == "anthropic-direct"
    assert pricing.deployment_for("sonnet-5-5", None) is None
    monkeypatch.setattr(deployments, "deployments_for_sync", MagicMock(side_effect=RuntimeError("db")))
    assert pricing.deployment_for("sonnet-5-5", "acc3") is None


# ── adapters ──────────────────────────────────────────────────────────────────────────────────────────────────────

def _client():
    from shared.llm_client.client import LLMClient
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    c._openai = MagicMock()
    return c


def test_openai_cached_prompt_tokens_are_billed_at_the_cache_read_rate(rates):
    from shared.llm_client.catalog import CatalogModel
    from shared.llm_client.models import LLMRequest
    luna = CatalogModel(model_key="gpt-6-luna-openai", label="x", vendor="openai", provider="openai",
                        api_style="openai_chat", bedrock_profile_ids={}, callable_via=("llm_client",),
                        price_in_per_mtok=0.1, price_out_per_mtok=0.5, enabled=True)
    c = _client()
    usage = MagicMock(prompt_tokens=10_000, completion_tokens=1000)
    usage.prompt_tokens_details = MagicMock(cached_tokens=8000)
    c._openai.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="ok"), finish_reason="stop")], usage=usage)
    with patch("shared.llm_client.client.get_model_sync", return_value=luna), \
            patch("shared.llm_client.pricing.deployment_for", return_value=None):
        resp = c._call_openai(LLMRequest(system_prompt="s", user_prompt="u"), model="gpt-6-luna", catalog_model=luna)
    assert resp.input_tokens == 10_000 and resp.cache_read_tokens == 8000 and resp.pricing_mode == "catalog"
    assert resp.cost_usd == pytest.approx((2000 * 0.0001 + 8000 * 0.00001 + 1000 * 0.0005) / 1000)


@pytest.mark.parametrize("details", [None, MagicMock(), {"cached_tokens": 5}, {"cached_tokens": 10**9}])
def test_openai_cached_tokens_are_read_defensively(details):
    from shared.llm_client.client import _openai_cached_tokens
    usage = MagicMock(prompt_tokens_details=details)
    expected = {None: 0, 5: 5, 10**9: 100}.get(details.get("cached_tokens") if isinstance(details, dict) else None, 0)
    assert _openai_cached_tokens(usage, 100) == expected


def test_anthropic_usage_fields_for_one_hour_writes_and_geo():
    from shared.llm_client.adapters.anthropic_messages import _int_field, _str_field
    usage = MagicMock(cache_creation={"ephemeral_1h_input_tokens": 700, "ephemeral_5m_input_tokens": 300},
                      inference_geo="us")
    assert _int_field(usage.cache_creation, "ephemeral_1h_input_tokens") == 700
    assert _str_field(usage, "inference_geo") == "us"
    assert _int_field(None, "ephemeral_1h_input_tokens") == 0 and _int_field(MagicMock(), "x") == 0
    assert _str_field(MagicMock(), "inference_geo") is None


def test_converse_cost_uses_the_deployment_override(rates):
    c = _client()
    with patch("shared.llm_client.pricing.deployment_for", return_value=_dep(price_in_override=10.0)) as find:
        q = c._quote("sonnet-5-5", Usage(input_tokens=1000), "acc3")
    find.assert_called_once_with("sonnet-5-5", "acc3")
    assert q.pricing_mode == "override" and q.cost_usd == pytest.approx(0.01)


# ── the log and the rollup ────────────────────────────────────────────────────────────────────────────────────────

def test_s6_extras_only_when_the_row_has_something_to_say(rates):
    assert call_log._s6_extras("sonnet-5-5", None, None, None) is None
    assert call_log._s6_extras("sonnet-5-5", 0, 0, None) is None
    assert call_log._s6_extras("sonnet-5-5", 4000, 0, "catalog") == (4000, 0, "catalog")
    assert call_log._s6_extras("mystery-model", None, None, None) == (None, None, "estimate")   # flagged anyway
    assert call_log._s6_extras("sonnet-5-5", MagicMock(), True, MagicMock()) is None           # test doubles


def _row():
    return ("t", "s", "writer", "m", 1, 1, 0.1, "{}", None, None, None, None, None, None, None)


@pytest.mark.asyncio
async def test_rows_without_s6_fields_keep_the_pre_s6_statement(monkeypatch):
    monkeypatch.setattr(call_log, "_s6_missing_at", 0.0)
    conn = MagicMock(execute=AsyncMock())
    await call_log._execute_rows(conn, call_log._INSERT_SQL, [_row()], [None], many=False)
    assert conn.execute.call_args.args[0] == call_log._INSERT_SQL and len(conn.execute.call_args.args) == 16


@pytest.mark.asyncio
async def test_s6_rows_use_the_new_columns_and_fall_back_before_migration_312(monkeypatch):
    monkeypatch.setattr(call_log, "_s6_missing_at", 0.0)
    conn = MagicMock(execute=AsyncMock())
    await call_log._execute_rows(conn, call_log._INSERT_SQL, [_row()], [(10, 0, "catalog")], many=False)
    assert conn.execute.call_args.args[0] == call_log._INSERT_S6_SQL and conn.execute.call_args.args[-1] == "catalog"

    calls = []

    async def execute(sql, *args):
        calls.append((sql, len(args)))
        if sql == call_log._INSERT_S6_SQL:
            raise asyncpg.exceptions.UndefinedColumnError('column "pricing_mode" does not exist')
    conn = MagicMock(execute=execute)
    await call_log._execute_rows(conn, call_log._INSERT_SQL, [_row()], [(10, 0, "catalog")], many=False)
    await call_log._execute_rows(conn, call_log._INSERT_SQL, [_row()], [(10, 0, "catalog")], many=False)
    assert calls == [(call_log._INSERT_S6_SQL, 18), (call_log._INSERT_SQL, 15), (call_log._INSERT_SQL, 15)]


@pytest.mark.asyncio
async def test_a_relabelled_insert_never_takes_the_s6_path(monkeypatch):
    monkeypatch.setattr(call_log, "_s6_missing_at", 0.0)
    relabelled = call_log._INSERT_SQL.replace("$2", "'adhoc_aa729'")
    conn = MagicMock(executemany=AsyncMock())
    await call_log._execute_rows(conn, relabelled, [_row(), _row()], [(1, 2, "estimate"), None], many=True)
    sql, args = conn.executemany.call_args.args
    assert sql == relabelled and all(len(a) == 15 for a in args)


def test_billed_record_carries_the_s6_fields():
    from shared.llm_client.models import LLMResponse
    rec = call_log.billed_record(LLMResponse(content="", model_used="m", provider="anthropic", cache_read_tokens=3,
                                             cache_write_tokens=4, pricing_mode="catalog+geo_us"))
    assert (rec["cache_read_tokens"], rec["cache_write_tokens"], rec["pricing_mode"]) == (3, 4, "catalog+geo_us")
    rows = call_log.billed_unusable_rows(stage="s", role="judge", billed=[rec], error="x")
    assert rows[0]["pricing_mode"] == "catalog+geo_us" and set(rows[0]) <= set(call_log._ROW_FIELDS)


@pytest.mark.asyncio
@pytest.mark.parametrize("ready, expected", [(6, "LLM_DAILY_INSERT_S6_SQL"), (3, "LLM_DAILY_INSERT_SQL")])
async def test_rollup_sums_the_cache_once_both_tables_have_the_columns(ready, expected):
    from services.retention import rollups

    class Txn:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False
    conn = MagicMock(transaction=MagicMock(return_value=Txn()), fetchval=AsyncMock(return_value=ready),
                     execute=AsyncMock(return_value="INSERT 0 7"))
    assert await rollups.write(conn, "llm_call_daily", "cutoff") == 7
    assert conn.execute.call_args.args[0] == getattr(rollups, expected)
    assert "estimate_calls" in rollups.LLM_DAILY_INSERT_S6_SQL


def test_quote_type_is_what_the_client_returns(rates):
    assert isinstance(_client()._quote("sonnet-5-5", Usage(input_tokens=1)), Quote)
