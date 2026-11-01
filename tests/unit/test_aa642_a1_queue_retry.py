"""AA-642 A1 (review S263) — the declared degrade mode ``queue_retry`` of t9_write / t2_generate really holds the work.

Before the fix the T9 write turned the kill switch refusal (``LLMStageDisabled``) into a 'failed' piece and the job
ended "succeeded"; the T2 job failed and its on_terminal hook marked the version 'failed', so the re-queue after the
stage was switched on ran a job that skipped itself. These tests drive the whole hold: refusal -> job failed without a
retry -> on_terminal leaves the piece / version to do -> held while the stage is off -> re-queued once it is on -> the
handler runs again and writes.

No database (the in-memory store of test_aa1016_approval_requeue behind a fake pool), no provider call. Invented
tenants and places only.
"""
import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.jobs import queue
from shared.jobs.worker import failure_outcome
from shared.llm_client import tenant_budget
from shared.llm_client.errors import LLMStageDisabled, is_stage_switch_hold, stage_disabled_in
from tests.unit.test_aa1016_approval_requeue import (PIECE_ID, REQUEST_ID, T0, TENANT, VERSION_ID, Pool, Store, _budget,
                                                     _ctx)

import services.jobs.t2_rewrite_job  # noqa: F401,E402 — registers the kinds
import services.jobs.t9_write_job  # noqa: F401,E402


T9_CONTEXT = {"atom_text": "Velmora Ridge has a lake.", "goal": {"key": "promotion"},
              "channel_style": {"channel": "facebook"}, "brand_audience": {}, "chosen": {}, "cta": "Ask us.",
              "destination": None, "trip_name": None, "brand_rubric_text": "", "channel": "facebook", "atom_id": "a1",
              "tenant_id": TENANT}


async def _fail_like_the_worker(store, kind_mod, exc):
    """What Worker._execute_job does with a handler exception that is not an approval stop: failure_outcome, then
    queue.fail (no retry for an account refusal), then the kind's on_terminal hook."""
    assert tenant_budget.approval_stop_in(exc) is None
    error, retryable, _reason = failure_outcome(exc)
    assert not retryable, "a switched-off stage cannot answer on a retry"
    store.job.update(status="failed", error=error, finished_at=T0)
    hook = {"t9_write": "mark_piece_failed", "t2_rewrite": "mark_version_failed"}[store.job["kind"]]
    await getattr(kind_mod, hook)(Pool(store), dict(store.job))


async def _requeue(store, switched_off: frozenset) -> dict:
    with patch("shared.jobs.worker.load_kinds", lambda: None), \
            patch.object(queue, "blocked_connections", AsyncMock(return_value=({}, []))), \
            patch.object(queue, "disabled_stages", AsyncMock(return_value=switched_off)):
        return await queue.retry_by_reason(Pool(store), since=T0 - timedelta(days=1), dry_run=False,
                                           rand=lambda: 0.0)


# ── the helpers ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_refusal_is_found_in_the_explicit_cause_chain_only():
    off = LLMStageDisabled("t9_write", "drill")
    assert stage_disabled_in(off) is off
    try:
        try:
            raise off
        except Exception as inner:
            raise RuntimeError("node failed") from inner
    except RuntimeError as wrapped:
        assert stage_disabled_in(wrapped) is off
    try:
        try:
            raise off
        except Exception:
            raise ValueError("cleanup failed")
    except ValueError as unrelated:
        assert stage_disabled_in(unrelated) is None
    assert stage_disabled_in(None) is None


def test_only_a_job_failed_by_the_switch_is_held():
    err = failure_outcome(LLMStageDisabled("t9_write", "drill"))[0]
    assert is_stage_switch_hold({"status": "failed", "error": err})
    assert not is_stage_switch_hold({"status": "cancelled", "error": "cancelled by admin"})      # declined
    assert not is_stage_switch_hold({"status": "failed", "error": "llm_access_denied: Error 002 for this account"})
    assert not is_stage_switch_hold({"status": "failed", "error": "RuntimeError: rewrite did not succeed"})
    assert not is_stage_switch_hold(None)


# ── T9 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_t9_write_raises_the_refusal_instead_of_a_failed_piece():
    from services.acp_content_writing import service as writing_service
    off = LLMStageDisabled("t9_write", "drill")
    with patch.object(writing_service, "write_content", side_effect=off), \
            patch.object(writing_service, "_finalize_piece", new=AsyncMock()) as fin:
        with pytest.raises(LLMStageDisabled):
            await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), dict(T9_CONTEXT),
                                                       pool=MagicMock())
    fin.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_t9_write_refused_by_the_switch_is_written_after_the_stage_is_on():
    """Through the real t9_write handler and the real T9 service."""
    from services.acp_content_writing import service as writing_service
    from services.jobs import t9_write_job as t9
    payload = {"request_id": REQUEST_ID, "piece_id": PIECE_ID, "tenant_id": TENANT, "context": dict(T9_CONTEXT)}
    store = Store("t9_write", "processing", payload, None)
    finishes = []
    real_finish = t9.WritingProgress.finish

    async def spy_finish(self, ok, message=None):
        finishes.append((ok, message))
        await real_finish(self, ok, message)

    with patch.object(t9.WritingProgress, "finish", spy_finish), \
            patch.object(writing_service, "write_content", side_effect=LLMStageDisabled("t9_write", "drill")), \
            patch.object(writing_service, "_finalize_piece", new=AsyncMock()) as fin:
        with pytest.raises(LLMStageDisabled) as ei:
            await t9.run(_ctx(Pool(store), "t9_write", payload))
    fin.assert_not_awaited()
    assert finishes[-1] == (False, tenant_budget.ON_HOLD_MESSAGE)     # the tenant is not told to try again
    await _fail_like_the_worker(store, t9, ei.value)
    assert store.domain == "processing"                                  # held, not 'failed'

    held = await _requeue(store, frozenset({"t9_write"}))               # still off: held
    assert held["requeued"] == 0 and held["held"][0]["held"] == "stage still switched off: t9_write"
    assert store.job["status"] == "failed"

    out = await _requeue(store, frozenset())                            # switched on: re-queued
    assert out["requeued"] == 1 and store.job["status"] == "queued", out

    async def writes(request_id, piece_id, context, pool):
        store.domain = "approved"
    with patch.object(t9.service, "run_write_background", AsyncMock(side_effect=writes)) as bg:
        done = await t9.run(_ctx(Pool(store), "t9_write", payload))
    bg.assert_awaited_once()
    assert done == {"piece_id": PIECE_ID, "piece_status": "approved"}


@pytest.mark.asyncio
async def test_a_declined_t9_write_marks_its_piece_failed():
    from services.jobs import t9_write_job as t9
    store = Store("t9_write", "processing", {"piece_id": PIECE_ID}, None)
    store.job.update(status="cancelled", error="cancelled by admin")
    await t9.mark_piece_failed(Pool(store), dict(store.job))
    assert store.domain == "failed"


# ── T2 ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_t2_rewrite_refused_by_the_switch_is_written_after_the_stage_is_on():
    from services.jobs import t2_rewrite_job as t2
    payload = {"version_id": VERSION_ID, "tenant_id": TENANT, "published_tour_id": "p-1",
               "rewrite_language": "EN-US"}
    store = Store("t2_rewrite", "pending", payload, _budget())
    prepare = AsyncMock(return_value=({"name": "Velmora Ridge Trek"}, {"_brand_loaded": True}))
    facts = AsyncMock(side_effect=lambda pool, tour, tenant: (tour, None))
    off = LLMStageDisabled("t2_generate", "drill")
    with patch.object(t2, "_prepare", prepare), \
            patch("services.content_generation.t2_facts.with_facts", facts), \
            patch.object(t2, "_rewrite_and_save", AsyncMock(side_effect=_wrapped(off))):
        with pytest.raises(RuntimeError) as ei:
            await t2.run(_ctx(Pool(store), "t2_rewrite", payload))
    await _fail_like_the_worker(store, t2, ei.value)
    assert store.domain == "pending"                                     # held, not 'failed'

    out = await _requeue(store, frozenset())
    assert out["requeued"] == 1 and store.job["status"] == "queued", out

    async def rewrites(*_a, **_k):
        store.domain = "ai_generated"
        return {}
    with patch.object(t2, "_prepare", prepare), \
            patch("services.content_generation.t2_facts.with_facts", facts), \
            patch.object(t2, "_rewrite_and_save", AsyncMock(side_effect=rewrites)) as rw:
        done = await t2.run(_ctx(Pool(store), "t2_rewrite", payload))
    rw.assert_awaited_once()
    assert done["version_status"] == "ai_generated" and "skipped" not in done


def _wrapped(off: LLMStageDisabled) -> RuntimeError:
    """The refusal as a pipeline wrapper raises it (``raise ... from``): the worker reads the explicit cause chain."""
    try:
        raise RuntimeError("rewrite failed") from off
    except RuntimeError as e:
        return e


@pytest.mark.asyncio
async def test_a_t2_rewrite_failed_otherwise_still_marks_its_version_failed():
    from services.jobs import t2_rewrite_job as t2
    store = Store("t2_rewrite", "pending", {"version_id": VERSION_ID}, None)
    store.job.update(status="failed", error="llm_access_denied: Error 002 for this account")   # an account block
    await t2.mark_version_failed(Pool(store), dict(store.job))
    assert store.domain == "failed"
