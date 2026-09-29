"""AA-646 follow-up (KAN-90, 29/09/2026) — a tenant T2 rewrite never buys DataForSEO: it reads the
cached seo_context row and, on a miss, rewrites without SEO keywords instead of calling
process_seo() (which was outside every spend budget)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.jobs import t2_rewrite_job as t2


def _pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


async def _run(seo_row):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=seo_row)
    rewrite = AsyncMock(return_value={"status": "failed", "error": "stop here"})
    with patch("services.seo_intelligence.handler.process_seo", AsyncMock()) as m_seo, \
         patch("api.routers.v1_pipeline._rewrite_tour", rewrite):
        with pytest.raises(RuntimeError, match="did not succeed"):
            await t2._rewrite_and_save(
                _pool(conn), {"tour_id": "tour-1"}, {"name": "Sapa", "country": "Vietnam"}, {},
                "tenant-1", "version-1", "p-1")
    return m_seo, rewrite.call_args.kwargs["seo"]


@pytest.mark.asyncio
async def test_missing_seo_context_never_calls_dataforseo():
    m_seo, seo = await _run(None)
    m_seo.assert_not_awaited()
    assert seo == {}


@pytest.mark.asyncio
async def test_cached_seo_context_is_passed_to_the_rewrite():
    m_seo, seo = await _run({"top_keywords": '["sapa trekking"]', "keyword_ideas": None,
                             "people_also_ask": None})
    m_seo.assert_not_awaited()
    assert seo["top_keywords"] == ["sapa trekking"]
    assert seo["keywords"] == {"top_keywords": ["sapa trekking"]}
