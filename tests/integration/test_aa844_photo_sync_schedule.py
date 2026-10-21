"""AA-844 — the daily photo_sync schedule against real Postgres (lesson S229/S231: new SQL needs a
real-DB test).

- enqueue_scheduled creates one job per CON country with key photo_sync:schedule:<slug>:<day>, in
  one transaction; a second call the same day is skipped (jobs still queued) or creates nothing
  (created=False) once they finished; another queued photo_sync job makes it skip.
- LAST_SCHEDULED_SQL runs (DISTINCT ON + ORDER BY on the same expression) and last_scheduled()
  returns the expected shape; a failed job (no result -> errors NULL) is not all_ok.
Each test cleans its own photo_sync rows (the shared test DB has no other photo_sync jobs).
"""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.jobs import photo_sync_job  # noqa: E402  (registers the kind)
from services.photos.sync import CON_FOLDERS, country_slug  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
DAY = "2026-10-21"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute("DELETE FROM shared.job WHERE kind = 'photo_sync'")
    yield p
    await p.execute("DELETE FROM shared.job WHERE kind = 'photo_sync'")
    await p.close()


async def _finish_all(pool, *, errors=0, fail_country=None):
    """Mark every scheduled job as the worker would: succeeded with totals, or failed (no result)."""
    rows = await pool.fetch("SELECT id, payload FROM shared.job WHERE kind = 'photo_sync'")
    for r in rows:
        payload = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
        country = payload["folders"][0]["country"]
        if country == fail_country:
            await pool.execute("UPDATE shared.job SET status = 'failed', finished_at = now(), error = 'x' "
                               "WHERE id = $1", r["id"])
        else:
            result = {"trigger": "schedule", "totals": {"images": 3, "errors": errors}}
            await pool.execute("UPDATE shared.job SET status = 'succeeded', finished_at = now(), "
                               "result = $2::jsonb WHERE id = $1", r["id"], json.dumps(result))


@pytest.mark.asyncio
async def test_first_call_enqueues_one_job_per_country(pool):
    out = await photo_sync_job.enqueue_scheduled(pool, day=DAY)
    assert out["skipped"] is None
    assert [e["country"] for e in out["enqueued"]] == [f["country"] for f in CON_FOLDERS]
    assert all(e["created"] for e in out["enqueued"])
    rows = await pool.fetch("SELECT idempotency_key, created_by, payload, status FROM shared.job "
                            "WHERE kind = 'photo_sync' ORDER BY idempotency_key")
    assert sorted(r["idempotency_key"] for r in rows) == sorted(
        f"photo_sync:schedule:{country_slug(f['country'])}:{DAY}" for f in CON_FOLDERS)
    assert {r["created_by"] for r in rows} == {"scheduler:aa-844"}
    assert {r["status"] for r in rows} == {"queued"}
    for r in rows:
        payload = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
        assert payload["trigger"] == "schedule" and payload["match_destinations"] is False
        assert len(payload["folders"]) == 1


@pytest.mark.asyncio
async def test_second_call_same_day_adds_nothing(pool):
    await photo_sync_job.enqueue_scheduled(pool, day=DAY)
    # Still queued: skipped by the active check.
    again = await photo_sync_job.enqueue_scheduled(pool, day=DAY)
    assert again["enqueued"] == [] and "already queued" in again["skipped"]
    # Finished: the idempotency keys hold, nothing new.
    await _finish_all(pool)
    third = await photo_sync_job.enqueue_scheduled(pool, day=DAY)
    assert third["skipped"] is None and not any(e["created"] for e in third["enqueued"])
    assert await pool.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'photo_sync'") == len(CON_FOLDERS)


@pytest.mark.asyncio
async def test_an_active_manual_job_makes_the_schedule_skip(pool):
    from shared.jobs.registry import enqueue

    manual_id, _ = await enqueue(pool, "photo_sync", {"folders": CON_FOLDERS[:1]}, created_by="admin:test")
    out = await photo_sync_job.enqueue_scheduled(pool, day=DAY)
    assert out["enqueued"] == [] and out["skipped"] == f"photo_sync job {manual_id} already queued"
    assert await pool.fetchval("SELECT count(*) FROM shared.job WHERE kind = 'photo_sync'") == 1


@pytest.mark.asyncio
async def test_last_scheduled_shape_and_all_ok(pool):
    async with pool.acquire() as conn:
        assert await photo_sync_job.last_scheduled(conn) is None

    await photo_sync_job.enqueue_scheduled(pool, day=DAY)
    await _finish_all(pool)
    async with pool.acquire() as conn:
        got = await photo_sync_job.last_scheduled(conn)
    assert set(got) == {"day", "countries", "all_ok"}
    assert [c["country"] for c in got["countries"]] == [f["country"] for f in CON_FOLDERS]
    assert all(c["status"] == "succeeded" and c["errors"] == 0 and c["job_id"] for c in got["countries"])
    assert got["all_ok"] is True


@pytest.mark.asyncio
async def test_last_scheduled_failed_job_or_errors_is_not_ok(pool):
    await photo_sync_job.enqueue_scheduled(pool, day=DAY)
    await _finish_all(pool, fail_country=CON_FOLDERS[0]["country"])
    async with pool.acquire() as conn:
        got = await photo_sync_job.last_scheduled(conn)
    first = got["countries"][0]
    assert first["status"] == "failed" and first["errors"] is None
    assert got["all_ok"] is False

    await pool.execute("DELETE FROM shared.job WHERE kind = 'photo_sync'")
    await photo_sync_job.enqueue_scheduled(pool, day=DAY)
    await _finish_all(pool, errors=2)
    async with pool.acquire() as conn:
        got = await photo_sync_job.last_scheduled(conn)
    assert all(c["errors"] == 2 for c in got["countries"]) and got["all_ok"] is False


@pytest.mark.asyncio
async def test_last_scheduled_ignores_manual_jobs(pool):
    from shared.jobs.registry import enqueue

    await enqueue(pool, "photo_sync", {"folders": CON_FOLDERS[:1]}, created_by="admin:test")
    async with pool.acquire() as conn:
        assert await photo_sync_job.last_scheduled(conn) is None
