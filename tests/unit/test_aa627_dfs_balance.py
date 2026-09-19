"""AA-627 (epic AA-616) — DataForSEO account balance daily check + low-balance alert.

Covers: DataForSEOClient._parse_money / fetch_balance parsing (httpx mocked, no live DFS),
balance snapshot writer (asyncpg mocked, no live DB), and the low-balance alert throttle helper
_maybe_alert_low_balance (mocked conn). No live DB / no live DFS.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── _parse_money / fetch_balance ────────────────────────────────────────────

def _client():
    from services.seo_intelligence.dataforseo_client import DataForSEOClient
    return DataForSEOClient(login="x", password="y")


def test_parse_money_reads_money_block():
    c = _client()
    resp = {"tasks": [{"result": [{"money": {"balance": 49.96, "total": 201.0, "currency": "USD"}}]}]}
    money = c._parse_money(resp)
    assert money["balance"] == 49.96
    assert money["currency"] == "USD"


def test_parse_money_empty_on_bad_shape():
    c = _client()
    assert c._parse_money({}) == {}
    assert c._parse_money({"tasks": []}) == {}
    assert c._parse_money({"tasks": [{"result": []}]}) == {}
    assert c._parse_money(None) == {}


@pytest.mark.asyncio
async def test_fetch_balance_gets_user_data_and_parses():
    c = _client()
    payload = {"cost": 0, "tasks": [{"result": [{"money": {"balance": 12.34, "currency": "USD"}}]}]}

    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json = MagicMock(return_value=payload)
    fake_http = AsyncMock()
    fake_http.get = AsyncMock(return_value=fake_resp)
    fake_ctx = MagicMock()
    fake_ctx.__aenter__ = AsyncMock(return_value=fake_http)
    fake_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("services.seo_intelligence.dataforseo_client.httpx.AsyncClient", return_value=fake_ctx), \
         patch.object(c, "_log_live"):
        money = await c.fetch_balance()

    # GET (not POST) to the free appendix/user_data endpoint
    called_url = fake_http.get.call_args.args[0]
    assert called_url.endswith("/appendix/user_data")
    assert money["balance"] == 12.34


# ── balance snapshot writer ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_balance_snapshot_binds_fields_and_returns_row():
    from shared.dfs_client import balance

    written = {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "balance_usd": 5.0, "currency": "USD", "below_threshold": True,
        "threshold_usd": 10.0, "fetched_at": MagicMock(isoformat=lambda: "2026-09-19T00:00:00+00:00"),
    }
    fake_conn = AsyncMock()
    fake_conn.fetchrow = AsyncMock(return_value=written)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    out = await balance.record_balance_snapshot(
        pool, balance_usd=5.0, currency="USD", below_threshold=True,
        threshold_usd=10.0, raw={"balance": 5.0},
    )
    args = fake_conn.fetchrow.call_args.args
    # bind order: SQL, balance_usd, currency, below_threshold, threshold_usd, raw
    assert args[1] == 5.0
    assert args[2] == "USD"
    assert args[3] is True
    assert args[4] == 10.0
    assert out["balance_usd"] == 5.0
    assert out["below_threshold"] is True


@pytest.mark.asyncio
async def test_read_latest_balance_none_when_empty():
    from shared.dfs_client import balance

    fake_conn = AsyncMock()
    fake_conn.fetchrow = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=fake_conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    assert await balance.read_latest_balance(pool) is None


# ── low-balance alert throttle ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alert_inserts_when_none_recent():
    from api.routers import admin_llm_ops

    fake_conn = AsyncMock()
    fake_conn.fetchval = AsyncMock(return_value=None)   # no unread alert in last 24h
    fake_conn.execute = AsyncMock()
    inserted = await admin_llm_ops._maybe_alert_low_balance(fake_conn, balance_usd=2.0, threshold=10.0)
    assert inserted is True
    fake_conn.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_alert_throttled_when_recent_exists():
    from api.routers import admin_llm_ops

    fake_conn = AsyncMock()
    fake_conn.fetchval = AsyncMock(return_value=1)      # an unread alert already exists
    fake_conn.execute = AsyncMock()
    inserted = await admin_llm_ops._maybe_alert_low_balance(fake_conn, balance_usd=2.0, threshold=10.0)
    assert inserted is False
    fake_conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_alert_swallows_db_error():
    from api.routers import admin_llm_ops

    fake_conn = AsyncMock()
    fake_conn.fetchval = AsyncMock(side_effect=RuntimeError("db down"))
    # must not raise; returns False
    assert await admin_llm_ops._maybe_alert_low_balance(fake_conn, balance_usd=2.0, threshold=10.0) is False
