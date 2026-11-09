"""AA-804 (UI parts 1 to 3) — the AI providers page SQL against real Postgres (migrations applied by conftest).

- trace_job: one job's rows from llm_call_log, dfs_call_log, geo.geocode_call and decision_log in time order; the
  geocoder ledger (no job index) is read only inside the job's run window, decision_log by its job index (319);
  another job's rows never show.
- trace_tour (UI part 3): a tour's rows through its jobs' payload, the tour_id columns and the content pieces its jobs
  wrote; list_calls: every ledger's page SQL (filters, keyset) runs on the real schema, a refused attempt is a row.
- read_providers: every source query runs on the real schema (no error row), the month ledger attributes a row to
  its account, and Jev / DataForSEO / Geoapify rows are always listed.
- AA-1025 C1: the 24 h / 7 d stats queries (calls, errors, refused, p50 / p95) on the real schema.
- read_budgets (UI part 2): the spend_budget read, the guard's day spend and the connection budget reads run on the
  real schema; a probe job-scope row lands in the job tier with its kind.

The fixture rows live in July 2024 (no other test writes there) and are deleted at the end. Names are invented.
"""
import json
import os
import sys
from datetime import datetime, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import ai_gateway as gw  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
KIND = "aa804_probe"
STAGE = "aa804_probe_stage"
EP = "aa804_probe_endpoint"
ACCOUNT = "aa804-probe-conn"
GEO = "aa804_probe_geo"


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    # test_aa712_migration_226 (sorted before this file) drops schema geo and re-applies 226 only: put back the
    # geocode_call columns of 255 (endpoint, credits) and 319 (tenant_id, failed_requests), as test_aa804_ledger_v2_db
    # does. Both files are idempotent.
    migrations = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations")
    async with p.acquire() as c:
        for name in ("255_geo_place_shape.sql", "319_llm_ledger_v2.sql"):
            with open(os.path.join(migrations, name), encoding="utf-8") as f:
                await c.execute(f.read())
    yield p
    async with p.acquire() as c:
        await c.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)
        await c.execute("DELETE FROM shared.dfs_call_log WHERE endpoint = $1", EP)
        await c.execute("DELETE FROM geo.geocode_call WHERE provider = $1", GEO)
        await c.execute("DELETE FROM shared.decision_log WHERE stage = $1", STAGE)
        await c.execute("DELETE FROM shared.job WHERE kind = $1", KIND)
    await p.close()


async def _job(c, started, finished, tour_id=None):
    return await c.fetchval(
        """INSERT INTO shared.job (kind, payload, status, created_by, started_at, finished_at)
           VALUES ($1, $2::jsonb, 'succeeded', 'admin:probe', $3, $4) RETURNING id::text""",
        KIND, json.dumps({"tour_id": tour_id}), started, finished)


async def _llm(c, at, cost, job_id, provider="bedrock-satellite", account=ACCOUNT, signal="{}", status=None,
               tour_id=None, piece_id=None, error_class=None, app=None):
    await c.execute(
        """INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, provider, account, job_id,
                                            created_at, tokens_in, tokens_out, status, tour_id, content_piece_id,
                                            error_class, latency_ms, app)
           VALUES ($1, 'writer', 'probe-model', $2, $3::jsonb, $4, $5, $6::uuid, $7, 10, 20, $8, $9::uuid,
                   $10::uuid, $11, 25, $12)""",
        STAGE, cost, signal, provider, account, job_id, at, status, tour_id, piece_id, error_class, app)


async def _dfs(c, at, cost, job_id, tour_id=None, status=None):
    await c.execute(
        """INSERT INTO shared.dfs_call_log (endpoint, cost_usd, cache_hit, fetched_live, keyword_count, job_id,
                                            created_at, tour_id, status, latency_ms)
           VALUES ($1, $2, false, true, 3, $3::uuid, $4, $5::uuid, $6, 700)""", EP, cost, job_id, at, tour_id,
        status)


async def _geo(c, at, job_id, credits=2):
    await c.execute(
        """INSERT INTO geo.geocode_call (called_at, provider, requests, cost_usd, job_id, endpoint, credits)
           VALUES ($1, $2, 1, 0, $3::uuid, 'search', $4)""", at, GEO, job_id, credits)


async def _decision(c, at, job_id, zone="accept", error=None, question="aa804_q", cost=0.001):
    await c.execute(
        """INSERT INTO shared.decision_log (created_at, stage, question_key, subject_key, job_id, mode, zone, model,
                                            latency_ms, cost_usd, error, cached)
           VALUES ($1, $2, $6, 'aa804_subject', $3::uuid, 'shadow', $4, 'probe-jev', 300, $7, $5, false)""",
        at, STAGE, job_id, zone, error, question, cost)


@pytest.mark.asyncio
async def test_trace_unions_the_four_ledgers_inside_the_run_window(pool):
    start, end = _utc(2024, 7, 10, 1), _utc(2024, 7, 10, 3)
    async with pool.acquire() as c:
        job = await _job(c, start, end)
        other = await _job(c, start, end)
        await _llm(c, _utc(2024, 7, 10, 1, 5), 0.10, job)
        await _llm(c, _utc(2024, 7, 10, 2, 0), 0.20, job, signal='{"shadow": true}')
        await _llm(c, _utc(2024, 7, 10, 1, 30), 9.00, other)
        await _dfs(c, _utc(2024, 7, 10, 1, 10), 0.05, job)
        await _geo(c, _utc(2024, 7, 10, 1, 20), job)
        await _geo(c, _utc(2024, 7, 12, 1, 20), job)                  # outside the run window: not read
        await _decision(c, _utc(2024, 7, 10, 1, 15), job, zone="error", error="probe failure")
        await _decision(c, _utc(2024, 7, 9, 23, 0), job)               # before the start: read by the job index
        await _decision(c, _utc(2024, 7, 10, 1, 16), other)

    out = await gw.trace_job(pool, job, now=_utc(2024, 7, 20))
    assert [e["source"] for e in out["events"]] == ["jev", "llm", "dfs", "jev", "geocoder", "llm"]
    assert out["events"][-1]["outcome"] == "shadow" and out["events"][3]["outcome"] == "error"
    assert out["sources"]["llm"]["events"] == 2 and out["sources"]["llm"]["cost_usd"] == 0.3
    assert out["sources"]["jev"]["events"] == 2 and out["sources"]["geocoder"]["events"] == 1
    assert out["totals"]["events"] == 6 and out["job"]["kind"] == KIND
    assert out["events"][1]["latency_ms"] == 25 and "rows_before_ledger_v2" in out["notes"]
    assert all(v["error"] is None for v in out["sources"].values())

    limited = await gw.trace_job(pool, job, limit=1, now=_utc(2024, 7, 20))
    assert limited["sources"]["llm"] == {"events": 2, "shown": 1, "cost_usd": 0.3, "truncated": True, "read": True,
                                         "error": None}

    assert await gw.trace_job(pool, "00000000-0000-4000-8000-000000000804") is None


@pytest.mark.asyncio
async def test_trace_counts_a_jev_call_once(pool):
    """decide() writes one ``typesafe`` llm_call_log row (whole cost) and one decision_log row per question (its
    share) for the same Jev call; the trace reads Jev from decision_log only."""
    start, end = _utc(2024, 7, 11, 1), _utc(2024, 7, 11, 2)
    async with pool.acquire() as c:
        job = await _job(c, start, end)
        await _llm(c, _utc(2024, 7, 11, 1, 5), 0.10, job)
        await _llm(c, _utc(2024, 7, 11, 1, 10), 0.006, job, provider="typesafe", account=None)
        await _decision(c, _utc(2024, 7, 11, 1, 10), job, question="aa804_q1", cost=0.003)
        await _decision(c, _utc(2024, 7, 11, 1, 10), job, question="aa804_q2", cost=0.003)

    out = await gw.trace_job(pool, job, now=_utc(2024, 7, 20))
    assert [e["source"] for e in out["events"]] == ["llm", "jev", "jev"]
    assert out["sources"]["llm"]["events"] == 1 and out["sources"]["llm"]["cost_usd"] == pytest.approx(0.1)
    assert out["sources"]["jev"]["events"] == 2 and out["sources"]["jev"]["cost_usd"] == pytest.approx(0.006)
    assert out["totals"]["events"] == 3 and out["totals"]["cost_usd"] == pytest.approx(0.106)


@pytest.mark.asyncio
async def test_tour_trace_reads_the_jobs_the_columns_and_the_pieces(pool):
    tour = "4d3c2b1a-0f9e-4d8c-8b7a-6f5e4d3c2b1a"
    start, end = _utc(2024, 7, 13, 1), _utc(2024, 7, 13, 2)
    async with pool.acquire() as c:
        job = await _job(c, start, end, tour_id=tour)
        other = await _job(c, start, end)
        tenant = await c.fetchval("SELECT tenant_id FROM shared.tenants ORDER BY created_at LIMIT 1")
        piece = None
        if tenant is not None:
            req = await c.fetchval(
                """INSERT INTO acp_shared.angle_gate_request (tenant_id, atom_id, status)
                   VALUES ($1, 'aa804_probe_atom', 'approved') RETURNING request_id""", tenant)
            piece = await c.fetchval(
                """INSERT INTO acp_shared.content_piece (tenant_id, angle_gate_request_id, attempt_number,
                                                         content_text, status, gate_ledger, repair_log, job_id)
                   VALUES ($1, $2, 1, 'invented text', 'approved', '{}'::jsonb, '[]'::jsonb, $3::uuid)
                   RETURNING piece_id::text""", tenant, req, job)
        await _llm(c, _utc(2024, 7, 13, 1, 5), 0.10, job, status="ok")
        await _llm(c, _utc(2024, 7, 13, 1, 6), 0.20, None, status="ok", tour_id=tour)
        if piece:
            await _llm(c, _utc(2024, 7, 13, 1, 7), 0.30, None, status="ok", piece_id=piece)
        await _llm(c, _utc(2024, 7, 13, 1, 8), 9.00, other, status="ok")          # another job: not read
        await _dfs(c, _utc(2024, 7, 13, 1, 9), 0.05, None, tour_id=tour, status="ok")
        await _decision(c, _utc(2024, 7, 13, 1, 10), job)
        await _geo(c, _utc(2024, 7, 13, 1, 11), job)
    try:
        out = await gw.trace_tour(pool, tour)
        assert out["kind"] == "tour" and [j["id"] for j in out["tour"]["jobs"]] == [job]
        assert all(v["error"] is None for v in out["sources"].values()), out["sources"]
        llm = [e for e in out["events"] if e["source"] == "llm"]
        assert [e["cost_usd"] for e in llm] == ([0.1, 0.2, 0.3] if piece else [0.1, 0.2])
        assert [e["source"] for e in out["events"] if e["source"] != "llm"] == ["dfs", "jev", "geocoder"]
        assert "rows_before_ledger_v2" not in out["notes"]
    finally:
        async with pool.acquire() as c:
            if piece:
                await c.execute("DELETE FROM shared.llm_call_log WHERE content_piece_id = $1::uuid", piece)
                await c.execute("DELETE FROM acp_shared.content_piece WHERE piece_id = $1::uuid", piece)
                await c.execute("DELETE FROM acp_shared.angle_gate_request WHERE atom_id = 'aa804_probe_atom'")


@pytest.mark.asyncio
async def test_calls_list_every_ledger_sql_runs_and_pages(pool):
    from api.services import ai_gateway_calls as calls

    now = _utc(2024, 7, 14, 12)
    async with pool.acquire() as c:
        job = await _job(c, _utc(2024, 7, 14, 1), _utc(2024, 7, 14, 2))
        await _llm(c, _utc(2024, 7, 14, 11, 0), 0.0, None, provider="anthropic", account=None, status="refused",
                   error_class="credential")
        await _llm(c, _utc(2024, 7, 14, 11, 1), 0.02, job, status="ok", app="tripplanner")
        await _llm(c, _utc(2024, 7, 14, 11, 2), 0.01, job, provider="typesafe", account=None, status="ok")
        await _dfs(c, _utc(2024, 7, 14, 11, 3), 0.05, job, status="ok")
        await _decision(c, _utc(2024, 7, 14, 11, 4), job, zone="error", error="probe failure")
        await _geo(c, _utc(2024, 7, 14, 11, 5), job)

    async def ours(f, **kw):
        out = await calls.list_calls(pool, f, now=now, **kw)
        assert out["errors"] == [], out["errors"]
        return [r for r in out["rows"] if r["stage"] in (STAGE, None) and
                (r["source"] != "dfs" or r["what"] == EP) and (r["source"] != "geocoder" or r["provider"] == GEO)], out

    rows, _ = await ours(calls.parse_filters(window="24h"))
    assert [r["source"] for r in rows] == ["geocoder", "jev", "dfs", "llm", "llm"]   # typesafe row left out
    assert rows[-1]["status"] == "refused" and rows[-1]["error_class"] == "credential"
    rows, _ = await ours(calls.parse_filters(status="refused", stage=STAGE))
    assert [(r["source"], r["status"]) for r in rows] == [("llm", "refused")]
    rows, _ = await ours(calls.parse_filters(status="error", job_id=job))
    assert [r["source"] for r in rows] == ["jev"]
    rows, _ = await ours(calls.parse_filters(app="tripplanner", stage=STAGE))
    assert [r["cost_usd"] for r in rows] == [0.02]
    rows, _ = await ours(calls.parse_filters(provider="anthropic", stage=STAGE))
    assert len(rows) == 1 and rows[0]["provider"] == "anthropic"
    rows, _ = await ours(calls.parse_filters(provider="geocoder", job_id=job))
    assert [r["source"] for r in rows] == ["geocoder"]

    # keyset: pages of one row walk the job's four rows without a gap or a repeat
    seen, cursor = [], None
    for _ in range(6):
        out = await calls.list_calls(pool, calls.parse_filters(job_id=job), cursor=cursor, limit=1, now=now)
        seen += [r["id"] for r in out["rows"]]
        cursor = out["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 4


@pytest.mark.asyncio
async def test_providers_queries_run_on_the_real_schema(pool):
    now = _utc(2024, 7, 20, 12)
    async with pool.acquire() as c:
        await _llm(c, _utc(2024, 7, 20, 9), 0.40, None, provider="openai")
        await _llm(c, _utc(2024, 7, 3, 9), 1.00, None, provider="openai")
        await _llm(c, _utc(2024, 6, 30, 23), 5.00, None, provider="openai")   # last month: not counted
        await _llm(c, _utc(2024, 7, 20, 8), 0.02, None, provider="typesafe", account=None)
        await _dfs(c, _utc(2024, 7, 20, 7), 0.30, None)
        await _geo(c, _utc(2024, 7, 20, 6), None, credits=5)

    day, month = gw.day_and_month_start(now)
    ledger = [dict(r) for r in await pool.fetch(gw.LEDGER_SQL, day, month, now)]
    mine = [r for r in ledger if r["account"] == ACCOUNT]
    assert len(mine) == 1 and mine[0]["month_usd"] == pytest.approx(1.4) and mine[0]["today_usd"] == pytest.approx(0.4)
    assert mine[0]["month_calls"] == 2
    geo = await pool.fetchrow(gw.GEO_SQL, day, month, now, GEO)
    assert geo["credits_today"] == 5 and geo["month_requests"] == 1

    out = await gw.read_providers(pool, now=now)
    assert out["errors"] == [], out["errors"]
    keys = [p["key"] for p in out["providers"]]
    assert keys[-6:] == ["crm:llm", "jev", "dataforseo", "geoapify", "mapbox", "guardrails"]
    assert any(k.startswith("llm:") for k in keys), "the seeded connections are listed"
    # the probe account names no connection: its spend is the unattributed line
    assert out["unattributed_llm"]["month_usd"] >= 1.4
    by = {p["key"]: p for p in out["providers"]}
    assert by["jev"]["spend"]["month_usd"] >= 0.02
    assert by["dataforseo"]["spend"]["today_usd"] >= 0.3
    assert out["month_start"] == month.isoformat() == _utc(2024, 7, 1).isoformat()
    # AA-1025 C1: every row carries its 24 h / 7 d stats; the CRM rows say they were not asked (no token here)
    assert all("stats" in p for p in out["providers"])
    assert by["crm:llm"]["status_reason"] == "crm_unreachable" and by["mapbox"]["status"] == "untracked"


@pytest.mark.asyncio
async def test_stats_queries_count_attempts_errors_and_latency(pool):
    """AA-1025 C1: LLM_STATS_SQL / DFS_STATS_SQL / GEO_STATS_SQL on the real schema: refused rows are counted apart and
    stay out of the error rate and the latency, failed statuses are errors, the 24 h window is inside the 7 d one,
    rows before ledger v2 (status NULL) and cache hits are left out."""
    now = _utc(2024, 7, 20, 12)
    day_ago, week_ago = _utc(2024, 7, 19, 12), _utc(2024, 7, 13, 12)
    conn = "aa1025-probe-conn"
    rows = [  # (at, status, latency_ms)
        (_utc(2024, 7, 20, 9), "ok", 100), (_utc(2024, 7, 20, 10), "ok", 200), (_utc(2024, 7, 20, 11), "error", 900),
        (_utc(2024, 7, 20, 11), "refused", 0), (_utc(2024, 7, 15, 9), "truncated", 400),
        (_utc(2024, 7, 15, 9), "ok", 300), (_utc(2024, 7, 15, 9), None, 5000),          # before ledger v2: left out
        (_utc(2024, 7, 1, 9), "error", 50),                                            # older than 7 d
    ]
    async with pool.acquire() as c:
        for at, status, ms in rows:
            await c.execute(
                """INSERT INTO shared.llm_call_log (stage, role, model, cost_usd, quality_signal, provider,
                                                    connection_key, created_at, status, latency_ms)
                   VALUES ($1, 'writer', 'probe-model', 0, '{}'::jsonb, 'openai', $2, $3, $4, $5)""",
                STAGE, conn, at, status, ms)
        await _dfs(c, _utc(2024, 7, 20, 7), 0.1, None, status="ok")
        await _dfs(c, _utc(2024, 7, 20, 8), 0.0, None, status="error")
        await c.execute(
            """INSERT INTO shared.dfs_call_log (endpoint, cost_usd, cache_hit, fetched_live, created_at, status)
               VALUES ($1, 0, true, false, $2, 'ok')""", EP, _utc(2024, 7, 20, 9))        # cache hit: no call
        await c.execute(
            """INSERT INTO geo.geocode_call (called_at, provider, requests, cost_usd, endpoint, failed_requests)
               VALUES ($1, $2, 10, 0, 'search', 2), ($3, $2, 5, 0, 'search', NULL)""",
            _utc(2024, 7, 20, 6), GEO, _utc(2024, 7, 16, 6))

    llm = {r["connection_key"]: dict(r) for r in await pool.fetch(gw.LLM_STATS_SQL, day_ago, week_ago, now)}
    r = llm[conn]
    assert (r["calls_24h"], r["errors_24h"], r["refused_24h"]) == (3, 1, 1)
    assert (r["calls_7d"], r["errors_7d"], r["refused_7d"]) == (5, 2, 1)
    assert r["p50_24h"] == pytest.approx(200.0) and r["p95_24h"] == pytest.approx(830.0)
    assert r["p50_7d"] == pytest.approx(300.0)
    row = gw.with_stats({"key": "x"}, r)
    assert row["error_pct"] == 33.3 and row["latency_p50_ms"] == 200 and row["stats"]["7d"]["error_pct"] == 40.0

    dfs = dict(await pool.fetchrow(gw.DFS_STATS_SQL.replace("AND NOT cache_hit", "AND NOT cache_hit AND endpoint = $4"),
                                   day_ago, week_ago, now, EP))
    assert (dfs["calls_24h"], dfs["errors_24h"]) == (2, 1) and dfs["p50_24h"] == pytest.approx(700.0)
    geo = dict(await pool.fetchrow(gw.GEO_STATS_SQL, day_ago, week_ago, now, GEO))
    assert (geo["calls_24h"], geo["errors_24h"], geo["calls_7d"], geo["errors_7d"]) == (10, 2, 15, 2)
    assert geo["p50_24h"] is None


@pytest.mark.asyncio
async def test_budgets_queries_run_on_the_real_schema(pool):
    scope = f"job:{KIND}"
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO shared.spend_budget (provider, scope, per_run_usd, per_day_usd, hard_stop, alert_pct,
                                                updated_by)
               VALUES ('dfs', $1, 0.75, NULL, true, 80, 'admin:probe')""", scope)
    try:
        out = await gw.read_budgets(pool)
        assert out["errors"] == [], out["errors"]
        job = [r for r in out["tiers"]["job"] if r["scope"] == scope]
        assert job and job[0]["job_kind"] == KIND and job[0]["per_run_usd"] == 0.75
        assert job[0]["state"] == "no_daily_cap"
        assert all(r["scope"] == "global" for r in out["tiers"]["provider"])
        assert out["tiers"]["tenant"] == []
        assert set(out["spent_today_usd"]) == set(out["providers"])
        assert out["tiers"]["connection"], "the seeded connections are listed"
        assert {c["tier"] for c in out["coverage"]} >= {"provider"}
    finally:
        async with pool.acquire() as c:
            await c.execute("DELETE FROM shared.spend_budget WHERE scope = $1", scope)


@pytest.mark.asyncio
async def test_a_kind_scoped_job_row_reads_its_kind_spend(pool, monkeypatch):
    """A job kind guarded on its own spend (cost_guard.KIND_DAY_SCOPED) is measured with kind_day_spend on the real
    schema: the probe kind has no LLM rows today, so its row reads $0 while the provider figure is untouched."""
    from shared import cost_guard

    scope = f"job:{KIND}"
    monkeypatch.setattr(cost_guard, "KIND_DAY_SCOPED", frozenset({("dfs", KIND)}))
    async with pool.acquire() as c:
        await c.execute(
            """INSERT INTO shared.spend_budget (provider, scope, per_run_usd, per_day_usd, hard_stop, alert_pct,
                                                updated_by)
               VALUES ('dfs', $1, NULL, 0.5, true, 80, 'admin:probe')""", scope)
    try:
        out = await gw.read_budgets(pool)
        assert out["errors"] == [], out["errors"]
        job = [r for r in out["tiers"]["job"] if r["scope"] == scope]
        assert job and job[0]["day_scope"] == "kind" and job[0]["spent_today_usd"] == 0.0
        assert job[0]["used_pct"] == 0.0 and job[0]["state"] == "ok"
    finally:
        async with pool.acquire() as c:
            await c.execute("DELETE FROM shared.spend_budget WHERE scope = $1", scope)
