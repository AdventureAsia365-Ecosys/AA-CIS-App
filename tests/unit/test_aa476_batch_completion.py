"""AA-476 — pipeline_runs.status stuck at 'ingesting' fix.

Root cause: sync_batch_completion() (services/export/handler.py) is the ONLY place
pipeline_runs.status ever advances, and it used to only recognize 'published' as a
resolved tour outcome. A rejected tour never got any raw_tours.pipeline_status update at
all (reject_review() only touched review_queue/generated_content), so it stayed 'ingested'
forever and the batch's status never left 'ingesting' — even once every other tour in the
batch was long done.

AA-483 changed sync_batch_completion()'s status-flip mechanism (see that function's own
docstring) — the flip is now a single atomic `UPDATE ... WHERE NOT EXISTS(...) RETURNING 1`
instead of a separate SELECT COUNT(*) + conditional UPDATE, and the function now returns
(pending_count, just_completed) instead of just pending_count. This file's FakeConn/assertions
were updated to match that shape; test_aa483_batch_completion_race.py covers the new
atomicity/no-double-fire behavior specifically.

These tests drive the real functions (no re-implemented copies), with a minimal fake conn.
"""
import pytest
from unittest.mock import AsyncMock

from services.export.handler import sync_batch_completion, mark_tour_rejected

BATCH_ID = "44444444-4444-4444-4444-444444444444"
TOUR_ID = "55555555-5555-5555-5555-555555555555"


class FakeConn:
    """Fakes just enough of asyncpg's Connection for sync_batch_completion/mark_tour_rejected:
    execute() for the tours_passed UPDATE, fetchval() for BOTH the pending-count SELECT and the
    atomic status-flip UPDATE...RETURNING (distinguished by SQL content — a real connection
    would just run whichever statement text it's given), fetchrow() for mark_tour_rejected's
    UPDATE ... RETURNING batch_id."""

    def __init__(self, pending_count: int, rejected_row_batch_id=BATCH_ID):
        self.pending_count = pending_count
        self.rejected_row_batch_id = rejected_row_batch_id
        self.executed: list[str] = []
        self.fetchval_calls: list[str] = []

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return "UPDATE 1"

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append(sql)
        if "UPDATE shared.pipeline_runs" in sql and "RETURNING 1" in sql:
            # Simulates the atomic flip: matches only when nothing is left pending.
            return 1 if self.pending_count == 0 else None
        return self.pending_count

    async def fetchrow(self, sql, *args):
        if self.rejected_row_batch_id is None:
            return None
        return {"batch_id": self.rejected_row_batch_id}


@pytest.mark.asyncio
async def test_sync_batch_completion_stays_ingesting_while_tours_pending():
    """>0 non-terminal tours remain → status flip UPDATE never matches a row."""
    conn = FakeConn(pending_count=2)
    pending, just_completed = await sync_batch_completion(conn, BATCH_ID)
    assert pending == 2
    assert just_completed is False


@pytest.mark.asyncio
async def test_sync_batch_completion_flips_to_completed_when_zero_pending():
    """All tours terminal (published + hitl_rejected/failed mix) → status flip fires."""
    conn = FakeConn(pending_count=0)
    pending, just_completed = await sync_batch_completion(conn, BATCH_ID)
    assert pending == 0
    assert just_completed is True


@pytest.mark.asyncio
async def test_sync_batch_completion_pending_query_excludes_terminal_non_published():
    """The pending count query must treat hitl_rejected/failed as resolved, not just
    published — this is the actual AA-476 fix, not just a status-flip mechanism check."""
    conn = FakeConn(pending_count=0)
    captured_sql = []

    async def fetchval(sql, *args):
        captured_sql.append(sql)
        if "UPDATE shared.pipeline_runs" in sql and "RETURNING 1" in sql:
            return 1
        return 0

    conn.fetchval = fetchval
    await sync_batch_completion(conn, BATCH_ID)
    pending_count_sql = next(s for s in captured_sql if "SELECT COUNT" in s)
    assert "hitl_rejected" in pending_count_sql
    assert "failed" in pending_count_sql
    assert "published" in pending_count_sql


@pytest.mark.asyncio
async def test_mark_tour_rejected_sets_terminal_status_and_syncs_batch():
    """reject_review()'s missing half: raw_tours.pipeline_status must actually change, and
    the batch completion check must run off the back of it."""
    conn = FakeConn(pending_count=0, rejected_row_batch_id=BATCH_ID)
    await mark_tour_rejected(conn, TOUR_ID)

    # sync_batch_completion() ran (its tours_passed UPDATE is the one execute() call in this
    # path) and its atomic flip fetchval() matched a row (pending_count=0 here).
    assert any("tours_passed" in c for c in conn.executed)
    flip_calls = [s for s in conn.fetchval_calls if "RETURNING 1" in s]
    assert len(flip_calls) == 1


@pytest.mark.asyncio
async def test_mark_tour_rejected_noop_when_tour_has_no_batch():
    """A tour with no batch_id (shouldn't happen, but defensive) must not explode or touch
    pipeline_runs."""
    conn = FakeConn(pending_count=0, rejected_row_batch_id=None)
    await mark_tour_rejected(conn, TOUR_ID)
    assert conn.executed == []


@pytest.mark.asyncio
async def test_mark_tour_rejected_never_demotes_a_tour_with_an_active_master():
    """S218: rejecting an OLD version's review row after the tour already reached Master set
    'hitl_rejected' on a live tour (Taiwan / Korea, 01/10). The UPDATE is guarded on 'no active
    Master'; when the guard matches nothing (row None) the batch sync is skipped."""
    conn = FakeConn(pending_count=0, rejected_row_batch_id=None)
    seen = []
    orig = conn.fetchrow

    async def spy(sql, *args):
        seen.append(sql)
        return None   # guard excluded the tour — nothing updated
    conn.fetchrow = spy
    await mark_tour_rejected(conn, TOUR_ID)
    assert "master_status = 'active'" in seen[0] and "NOT EXISTS" in seen[0]
    assert conn.executed == []   # no batch sync when nothing changed
    conn.fetchrow = orig


@pytest.mark.asyncio
async def test_aa746_trashed_superseded_deleted_and_in_review_tours_count_as_settled():
    """AA-746: 10 batches sat in 'ingesting' forever because a trashed/superseded tour (or one
    waiting on a human in Review Queue) never reaches published/hitl_rejected/failed."""
    conn = FakeConn(pending_count=0)
    captured_sql = []

    async def fetchval(sql, *args):
        captured_sql.append(sql)
        if "UPDATE shared.pipeline_runs" in sql and "RETURNING 1" in sql:
            return 1
        return 0

    conn.fetchval = fetchval
    await sync_batch_completion(conn, BATCH_ID)
    pending_sql = next(s for s in captured_sql if "SELECT COUNT" in s)
    flip_sql = next(s for s in captured_sql if "RETURNING 1" in s)
    for sql in (pending_sql, flip_sql):
        assert "rt.source_status = 'active'" in sql
        assert "rt.deleted_at IS NULL" in sql
        assert "hitl_required" in sql
        assert "review_status = 'pending'" in sql
