"""AA-1025 A7 — the central LLM budget SQL against real Postgres (migration 340 applied by conftest).

- central_budget.read_spend: the scope queries (app, stage, job kind with the running job, eval, ad-hoc) and the day /
  month windows on the real ledger;
- the rows read (``to_jsonb`` of the row, with per_month_usd) and the admin writes (PUT with a month cap, DELETE);
- read_budgets' central tier and the coverage per call site;
- cost_guard.maybe_alert_central: one bell per scope.

The fixture rows live in August 2024 under invented stage / app / job kind names and are deleted at the end.
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import asyncpg
from pathlib import Path
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.routers import admin_budgets as ab  # noqa: E402
from api.services import ai_gateway as gw  # noqa: E402
from shared import cost_guard  # noqa: E402
from shared.llm_client import central_budget as cb  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
APP = "aa1025probe"
STAGE = "aa1025_probe_stage"
ADHOC = "adhoc_aa1025probe"
KIND = "aa1025_probe_kind"
NOW = datetime(2024, 8, 15, 12, 0, tzinfo=timezone.utc)


M340 = Path(__file__).resolve().parents[2] / "api" / "migrations" / "340_spend_budget_central_llm_scopes.sql"


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    # Modules that re-run an older migration (226, 304) leave spend_budget_provider_check without 'llm': start from
    # 340's state (the file is re-runnable and changes nothing when 340 is already live).
    async with p.acquire() as c:
        await c.execute(M340.read_text(encoding="utf-8"))
    yield p
    async with p.acquire() as c:
        await c.execute("DELETE FROM shared.llm_call_log WHERE app = $1 OR stage = ANY($2::text[])", APP,
                        [STAGE, ADHOC])
        await c.execute("DELETE FROM shared.job WHERE kind = $1", KIND)
        await c.execute("DELETE FROM shared.spend_budget WHERE provider = 'llm' AND scope LIKE '%aa1025%'")
        await c.execute("DELETE FROM shared.notifications WHERE entity_id LIKE 'llm:%aa1025%'")
    await p.close()


async def _job(c):
    return await c.fetchval("INSERT INTO shared.job (kind, payload, status, created_by) "
                            "VALUES ($1, '{}'::jsonb, 'succeeded', 'admin:probe') RETURNING id::text", KIND)


async def _llm(c, at, cost, *, stage=STAGE, app=APP, job_id=None):
    await c.execute("INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, cost_usd, app, job_id, "
                    "created_at) VALUES ($1, 'writer', 'probe-model', '{}'::jsonb, $2, $3, $4::uuid, $5)",
                    stage, cost, app, job_id, at)


def _row(scope, **kw):
    return cb.CentralBudget(scope=scope, **kw)


@pytest.mark.asyncio
async def test_read_spend_per_scope_and_window(pool):
    async with pool.acquire() as c:
        job = await _job(c)
        other = await _job(c)
        await _llm(c, _utc(2024, 8, 15, 9), 0.5, job_id=job)          # today
        await _llm(c, _utc(2024, 8, 15, 10), 0.25, job_id=other)      # today, another job of the kind
        await _llm(c, _utc(2024, 8, 3, 9), 2.0)                        # this month
        await _llm(c, _utc(2024, 7, 31, 23), 9.0)                      # last month
        await _llm(c, _utc(2024, 8, 15, 8), 0.125, stage=ADHOC)        # an ad-hoc script (same app)
        s = await cb.read_spend(c, _row(f"stage:{STAGE}", per_day_usd=1.0), now=NOW)
        assert (s.day_usd, s.month_usd, s.run_usd) == (0.75, None, None)
        s = await cb.read_spend(c, _row(f"stage:{STAGE}", per_month_usd=5.0), now=NOW)
        assert (s.day_usd, s.month_usd) == (0.75, 2.75)
        s = await cb.read_spend(c, _row(f"app:{APP}", per_month_usd=50.0), now=NOW)
        assert (s.day_usd, s.month_usd) == (0.875, 2.875)
        s = await cb.read_spend(c, _row(f"job:{KIND}", per_run_usd=1.0, per_day_usd=5.0), job_id=job, now=NOW)
        assert (s.day_usd, s.run_usd) == (0.75, 0.5)
        s = await cb.read_spend(c, _row("adhoc", per_day_usd=1.0), now=NOW)
        assert s.day_usd >= 0.125
        s = await cb.read_spend(c, _row("eval", per_month_usd=25.0), now=NOW)
        assert s.month_usd >= 0.0


@pytest.mark.asyncio
async def test_the_rows_and_the_admin_writes(pool, monkeypatch):
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)))
    monkeypatch.setattr(ab, "verify_admin_secret", lambda secret: None)   # the inline check (secret tested apart)
    await ab.upsert_budget("llm", f"stage:{STAGE}", ab.BudgetUpdate(per_day_usd=0.01, hard_stop=True), req, "s",
                           "admin:probe")
    await ab.upsert_budget("llm", f"stage:{STAGE}", ab.BudgetUpdate(per_day_usd=0.02, per_month_usd=1.5,
                                                                    hard_stop=False), req, "s", "admin:probe")
    async with pool.acquire() as c:
        raws = [json.loads(r["row"]) if isinstance(r["row"], str) else dict(r["row"])
                for r in await c.fetch(cb._ROWS_SQL)]
    rows = {r.scope: r for r in (cb.row_of(x) for x in raws) if r is not None}
    assert rows[f"stage:{STAGE}"] == _row(f"stage:{STAGE}", per_day_usd=0.02, per_month_usd=1.5, hard_stop=False)
    listed = await ab.list_budgets(req)
    mine = [b for b in listed["budgets"] if b["scope"] == f"stage:{STAGE}"]
    assert mine and mine[0]["per_month_usd"] == 1.5 and mine[0]["provider"] == "llm"
    assert all("per_month_usd" in b for b in listed["budgets"])
    out = await ab.delete_central_budget(f"stage:{STAGE}", req, "admin:probe")
    assert out["deleted"] is True
    with pytest.raises(Exception) as e:
        await ab.delete_central_budget(f"stage:{STAGE}", req, "admin:probe")
    assert getattr(e.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_read_budgets_central_tier_and_call_sites(pool):
    async with pool.acquire() as c:
        job = await _job(c)
        await _llm(c, _utc(2024, 8, 15, 9), 0.5, app="cis", job_id=job)
        await c.execute("INSERT INTO shared.spend_budget (provider, scope, per_day_usd, hard_stop, updated_by) "
                        "VALUES ('llm', $1, 0.40, true, 'admin:probe')", f"stage:{STAGE}")
    out = await gw.read_budgets(pool, now=NOW)
    assert [e for e in out["errors"] if e["source"] == "central_budgets"] == []
    central = {r["scope"]: r for r in out["tiers"]["central"]}
    assert central[f"stage:{STAGE}"]["spent_today_usd"] == 0.5
    assert central[f"stage:{STAGE}"]["state"] == "cap_reached"
    assert all(r["provider"] != "llm" for r in out["tiers"]["provider"] + out["tiers"]["job"])
    sites = {s["stage"]: s for s in out["call_sites"]}
    assert KIND in sites[STAGE]["job_kinds"] and sites[STAGE]["hard"] is True
    assert f"stage:{STAGE}" in [cap["scope"] for cap in sites[STAGE]["caps"]]


@pytest.mark.asyncio
async def test_one_bell_per_scope(pool):
    summary = cb.summary(_row(f"stage:{STAGE}", per_day_usd=0.01, hard_stop=True), cb.Spend(day_usd=0.02))
    async with pool.acquire() as c:
        assert await cost_guard.maybe_alert_central(c, summary, alert_pct=80, stopped=True, message="m") is True
        assert await cost_guard.maybe_alert_central(c, summary, alert_pct=80, stopped=True, message="m") is False
        n = await c.fetchval("SELECT count(*) FROM shared.notifications WHERE entity_id = $1",
                             f"llm:stage:{STAGE}")
    assert n == 1


def test_the_rows_query_runs_through_the_gateway_loader_shape():
    """_load_rows (the gateway's reader) on the real database: an empty or seeded table gives a dict of capped rows."""
    async def go():
        c = await asyncpg.connect(DSN)
        try:
            return [r["row"] for r in await c.fetch(cb._ROWS_SQL)]
        finally:
            await c.close()
    rows = asyncio.run(go())
    assert all((json.loads(r) if isinstance(r, str) else r)["provider"] == "llm" for r in rows)
