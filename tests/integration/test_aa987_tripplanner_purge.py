"""AA-987 — the TripPlanner purge of a tour lifecycle event, on real Postgres (services/jobs/tripplanner_purge_job.py).

A lifecycle action writes gold_aa_internal.tour_lifecycle_event; run_after_commit enqueues one `tripplanner_purge`
job per event, keyed on the event's idempotency key; a replay of the post-commit work creates no second job; the
sweep enqueues an event that has no job (an action whose post-commit step never ran) and is idempotent; the handler
reads the event back from the outbox table and posts a body whose signature TripPlanner can verify. The HTTP call is
a stub (no network). Each test runs in one rolled-back transaction (the helpers of test_aa986_l2_tour_lifecycle).
"""
import hashlib
import hmac
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
import test_aa986_l2_tour_lifecycle as l2  # noqa: E402  (seed helpers)

from services import tour_lifecycle as tl  # noqa: E402
from services.jobs import tripplanner_purge_job as tp  # noqa: E402

SECRET = "aa987-test-secret"


@pytest_asyncio.fixture
async def conn():
    """One rolled-back transaction, the same shape as test_aa986_l2_tour_lifecycle's fixture."""
    c = await asyncpg.connect(l2.DSN)
    tr = c.transaction()
    await tr.start()
    try:
        if await c.fetchval("SELECT to_regclass('gold_aa_internal.tenant_tour_versions')") is None:
            await c.execute(l2.TTV_LIVE_STANDIN_DDL)
        yield c
    finally:
        await tr.rollback()
        await c.close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("TRIPPLANNER_REVALIDATE_URL", raising=False)
    monkeypatch.setenv("TRIPPLANNER_REVALIDATE_SECRET", SECRET)
    rec = AsyncMock()
    monkeypatch.setattr("services.tour_lifecycle.effects.recompute_after_status_change", rec)
    monkeypatch.setattr("services.tour_lifecycle.effects.recheck_trip_pages", AsyncMock())


async def _purge_jobs(conn, t):
    rows = await conn.fetch(
        "SELECT j.idempotency_key, j.payload, j.max_attempts, e.event FROM shared.job j "
        "JOIN gold_aa_internal.tour_lifecycle_event e ON j.idempotency_key = 'tripplanner_purge:' || e.idempotency_key "
        "WHERE j.kind = 'tripplanner_purge' AND e.source_id = $1 ORDER BY e.id", t.group)
    return [(r["event"], json.loads(r["payload"]), r["max_attempts"]) for r in rows]


@pytest.mark.asyncio
async def test_each_lifecycle_event_gets_one_purge_job_and_a_replay_adds_none(conn):
    t = await l2._tour(conn, "aa987-retire")
    out = await tl.trash_master(conn, t.tour)
    assert out.events and all(e["idempotency_key"] for e in out.events)
    await tl.run_after_commit(l2._OneConnPool(conn), out)
    jobs = await _purge_jobs(conn, t)
    assert [j[0] for j in jobs] == ["retired"]
    assert jobs[0][1] == {"event_key": out.events[0]["idempotency_key"], "trigger": "after_commit"}
    assert jobs[0][2] == tp.MAX_ATTEMPTS

    await tl.run_after_commit(l2._OneConnPool(conn), out)          # replay of the post-commit work
    assert len(await _purge_jobs(conn, t)) == 1

    out = await tl.restore_master(conn, t.tour)                      # comes back paused: no served-state event yet
    await tl.run_after_commit(l2._OneConnPool(conn), out)
    out = await tl.activate_master(conn, t.tour)
    await tl.run_after_commit(l2._OneConnPool(conn), out)
    events = [j[0] for j in await _purge_jobs(conn, t)]
    assert events[0] == "retired" and events[-1] == "restored"
    assert len(events) == await conn.fetchval(
        "SELECT count(*) FROM gold_aa_internal.tour_lifecycle_event WHERE source_id = $1", t.group)


@pytest.mark.asyncio
async def test_the_sweep_enqueues_an_event_without_a_job_once(conn):
    t = await l2._tour(conn, "aa987-sweep")
    await tl.pause_master(conn, t.tour)                               # no run_after_commit: the lost-enqueue case
    assert await _purge_jobs(conn, t) == []
    # now() is the transaction start inside this rolled-back test, so the lag is negative here.
    created = await tp.sweep(conn, lag_s=-60)
    assert created >= 1
    jobs = await _purge_jobs(conn, t)
    assert [(j[0], j[1]["trigger"]) for j in jobs] == [("paused", "sweep")]
    assert await tp.sweep(conn, lag_s=-60) == 0                       # idempotent
    assert await tp.sweep(conn, window_s=0, lag_s=-60) == 0           # outside the window: nothing


@pytest.mark.asyncio
async def test_the_handler_reads_the_outbox_row_and_signs_the_notice(conn):
    t = await l2._tour(conn, "aa987-deliver")
    out = await tl.trash_master(conn, t.tour)
    key = out.events[0]["idempotency_key"]
    sent = {}

    async def post(url, body, headers):
        sent.update(url=url, body=body, headers=headers)
        return 200, '{"ok": true}'

    ctx = SimpleNamespace(payload={"event_key": key, "trigger": "after_commit"}, pool=l2._OneConnPool(conn),
                          job_id="j", job=SimpleNamespace(attempt=1))
    with patch.object(tp, "_post", post):
        result = await tp.run(ctx)
    assert result["delivered"] is True and result["lifecycle_event"] == "retired" and result["source_id"] == t.group

    body = json.loads(sent["body"])
    assert body["contract"] == tp.CONTRACT and len(body["events"]) == 1
    event = body["events"][0]
    assert event["idempotency_key"] == key and event["source_id"] == t.group and event["event"] == "retired"
    assert event["event_id"] == await conn.fetchval(
        "SELECT id FROM gold_aa_internal.tour_lifecycle_event WHERE idempotency_key = $1", key)
    ts, mac = (p.split("=", 1)[1] for p in sent["headers"][tp.SIGNATURE_HEADER].split(","))
    expected = hmac.new(SECRET.encode(), f"{ts}.".encode() + sent["body"], hashlib.sha256).hexdigest()
    assert hmac.compare_digest(mac, expected)


@pytest.mark.asyncio
async def test_the_runtime_role_can_enqueue_read_and_sweep(conn):
    """cis_app (api + worker login) holds SELECT on the event table (CIS 272) and DML on shared.job (Dev)."""
    await conn.execute(l2.migration_body(272))
    t = await l2._tour(conn, "aa987-role")
    out = await tl.pause_master(conn, t.tour)
    for schema in ("gold_aa_internal", "shared"):
        await conn.execute(f"GRANT USAGE ON SCHEMA {schema} TO cis_app")
    await conn.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON shared.job TO cis_app")
    await conn.execute("SET LOCAL ROLE cis_app")
    try:
        assert await tp.sweep(conn, lag_s=-60) >= 1
        row = await conn.fetchrow(tp.EVENT_SQL, out.events[0]["idempotency_key"])
    finally:
        await conn.execute("RESET ROLE")
    assert row is not None and row["event"] == "paused"
