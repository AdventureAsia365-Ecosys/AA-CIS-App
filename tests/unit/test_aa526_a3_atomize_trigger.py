"""AA-526 — atomize moves to A3 (services/export/handler.py::process_export()), owner_scope=
'platform', replacing the removed per-tenant trigger (api/routers/v1_tours.py's now-deleted
atomize_version() endpoint). Covers:

  1. process_export() launches _run_a3_atomize_background() as a real, referenced background
     task (strong-ref pattern) right after a tour is marked 'published' — not awaited inline,
     so a slow multi-day atomize run never adds latency to the admin action that calls
     process_export() directly.
  2. _run_a3_atomize_background() itself: calls run_t5_atomize() with owner_scope='platform'
     (not a tenant UUID) and the right `rewritten` shape built from generated_content's own
     columns.
  3. _SingleConnAsPool — the thin adapter that lets run_t5_atomize()'s pool.acquire() calls work
     against a single bare asyncpg.Connection.
  4. _llm_log_tenant_id() (services/acp_produce/tenant_pipeline.py) — the real bug this build
     found: record_call_with_pool()'s tenant_id is cast `$1::uuid`, so passing the literal string
     "platform" straight through (as every pre-AA-526 caller's real tenant UUID always did
     safely) would silently fail that INSERT on every single atomize LLM call.

AA-545 (06/09/2026) — Segment/Score/Route ARE now run here, platform-wide, right after atomize —
this file's original point 2/5 (which explicitly reverted that as a real FK/scoping bug) is
superseded; see `TestA3RunsSegmentScoreRouteAfterAtomize` below and
docs/implementation-notes/AA-545.md. `TestSegmentMatchingReadsSharedAtoms`/
`TestRunRankingPipelineRunsSegmentMatchingFirst` (AA-526's own tests for the old per-tenant
trigger) are replaced accordingly.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_produce.tenant_pipeline import _llm_log_tenant_id
from services.export import handler as export_handler

TOUR_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
GC_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class TestLlmLogTenantId:
    def test_real_tenant_uuid_passed_through(self):
        real_id = str(uuid.uuid4())
        assert _llm_log_tenant_id(real_id) == real_id

    def test_platform_scope_returns_none(self):
        """The actual gap this build found: "platform" is not a valid ::uuid literal —
        record_call_with_pool() would otherwise silently fail every single A3 atomize LLM
        call's cost/usage log (caught by that function's own try/except, but a real, avoidable
        loss of visibility on a stage that now runs on every published tour)."""
        assert _llm_log_tenant_id("platform") is None

    def test_none_input_returns_none(self):
        assert _llm_log_tenant_id(None) is None


@pytest.mark.asyncio
class TestAtomizeSkipsCompetitorIndexForNonTenantScope:
    """AA-526 — the real bug this build's OWN live-verify caught (unit tests alone missed it,
    since every existing test mocks build_competitor_index rather than exercising its real
    ::uuid cast): services/acp_shared/competitor_index.py's queries cast `tenant_id = $1::uuid`
    — genuinely tenant-only by that module's own docstring (never wired for platform-scope
    atoms). Calling run_t5_atomize("platform", ...) crashed this on every single call, before
    a single atom was ever inserted. Confirmed live (05/09/2026, real HTTP admin approve -> real
    process_export() -> real a3_atomize_failed log: "invalid input for query argument $1:
    'platform' (invalid UUID...)"). Fixed by skipping the fetch for a non-tenant owner_scope."""

    async def test_atomize_per_day_skips_competitor_fetch_for_platform_scope(self):
        from services.acp_produce import tenant_pipeline
        from services.content_generation.itinerary_utils import parse_canonical_itinerary_days

        row = {
            "id": TOUR_ID, "name": "Tour", "aa_summary": "s", "aa_highlights": [],
            "itinerary_source": "Day 1 — Arrive\nWalk around the old town.",
        }
        days = parse_canonical_itinerary_days(row["itinerary_source"])
        assert days  # sanity — real parse, not empty

        conn = AsyncMock()
        conn.fetch.return_value = []  # atomize_day_fingerprint: nothing cached yet
        conn.execute = AsyncMock(return_value="UPDATE 0")
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=ctx)

        fake_llm_result = MagicMock(
            text='{"atoms": []}', model_used="sonnet", usage={}, stop_reason="end_turn",
        )
        with patch("services.acp_produce.tenant_pipeline.invoke_claude", return_value=fake_llm_result), \
             patch("services.acp_produce.tenant_pipeline.get_stage_config",
                   AsyncMock(return_value=MagicMock(model_id="sonnet", account_route="acc3"))), \
             patch("services.acp_shared.competitor_index.build_competitor_index", AsyncMock()) as m_build, \
             patch("shared.llm_client.call_log.record_call_with_pool", AsyncMock()):
            result = await tenant_pipeline._atomize_per_day(
                "platform", TOUR_ID, GC_ID, row, days, "somehash", pool, "Vietnam",
            )

        m_build.assert_not_awaited()  # the actual regression guard
        assert result["status"] == "success"

    async def test_atomize_whole_tour_legacy_skips_competitor_fetch_for_platform_scope(self):
        from services.acp_produce import tenant_pipeline

        conn = AsyncMock()
        conn.fetchval.return_value = None  # no prior source_hash
        conn.execute = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=ctx)

        fake_llm_result = MagicMock(
            text='{"atoms": [{"place": "Old town", "action": "walk"}]}',
            model_used="sonnet", usage={}, stop_reason="end_turn",
        )
        row = {"id": TOUR_ID, "name": "Tour", "aa_summary": "s", "aa_highlights": [],
               "itinerary_source": ""}
        with patch("services.acp_produce.tenant_pipeline.invoke_claude", return_value=fake_llm_result), \
             patch("services.acp_produce.tenant_pipeline.get_stage_config",
                   AsyncMock(return_value=MagicMock(model_id="sonnet", account_route="acc3"))), \
             patch("services.acp_shared.competitor_index.build_competitor_index", AsyncMock()) as m_build, \
             patch("shared.llm_client.call_log.record_call_with_pool", AsyncMock()):
            result = await tenant_pipeline._atomize_whole_tour_legacy(
                "platform", TOUR_ID, row, "somehash", pool, "Vietnam",
            )

        m_build.assert_not_awaited()  # the actual regression guard
        assert result["status"] == "success"
        assert result["atom_count"] == 1


@pytest.mark.asyncio
class TestSegmentMatchingReadsByTourIdPlatformWide:
    """AA-545 — `run_segment_matching()` is now `(tour_id, pool)`, platform-wide, incremental —
    reads THIS tour's atoms by `tour_id` directly (no `owner_scope`/`tenant_tour_versions`
    filter at all anymore, since atoms are already platform-wide since AA-526 and Segment no
    longer needs a tenant to scope by). Returns early (no queries beyond the first) when the
    tour has no eligible atoms."""

    async def test_query_scopes_by_tour_id_no_tenant_filter(self):
        from services.acp_contract import segment_matching

        conn = AsyncMock()
        conn.fetch.return_value = []  # no atoms for this tour -> early return
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=conn)
        ctx.__aexit__ = AsyncMock(return_value=False)
        pool = MagicMock()
        pool.acquire = MagicMock(return_value=ctx)

        result = await segment_matching.run_segment_matching(TOUR_ID, pool)

        atom_query, *params = conn.fetch.call_args_list[0][0]
        assert "tour_id = $1" in atom_query
        assert "owner_scope" not in atom_query
        assert "tenant_tour_versions" not in atom_query
        assert params == [TOUR_ID]
        assert result == {"segments_written": 0, "atoms": 0, "aliases": 0, "existing_segments": 0}


@pytest.mark.asyncio
class TestSingleConnAsPool:
    async def test_acquire_yields_the_same_connection_every_time(self):
        conn = object()
        pool = export_handler._SingleConnAsPool(conn)
        async with pool.acquire() as c1:
            assert c1 is conn
        async with pool.acquire() as c2:
            assert c2 is conn


@pytest.mark.asyncio
class TestRunA3AtomizeBackground:
    async def test_calls_run_t5_atomize_with_platform_owner_scope(self):
        conn = AsyncMock()
        conn.close = AsyncMock()
        rewritten = {"name": "Tour", "summary": "s", "highlights": "[]", "itineraries": "Day 1..."}

        with patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=conn)), \
             patch("services.export.handler.get_database_url", MagicMock(return_value="postgresql://fake")), \
             patch(
                 "services.acp_produce.tenant_pipeline.run_t5_atomize",
                 AsyncMock(return_value={"status": "success", "atom_count": 3}),
             ) as m_atomize, \
             patch("services.acp_contract.segment_matching.run_segment_matching", AsyncMock()), \
             patch("services.acp_contract.atom_ranking.precompute_question_landings",
                   AsyncMock(return_value={})), \
             patch("services.acp_contract.atom_ranking.run_atom_ranking", AsyncMock()), \
             patch("services.acp_contract.route_detection.run_route_detection", AsyncMock()):
            await export_handler._run_a3_atomize_background(
                tour_id=TOUR_ID, rewritten=rewritten, country="Vietnam", version_id=GC_ID,
            )

        m_atomize.assert_awaited_once()
        args, kwargs = m_atomize.call_args
        assert args[0] == "platform"  # owner_scope — NOT a tenant UUID
        assert args[1] == TOUR_ID
        assert args[2] == rewritten
        assert kwargs["country"] == "Vietnam"
        assert kwargs["version_id"] == GC_ID
        conn.close.assert_awaited_once()

    async def test_atomize_failure_is_swallowed_never_raises(self):
        """Best-effort by design — a real atomize/DB error here must never propagate (this task
        is fire-and-forget, launched from process_export() with nothing awaiting it that could
        handle an exception)."""
        conn = AsyncMock()
        conn.close = AsyncMock()

        with patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=conn)), \
             patch("services.export.handler.get_database_url", MagicMock(return_value="postgresql://fake")), \
             patch(
                 "services.acp_produce.tenant_pipeline.run_t5_atomize",
                 AsyncMock(side_effect=RuntimeError("boom")),
             ):
            await export_handler._run_a3_atomize_background(
                tour_id=TOUR_ID, rewritten={}, country="", version_id=GC_ID,
            )  # must not raise

        conn.close.assert_awaited_once()  # connection still cleaned up on the failure path


class TestTenantRewriteNoLongerHasAResearchHook:
    """AA-646 — the per-tenant-rewrite research hook (`_run_research_only`, AA-545) is gone:
    after AA-545 it swept every platform place per rewrite. Research is admin-triggered only
    (api/routers/admin_segment_research.py). The behavioural check (a rewrite never calls
    run_segment_research) lives in test_aa469_viec1_t4_t5_split.py."""

    def test_hook_removed(self):
        from api.routers import v1_tours

        assert not hasattr(v1_tours, "_run_research_only")


@pytest.mark.asyncio
class TestA3RunsSegmentScoreRouteAfterAtomize:
    """AA-545 — Segment/Score/Route now run at A3, right after atomize succeeds, platform-wide
    (no tenant param) — the real permanent trigger point (see docs/implementation-notes/
    AA-545.md), superseding AA-526's own original "deliberately not run here" decision."""

    async def test_segment_matching_then_one_debounced_platform_recompute(self):
        """AA-743 — A3 does this tour's segment matching only; Score + Route go to the debounced
        platform `recompute` job, scoped to this tour's Segments (no per-tour platform pass)."""
        conn = AsyncMock()
        conn.close = AsyncMock()
        conn.fetch = AsyncMock(return_value=[{"segment_id": "seg_a"}, {"segment_id": "seg_b"}])
        rewritten = {"name": "Tour", "summary": "s", "highlights": "[]", "itineraries": "Day 1..."}
        ranking = AsyncMock()
        routes = AsyncMock()

        with patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=conn)), \
             patch("services.export.handler.get_database_url", MagicMock(return_value="postgresql://fake")), \
             patch(
                 "services.acp_produce.tenant_pipeline.run_t5_atomize",
                 AsyncMock(return_value={"status": "success", "atom_count": 3}),
             ), \
             patch("services.acp_contract.segment_matching.run_segment_matching",
                   AsyncMock(return_value={"segments_written": 1})) as seg, \
             patch("services.jobs.recompute_job.enqueue_recompute",
                   AsyncMock(return_value=("job-9", False))) as enq, \
             patch("services.acp_contract.atom_ranking.run_atom_ranking", ranking), \
             patch("services.acp_contract.route_detection.run_route_detection", routes):
            out = await export_handler._run_a3_atomize_background(
                tour_id=TOUR_ID, rewritten=rewritten, country="Vietnam", version_id=GC_ID,
            )

        seg.assert_awaited_once()
        assert seg.await_args.args[0] == TOUR_ID
        ranking.assert_not_awaited()
        routes.assert_not_awaited()
        kw = enq.await_args.kwargs
        assert kw["scope"] == "platform" and kw["segment_ids"] == ["seg_a", "seg_b"]
        assert kw["debounce_s"] > 0
        assert out == {"segment_score_route": "ok", "segments": 2, "score_route_job": "job-9",
                       "score_route_job_reused": True}

    async def test_segment_score_route_failure_is_swallowed_never_raises(self):
        """Best-effort, same precedent as atomize itself — must never surface as the publish
        having failed."""
        conn = AsyncMock()
        conn.close = AsyncMock()

        with patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=conn)), \
             patch("services.export.handler.get_database_url", MagicMock(return_value="postgresql://fake")), \
             patch(
                 "services.acp_produce.tenant_pipeline.run_t5_atomize",
                 AsyncMock(return_value={"status": "success", "atom_count": 1}),
             ), \
             patch(
                 "services.acp_contract.segment_matching.run_segment_matching",
                 AsyncMock(side_effect=RuntimeError("boom")),
             ):
            await export_handler._run_a3_atomize_background(
                tour_id=TOUR_ID, rewritten={}, country="", version_id=GC_ID,
            )  # must not raise

        conn.close.assert_awaited_once()


@pytest.mark.asyncio
class TestProcessExportLaunchesAtomize:
    async def test_atomize_job_enqueued_after_publish(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
        """process_export() must enqueue the A3 atomize job (AA-652; before, an in-process task
        with a strong ref) rather than run it inline."""
        conn = AsyncMock()
        conn.fetchrow.side_effect = [
            {
                "id": GC_ID, "tour_id": TOUR_ID, "tenant_id": TOUR_ID, "batch_id": None,
                "aa_name": "Tour", "aa_subtitle": "s", "aa_summary": "sum", "aa_description": "d",
                "aa_highlights": "[]", "aa_itineraries": "Day 1...", "mobile_card_text": None,
                "seo_title": "t", "seo_meta": "m" * 150, "seo_keywords_used": "[]", "og_tags": "{}",
                "quality_score_id": None, "quality_score": 9.0,
                "country": "Vietnam", "duration": "5 days",
            },
            {"id": GC_ID},
        ]
        conn.execute = AsyncMock()
        conn.close = AsyncMock()

        with patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=conn)), \
             patch("services.jobs.a3_atomize_job.enqueue_a3_atomize", AsyncMock(return_value="job-1")) as m_atomize_bg:
            await export_handler.process_export(GC_ID)

        # AA-652 — enqueued as a durable `a3_atomize` job on the publish's own connection.
        m_atomize_bg.assert_awaited_once()
        assert m_atomize_bg.call_args.args[0] is conn
        kwargs = m_atomize_bg.call_args.kwargs
        assert kwargs["tour_id"] == TOUR_ID
        assert kwargs["version_id"] == GC_ID  # generated_content.id, this tour's real version
        assert kwargs["country"] == "Vietnam"
        assert kwargs["rewritten"]["name"] == "Tour"
