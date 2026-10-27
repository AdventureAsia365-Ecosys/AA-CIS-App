"""AA-990 — the credit-reset row and the deploy-gate horizon against a real shared.job (CI Postgres).

The unit tests (tests/unit/test_aa990_geocode_stall.py, test_aa725_deploy_gate.py) check these paths with faked pools;
here the SQL itself runs: queue.active_count's horizon CASE, the move of an already-queued place_geocode row to the
reset, and the reset key's ON CONFLICT path when its row is no longer queued.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.places import enqueue as enq  # noqa: E402
from shared.jobs import queue  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute("DELETE FROM shared.job")
    yield p
    await p.execute("DELETE FROM shared.job")
    await p.close()


@pytest.fixture
def plain_enqueue(monkeypatch):
    """registry.enqueue needs the kind registered (importing services.jobs pulls in the whole app); the queue's own
    enqueue runs the same INSERT … ON CONFLICT."""
    async def _enqueue(db, kind, payload, **kw):
        return await queue.enqueue(db, kind, payload, **kw)
    monkeypatch.setattr(enq, "enqueue", _enqueue)


@pytest.mark.asyncio
async def test_active_count_leaves_rows_due_beyond_the_horizon_to_scheduled_later(pool):
    now = datetime.now(timezone.utc)
    await queue.enqueue(pool, "aa990_far", {}, run_after=now + timedelta(hours=2))
    await queue.enqueue(pool, "aa990_soon", {}, run_after=now + timedelta(minutes=30))
    out = await queue.active_count(pool)
    assert out["active"] == 1 and out["scheduled_later"] == 1 and out["by_status"] == {"queued": 1}


@pytest.mark.asyncio
async def test_credit_reset_moves_the_queued_geocode_row_and_adds_none(pool, plain_enqueue):
    queued, _ = await queue.enqueue(pool, enq.GEOCODE_KIND, {"reason": "place_extract"})
    job, at, created = await enq.enqueue_after_credit_reset(pool, reason="place_geocode:credit_reset",
                                                            caller_job_id="00000000-0000-0000-0000-000000000001")
    assert (job, created) == (queued, False)
    rows = await pool.fetch("SELECT id::text, run_after FROM shared.job WHERE kind = $1", enq.GEOCODE_KIND)
    assert [(r["id"], r["run_after"]) for r in rows] == [(queued, at)]


@pytest.mark.asyncio
async def test_a_cancelled_reset_row_does_not_swallow_the_next_stop(pool, plain_enqueue):
    now = datetime.now(timezone.utc)
    first, at, created = await enq.enqueue_after_credit_reset(pool, reason="r", now=now)
    assert created
    await pool.execute("UPDATE shared.job SET status = 'cancelled' WHERE id = $1::uuid", first)
    caller = "00000000-0000-0000-0000-000000000002"
    second, at2, created2 = await enq.enqueue_after_credit_reset(pool, reason="r", now=now, caller_job_id=caller)
    assert created2 and second != first and at2.date() == at.date()
    row = await pool.fetchrow("SELECT status, idempotency_key FROM shared.job WHERE id = $1::uuid", second)
    assert row["status"] == "queued"
    assert row["idempotency_key"] == f"{enq.GEOCODE_KIND}:credit_reset:{at2:%Y-%m-%d}:{caller}"
