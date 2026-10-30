"""AA-1003 R4 part 2 against real Postgres: every SQL the TP-01, SEC-03, SEC-08, DEV-01, DEV-03 and OPS-15 readers run,
as written, on the replayed schema, then each renderer on what the collectors returned.

Fixture rows (TripPlanner ledger calls, staff audit rows, worker starts, a DEV-03 run) and the TripPlanner contract
view stand-in live in one transaction rolled back at the end. OPS-15's ECS and health parts are outside the DB and
replaced by stand-ins.
"""
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from services.reports import delivery, money, planner, registry, security  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
UTC = timezone.utc
MON, SUN = date(2023, 6, 5), date(2023, 6, 11)         # an ISO week no other test writes
JUNE1, JUNE30 = date(2023, 6, 1), date(2023, 6, 30)
TAG = "aa1003_r4p2_probe"
NOW = datetime(2023, 7, 4, 2, tzinfo=UTC)

TP_VIEW = ("SELECT * FROM (VALUES ('2023-06-06'::date, 'planner_session_started'::text, 12::bigint), "
           "('2023-06-06'::date, 'trip_started'::text, 3::bigint), ('2023-06-07'::date, 'inquiry_sent'::text, "
           "1::bigint)) AS v(day, metric, n)")


def _ts(day: date, hour: int = 3) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


@pytest_asyncio.fixture
async def env():
    c = await asyncpg.connect(DSN)
    tr = c.transaction()
    await tr.start()
    try:
        if await c.fetchval("SELECT to_regclass('tripplanner.v_ops_traffic_daily')") is None:
            await c.execute("CREATE SCHEMA IF NOT EXISTS tripplanner")
            await c.execute(f"CREATE VIEW tripplanner.v_ops_traffic_daily AS {TP_VIEW}")
        for stage, cost in (("tp_compose", 1.5), ("tp_search_embed", 0.001)):
            await c.execute(
                "INSERT INTO shared.llm_call_log (id, stage, role, model, tokens_in, tokens_out, cost_usd, "
                "quality_signal, created_at, account, fallback_used, provider) VALUES (gen_random_uuid(), $1, "
                "'writer', 'm', 10, 5, $2, '{\"app\": \"tripplanner\"}'::jsonb, $3, 'acc3', false, 'bedrock')",
                stage, cost, _ts(MON + timedelta(days=1)))
        for i, (actor, action, status, hour) in enumerate((
                ("admin:" + TAG, "staff.request", 200, 16), ("admin:" + TAG, "staff.request", 403, 16),
                ("admin:" + TAG, "staff.roles_changed", None, 3))):
            await c.execute(
                "INSERT INTO acp_shared.audit_log (id, tenant_id, actor, action, resource_type, resource_id, details, "
                "created_at, actor_type, http_method, route, status) VALUES (gen_random_uuid(), NULL, $1, $2, "
                "'admin_user', $3, '{}'::jsonb, $4, 'staff', 'POST', '/admin/x', $5)",
                actor, action, f"{TAG}-{i}", _ts(MON + timedelta(days=1), hour), status)
        # aaa111 started 12 days before the week (before the 8-day history cutoff) and is still live at its start:
        # only the baseline row of WORKER_STARTS_SQL tells DEV-03 that bbb222 is a deploy
        for sha, day, rev in (("aaa111", MON - timedelta(days=12), 1), ("bbb222", MON + timedelta(days=1), 2),
                              ("aaa111", MON + timedelta(days=3), 3)):
            await c.execute(
                "INSERT INTO shared.job_worker (worker_id, host, started_at, last_seen_at, max_parallel, caps, "
                "running_jobs, reaped_requeued, reaped_failed, task_revision, git_sha) VALUES ($1, $2, $3, $3, 1, "
                "'{}'::jsonb, 0, 0, 0, $4, $5)", f"{TAG}-{uuid.uuid4()}", TAG, _ts(day), rev, sha)
        await c.execute(
            "INSERT INTO shared.report_run (id, report_id, period_start, period_end, status, trigger, output, "
            "created_at, updated_at) VALUES (gen_random_uuid(), 'DEV-03', $1, $2, 'succeeded', 'manual', $3::jsonb, "
            "now(), now())", date(2023, 5, 29), date(2023, 6, 4),
            json.dumps({"cis_deploys": [{"git_sha": "zzz999", "at": "2023-06-02T03:00:00+00:00", "rollback": False}]}))
        yield c
    finally:
        await tr.rollback()
        await c.close()


def _ok(facts: dict) -> dict:
    return {k: v for k, v in facts.items() if money.failed(v)}


async def test_tp01_reads_the_contract_view_and_the_ledger(env):
    reg = registry.load()
    facts = await planner.collect(env, MON, SUN)
    assert _ok(facts) == {}, facts
    out = planner.render_tp01(reg, reg.report("TP-01"), MON, SUN, facts, NOW)
    k = {x["id"]: x for x in out.output["kpis"]}
    assert k["K-TP-LLMCAP"]["value"] == pytest.approx(1.501)
    assert out.output["llm_spend"]["by_stage"]["tp_compose"] == pytest.approx(1.5)
    assert out.output["funnel"]["this"]["ai_search"] >= 1


async def test_security_readers_and_renderers(env):
    reg = registry.load()
    sec08 = await security.collect_sec08(env, JUNE1, JUNE30)
    assert _ok(sec08) == {}, sec08
    assert any(r["resource_id"] == f"{TAG}-2" for r in sec08["directory"])
    assert any(r["actor"] == "admin:" + TAG and r["status"] == 403 for r in sec08["refused"])
    assert "ok" in sec08["chain"] and sec08["chain"]["checked"] >= 3
    out = security.render_sec08(reg, reg.report("SEC-08"), JUNE1, JUNE30, sec08, NOW)
    assert out.output["report_id"] == "SEC-08"
    sec03 = await security.collect_sec03(env, JUNE1, JUNE30, NOW)
    assert _ok(sec03) == {}, sec03
    assert set(sec03["staff"]) == {"active", "owners", "password_logins", "stale", "not_linked", "disabled"}
    out = security.render_sec03(reg, reg.report("SEC-03"), JUNE1, JUNE30, sec03, NOW)
    assert out.output["report_id"] == "SEC-03"


async def test_delivery_readers_and_renderers(env, monkeypatch):
    reg = registry.load()
    dev03 = await delivery.collect_dev03(env, MON, SUN)
    assert _ok(dev03) == {}, dev03
    out = delivery.render_dev03(reg, reg.report("DEV-03"), MON, SUN, dev03, NOW)
    assert [(d["git_sha"], d["rollback"]) for d in out.output["cis_deploys"]] == [("bbb222", False),
                                                                                  ("aaa111", True)]
    dev01 = await delivery.collect_dev01(env, JUNE1, JUNE30)
    assert _ok(dev01) == {}, dev01
    out = delivery.render_dev01(reg, reg.report("DEV-01"), JUNE1, JUNE30, dev01, NOW)
    assert [d["git_sha"] for d in out.output["cis_deploys"]] == ["zzz999", "bbb222", "aaa111"]

    async def no_ecs():
        return {"error": "not permitted (ecs:DescribeServices)"}

    async def no_apps(conn):
        return {"error": "no network in tests"}
    from services.reports import digest
    monkeypatch.setattr(delivery, "ecs_state", no_ecs)
    monkeypatch.setattr(digest, "load_apps", no_apps)
    ops15 = await delivery.collect_ops15(env)
    assert set(_ok(ops15)) == {"ecs", "apps"}, ops15
    out = delivery.render_ops15(reg, reg.report("OPS-15"), MON, SUN, ops15, NOW)
    assert {"migrations", "cis_jobs", "open_waves"} <= set(out.output["snapshot"]["captured"])
    assert isinstance(await env.fetchval("SELECT 1"), int)          # the transaction is still usable
