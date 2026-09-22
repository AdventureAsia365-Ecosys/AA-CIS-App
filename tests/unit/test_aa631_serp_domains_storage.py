"""AA-631 — segment_research.py's _store_serp_domains() + _serp_tool()'s single-call-feeds-
both-PAA-and-domains behavior. Pure-mocked-DB/DFS tests only."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_store_serp_domains_writes_and_invalidates():
    from services.acp_contract.segment_research import _store_serp_domains

    conn = AsyncMock()
    conn.execute = AsyncMock()

    with patch("services.acp_contract.atom_ranking.invalidate_contested_cache_for_keyword",
               new=AsyncMock(return_value=1)) as m_invalidate:
        await _store_serp_domains(conn, "sukhbaatar square", "US", ["wikipedia.org", "a.com"])

    conn.execute.assert_called_once()
    sql = conn.execute.call_args.args[0]
    assert "SET serp_domains" in sql
    m_invalidate.assert_awaited_once_with(conn, "sukhbaatar square")


@pytest.mark.asyncio
async def test_store_serp_domains_skips_when_empty():
    from services.acp_contract.segment_research import _store_serp_domains

    conn = AsyncMock()
    conn.execute = AsyncMock()

    with patch("services.acp_contract.atom_ranking.invalidate_contested_cache_for_keyword",
               new=AsyncMock()) as m_invalidate:
        await _store_serp_domains(conn, "kw", "US", [])

    conn.execute.assert_not_called()
    m_invalidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_serp_domains_invalidation_failure_does_not_raise():
    from services.acp_contract.segment_research import _store_serp_domains

    conn = AsyncMock()
    conn.execute = AsyncMock()

    with patch("services.acp_contract.atom_ranking.invalidate_contested_cache_for_keyword",
               new=AsyncMock(side_effect=RuntimeError("db down"))):
        await _store_serp_domains(conn, "kw", "US", ["a.com"])

    conn.execute.assert_called_once()  # the search_demand UPDATE still happened


@pytest.mark.asyncio
async def test_serp_tool_single_call_feeds_both_paa_and_domains():
    """AA-631's own cost-neutral claim: ONE _serp_advanced() call per market, feeding BOTH
    _store_paa() and _store_serp_domains() — not two separate DFS calls."""
    from services.acp_contract.segment_research import _serp_tool

    fake_serp = {"tasks": [{"result": [{"items": [
        {"type": "people_also_ask", "items": [{"title": "What is Sukhbaatar Square"}]},
        {"type": "organic", "domain": "wikipedia.org"},
    ]}]}]}

    client = AsyncMock()
    client._serp_advanced = AsyncMock(return_value=fake_serp)
    # _parse_paa/_parse_organic_domains are plain sync methods on the real client class —
    # exercise the REAL parsing logic here (not mocked) so this test also proves the single
    # response feeds both parsers correctly, not just that both are called.
    from services.seo_intelligence.dataforseo_client import DataForSEOClient
    real_client = DataForSEOClient(login="x", password="y")
    client._parse_paa = real_client._parse_paa
    client._parse_organic_domains = real_client._parse_organic_domains

    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("services.acp_contract.segment_research._store_paa", new=AsyncMock()) as m_paa, \
         patch("services.acp_contract.segment_research._store_serp_domains", new=AsyncMock()) as m_domains:
        seen = await _serp_tool(
            client, "sukhbaatar square", ["US"], {"US": (2840, "en")}, pool,
        )

    assert seen == ["What is Sukhbaatar Square"]
    client._serp_advanced.assert_awaited_once()  # exactly ONE DFS call for this market
    m_paa.assert_awaited_once_with(conn, "sukhbaatar square", "US", ["What is Sukhbaatar Square"])
    m_domains.assert_awaited_once_with(conn, "sukhbaatar square", "US", ["wikipedia.org"])


@pytest.mark.asyncio
async def test_serp_tool_handles_serp_advanced_failure_gracefully():
    from services.acp_contract.segment_research import _serp_tool

    client = AsyncMock()
    client._serp_advanced = AsyncMock(side_effect=RuntimeError("DFS down"))

    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("services.acp_contract.segment_research._store_paa", new=AsyncMock()) as m_paa, \
         patch("services.acp_contract.segment_research._store_serp_domains", new=AsyncMock()) as m_domains:
        seen = await _serp_tool(client, "kw", ["US"], {"US": (2840, "en")}, pool)

    assert seen == []
    m_paa.assert_awaited_once_with(conn, "kw", "US", [])
    m_domains.assert_awaited_once_with(conn, "kw", "US", [])
