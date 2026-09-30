"""AA-198 [AA-193·F1]: _resolve_brand_rule selection priority.

Bug fixed: old fallback `WHERE is_active=true ORDER BY version DESC LIMIT 1` picked the
cross-brand max version (Terra Family v2) regardless of intent. New priority is
explicit id -> named active brand -> explicit 'default'.
"""

import pytest

from api.routers.admin_pipeline import _resolve_brand_rule

TENANT = "00000000-0000-0000-0000-000000000001"


class _FakeConn:
    """Records the last fetchrow(sql, *params) call and returns a canned row."""

    def __init__(self, row):
        self._row = row
        self.sql = None
        self.params = None

    async def fetchrow(self, sql, *params):
        self.sql = " ".join(sql.split())  # normalize whitespace for assertions
        self.params = params
        return self._row


@pytest.mark.asyncio
async def test_resolve_by_brand_identity_id():
    row = {"id": "id-aaa", "brand_name": "Atlas & Hearth", "version": 1}
    conn = _FakeConn(row)

    result = await _resolve_brand_rule(conn, TENANT, "id-aaa", None)

    assert result is row
    assert "id = $1::uuid" in conn.sql
    assert "AND tenant_id = $2::uuid" in conn.sql
    assert conn.params == ("id-aaa", TENANT)


@pytest.mark.asyncio
async def test_resolve_by_brand_name_requires_active():
    row = {"id": "id-tp", "brand_name": "Trail Pulse", "version": 1}
    conn = _FakeConn(row)

    result = await _resolve_brand_rule(conn, TENANT, None, "Trail Pulse")

    assert result is row
    assert "brand_name = $2 AND is_active = true" in conn.sql
    assert "ORDER BY version DESC LIMIT 1" in conn.sql
    assert conn.params == (TENANT, "Trail Pulse")


@pytest.mark.asyncio
async def test_resolve_default_when_no_id_no_name():
    row = {"id": "id-def", "brand_name": "default", "version": 1}
    conn = _FakeConn(row)

    result = await _resolve_brand_rule(conn, TENANT, None, None)

    assert result is row
    # AA-700: 'default' first, else the tenant's newest active row (portal-named brand)
    assert "is_active = true" in conn.sql and "ORDER BY (brand_name = 'default') DESC, version DESC" in conn.sql
    assert conn.params == (TENANT,)


@pytest.mark.asyncio
async def test_brand_identity_id_wins_over_brand_name():
    conn = _FakeConn({"id": "id-aaa", "brand_name": "Atlas & Hearth", "version": 1})

    await _resolve_brand_rule(conn, TENANT, "id-aaa", "Trail Pulse")

    # id branch only — name must not appear in the query, only the id filter
    assert "id = $1::uuid" in conn.sql
    assert "brand_name = $2" not in conn.sql
    assert conn.params == ("id-aaa", TENANT)


@pytest.mark.asyncio
async def test_no_brand_does_not_use_cross_brand_max_version():
    """Regression: no-brand path must select 'default', never cross-brand max version."""
    conn = _FakeConn({"id": "id-def", "brand_name": "default", "version": 1})

    await _resolve_brand_rule(conn, TENANT, None, None)

    # the old buggy fallback selected by version across all active brands
    assert "ORDER BY version DESC" not in conn.sql
    assert "brand_name = 'default'" in conn.sql
