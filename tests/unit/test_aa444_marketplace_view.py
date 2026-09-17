"""AA-444 — tenant Marketplace view (api/routers/v1_marketplace.py).

Mocks the asyncpg pool per the pool.acquire() convention established in
test_aa300_admin_atoms.py / test_aa330_admin_marketplace.py — no live DB. The
`tenant=` dependency is bypassed the same way test_aa300_admin_atoms.py bypasses
`owner_scope=` — called directly with the resolved value, not through FastAPI's
Depends() machinery.
"""
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.routers import v1_marketplace

TENANT_ID = str(uuid.uuid4())
TOUR_A = str(uuid.uuid4())
TOUR_B = str(uuid.uuid4())
VERSION_A = str(uuid.uuid4())
PUBLISHED_A = str(uuid.uuid4())


def _make_pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _make_request(pool):
    request = MagicMock()
    request.app.state.pool = pool
    return request


def _marketplace_row(**over):
    base = {
        "version_id": uuid.UUID(VERSION_A), "version_number": 2, "status": "approved",
        "quality_score": Decimal("8.5"), "qa_status": "passed", "qa_auto_passed": False,
        "version_created_at": None,
        "published_tour_id": uuid.UUID(PUBLISHED_A), "tour_id": uuid.UUID(TOUR_A),
        "name": "Ha Long Bay Cruise", "country": "Vietnam", "duration": "3 days",
        "price_raw": "US$450", "atom_count": 12, "high_atom_count": 5,
    }
    base.update(over)
    return base


class TestGetMarketplaceQuery:
    @pytest.mark.asyncio
    async def test_query_scoped_to_tenant_id_and_owner_scope(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {"posts_per_week": 3}
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await v1_marketplace.get_marketplace(request, tenant={"sub": TENANT_ID})

        # Both $1 (tenant_tour_versions.tenant_id) and $2 (tour_atoms.owner_scope) are the
        # SAME tenant_id — this is the exact join ADR-2026-038 §0.3 specifies, not two
        # independently-chosen filters that happen to coincide.
        query, tenant_param, owner_scope_param = conn.fetch.call_args[0]
        assert "tenant_tour_versions" in query
        assert "tour_atoms" in query
        assert "WHERE ttv.tenant_id = $1" in query
        assert "WHERE owner_scope = $2" in query
        assert tenant_param == TENANT_ID
        assert owner_scope_param == TENANT_ID

    @pytest.mark.asyncio
    async def test_does_not_touch_marketplace_portfolios(self):
        """AA-444's whole point: this new view must never read the deprecated table."""
        conn = AsyncMock()
        conn.fetchrow.return_value = {"posts_per_week": 3}
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        await v1_marketplace.get_marketplace(request, tenant={"sub": TENANT_ID})

        query = conn.fetch.call_args[0][0]
        assert "marketplace_portfolios" not in query


class TestGetMarketplaceResponseShape:
    @pytest.mark.asyncio
    async def test_returns_price_and_runway_estimates(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {"posts_per_week": 3}
        conn.fetch.return_value = [_marketplace_row()]
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await v1_marketplace.get_marketplace(request, tenant={"sub": TENANT_ID})

        assert result["tenant_id"] == TENANT_ID
        assert result["posts_per_week"] == 3
        assert result["total_tours"] == 1
        assert result["total_atoms"] == 12

        tour = result["tours"][0]
        assert tour["name"] == "Ha Long Bay Cruise"
        assert tour["price_available"] is True
        assert tour["price_usd"] == 450.0
        # runway_months(12, 3) == floor(12 / (3*4)) == 1, same reused formula
        assert tour["runway_months"] == 1
        assert "price_raw" not in tour  # popped before returning, never leaked to the client

    @pytest.mark.asyncio
    async def test_no_posts_per_week_means_no_runway(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {"posts_per_week": None}
        conn.fetch.return_value = [_marketplace_row()]
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await v1_marketplace.get_marketplace(request, tenant={"sub": TENANT_ID})

        assert result["posts_per_week"] is None
        assert result["tours"][0]["runway_months"] is None

    @pytest.mark.asyncio
    async def test_unparseable_price_is_unavailable_not_fabricated(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {"posts_per_week": 2}
        conn.fetch.return_value = [_marketplace_row(price_raw="On request")]
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await v1_marketplace.get_marketplace(request, tenant={"sub": TENANT_ID})

        tour = result["tours"][0]
        assert tour["price_available"] is False
        assert tour["price_usd"] is None

    @pytest.mark.asyncio
    async def test_zero_atom_tour_still_included(self):
        """LEFT JOIN semantics: a rewritten tour with no atoms yet is a real gap signal
        (ADR-2026-038 §0.3), not something to hide from the tenant."""
        conn = AsyncMock()
        conn.fetchrow.return_value = {"posts_per_week": 3}
        conn.fetch.return_value = [_marketplace_row(atom_count=0, high_atom_count=0)]
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await v1_marketplace.get_marketplace(request, tenant={"sub": TENANT_ID})

        assert result["total_tours"] == 1
        assert result["tours"][0]["atom_count"] == 0
        assert result["tours"][0]["runway_months"] == 0

    @pytest.mark.asyncio
    async def test_no_tenant_row_still_returns_empty_posts_per_week(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = None
        conn.fetch.return_value = []
        pool = _make_pool(conn)
        request = _make_request(pool)

        result = await v1_marketplace.get_marketplace(request, tenant={"sub": TENANT_ID})

        assert result["posts_per_week"] is None
        assert result["tours"] == []
        assert result["total_tours"] == 0
        assert result["total_atoms"] == 0


class TestAuthReusesV1ToursDependency:
    def test_get_tenant_imported_not_redefined(self):
        from api.routers.v1_tours import get_tenant

        assert v1_marketplace.get_tenant is get_tenant
