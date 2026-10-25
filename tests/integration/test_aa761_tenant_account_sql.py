"""AA-761 against real Postgres (every migration replayed by conftest): the SQL behind /v1/account.

Cross-tenant refusal on actual rows: tenant A's activity, CSV and API key facts never include tenant B's rows, and
A's own staff-only rows (admin silent corrections, the staff request trail, brand edits) stay hidden. Date filters,
paging totals and the key's issue / last-use times are read from rows this test writes.
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

from api.services import tenant_account as ta  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


async def _tenant(pool, tag: str, suffix: str) -> uuid.UUID:
    tid = uuid.uuid4()
    await pool.execute(
        "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, api_key_hash, rate_limit_rpm, is_active, "
        "posts_per_week) VALUES ($1, $2, $3, 'starter', $4, 60, true, 3)",
        tid, f"AA-761 {suffix} {tag}", f"aa761-{suffix}-{tag}", f"hash-{suffix}-{tag}")
    return tid


async def _audit(pool, tenant_id, action, at, actor=None, details=None, resource_type="tenant", resource_id=None):
    await pool.execute(
        "INSERT INTO acp_shared.audit_log (id, tenant_id, actor, action, resource_type, resource_id, details, "
        "created_at) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)",
        uuid.uuid4(), str(tenant_id), actor or f"tenant:{tenant_id}", action, resource_type,
        resource_id or str(tenant_id), json.dumps(details or {}), at)


@pytest_asyncio.fixture
async def env():
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    tag = uuid.uuid4().hex[:8]
    a = await _tenant(pool, tag, "a")
    b = await _tenant(pool, tag, "b")
    # A: 3 visible rows on 3 days + 4 rows a tenant must never see
    await _audit(pool, a, "agency.onboard", T0, actor="admin:1", details={"name": "x", "plan_id": "p-secret"})
    await _audit(pool, a, "tenant.login", T0 + timedelta(days=1), details={"client": "portal"})
    await _audit(pool, a, "content_piece.published", T0 + timedelta(days=2), resource_type="content_piece",
                 details={"channel": "blog", "success": True, "publish_id": "pub-secret"})
    await _audit(pool, a, "content_piece.admin_sentence_cut", T0 + timedelta(days=2), actor="admin:1",
                 resource_type="content_piece", details={"before": "staff-only text"})
    await _audit(pool, a, "staff.request", T0 + timedelta(days=2), actor="admin:1")
    await _audit(pool, a, "tenant.brand_updated", T0 + timedelta(days=2), actor="admin:1")
    await _audit(pool, a, "tenant.erased", T0 + timedelta(days=2), actor="admin:1")
    # B: rows of another tenant, newer than all of A's
    await _audit(pool, b, "tenant.login", T0 + timedelta(days=5), details={"client": "portal"})
    await _audit(pool, b, "tenant.key_rotated", T0 + timedelta(days=5), actor="admin:1")
    await pool.execute(
        "INSERT INTO shared.tenant_api_usage (id, tenant_id, endpoint, method, status_code, called_at, actor_type) "
        "VALUES ($1, $2, '/v1/tours/pool', 'GET', 200, $3, 'tenant'), "
        "($4, $5, '/v1/tours/pool', 'GET', 200, $6, 'tenant')",
        uuid.uuid4(), a, T0 + timedelta(days=3), uuid.uuid4(), b, T0 + timedelta(days=9))
    yield SimpleNamespace(pool=pool, a=str(a), b=str(b))
    await delete_audit_rows(pool, "tenant_id = ANY($1::text[])", [str(a), str(b)])
    await pool.execute("DELETE FROM shared.tenant_api_usage WHERE tenant_id = ANY($1::uuid[])", [a, b])
    await pool.execute("DELETE FROM shared.tenants WHERE tenant_id = ANY($1::uuid[])", [a, b])
    await pool.close()


@pytest.mark.asyncio
async def test_activity_is_own_tenant_and_allowlisted_only(env):
    out = await ta.list_activity(env.pool, env.a, ta.ActivityFilters(), page=1, page_size=25)
    assert out["pagination"]["total"] == 3
    actions = [r["action"] for r in out["data"]]
    assert actions == ["content_piece.published", "tenant.login", "agency.onboard"]  # newest first
    text = json.dumps(out)
    for leak in ("staff-only text", "pub-secret", "p-secret", "staff.request", "admin_sentence", env.b):
        assert leak not in text
    assert out["data"][0]["detail"] == "WordPress"
    assert out["data"][2]["actor"] == "adventure_asia"


@pytest.mark.asyncio
async def test_other_tenant_sees_only_its_rows(env):
    out = await ta.list_activity(env.pool, env.b, ta.ActivityFilters())
    assert sorted(r["action"] for r in out["data"]) == ["tenant.key_rotated", "tenant.login"]
    assert env.a not in json.dumps(out)


@pytest.mark.asyncio
async def test_filters_and_paging(env):
    content = await ta.list_activity(env.pool, env.a, ta.ActivityFilters(category="content"))
    assert [r["action"] for r in content["data"]] == ["content_piece.published"]
    day = T0.date() + timedelta(days=1)
    one_day = await ta.list_activity(env.pool, env.a, ta.ActivityFilters(since=day, until=day))
    assert [r["action"] for r in one_day["data"]] == ["tenant.login"]
    p2 = await ta.list_activity(env.pool, env.a, ta.ActivityFilters(), page=2, page_size=2)
    assert p2["pagination"] == {"page": 2, "page_size": 2, "total": 3, "pages": 2}
    assert [r["action"] for r in p2["data"]] == ["agency.onboard"]
    nothing = await ta.list_activity(env.pool, env.a, ta.ActivityFilters(since=date(2030, 1, 1)))
    assert nothing["pagination"]["total"] == 0 and nothing["data"] == []


@pytest.mark.asyncio
async def test_export_is_the_whole_filtered_set_of_the_tenant(env):
    csv_text = await ta.export_activity_csv(env.pool, env.a, ta.ActivityFilters())
    lines = csv_text.strip().split("\n")
    assert len(lines) == 1 + 3
    assert env.b not in csv_text and "staff-only text" not in csv_text and "Brand" not in csv_text


@pytest.mark.asyncio
async def test_api_key_facts_are_per_tenant(env):
    a = await ta.api_key_facts(env.pool, env.a)
    assert a["has_key"] is True and a["masked"].startswith("cis_")
    assert a["issued_at"] == T0.isoformat()  # agency.onboard; B's key rotation is not A's
    assert a["last_sign_in_at"] == (T0 + timedelta(days=1)).isoformat()
    assert a["last_api_call_at"] == (T0 + timedelta(days=3)).isoformat()
    assert a["last_used_at"] == a["last_api_call_at"]
    assert "hash-a" not in json.dumps(a)
    b = await ta.api_key_facts(env.pool, env.b)
    assert b["issued_at"] == (T0 + timedelta(days=5)).isoformat()
    assert b["last_used_at"] == (T0 + timedelta(days=9)).isoformat()
    await env.pool.execute("UPDATE shared.tenants SET api_key_hash = 'REVOKED_' || tenant_id::text "
                           "WHERE tenant_id = $1", uuid.UUID(env.b))
    assert (await ta.api_key_facts(env.pool, env.b))["has_key"] is False
    assert (await ta.api_key_facts(env.pool, str(uuid.uuid4())))["has_key"] is False
