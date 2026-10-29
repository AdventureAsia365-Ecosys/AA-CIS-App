"""AA-665 — Cost Operations: DFS call log job_id (C2), DFS runs by job (C2), month-end forecast vs budget (C3).

No DB: asyncpg is mocked; the SQL itself is exercised by tests/integration/test_aa665_cost_ops_sql.py.
"""
import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.core import staff_authz
from api.services import cost_ops
from shared.dfs_client import call_log as dfs_log
from shared.llm_client.call_log import bind_job

JOB = "6f1c2a9e-0b7d-4c55-9a1e-3f2d4b6c8e01"


# ── C2: the DFS call log writes the bound job id ─────────────────────────────────────────────────────────────────────

async def _record(**kwargs):
    conn = AsyncMock()
    with patch("shared.dfs_client.call_log.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("shared.dfs_client.call_log.get_database_url", return_value="postgres://fake"):
        await dfs_log.record_dfs_call(endpoint="search_volume", fetched_live=True, cost_usd=0.06, **kwargs)
    return conn.execute.call_args.args


def test_insert_names_job_id_last():
    assert "keyword_count, meta, job_id)" in dfs_log._INSERT_SQL
    assert "$11::uuid" in dfs_log._INSERT_SQL


@pytest.mark.asyncio
async def test_bound_job_id_is_logged():
    with bind_job(JOB):
        args = await _record()
    assert args[11] == JOB
    assert args[3] == "search_volume"         # the older bind order is unchanged


@pytest.mark.asyncio
async def test_no_job_logs_null():
    args = await _record()
    assert args[11] is None


@pytest.mark.asyncio
async def test_explicit_job_id_wins_and_none_means_none():
    other = str(uuid.uuid4())
    with bind_job(JOB):
        assert (await _record(job_id=other))[11] == other
        assert (await _record(job_id=None))[11] is None


@pytest.mark.asyncio
async def test_non_uuid_job_id_is_dropped_not_the_row():
    """S254 lesson: a slug bound as job id made asyncpg reject the row; here the row is kept, job_id NULL."""
    with bind_job("aa_internal"):
        args = await _record()
    assert args[11] is None
    assert args[6] == 0.06


@pytest.mark.asyncio
async def test_pool_variant_logs_bound_job_id():
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    with bind_job(JOB):
        await dfs_log.record_dfs_call_with_pool(pool, endpoint="keywords_for_keywords", fetched_live=True,
                                                cost_usd=0.09)
    assert conn.execute.call_args.args[11] == JOB


def test_sync_wrapper_reads_the_job_id_in_the_callers_context():
    seen = {}

    async def fake_record(**kwargs):
        seen.update(kwargs)

    with patch("shared.dfs_client.call_log.record_dfs_call", fake_record):
        with bind_job(JOB):
            dfs_log.record_dfs_call_sync(endpoint="serp_advanced", fetched_live=True, cost_usd=0.002)
    assert seen["job_id"] == JOB


@pytest.mark.asyncio
async def test_sync_wrapper_inside_a_loop_keeps_the_job_id():
    seen = {}

    async def fake_record(**kwargs):
        seen.update(kwargs)

    with patch("shared.dfs_client.call_log.record_dfs_call", fake_record):
        with bind_job(JOB):
            dfs_log.record_dfs_call_sync(endpoint="serp_advanced", fetched_live=True, cost_usd=0.002)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert seen["job_id"] == JOB


# ── C2: runs by job ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_payload_scope_is_short_and_ordered():
    payload = {"country": "Bhutan", "tour_ids": ["a", "b", "c"], "dry_run": False, "note": "x" * 200,
               "nested": {"a": 1}, "empty": None, "one": [1]}
    scope = cost_ops.payload_scope(payload)
    assert scope == {"country": "Bhutan", "tour_ids": "3 items", "dry_run": "False", "note": "x" * 80,
                     "nested": "1 field", "one": "1 item"}
    assert cost_ops.payload_scope('{"country": "Laos"}') == {"country": "Laos"}
    assert cost_ops.payload_scope(None) == {}
    assert len(cost_ops.payload_scope({f"k{i}": i for i in range(20)})) == 8


class _Pool:
    """fetch / fetchrow answer by SQL text."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self.answers.get(sql, [])

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        rows = self.answers.get(sql, [])
        return rows[0] if rows else None


def _run_row(job_id, cost, found=True, **job):
    base = {"job_id": uuid.UUID(job_id) if job_id else None, "calls": 3, "live_calls": 2, "cache_hits": 1,
            "dfs_cost_usd": cost, "keywords": 40, "first_call_at": datetime(2026, 9, 25, 1, tzinfo=timezone.utc),
            "last_call_at": datetime(2026, 9, 25, 2, tzinfo=timezone.utc), "endpoints": ["search_volume"],
            "job_found": found, "kind": None, "status": None, "created_by": None, "payload": None,
            "job_reported_cost_usd": None, "error": None, "parent_job_id": None, "attempt": None,
            "job_created_at": None, "started_at": None, "finished_at": None, "llm_cost_usd": 0.0}
    base.update(job)
    return base


@pytest.mark.asyncio
async def test_dfs_runs_shapes_linked_and_unlinked_groups():
    since = datetime(2026, 9, 25, tzinfo=timezone.utc)
    until = since + timedelta(days=1)
    pool = _Pool({
        cost_ops.DFS_RUNS_SQL: [
            _run_row(JOB, 4.2, kind="segment_research", status="stopped_budget", created_by="admin:ana",
                     payload={"country": "Bhutan", "segment_ids": [1, 2]}, job_reported_cost_usd=4.2,
                     error="E" * 400, attempt=1, llm_cost_usd=0.5,
                     started_at=since, finished_at=since + timedelta(hours=2)),
            _run_row(None, 0.3, found=False),
        ],
        cost_ops.DFS_SUMMARY_SQL: [{"calls": 6, "dfs_cost_usd": 4.5, "linked_calls": 3, "linked_cost_usd": 4.2,
                                    "jobs": 1}],
    })
    out = await cost_ops.dfs_runs(pool, since, until, limit=50)
    linked, unlinked = out["runs"]
    assert linked["job_id"] == JOB and linked["attribution"] == "job"
    assert linked["job"]["kind"] == "segment_research" and linked["job"]["created_by"] == "admin:ana"
    assert linked["job"]["scope"] == {"country": "Bhutan", "segment_ids": "2 items"}
    assert len(linked["job"]["error"]) == 300
    assert linked["llm_cost_usd"] == 0.5 and linked["dfs_cost_usd"] == 4.2
    assert unlinked["job_id"] is None and unlinked["attribution"] == "none" and unlinked["job"] is None
    assert out["summary"] == {"calls": 6, "dfs_cost_usd": 4.5, "linked_calls": 3, "linked_cost_usd": 4.2,
                              "jobs": 1}
    assert out["inferred"] is None and out["truncated"] is False
    assert pool.calls[0][1] == (since, until, 50)


@pytest.mark.asyncio
async def test_dfs_runs_infer_window_is_capped():
    since = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        await cost_ops.dfs_runs(_Pool({}), since, since + timedelta(days=40), infer_unlinked=True)


@pytest.mark.asyncio
async def test_dfs_runs_infer_unlinked():
    since = datetime(2026, 9, 25, tzinfo=timezone.utc)
    jid = uuid.UUID(JOB)
    pool = _Pool({
        cost_ops.DFS_INFER_SQL: [
            {"job_id": jid, "attribution": "window", "candidate_kinds": [], "incident_id": None, "calls": 5,
             "live_calls": 5, "dfs_cost_usd": 3.0, "first_call_at": since, "last_call_at": since},
            {"job_id": None, "attribution": "ambiguous", "incident_id": None,
             "candidate_kinds": ["s1_rewrite", "segment_research"],
             "calls": 2, "live_calls": 2, "dfs_cost_usd": 0.1, "first_call_at": since, "last_call_at": since},
        ],
        cost_ops.JOBS_BY_ID_SQL: [{"id": jid, "kind": "segment_research", "status": "failed", "created_by": None,
                                   "payload": {}, "job_reported_cost_usd": 3.0, "error": None, "started_at": since,
                                   "finished_at": None}],
    })
    out = await cost_ops.dfs_runs(pool, since, since + timedelta(days=1), infer_unlinked=True)
    window, ambiguous = out["inferred"]
    assert window["job_id"] == JOB and window["job"]["kind"] == "segment_research"
    assert ambiguous["job_id"] is None and ambiguous["candidate_kinds"] == ["s1_rewrite", "segment_research"]


# ── C3: forecast ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_project_current_month():
    start, end = date(2026, 10, 1), date(2026, 11, 1)
    daily = {date(2026, 10, d): 2.0 for d in range(1, 28)}      # 2 USD a day on 1..27/10
    daily[date(2026, 10, 28)] = 0.5                              # today, partial
    p = cost_ops.project(daily, start, end, complete_until=date(2026, 10, 28))
    assert p["mtd_usd"] == 54.5
    assert p["run_rate_usd_per_day"] == 2.0
    assert p["days_left"] == 4                                   # 28, 29, 30, 31
    assert p["forecast_usd"] == 54.0 + 2.0 * 4


def test_project_first_day_uses_previous_month_rate():
    start, end = date(2026, 11, 1), date(2026, 12, 1)
    daily = {date(2026, 10, d): 3.0 for d in range(25, 32)}
    p = cost_ops.project(daily, start, end, complete_until=date(2026, 11, 1))
    assert p["mtd_usd"] == 0 and p["run_rate_usd_per_day"] == 3.0 and p["forecast_usd"] == 90.0


def test_project_closed_month_is_its_total():
    start, end = date(2026, 9, 1), date(2026, 10, 1)
    daily = {date(2026, 9, d): 1.0 for d in range(1, 31)}
    p = cost_ops.project(daily, start, end, complete_until=end)
    assert p == {"mtd_usd": 30.0, "run_rate_usd_per_day": 1.0, "days_left": 0, "forecast_usd": 30.0}


def test_project_missing_days_mean_zero_for_logs_unknown_for_ce():
    start, end = date(2026, 10, 1), date(2026, 11, 1)
    daily = {date(2026, 10, 26): 7.0}
    assert cost_ops.project(daily, start, end, date(2026, 10, 27))["run_rate_usd_per_day"] == 1.0
    assert cost_ops.project(daily, start, end, date(2026, 10, 27), mean_over_present=True)[
        "run_rate_usd_per_day"] == 7.0


def test_budget_status():
    assert cost_ops.budget_status(260, 250) == "over"
    assert cost_ops.budget_status(210, 250) == "warn"
    assert cost_ops.budget_status(100, 250) == "ok"
    assert cost_ops.budget_status(100, None) == "no_budget"
    assert cost_ops.budget_status(None, 250) == "no_data"
    assert cost_ops.budget_status(240, 250, alert_pct=99) == "ok"


def test_aws_budgets_env_override(monkeypatch):
    monkeypatch.setenv(cost_ops.AWS_BUDGET_ENV, "acc2=300, acc3=20,bad")
    assert cost_ops.aws_budgets() == {"acc2": 300.0, "acc3": 20.0}
    monkeypatch.delenv(cost_ops.AWS_BUDGET_ENV)
    assert cost_ops.aws_budgets() == cost_ops.AWS_MONTHLY_BUDGET_USD


@pytest.mark.asyncio
async def test_cost_forecast_providers_aws_and_budgets(monkeypatch):
    now = datetime(2026, 10, 28, 9, tzinfo=timezone.utc)
    monkeypatch.setattr("shared.aws_client.cost_explorer.account_key",
                        lambda a: {"A2": "acc2", "A3": "acc3"}.get(a))
    monkeypatch.delenv(cost_ops.AWS_BUDGET_ENV, raising=False)
    ai = []
    for d in range(21, 28):
        day = date(2026, 10, d)
        ai += [{"day": day, "provider": "bedrock", "account": "acc3", "cost_usd": 5.0},
               {"day": day, "provider": "bedrock", "account": None, "cost_usd": 0.1},
               {"day": day, "provider": "openai", "account": None, "cost_usd": 1.0},
               {"day": day, "provider": "typesafe", "account": None, "cost_usd": 0.2}]
    old = datetime(2026, 10, 27, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 10, 28, 1, tzinfo=timezone.utc)
    ce_rows = [{"account_id": "A2", "day": date(2026, 10, d), "amount_usd": 8.0, "fetched_at": old}
               for d in range(1, 26)]
    ce_rows += [{"account_id": "A3", "day": date(2026, 10, d), "amount_usd": 10.0, "fetched_at": old}
                for d in range(20, 26)]
    # acc2's App-tag snapshot was fetched later for 25/10 and also has 26/10: those days win
    app_rows = [{"account_id": "A2", "day": date(2026, 10, 25), "amount_usd": 9.0, "fetched_at": newer},
                {"account_id": "A2", "day": date(2026, 10, 26), "amount_usd": 9.0, "fetched_at": newer}]
    pool = _Pool({
        cost_ops.AI_DAILY_SQL: ai,
        cost_ops.DFS_DAILY_SQL: [{"day": date(2026, 10, 25), "cost_usd": 7.0}],
        cost_ops.CE_DAILY_SQL: ce_rows,
        cost_ops.CE_APP_DAILY_SQL: app_rows,
        cost_ops.BUDGETS_SQL: [{"provider": "dfs", "per_day_usd": 10.0, "alert_pct": 80},
                               {"provider": "bedrock", "per_day_usd": 1.0, "alert_pct": 80}],
    })
    out = await cost_ops.cost_forecast(pool, None, now=now)
    assert out["month"] == "2026-10" and out["days_in_month"] == 31 and out["is_current_month"]
    assert out["complete_days"] == 27
    # the queries reach back RUN_RATE_DAYS before today only when the month is younger than that
    assert pool.calls[0][1] == (date(2026, 10, 1), date(2026, 10, 29))
    by = {p["key"]: p for p in out["providers"]}
    assert set(by) == {"bedrock", "openai", "jev", "dfs"}
    assert by["bedrock"]["run_rate_usd_per_day"] == 5.1
    assert by["bedrock"]["forecast_usd"] == pytest.approx(7 * 5.1 + 5.1 * 4)
    assert by["bedrock"]["budget_usd"] == 31.0 and by["bedrock"]["status"] == "over"
    assert [a["account"] for a in by["bedrock"]["accounts"]] == ["acc3", "acc2"]   # account-less Bedrock = acc2
    assert by["jev"]["mtd_usd"] == pytest.approx(1.4) and by["jev"]["status"] == "no_budget"
    assert by["dfs"]["budget_usd"] == 310.0 and by["dfs"]["status"] == "ok"
    assert by["dfs"]["run_rate_usd_per_day"] == 1.0
    aws = {a["account"]: a for a in out["aws_accounts"]}
    a2 = aws["acc2"]
    assert a2["covered_to"] == "2026-10-26" and a2["fetched_at"] == newer.isoformat()
    assert a2["mtd_usd"] == pytest.approx(24 * 8 + 9 + 9)
    assert a2["run_rate_usd_per_day"] == pytest.approx((5 * 8 + 9 + 9) / 7, abs=1e-3)
    assert a2["days_left"] == 5                                  # 27..31/10 projected
    assert a2["budget_usd"] == 300.0 and a2["days_missing"] == 0
    a3 = aws["acc3"]
    assert a3["covered_from"] == "2026-10-20" and a3["days_missing"] == 19
    assert a3["run_rate_usd_per_day"] == 10.0                    # mean over the 6 stored days
    assert set(aws) == {"acc2", "acc3"}   # AA-998: no retired-account row without its data


@pytest.mark.asyncio
async def test_cost_forecast_shows_retired_account_history_without_its_id(monkeypatch):
    """AA-998: a month that still holds spend of the retired fallback account (llm_call_log rows, stored CE days)
    shows it as one 'retired' row after acc2 / acc3, never as a raw account ID."""
    now = datetime(2026, 9, 30, 9, tzinfo=timezone.utc)
    monkeypatch.setattr("shared.aws_client.cost_explorer.account_key",
                        lambda a: {"A2": "acc2", "A3": "acc3"}.get(a, "retired"))
    monkeypatch.delenv(cost_ops.AWS_BUDGET_ENV, raising=False)
    ai = [{"day": date(2026, 9, 10), "provider": "bedrock-satellite", "account": "acc1", "cost_usd": 0.5},
          {"day": date(2026, 9, 10), "provider": "bedrock-satellite", "account": "acc3", "cost_usd": 2.0}]
    fetched = datetime(2026, 9, 29, 1, tzinfo=timezone.utc)
    ce_rows = [{"account_id": "A1", "day": date(2026, 9, 10), "amount_usd": 0.97, "fetched_at": fetched}]
    pool = _Pool({cost_ops.AI_DAILY_SQL: ai, cost_ops.DFS_DAILY_SQL: [], cost_ops.CE_DAILY_SQL: ce_rows,
                  cost_ops.CE_APP_DAILY_SQL: [], cost_ops.BUDGETS_SQL: []})
    out = await cost_ops.cost_forecast(pool, "2026-09", now=now)
    bedrock = next(p for p in out["providers"] if p["key"] == "bedrock")
    assert [a["account"] for a in bedrock["accounts"]] == ["acc3", "retired"]
    assert bedrock["accounts"][1]["label"] == "Retired account"   # AA-998: no ID, never the internal key
    aws = [a["account"] for a in out["aws_accounts"]]
    assert aws == ["acc2", "acc3", "retired"]
    assert out["aws_accounts"][0]["label"] == "111122223333 (main infrastructure)"   # AA-998: the ID, not the key
    retired = out["aws_accounts"][2]
    assert retired["mtd_usd"] == pytest.approx(0.97) and retired["status"] == "no_budget"
    assert "A1" not in str(out)


@pytest.mark.asyncio
async def test_cost_forecast_aws_partial_today_counts_in_mtd_not_in_the_rate(monkeypatch):
    """A Cost Explorer fetch made today stores today's partial day: month to date includes it, the run rate does not
    (else a few cents for today drag the 7-day mean down and the forecast with it)."""
    now = datetime(2026, 10, 28, 9, tzinfo=timezone.utc)
    monkeypatch.setattr("shared.aws_client.cost_explorer.account_key", lambda a: {"A2": "acc2"}.get(a))
    monkeypatch.delenv(cost_ops.AWS_BUDGET_ENV, raising=False)
    fetched = datetime(2026, 10, 28, 8, tzinfo=timezone.utc)
    ce_rows = [{"account_id": "A2", "day": date(2026, 10, d), "amount_usd": 8.0, "fetched_at": fetched}
               for d in range(1, 28)]
    ce_rows.append({"account_id": "A2", "day": date(2026, 10, 28), "amount_usd": 0.5, "fetched_at": fetched})
    pool = _Pool({cost_ops.CE_DAILY_SQL: ce_rows})
    out = await cost_ops.cost_forecast(pool, None, now=now)
    a2 = next(a for a in out["aws_accounts"] if a["account"] == "acc2")
    assert a2["covered_to"] == "2026-10-28"
    assert a2["mtd_usd"] == pytest.approx(27 * 8 + 0.5)
    assert a2["run_rate_usd_per_day"] == pytest.approx(8.0)            # 21..27/10, today left out
    assert a2["days_present_in_rate_window"] == 7
    assert a2["days_left"] == 4 and a2["forecast_usd"] == pytest.approx(27 * 8 + 4 * 8)


@pytest.mark.asyncio
async def test_cost_forecast_rejects_a_future_month():
    with pytest.raises(ValueError):
        await cost_ops.cost_forecast(_Pool({}), "2099-01", now=datetime(2026, 10, 28, tzinfo=timezone.utc))


# ── routes ───────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_new_reads_are_admin_money_reads():
    assert staff_authz.POLICY[("GET", "/admin/dfs-usage/runs")] == staff_authz.SPEND_READ
    assert staff_authz.POLICY[("GET", "/admin/cost-ops/forecast")] == staff_authz.SPEND_READ


def test_routes_registered():
    from api.routers.admin_llm_ops import router

    paths = {(m, r.path) for r in router.routes for m in getattr(r, "methods", ())}
    assert ("GET", "/admin/dfs-usage/runs") in paths
    assert ("GET", "/admin/cost-ops/forecast") in paths


def test_aws_budgets_match_the_terraform_defaults_when_infra_is_checked_out():
    """AA-1001 R0: the Cost Ops page showed acc2 against 250 USD while Terraform had set 300 since AA-896 (S255)."""
    import re
    from pathlib import Path
    infra = Path(__file__).resolve().parents[3] / "infra" / "AA-CIS-Infra" / "accounts"
    pins = {"acc2": (infra / "aa365" / "variables.tf", "total_monthly_budget_usd"),
            "acc3": (infra / "acc3-bedrock" / "cost_guard.tf", "acc3_budget_monthly_usd")}
    if not all(p.exists() for p, _ in pins.values()):
        pytest.skip("AA-CIS-Infra is not checked out next to this repo (CI)")
    for key, (path, var) in pins.items():
        block = re.search(r'variable "%s" \{(.*?)\n\}' % var, path.read_text(), re.S)
        assert block, f"{var} not found in {path}"
        default = float(re.search(r"default\s*=\s*([0-9.]+)", block.group(1)).group(1))
        assert cost_ops.AWS_MONTHLY_BUDGET_USD[key] == default, key
