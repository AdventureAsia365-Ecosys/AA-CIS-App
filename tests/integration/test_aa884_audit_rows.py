"""AA-884 PR C against real Postgres (CIS 248 applied by conftest): the SQL behind the staff audit middleware, the
audit list / chain verifier, cost per app and the Platform Apps reads.

- the middleware's insert (write_audit_log with the request columns) fills actor_type / method / route / status and
  the trigger chains it; the Python verifier agrees with the trigger from the test's own first row;
- list_audit filters by route and joins the staff name; the keyset cursor pages;
- AI_SPEND_SQL counts a rolled day from llm_call_daily and the other days from llm_call_log, never both;
- TP_SPEND_TODAY_SQL counts only TripPlanner's online stages since UTC midnight;
- the App-tag snapshot keeps the newest fetch per (tag, service, day);
- worker_register writes git_sha and worker_health returns it; deploy_gate_status answers.
"""
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from _audit_cleanup import delete_audit_rows

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

from api.middleware import staff_audit  # noqa: E402
from api.services import platform_apps, platform_audit, platform_cost  # noqa: E402
from shared.aws_client import cost_explorer as ce  # noqa: E402
from shared.jobs import queue  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SECRET = "aa884-integration-secret"


@pytest_asyncio.fixture
async def env(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    tag = uuid.uuid4().hex[:8]
    admin_id = uuid.uuid4()
    await pool.execute(
        "INSERT INTO shared.admin_users (id, username, password_hash, role, is_active, email, display_name) "
        "VALUES ($1, $2, NULL, 'admin', true, $3, 'Audit Tester')", admin_id, f"aa884-{tag}",
        f"aa884-{tag}@adventure.asia")
    yield SimpleNamespace(pool=pool, tag=tag, admin_id=admin_id)
    await delete_audit_rows(pool, "resource_id LIKE $1", f"/admin/aa884-{tag}%")
    await pool.execute("DELETE FROM shared.admin_users WHERE id = $1", admin_id)
    await pool.close()


def _scope(pool, path, method="POST", headers=None, route=None, query=b""):
    hdrs = [(b"x-admin-secret", SECRET.encode())] + [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    return {"type": "http", "method": method, "path": path, "query_string": query, "headers": hdrs,
            "app": SimpleNamespace(state=SimpleNamespace(pool=pool)),
            "route": SimpleNamespace(path=route) if route else None}


@pytest.mark.asyncio
async def test_middleware_rows_are_chained_and_verify(env):
    p, tag = env.pool, env.tag
    paths = [f"/admin/aa884-{tag}/{i}" for i in range(3)]
    for i, path in enumerate(paths):
        scope = _scope(p, path, headers={"x-admin-role": "admin", "x-admin-user-id": str(env.admin_id)},
                       route="/admin/aa884-x/{n}", query=b"a=1")
        assert staff_audit.should_log(scope)
        await staff_audit._write(scope, 200 if i else 403)
    rows = await p.fetch("SELECT actor, actor_type::text AS actor_type, action, http_method, route, status, chain_seq, "
                         "prev_hash, row_hash, details FROM acp_shared.audit_log WHERE resource_id = ANY($1) "
                         "ORDER BY chain_seq", paths)
    assert len(rows) == 3
    assert [r["status"] for r in rows] == [403, 200, 200]
    assert all(r["actor_type"] == "staff" and r["action"] == "staff.request" and r["http_method"] == "POST"
               and r["route"] == "/admin/aa884-x/{n}" and r["actor"] == f"admin:{env.admin_id}" for r in rows)
    assert json.loads(rows[0]["details"]) == {"auth": "legacy_unverified", "role": "admin", "query_keys": ["a"]}
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]

    result = await platform_audit.verify_chain(p, from_seq=rows[0]["chain_seq"])
    assert result["ok"] is True and result["checked"] >= 3, result

    cached = await platform_audit.verify_chain_cached(p, None, from_seq=rows[0]["chain_seq"])
    assert cached["ok"] is True and cached["pre_chain_rows"] >= 0


@pytest.mark.asyncio
async def test_unchained_row_after_248_is_flagged(env):
    """Review finding (S243): a row inserted with the chain trigger skipped (replica mode) has chain_seq NULL; the
    chain still verifies, so verify_chain_cached must flag it as unchained after the boundary, not as pre-248."""
    p, tag = env.pool, env.tag
    before = await platform_audit.unchained_rows(p)
    assert before["chain_boundary"] is not None  # 248's applied_at in shared.schema_versions
    path = f"/admin/aa884-{tag}/bypass"
    async with p.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SET LOCAL session_replication_role = replica")
            await conn.execute(
                "INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, created_at) "
                "VALUES ('admin:x', 'staff.request', 'http_route', $1, now())", path)
    row = await p.fetchrow("SELECT id::text AS id, chain_seq FROM acp_shared.audit_log WHERE resource_id = $1", path)
    assert row["chain_seq"] is None
    after = await platform_audit.unchained_rows(p)
    assert after["unchained_after_genesis"] == before["unchained_after_genesis"] + 1
    assert after["pre_chain_rows"] == before["pre_chain_rows"] and row["id"] in after["unchained_after_genesis_ids"]
    # Full check: ok must be false. (On this shared test DB other teardowns leave chain gaps, which may be reported
    # first — so assert the count, not the problem text; the unit test pins the text.)
    result = await platform_audit.verify_chain_cached(p, None)
    assert result["ok"] is False and result["unchained_after_genesis"] >= 1
    await delete_audit_rows(p, "resource_id = $1", path)
    assert (await platform_audit.unchained_rows(p))["unchained_after_genesis"] == before["unchained_after_genesis"]


@pytest.mark.asyncio
async def test_machine_row(env):
    p, tag = env.pool, env.tag
    await staff_audit._write(_scope(p, f"/admin/aa884-{tag}/m", route="/admin/x"), 200)
    row = await p.fetchrow("SELECT actor, actor_type::text AS t, action FROM acp_shared.audit_log "
                           "WHERE resource_id = $1", f"/admin/aa884-{tag}/m")
    assert (row["actor"], row["t"], row["action"]) == ("machine", "machine", "machine.request")


@pytest.mark.asyncio
async def test_list_audit_filters_joins_and_pages(env):
    p, tag = env.pool, env.tag
    route = f"/admin/aa884-{tag}/{{n}}"
    for i in range(3):
        await staff_audit._write(_scope(p, f"/admin/aa884-{tag}/{i}", route=route,
                                        headers={"x-admin-role": "admin", "x-admin-user-id": str(env.admin_id)}), 200)
    page1 = await platform_audit.list_audit(p, scope="staff", route=route, limit=2)
    assert len(page1["rows"]) == 2 and page1["next_cursor"]
    first = page1["rows"][0]
    assert first["actor_name"] == "Audit Tester" and first["actor_email"] == f"aa884-{env.tag}@adventure.asia"
    assert first["route"] == route and first["chain_seq"] is not None and first["details"]["role"] == "admin"
    page2 = await platform_audit.list_audit(p, scope="staff", route=route, limit=2, cursor=page1["next_cursor"])
    assert len(page2["rows"]) == 1 and page2["next_cursor"] is None
    ids = {r["id"] for r in page1["rows"]} | {r["id"] for r in page2["rows"]}
    assert len(ids) == 3
    by_action = await platform_audit.list_audit(p, scope="all", action="staff.req", route=route)
    assert len(by_action["rows"]) == 3


@pytest.mark.asyncio
async def test_list_audit_tolerates_non_uuid_admin_actors(env):
    # Live S243: rows written before AA-884 carry actors like 'admin:claude-code-s222'; the staff-name join must
    # not cast them to uuid (500 "invalid input syntax for type uuid").
    p, tag = env.pool, env.tag
    async with p.acquire() as c:
        await c.execute("INSERT INTO acp_shared.audit_log (actor, action, resource_type, resource_id, created_at) "
                        "VALUES ($1, 'staff.legacy', 'admin_user', $2, now())", f"admin:claude-code-{tag}", tag)
    rows = (await platform_audit.list_audit(p, scope="staff", limit=50))["rows"]
    legacy = [r for r in rows if r["actor"] == f"admin:claude-code-{tag}"]
    assert len(legacy) == 1 and legacy[0]["actor_name"] is None


@pytest.mark.asyncio
async def test_ai_spend_sql_rolled_and_raw_days_never_both(env):
    p = env.pool
    marker = f"aa884-{env.tag}"
    # Days far outside any rollup window: another test runs the llm_call_daily rollup over the last 45 days, and once
    # that window reached 11/09/2026 (on 26/10/2026) it wrote a daily row for d2 and hid the raw row (time bomb, S252).
    d1, d2 = date(2024, 1, 10), date(2024, 1, 11)
    try:
        # d1: rolled up (llm_call_daily) AND a stray raw row that must be ignored; d2: raw only.
        await p.execute("""
            INSERT INTO shared.llm_call_daily (day, app, tenant_id, stage, role, model, provider, account, is_shadow,
                                               calls, fallback_calls, tokens_in, tokens_out, cost_usd, updated_at)
            VALUES ($1, 'tripplanner', NULL, $2, 'writer', 'm', 'bedrock-satellite', 'acc3', false,
                    7, 0, 0, 0, 1.5, now())""", d1, marker)
        for day, app, provider, account, cost in ((d1, "tripplanner", "bedrock-satellite", "acc3", 99.0),
                                                  (d2, "tripplanner", "bedrock-satellite", "acc3", 0.25),
                                                  (d2, None, "bedrock", "acc2", 0.5)):
            qs = json.dumps({"app": app} if app else {})
            await p.execute("""
                INSERT INTO shared.llm_call_log (id, stage, role, model, cost_usd, quality_signal, created_at,
                                                 provider, account)
                VALUES ($1, $2, 'writer', 'm', $3, $4::jsonb, $5, $6, $7)""",
                            uuid.uuid4(), marker, cost, qs,
                            datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=3),
                            provider, account)
        rows = await p.fetch(platform_cost.AI_SPEND_SQL, d1, d2 + timedelta(days=1))
        got = {(r["app"], r["provider"], r["account"]): (r["calls"], round(r["cost_usd"], 4), r["billed_in_acc2"])
               for r in rows}
        assert got[("tripplanner", "bedrock-satellite", "acc3")][:2] == (8, 1.75)  # 7 rolled + 1 raw (d2)
        assert got[("cis", "bedrock", "acc2")] == (1, 0.5, True)  # no app marker -> cis; acc2 Bedrock billed in acc2
    finally:
        await p.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", marker)
        await p.execute("DELETE FROM shared.llm_call_daily WHERE stage = $1", marker)


@pytest.mark.asyncio
async def test_tp_spend_today_counts_online_stages_since_utc_midnight(env):
    p = env.pool
    ids = []
    try:
        now = datetime.now(timezone.utc)
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        for stage, app, when, cost in (("tp_compose", "tripplanner", now, 0.4),
                                       ("tp_search_embed", "tripplanner", now, 0.1),
                                       ("tp_extract", "tripplanner", now, 5.0),          # offline stage
                                       ("tp_compose", "cis", now, 7.0),                  # other app
                                       ("tp_compose", "tripplanner", midnight - timedelta(seconds=1), 3.0)):
            i = uuid.uuid4()
            ids.append(i)
            await p.execute("""
                INSERT INTO shared.llm_call_log (id, stage, role, model, cost_usd, quality_signal, created_at)
                VALUES ($1, $2, 'writer', 'm', $3, $4::jsonb, $5)""", i, stage, cost, json.dumps({"app": app}), when)
        rows = await p.fetch(platform_apps.TP_SPEND_TODAY_SQL, ["tp_compose", "tp_search_embed"], "tripplanner")
        got = {r["stage"]: (r["calls"], round(r["cost_usd"], 4)) for r in rows}
        # Other rows of today may exist in a shared test DB: ours are at least these.
        assert got["tp_compose"][0] >= 1 and got["tp_compose"][1] >= 0.4 and got["tp_compose"][1] < 3.4
        assert got["tp_search_embed"][1] >= 0.1
        assert "tp_extract" not in got
    finally:
        await p.execute("DELETE FROM shared.llm_call_log WHERE id = ANY($1::uuid[])", ids)


@pytest.mark.asyncio
async def test_app_snapshot_newest_fetch_wins(env):
    p = env.pool
    acct = f"aa884-{env.tag}"
    try:
        rows = [{"account_id": acct, "app_tag": "", "service": "Amazon RDS", "period_start": "2026-08-01",
                 "period_end": "2026-08-02", "amount_usd": 1.0, "unit": "USD"},
                {"account_id": acct, "app_tag": "cis", "service": "Amazon ECS", "period_start": "2026-08-01",
                 "period_end": "2026-08-02", "amount_usd": 2.0, "unit": "USD"}]
        assert await ce.record_app_snapshot(p, rows) == 2
        await p.execute("UPDATE shared.cost_explorer_app_snapshot SET fetched_at = now() - interval '1 day' "
                        "WHERE account_id = $1", acct)
        await ce.record_app_snapshot(p, [{**rows[0], "amount_usd": 1.5}])
        out = await ce.read_app_cost_range(p, date(2026, 8, 1), date(2026, 8, 2))
        assert out["tags"][""]["total_usd"] == 1.5 and out["tags"]["cis"]["total_usd"] == 2.0
        assert out["total_usd"] == 3.5 and out["covered_from"] == "2026-08-01"
        # Per-day newest fetch (the lazy refresh's coverage / age check reads it).
        assert list(out["day_fetched_at"]) == ["2026-08-01"]
        assert out["day_fetched_at"]["2026-08-01"] == out["fetched_at"]
    finally:
        await p.execute("DELETE FROM shared.cost_explorer_app_snapshot WHERE account_id = $1", acct)


@pytest.mark.asyncio
async def test_worker_git_sha_and_deploy_gate(env):
    p = env.pool
    wid = f"aa884-{env.tag}"
    try:
        await queue.worker_register(p, wid, "host", 2, {"x": 1}, 7, "deadbeef")
        health = await queue.worker_health(p)
        mine = next(w for w in health["workers"] if w["worker_id"] == wid)
        assert mine["git_sha"] == "deadbeef" and mine["alive"] is True
        await queue.worker_register(p, wid, "host", 2, {"x": 1}, 8, "cafe")  # re-register (new image)
        assert await p.fetchval("SELECT git_sha FROM shared.job_worker WHERE worker_id = $1", wid) == "cafe"
    finally:
        await p.execute("DELETE FROM shared.job_worker WHERE worker_id = $1", wid)
    from shared.jobs.worker import deploy_gate_status
    gate = await deploy_gate_status(p)
    assert set(gate) >= {"active", "safe_to_deploy", "exempt_kinds", "open_waves", "blocking_running", "blocked_by"}
