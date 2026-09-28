"""AA-469 Việc 1 — split T4 (save to My Catalog) / T5 (atomize) into 2 independent triggers.

STEP0 (docs/claude_audit/AA-469-viec1-step0-t4-t5-split-investigation.md) confirmed the real
bug: run_t5_atomize() used to run UNCONDITIONALLY right after T4's UPDATE inside
trigger_rewrite()'s _do_rewrite_and_save() closure (api/routers/v1_tours.py) — including when
T3's QA gate failed both repair rounds and got auto-passed (qa_auto_passed=True, AA-436). The
fix: the closure no longer calls run_t5_atomize() at all, on EITHER the real-pass or the
auto-pass path — driven end-to-end (mocks only at the LLM/DB boundary, same shape as
test_aa445_t5_distinctiveness.py's pool fake) rather than asserted via source inspection, since
the call site is a nested closure with no other seam to test through.

AA-526 (04/09/2026) — the standalone trigger this file used to ALSO cover, POST /v1/tours/
versions/{version_id}/atomize (v1_tours.atomize_version()), is now DELETED entirely: atomize
moves to A3 (services/export/handler.py::process_export()), platform-scope, not tenant-triggered
at all anymore. That half of this file's own tests is removed with it. What replaces it in this
closure: Segment research + ranking + route-detection (AA-509/510/515, tenant-market-specific,
still needs a per-tenant trigger unlike atomize itself) now fires HERE instead — see the 2
remaining tests' own `m_ranking` assertions.

AA-646 (28/09/2026) — superseded: segment research no longer fires from a tenant rewrite at all
(it swept every platform place per rewrite after AA-545). `m_ranking` now patches
run_segment_research itself and both tests assert it is NOT awaited."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import v1_tours

TENANT_ID = "33333333-3333-3333-3333-333333333333"
TOUR_ID = "44444444-4444-4444-4444-444444444444"
PUBLISHED_TOUR_ID = "55555555-5555-5555-5555-555555555555"
VERSION_ID = "66666666-6666-6666-6666-666666666666"


def _pool_ctx(conn):
    """Same shape as test_aa445_t5_distinctiveness.py's _pool_ctx — pool.acquire() always
    returns the same async-context-manager wrapping the same conn (single-threaded asyncio,
    no concurrent acquires in this code path, so reusing one conn across every `async with
    pool.acquire()` block is faithful to real execution order)."""
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


class _FakeRequest:
    def __init__(self, pool):
        self.app = SimpleNamespace(state=SimpleNamespace(pool=pool))


PT_ROW = {
    "id": PUBLISHED_TOUR_ID, "tour_id": TOUR_ID, "aa_name": "Sapa Trek",
    "aa_subtitle": "Original subtitle", "aa_summary": "Original summary",
    "aa_description": "desc", "aa_highlights": [], "aa_itineraries": "",
    "seo_title": "st", "seo_meta": "sm", "seo_keywords_used": None,
    "country": "Vietnam", "duration": "3D2N",
}
EXISTING_SEO_ROW = {"top_keywords": "[]", "keyword_ideas": "[]", "people_also_ask": "[]"}
REWRITE_RESULT = {
    "status": "success",
    "generated": {
        "name": "Sapa Trek Reimagined", "subtitle": "New subtitle", "summary": "New summary",
        "highlights": ["h1"], "itineraries": "Day 1...", "seo_title": "New st",
        "seo_meta": "New sm", "trip_type": "trek",
    },
}


async def _drive_trigger_rewrite(qa_result: dict):
    """Calls the real trigger_rewrite() endpoint, then runs the `t2_rewrite` job it enqueued
    (AA-652 — the rewrite moved from a nested closure into services/jobs/t2_rewrite_job.py)
    against the same fake pool."""
    from services.jobs import t2_rewrite_job
    from shared.jobs import queue
    from shared.jobs.registry import JobContext

    conn = AsyncMock()
    # Endpoint: quota plan fetchrow, PT_ROW. Job: PT_ROW again, brand rules (none), existing SEO,
    # then the final version row.
    conn.fetchrow.side_effect = [{"plan": "starter", "quota": 50}, PT_ROW,
                                 PT_ROW, None, EXISTING_SEO_ROW,
                                 {"status": "ai_generated", "score": 8.5, "qa_status": "passed"}]
    # Endpoint: quota used, next_ver, INSERT id. Job: version status, source_score.
    conn.fetchval.side_effect = [1, 1, VERSION_ID, "pending", 5.0]
    pool = _pool_ctx(conn)
    request = _FakeRequest(pool)
    tenant = {"sub": TENANT_ID}
    body = v1_tours.RewriteRequest(rewrite_language="en-US", seo_mode="standard")

    with patch("api.routers.v1_pipeline._rewrite_tour", AsyncMock(return_value=REWRITE_RESULT)) as m_rewrite, \
         patch("services.acp_produce.tenant_pipeline.run_t3_qa_gate", AsyncMock(return_value=qa_result)) as m_qa, \
         patch("services.acp_produce.tenant_pipeline.escalate_t3_failure", AsyncMock()) as m_escalate, \
         patch("services.acp_produce.tenant_pipeline.run_t5_atomize", AsyncMock()) as m_atomize, \
         patch("services.acp_contract.segment_research.run_segment_research", AsyncMock()) as m_ranking, \
         patch("shared.jobs.registry.enqueue", AsyncMock(return_value=("job-1", True))) as m_enqueue:

        resp = await v1_tours.trigger_rewrite(PUBLISHED_TOUR_ID, body, request, tenant)
        m_enqueue.assert_awaited_once()
        payload = m_enqueue.call_args.args[2]
        job = queue.Job(id="job-1", kind="t2_rewrite", payload=payload, status="running",
                        attempt=1, max_attempts=2, progress={}, cost_usd=0.0)
        ctx = JobContext(pool, job, {"redis": None})
        ctx.progress = AsyncMock()
        await t2_rewrite_job.run(ctx)

    return resp, conn, m_rewrite, m_qa, m_escalate, m_atomize, m_ranking


@pytest.mark.asyncio
async def test_real_qa_pass_does_not_auto_atomize():
    """Baseline: even a clean T3 pass no longer triggers T5 automatically — T4 (the UPDATE) is
    the real stopping point now."""
    qa_result = {
        "result": {**REWRITE_RESULT, "quality_score": 8.5},
        "passed": True, "attempts": 0, "structural_issues": [], "grounding_issues": [],
    }
    resp, conn, m_rewrite, m_qa, m_escalate, m_atomize, m_ranking = await _drive_trigger_rewrite(qa_result)

    assert resp["status"] == "pending"
    m_rewrite.assert_awaited_once()
    m_qa.assert_awaited_once()
    m_escalate.assert_not_awaited()
    m_atomize.assert_not_awaited()  # <- the actual AA-469 Việc 1 regression guard
    m_ranking.assert_not_awaited()  # AA-646 — a tenant rewrite never buys DFS research

    execute_calls = [c for c in conn.execute.call_args_list
                      if "UPDATE gold_aa_internal.tenant_tour_versions" in c.args[0]
                      and "rewritten_content" in c.args[0]]
    assert len(execute_calls) == 1
    args = execute_calls[0].args
    assert args[2] == "ai_generated"   # new_status (score >= 7.0)
    assert args[6] is False            # qa_auto_passed


@pytest.mark.asyncio
async def test_qa_auto_pass_does_not_auto_atomize():
    """THE original bug's exact repro condition (STEP0): T3 exhausts both repair rounds
    (passed=False) and gets auto-passed (AA-436) — escalate_t3_failure() still fires (A4 must
    still see it), but run_t5_atomize() must NOT."""
    qa_result = {
        "result": {**REWRITE_RESULT, "quality_score": 6.0},
        "passed": False, "attempts": 2,
        "structural_issues": ["FORBIDDEN_WORD"], "grounding_issues": [],
    }
    resp, conn, m_rewrite, m_qa, m_escalate, m_atomize, m_ranking = await _drive_trigger_rewrite(qa_result)

    assert resp["status"] == "pending"
    m_escalate.assert_awaited_once()   # review_queue / A4 path unaffected by this fix
    m_atomize.assert_not_awaited()     # <- was unconditionally called pre-fix; must not be now
    m_ranking.assert_not_awaited()     # AA-646 — nor on the auto-pass path

    execute_calls = [c for c in conn.execute.call_args_list
                      if "UPDATE gold_aa_internal.tenant_tour_versions" in c.args[0]
                      and "rewritten_content" in c.args[0]]
    args = execute_calls[0].args
    assert args[2] == "needs_review"
    assert args[6] is True             # qa_auto_passed=True — the exact flag the bug ignored
