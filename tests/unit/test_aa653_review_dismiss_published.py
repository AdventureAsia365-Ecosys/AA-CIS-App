"""AA-653 (S207) — a tour with a live Master Content version leaves the review queue.

Reviewers only work on tours with nothing published; a better version of a published tour comes
from Regenerate. Two places enforce it: process_export() dismisses the tour's pending rows right
after publishing, and _enqueue_review() writes a failed version of an already-published tour as
'dismissed' (soft, still traceable) instead of 'pending'.
"""
from unittest.mock import AsyncMock, patch

import pytest

from api.routers.admin_pipeline import _enqueue_review
from services.export import handler

GC_ROW = {"id": "gc-1", "tour_id": "t-1", "tenant_id": "t-1", "batch_id": None, "aa_name": "Tour",
          "aa_subtitle": "s", "aa_summary": "sum", "aa_description": "d", "aa_highlights": "[]",
          "aa_itineraries": "Day 1...", "mobile_card_text": None, "seo_title": "t", "seo_meta": "m" * 150,
          "seo_keywords_used": "[]", "og_tags": "{}", "quality_score_id": None, "quality_score": 9.0,
          "country": "Vietnam", "duration": "5 days"}


def _dismiss_calls(conn):
    return [c for c in conn.execute.call_args_list
            if "review_queue" in c.args[0] and "'dismissed'" in c.args[0]]


@pytest.mark.asyncio
async def test_publish_dismisses_the_tours_pending_reviews(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    conn = AsyncMock()
    conn.fetchrow.side_effect = [dict(GC_ROW), {"id": "gc-1"}]
    with patch.object(handler.asyncpg, "connect", AsyncMock(return_value=conn)), \
         patch("services.jobs.a3_atomize_job.enqueue_a3_atomize", AsyncMock()):
        out = await handler.process_export("gc-1")
    assert out["status"] == "exported"
    calls = _dismiss_calls(conn)
    assert len(calls) == 1
    sql, *args = calls[0].args
    assert "review_status = 'pending'" in sql and "tour_id = $1" in sql
    assert args == ["t-1"]


@pytest.mark.asyncio
async def test_publish_survives_a_failed_dismiss(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    conn = AsyncMock()
    conn.fetchrow.side_effect = [dict(GC_ROW), {"id": "gc-1"}]

    async def execute(sql, *args):
        if "review_queue" in sql:
            raise RuntimeError("db")
        return "UPDATE 1"
    conn.execute.side_effect = execute
    with patch.object(handler.asyncpg, "connect", AsyncMock(return_value=conn)), \
         patch("services.jobs.a3_atomize_job.enqueue_a3_atomize", AsyncMock()):
        out = await handler.process_export("gc-1")
    assert out["status"] == "exported"


@pytest.mark.asyncio
async def test_enqueue_marks_a_published_tour_dismissed_not_pending():
    conn = AsyncMock()
    await _enqueue_review(conn, "t-1", "gc-2", {"quality_score": 6.0})
    sql = conn.execute.call_args.args[0]
    # status is decided in SQL from the tour's live Master Content row
    assert "gold_aa_internal.published_tours" in sql
    assert "master_status <> 'trashed'" in sql
    assert "THEN 'dismissed' ELSE 'pending'" in sql
    # a repeat run must not add a second row for the same version, pending or dismissed
    assert "review_status IN ('pending', 'dismissed')" in sql
