"""
Unit tests for AA-155: S0 dedup detection + decision flow.

Tests:
- normalize_group_key: mixed-case, whitespace, None provider
- decide_staging: bypass removes row; replace supersedes old + inserts new active
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from services.ingestion.handler import normalize_group_key


# ── normalize_group_key ───────────────────────────────────────────────────────

def test_normalize_lowercase_strip():
    name, prov = normalize_group_key("  Vietnam Highlight Tour  ", "Horizon Voyages")
    assert name == "vietnam highlight tour"
    assert prov == "horizon voyages"


def test_normalize_already_lower():
    name, prov = normalize_group_key("mekong delta", "adventure asia")
    assert name == "mekong delta"
    assert prov == "adventure asia"


def test_normalize_none_provider():
    name, prov = normalize_group_key("Bali Classic", None)
    assert name == "bali classic"
    assert prov == ""


def test_normalize_empty_provider():
    name, prov = normalize_group_key("Tour A", "")
    assert name == "tour a"
    assert prov == ""


def test_normalize_mixed_case_provider():
    name, prov = normalize_group_key("Ha Long Bay", "SUNSET CRUISES")
    assert name == "ha long bay"
    assert prov == "sunset cruises"


def test_normalize_returns_tuple():
    result = normalize_group_key("test", "prov")
    assert isinstance(result, tuple)
    assert len(result) == 2


# ── decide_staging: bypass ────────────────────────────────────────────────────

# S241: decide_staging now checks the admin secret (it declared the header but never verified it).
_SECRET = "s241-test-secret"


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    from api.routers import admin
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)


def _make_conn(staging_row: dict):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=staging_row)
    conn.execute  = AsyncMock(return_value=None)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__  = AsyncMock(return_value=False)

    txn = AsyncMock()
    txn.__aenter__ = AsyncMock(return_value=None)
    txn.__aexit__  = AsyncMock(return_value=False)
    conn.transaction = MagicMock(return_value=txn)
    return conn


def _make_pool(conn):
    pool = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__  = AsyncMock(return_value=False)
    pool.acquire = MagicMock(return_value=ctx)
    return pool


def _make_request(pool):
    req = MagicMock()
    req.app.state.pool = pool
    return req


_STAGING_ROW = {
    "id": "aaaaaaaa-0000-0000-0000-000000000001",
    "batch_id": "bbbbbbbb-0000-0000-0000-000000000001",
    "tenant_id": "00000000-0000-0000-0000-000000000001",
    "parsed_payload": json.dumps({
        "src_name": "Ha Long Bay Tour", "provider": "Horizon Voyages",
        "country": "Vietnam", "price_raw": "1200",
    }),
    "matched_tour_id": "cccccccc-0000-0000-0000-000000000001",
    "matched_source_group_id": "dddddddd-0000-0000-0000-000000000001",
}


@pytest.mark.asyncio
async def test_bypass_deletes_staging_row():
    from api.routers.admin_pipeline import decide_staging, DecideRequest
    conn = _make_conn(_STAGING_ROW)
    pool = _make_pool(conn)
    body = DecideRequest(decision="bypass")
    result = await decide_staging("aaaaaaaa-0000-0000-0000-000000000001", body, _make_request(pool),
                                  x_admin_secret=_SECRET)
    assert result["decision"] == "bypass"
    assert result["committed"] is False
    # DELETE must have been called
    conn.execute.assert_awaited_once()
    call_sql = conn.execute.call_args[0][0]
    assert "DELETE" in call_sql.upper()


@pytest.mark.asyncio
async def test_bypass_invalid_decision_raises_400():
    from api.routers.admin_pipeline import decide_staging, DecideRequest
    from fastapi import HTTPException
    conn = _make_conn(_STAGING_ROW)
    pool = _make_pool(conn)
    body = DecideRequest(decision="unknown_action")
    with pytest.raises(HTTPException) as exc:
        await decide_staging("aaaaaaaa-0000-0000-0000-000000000001", body, _make_request(pool), x_admin_secret=_SECRET)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_staging_not_found_raises_404():
    from api.routers.admin_pipeline import decide_staging, DecideRequest
    from fastapi import HTTPException
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__  = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    body = DecideRequest(decision="bypass")
    with pytest.raises(HTTPException) as exc:
        await decide_staging("nonexistent", body, _make_request(pool), x_admin_secret=_SECRET)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_decide_staging_without_secret_is_403_and_touches_nothing():
    """S241 — the hole: no X-Admin-Secret used to reach the DB write."""
    from api.routers.admin_pipeline import decide_staging, DecideRequest
    from fastapi import HTTPException
    conn = _make_conn(_STAGING_ROW)
    pool = _make_pool(conn)
    with pytest.raises(HTTPException) as exc:
        await decide_staging("aaaaaaaa-0000-0000-0000-000000000001", DecideRequest(decision="bypass"),
                             _make_request(pool), x_admin_secret=None)
    assert exc.value.status_code == 403
    pool.acquire.assert_not_called()
