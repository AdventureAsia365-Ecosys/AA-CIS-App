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


class _OrderedClient(_FakeClient):
    """Records the order of posts and polls, and that no two posts are in flight together."""

    def __init__(self, budget_left_posts: int | None = None, **kw):
        super().__init__(**kw)
        self.events: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.budget_left_posts = budget_left_posts

    def _precheck(self, endpoint, **kw):
        from shared.cost_guard import BudgetExceeded
        if self.budget_left_posts is not None:
            if self.budget_left_posts <= 0:
                raise BudgetExceeded("dfs run budget", "dataforseo", "per_run")
            self.budget_left_posts -= 1
        super()._precheck(endpoint, **kw)

    async def _request(self, endpoint, url, *, payload=None, **kw):
        import asyncio
        if url.endswith("task_post"):
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0)
            self.in_flight -= 1
            self.events.append("post")
        elif url.endswith("tasks_ready"):
            self.events.append("ready")
        return await super()._request(endpoint, url, payload=payload, **kw)


@pytest.mark.asyncio
async def test_queue_serps_post_every_request_before_the_first_poll():
    client = _OrderedClient()
    queue = q.StandardQueue(client, sleep=AsyncMock())
    out = await queue.serps([(f"kw{i}", 2840, "en") for i in range(250)])
    assert len(out) == 250 and all(isinstance(v, dict) for v in out.values())
    assert client.events[:3] == ["post", "post", "post"] and client.events[3] == "ready"


@pytest.mark.asyncio
async def test_queue_serps_budget_stop_keeps_the_paid_tasks_and_fails_the_rest():
    from shared.cost_guard import BudgetExceeded
    client = _OrderedClient(budget_left_posts=1)
    queue = q.StandardQueue(client, sleep=AsyncMock())
    out = await queue.serps([(f"kw{i}", 2840, "en") for i in range(150)])
    assert all(isinstance(out[i], dict) for i in range(100))
    assert all(isinstance(out[i], BudgetExceeded) for i in range(100, 150))
    with pytest.raises(BudgetExceeded):                     # nothing paid yet: the stop is raised as before
        await q.StandardQueue(_OrderedClient(budget_left_posts=0), sleep=AsyncMock()).serps([("kw", 2840, "en")])


@pytest.mark.asyncio
async def test_queue_posts_are_serialised_while_waits_run_together():
    import asyncio
    client = _OrderedClient()
    queue = q.StandardQueue(client, sleep=AsyncMock())
    a, b = await asyncio.gather(queue.volumes(["zarvok lake"], 2840, "en"),
                                queue.volumes(["melnith gorge"], 2826, "en"))
    assert a == {"zarvok lake": 10} and b == {"melnith gorge": 10}
    assert client.max_in_flight == 1 and client.events.count("post") == 2


@pytest.mark.asyncio
async def test_queue_task_that_never_finishes_is_an_error_for_that_task_only():
    client = _FakeClient(ready_after=10**6)
    clock = iter(range(0, 10**6, 100))
    queue = q.StandardQueue(client, poll_s=1, timeout_s=250, direct_after_s=10**9,
                            sleep=AsyncMock(), clock=lambda: next(clock))
    out = await queue.serps([("zarvok lake", 2840, "en")])
    assert isinstance(out[0], q.QueuedTaskFailed) and "not finished" in str(out[0])
    assert out[0].task_id == "id-" + client.posted[0]["tag"] and not out[0].fatal


@pytest.mark.asyncio
async def test_queue_error_answer_after_accept_is_a_queued_task_failure():
    client = _FakeClient(get_code=50000)
    queue = q.StandardQueue(client, sleep=AsyncMock())
    with pytest.raises(q.QueuedTaskFailed) as exc:
        await queue.volumes(["zarvok lake"], 2840, "en")
    assert exc.value.task_id and not exc.value.fatal


class _FlakyGetClient(_FakeClient):
    """The first task_get fails in transport (a free poll), the next one answers."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.gets = 0

    async def _request(self, endpoint, url, *, payload=None, **kw):
        if "task_get" in url:
            self.gets += 1
            if self.gets == 1:
                raise ConnectionError("connection reset")
        return await super()._request(endpoint, url, payload=payload, **kw)


@pytest.mark.asyncio
async def test_queue_failed_free_poll_is_asked_again_not_counted_as_the_task_failing():
    client = _FlakyGetClient()
    queue = q.StandardQueue(client, sleep=AsyncMock())
    assert await queue.volumes(["zarvok lake"], 2840, "en") == {"zarvok lake": 10}
    assert client.gets == 2 and len(client.posted) == 1      # paid once


class _StaggeredClient(_FakeClient):
    """tasks_ready lists one more posted task per poll (in post order)."""

    async def _request(self, endpoint, url, *, payload=None, **kw):
        if url.endswith("tasks_ready"):
            self.polls += 1
            ids = [{"id": f"id-{t['tag']}"} for t in self.posted[:self.polls]]
            return {"status_code": 20000, "tasks": [{"result": ids}]}, 5
        return await super()._request(endpoint, url, payload=payload, **kw)


@pytest.mark.asyncio
async def test_queue_serps_report_each_task_as_it_is_collected():
    client = _StaggeredClient()
    queue = q.StandardQueue(client, sleep=AsyncMock())
    reported: list[tuple[int, int]] = []          # (index, polls so far)

    async def on_result(i, value):
        assert isinstance(value, dict)
        reported.append((i, client.polls))

    out = await queue.serps([("zarvok lake", 2840, "en"), ("melnith gorge", 2840, "en"),
                             ("quorra valley", 2840, "en")], on_result=on_result)
    assert reported == [(0, 1), (1, 2), (2, 3)]    # each one before the wait for the next ended
    assert sorted(out) == [0, 1, 2]


@pytest.mark.asyncio
async def test_queue_serps_report_refused_posts_and_budget_stops_too():
    client = _OrderedClient(budget_left_posts=1)
    queue = q.StandardQueue(client, sleep=AsyncMock())
    reported: dict = {}

    async def on_result(i, value):
        assert i not in reported                   # once per index
        reported[i] = value

    from shared.cost_guard import BudgetExceeded
    await queue.serps([(f"kw{i}", 2840, "en") for i in range(150)], on_result=on_result)
    assert sorted(reported) == list(range(150))
    assert all(isinstance(reported[i], BudgetExceeded) for i in range(100, 150))


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
    # a budget row with no limit and no max_usd: capped, never an unbounded selection
    assert srj.max_usd_for({}, budget) == srj.MAX_USD_PER_RUN
    assert srj.max_usd_for({"max_usd": 500.0}, budget) == srj.MAX_USD_PER_RUN


def _budget(**kw):
    from shared.cost_guard import RunBudget

    return RunBudget(provider="dataforseo", scope="segment_research", **{
        "per_run_usd": None, "per_day_usd": None, **kw})


def test_narrow_to_makes_max_usd_the_hard_per_run_limit():
    from services.jobs import segment_research_job as srj
    from shared.cost_guard import BudgetExceeded

    unlimited = _budget()
    srj.narrow_to(unlimited, 2.0)
    assert unlimited.per_run_usd == 2.0 and unlimited.hard_stop
    unlimited.charge(1.99)
    with pytest.raises(BudgetExceeded):
        unlimited.check(0.02)                       # the post that would cross the approved cap is refused

    tighter_row = _budget(per_run_usd=1.0)
    srj.narrow_to(tighter_row, 2.0)
    assert tighter_row.per_run_usd == 1.0           # the row's own lower cap still wins

    # an alert-only row: the per-run cap becomes hard at max_usd, its daily cap stays alert-only
    alert_only = _budget(per_run_usd=0.5, per_day_usd=1.0, hard_stop=False)
    srj.narrow_to(alert_only, 2.0)
    assert alert_only.per_run_usd == 2.0 and alert_only.hard_stop and not alert_only.day_is_hard
    alert_only.check(1.5)                           # over the alert-only daily figure: not a stop


@pytest.mark.asyncio
async def test_job_runs_with_its_budget_narrowed_and_refuses_the_loop_strategy():
    from services.jobs import segment_research_job as srj
    from shared.jobs.registry import NonRetryable

    dfs, llm = _budget(), _budget(per_run_usd=1.0)
    ctx = MagicMock()
    ctx.pool = MagicMock()
    ctx.progress = AsyncMock()
    ctx.payload = {"markets": ["US"], "strategy": "loop"}
    with pytest.raises(NonRetryable):
        await srj.run(ctx)

    ctx.payload = {"markets": ["US"], "max_usd": 3.0}
    seen = {}

    async def fake_run(target, pool, **kw):
        seen.update(kw)
        return {"aborted": False}

    with patch.object(srj, "load_budgets", AsyncMock(return_value=(dfs, llm))), \
         patch.object(srj, "dfs_balance", AsyncMock(return_value=40.0)), \
         patch.object(srj, "maybe_alert", AsyncMock()), \
         patch("services.acp_contract.segment_research.run_segment_research", fake_run):
        await srj.run(ctx)
    assert seen["max_usd"] == 3.0 and seen["dfs_budget"] is dfs
    assert dfs.per_run_usd == 3.0 and dfs.hard_stop


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
