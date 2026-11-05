"""AA-1042 (S268, lane trace) — on real Postgres: the llm_route_policy migration (shared.llm_route_policy) and
cis_app's grant on it, the route trace reads (requests, one trace, rungs) and the route watch stats over llm_call_log
rows tagged with quality_signal.route_trace (a skipped primary included), the policy write / reset with their audit
rows, and the fallback content list queries (catalog, tenant).

Invented stage names only (zz1042_*); every row sits in 2099 so the rows of other tests never count.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from _audit_cleanup import delete_audit_rows  # noqa: E402  (audit_log is append-only, CIS 248)
from api.services import llm_route_trace as svc  # noqa: E402
from shared.llm_client import route_watch as rw  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
NOW = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)
STAGE = "zz1042_writer"
OTHER = "zz1042_judge"
# Found by name, so a renumber at merge time (ADR 0017 d1: main max + 1) touches only the file and its body.
MIGRATIONS = Path(__file__).resolve().parents[2] / "api" / "migrations"
(MIGRATION,) = MIGRATIONS.glob("[0-9][0-9][0-9]_llm_route_policy.sql")
VERSION = MIGRATION.name.split("_", 1)[0]


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    async with p.acquire() as c:
        await c.execute(MIGRATION.read_text())        # re-runnable: CREATE IF NOT EXISTS, ON CONFLICT
        await _clean(c)
        await c.execute(
            "INSERT INTO shared.llm_role_config (stage, role, provider, model_id, is_active, updated_at, updated_by, "
            "fallback_model_ids, shadow_sample_pct) VALUES ($1, 'writer', 'claude', 'zz-model-one', true, now(), "
            "'test', ARRAY['zz-model-two', 'zz-model-three'], 0), ($2, 'judge', 'claude', 'zz-model-one', true, "
            "now(), 'test', ARRAY[]::text[], 0)", STAGE, OTHER)
    rw.reset()
    yield p
    async with p.acquire() as c:
        await _clean(c)
        await delete_audit_rows(c, "resource_type = 'llm_route_policy' AND resource_id = ANY($1::text[])",
                                [STAGE, OTHER])
    rw.reset()
    await p.close()


async def _clean(c):
    await c.execute("DELETE FROM shared.llm_call_log WHERE stage = ANY($1::text[])", [STAGE, OTHER])
    await c.execute("DELETE FROM shared.llm_route_policy WHERE stage = ANY($1::text[])", [STAGE, OTHER])
    await c.execute("DELETE FROM shared.llm_role_config WHERE stage = ANY($1::text[])", [STAGE, OTHER])


async def _row(c, *, stage=STAGE, trace=None, model="zz-model-one", status="ok", attempt=1, position=0,
               cost=0.01, at=NOW - timedelta(minutes=10), error_class=None, shadow=False, connection="acc3",
               fallback_used=None):
    signal = {}
    if trace:
        signal["route_trace"] = trace
        signal["route_position" if status == "ok" else "position"] = position
    if status != "ok":
        signal.update(gateway_attempt=True, error=f"{model} refused")
    await c.execute(
        "INSERT INTO shared.llm_call_log (stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal, "
        "created_at, status, error_class, attempt_no, latency_ms, connection_key, provider, is_shadow, app, "
        "fallback_used) "
        "VALUES ($1, 'writer', $2, 10, 5, $3, $4::jsonb, $5, $6, $7, $8, 120, $9, 'bedrock-satellite', $10, 'cis', "
        "$11)",
        stage, model, cost if status == "ok" else 0.0, json.dumps(signal), at, status, error_class, attempt,
        connection, shadow, fallback_used)


async def _scene(c):
    """Three requests: one primary answer, one answered by the 3rd model after two refusals, one with no answer."""
    t1, t2, t3 = (str(uuid.uuid4()) for _ in range(3))
    await _row(c, trace=t1, at=NOW - timedelta(minutes=50))
    await _row(c, trace=t2, status="refused", error_class="access_denied", attempt=1, position=0,
               at=NOW - timedelta(minutes=20))
    await _row(c, trace=t2, model="zz-model-two", status="error", error_class="other", attempt=2, position=1,
               at=NOW - timedelta(minutes=20))
    await _row(c, trace=t2, model="zz-model-three", attempt=3, position=2, cost=0.05, connection="api",
               at=NOW - timedelta(minutes=19))
    await _row(c, trace=t3, status="refused", error_class="access_denied", attempt=1, position=0,
               at=NOW - timedelta(minutes=5))
    await _row(c, trace=str(uuid.uuid4()), shadow=True)                       # a shadow row never counts
    return t1, t2, t3


@pytest.mark.asyncio
async def test_migration_shape_and_cis_app_grant(pool):
    async with pool.acquire() as c:
        cols = {r["column_name"] for r in await c.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'shared' "
            "AND table_name = 'llm_route_policy'")}
        assert {"stage", "fallback_alert_pct", "min_requests", "alert_last_rung", "cost_shift_usd",
                "hold_fallback_writes", "notes", "updated_at", "updated_by"} <= cols
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await c.execute("INSERT INTO shared.llm_route_policy (stage, fallback_alert_pct) VALUES ($1, 0)", STAGE)
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await c.execute("INSERT INTO shared.llm_route_policy (stage) VALUES ('Bad Stage')")
        if await c.fetchval("SELECT count(*) AS n FROM pg_roles WHERE rolname = 'cis_app'"):
            ok = await c.fetchval("SELECT has_table_privilege('cis_app', 'shared.llm_route_policy', "
                                  "'SELECT, INSERT, UPDATE, DELETE') AS ok")
            assert ok is True
        assert await c.fetchval("SELECT count(*) AS n FROM shared.schema_versions WHERE version = $1", VERSION) == 1


@pytest.mark.asyncio
async def test_requests_trace_and_rungs_read_the_tagged_rows(pool):
    async with pool.acquire() as c:
        t1, t2, t3 = await _scene(c)
    out = await svc.read_requests(pool, stage=STAGE, window="1h", now=NOW)
    by = {r["trace_id"]: r for r in out["requests"]}
    assert set(by) == {t1, t2, t3}
    assert by[t1]["outcome"] == "primary" and by[t2]["outcome"] == "fallback" and by[t3]["outcome"] == "no_answer"
    assert (by[t2]["attempts"], by[t2]["failed_attempts"], by[t2]["served_model"], by[t2]["served_connection"],
            by[t2]["max_position"]) == (3, 2, "zz-model-three", "api", 2)
    only = await svc.read_requests(pool, stage=STAGE, window="1h", fallback_only=True, now=NOW)
    assert {r["trace_id"] for r in only["requests"]} == {t2, t3}

    tr = await svc.read_trace(pool, t2, at=NOW - timedelta(minutes=20), now=NOW)
    assert [a["attempt_no"] for a in tr["attempts"]] == [1, 2, 3]
    assert [a["status"] for a in tr["attempts"]] == ["refused", "error", "ok"]
    assert tr["attempts"][0]["error_class"] == "access_denied" and tr["attempts"][0]["position"] == 0
    assert tr["served"]["model"] == "zz-model-three" and tr["route"] == ["zz-model-one", "zz-model-two",
                                                                          "zz-model-three"]
    assert await svc.read_trace(pool, t2, at=NOW - timedelta(hours=3), now=NOW) is None   # outside +-15 min

    rungs = await svc.read_rungs(pool, window="1h", stage=STAGE, now=NOW)
    (s,) = rungs["stages"]
    assert (s["answered"], s["fallback_answered"], s["fallback_pct"], s["lost_requests"]) == (2, 1, 50.0, 1)
    assert [(r["rung"], r["model"], r["share_pct"]) for r in s["rungs"]] == [
        ("primary", "zz-model-one", 50.0), ("fallback", "zz-model-three", 50.0)]


@pytest.mark.asyncio
async def test_the_watch_raises_fallback_and_last_model_then_primary_back(pool):
    async with pool.acquire() as c:
        await _scene(c)
        stats = await rw.read_stats(c, NOW - rw.WATCH_WINDOW)
        routes = await rw.read_routes(c)
    rows = {STAGE: {"min_requests": 2}}
    found = rw.watch_findings({STAGE: stats[STAGE]}, {}, {}, routes, rows, {})
    assert sorted(f.check for f in found) == [f"route_fb.{STAGE}", f"route_last.{STAGE}"]
    # after a fallback bell 30 min ago, a primary answer newer than every fallback answer is "primary back"
    async with pool.acquire() as c:
        await _row(c, trace=str(uuid.uuid4()), at=NOW - timedelta(minutes=1))
        stats = await rw.read_stats(c, NOW - rw.WATCH_WINDOW)
    state = {f"route_fb.{STAGE}": NOW - timedelta(minutes=30)}
    back = rw.watch_findings({STAGE: stats[STAGE]}, {}, {}, routes, rows, state)
    assert f"route_back.{STAGE}" in {f.check for f in back}


async def _circuit_open_scene(c, n=3, start=NOW - timedelta(minutes=30)):
    """The primary's circuit is open: each request has the gateway's net row for the first model (refused, NO attempt
    number, position 0: no provider call; the class of the refusals that opened the circuit) and the 2nd model's
    answer with attempt_no 1 at route position 1."""
    traces = []
    for i in range(n):
        t = str(uuid.uuid4())
        at = start + timedelta(minutes=i)
        await _row(c, trace=t, status="refused", error_class="access_denied", attempt=None, position=0, at=at)
        await _row(c, trace=t, model="zz-model-two", attempt=1, position=1, fallback_used=True, cost=0.02,
                   at=at + timedelta(seconds=2))
        traces.append(t)
    return traces


@pytest.mark.asyncio
async def test_a_skipped_primary_counts_as_fallback_everywhere(pool):
    # review fix: attempt_no 1 is not the primary when the earlier models were skipped without an attempt
    async with pool.acquire() as c:
        traces = await _circuit_open_scene(c)
        stats = await rw.read_stats(c, NOW - rw.WATCH_WINDOW)
        base = await rw.read_baseline(c, NOW - rw.BASELINE_WINDOW)
        routes = await rw.read_routes(c)
    st = stats[STAGE]
    assert (st["answered"], st["primary_answered"], st["fallback_answered"], st["unknown_answered"]) == (3, 0, 3, 0)
    assert st["last_primary_at"] is None and st["fallback_cost_usd"] == pytest.approx(0.06)
    assert STAGE not in base                                                  # no primary answer to average
    # degraded since a fallback bell 40 min ago: the fallback answers do NOT raise "primary back"
    state = {f"route_fb.{STAGE}": NOW - timedelta(minutes=40)}
    checks = {f.check for f in rw.watch_findings(stats, {}, {}, routes, {STAGE: {"min_requests": 2}}, state)}
    assert f"route_back.{STAGE}" not in checks and f"route_fb.{STAGE}" in checks

    out = await svc.read_requests(pool, stage=STAGE, window="1h", now=NOW)
    assert {r["trace_id"]: r["outcome"] for r in out["requests"]} == {t: "fallback" for t in traces}
    only = await svc.read_requests(pool, stage=STAGE, window="1h", fallback_only=True, now=NOW)
    assert {r["trace_id"] for r in only["requests"]} == set(traces)
    tr = await svc.read_trace(pool, traces[0], at=NOW - timedelta(minutes=30), now=NOW)
    assert tr["outcome"] == "fallback" and [a["status"] for a in tr["attempts"]] == ["refused", "ok"]
    rungs = await svc.read_rungs(pool, window="1h", stage=STAGE, now=NOW)
    (s,) = rungs["stages"]
    assert [(r["rung"], r["model"], r["share_pct"]) for r in s["rungs"]] == [("fallback", "zz-model-two", 100.0)]
    assert s["fallback_pct"] == 100.0


@pytest.mark.asyncio
async def test_the_cost_baseline_reads_primary_answers_only(pool):
    async with pool.acquire() as c:
        await _scene(c)
        base = await rw.read_baseline(c, NOW - rw.BASELINE_WINDOW)
    assert base[STAGE]["n"] == 1 and base[STAGE]["avg_cost_usd"] == pytest.approx(0.01)


@pytest.mark.asyncio
async def test_policy_write_and_reset_are_audited_and_change_the_effective_values(pool):
    out = await svc.write_policy(pool, STAGE, {"fallback_alert_pct": 40, "hold_fallback_writes": True},
                                 actor="admin:zz1042")
    assert out["fallback_alert_pct"] == 40 and out["hold_fallback_writes"] is True
    view = await svc.read_policy(pool)
    assert view["ready"] is True
    st = {s["stage"]: s for s in view["stages"]}[STAGE]
    assert st["effective"]["fallback_alert_pct"] == 40 and st["effective"]["min_requests"] == 5
    async with pool.acquire() as c:
        ready, rows = await rw.read_policies(c)
    assert ready and rw.effective(rows, STAGE).hold_fallback_writes is True
    assert (await svc.clear_policy(pool, STAGE, actor="admin:zz1042"))["cleared"] is True
    with pytest.raises(LookupError):
        await svc.clear_policy(pool, STAGE, actor="admin:zz1042")
    async with pool.acquire() as c:
        actions = [r["action"] for r in await c.fetch(
            "SELECT action FROM acp_shared.audit_log WHERE resource_type = 'llm_route_policy' AND resource_id = $1 "
            "ORDER BY created_at", STAGE)]
    assert actions == ["llm_route_policy_set", "llm_route_policy_cleared"]
    with pytest.raises(ValueError):
        await svc.write_policy(pool, STAGE, {"min_requests": 0}, actor="admin:zz1042")


@pytest.mark.asyncio
async def test_the_fallback_content_queries_run(pool):
    out = await svc.read_fallback_content(pool, window="7d", now=NOW)
    assert out == {"window": "7d", "tours": []}
    tenant = await svc.read_fallback_tenant_content(pool, window="7d", now=NOW)
    assert tenant == {"window": "7d", "versions": [], "pieces": []}


@pytest.mark.asyncio
async def test_deferred_rungs_live_in_the_answered_row_and_show_as_skipped_attempts(pool):
    """AA-1042 (S270): the rungs the gateway deferred behind an open refusal circuit have no row of their own; the
    answered row lists them in quality_signal.route_skips. The request is still a fallback answer everywhere, the
    request list counts them, and the trace shows them as skipped attempts in route order."""
    trace = str(uuid.uuid4())
    skips = [{"model": "zz-model-one", "connection": "acc3", "reason": "circuit_open:access_denied", "position": 0},
             {"model": "zz-model-two", "connection": "acc3", "reason": "circuit_open:access_denied", "position": 1}]
    at = NOW - timedelta(minutes=12)
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO shared.llm_call_log (stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal, "
            "created_at, status, attempt_no, latency_ms, connection_key, provider, app, fallback_used) "
            "VALUES ($1, 'writer', 'zz-model-three', 10, 5, 0.02, $2::jsonb, $3, 'ok', 1, 300, 'api', 'openai', "
            "'cis', true)",
            STAGE, json.dumps({"route_trace": trace, "route_position": 2, "route_skips": skips}), at)
        stats = await rw.read_stats(c, NOW - rw.WATCH_WINDOW)
    st = stats[STAGE]
    assert (st["answered"], st["fallback_answered"], st["failed_attempts"], st["max_position"]) == (1, 1, 0, 2)
    out = await svc.read_requests(pool, stage=STAGE, window="1h", fallback_only=True, now=NOW)
    (r,) = out["requests"]
    assert (r["trace_id"], r["outcome"], r["failed_attempts"], r["skipped_attempts"]) == (trace, "fallback", 0, 2)
    tr = await svc.read_trace(pool, trace, at=at, now=NOW)
    assert [(a["model"], a["status"], a["position"]) for a in tr["attempts"]] == \
        [("zz-model-one", "skipped", 0), ("zz-model-two", "skipped", 1), ("zz-model-three", "ok", 2)]
    assert tr["outcome"] == "fallback" and tr["served"]["model"] == "zz-model-three"
    assert tr["cost_usd"] == pytest.approx(0.02) and tr["attempts"][0]["error_class"] == "access_denied"
