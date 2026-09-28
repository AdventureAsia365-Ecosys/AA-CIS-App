"""AA-646 + AA-647 — DataForSEO research is admin-scoped, and a failed DFS call is never cached.

25/09/2026: one tenant rewrite researched 2,215 places x 3 markets ($49), the DFS balance ran out
mid-run, and the failures were cached as NULL volume for 182 days (14,691 rows). Mock-only tests:
no DB, no network.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.acp_contract import segment_research as sr
from services.seo_intelligence import dataforseo_client as dfs
from shared.llm_client.models import LLMResponse


# ── DataForSEO client: failures are distinguishable from "no data" ─────────────────────────────

def _fake_httpx(response=None, exc=None):
    client = MagicMock()
    if exc is not None:
        client.post = AsyncMock(side_effect=exc)
    else:
        client.post = AsyncMock(return_value=response)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


def _json_response(body: dict):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=body)
    return resp


def _http_error(status: int):
    req = httpx.Request("POST", "https://api.dataforseo.com/v3/x")
    return httpx.HTTPStatusError(str(status), request=req, response=httpx.Response(status, request=req))


OK_BODY = {"status_code": 20000, "tasks": [{"status_code": 20000,
                                            "result": [{"keyword": "kyoto", "search_volume": 1000}]}]}
PAYMENT_BODY = {"status_code": 20000, "tasks": [{"status_code": 40200,
                                                 "status_message": "Payment Required.", "result": None}]}


def test_status_error_none_for_success():
    assert dfs._dfs_status_error(OK_BODY) is None


def test_status_error_payment_is_fatal():
    err = dfs._dfs_status_error(PAYMENT_BODY)
    assert err is not None and err.fatal and err.status_code == 40200


def test_status_error_other_code_not_fatal():
    err = dfs._dfs_status_error({"status_code": 20000, "tasks": [{"status_code": 50000}]})
    assert err is not None and not err.fatal


@pytest.mark.parametrize("status,fatal", [(402, True), (401, True), (500, False)])
def test_as_dfs_error_classifies_http_status(status, fatal):
    assert dfs.as_dfs_error(_http_error(status)).fatal is fatal


@pytest.mark.asyncio
async def test_volumes_bulk_raises_on_http_402_when_asked():
    client = dfs.DataForSEOClient(login="x", password="y")
    with patch.object(dfs.httpx, "AsyncClient", _fake_httpx(exc=_http_error(402))):
        with pytest.raises(dfs.DFSCallError) as info:
            await client.fetch_volumes_bulk(["kyoto"], raise_on_error=True)
    assert info.value.fatal


@pytest.mark.asyncio
async def test_volumes_bulk_raises_on_200_with_payment_status():
    client = dfs.DataForSEOClient(login="x", password="y")
    with patch.object(dfs.httpx, "AsyncClient", _fake_httpx(_json_response(PAYMENT_BODY))), \
         patch.object(dfs, "record_dfs_call_sync"):
        with pytest.raises(dfs.DFSCallError) as info:
            await client.fetch_volumes_bulk(["kyoto"], raise_on_error=True)
    assert info.value.fatal and info.value.status_code == 40200


@pytest.mark.asyncio
async def test_volumes_bulk_default_mode_unchanged():
    """Other callers keep the old swallow-and-map-to-None behaviour."""
    client = dfs.DataForSEOClient(login="x", password="y")
    with patch.object(dfs.httpx, "AsyncClient", _fake_httpx(exc=_http_error(402))):
        assert await client.fetch_volumes_bulk(["kyoto"]) == {"kyoto": None}


@pytest.mark.asyncio
async def test_volumes_bulk_success_parses_rows():
    client = dfs.DataForSEOClient(login="x", password="y")
    with patch.object(dfs.httpx, "AsyncClient", _fake_httpx(_json_response(OK_BODY))), \
         patch.object(dfs, "record_dfs_call_sync"):
        assert await client.fetch_volumes_bulk(["kyoto", "nara"], raise_on_error=True) == \
            {"kyoto": 1000, "nara": None}


# ── research loop: failures are not cached, fatal errors abort the run ─────────────────────────

def _pool():
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)   # _cached_volume: nothing cached
    conn.fetch = AsyncMock(return_value=[])
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    return pool, conn


def _resp(content: str) -> LLMResponse:
    return LLMResponse(content=content, model_used="haiku-4-5", provider="bedrock",
                       input_tokens=10, output_tokens=5, cost_usd=0.0001)


def _research_log_writes(conn) -> list:
    return [c for c in conn.execute.call_args_list if "segment_research_log" in c.args[0]]


def _search_demand_writes(conn) -> list:
    return [c for c in conn.execute.call_args_list if "INSERT INTO acp_contract.search_demand" in c.args[0]]


@pytest.mark.asyncio
async def test_failed_volume_purchase_is_not_cached_and_place_not_marked_researched():
    pool, conn = _pool()
    guard = sr._RunGuard()
    client = MagicMock()
    client.fetch_volumes_bulk = AsyncMock(side_effect=dfs.DFSCallError("Payment Required", fatal=True,
                                                                         status_code=40200))
    batchers = {"US": sr._VolumeBatcher(client, 2840, "en", linger=0, guard=guard)}
    fake_llm = MagicMock()
    fake_llm.generate.side_effect = [_resp('{"thought": "t", "tool": "volumes", "keywords": ["kyoto"]}')]

    with patch.object(sr, "LLMClient", return_value=fake_llm), \
         patch.object(sr, "record_call_with_pool", new=AsyncMock()):
        result = await sr._research_place(
            "kyoto", ["explore"], ["US"], [(2840, "United States", "en")], batchers, client,
            pool, asyncio.Semaphore(1), guard,
        )

    assert result.failed
    assert _search_demand_writes(conn) == []      # no NULL cached as "fresh"
    assert _research_log_writes(conn) == []       # place stays stale → retried next run
    assert guard.aborted and "Payment Required" in guard.reason


@pytest.mark.asyncio
async def test_aborted_run_skips_place_before_any_llm_call():
    pool, conn = _pool()
    guard = sr._RunGuard(aborted=True, reason="Payment Required")
    fake_llm = MagicMock()

    with patch.object(sr, "LLMClient", return_value=fake_llm):
        result = await sr._research_place(
            "nara", ["explore"], ["US"], [(2840, "United States", "en")], {}, MagicMock(),
            pool, asyncio.Semaphore(1), guard,
        )

    assert result.skipped and result.failed and result.llm_calls == 0
    fake_llm.generate.assert_not_called()
    assert _research_log_writes(conn) == []


@pytest.mark.asyncio
async def test_successful_place_is_still_marked_researched():
    pool, conn = _pool()
    fake_llm = MagicMock()
    fake_llm.generate.side_effect = [_resp('{"thought": "t", "tool": "done", "keywords": []}')]

    with patch.object(sr, "LLMClient", return_value=fake_llm), \
         patch.object(sr, "record_call_with_pool", new=AsyncMock()):
        result = await sr._research_place(
            "kyoto", ["explore"], ["US"], [(2840, "United States", "en")], {}, MagicMock(),
            pool, asyncio.Semaphore(1),
        )

    assert not result.failed
    assert len(_research_log_writes(conn)) == 1


@pytest.mark.asyncio
async def test_non_fatal_error_fails_place_but_does_not_abort_run():
    guard = sr._RunGuard()
    guard.record(dfs.DFSCallError("timeout", fatal=False))
    assert not guard.aborted


@pytest.mark.asyncio
async def test_suggestions_failure_raises_place_failed():
    client = MagicMock()
    client.fetch_keyword_ideas = AsyncMock(side_effect=dfs.DFSCallError("boom"))
    with pytest.raises(sr.PlacePurchaseFailed):
        await sr._suggestions_tool(client, "kyoto", 2840, "en", sr._RunGuard())


# ── run_segment_research: scope + cap + dry run (AA-646) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_dry_run_makes_no_dfs_or_llm_call_and_applies_cap():
    pool, conn = _pool()
    conn.fetch = AsyncMock(side_effect=[
        [{"canonical_place": p, "canonical_action": "explore"} for p in ("a", "b", "c")],  # scope
        [], [], [],                                                                          # stale x3
    ])
    with patch.object(sr, "DataForSEOClient") as m_client, patch.object(sr, "LLMClient") as m_llm:
        result = await sr.run_segment_research({"countries": ["US"]}, pool, max_places=2, dry_run=True)

    m_client.assert_not_called()
    m_llm.assert_not_called()
    assert result["places_in_scope"] == 3
    assert result["places_stale"] == 3
    assert result["places_selected"] == 2
    assert result["places_researched"] == 0


@pytest.mark.asyncio
async def test_country_scope_uses_scoped_query():
    pool, conn = _pool()
    conn.fetch = AsyncMock(return_value=[])
    await sr.run_segment_research({"countries": ["US"]}, pool, country="Bhutan", max_places=5, dry_run=True)
    sql, tour_ids, country = conn.fetch.await_args_list[0].args
    assert "atom_segment_member" in sql and "raw_tours" in sql
    assert tour_ids is None and country == "Bhutan"


# ── admin endpoint validation ─────────────────────────────────────────────────────────────────

def test_admin_scope_rejects_unknown_market_and_uncapped_runs():
    from pydantic import ValidationError

    from api.routers.admin_segment_research import MAX_PLACES_PER_RUN, ResearchScope

    assert ResearchScope(markets=["us"], max_places=10).markets == ["US"]
    with pytest.raises(ValidationError):
        ResearchScope(markets=["XX"], max_places=10)
    with pytest.raises(ValidationError):
        ResearchScope(markets=["US"], max_places=MAX_PLACES_PER_RUN + 1)
    with pytest.raises(ValidationError):
        ResearchScope(markets=["US"])  # max_places is mandatory
