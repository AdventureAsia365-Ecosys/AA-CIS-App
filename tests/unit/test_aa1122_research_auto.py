"""AA-1122 items 4-5 — the nightly research tick (decision table), the tour PAA backfill plan / run and the rename carry.
No DB, no provider call; invented places and tours only."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_contract import research_auto as ra
from services.acp_contract import research_carry as sm
from services.seo_intelligence import tour_paa
from shared import cost_guard
from shared import research_budget as cg

NOW = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
CFG = {"enabled": True, "window_start": 17, "window_end": 23, "new_min_places": 20, "new_max_wait_days": 7,
       "refresh_every_days": 30, "max_failures": 2, "activation_stale_pct": 50.0, "new_wait_alert_days": 14,
       "month_warn_pct": 80.0, "auto_max_usd_per_run": 3.0, "max_usd_per_run": 50.0, "min_run_usd": 0.05,
       "dfs_queue": "standard", "tenant_statuses": ["active"]}


def _pending(new=0, wait=None, markets=("US",)):
    return ra.Pending(places_in_scope=100, new=new, oldest_new_wait_days=wait, markets=list(markets))


def _month(spent=0.0, cap=5.0):
    return cg.SharedCap("month", cap, spent_at_start=spent)


def _job(status, created_by="system:auto", scope="auto_new", days_ago=1):
    return {"id": f"j-{status}-{days_ago}", "status": status, "created_by": created_by, "scope": scope,
            "created_at": NOW - timedelta(days=days_ago), "finished_at": None}


def test_in_window_plain_and_wrapping():
    assert ra.in_window(17, 17, 23) and ra.in_window(22, 17, 23) and not ra.in_window(23, 17, 23)
    assert ra.in_window(1, 22, 3) and ra.in_window(23, 22, 3) and not ra.in_window(12, 22, 3)
    assert not ra.in_window(5, 5, 5)


def test_failure_streak_counts_auto_failures_until_a_success():
    jobs = [_job("running"), _job("failed"), _job("stopped_budget", days_ago=2), _job("succeeded", "admin:zz", None, 3),
            _job("failed", days_ago=4)]
    assert ra.failure_streak(jobs) == 2
    assert ra.failure_streak([_job("failed", "admin:zz", None)]) == 0


@pytest.mark.parametrize("new,wait,expected", [
    (25, 1.0, ("enqueue", "new")),        # enough new places
    (3, 8.0, ("enqueue", "new")),         # few, but the oldest waited long enough
    (3, 2.0, ("enqueue", "refresh")),     # not due for new; no refresh ever ran
])
def test_decide_new_or_refresh(new, wait, expected):
    d = ra.decide(NOW, CFG, _pending(new, wait), [], _month())
    assert (d.action, d.kind) == expected and d.max_usd == 3.0


def test_decide_nothing_due_when_refreshed_recently():
    jobs = [_job("succeeded", scope="auto_refresh", days_ago=5)]
    assert ra.decide(NOW, CFG, _pending(3, 2.0), jobs, _month()).action == "nothing_due"
    # a failed refresh run counts too (critic C9): no nightly retry loop
    jobs = [_job("failed", scope="auto_refresh", days_ago=5)]
    assert ra.decide(NOW, CFG, _pending(3, 2.0), jobs, _month()).action == "nothing_due"


def test_decide_caps_the_run_by_the_month_left_and_skips_below_the_floor():
    d = ra.decide(NOW, CFG, _pending(30), [], _month(spent=4.0))
    assert d.action == "enqueue" and d.max_usd == pytest.approx(1.0)
    d = ra.decide(NOW, CFG, _pending(30), [], _month(spent=4.98))
    assert d.action == "skipped_cap" and "research_auto_capped" in [a[0] for a in d.alerts]
    d = ra.decide(NOW, CFG, _pending(30), [], _month(cap=0.0))
    assert d.action == "skipped_cap"                  # 0 = no research spend, never unlimited


def test_decide_alerts_on_long_waits_and_month_spend_and_never_buys_new_markets_only():
    d = ra.decide(NOW, CFG, _pending(1, 20.0), [_job("succeeded", scope="auto_refresh")], _month(spent=4.2))
    assert {"research_new_waiting", "research_month_spend"} <= {a[0] for a in d.alerts}
    d = ra.decide(NOW, CFG, _pending(30, markets=()), [], _month())
    assert d.action == "no_markets"


def _patch_cfg(**over):
    return patch.object(ra, "config", return_value={**CFG, **over})


@pytest.mark.asyncio
async def test_tick_disabled_and_outside_window_do_nothing():
    pool = MagicMock()
    with _patch_cfg(enabled=False):
        assert (await ra.tick(pool, now=NOW))["action"] == "disabled"
    with _patch_cfg():
        assert (await ra.tick(pool, now=NOW.replace(hour=9)))["action"] == "outside_window"


@pytest.mark.asyncio
async def test_tick_enqueues_one_capped_run_with_a_daily_key():
    enq = AsyncMock(return_value=("job-1", True))
    with _patch_cfg(), patch.object(ra, "_active", AsyncMock(return_value=False)), \
            patch.object(ra, "recent_jobs", AsyncMock(return_value=[])), \
            patch.object(ra, "auto_markets", AsyncMock(return_value=["US", "UK"])), \
            patch.object(ra, "pending", AsyncMock(return_value=_pending(25, 2.0, ("US", "UK")))), \
            patch.object(cg, "load_research_month_cap", AsyncMock(return_value=_month(1.0))), \
            patch("shared.jobs.registry.enqueue", enq), patch.object(ra, "_alert", AsyncMock(return_value={})):
        out = await ra.tick(MagicMock(), now=NOW)
    assert out["action"] == "enqueue" and out["job_id"] == "job-1"
    kind, payload = enq.await_args.args[1], enq.await_args.args[2]
    assert kind == "segment_research"
    assert payload["scope"] == "auto_new" and payload["max_usd"] == 3.0 and payload["dfs_queue"] == "standard"
    assert payload["markets"] == ["US", "UK"]
    assert enq.await_args.kwargs == {"idempotency_key": "segment_research:auto:new:2026-11-20",
                                     "created_by": "system:auto"}


@pytest.mark.asyncio
async def test_tick_pauses_after_failures_and_refuses_on_an_unreadable_ledger():
    alert = AsyncMock(return_value={})
    with _patch_cfg(), patch.object(ra, "_active", AsyncMock(return_value=False)), \
            patch.object(ra, "recent_jobs", AsyncMock(return_value=[_job("failed"), _job("failed", days_ago=2)])), \
            patch.object(ra, "_alert", alert):
        assert (await ra.tick(MagicMock(), now=NOW))["action"] == "paused_failures"
    assert alert.await_args.args[1][0][0] == "research_auto_failed"
    enq = AsyncMock()
    with _patch_cfg(), patch.object(ra, "_active", AsyncMock(return_value=False)), \
            patch.object(ra, "recent_jobs", AsyncMock(return_value=[])), \
            patch.object(ra, "auto_markets", AsyncMock(return_value=["US"])), \
            patch.object(ra, "pending", AsyncMock(return_value=_pending(25))), \
            patch.object(cg, "load_research_month_cap", AsyncMock(side_effect=RuntimeError("db"))), \
            patch("shared.jobs.registry.enqueue", enq), patch.object(ra, "_alert", AsyncMock(return_value={})):
        assert (await ra.tick(MagicMock(), now=NOW))["action"] == "ledger_error"
    enq.assert_not_awaited()


@pytest.mark.asyncio
async def test_tick_skips_while_research_runs():
    with _patch_cfg(), patch.object(ra, "_active", AsyncMock(return_value=True)):
        assert (await ra.tick(MagicMock(), now=NOW))["action"] == "research_active"


@pytest.mark.asyncio
async def test_tick_safe_never_raises():
    with patch.object(ra, "tick", AsyncMock(side_effect=RuntimeError("boom"))):
        assert (await ra.tick_safe(MagicMock()))["action"] == "error"


# ── tour PAA backfill ───────────────────────────────────────────────────────────────────────────────────────────────
SPEC = {"tour_id": "t1", "name": "Zz Lantern Valley Trek", "places": ["lantern valley", "trek"]}
ROWS = [
    {"keyword": "zz lantern valley", "market": "US", "search_volume": 900, "people_also_ask": [],
     "serp_fresh": False},
    {"keyword": "zz lantern valley trek", "market": "US", "search_volume": 300,
     "people_also_ask": ["How long is the lantern valley trek?", "When to go?"], "serp_fresh": True},
    {"keyword": "zz other ridge", "market": "US", "search_volume": 5000, "people_also_ask": ["Unrelated?"],
     "serp_fresh": False},
]


def test_plan_tour_takes_store_questions_of_its_places_and_a_keyword_to_buy():
    with patch("services.seo_intelligence.s1_prefetch.strong_terms", side_effect=lambda p: ["lantern valley"]):
        paa, buy = tour_paa.plan_tour(SPEC, ROWS, 5, 10)
    assert paa == ["How long is the lantern valley trek?", "When to go?"]
    assert buy == "zz lantern valley"            # the best place keyword whose SERP is not fresh


def test_plan_tour_never_buys_a_keyword_whose_serp_is_fresh():
    rows = [dict(ROWS[0], serp_fresh=True)]
    with patch("services.seo_intelligence.s1_prefetch.strong_terms", side_effect=lambda p: ["lantern valley"]):
        assert tour_paa.plan_tour(SPEC, rows, 5, 10) == ([], None)


def test_write_sql_never_overwrites_a_bought_list_nor_fetched_at():
    assert "fetched_at" not in tour_paa.WRITE_SQL
    assert "paa_source IS NOT NULL OR coalesce(people_also_ask" in tour_paa.WRITE_SQL


def _plans(*items):
    return [tour_paa.TourPlan(t, t, paa, kw) for t, paa, kw in items]


@pytest.mark.asyncio
async def test_store_only_run_fills_from_the_store_and_marks_nothing():
    writes = AsyncMock()
    with patch.object(tour_paa, "build_plan", AsyncMock(return_value=_plans(("t1", ["Q?"], None),
                                                                              ("t2", [], "zz kw")))), \
            patch.object(tour_paa, "_write", writes):
        out = await tour_paa.run(MagicMock(), paid=False)
    assert out["filled_store"] == 1 and out["marked_none"] == 0
    assert [c.args[1:] for c in writes.await_args_list] == [("t1", ["Q?"], "store")]


@pytest.mark.asyncio
async def test_paid_run_buys_one_serp_per_keyword_and_stops_at_the_cap():
    writes = AsyncMock()
    client = MagicMock()
    calls = []

    async def serp(kw, loc, lang):
        calls.append(kw)
        if len(calls) > 1:
            raise cost_guard.BudgetExceeded("research month budget $5.00 reached", "research", "per_month")
        return {"tasks": []}
    client._serp_advanced = serp
    client._parse_paa = MagicMock(return_value=["Is it open in winter?"])
    client._parse_related = MagicMock(return_value=[])
    client._parse_organic_domains = MagicMock(return_value=[])
    cache = MagicMock()
    cache.serps = AsyncMock(return_value={})
    cache.put_serp_answer = AsyncMock()
    cache.log_hits = AsyncMock()
    plans = _plans(("t1", [], "zz a"), ("t2", [], "zz a"), ("t3", [], "zz b"), ("t4", [], None))
    with patch.object(tour_paa, "build_plan", AsyncMock(return_value=plans)), patch.object(tour_paa, "_write", writes), \
            patch("services.seo_intelligence.research_cache.ResearchCache", return_value=cache), \
            patch("services.seo_intelligence.research_cache.fresh_windows", AsyncMock(return_value=None)), \
            patch("services.seo_intelligence.s1_prefetch.paa_caps", return_value=(5, 10)):
        out = await tour_paa.run(MagicMock(), paid=True, client=client)
    assert calls == ["zz a", "zz b"] and out["serp_calls"] == 1 and out["filled_serp"] == 2
    assert out["skipped_cap"] == 1 and out["marked_none"] == 1 and "month" in out["stop_reason"]
    sources = {c.args[1]: c.args[3] for c in writes.await_args_list}
    assert sources == {"t4": "none", "t1": "serp", "t2": "serp"}          # t3 untouched: the next run tries again


@pytest.mark.asyncio
async def test_paid_backfill_buys_nothing_when_the_month_is_spent():
    from services.jobs import tour_paa_job

    with patch.object(cg, "research_month_spend", AsyncMock(return_value={"total_usd": 5.0})), \
            patch.object(cg, "job_spend", AsyncMock(return_value=0.0)), \
            patch("shared.settings.get", side_effect=lambda k: {"cis.spend.research_max_usd_per_month": 5.0,
                                                               "cis.spend.tour_paa_backfill_max_usd_per_run": 1.0}[k]):
        budget, reason = await tour_paa_job.paid_budget(MagicMock(), "job-1")
    assert budget is None and "month cap" in reason


# ── rename carry ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_research_follows_a_same_plain_keyword_rename_or_merge_only():
    labels = {"s1": "Hội An", "s2": "Tiger's Nest", "s3": "Zz Amber Falls", "s4": "Zz Amber Falls!"}
    rows = {"s1": ("s1", "Hoi An", "visit"), "s2": ("s2", "Taktsang Monastery", "hike"),
            "s4": ("s4", "Zz Amber Falls!", "walk")}
    got = sm.research_renames(labels, rows, [("s3", "s4")], ["s1", "s2"])
    assert got == [("Hội An", "Hoi An"), ("Zz Amber Falls", "Zz Amber Falls!")]


@pytest.mark.asyncio
async def test_carry_research_copies_log_and_map_in_the_callers_transaction():
    conn = MagicMock()
    conn.execute = AsyncMock()
    assert await sm.carry_research(conn, [("Hội An", "Hoi An")]) == 1
    sqls = [c.args[0] for c in conn.execute.await_args_list]
    assert "segment_research_log" in sqls[0] and "GREATEST" in sqls[0]
    assert "place_keyword" in sqls[1] and "NOT EXISTS" in sqls[1]
    assert await sm.carry_research(conn, []) == 0


def test_capacity_line_registered_for_the_new_kind():
    from shared.llm_client import capacity
    from shared.jobs.registry import kinds

    import services.jobs  # noqa: F401  (registers the kinds)
    assert "tour_paa_backfill" in kinds()
    src = open(capacity.__file__, encoding="utf-8").read()
    assert '"tour_paa_backfill": None' in src
