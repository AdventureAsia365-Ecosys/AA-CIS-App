"""AA-739 — Review Queue: server-side paging/filters/facets and a block reason ahead of the score."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from api.routers import admin_pipeline as ap


def test_block_needs_human_for_fact_check_even_with_high_score():
    b = ap._review_block(8.0, ["FACT_CHECK_MANUAL_CHECK"], [], "manual_check")
    assert b["kind"] == "needs_human" and "elephant" in b["label"]


def test_block_hard_code_names_the_field():
    b = ap._review_block(8.0, ["SEO_META_TOO_LONG"], [{"code": "SEO_META_TOO_LONG", "field": "seo_meta"}], "fixed")
    assert b == {"kind": "hard", "label": "Blocked: SEO_META_TOO_LONG in seo_meta"}


def test_block_low_quality_when_no_hard_code():
    assert ap._review_block(6.0, [], [], "pass")["kind"] == "low_quality"


def test_block_soft_code_only_is_other():
    assert ap._review_block(7.5, ["DFS_INTENT_UNDERUSED"], [], "pass")["kind"] == "other"


def _run(**query):
    conn = MagicMock()
    conn.fetch = AsyncMock(side_effect=[[], [{"country": "India", "n": 33}, {"country": "Nepal", "n": 3}]])
    conn.fetchval = AsyncMock(side_effect=[36, '["explore"]'])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    request = MagicMock()
    request.app.state.pool.acquire = MagicMock(return_value=cm)
    with patch.object(ap, "verify_admin_secret", lambda _s: None):
        out = asyncio.run(ap.admin_review_queue(request, x_admin_secret="x", **query))
    return out, conn


def test_country_and_score_filters_go_to_sql_and_facets_ignore_them():
    out, conn = _run(page=2, page_size=100, country="India", score="low")
    rows_sql, rows_args = conn.fetch.call_args_list[0].args[0], conn.fetch.call_args_list[0].args[1:]
    assert "rt.country = $3" in rows_sql and "rq.score_overall >= 5 AND rq.score_overall < 7" in rows_sql
    assert "LIMIT 100 OFFSET 100" in rows_sql
    assert rows_args[-1] == "India"
    facet_sql = conn.fetch.call_args_list[1].args[0]
    assert "rt.country = $" not in facet_sql            # facets list every country
    assert out["facets"]["countries"] == [{"country": "India", "n": 33}, {"country": "Nepal", "n": 3}]
    assert out["pagination"] == {"page": 2, "page_size": 100, "total": 36}


def test_page_size_is_clamped():
    out, conn = _run(page_size=5000)
    assert out["pagination"]["page_size"] == ap._REVIEW_MAX_PAGE_SIZE
