"""
tests/unit/test_aa455_publish_log.py — AA-455 bước 1: acp_shared.publish_log

Covers the two new admin_a4.py endpoints (GET .../publish-log, POST .../publish-log/{id}/unpublish)
and the new v1_publish.py tenant self-unpublish endpoint (DELETE /v1/publish-log/{id}).
Mocks asyncpg via pool.acquire() (matches admin_a4.py's own connection style, not
admin_produce.py's direct pool.fetch — following test_competitors.py's mock-pool convention,
which also uses pool.acquire()).
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

_TEST_SECRET = "test-admin-secret"


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _TEST_SECRET)


def _make_pool(fetchval=None, fetchrow=None, fetch=None, execute=None):
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=fetchval)
    conn.fetchrow = AsyncMock(return_value=fetchrow)
    conn.fetch = AsyncMock(return_value=fetch or [])
    conn.execute = AsyncMock(return_value=execute or "UPDATE 1")

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


def _make_request(pool, headers=None):
    req = MagicMock()
    req.app.state.pool = pool
    req.app.state.redis.get = AsyncMock(return_value="false")  # S241 D-12: explicit OFF
    req.headers = headers or {}
    return req


PUBLISH_ID = uuid.uuid4()
TENANT_ID = str(uuid.uuid4())
OTHER_TENANT_ID = str(uuid.uuid4())
ADMIN_ID = str(uuid.uuid4())


# ── admin_a4.get_publish_log ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_publish_log_requires_admin_secret():
    from api.routers.admin_a4 import get_publish_log

    pool, _ = _make_pool(fetch=[])
    req = _make_request(pool)

    with pytest.raises(HTTPException) as exc_info:
        await get_publish_log(req, tenant_id=None, limit=200, x_admin_secret="wrong")
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_get_publish_log_returns_rows():
    from api.routers.admin_a4 import get_publish_log

    row = {
        "publish_id": str(PUBLISH_ID), "piece_id": str(uuid.uuid4()), "tenant_id": TENANT_ID,
        "tenant_name": "Acme", "tenant_slug": "acme", "channel": "blog", "status": "published",
        "external_id": "123", "external_url": "https://blog.example.com/p/123",
        "published_at": datetime(2026, 8, 24, tzinfo=timezone.utc), "unpublished_at": None,
        "unpublished_by": None, "last_error": None,
        "created_at": datetime(2026, 8, 24, tzinfo=timezone.utc),
    }
    pool, _ = _make_pool(fetch=[row])
    req = _make_request(pool)

    result = await get_publish_log(req, tenant_id=None, limit=200, x_admin_secret=_TEST_SECRET)
    assert result["total"] == 1
    assert result["data"][0]["status"] == "published"


# ── admin_a4.force_unpublish ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_force_unpublish_requires_admin_secret():
    from api.routers.admin_a4 import force_unpublish

    pool, _ = _make_pool(fetchrow=None)
    req = _make_request(pool)

    with pytest.raises(HTTPException) as exc_info:
        await force_unpublish(PUBLISH_ID, req, x_admin_secret="wrong", x_admin_user_id=None)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_force_unpublish_success_records_admin_actor():
    from api.routers.admin_a4 import force_unpublish

    row = {
        "publish_id": str(PUBLISH_ID), "tenant_id": TENANT_ID, "channel": "blog",
        "status": "unpublished", "unpublished_at": datetime(2026, 8, 24, tzinfo=timezone.utc),
    }
    pool, conn = _make_pool(fetchrow=row)
    req = _make_request(pool)

    result = await force_unpublish(PUBLISH_ID, req, x_admin_secret=_TEST_SECRET, x_admin_user_id=ADMIN_ID)

    assert result["status"] == "unpublished"
    assert result["unpublished_by"] == f"admin:{ADMIN_ID}"
    sql = conn.fetchrow.call_args[0][0]
    assert "status = 'published'" in sql
    assert f"admin:{ADMIN_ID}" in conn.fetchrow.call_args[0][2]


@pytest.mark.asyncio
async def test_force_unpublish_malformed_admin_header_falls_back_to_unknown():
    from api.routers.admin_a4 import force_unpublish

    row = {
        "publish_id": str(PUBLISH_ID), "tenant_id": TENANT_ID, "channel": "blog",
        "status": "unpublished", "unpublished_at": datetime(2026, 8, 24, tzinfo=timezone.utc),
    }
    pool, _ = _make_pool(fetchrow=row)
    req = _make_request(pool)

    result = await force_unpublish(PUBLISH_ID, req, x_admin_secret=_TEST_SECRET, x_admin_user_id="not-a-uuid")
    assert result["unpublished_by"] == "admin:unknown"


@pytest.mark.asyncio
async def test_force_unpublish_404_when_already_unpublished():
    """No row returned (status != 'published') -> 404, not a silent no-op double-action."""
    from api.routers.admin_a4 import force_unpublish

    pool, _ = _make_pool(fetchrow=None)
    req = _make_request(pool)

    with pytest.raises(HTTPException) as exc_info:
        await force_unpublish(PUBLISH_ID, req, x_admin_secret=_TEST_SECRET, x_admin_user_id=ADMIN_ID)
    assert exc_info.value.status_code == 404


# ── v1_publish.unpublish (tenant self-unpublish) ─────────────────────────────

@pytest.mark.asyncio
async def test_tenant_unpublish_success_records_tenant_actor():
    """AA-815 — the route now reads the row, removes the post remotely, then flips it (full
    remote-delete coverage in test_aa815_unpublish_disconnect.py)."""
    from unittest.mock import patch

    from api.routers import v1_publish as mod
    from services.acp_s4_blog.cms.wordpress import WordPressAdapter

    published = {"publish_id": str(PUBLISH_ID), "piece_id": str(uuid.uuid4()), "channel": "blog",
                 "external_id": "42", "external_url": "https://b.com/p/42/"}
    updated = {"publish_id": str(PUBLISH_ID), "channel": "blog", "status": "unpublished",
               "unpublished_at": datetime(2026, 8, 24, tzinfo=timezone.utc)}
    pool, conn = _make_pool()
    conn.fetchrow = AsyncMock(side_effect=[published, {"secret_key": "acp/cms/x"}, updated])
    pool.execute = AsyncMock(return_value="INSERT 0 1")
    req = _make_request(pool)
    tenant = {"sub": TENANT_ID, "role": "tenant"}

    with patch.object(mod, "_get_secret", return_value={"wp_url": "https://b.com", "username": "u",
                                                        "app_password": "p"}), \
         patch.object(WordPressAdapter, "delete_post", AsyncMock(return_value="trashed")):
        result = await mod.unpublish(PUBLISH_ID, req, tenant=tenant)

    assert result["status"] == "unpublished"
    assert result["unpublished_by"] == f"tenant:{TENANT_ID}"
    params = conn.fetchrow.call_args[0]
    assert str(PUBLISH_ID) == str(params[1])
    assert params[2] == TENANT_ID
    assert params[3] == f"tenant:{TENANT_ID}"


@pytest.mark.asyncio
async def test_tenant_unpublish_404_cross_tenant_no_leak():
    """Row belongs to another tenant -> the ownership-scoped UPDATE returns nothing -> 404,
    identical to the 'not found at all' case (AA-445-02 no-existence-leak lesson)."""
    from api.routers.v1_publish import unpublish

    pool, conn = _make_pool(fetchrow=None)  # WHERE tenant_id=$2 excludes the row -> no match
    req = _make_request(pool)
    tenant = {"sub": OTHER_TENANT_ID, "role": "tenant"}

    with pytest.raises(HTTPException) as exc_info:
        await unpublish(PUBLISH_ID, req, tenant=tenant)
    assert exc_info.value.status_code == 404
    # confirm the query itself was scoped by the caller's own tenant_id, not the row's owner
    assert conn.fetchrow.call_args[0][2] == OTHER_TENANT_ID


@pytest.mark.asyncio
async def test_tenant_unpublish_404_when_already_unpublished():
    from api.routers.v1_publish import unpublish

    pool, _ = _make_pool(fetchrow=None)
    req = _make_request(pool)
    tenant = {"sub": TENANT_ID, "role": "tenant"}

    with pytest.raises(HTTPException) as exc_info:
        await unpublish(PUBLISH_ID, req, tenant=tenant)
    assert exc_info.value.status_code == 404
