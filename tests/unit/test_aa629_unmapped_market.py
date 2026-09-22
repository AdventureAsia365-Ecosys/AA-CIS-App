"""AA-629 — tenant target_market outside DFS_LOCATION_MAP no longer silently falls back to US.

Covers: seed_builder.unmatched_countries() (pure), shared.dfs_client.unmapped_market's
record/list helpers (asyncpg mocked, no live DB), and slate.py::_tenant_market_codes()'s
best-effort recording (mocked TenantConfigService + conn). No live DB.
"""
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


# ── unmatched_countries() — pure ────────────────────────────────────────────

def test_unmatched_countries_empty_input_is_not_a_bug():
    from services.seo_intelligence.seed_builder import unmatched_countries
    assert unmatched_countries({}) == []
    assert unmatched_countries({"countries": []}) == []
    assert unmatched_countries(None) == []


def test_unmatched_countries_all_unmatched_returns_them():
    from services.seo_intelligence.seed_builder import unmatched_countries
    assert unmatched_countries({"countries": ["VN", "TH"]}) == ["VN", "TH"]


def test_unmatched_countries_some_matched_is_not_all_unmatched():
    # DE is known -> tenant HAS a real usable market, ZZ being unknown alongside it is not
    # "all unmatched" (resolve_buyer_market(s)() already has a real market to use here).
    from services.seo_intelligence.seed_builder import unmatched_countries
    assert unmatched_countries({"countries": ["DE", "ZZ"]}) == []


def test_unmatched_countries_dedupes_preserving_order():
    from services.seo_intelligence.seed_builder import unmatched_countries
    assert unmatched_countries({"countries": ["VN", "TH", "VN"]}) == ["VN", "TH"]


def test_resolve_buyer_market_and_markets_unchanged_by_aa629():
    # AA-629 deliberately did not change resolve_buyer_market()/resolve_buyer_markets()'s own
    # US-fallback behavior — 5+ call sites destructure a bare tuple/list and must keep working.
    from services.seo_intelligence.seed_builder import resolve_buyer_market, resolve_buyer_markets
    assert resolve_buyer_market({"countries": ["VN"]})[1] == "United States"
    assert resolve_buyer_markets({"countries": ["VN", "TH"]}) == resolve_buyer_markets({})


# ── shared.dfs_client.unmapped_market — asyncpg mocked ──────────────────────

@pytest.mark.asyncio
async def test_record_unmapped_market_request_accepts_a_pool():
    from shared.dfs_client import unmapped_market

    written = {
        "id": uuid4(), "tenant_id": uuid4(), "country_code": "VN",
        "requested_at": MagicMock(isoformat=lambda: "2026-09-22T00:00:00+00:00"),
        "first_seen_at": MagicMock(isoformat=lambda: "2026-09-22T00:00:00+00:00"),
        "resolved_at": None,
    }
    fake_conn = AsyncMock()
    fake_conn.fetchrow = AsyncMock(return_value=written)

    import asyncpg
    pool = MagicMock(spec=asyncpg.Pool)
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    out = await unmapped_market.record_unmapped_market_request(pool, uuid4(), "VN")
    assert out["country_code"] == "VN"
    assert out["resolved_at"] is None
    fake_conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_unmapped_market_request_accepts_a_live_connection():
    # slate.py's own use: it already holds a `conn` (inside TenantConfigService(conn)'s scope)
    # and must reuse it, not acquire a second one from the pool.
    from shared.dfs_client import unmapped_market

    written = {
        "id": uuid4(), "tenant_id": uuid4(), "country_code": "TH",
        "requested_at": MagicMock(isoformat=lambda: "2026-09-22T00:00:00+00:00"),
        "first_seen_at": MagicMock(isoformat=lambda: "2026-09-22T00:00:00+00:00"),
        "resolved_at": None,
    }
    fake_conn = AsyncMock()
    fake_conn.fetchrow = AsyncMock(return_value=written)

    out = await unmapped_market.record_unmapped_market_request(fake_conn, uuid4(), "TH")
    assert out["country_code"] == "TH"
    fake_conn.fetchrow.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_unmapped_market_requests_empty_is_the_healthy_state():
    from shared.dfs_client import unmapped_market

    fake_conn = AsyncMock()
    fake_conn.fetch = AsyncMock(return_value=[])

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    assert await unmapped_market.list_unmapped_market_requests(pool) == []


@pytest.mark.asyncio
async def test_list_unmapped_market_requests_groups_by_country():
    from shared.dfs_client import unmapped_market

    row = {
        "country_code": "VN", "tenant_count": 2,
        "first_seen_at": MagicMock(isoformat=lambda: "2026-09-20T00:00:00+00:00"),
        "last_requested_at": MagicMock(isoformat=lambda: "2026-09-22T00:00:00+00:00"),
        "tenant_ids": ["a", "b"],
    }
    fake_conn = AsyncMock()
    fake_conn.fetch = AsyncMock(return_value=[row])

    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    out = await unmapped_market.list_unmapped_market_requests(pool)
    assert len(out) == 1
    assert out[0]["country_code"] == "VN"
    assert out[0]["tenant_count"] == 2


# ── slate.py::_tenant_market_codes() — best-effort record, never breaks the read path ───────

@pytest.mark.asyncio
async def test_tenant_market_codes_records_when_all_unmatched():
    from services.acp_shared import slate

    tenant_id = uuid4()
    fake_conn = AsyncMock()

    fake_cfg = MagicMock(target_market={"countries": ["VN"]})
    fake_service = MagicMock()
    fake_service.get_seo_config = AsyncMock(return_value=fake_cfg)

    with patch("shared.services.tenant_config_service.TenantConfigService", return_value=fake_service), \
         patch("shared.dfs_client.unmapped_market.record_unmapped_market_request", new=AsyncMock()) as rec:
        codes, unmatched = await slate._tenant_market_codes(tenant_id, fake_conn)

    assert unmatched == ["VN"]
    assert codes == ["US"]  # resolve_buyer_markets() fallback, unchanged
    rec.assert_awaited_once_with(fake_conn, tenant_id, "VN")


@pytest.mark.asyncio
async def test_tenant_market_codes_no_record_when_tenant_declared_nothing():
    from services.acp_shared import slate

    tenant_id = uuid4()
    fake_conn = AsyncMock()

    fake_cfg = MagicMock(target_market={})
    fake_service = MagicMock()
    fake_service.get_seo_config = AsyncMock(return_value=fake_cfg)

    with patch("shared.services.tenant_config_service.TenantConfigService", return_value=fake_service), \
         patch("shared.dfs_client.unmapped_market.record_unmapped_market_request", new=AsyncMock()) as rec:
        codes, unmatched = await slate._tenant_market_codes(tenant_id, fake_conn)

    assert unmatched == []
    assert codes == ["US"]
    rec.assert_not_awaited()


@pytest.mark.asyncio
async def test_tenant_market_codes_record_failure_does_not_raise():
    # A DB hiccup writing the unmapped-market request must never break the tenant's own Slate
    # read — this is a best-effort side record, not a required step.
    from services.acp_shared import slate

    tenant_id = uuid4()
    fake_conn = AsyncMock()

    fake_cfg = MagicMock(target_market={"countries": ["VN"]})
    fake_service = MagicMock()
    fake_service.get_seo_config = AsyncMock(return_value=fake_cfg)

    with patch("shared.services.tenant_config_service.TenantConfigService", return_value=fake_service), \
         patch("shared.dfs_client.unmapped_market.record_unmapped_market_request",
               new=AsyncMock(side_effect=RuntimeError("db down"))):
        codes, unmatched = await slate._tenant_market_codes(tenant_id, fake_conn)

    assert unmatched == ["VN"]
    assert codes == ["US"]
