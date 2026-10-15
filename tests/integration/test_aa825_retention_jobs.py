"""AA-825 — retention job kinds against real Postgres (migration 233 applied by conftest; its view
rebuild is skipped in the CI DB, the rollup tables and indexes are there).

Each test seeds rows it can recognise (its own tenant / stage / account marker), runs the kind's
handler through a real shared.job row and checks: DRY RUN writes nothing, a live run rolls up first
and then archives + deletes exactly the expired rows, protected rows stay, the 2x rule holds a run.
S3 is a recording double. Assertions are on the seeded rows, not on table totals, because the
shared test DB can hold old rows of other tests (the run may delete those too; that is fine there).
"""
import gzip
import json
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.jobs.registry import JobContext  # noqa: E402
from services.jobs import retention_job  # noqa: E402
from services.retention.archive import S3Archiver  # noqa: E402
from services.retention.policy import SPECS  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kw):
        self.objects[kw["Key"]] = kw

    def lines(self):
        out = []
        for kw in self.objects.values():
            out += [json.loads(x) for x in gzip.decompress(kw["Body"]).decode().splitlines() if x]
        return out


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    yield p
    await p.execute("DELETE FROM shared.job WHERE kind LIKE 'retention_%'")
    await p.close()


async def _run(pool, kind, payload, s3=None):
    """Runs the kind's handler as the worker would (a real shared.job row for progress), then marks
    the job succeeded with its result so the next run sees it as history."""
    job_id, _ = await queue.enqueue(pool, kind, payload)
    row = await pool.fetchrow("SELECT * FROM shared.job WHERE id = $1::uuid", job_id)
    ctx = JobContext(pool, queue._to_job(row))
    archiver = S3Archiver(s3, job_id) if s3 is not None else None
    result = await retention_job.run_spec(ctx, SPECS[kind], archiver=archiver)
    await pool.execute("UPDATE shared.job SET status = 'succeeded', finished_at = now(), "
                       "result = $2::jsonb WHERE id = $1::uuid", job_id, json.dumps(result, default=str))
    return result


async def _tenant(pool) -> str:
    tid = str(uuid.uuid4())
    await pool.execute("""
        INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active, posts_per_week)
        VALUES ($1::uuid, 'AA-825 test', $2, 'starter', 60, true, 3)""", tid, f"aa825-{tid[:8]}")
    return tid


# ── llm_call_log ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_call_log_dry_run_then_live_rolls_up_archives_and_deletes(pool):
    stage = f"adhoc_aa825_{uuid.uuid4().hex[:8]}"
    old = "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' - interval '200 days' + interval '2 hours'"
    await pool.execute(f"""
        INSERT INTO shared.llm_call_log (stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal,
                                         provider, account, fallback_used, created_at)
        VALUES ($1, 'writer', 'm1', 100, 10, 0.5, '{{"app": "tripplanner"}}'::jsonb, 'bedrock', 'acc3', false, {old}),
               ($1, 'writer', 'm1', 50, 5, 0.25, '{{"app": "tripplanner"}}'::jsonb, 'bedrock', 'acc3', true,
                {old} + interval '1 hour'),
               ($1, 'writer', 'm1', 1, 1, 1.0, '{{"shadow": true}}'::jsonb, 'bedrock', 'acc3', false, {old}),
               ($1, 'writer', 'm1', 7, 7, 9.0, '{{}}'::jsonb, 'bedrock', 'acc3', false, now() - interval '179 days')
    """, stage)

    dry = await _run(pool, "retention_llm_call_log", {})
    assert dry["mode"] == "dry_run" and dry["candidates_total"] >= 3
    assert dry["rollup_pending"] >= 1
    assert await pool.fetchval("SELECT count(*) FROM shared.llm_call_log WHERE stage = $1", stage) == 4
    assert await pool.fetchval("SELECT count(*) FROM shared.llm_call_daily WHERE stage = $1", stage) == 0

    s3 = FakeS3()
    live = await _run(pool, "retention_llm_call_log",
                      {"trigger": "manual", "dry_run": False, "max_rows": 100000, "pause_ms": 0}, s3)
    assert live["mode"] == "live" and live["deleted_total"] == live["candidates_total"]
    assert live["archived_total"] == live["deleted_total"]
    # only the 179-day row survives
    assert await pool.fetchval("SELECT count(*) FROM shared.llm_call_log WHERE stage = $1", stage) == 1
    # rollup written before the delete, grouped by app and shadow flag
    rows = {(r["app"], r["is_shadow"]): r for r in await pool.fetch(
        "SELECT * FROM shared.llm_call_daily WHERE stage = $1", stage)}
    tp = rows[("tripplanner", False)]
    assert (tp["calls"], tp["fallback_calls"], tp["tokens_in"], tp["tokens_out"]) == (2, 1, 150, 15)
    assert float(tp["cost_usd"]) == 0.75 and tp["provider"] == "bedrock" and tp["account"] == "acc3"
    assert rows[("cis", True)]["calls"] == 1
    # archive: every deleted row is in S3, one object per month, under archive/llm_call_log/yyyy/mm/
    archived = [x for x in s3.lines() if x["stage"] == stage]
    assert len(archived) == 3 and len(s3.lines()) == live["deleted_total"]
    assert all(k.startswith("archive/llm_call_log/") and k.endswith(".jsonl.gz") for k in s3.objects)

    # the next scheduled run is live (an approved live run with the same windows exists)
    nxt = await _run(pool, "retention_llm_call_log", {"trigger": "schedule", "pause_ms": 0}, FakeS3())
    assert nxt["mode"] == "live" and nxt["deleted_total"] == 0
    # and the rollup is not written twice for the same day
    assert await pool.fetchval("SELECT sum(calls) FROM shared.llm_call_daily WHERE stage = $1", stage) == 3


@pytest.mark.asyncio
async def test_first_scheduled_run_is_a_dry_run(pool):
    await pool.execute("DELETE FROM shared.job WHERE kind = 'retention_decision_log'")
    res = await _run(pool, "retention_decision_log", {"trigger": "schedule"})
    assert res["mode"] == "dry_run" and "first run" in res["reason"]


@pytest.mark.asyncio
async def test_scheduled_run_is_held_when_candidates_exceed_twice_the_last_run(pool, monkeypatch):
    await pool.execute("DELETE FROM shared.job WHERE kind = 'retention_cost_explorer_snapshot'")
    acct = f"aa825-{uuid.uuid4().hex[:6]}"
    # an approved live run that deleted 0 rows
    await _run(pool, "retention_cost_explorer_snapshot", {"trigger": "manual", "dry_run": False, "max_rows": 1})
    await pool.execute("""
        INSERT INTO shared.cost_explorer_snapshot (account_id, service, period_start, period_end, amount_usd,
                                                   fetched_at)
        VALUES ($1, 'S', '2026-01-01', '2026-01-02', 1, now() - interval '5 days'),
               ($1, 'S', '2026-01-01', '2026-01-02', 2, now() - interval '4 days'),
               ($1, 'S', '2026-01-01', '2026-01-02', 3, now() - interval '3 days')""", acct)
    monkeypatch.setattr(retention_job, "GROWTH_FLOOR_ROWS", 1)
    held = await _run(pool, "retention_cost_explorer_snapshot", {"trigger": "schedule", "pause_ms": 0})
    assert held["mode"] == "held" and held["deleted_total"] == 0
    assert await pool.fetchval("SELECT count(*) FROM shared.cost_explorer_snapshot WHERE account_id = $1", acct) == 3
    assert await pool.fetchval(
        "SELECT count(*) FROM shared.notifications WHERE event_type = $1 AND entity_id = $2",
        retention_job.HELD_EVENT, "retention_cost_explorer_snapshot") >= 1
    await pool.execute("DELETE FROM shared.notifications WHERE event_type = $1", retention_job.HELD_EVENT)
    await pool.execute("DELETE FROM shared.cost_explorer_snapshot WHERE account_id = $1", acct)


# ── cost_explorer_snapshot: latest fetch per (account, service, day) kept ───────────────────────

@pytest.mark.asyncio
async def test_cost_explorer_keeps_the_latest_fetch_per_day(pool):
    acct = f"aa825-{uuid.uuid4().hex[:6]}"
    await pool.execute("""
        INSERT INTO shared.cost_explorer_snapshot (account_id, service, period_start, period_end, amount_usd,
                                                   fetched_at)
        VALUES ($1, 'S', '2026-01-01', '2026-01-02', 1, now() - interval '5 days'),
               ($1, 'S', '2026-01-01', '2026-01-02', 2, now() - interval '4 days'),
               ($1, 'S', '2026-01-02', '2026-01-03', 7, now() - interval '5 days'),
               ($1, 'S', '2026-01-01', '2026-01-02', 3, now() - interval '3 days')""", acct)
    reader = """SELECT DISTINCT ON (account_id, service, period_start) period_start, amount_usd
                  FROM shared.cost_explorer_snapshot WHERE account_id = $1
                 ORDER BY account_id, service, period_start, fetched_at DESC"""
    before = [tuple(r) for r in await pool.fetch(reader, acct)]
    res = await _run(pool, "retention_cost_explorer_snapshot",
                     {"trigger": "manual", "dry_run": False, "max_rows": 1000, "pause_ms": 0})
    assert res["mode"] == "live" and res["archived_total"] == 0
    left = await pool.fetch("SELECT amount_usd FROM shared.cost_explorer_snapshot WHERE account_id = $1 "
                            "ORDER BY amount_usd", acct)
    assert [float(r["amount_usd"]) for r in left] == [3.0, 7.0]
    assert [tuple(r) for r in await pool.fetch(reader, acct)] == before
    await pool.execute("DELETE FROM shared.cost_explorer_snapshot WHERE account_id = $1", acct)


# ── shared.job ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_job_retention_windows_archive_failed_only_and_keeps_referenced_rows(pool):
    tag = f"aa825-{uuid.uuid4().hex[:6]}"

    async def job(status, days_ago, parent=None):
        return str(await pool.fetchval("""
            INSERT INTO shared.job (kind, payload, status, created_by, parent_job_id, finished_at)
            VALUES ('aa825_test', '{}'::jsonb, $1, $2, $3::uuid,
                    CASE WHEN $1 IN ('queued', 'running') THEN NULL ELSE now() - make_interval(days => $4) END)
            RETURNING id""", status, tag, parent, days_ago))

    ok_old = await job("succeeded", 31)
    ok_new = await job("succeeded", 29)
    failed_old = await job("failed", 91)
    failed_mid = await job("failed", 60)
    cancelled_old = await job("cancelled", 100)
    parent = await job("succeeded", 40)
    await job("succeeded", 1, parent=parent)          # a child keeps its parent (FK parent_job_id)
    running = await job("running", 0)

    s3 = FakeS3()
    res = await _run(pool, "retention_job", {"trigger": "manual", "dry_run": False, "max_rows": 100000,
                                             "pause_ms": 0}, s3)
    assert "shared.job.parent_job_id" in res["fk_protected_by"]
    left = {str(r["id"]) for r in await pool.fetch("SELECT id FROM shared.job WHERE created_by = $1", tag)}
    assert ok_old not in left and failed_old not in left and cancelled_old not in left
    assert {ok_new, failed_mid, parent, running} <= left
    archived_ids = {x["id"] for x in s3.lines()}
    assert {failed_old, cancelled_old} <= archived_ids and ok_old not in archived_ids
    parts = {p["part"]: p for p in res["parts"]}
    assert parts["succeeded"]["archive"] is False and parts["failed"]["archive"] is True
    await pool.execute("DELETE FROM shared.job WHERE created_by = $1 AND parent_job_id IS NOT NULL", tag)
    await pool.execute("DELETE FROM shared.job WHERE created_by = $1", tag)


# ── decision_log ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_decision_log_keeps_labelled_rows_and_rows_inside_the_cache_window(pool):
    key = f"aa825_{uuid.uuid4().hex[:6]}"
    await pool.execute("""
        INSERT INTO shared.decision_log (stage, question_key, subject_key, mode, zone, outcome, created_at)
        VALUES ('t', $1, 'old', 'shadow', 'accept', NULL, now() - interval '200 days'),
               ('t', $1, 'labelled', 'shadow', 'accept', '{"truth": true}', now() - interval '200 days'),
               ('t', $1, 'recent', 'shadow', 'accept', NULL, now() - interval '170 days')""", key)
    s3 = FakeS3()
    await _run(pool, "retention_decision_log", {"trigger": "manual", "dry_run": False, "max_rows": 100000,
                                                "pause_ms": 0}, s3)
    left = {r["subject_key"] for r in await pool.fetch(
        "SELECT subject_key FROM shared.decision_log WHERE question_key = $1", key)}
    assert left == {"labelled", "recent"}
    assert [x["subject_key"] for x in s3.lines() if x["question_key"] == key] == ["old"]
    await pool.execute("DELETE FROM shared.decision_log WHERE question_key = $1", key)


# ── tenant_api_usage + the admin usage reader ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_usage_monthly_rollup_keeps_the_admin_usage_numbers(pool):
    from api.routers.admin import TENANT_USAGE_MONTHLY_SQL
    tid = await _tenant(pool)
    # a whole month ~4 months back (part of it older than 90 days), plus this month
    month = "date_trunc('month', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' - interval '4 months'"
    await pool.execute(f"""
        INSERT INTO shared.tenant_api_usage (tenant_id, endpoint, method, status_code, response_ms,
                                             called_at, actor_type)
        VALUES ($1::uuid, '/v1/tours', 'GET', 200, 10, {month} + interval '1 day', 'tenant'),
               ($1::uuid, '/v1/tours', 'GET', 429, NULL, {month} + interval '2 days', 'tenant'),
               ($1::uuid, '/v1/tours', 'GET', 500, 30, {month} + interval '20 days', 'tenant'),
               ($1::uuid, '/v1/tours', 'GET', 200, 5, now(), 'tenant')""", tid)
    before = [dict(r) for r in await pool.fetch(TENANT_USAGE_MONTHLY_SQL, uuid.UUID(tid), 6)]

    s3 = FakeS3()
    res = await _run(pool, "retention_tenant_api_usage",
                     {"trigger": "manual", "dry_run": False, "max_rows": 100000, "pause_ms": 0}, s3)
    assert res["rollup_rows"] >= 1
    m = await pool.fetchrow("SELECT * FROM shared.tenant_api_usage_monthly WHERE tenant_id = $1::uuid", tid)
    assert (m["calls"], m["ok_calls"], m["rate_limited_calls"], m["server_error_calls"]) == (3, 1, 1, 1)
    assert (m["response_ms_sum"], m["response_ms_count"]) == (40, 2)
    # this month's row is never touched; older raw rows are gone (archived) or still inside 90 days
    assert await pool.fetchval("SELECT count(*) FROM shared.tenant_api_usage WHERE tenant_id = $1::uuid "
                               "AND called_at >= date_trunc('month', now())", tid) == 1
    assert len([x for x in s3.lines() if x["tenant_id"] == tid]) >= 1
    after = [dict(r) for r in await pool.fetch(TENANT_USAGE_MONTHLY_SQL, uuid.UUID(tid), 6)]
    assert after == before
    await pool.execute("DELETE FROM shared.tenant_api_usage_monthly WHERE tenant_id = $1::uuid", tid)
    await pool.execute("DELETE FROM shared.tenant_api_usage WHERE tenant_id = $1::uuid", tid)
    await pool.execute("DELETE FROM shared.tenants WHERE tenant_id = $1::uuid", tid)


# ── scheduling ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_schedule_all_is_idempotent_per_day_and_skips_while_a_wave_is_open(pool):
    day = f"2099-01-01-{uuid.uuid4().hex[:6]}"
    first = await retention_job.schedule_all(pool, day=day)
    again = await retention_job.schedule_all(pool, day=day)
    assert {e["kind"] for e in first["enqueued"]} == set(SPECS)
    assert all(e["created"] for e in first["enqueued"]) and not any(e["created"] for e in again["enqueued"])
    payloads = await pool.fetch("SELECT payload FROM shared.job WHERE idempotency_key LIKE $1", f"retention:%:{day}")
    assert all(json.loads(p["payload"])["trigger"] == "schedule" for p in payloads)

    wave = await pool.fetchval("""
        INSERT INTO shared.rerun_wave (name, expected_tour_ids, status)
        VALUES ('aa825', ARRAY[gen_random_uuid()], 'open')
        RETURNING id""")
    try:
        skipped = await retention_job.schedule_all(pool, day=day + "-w")
        assert skipped["enqueued"] == [] and "wave" in skipped["skipped"]
    finally:
        await pool.execute("DELETE FROM shared.rerun_wave WHERE id = $1", wave)


@pytest.mark.asyncio
async def test_status_lists_every_kind(pool):
    st = await retention_job.status(pool)
    assert {k["kind"] for k in st["kinds"]} == set(SPECS)
    assert all(k["scheduled_mode"] in ("live", "dry_run") for k in st["kinds"])


@pytest.mark.asyncio
async def test_small_batches_walk_the_keyset_and_resume_from_the_stored_cursor(pool):
    stage = f"adhoc_aa825_{uuid.uuid4().hex[:8]}"
    await pool.execute("""
        INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, created_at)
        SELECT $1, 'writer', 'm', '{}'::jsonb, now() - interval '300 days' + make_interval(mins => g)
          FROM generate_series(1, 5) g""", stage)
    s3 = FakeS3()
    first = await _run(pool, "retention_llm_call_log", {"trigger": "manual", "dry_run": False, "max_rows": 2,
                                                         "batch_size": 1, "pause_ms": 0}, s3)
    part = first["parts"][0]
    assert first["deleted_total"] == 2 and part["batches"] == 2 and part["objects"] == 2
    assert part["cursor"]["ts"] and len(s3.objects) == 2
    # a retry of a job that already deleted 2 rows continues after its cursor and counts them
    job_id, _ = await queue.enqueue(pool, "retention_llm_call_log",
                                    {"trigger": "manual", "dry_run": False, "max_rows": 4, "batch_size": 2,
                                     "pause_ms": 0})
    await queue.set_progress(pool, job_id, {"deleted_total": 2, "cursors": {"raw": part["cursor"]}})
    row = await pool.fetchrow("SELECT * FROM shared.job WHERE id = $1::uuid", job_id)
    res = await retention_job.run_spec(JobContext(pool, queue._to_job(row)), SPECS["retention_llm_call_log"],
                                       archiver=S3Archiver(s3, job_id))
    assert res["resumed_after"] == 2 and res["deleted_total"] == 4
    assert await pool.fetchval("SELECT count(*) FROM shared.llm_call_log WHERE stage = $1", stage) == 1
