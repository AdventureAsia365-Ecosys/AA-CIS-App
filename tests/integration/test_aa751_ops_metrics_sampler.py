"""AA-751 / AA-832 — the api's ops metrics sampler against a real Postgres.

- ROLE_CONNECTIONS_SQL: a LOGIN role with a CONNECTION LIMIT and one open session is counted 1; a role
  without a limit or without LOGIN is not listed (moved from the removed AA-832 job test).
- queue.claim() merges progress.attempt_started_at on every claim and keeps the first started_at (F18).
- backlog.sample(): the four reads on real shared.job / shared.job_worker rows — no live worker,
  unregistered kind, claimable since the worker's start, overdue on the current attempt.
- sample_once(): one PutMetricData per namespace (CloudWatch is a recording stub).
"""
import json
import os
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.core import ops_metrics_sampler as sampler  # noqa: E402
from shared.jobs import backlog, queue  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
KINDS = {"aa751_k": SimpleNamespace(background=False, expected_seconds=60)}


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute("DELETE FROM shared.job")
    await p.execute("DELETE FROM shared.job_worker")
    yield p
    await p.execute("DELETE FROM shared.job")
    await p.execute("DELETE FROM shared.job_worker")
    await p.close()


@pytest_asyncio.fixture
async def roles(pool):
    """Three cluster-wide roles with a unique suffix: limited LOGIN, unlimited LOGIN, limited NOLOGIN.
    CI's cistest is SUPERUSER (postgres service); a local DB without CREATEROLE skips."""
    if not await pool.fetchval("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user"):
        pytest.skip("test DB user cannot CREATE ROLE (CI runs it as SUPERUSER)")
    sfx = uuid.uuid4().hex[:8]
    names = {"limited": f"aa832_lim_{sfx}", "unlimited": f"aa832_unl_{sfx}", "nologin": f"aa832_nol_{sfx}"}
    await pool.execute(f"CREATE ROLE {names['limited']} LOGIN PASSWORD 'aa832' CONNECTION LIMIT 4")
    await pool.execute(f"CREATE ROLE {names['unlimited']} LOGIN PASSWORD 'aa832'")
    await pool.execute(f"CREATE ROLE {names['nologin']} NOLOGIN CONNECTION LIMIT 4")
    yield names
    for n in names.values():
        await pool.execute(f"DROP ROLE IF EXISTS {n}")


class StubCloudWatch:
    def __init__(self):
        self.calls = []

    def put_metric_data(self, **kw):
        self.calls.append(kw)


async def _queued(pool, kind, minutes_ago):
    return await pool.fetchval(
        "INSERT INTO shared.job (kind, created_by, run_after) "
        "VALUES ($1, 'aa751-test', now() - make_interval(mins => $2)) RETURNING id::text", kind, minutes_ago)


# ── AA/RDS ──────────────────────────────────────────────────────────────────────────────────────

async def test_sql_counts_sessions_of_limited_login_roles(pool, roles):
    rows = {r["rolname"]: r for r in await pool.fetch(sampler.ROLE_CONNECTIONS_SQL)}
    assert roles["unlimited"] not in rows and roles["nologin"] not in rows
    assert rows[roles["limited"]]["rolconnlimit"] == 4 and rows[roles["limited"]]["connections"] == 0

    conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME,
                                 user=roles["limited"], password="aa832")
    try:
        rows = {r["rolname"]: r for r in await pool.fetch(sampler.ROLE_CONNECTIONS_SQL)}
        assert rows[roles["limited"]]["connections"] == 1
    finally:
        await conn.close()


async def test_sample_once_publishes_both_namespaces(pool, roles):
    cw = StubCloudWatch()
    out = await sampler.sample_once(pool, cw, kinds=KINDS)
    assert "rds_error" not in out and "jobs_error" not in out, out
    assert out["roles"][roles["limited"]] == {"connections": 0, "limit": 4, "pct": 0.0}
    assert [c["Namespace"] for c in cw.calls] == ["AA/RDS", "AA/Jobs/CIS"]
    dims = {d["Dimensions"][0]["Value"] for d in cw.calls[0]["MetricData"]}
    assert roles["limited"] in dims and roles["unlimited"] not in dims
    assert {d["MetricName"]: d["Value"] for d in cw.calls[1]["MetricData"]} == {
        "OldestQueuedAgeSeconds": 0.0, "OverdueRunningJobs": 0.0}


# ── F18: attempt_started_at ─────────────────────────────────────────────────────────────────────

async def test_claim_writes_attempt_started_at_per_attempt(pool):
    job_id, _ = await queue.enqueue(pool, "aa751_k", {})
    await queue.set_progress(pool, job_id, {"cursors": {"p": 1}})
    first = await queue.claim(pool, "w1", {"aa751_k": 1}, 60)
    assert first.id == job_id and "attempt_started_at" in first.progress
    assert first.progress["cursors"] == {"p": 1}          # handler keys survive the merge
    # age the first attempt, re-queue it (lease lost / retry), claim again
    await pool.execute(
        "UPDATE shared.job SET started_at = now() - interval '1 hour', status = 'queued', locked_by = NULL, "
        "locked_until = NULL, progress = progress || jsonb_build_object('attempt_started_at', now() - interval "
        "'1 hour') WHERE id = $1::uuid", job_id)
    second = await queue.claim(pool, "w2", {"aa751_k": 1}, 60)
    row = await pool.fetchrow(
        "SELECT now() - started_at AS since_first, "
        "now() - (progress->>'attempt_started_at')::timestamptz AS since_attempt, progress "
        "FROM shared.job WHERE id = $1::uuid", job_id)
    assert second.attempt == 2
    assert row["since_first"].total_seconds() > 3500           # first attempt's start kept
    assert row["since_attempt"].total_seconds() < 60           # this attempt
    assert json.loads(row["progress"])["cursors"] == {"p": 1}


# ── backlog.sample on real rows ─────────────────────────────────────────────────────────────────

async def test_no_live_worker(pool):
    await _queued(pool, "aa751_k", 20)
    out = await backlog.sample(pool, KINDS)
    assert out["starved"]["aa751_k"]["reason"] == "no_live_worker"
    assert 1190 <= out["oldest_queued_age_seconds"] <= 1260
    # a worker that stopped 5 minutes ago: starved since then
    await queue.worker_register(pool, "w-old", "h", 8, {"aa751_k": 1})
    await pool.execute("UPDATE shared.job_worker SET stopped_at = now(), last_seen_at = now() - interval '5 minutes'")
    out = await backlog.sample(pool, KINDS)
    assert out["live_workers"] == 0 and 290 <= out["oldest_queued_age_seconds"] <= 330


async def test_unregistered_kind_probe(pool):
    """Window B probe: x6_probe is in no worker's caps, run_after now - 20 min."""
    await queue.worker_register(pool, "w1", "h", 8, {"aa751_k": 1})
    await _queued(pool, "x6_probe", 20)
    out = await backlog.sample(pool, KINDS)
    assert out["starved"]["x6_probe"]["reason"] == "unregistered"
    assert out["oldest_queued_age_seconds"] >= 1190


async def test_claimable_since_worker_start_and_last_finish(pool):
    await queue.worker_register(pool, "w1", "h", 8, {"aa751_k": 1})
    await _queued(pool, "aa751_k", 30)
    out = await backlog.sample(pool, KINDS)
    assert out["starved"]["aa751_k"]["reason"] == "claimable"
    assert out["oldest_queued_age_seconds"] < 30                 # the worker just started
    await pool.execute("UPDATE shared.job_worker SET started_at = now() - interval '10 minutes'")
    out = await backlog.sample(pool, KINDS)
    assert 590 <= out["oldest_queued_age_seconds"] <= 630
    # a job finished 2 minutes ago freed capacity then
    await pool.execute("INSERT INTO shared.job (kind, created_by, status, finished_at) "
                       "VALUES ('aa751_k', 'aa751-test', 'succeeded', now() - interval '2 minutes')")
    out = await backlog.sample(pool, KINDS)
    assert 110 <= out["oldest_queued_age_seconds"] <= 150
    # at the kind's cap: by design, 0
    job = await queue.claim(pool, "w1", {"aa751_k": 1}, 600)
    assert job is not None
    await _queued(pool, "aa751_k", 30)
    out = await backlog.sample(pool, KINDS)
    assert out["oldest_queued_age_seconds"] == 0.0 and out["running"] == 1


async def test_overdue_running_on_current_attempt(pool):
    await queue.worker_register(pool, "w1", "h", 8, {"aa751_k": 1})
    await pool.execute(
        "INSERT INTO shared.job (kind, created_by, status, locked_by, locked_until, attempt, started_at, progress) "
        "VALUES ('aa751_k', 'aa751-test', 'running', 'w1', now() + interval '1 minute', 1, now() - interval '1 hour', "
        "jsonb_build_object('attempt_started_at', now() - interval '3 minutes'))")
    out = await backlog.sample(pool, KINDS)                       # expected 60 s: 180 > 120
    assert out["overdue_running_jobs"] == 1
    await pool.execute("UPDATE shared.job SET progress = jsonb_build_object('attempt_started_at', now())")
    out = await backlog.sample(pool, KINDS)
    assert out["overdue_running_jobs"] == 0


async def test_deploy_gate_exempt_mechanism_still_filters(pool):
    await _queued(pool, "aa751_exempt", 1)
    await _queued(pool, "aa751_k", 1)
    assert (await queue.active_count(pool))["active"] == 2
    assert (await queue.active_count(pool, exempt_kinds=["aa751_exempt"]))["active"] == 1


# ── retired kinds: rolling-deploy leftovers are cancelled, nothing else is touched ──────────────

async def test_retired_kind_rows_cancelled_on_a_tick(pool):
    q = await _queued(pool, "ops_rds_role_connections", 30)
    r = await _queued(pool, "ops_rds_role_connections", 1)
    await pool.execute("UPDATE shared.job SET status = 'running', started_at = now(), locked_by = 'old-w', "
                       "locked_until = now() + interval '5 min' WHERE id = $1::uuid", r)
    other = await _queued(pool, "aa751_k", 30)
    out = await sampler.sample_once(pool, StubCloudWatch(), kinds=KINDS)
    assert out["retired_cancelled"] == 2 and "retired_error" not in out, out
    rows = {str(x["id"]): x for x in await pool.fetch("SELECT id, status, error, locked_by, finished_at FROM shared.job")}
    for jid in (q, r):
        assert rows[jid]["status"] == "cancelled"
        assert rows[jid]["error"] == "kind removed (replaced by the ops metrics sampler)"
        assert rows[jid]["locked_by"] is None and rows[jid]["finished_at"] is not None
    assert rows[other]["status"] == "queued"
    # the old worker finishing its row afterwards cannot overwrite the cancel
    assert not await queue.complete(pool, r, "old-w", {"ok": True}, None)
    # idempotent: the next tick finds nothing
    again = await sampler.sample_once(pool, StubCloudWatch(), kinds=KINDS)
    assert "retired_cancelled" not in again
