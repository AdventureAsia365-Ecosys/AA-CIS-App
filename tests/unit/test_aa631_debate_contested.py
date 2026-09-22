"""AA-631 (Debate standard #1, `contested`) — everywhere_domains()/compute_contested() (pure),
invalidate_contested_cache_for_keyword() (asyncpg mocked), and dataforseo_client's organic-
domain parsing (_parse_organic_domains/fetch_organic_domains). No live DB, no live DFS."""
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── everywhere_domains() ──────────────────────────────────────────────────────

def test_everywhere_domains_empty_input():
    from services.acp_contract.atom_ranking import everywhere_domains
    assert everywhere_domains([]) == set()


def test_everywhere_domains_default_threshold_needs_majority():
    from services.acp_contract.atom_ranking import everywhere_domains
    # wikipedia.org in 3/4 rows (0.75 >= 0.5); niche.com in 1/4 (0.25 < 0.5)
    rows = [
        ["wikipedia.org", "a.com"],
        ["wikipedia.org", "b.com"],
        ["wikipedia.org", "niche.com"],
        ["c.com", "d.com"],
    ]
    assert everywhere_domains(rows) == {"wikipedia.org"}


def test_everywhere_domains_dedupes_within_one_row():
    from services.acp_contract.atom_ranking import everywhere_domains
    # wikipedia.org appears TWICE in row 1 but must count once for that row's own membership —
    # if it counted twice, a 3-row sample could show a domain "appearing" more times than rows
    # exist. Both domains here are the ONLY member of their own row (1/2 rows = 0.5 each), so
    # both meet a 0.5 threshold; a stricter 0.6 threshold excludes both.
    rows = [["wikipedia.org", "wikipedia.org"], ["x.com"]]
    assert everywhere_domains(rows, threshold=0.5) == {"wikipedia.org", "x.com"}
    assert everywhere_domains(rows, threshold=0.6) == set()


def test_everywhere_domains_custom_threshold():
    from services.acp_contract.atom_ranking import everywhere_domains
    rows = [["a.com"], ["a.com"], ["a.com"], ["b.com"]]
    assert everywhere_domains(rows, threshold=0.2) == {"a.com", "b.com"}
    assert everywhere_domains(rows, threshold=0.5) == {"a.com"}


# ── compute_contested() ────────────────────────────────────────────────────

def test_compute_contested_empty_serp_is_zero():
    from services.acp_contract.atom_ranking import compute_contested
    assert compute_contested([], {"wikipedia.org"}) == 0.0


def test_compute_contested_no_everywhere_domain_is_zero():
    from services.acp_contract.atom_ranking import compute_contested
    assert compute_contested(["a.com", "b.com"], {"wikipedia.org"}) == 0.0


def test_compute_contested_fully_dominated_is_one():
    from services.acp_contract.atom_ranking import compute_contested
    ew = {"wikipedia.org", "tripadvisor.com"}
    assert compute_contested(["wikipedia.org", "tripadvisor.com"], ew) == 1.0


def test_compute_contested_partial_share():
    from services.acp_contract.atom_ranking import compute_contested
    ew = {"wikipedia.org"}
    # 2 of 4 domains are "everywhere"
    assert compute_contested(["wikipedia.org", "a.com", "wikipedia.org", "b.com"], ew) == 0.5


# ── invalidate_contested_cache_for_keyword() — asyncpg mocked ───────────────


@pytest.mark.asyncio
async def test_invalidate_contested_sets_null_for_claiming_segment():
    from services.acp_contract.atom_ranking import invalidate_contested_cache_for_keyword

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        {"segment_id": "seg-1", "canonical_place": "Sukhbaatar Square", "canonical_action": "visit"},
        {"segment_id": "seg-2", "canonical_place": "Gandan Monastery", "canonical_action": "visit"},
    ])
    conn.execute = AsyncMock()

    count = await invalidate_contested_cache_for_keyword(conn, "sukhbaatar square")

    assert count == 1
    conn.execute.assert_called_once()
    sql, stale_ids = conn.execute.call_args.args[0], conn.execute.call_args.args[1]
    assert "SET contested = NULL" in sql
    assert stale_ids == ["seg-1"]


@pytest.mark.asyncio
async def test_invalidate_contested_no_claiming_segment_is_noop():
    from services.acp_contract.atom_ranking import invalidate_contested_cache_for_keyword

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[
        {"segment_id": "seg-1", "canonical_place": "Gandan Monastery", "canonical_action": "visit"},
    ])
    conn.execute = AsyncMock()

    count = await invalidate_contested_cache_for_keyword(conn, "sukhbaatar square")
    assert count == 0
    conn.execute.assert_not_called()


@pytest.mark.asyncio
async def test_invalidate_contested_only_queries_cached_segments():
    from services.acp_contract.atom_ranking import invalidate_contested_cache_for_keyword

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()

    await invalidate_contested_cache_for_keyword(conn, "anything")
    fetch_sql = conn.fetch.call_args.args[0]
    assert "contested IS NOT NULL" in fetch_sql


# ── dataforseo_client._parse_organic_domains / fetch_organic_domains ───────


def _client():
    from services.seo_intelligence.dataforseo_client import DataForSEOClient
    return DataForSEOClient(login="x", password="y")


def test_parse_organic_domains_reads_only_organic_items():
    c = _client()
    resp = {"tasks": [{"result": [{"items": [
        {"type": "organic", "domain": "wikipedia.org"},
        {"type": "people_also_ask", "items": [{"title": "not a domain"}]},
        {"type": "organic", "domain": "tripadvisor.com"},
        {"type": "related_searches"},
    ]}]}]}
    assert c._parse_organic_domains(resp) == ["wikipedia.org", "tripadvisor.com"]


def test_parse_organic_domains_keeps_duplicates_in_rank_order():
    c = _client()
    resp = {"tasks": [{"result": [{"items": [
        {"type": "organic", "domain": "a.com"},
        {"type": "organic", "domain": "a.com"},
        {"type": "organic", "domain": "b.com"},
    ]}]}]}
    assert c._parse_organic_domains(resp) == ["a.com", "a.com", "b.com"]


def test_parse_organic_domains_empty_on_bad_shape():
    c = _client()
    assert c._parse_organic_domains({}) == []
    assert c._parse_organic_domains({"tasks": []}) == []
    assert c._parse_organic_domains(None) == []


def test_parse_organic_domains_skips_items_without_domain():
    c = _client()
    resp = {"tasks": [{"result": [{"items": [
        {"type": "organic"},  # no domain field
        {"type": "organic", "domain": "real.com"},
    ]}]}]}
    assert c._parse_organic_domains(resp) == ["real.com"]


@pytest.mark.asyncio
async def test_fetch_organic_domains_calls_serp_advanced_and_parses():
    c = _client()
    fake_serp_response = {"tasks": [{"result": [{"items": [
        {"type": "organic", "domain": "wikipedia.org"},
    ]}]}]}
    c._serp_advanced = AsyncMock(return_value=fake_serp_response)

    domains = await c.fetch_organic_domains("sukhbaatar square", 2344, "en")

    assert domains == ["wikipedia.org"]
    c._serp_advanced.assert_awaited_once_with("sukhbaatar square", 2344, "en")
