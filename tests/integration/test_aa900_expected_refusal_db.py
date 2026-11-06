"""AA-900 (S271 lane l11) — the expected-refusal rule (shared/llm_client/expected_refusal.py) in every SQL that counts
gateway errors, against real Postgres (migrations applied by conftest).

Scene (invented keys): connection zz-aa900-acc answers (access_state ok) and has two deployments: zz-aa900-denied
(its last probe: access_denied, a model with no grant) and zz-aa900-was-ok (its last probe: ok). Connection
zz-aa900-blocked is refused account-wide (access_state access_denied) with a denied deployment too. Ledger rows of the
last hour on them:

  * 3 refused / access_denied on zz-aa900-denied @ zz-aa900-acc        -> expected (not an error, not "refused")
  * 1 refused / access_denied on zz-aa900-was-ok @ zz-aa900-acc        -> counted (a working model starts refusing)
  * 1 refused / throttled on zz-aa900-denied @ zz-aa900-acc            -> counted (another class)
  * 1 refused / access_denied on zz-aa900-denied @ zz-aa900-blocked    -> counted (account-wide is never expected)
  * 1 ok row on zz-aa900-was-ok @ zz-aa900-acc

Checked: ai_gateway.LLM_STATS_SQL (refused vs expected_refused), the trace row column (llm_event's flag),
route_watch.read_stats (failed_attempts vs expected_refusals), llm_route_trace.requests_sql, and
llm_availability.read_connections (a breaker row of the denied deployment is listed as known, the connection is not
open). No ledger row is changed.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import ai_gateway as gw  # noqa: E402
from api.services import llm_availability as av  # noqa: E402
from api.services import llm_route_trace as rt  # noqa: E402
from shared.llm_client import route_watch as rw  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "zz_aa900_stage"
ACC, BLOCKED = "zz-aa900-acc", "zz-aa900-blocked"
DENIED, WAS_OK = "zz-aa900-denied", "zz-aa900-was-ok"
ACTOR = "test-aa900-l11"


async def _clean(c):
    await c.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)
    await c.execute("DELETE FROM shared.llm_provider_health WHERE connection_key = ANY($1::text[])", [ACC, BLOCKED])
    await c.execute("DELETE FROM shared.llm_model_deployment WHERE connection_key = ANY($1::text[])", [ACC, BLOCKED])
    await c.execute("DELETE FROM shared.llm_provider_connection WHERE connection_key = ANY($1::text[])", [ACC, BLOCKED])
    await c.execute("DELETE FROM shared.llm_model_catalog WHERE model_key = ANY($1::text[])", [DENIED, WAS_OK])


@pytest_asyncio.fixture
async def scene():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    now = datetime.now(timezone.utc)
    job = str(uuid.uuid4())
    trace = str(uuid.uuid4())
    async with p.acquire() as c:
        await _clean(c)
        for m in (DENIED, WAS_OK):
            await c.execute(
                "INSERT INTO shared.llm_model_catalog (model_key, label, vendor, provider, api_style, "
                "bedrock_profile_ids, callable_via, supports_temperature, enabled, updated_by) "
                "VALUES ($1, 'Invented', 'anthropic', 'bedrock', 'converse', '{}'::jsonb, '{llm_client}', false, "
                "false, $2)", m, ACTOR)
        for key, state in ((ACC, "ok"), (BLOCKED, "access_denied")):
            await c.execute(
                "INSERT INTO shared.llm_provider_connection (connection_key, kind, auth_mode, label, region, "
                "data_scope, enabled, updated_by, access_state) VALUES ($1, 'bedrock', 'task_role', 'test', "
                "'us-west-2', 'aws_only', $2, $3, $4)", key, key == ACC, ACTOR, state)
        for model, conn, state in ((DENIED, ACC, "access_denied"), (WAS_OK, ACC, "ok"),
                                   (DENIED, BLOCKED, "access_denied")):
            await c.execute(
                "INSERT INTO shared.llm_model_deployment (model_key, connection_key, wire_id, priority, enabled, "
                "origin, updated_by, access_state) VALUES ($1, $2, 'zz-wire', 100, false, 'admin', $3, $4)",
                model, conn, ACTOR, state)

        async def row(model, conn, status, error_class=None, cost=0.0):
            signal = {"route_trace": trace, "position": 0} if status != "ok" else {"route_trace": trace,
                                                                                  "route_position": 0}
            await c.execute(
                "INSERT INTO shared.llm_call_log (stage, role, model, tokens_in, tokens_out, cost_usd, "
                "quality_signal, created_at, status, error_class, attempt_no, latency_ms, connection_key, provider, "
                "app, job_id) VALUES ($1, 'judge', $2, 10, 5, $3, $4::jsonb, $5, $6, $7, 1, 50, $8, "
                "'bedrock-satellite', 'cis', $9::uuid)",
                STAGE, model, cost, json.dumps(signal), now - timedelta(minutes=30), status, error_class, conn, job)

        for _ in range(3):
            await row(DENIED, ACC, "refused", "access_denied")
        await row(WAS_OK, ACC, "refused", "access_denied")
        await row(DENIED, ACC, "refused", "throttled")
        await row(DENIED, BLOCKED, "refused", "access_denied")
        await row(WAS_OK, ACC, "ok", cost=0.001)
        await c.execute(
            "INSERT INTO shared.llm_provider_health (connection_key, model_key, last_ok_at, last_error_at, "
            "last_error_class, last_error, consecutive_failures, circuit_open_until, open_streak, opened_at) "
            "VALUES ($1, $2, NULL, $3, 'access_denied', 'boom', 9, $4, 9, $5), ($1, $6, $3, NULL, NULL, NULL, 0, "
            "NULL, 0, NULL)",
            ACC, DENIED, now - timedelta(minutes=30), now + timedelta(hours=5), now - timedelta(days=8), WAS_OK)
    yield {"pool": p, "now": now, "job": job, "trace": trace}
    async with p.acquire() as c:
        await _clean(c)
    await p.close()


@pytest.mark.asyncio
async def test_provider_stats_count_expected_refusals_apart(scene):
    now = scene["now"]
    rows = {r["connection_key"]: dict(r) for r in await scene["pool"].fetch(
        gw.LLM_STATS_SQL, now - timedelta(hours=24), now - timedelta(days=7), now + timedelta(minutes=1))}
    acc, blocked = gw.window_stats(rows[ACC], "24h"), gw.window_stats(rows[BLOCKED], "24h")
    assert (acc["calls"], acc["errors"], acc["refused"], acc["expected_refused"]) == (1, 0, 2, 3)
    assert (blocked["refused"], blocked["expected_refused"]) == (1, 0)
    assert gw.window_stats(rows[ACC], "7d")["expected_refused"] == 3


@pytest.mark.asyncio
async def test_each_ledger_row_carries_its_flag(scene):
    rows = await scene["pool"].fetch(gw.TRACE_LLM_SQL, scene["job"], 50)
    events = [gw.llm_event(dict(r)) for r in rows]
    flagged = sorted((e["model"], e["account"], e["error_class"]) for e in events if e["expected_refusal"])
    assert flagged == [(DENIED, ACC, "access_denied")] * 3
    assert sum(1 for e in events if e["outcome"] == "refused" and not e["expected_refusal"]) == 3


@pytest.mark.asyncio
async def test_route_watch_and_trace_leave_expected_refusals_out_of_failed_attempts(scene):
    async with scene["pool"].acquire() as c:
        stats = await rw.read_stats(c, scene["now"] - timedelta(hours=1))
        reqs = await c.fetch(rt.requests_sql(stage=STAGE, fallback_only=False), scene["now"] - timedelta(hours=1),
                             STAGE, 10)
    st = stats[STAGE]
    assert (st["failed_attempts"], st["expected_refusals"], st["answered"]) == (3, 3, 1)
    (req,) = [rt.request_view(dict(r)) for r in reqs]
    assert (req["failed_attempts"], req["expected_refusals"]) == (3, 3)


@pytest.mark.asyncio
async def test_the_availability_card_lists_the_known_model_and_keeps_the_connection_ok(scene):
    async with scene["pool"].acquire() as c:
        ready, conns = await av.read_connections(c, now=scene["now"])
    assert ready
    (acc,) = [x for x in conns if x["connection_key"] == ACC]
    assert acc["known_refused_models"] == [DENIED] and acc["open_models"] == 0 and acc["status"] == "ok"
    assert {m["model_key"]: m["expected_refusal"] for m in acc["models"]} == {DENIED: True, WAS_OK: False}
