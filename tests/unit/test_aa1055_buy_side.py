"""AA-1055 PR 2 (buy side) — the DataForSEO Standard queue client, the landing sweep, the free seo_context copy, the
cached-only exclusion read and the research job / route balance checks. Mock-only; invented names."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.seo_intelligence import dfs_queue as q
from services.seo_intelligence.dataforseo_client import DFSCallError


# ── Standard queue ─────────────────────────────────────────────────────────────────────────────

def test_queue_mode_defaults_to_live_and_refuses_unknown_values(monkeypatch):
    monkeypatch.delenv(q.QUEUE_ENV, raising=False)
    assert q.research_queue_mode() == "live"
    monkeypatch.setenv(q.QUEUE_ENV, "standard")
    assert q.research_queue_mode() == "standard"
    assert q.research_queue_mode("live") == "live"          # the job payload wins
    with pytest.raises(ValueError):
        q.research_queue_mode("priority")


def test_parsers():
    post = {"status_code": 20000, "cost": 0.0012, "tasks": [
        {"id": "t-1", "status_code": 20100, "cost": 0.0006, "data": {"tag": "a"}},
        {"id": "t-2", "status_code": 40501, "cost": 0, "data": {"tag": "b"}}]}
    posted = q.parse_post(post)
    assert [(t.task_id, t.tag, t.cost_usd) for t in posted] == [("t-1", "a", 0.0006)]
    assert q.parse_ready({"tasks": [{"result": [{"id": "t-1"}, {"id": "t-9"}]}]}) == {"t-1", "t-9"}
    assert q.task_get_state({"tasks": [{"status_code": 20000}]}) == "ready"
    assert q.task_get_state({"tasks": [{"status_code": 40602}]}) == "pending"
    assert q.task_get_state({"tasks": [{"status_code": 50000}]}) == "error"
    body = {"tasks": [{"result": [{"keyword": "zarvok lake", "search_volume": 70}]}]}
    assert q.parse_volumes(body, ["zarvok lake", "melnith"]) == {"zarvok lake": 70, "melnith": None}


class _FakeClient:
    """Answers _request like DataForSEO: task_post, tasks_ready (after `ready_after` polls), task_get."""

    def __init__(self, ready_after: int = 1, post_code: int = 20100, get_code: int = 20000):
        self.ready_after, self.post_code, self.get_code = ready_after, post_code, get_code
        self.polls = 0
        self.posted: list[dict] = []
        self.logged: list[dict] = []
        self.prechecks: list[int] = []

    def _precheck(self, endpoint, **kw):
        self.prechecks.append(kw.get("tasks", 1))

    def _log_live(self, endpoint, data, **kw):
        self.logged.append({"endpoint": endpoint, "task_ok": kw.get("task_ok"), "meta": kw.get("meta")})

    async def _request(self, endpoint, url, *, payload=None, **kw):
        if url.endswith("task_post"):
            self.posted.extend(payload)
            return {"status_code": 20000, "cost": 0.0006 * len(payload), "tasks": [
                {"id": f"id-{t['tag']}", "status_code": self.post_code, "cost": 0.0006, "data": {"tag": t["tag"]}}
                for t in payload]}, 5
        if url.endswith("tasks_ready"):
            self.polls += 1
            ids = [{"id": f"id-{t['tag']}"} for t in self.posted] if self.polls >= self.ready_after else []
            return {"status_code": 20000, "tasks": [{"result": ids}]}, 5
        task_id = url.rsplit("/", 1)[1]
        tag = task_id[3:]
        kw_ = next(t for t in self.posted if t["tag"] == tag)
        if "keywords" in kw_:
            result = [{"keyword": k, "search_volume": 10} for k in kw_["keywords"]]
        else:
            result = [{"items": [{"type": "people_also_ask", "items": [{"title": "Q?"}]}]}]
        return {"status_code": 20000, "tasks": [{"status_code": self.get_code, "result": result}]}, 5


@pytest.mark.asyncio
async def test_queue_volume_task_posts_once_waits_and_parses():
    client = _FakeClient(ready_after=2)
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    queue = q.StandardQueue(client, poll_s=7, sleep=fake_sleep)
    out = await queue.volumes(["zarvok lake", "melnith gorge"], 2840, "en")
    assert out == {"zarvok lake": 10, "melnith gorge": 10}
    assert len(client.posted) == 1 and client.posted[0]["location_code"] == 2840
    assert sleeps == [7]                                   # one wait between the two polls
    assert client.logged[0]["endpoint"] == q.VOLUME_ENDPOINT and client.logged[0]["task_ok"] == (20100,)


@pytest.mark.asyncio
async def test_queue_serps_post_100_per_request_and_return_by_index():
    client = _FakeClient()
    queue = q.StandardQueue(client, sleep=AsyncMock())
    asks = [(f"kw{i}", 2840, "en") for i in range(150)]
    out = await queue.serps(asks)
    assert sorted(out) == list(range(150)) and all(isinstance(v, dict) for v in out.values())
    assert client.prechecks == [100, 50]                   # the budget is checked per task before each post
    assert [c["meta"]["tasks"] for c in client.logged] == [100, 50]


@pytest.mark.asyncio
async def test_queue_task_that_never_finishes_is_an_error_for_that_task_only():
    client = _FakeClient(ready_after=10**6)
    clock = iter(range(0, 10**6, 100))
    queue = q.StandardQueue(client, poll_s=1, timeout_s=250, direct_after_s=10**9,
                            sleep=AsyncMock(), clock=lambda: next(clock))
    out = await queue.serps([("zarvok lake", 2840, "en")])
    assert isinstance(out[0], DFSCallError) and "not finished" in str(out[0])


@pytest.mark.asyncio
async def test_queue_error_answer_raises_for_volume():
    client = _FakeClient(get_code=50000)
    queue = q.StandardQueue(client, sleep=AsyncMock())
    with pytest.raises(DFSCallError):
        await queue.volumes(["zarvok lake"], 2840, "en")


@pytest.mark.asyncio
async def test_queue_post_refused_by_payment_is_fatal():
    client = _FakeClient(post_code=40200)
    queue = q.StandardQueue(client, sleep=AsyncMock())
    with pytest.raises(DFSCallError) as exc:
        await queue.volumes(["zarvok lake"], 2840, "en")
    assert exc.value.fatal


def test_task_post_20100_is_a_success_for_the_ledger():
    from services.seo_intelligence.dataforseo_client import _dfs_status_error

    body = {"status_code": 20000, "tasks": [{"status_code": 20100}, {"status_code": 20100}]}
    assert _dfs_status_error(body, (20100,)) is None
    assert _dfs_status_error(body) is not None              # a live call still needs 20000
    second_bad = {"status_code": 20000, "tasks": [{"status_code": 20100}, {"status_code": 40200}]}
    assert _dfs_status_error(second_bad, (20100,)).fatal     # every task is read, not only the first


def test_queue_estimates_never_undercut_the_live_price_until_measured():
    from shared.cost_guard import DFS_CALL_ESTIMATE_USD as E

    assert E["search_volume_bulk_queue"] >= E["search_volume_bulk"]
    assert E["serp_advanced_queue"] >= E["serp_advanced"]


# ── cached-only exclusions (free preview / sweep) ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cached_only_exclusions_never_call_jev():
    from services.acp_contract import atom_ranking as ar
    from shared.llm_client.decide import Verdict

    rows = [{"segment_id": "s1", "canonical_place": "Zarvok Lake", "canonical_action": "arrive at",
             "activity_types": ["experience", "experience"]}]
    moment = ar.landing_moment("Zarvok Lake", "arrive at")
    stored = {(ar.TYPE_Q, f"type:{moment}"): Verdict(ar.TYPE_Q, "enforce", "accept", choice="experience")}
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    with patch.object(ar, "decide", AsyncMock()) as m_decide, \
         patch("shared.llm_client.decide.platform_verdicts", AsyncMock(return_value=stored)):
        out = await ar.resolve_exclusions(pool, rows, cached_only=True)
    m_decide.assert_not_awaited()
    if ar.classify_exclusion("Zarvok Lake", "arrive at") == "transit":
        assert out["s1"] is None                            # the stored pick overrules the verb rule


# ── landing sweep ──────────────────────────────────────────────────────────────────────────────

def _sweep_pool(active=None, recent=None, rows=()):
    conn = MagicMock()

    async def fetchval(sql, *args):
        if "status IN ('queued', 'running')" in sql:
            return active
        return recent

    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.fetch = AsyncMock(return_value=list(rows))
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


NIGHT = datetime(2026, 11, 9, 18, 30, tzinfo=timezone.utc)
DAY = datetime(2026, 11, 9, 4, 0, tzinfo=timezone.utc)


def _cand(seg, never=True, place="Zarvok Lake", action="explore"):
    return {"segment_id": seg, "canonical_place": place, "canonical_action": action, "activity_types": [],
            "never_landed": never}


@pytest.mark.asyncio
async def test_sweep_acts_only_at_night_once_a_day_and_never_beside_a_recompute(monkeypatch):
    from services.acp_contract import landing_sweep as ls

    monkeypatch.delenv(ls.SWITCH_ENV, raising=False)
    pool, _ = _sweep_pool()
    assert (await ls.sweep(pool, now=DAY))["action"] == "outside_window"
    pool, _ = _sweep_pool(active=1)
    assert (await ls.sweep(pool, now=NIGHT))["action"] == "recompute_active"
    pool, _ = _sweep_pool(recent=1)
    assert (await ls.sweep(pool, now=NIGHT))["action"] == "recent"
    monkeypatch.setenv(ls.SWITCH_ENV, "off")
    assert (await ls.sweep(pool, now=NIGHT))["action"] == "off"


@pytest.mark.asyncio
async def test_sweep_enqueues_one_recompute_for_unlanded_live_segments_never_landed_first(monkeypatch):
    from services.acp_contract import landing_sweep as ls

    monkeypatch.delenv(ls.SWITCH_ENV, raising=False)
    rows = [_cand("s-b", never=False), _cand("s-a", never=True),
            _cand("s-x", never=True, place="xx", action="transfer to the airport")]
    pool, _ = _sweep_pool(rows=rows)
    enq = AsyncMock(return_value=("job-s", True))
    excl = AsyncMock(return_value={"s-a": None, "s-b": None, "s-x": "transit"})
    with patch("services.jobs.recompute_job.enqueue_recompute", enq), \
         patch("services.acp_contract.atom_ranking.resolve_exclusions", excl):
        out = await ls.sweep(pool, now=NIGHT)
    assert out == {"action": "enqueued", "job_id": "job-s", "created": True, "segments": 2}
    assert enq.await_args.kwargs["segment_ids"] == ["s-a", "s-b"]
    assert enq.await_args.kwargs["reason"] == ls.REASON and enq.await_args.kwargs["scope"] == "platform"
    assert excl.await_args.kwargs["cached_only"] is True


@pytest.mark.asyncio
async def test_sweep_with_nothing_to_land_enqueues_nothing_and_errors_never_raise(monkeypatch):
    from services.acp_contract import landing_sweep as ls

    monkeypatch.delenv(ls.SWITCH_ENV, raising=False)
    pool, _ = _sweep_pool(rows=[])
    with patch("services.jobs.recompute_job.enqueue_recompute", AsyncMock()) as enq:
        assert (await ls.sweep(pool, now=NIGHT))["action"] == "nothing"
    enq.assert_not_awaited()
    broken = MagicMock()
    broken.acquire.side_effect = RuntimeError("pool closed")
    with patch.object(ls, "in_window", return_value=True):
        assert (await ls.sweep_safe(broken))["action"] == "error"


def test_sweep_sql_reads_live_segments_and_the_per_place_research_date():
    from services.acp_contract import landing_sweep as ls

    sql = ls.CANDIDATES_SQL
    assert "v_active_tour_atoms" in sql and "NOT ta.deleted" in sql
    assert "questions_count IS NULL" in sql and "max(researched_at)" in sql


@pytest.mark.asyncio
async def test_worker_tick_runs_the_sweep_and_never_fails_the_loop():
    from shared.jobs import worker as w

    wk = w.Worker(MagicMock())
    assert wk.landing_sweep_every_seconds == w.LANDING_SWEEP_EVERY_S
    with patch("services.acp_contract.landing_sweep.sweep_safe", AsyncMock(side_effect=RuntimeError("x"))):
        await wk._landing_sweep()            # swallowed, logged
    with patch("services.acp_contract.landing_sweep.sweep_safe",
               AsyncMock(return_value={"action": "enqueued", "job_id": "j", "segments": 3})) as m:
        await wk._landing_sweep()
    m.assert_awaited_once()


# ── the free seo_context copy ──────────────────────────────────────────────────────────────────

def _ctx_row(seed, ideas, paa, related, at, cache_key="seo:x:2840:v3"):
    import json
    return {"keyword_search": seed, "keyword_ideas": json.dumps(ideas), "people_also_ask": json.dumps(paa),
            "related_keywords": json.dumps(related), "cache_key": cache_key, "fetched_at": at}


def test_copy_plan_keeps_the_newest_per_keyword_and_only_measured_volumes():
    from services.acp_contract import seo_context_copy as c

    old, new = datetime(2026, 10, 2, tzinfo=timezone.utc), datetime(2026, 10, 20, tzinfo=timezone.utc)
    rows = [
        _ctx_row("Zarvok Lake tours", [{"keyword": "Zarvok Lake", "search_volume": 300},
                                       {"keyword": "zarvok pier", "search_volume": None}], ["Is it deep?"], [], old),
        _ctx_row("zarvok lake tours", [{"keyword": "zarvok lake", "search_volume": 320}], [], ["zarvok map"], new),
        _ctx_row("melnith gorge", "not a list", [], [], new, cache_key="seo:melnith:2826:v3"),
    ]
    plan = c.plan_copy(rows)
    assert plan.rows_read == 3
    assert plan.volumes == {("zarvok lake", "US"): (320, new)}
    assert plan.serps == {("zarvok lake tours", "US"): ([], ["zarvok map"], new)}
    assert c.market_of("seo:melnith:2826:v3") == "UK" and c.market_of(None) == "US"


@pytest.mark.asyncio
async def test_copy_dry_run_writes_nothing_and_apply_merges():
    from services.acp_contract import seo_context_copy as c

    at = datetime(2026, 10, 20, tzinfo=timezone.utc)
    conn = MagicMock()

    async def fetch(sql, *args):
        if "seo_context" in sql:
            return [_ctx_row("zarvok lake tours", [{"keyword": "zarvok lake", "search_volume": 320}],
                             ["Is it deep?"], ["zarvok map"], at)]
        return [{"keyword": "zarvok lake", "market": "US", "retrieved_on": datetime(2026, 11, 8, tzinfo=timezone.utc),
                 "paa_retrieved_on": None}]

    conn.fetch = AsyncMock(side_effect=fetch)
    conn.executemany = AsyncMock()
    tx = MagicMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=tx)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    dry = await c.run_copy(pool)
    conn.executemany.assert_not_awaited()
    assert dry["volume_rows_new"] == 0 and dry["volume_rows_newer"] == 0   # the store's November volume wins
    assert dry["serp_rows_new"] == 1

    with patch("services.acp_contract.atom_ranking.invalidate_questions_cache_for_keyword",
               AsyncMock(return_value=2)):
        done = await c.run_copy(pool, apply=True)
    sqls = [call.args[0] for call in conn.executemany.await_args_list]
    assert any("WHERE acp_contract.search_demand.retrieved_on < excluded.retrieved_on" in s for s in sqls)
    assert any("paa_retrieved_on < excluded.paa_retrieved_on" in s for s in sqls)
    assert done["written"] is True and done["segments_invalidated"] == 2


# ── job and route: the balance is checked against the selection's estimate ────────────────────

@pytest.mark.asyncio
async def test_balance_gate_passes_a_small_run_on_a_low_balance():
    from services.jobs import segment_research_job as srj
    from shared.jobs.registry import NonRetryable

    await srj.balance_gate(3.0)({"estimate": {"dfs_usd_cap": 1.2}})      # was refused against the $5 budget
    with pytest.raises(NonRetryable):
        await srj.balance_gate(1.0)({"estimate": {"dfs_usd_cap": 1.2}})
    with pytest.raises(NonRetryable):
        await srj.balance_gate(None)({"estimate": {"dfs_usd_cap": 0.1}})


def test_max_usd_never_exceeds_the_budget():
    from services.jobs import segment_research_job as srj

    budget = MagicMock()
    budget.remaining_usd.return_value = 5.0
    assert srj.max_usd_for({}, budget) == 5.0
    assert srj.max_usd_for({"max_usd": 2.0}, budget) == 2.0
    assert srj.max_usd_for({"max_usd": 9.0}, budget) == 5.0
    budget.remaining_usd.return_value = None
    assert srj.max_usd_for({"max_usd": 9.0}, budget) == 9.0


@pytest.mark.asyncio
async def test_route_refuses_when_the_balance_is_below_the_capped_estimate():
    from fastapi import HTTPException

    from api.routers import admin_segment_research as r

    budget = MagicMock()
    budget.remaining_usd.return_value = 5.0
    request = MagicMock()
    request.app.state.pool = MagicMock()
    preview = {"places_selected": 10, "estimate": {"dfs_usd_cap": 2.5}}
    with patch.object(r, "verify_admin_secret"), \
         patch.object(r.job_queue, "latest", AsyncMock(return_value=None)), \
         patch("services.acp_contract.segment_research.run_segment_research", AsyncMock(return_value=preview)), \
         patch.object(r, "load_budgets", AsyncMock(return_value=(budget, budget))), \
         patch.object(r, "dfs_balance", AsyncMock(return_value=2.0)), \
         patch.object(r, "enqueue", AsyncMock()) as m_enqueue:
        with pytest.raises(HTTPException) as exc:
            await r.run_research(r.ResearchScope(markets=["US"]), request, "s", "a")
    assert exc.value.status_code == 409 and "capped estimate" in exc.value.detail
    m_enqueue.assert_not_awaited()
