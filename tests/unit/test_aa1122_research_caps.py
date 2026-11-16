"""AA-1122 item 4 — the hard monthly research cap and the automatic run cap, and the GUARD that no research path can
spend past the month cap (tech lead: "a guard test fails if any research path can spend past the monthly cap").

Every paid research path buys through a budgeted DataForSEO client, the keyword LLM budget or the Jev keyword gate;
each one checks the SharedCap(s) attached to its RunBudget before the paid call. The guard drives each path with
fakes until the month cap stops it and asserts that the spend charged never passes the cap. No DB, no provider call;
invented places only."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_contract import research_plan as rp
from services.acp_contract import segment_research as sr
from services.acp_contract import segment_research_batch as rb
from services.seo_intelligence.dataforseo_client import DataForSEOClient
from shared import cost_guard
from shared import research_budget as cg
from shared.jobs.registry import NonRetryable


def _budget(provider: str, caps=()) -> cg.CappedRunBudget:
    b = cost_guard.RunBudget(provider=provider, scope="job:segment_research", per_run_usd=None, per_day_usd=None)
    return cg.with_caps(b, *caps)


# ── SharedCap ───────────────────────────────────────────────────────────────────────────────────────────────────────
def test_shared_cap_counts_every_budget_it_is_attached_to():
    month = cg.SharedCap("month", 1.0, spent_at_start=0.7)
    dfs, llm = _budget("dfs", [month]), _budget("bedrock", [month])
    dfs.charge(0.1)
    llm.charge(0.1)
    assert round(month.spent, 6) == 0.9 and round(month.remaining(), 6) == 0.1
    llm.check(0.1)                                   # exactly at the cap is allowed
    with pytest.raises(cost_guard.BudgetExceeded) as e:
        dfs.check(0.11)
    assert e.value.limit == "per_month" and "month" in str(e.value)
    assert dfs.remaining_usd() == pytest.approx(0.1)


def test_a_zero_cap_means_no_spend_never_unlimited():
    month = cg.SharedCap("month", 0.0)
    b = _budget("dfs", [month])
    with pytest.raises(cost_guard.BudgetExceeded):
        b.check(0.0001)
    b.check(0.0)                                     # a free call (a store hit) is never refused
    assert b.remaining_usd() == 0.0
    assert cg.SharedCap("month", None).remaining() == float("inf")


@pytest.mark.asyncio
async def test_month_spend_is_read_by_job_not_by_stage_and_fails_closed():
    conn = MagicMock()
    conn.fetchrow = AsyncMock(return_value={"dfs_usd": 1.25, "llm_usd": 0.5})
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    got = await cg.research_month_spend(pool, datetime(2026, 11, 16, 3, tzinfo=timezone.utc))
    sql, kinds, start = conn.fetchrow.await_args.args
    assert "shared.job" in sql and "job_id IN" in sql and "stage" not in sql      # critic C1: by job, never stage
    assert kinds == list(cg.RESEARCH_JOB_KINDS) and "tour_paa_backfill" in kinds
    assert start == datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert got["total_usd"] == 1.75
    conn.fetchrow = AsyncMock(side_effect=RuntimeError("db down"))
    with pytest.raises(RuntimeError):
        await cg.load_research_month_cap(pool)


# ── the job: scope, caps across retries ──────────────────────────────────────────────────────────────────────────────
def test_an_automatic_run_needs_a_known_scope():
    from services.jobs.segment_research_job import check_scope

    assert check_scope({"scope": "auto_new"}, "system:auto") == "auto_new"
    assert check_scope({}, "admin:zz") is None
    with pytest.raises(NonRetryable):
        check_scope({}, "system:auto")
    with pytest.raises(NonRetryable):
        check_scope({"scope": "everything"}, "admin:zz")


@pytest.mark.asyncio
async def test_a_retry_subtracts_what_earlier_attempts_spent():
    from services.jobs import segment_research_job as srj

    with patch.object(cg, "research_month_spend", AsyncMock(return_value={"total_usd": 1.0})), \
            patch.object(cg, "job_spend", AsyncMock(return_value=2.5)), \
            patch.object(srj.settings, "get", side_effect=lambda k: {
                "cis.spend.research_auto_max_usd_per_run": 3.0, "cis.spend.research_max_usd_per_month": 5.0,
                "cis.spend.segment_research_max_usd_per_run": 50.0}[k]):
        month, run, prior = await srj.shared_caps(MagicMock(), "job-1", "auto_new")
    assert prior == 2.5 and run.remaining() == pytest.approx(0.5) and month.remaining() == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_an_unreadable_ledger_refuses_the_run():
    from services.jobs import segment_research_job as srj

    with patch.object(cg, "research_month_spend", AsyncMock(side_effect=RuntimeError("timeout"))):
        with pytest.raises(NonRetryable):
            await srj.shared_caps(MagicMock(), "job-1", None)


@pytest.mark.asyncio
async def test_a_spent_month_stops_the_job_before_anything_is_read():
    from services.jobs import segment_research_job as srj

    ctx = SimpleNamespace(payload={"markets": ["US"], "scope": "auto_new"}, pool=MagicMock(), job_id="j",
                          job=SimpleNamespace(created_by="system:auto", attempt=1))
    month = cg.SharedCap("month", 5.0, spent_at_start=5.0)
    with patch.object(srj, "shared_caps", AsyncMock(return_value=(month, None, 0.0))), \
            patch.object(srj, "wait_for_lane", AsyncMock()), \
            patch.object(srj, "load_budgets", AsyncMock()) as budgets:
        with pytest.raises(cost_guard.BudgetExceeded) as e:
            await srj.run(ctx)
    assert "month cap reached" in str(e.value)
    budgets.assert_not_awaited()


# ── THE GUARD: no research path spends past the month cap ────────────────────────────────────────────────────────────
def _dfs_client(caps, price):
    """A DataForSEO client whose every paid call costs `price` (real check / charge through its RunBudget)."""
    budget = _budget("dfs", caps)
    client = DataForSEOClient(login="zz", password="zz", budget=budget, stage="a3_search_demand")

    async def _request(endpoint, url, **kw):
        return {"status_code": 20000, "cost": price, "tasks": [{"status_code": 20000, "cost": price, "result": [
            {"keyword": k, "search_volume": 10} for k in (kw.get("payload") or [{}])[0].get("keywords", [])]
            or [{"items": []}]}]}, 1
    client._request = _request
    client._log_live = lambda endpoint, data, **kw: budget.charge(price)
    client._log_failed = MagicMock()
    return client, budget


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["volume_live", "serp_live", "suggestions", "tour_paa_serp"])
async def test_guard_no_dfs_research_path_spends_past_the_month_cap(path):
    month = cg.SharedCap("month", 0.25, spent_at_start=0.1)
    client, budget = _dfs_client([month], price=0.06)
    spent_calls = 0
    prices = {"search_volume_bulk": 0.06, "serp_advanced": 0.06, "keywords_for_keywords": 0.06}
    # The pre-call check reads the worst-case price table (cis.spend.dfs_call_estimate_usd); the cap holds as long as
    # that table is at or above the real price, which is what the setting is for.
    with patch.object(cost_guard, "dfs_call_estimate_usd", return_value=prices), \
            patch("services.seo_intelligence.dataforseo_client.dfs_call_estimate_usd", return_value=prices):
        for i in range(10):
            try:
                if path == "volume_live":
                    await client.fetch_volumes_bulk([f"zz place {i}"], 2840, "en", raise_on_error=True)
                elif path in ("serp_live", "tour_paa_serp"):
                    await client._serp_advanced(f"zz place {i}", 2840, "en")
                else:
                    await client.fetch_keyword_ideas_multi([f"zz place {i}"], 2840, "en")
                spent_calls += 1
            except cost_guard.BudgetExceeded:
                break
    assert spent_calls == 2                         # 0.1 + 2 x 0.06 = 0.22; a third call would pass 0.25
    assert month.spent <= month.cap_usd + 1e-9


@pytest.mark.asyncio
async def test_guard_the_keyword_llm_and_jev_never_pass_the_month_cap():
    month = cg.SharedCap("month", 0.03, spent_at_start=0.0)
    llm_budget = _budget("bedrock", [month])
    guard = sr._RunGuard()
    places = [rb._Place(f"Zz Place {i}", ["visit"], ["US"]) for i in range(40)]   # 3 propose chunks
    resp = SimpleNamespace(content='{"places": []}', cost_usd=0.012, model_used="m", input_tokens=1, output_tokens=1,
                           stop_reason="end", satellite_account=None, fallback_used=None)
    llm = MagicMock()
    llm.generate = MagicMock(return_value=resp)
    pool = MagicMock()
    with patch.object(rb, "LLMClient", return_value=llm), \
            patch.object(rb, "load_locations", AsyncMock(return_value={})), \
            patch.object(rb, "record_call_with_pool", AsyncMock()), \
            patch.object(rb, "billed_fields", return_value={}), \
            patch.object(rb, "_store_maps", AsyncMock()), \
            patch.object(rb, "LLM_PROPOSE_ESTIMATE_USD", 0.012), \
            patch.object(rb, "_gate_keywords", AsyncMock(return_value={"jev_keywords_checked": 0,
                                                                        "jev_keywords_rejected": 0})):
        out = await rb._propose(places, pool, guard, llm_budget)
    assert out["llm_calls"] == 2 and month.spent <= month.cap_usd + 1e-9
    assert guard.aborted and all(p.failed for p in places[30:])


@pytest.mark.asyncio
async def test_guard_jev_questions_are_reserved_before_they_are_asked():
    month = cg.SharedCap("month", 0.01, spent_at_start=0.0)
    llm_budget = _budget("bedrock", [month])
    places = [rb._Place("Zz Amber", ["visit"], ["US"], keywords=["zz amber", "zz amber falls", "zz amber walk"])]
    places[0].plain = "zz amber"
    decide = AsyncMock()
    with patch.object(rb, "decide", decide), patch("services.acp_contract.research_select.jev_ask_estimate_usd", return_value=0.006):
        out = await rb._gate_keywords(places, MagicMock(), llm_budget, sr._RunGuard())
    decide.assert_not_awaited()                     # 2 questions x 0.006 > 0.01: nothing asked, nothing bought
    assert out["jev_keywords_checked"] == 0 and places[0].failed
    month2 = cg.SharedCap("month", 1.0)
    places2 = [rb._Place("Zz Amber", ["visit"], ["US"], keywords=["zz amber", "zz amber falls"])]
    places2[0].plain = "zz amber"
    verdict = SimpleNamespace(verdicts={}, cost_usd=0.004, rejected=lambda q: False)
    with patch.object(rb, "decide", AsyncMock(return_value=verdict)), \
            patch("services.acp_contract.research_select.jev_ask_estimate_usd", return_value=0.006):
        await rb._gate_keywords(places2, MagicMock(), _budget("bedrock", [month2]), sr._RunGuard())
    assert month2.spent == pytest.approx(0.004)     # charged what Jev really cost


def test_guard_every_paid_research_entry_point_is_capped():
    """A new paid research path must either buy through a budget with the shared caps (listed here) or be added to
    this list after its own guard test: the list equals the research kinds the month ledger sums."""
    import inspect

    from services.jobs import segment_research_job, tour_paa_job
    assert set(cg.RESEARCH_JOB_KINDS) == {segment_research_job.KIND, tour_paa_job.KIND}
    assert "with_caps" in inspect.getsource(segment_research_job.run)
    assert "with_caps" in inspect.getsource(tour_paa_job.paid_budget)
    assert "load_research_month_cap" in inspect.getsource(tour_paa_job.paid_budget)


# ── THE GUARD: paying research is one lane over every research kind (S282 review) ─────────────────────────────────
class _JobTable:
    """An in-memory shared.job for the lane queries: claimed jobs are 'running' (the runner marks a job running when
    it claims it, before the handler starts)."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}

    def claim(self, job_id: str, kind: str, paid: bool | None = None) -> None:
        self.jobs[job_id] = {"id": job_id, "kind": kind, "status": "running", "paid": paid}

    def _paying(self, j: dict) -> bool:
        return j["kind"] == "segment_research" or (j["kind"] == "tour_paa_backfill" and bool(j["paid"]))

    @asynccontextmanager
    async def acquire(self):
        table = self

        class _Conn:
            async def fetchrow(self, sql, *args):
                assert sql in (cg.OTHER_PAYING_RUNNING_SQL, cg.ACTIVE_PAYING_SQL)
                me = args[0] if args else None
                hit = [j for j in table.jobs.values() if j["id"] != me and table._paying(j)]
                return {k: hit[0][k] for k in ("id", "kind", "status")} if hit else None
        yield _Conn()


def _job_ctx(pool, job_id, kind, payload):
    return SimpleNamespace(pool=pool, job_id=job_id, payload=payload, progress=AsyncMock(), add_cost=MagicMock(),
                           set_result=MagicMock(), job=SimpleNamespace(created_by="admin:zz", attempt=1, progress={}))


@pytest.mark.asyncio
async def test_guard_two_research_kinds_never_pay_against_one_month_at_once():
    """A segment research run and a paid tour PAA backfill each have concurrency 1, but per kind: two attempts of the
    two kinds both claimed means at most one reads the month and pays; the other goes back to the queue."""
    from services.jobs import segment_research_job as srj
    from services.jobs import tour_paa_job
    from shared.jobs.registry import Reschedule

    table = _JobTable()
    read_month = []

    async def _caps(pool, job_id, scope):
        read_month.append(job_id)
        raise cost_guard.BudgetExceeded("stop here: the month was read", "research", "per_month")

    async def _paid_budget(pool, job_id):
        read_month.append(job_id)
        return None, "stop here: the month was read"

    # 1) the segment run is claimed and starts alone: it reads the month
    table.claim("seg-1", "segment_research")
    with patch.object(srj, "open_caps", _caps):
        with pytest.raises(cost_guard.BudgetExceeded):
            await srj.run(_job_ctx(table, "seg-1", "segment_research", {"markets": ["US"]}))
    # 2) a paid tour PAA backfill is claimed while it runs: it waits, before it reads the month
    table.claim("paa-1", "tour_paa_backfill", paid=True)
    with patch.object(tour_paa_job, "paid_budget", _paid_budget), \
            patch("services.seo_intelligence.tour_paa.run", AsyncMock(return_value={})):
        with pytest.raises(Reschedule) as e:
            await tour_paa_job.run(_job_ctx(table, "paa-1", "tour_paa_backfill", {"paid": True}))
    assert "seg-1" in e.value.reason and e.value.progress["phase"] == "waiting_research_lane"
    # 3) both claimed before either checked: both wait, neither pays (they look again later)
    with patch.object(srj, "open_caps", _caps):
        with pytest.raises(Reschedule):
            await srj.run(_job_ctx(table, "seg-1", "segment_research", {"markets": ["US"]}))
    assert read_month == ["seg-1"]
    # 4) the segment run ends: the backfill now reads the month
    del table.jobs["seg-1"]
    with patch.object(tour_paa_job, "paid_budget", _paid_budget), \
            patch("services.seo_intelligence.tour_paa.run", AsyncMock(return_value={})), \
            patch("services.seo_intelligence.tour_paa.coverage", AsyncMock(return_value={})):
        await tour_paa_job.run(_job_ctx(table, "paa-1", "tour_paa_backfill", {"paid": True}))
    assert read_month == ["seg-1", "paa-1"]


@pytest.mark.asyncio
async def test_a_store_only_backfill_never_waits_for_the_lane():
    """The free derive a research run enqueues (paid false) reads the store only: it runs beside the research run."""
    from services.jobs import tour_paa_job

    table = _JobTable()
    table.claim("seg-1", "segment_research")
    table.claim("paa-free", "tour_paa_backfill", paid=False)
    run = AsyncMock(return_value={})
    with patch("services.seo_intelligence.tour_paa.run", run), \
            patch("services.seo_intelligence.tour_paa.coverage", AsyncMock(return_value={})):
        await tour_paa_job.run(_job_ctx(table, "paa-free", "tour_paa_backfill", {"paid": False}))
    assert run.await_args.kwargs["paid"] is False
    assert await cg.other_paying_research_job(table, "seg-1") is None   # a free backfill never blocks a research run


@pytest.mark.asyncio
async def test_run_endpoints_refuse_while_any_paying_research_job_is_active():
    from fastapi import HTTPException

    from api.routers import admin_segment_research as r

    request = MagicMock()
    request.app.state.pool = MagicMock()
    busy = AsyncMock(return_value={"id": "paa-7", "kind": "tour_paa_backfill", "status": "running"})
    with patch.object(r, "verify_admin_secret"), \
            patch.object(r.job_queue, "latest", AsyncMock(return_value=None)), \
            patch("shared.research_budget.active_paying_research_job", busy), \
            patch.object(r, "load_budgets", AsyncMock()) as budgets:
        with pytest.raises(HTTPException) as e:
            await r.run_research(r.ResearchScope(markets=["US"]), request, "s", "a")
    assert e.value.status_code == 409 and "paa-7" in e.value.detail
    budgets.assert_not_awaited()

    busy = AsyncMock(return_value={"id": "seg-7", "kind": "segment_research", "status": "queued"})
    with patch.object(r, "verify_admin_secret"), \
            patch.object(r, "_month_cap", AsyncMock(return_value=cg.SharedCap("month", 50.0))), \
            patch.object(r.job_queue, "latest", AsyncMock(return_value=None)), \
            patch("shared.research_budget.active_paying_research_job", busy), \
            patch.object(r, "enqueue", AsyncMock(return_value=("paa-8", True))) as m_enqueue:
        with pytest.raises(HTTPException) as e:
            await r.tour_paa_run(r.TourPaaScope(paid=True), request, "s", "a")
        assert e.value.status_code == 409 and "seg-7" in e.value.detail
        m_enqueue.assert_not_awaited()
        out = await r.tour_paa_run(r.TourPaaScope(paid=False), request, "s", "a")   # the free store pass may run
    assert out["job_id"] == "paa-8" and out["paid"] is False


def test_paying_sql_counts_every_segment_run_and_only_paid_backfills():
    for sql in (cg.ACTIVE_PAYING_SQL, cg.OTHER_PAYING_RUNNING_SQL):
        assert "kind = 'segment_research'" in sql and "kind = 'tour_paa_backfill'" in sql and "payload->'paid'" in sql
    assert "status = 'running'" in cg.OTHER_PAYING_RUNNING_SQL and "id <> $1::uuid" in cg.OTHER_PAYING_RUNNING_SQL
    assert "'queued', 'running'" in cg.ACTIVE_PAYING_SQL


# ── selection order ─────────────────────────────────────────────────────────────────────────────────────────────────
def test_order_key_never_researched_most_active_first_then_longest_wait():
    t1, t2 = datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc)
    info = {"Zz A": rp.PlaceInfo(1, t1), "Zz B": rp.PlaceInfo(5, t2), "Zz C": rp.PlaceInfo(1, t2)}
    assert sorted(info, key=lambda n: rp.order_key("new", n, info, {})) == ["Zz B", "Zz A", "Zz C"]
    ages = {"Zz A": (t2, t2), "Zz B": (t1, t2), "Zz C": (t1, t1)}
    assert sorted(info, key=lambda n: rp.order_key("stale", n, info, ages)) == ["Zz B", "Zz C", "Zz A"]


def _fresh_rows(rows):
    return [{"canonical_place": p, "market": m, "tier": t} for p, m, t in rows]


class _PlanPool:
    def __init__(self, fresh, ages, ahead=None):
        self.fresh, self.ages, self.ahead = fresh, ages, ahead

    def acquire(self):
        pool = self

        class _C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def fetch(self, sql, *args):
                if "min(researched_at)" in sql:
                    return [{"canonical_place": p, "oldest": a[0], "newest": a[1]} for p, a in pool.ages.items()]
                if "segment_research_log" in sql:
                    narrow = pool.ahead is not None and args[1].days < 100
                    return _fresh_rows(pool.ahead if narrow else pool.fresh)
                return []
        return _C()


@pytest.mark.asyncio
async def test_plan_puts_new_first_then_stale_oldest_and_auto_new_only_new():
    t_old, t_new = datetime(2026, 5, 1, tzinfo=timezone.utc), datetime(2026, 10, 1, tzinfo=timezone.utc)
    by_place = {"Zz Alpha": ["visit"], "Zz Beta": ["visit"], "Zz Gamma": ["visit"], "Zz Delta": ["visit"]}
    fresh = [("Zz Gamma", "US", "volume"), ("Zz Gamma", "US", "serp")]
    ages = {"Zz Alpha": (t_new, t_new), "Zz Beta": (t_old, t_old), "Zz Gamma": (t_new, t_new)}
    from datetime import timedelta
    w = sr.Windows(timedelta(days=180), timedelta(days=180), timedelta(days=7))
    sel = await rp.plan_selection(_PlanPool(fresh, ages), by_place, ["US"], ("volume", "serp"), w)
    assert [p.name for p in sel.places] == ["Zz Delta", "Zz Beta", "Zz Alpha"]
    assert [p.bucket for p in sel.places] == ["new", "stale", "stale"]
    sel = await rp.plan_selection(_PlanPool(fresh, ages), by_place, ["US"], ("volume", "serp"), w, scope="auto_new")
    assert [p.name for p in sel.places] == ["Zz Delta"]


@pytest.mark.asyncio
async def test_refresh_run_buys_ahead_places_within_the_share():
    from datetime import timedelta
    t = datetime(2026, 9, 1, tzinfo=timezone.utc)
    names = [f"Zz P{i}" for i in range(10)]
    by_place = {n: ["visit"] for n in names}
    fresh = [(n, "US", tier) for n in names for tier in ("volume", "serp")]
    ages = {n: (t, t) for n in names}
    w = sr.Windows(timedelta(days=180), timedelta(days=180), timedelta(days=7))
    ahead = rp.narrowed(w, 150)
    assert ahead.volume == timedelta(days=30)
    sel = await rp.plan_selection(_PlanPool(fresh, ages, ahead=[]), by_place, ["US"], ("volume", "serp"), w,
                                  scope="auto_refresh", ahead_windows=ahead, ahead_share_pct=20)
    assert [p.bucket for p in sel.places] == ["ahead", "ahead"] and sel.counts["ahead"] == 10
    manual = await rp.plan_selection(_PlanPool(fresh, ages, ahead=[]), by_place, ["US"], ("volume", "serp"), w,
                                     ahead_windows=ahead, ahead_share_pct=20)
    assert manual.places == []                      # a manual run never buys ahead


def test_select_within_usd_counts_the_llm_for_automatic_runs():
    places = [rb._Place(f"zz p{i}", [], ["US"], serp=True) for i in range(60)]
    plain = rb.select_within_usd(places, 0.2, queue="standard")
    with_llm = rb.select_within_usd(places, 0.2, queue="standard", with_llm=True)
    assert len(with_llm) <= len(plain)
    est = rb.estimate_plan(with_llm, queue="standard")
    assert est["dfs_usd_cap"] + est["llm_usd"] <= 0.2
