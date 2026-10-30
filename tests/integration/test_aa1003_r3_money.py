"""AA-1003 R3 against real Postgres: every SQL the money reports run, as written, on the replayed schema.

Fixture month 2023-03 (no other test writes it): LLM ledger rows split between a rolled-up day (shared.llm_call_daily)
and raw days (shared.llm_call_log), DataForSEO / geocoder / Jev ledger rows, a stored Cost Explorer snapshot, a budget
bell and an OPS-12 run. The readers and the two collectors run on one connection (ConnPool), the account key lookup
(STS) is stubbed. Every row this module writes is deleted at the end; the contract views do not exist in this DB, so
`demand` must come back as "not readable", never raise.
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.reports import money, money_sources as src, registry  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
UTC = timezone.utc
M1, ML = date(2023, 3, 1), date(2023, 3, 31)
ROLLED, RAW = date(2023, 3, 2), date(2023, 3, 3)
STAGE = "aa1003_r3_probe"
ACCOUNT = "<account>"
PLATFORM_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _ts(day: date, hour: int = 10) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


@pytest_asyncio.fixture
async def env(monkeypatch):
    from shared.aws_client import bronze, cost_explorer as ce

    monkeypatch.setattr(bronze, "caller_account_id", lambda: ACCOUNT)       # no STS call (labels only)
    monkeypatch.setattr(ce, "account_key", lambda a: "acc2" if a == ACCOUNT else "retired")
    monkeypatch.setattr(ce, "acc2_account_id", lambda: ACCOUNT)
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    await pool.execute(
        "INSERT INTO shared.llm_call_daily (day, app, tenant_id, stage, role, model, provider, account, is_shadow, "
        "calls, fallback_calls, tokens_in, tokens_out, cost_usd, updated_at) VALUES "
        "($1, 'cis', NULL, $2, 'writer', 'm-probe', 'bedrock', 'acc3', false, 10, 1, 1000, 500, 2.5, now())",
        ROLLED, STAGE)
    for shadow, cost in ((False, 1.0), (True, 0.5)):
        await pool.execute(
            "INSERT INTO shared.llm_call_log (id, stage, role, model, tokens_in, tokens_out, cost_usd, "
            "quality_signal, created_at, account, fallback_used, provider) VALUES "
            "(gen_random_uuid(), $1, 'writer', 'm-probe', 100, 50, $2, $3::jsonb, $4, 'acc3', false, 'bedrock')",
            STAGE, cost, json.dumps({"app": "cis", "shadow": shadow}), _ts(RAW))
    await pool.execute("INSERT INTO shared.dfs_call_log (endpoint, keyword_count, cost_usd, fetched_live, created_at) "
                       "VALUES ($1, 20, 0.6, true, $2)", STAGE, _ts(RAW))
    await pool.execute("INSERT INTO geo.geocode_call (called_at, provider, requests, cost_usd, endpoint) "
                       "VALUES ($1, $2, 500, 0.1, 'search')", _ts(RAW), STAGE)
    await pool.execute("INSERT INTO shared.decision_log (created_at, stage, question_key, subject_key, mode, zone, "
                       "cost_usd, cached) VALUES ($1, $2, 'q', 's', 'shadow', 'accept', 0.002, false), "
                       "($1, $2, 'q', 's', 'shadow', 'accept', 0, true)", _ts(RAW), STAGE)
    await pool.execute("INSERT INTO shared.cost_explorer_snapshot (account_id, service, period_start, period_end, "
                       "amount_usd, unit, raw) VALUES ($1, $2, $3, $4, 7.5, 'USD', NULL)",
                       ACCOUNT, STAGE, RAW, RAW + timedelta(days=1))
    await pool.execute("INSERT INTO shared.cost_explorer_app_snapshot (account_id, app_tag, service, period_start, "
                       "period_end, amount_usd, unit) VALUES ($1, 'cis', $2, $3, $4, 6.0, 'USD')",
                       ACCOUNT, STAGE, RAW, RAW + timedelta(days=1))
    await pool.execute("INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, "
                       "payload, target_roles, created_at) VALUES ($1::uuid, 'system', $2, 'spend_budget', $3, "
                       "$4::jsonb, ARRAY['admin'], $5)", PLATFORM_TENANT_ID, src.BUDGET_ALERT_EVENT, STAGE,
                       json.dumps({"message": "probe stopped by its per-run budget", "hard_stop": True}), _ts(RAW))
    await pool.execute("INSERT INTO shared.report_run (report_id, period_start, period_end, status, trigger, "
                       "triggered_by, output) VALUES ('OPS-12', $1, $1, 'succeeded', 'manual', $2, $3::jsonb)",
                       RAW, STAGE, json.dumps({"attention": ["x"], "facts": {"apps": {"items": [
                           {"id": "cis", "status": "ok"}]}}}))
    yield pool
    await pool.execute("DELETE FROM shared.llm_call_daily WHERE stage = $1", STAGE)
    await pool.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)
    await pool.execute("DELETE FROM shared.dfs_call_log WHERE endpoint = $1", STAGE)
    await pool.execute("DELETE FROM geo.geocode_call WHERE provider = $1", STAGE)
    await pool.execute("DELETE FROM shared.decision_log WHERE stage = $1", STAGE)
    await pool.execute("DELETE FROM shared.cost_explorer_snapshot WHERE service = $1", STAGE)
    await pool.execute("DELETE FROM shared.cost_explorer_app_snapshot WHERE service = $1", STAGE)
    await pool.execute("DELETE FROM shared.notifications WHERE entity_id = $1", STAGE)
    await pool.execute("DELETE FROM shared.report_run WHERE triggered_by = $1", STAGE)
    await pool.close()


async def test_ledger_readers_run_as_written(env):
    async with env.acquire() as c:
        llm = [r for r in await src.llm_ledger(c, M1, ML + timedelta(days=1)) if r["stage"] == STAGE]
        dfs = await src.dfs_daily(c, M1, ML + timedelta(days=1))
        geo = [r for r in await src.geo_daily(c, M1, ML + timedelta(days=1)) if r["provider"] == STAGE]
        jev = await src.jev_daily(c, M1, ML + timedelta(days=1))
        aws = await src.ce_daily(c, M1, ML + timedelta(days=1))
        alerts = [a for a in await src.budget_alerts(c, M1, ML + timedelta(days=1)) if a["provider"] == STAGE]
        budgets = await src.spend_budgets(c)
        published = await src.published_tours(c, M1, ML + timedelta(days=1))
        demand = await src.demand(c, M1, ML)
        runs = await src.digest_runs(c, M1, ML)
        db = await src.db_capacity(c)
    by_day = {(r["day"], r["is_shadow"]): r for r in llm}
    assert by_day[(ROLLED, False)]["cost_usd"] == pytest.approx(2.5) and by_day[(ROLLED, False)]["calls"] == 10
    assert by_day[(RAW, False)]["cost_usd"] == pytest.approx(1.0) and by_day[(RAW, True)]["cost_usd"] == 0.5
    assert by_day[(RAW, False)]["tokens_in"] == 100 and by_day[(RAW, False)]["app"] == "cis"
    assert any(r["day"] == RAW and r["keywords"] >= 20 for r in dfs)
    assert geo == [{"day": RAW, "provider": STAGE, "requests": 500, "cost_usd": pytest.approx(0.1)}]
    assert any(r["day"] == RAW and r["uncached"] >= 1 for r in jev)
    assert {"day": RAW, "account": "acc2"}.items() <= next(r for r in aws if r["day"] == RAW).items()
    assert all("account_id" not in r for r in aws)
    assert alerts and alerts[0]["stopped"] is True and alerts[0]["hard_stop"] == "true"
    assert isinstance(budgets, list) and set(published) == {"total", "first_published", "republished"}
    assert published["total"] == published["first_published"] + published["republished"]
    assert "error" in demand["tripplanner"] and "error" in demand["booking"]      # no contract views in this DB
    assert runs[0]["day"] == RAW and runs[0]["apps"] == [{"id": "cis", "status": "ok"}]
    assert db["database_bytes"] > 0 and db["max_connections"] > 0 and db["top_relations"]


async def test_collectors_and_renderers_on_the_replayed_schema(env, monkeypatch):
    async def no_rds(first, end):
        return {"unavailable": "not_permitted: test"}
    monkeypatch.setattr(src, "rds_metrics", no_rds)
    now = datetime(2023, 4, 5, 3, tzinfo=UTC)
    reg = registry.load()
    async with env.acquire() as c:
        month = await src.collect_month(c, M1, ML, now, src.MONTH_PARTS)
        week = await src.collect_week(c, RAW - timedelta(days=4), RAW + timedelta(days=2), with_business=True)
    unreadable = {k: v for k, v in month.items() if money.failed(v)}
    assert set(unreadable) <= {"rds"}, unreadable
    assert month["cost_by_app:2023-03"]["aws"]["total_usd"] >= 6.0
    assert month["services"]["accounts"] and all("account_id" not in a for a in month["services"]["accounts"])
    assert month["forecast_end"]["month"] == "2023-03"
    assert {k: v for k, v in week.items() if money.failed(v)} == {}
    for rid in ("FI-01", "FI-03", "FI-04", "FI-05", "FI-06", "OPS-08"):
        out = money.MONTH_RENDER[rid](reg, reg.report(rid), M1, ML, month, now)
        assert out.markdown.startswith("# ") and out.output["report_id"] == rid
    lm = money.llm_month(month)
    assert lm["shadow_ratio"] is not None and lm["cost_usd"] >= 4.0
    out = money.render_ex01(reg, reg.report("EX-01"), RAW - timedelta(days=4), RAW + timedelta(days=2), week, now)
    assert out.output["platform"]["digests"] == 1
    # no K-CAT-LIVE snapshot in 2023: the live count of v_tour_liveness stands in (S259 fix), never "—"
    assert out.output["tours_live"]["source"] == "live" and out.output["tours_live"]["value"] is not None
