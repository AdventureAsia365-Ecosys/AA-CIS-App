"""AA-735 nac 3 — the declared stage registry for the platform recompute (ADR 0003 layer B + C).

Proves the registry's declarations are well-formed, the orchestrator runs stages in order and
threads `question_counts` from landing to score, and — with the four wrapped functions mocked —
that TOUR_STAGES / PLATFORM_STAGES call them with the same arguments and emit the same progress
events the two hand-written chains in services/export/handler.py emitted before this refactor.
The last block pins the `recompute` job's result keys for both scopes (the Jobs page reads them).
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.recompute import (
    PLATFORM_STAGES,
    TOUR_STAGES,
    VALID_WRITE_STRATEGIES,
    RecomputeScope,
    all_stages,
    get_stage,
    run_stages,
)

TOUR = "11111111-1111-1111-1111-111111111111"


# ── registry declarations ───────────────────────────────────────────────────────────────────

def test_every_stage_declares_reads_writes_and_a_valid_strategy():
    stages = all_stages()
    assert set(stages) == {"segment", "landing", "score", "route"}
    for name, stage in stages.items():
        assert stage.name == name
        assert stage.reads and all(isinstance(t, str) for t in stage.reads), name
        assert stage.writes and all(isinstance(t, str) for t in stage.writes), name
        assert stage.write_strategy in VALID_WRITE_STRATEGIES, name


def test_per_table_strategies_cover_every_written_table_and_are_valid():
    # AA-735 — a stage that writes several tables declares a strategy PER table; the map must
    # cover exactly `writes`, every value must be a valid strategy, and the stage's own
    # `write_strategy` must be one of its per-table strategies.
    for name, stage in all_stages().items():
        assert set(stage.table_strategies) == set(stage.writes), name
        for table, strategy in stage.table_strategies.items():
            assert strategy in VALID_WRITE_STRATEGIES, (name, table)
        assert stage.write_strategy in stage.table_strategies.values(), name


def test_declared_write_strategies_match_the_real_sql():
    # Declared from the real SQL each wrapped function runs (see stages.py docstring).
    segment = get_stage("segment")
    assert segment.write_strategy == "upsert"
    # segment is mixed per table: atom_segment never deletes (mig 129 FK), member re-points as
    # insert+delete on a bridging merge, alias upserts.
    assert segment.table_strategies == {
        "acp_contract.atom_segment": "upsert",
        "acp_contract.atom_segment_member": "delete_insert",
        "acp_contract.atom_segment_alias": "upsert",
    }
    # landing upserts across every table it touches (questions_count cache, atom_matches, the two
    # embedding caches).
    landing = get_stage("landing")
    assert landing.write_strategy == "upsert"
    assert set(landing.writes) == {
        "acp_contract.atom_segment", "acp_contract.atom_matches",
        "acp_contract.atom_embedding", "acp_contract.question_embedding",
    }
    assert all(s == "upsert" for s in landing.table_strategies.values())
    # score: atom_ranking supersede + insert (AA-734).
    assert get_stage("score").write_strategy == "versioned_swap"
    # route is mixed per table: route versioned (AA-532), hub persistent upsert.
    route = get_stage("route")
    assert route.write_strategy == "versioned_swap"
    assert route.table_strategies == {
        "acp_contract.route": "versioned_swap",
        "acp_contract.hub": "upsert",
    }


def test_named_lists_mirror_the_two_old_chains():
    assert TOUR_STAGES == ["segment", "landing", "score", "route"]
    assert PLATFORM_STAGES == ["landing", "score", "route"]


# ── orchestrator ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_stages_rejects_an_unknown_stage_name():
    with pytest.raises(KeyError):
        await run_stages(["segment", "nonsense"], RecomputeScope(tour_id=TOUR), pool=object())


@pytest.mark.asyncio
async def test_run_stages_requires_tour_id_for_segment():
    with pytest.raises(ValueError):
        await run_stages(["segment"], RecomputeScope(), pool=object())


@pytest.mark.asyncio
async def test_run_stages_runs_in_order_and_returns_a_result_per_stage():
    order = []

    def _stage(name, result):
        async def body(ctx, scope):
            order.append(name)
            return result
        s = MagicMock()
        s.name = name
        s.run = body
        return s

    reg = {"a": _stage("a", {"x": 1}), "b": _stage("b", {"y": 2})}
    with patch("services.recompute.stages.get_stage", lambda n: reg[n]):
        out = await run_stages(["a", "b"], RecomputeScope(), pool=object())
    assert order == ["a", "b"]
    assert out == {"a": {"x": 1}, "b": {"y": 2}}


@pytest.mark.asyncio
async def test_landing_threads_question_counts_into_score():
    seen = {}

    async def fake_landing(pool, segment_ids, progress):
        return {"s1": 7, "s2": 3}

    async def fake_ranking(market, pool, question_counts):
        seen["question_counts"] = question_counts
        return {}

    with patch("services.acp_contract.atom_ranking.precompute_question_landings", fake_landing), \
         patch("services.acp_contract.atom_ranking.run_atom_ranking", fake_ranking), \
         patch("services.seo_intelligence.seed_builder.DFS_LOCATION_MAP", {"US": 1}):
        await run_stages(["landing", "score"], RecomputeScope(segment_ids={"s1", "s2"}),
                         pool=object())
    # score saw exactly the dict landing produced — proof the threading goes through ctx.data.
    assert seen["question_counts"] == {"s1": 7, "s2": 3}


# ── argument + progress equivalence with the old chains ───────────────────────────────────────

def _pool_yielding(conn):
    pool = MagicMock()

    @asynccontextmanager
    async def acquire():
        yield conn
    pool.acquire = acquire
    return pool


@pytest.mark.asyncio
async def test_tour_stages_call_the_four_functions_with_the_old_arguments():
    """TOUR_STAGES = segment -> landing -> score -> route, exactly as the old
    recompute_segment_score_route chain called them (segment matching scoped to the tour, landing
    scoped to the tour's own current Segments, ranking once per market, route once)."""
    seg = AsyncMock(return_value={"segment_rows": 5})
    landing = AsyncMock(return_value={"s1": 2})
    ranking = AsyncMock(return_value={"ranked": 1})
    route = AsyncMock(return_value={"routes": 1})

    # the tour's own current Segments, the exact query the old chain inlined
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"segment_id": "s1"}, {"segment_id": "s2"}])
    pool = _pool_yielding(conn)
    progress = MagicMock()

    with patch("services.acp_contract.segment_matching.run_segment_matching", seg), \
         patch("services.acp_contract.atom_ranking.precompute_question_landings", landing), \
         patch("services.acp_contract.atom_ranking.run_atom_ranking", ranking), \
         patch("services.acp_contract.route_detection.run_route_detection", route), \
         patch("services.seo_intelligence.seed_builder.DFS_LOCATION_MAP",
               {"US": 1, "UK": 2, "AU": 3, "DE": 4, "FR": 5, "NL": 6}):
        out = await run_stages(TOUR_STAGES, RecomputeScope(tour_id=TOUR, log_tour_id=TOUR),
                               pool=pool, progress=progress)

    # segment matching: scoped to the tour
    seg.assert_awaited_once_with(TOUR, pool)
    # landing: this tour's own current Segments + the progress callback (AA-688 pre-pass)
    landing.assert_awaited_once_with(pool, {"s1", "s2"}, progress)
    # ranking: one call per market, every call fed the same landing dict (AA-610)
    assert ranking.await_count == 6
    for c in ranking.await_args_list:
        assert c.args[1] is pool and c.args[2] == {"s1": 2}
    assert [c.args[0] for c in ranking.await_args_list] == ["US", "UK", "AU", "DE", "FR", "NL"]
    # route: once, platform-wide
    route.assert_awaited_once_with(pool)
    # result shape per stage (handler wrappers remap to {segment, ranking, route})
    assert set(out) == {"segment", "landing", "score", "route"}


@pytest.mark.asyncio
async def test_tour_stages_emit_the_same_progress_events_as_the_old_chain():
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[{"segment_id": "s1"}])
    pool = _pool_yielding(conn)
    events = []

    with patch("services.acp_contract.segment_matching.run_segment_matching",
               AsyncMock(return_value={})), \
         patch("services.acp_contract.atom_ranking.precompute_question_landings",
               AsyncMock(return_value={})), \
         patch("services.acp_contract.atom_ranking.run_atom_ranking", AsyncMock(return_value={})), \
         patch("services.acp_contract.route_detection.run_route_detection",
               AsyncMock(return_value={})), \
         patch("services.seo_intelligence.seed_builder.DFS_LOCATION_MAP",
               {"US": 1, "UK": 2, "AU": 3, "DE": 4, "FR": 5, "NL": 6}):
        await run_stages(TOUR_STAGES, RecomputeScope(tour_id=TOUR), pool=pool,
                         progress=events.append)

    # the ranking_markets 1..6 and route_detection 0/1 steps the Jobs page relies on (AA-687)
    assert events == [
        {"step": "ranking_markets", "done": 1, "total": 6},
        {"step": "ranking_markets", "done": 2, "total": 6},
        {"step": "ranking_markets", "done": 3, "total": 6},
        {"step": "ranking_markets", "done": 4, "total": 6},
        {"step": "ranking_markets", "done": 5, "total": 6},
        {"step": "ranking_markets", "done": 6, "total": 6},
        {"step": "route_detection", "done": 0, "total": 1},
        {"step": "route_detection", "done": 1, "total": 1},
    ]


@pytest.mark.asyncio
async def test_platform_stages_pass_the_segment_scope_straight_to_landing():
    """PLATFORM_STAGES = landing -> score -> route, as recompute_rankings_and_routes called them:
    no segment matching, landing scoped to the caller's Segment set (not a per-tour query)."""
    landing = AsyncMock(return_value={})
    with patch("services.acp_contract.atom_ranking.precompute_question_landings", landing), \
         patch("services.acp_contract.atom_ranking.run_atom_ranking", AsyncMock(return_value={})), \
         patch("services.acp_contract.route_detection.run_route_detection",
               AsyncMock(return_value={})), \
         patch("services.seo_intelligence.seed_builder.DFS_LOCATION_MAP", {"US": 1}):
        # scoped Segment set
        await run_stages(PLATFORM_STAGES, RecomputeScope(segment_ids={"a", "b"}), pool=object())
        assert landing.await_args.args[1] == {"a", "b"}
        # None = backfill, every Segment
        await run_stages(PLATFORM_STAGES, RecomputeScope(segment_ids=None), pool=object())
        assert landing.await_args.args[1] is None


@pytest.mark.asyncio
async def test_platform_stages_are_progress_silent_and_emit_no_per_tour_logs():
    """AA-735 — base's platform chain (recompute_rankings_and_routes) passed NO progress callback
    and logged only its own recompute_rankings_and_routes_done. The stage version must stay
    log-identical: called with no progress (exactly as the wrapper calls it), landing gets
    progress=None, no progress events fire, and the per-tour `ranking_done` /
    `route_detection_done` events (tour chain only) never appear."""
    landing = AsyncMock(return_value={})
    with patch("services.acp_contract.atom_ranking.precompute_question_landings", landing), \
         patch("services.acp_contract.atom_ranking.run_atom_ranking", AsyncMock(return_value={})), \
         patch("services.acp_contract.route_detection.run_route_detection",
               AsyncMock(return_value={})), \
         patch("services.seo_intelligence.seed_builder.DFS_LOCATION_MAP",
               {"US": 1, "UK": 2, "AU": 3, "DE": 4, "FR": 5, "NL": 6}), \
         patch("services.recompute.stages.logger") as log:
        # Exactly how recompute_rankings_and_routes calls it: no progress argument.
        await run_stages(PLATFORM_STAGES, RecomputeScope(segment_ids={"a"}), pool=object())
    # landing's 3rd positional arg is the progress callback — None on the platform path.
    assert landing.await_args.args[2] is None
    # the per-tour log events must not fire on the platform path (base logged neither).
    logged = [c.args[0] for c in log.info.call_args_list]
    assert "ranking_done" not in logged
    assert "route_detection_done" not in logged


# ── A3 atomize path runs segment matching through the one stage registry ──────────────────────

@pytest.mark.asyncio
async def test_a3_atomize_path_runs_segment_matching_once_through_run_stages():
    """AA-735 — `_run_a3_atomize_background` used to call `run_segment_matching(tour_id, pool)`
    inline; it now goes through `run_stages(["segment"], ...)`. The observable outcome must stay
    the base one: segment matching is still called EXACTLY once with (tour_id, pool), and the
    returned outcome still carries `segment_score_route="ok"` plus the debounced platform job's
    keys (segments / score_route_job / score_route_job_reused). Score + Route are NOT run inline
    here (they go to the enqueued `recompute` job), so only `segment` runs through run_stages."""
    from services.export import handler

    pool = AsyncMock()           # open_job_pool() result; .close() awaited in finally
    seg = AsyncMock(return_value={"segment_rows": 3})
    enqueue = AsyncMock(return_value=("job-9", True))

    with patch("services.acp_contract.a3_atomize.run_a3_atomize",
               AsyncMock(return_value={"atoms": 1})), \
         patch("services.acp_contract.segment_matching.run_segment_matching", seg), \
         patch("services.export.handler.tour_segment_ids",
               AsyncMock(return_value=["s1", "s2"])), \
         patch("services.jobs.recompute_job.enqueue_recompute", enqueue), \
         patch("services.export.handler.open_job_pool", AsyncMock(return_value=pool)):
        outcome = await handler._run_a3_atomize_background(
            TOUR, {"days": []}, "US", "v1")

    # segment matching ran exactly once, scoped to this tour, on the job pool
    seg.assert_awaited_once_with(TOUR, pool)
    # base outcome keys unchanged (S218: the step reports ok and the debounced job's identity)
    assert outcome["segment_score_route"] == "ok"
    assert outcome["segments"] == 2
    assert outcome["score_route_job"] == "job-9"
    assert outcome["score_route_job_reused"] is False
    # the platform score+route go to the enqueued job, not inline — enqueued once, scope platform
    enqueue.assert_awaited_once()
    assert enqueue.await_args.kwargs["scope"] == "platform"
    assert enqueue.await_args.kwargs["segment_ids"] == ["s1", "s2"]


# ── handler wrappers keep the Jobs-page result keys ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_recompute_segment_score_route_result_keys_unchanged():
    from services.export import handler
    with patch("services.recompute.run_stages",
               AsyncMock(return_value={"segment": {"a": 1}, "landing": {}, "score": {"US": {}},
                                      "route": {"r": 1}})):
        out = await handler.recompute_segment_score_route(TOUR, object())
    assert set(out) == {"segment", "ranking", "route"}
    assert out["ranking"] == {"US": {}} and out["segment"] == {"a": 1}


@pytest.mark.asyncio
async def test_recompute_rankings_and_routes_result_keys_unchanged():
    from services.export import handler
    with patch("services.recompute.run_stages",
               AsyncMock(return_value={"landing": {}, "score": {"US": {}}, "route": {"r": 1}})):
        out = await handler.recompute_rankings_and_routes(object(), segment_ids=["s1"])
    assert set(out) == {"ranking", "route"}
    assert out["ranking"] == {"US": {}} and out["route"] == {"r": 1}


# ── recompute_job.run result keys for both scopes (through the wrappers) ───────────────────────

def _ctx(payload):
    from shared.jobs import queue
    from shared.jobs.registry import JobContext
    job = queue.Job(id="job-1", kind="recompute", payload=payload, status="running", attempt=1,
                    max_attempts=2, progress={}, cost_usd=0.0)
    ctx = JobContext(None, job, {})
    ctx.progress = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_recompute_job_tour_scope_result_keys():
    from services.jobs import recompute_job as rc
    seg = AsyncMock(return_value={"segment": {}, "ranking": {}, "route": {}})
    with patch("services.export.handler.recompute_segment_score_route", seg), \
         patch("services.export.handler.recompute_rankings_and_routes", AsyncMock()), \
         patch("services.export.handler.open_job_pool", AsyncMock(return_value=AsyncMock())):
        out = await rc.run(_ctx({"scope": "tour", "tour_id": TOUR, "reason": "atom_delete"}))
    assert out["scope"] == "tour" and out["tour_id"] == TOUR
    assert {"segment", "ranking", "route"} <= set(out)


@pytest.mark.asyncio
async def test_recompute_job_platform_scope_result_keys():
    from services.jobs import recompute_job as rc
    plat = AsyncMock(return_value={"ranking": {}, "route": {}})
    with patch("services.export.handler.recompute_segment_score_route", AsyncMock()), \
         patch("services.export.handler.recompute_rankings_and_routes", plat), \
         patch("services.export.handler.open_job_pool", AsyncMock(return_value=AsyncMock())):
        out = await rc.run(_ctx({"scope": "platform", "reason": "master_status"}))
    assert out["scope"] == "platform"
    assert {"ranking", "route"} <= set(out)
