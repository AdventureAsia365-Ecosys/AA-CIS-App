"""AA-1055 PR 1 (S273 design section 3, decision 7) — T2 gets and is judged on fitting DFS keywords only.

1. `services/seo_intelligence/keyword_fit.py` holds the S1 v2 keyword filter (one function: `s1_writer_v2` re-exports
   it), plus the language gate.
2. `t2_seo` hands the T2 writer only keywords the source it rewrites covers and that are not transactional (at most
   5), and none for a non-English rewrite; PAA is kept; `diag` records what happened.
3. validate_node: on a T2 draft, DFS_INTENT_UNDERUSED fires only when a fitting keyword existed and the writer
   ignored it. A1 (S1) keeps the old rule.

Invented tours and places only. Mock-only: no DB, no DataForSEO, no LLM.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.content_generation import s1_writer_v2 as v2
from services.content_generation.graph import validate_node
from services.jobs import t2_rewrite_job as t2
from services.seo_intelligence import keyword_fit
from tests.unit.test_content_graph import make_state

TOUR = {
    "name": "Velmora Canyon Rafting Day",
    "summary": "A guided rafting day on the Velmora river through the canyon below Ostrik village.",
    "description": "Raft the Velmora canyon with a local guide, then walk up to Ostrik village for lunch.",
    "highlights": ["Rafting the Velmora canyon", "Lunch in Ostrik village"],
    "itineraries": "Day 1: Drive to the put-in, raft the canyon, walk to Ostrik village.",
    "country": "Kaldovia",
    "duration": "1 day",
}
SEO = {"top_keywords": [
    "velmora canyon rafting",            # covered
    "velmora rafting price",             # transactional
    "kaldovia scuba diving",             # not in the source
    {"keyword": "ostrik village"},       # covered, dict form
    "rafting kaldovia",                  # covered
    "velmora canyon rafting",            # duplicate
    "canyon rafting kaldovia",
    "velmora river rafting",
    "ostrik village walk",
]}


# ── 1. the shared filter ────────────────────────────────────────────────────────────────────────────────────────────

def test_s1_writer_v2_reexports_the_shared_function():
    assert v2.relevant_keywords is keyword_fit.relevant_keywords
    assert v2.MAX_KEYWORDS == keyword_fit.S1_MAX_KEYWORDS == 3


def test_relevant_keywords_keep_covered_non_transactional_in_dfs_order():
    assert keyword_fit.relevant_keywords(SEO, TOUR) == ["velmora canyon rafting", "ostrik village", "rafting kaldovia"]
    assert keyword_fit.relevant_keywords(SEO, TOUR, limit=5) == [
        "velmora canyon rafting", "ostrik village", "rafting kaldovia", "canyon rafting kaldovia",
        "velmora river rafting"]
    nested = {"keywords": {"top_keywords": ["ostrik village", "kaldovia scuba diving"]}}
    assert keyword_fit.relevant_keywords(nested, TOUR) == ["ostrik village"]
    assert keyword_fit.relevant_keywords(None, TOUR) == []


@pytest.mark.parametrize("language,english", [
    (None, True), ("", True), ("en-US", True), ("EN-US", True), ("en-GB", True), ("en", True), ("en_AU", True),
    ("English", True), ("de-DE", False), ("de", False), ("fr-FR", False), ("nl", False), ("vi-VN", False),
])
def test_is_english(language, english):
    assert keyword_fit.is_english(language) is english


def test_writer_keywords_block_a_non_english_rewrite():
    assert keyword_fit.writer_keywords(SEO, TOUR, "de-DE") == []
    assert keyword_fit.writer_keywords(SEO, TOUR, "en-GB") == keyword_fit.relevant_keywords(SEO, TOUR, limit=5)


# ── 2. t2_seo hands the writer fitting keywords only ────────────────────────────────────────────────────────────────

def _pool(conn):
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool


async def _t2_seo(market_seo, *, language="en-US", admin_row=None):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=admin_row)
    with patch("services.seo_intelligence.s1_prefetch.tenant_market_seo",
               AsyncMock(return_value=("UK", market_seo))):
        return await t2.t2_seo(_pool(conn), {"tour_id": "tour-1"}, "tenant-1", tour_dict=TOUR,
                               rewrite_language=language)


@pytest.mark.asyncio
async def test_t2_seo_keeps_only_fitting_keywords_and_the_paa():
    market = {**SEO, "keywords": {"top_keywords": SEO["top_keywords"]},
              "people_also_ask": ["Is the Velmora canyon safe to raft?"], "seo_market": "UK"}
    seo, diag = await _t2_seo(market)
    kept = ["velmora canyon rafting", "ostrik village", "rafting kaldovia", "canyon rafting kaldovia",
            "velmora river rafting"]
    assert seo["top_keywords"] == kept and seo["keywords"]["top_keywords"] == kept
    assert seo["people_also_ask"] == ["Is the Velmora canyon safe to raft?"]
    assert seo["seo_market"] == "UK"
    assert diag["seo_source"] == "tenant_market"
    assert diag["seo_keywords"] == {"in": len(SEO["top_keywords"]), "kept": 5, "language_blocked": False}


@pytest.mark.asyncio
async def test_t2_seo_gives_a_german_rewrite_no_dfs_keyword():
    market = {"top_keywords": ["velmora canyon rafting"], "keywords": {"top_keywords": ["velmora canyon rafting"]},
              "people_also_ask": ["How long is the Velmora rafting trip?"]}
    seo, diag = await _t2_seo(market, language="de-DE")
    assert seo["top_keywords"] == [] and seo["keywords"]["top_keywords"] == []
    assert seo["people_also_ask"] == ["How long is the Velmora rafting trip?"]
    assert diag["seo_keywords"] == {"in": 1, "kept": 0, "language_blocked": True}


@pytest.mark.asyncio
async def test_t2_seo_filters_the_admin_us_fallback_too():
    seo, diag = await _t2_seo(None, admin_row={"top_keywords": '["kaldovia scuba diving", "ostrik village"]',
                                               "keyword_ideas": None, "people_also_ask": None})
    assert diag["seo_source"] == "admin_us_fallback"
    assert seo["top_keywords"] == ["ostrik village"]


@pytest.mark.asyncio
async def test_t2_seo_with_nothing_cached_stays_empty():
    seo, diag = await _t2_seo(None)
    assert seo == {} and diag["seo_source"] == "none" and "seo_keywords" not in diag


@pytest.mark.asyncio
async def test_the_t2_job_passes_its_source_and_language_to_t2_seo():
    seen = {}

    async def fake_t2_seo(pool, pt, tenant_id, **kw):
        seen.update(kw)
        return {}, {"seo_source": "none", "market": None}

    rewrite = AsyncMock(return_value={"status": "failed", "error": "stop here"})
    with patch.object(t2, "t2_seo", fake_t2_seo), patch("api.routers.v1_pipeline._rewrite_tour", rewrite):
        with pytest.raises(RuntimeError, match="did not succeed"):
            await t2._rewrite_and_save(_pool(AsyncMock()), {"tour_id": "tour-1"}, TOUR,
                                       {"rewrite_language": "fr-FR"}, "tenant-1", "version-1", "p-1")
    assert seen == {"tour_dict": TOUR, "rewrite_language": "fr-FR"}


# ── 3. validate_node: the T2 penalty fires only on an ignored fitting keyword ───────────────────────────────────────

def _generated(seo_title, seo_meta):
    return {
        "name": "Velmora Canyon Rafting Day",
        "subtitle": "A river day below Ostrik village",
        "summary": "A guided rafting day on the Velmora river with lunch in Ostrik village afterwards.",
        "highlights": ["Rafting the Velmora canyon", "Lunch in Ostrik village", "A local river guide"],
        "itineraries": "Day 1: Drive to the put-in, raft the canyon, walk to Ostrik village.",
        "seo_title": seo_title,
        "seo_meta": seo_meta,
        "trip_type": "adventure",
    }


OFF_TOPIC = ("Kaldovia Highlands Walk | Hill Paths", "A quiet walk across the Kaldovia highlands with a guide, a picnic"
             " and wide views over the hill paths before an easy return in the afternoon.")
ON_TOPIC = ("Velmora Canyon Rafting Day | River Guide", "Raft the Velmora canyon with a local guide, then walk to"
            " Ostrik village for lunch on a full day on the river in Kaldovia with time to rest.")


def _codes(seo, generated, *, tenant=True, language="en-US"):
    state = make_state(tour=TOUR, seo=seo, generated=generated, is_tenant_rewrite=tenant, rewrite_language=language)
    return validate_node(state)["failure_codes"]


def test_t2_is_not_penalised_for_a_keyword_the_source_never_covers():
    seo = {"top_keywords": ["kaldovia scuba diving", "velmora rafting price"]}
    assert "DFS_INTENT_UNDERUSED" not in _codes(seo, _generated(*OFF_TOPIC))


def test_t2_is_penalised_when_it_ignores_a_fitting_keyword():
    seo = {"top_keywords": ["kaldovia scuba diving", "velmora canyon rafting"]}
    assert "DFS_INTENT_UNDERUSED" in _codes(seo, _generated(*OFF_TOPIC))
    assert "DFS_INTENT_UNDERUSED" not in _codes(seo, _generated(*ON_TOPIC))


def test_a_non_english_t2_rewrite_is_never_penalised_on_dfs_keywords():
    seo = {"top_keywords": ["velmora canyon rafting"]}
    assert "DFS_INTENT_UNDERUSED" not in _codes(seo, _generated(*OFF_TOPIC), language="de-DE")


def test_a1_keeps_the_old_rule():
    # S1 (not a tenant rewrite, no v2 draft): any DFS keyword counts, as before AA-1055
    seo = {"top_keywords": ["kaldovia scuba diving"]}
    assert "DFS_INTENT_UNDERUSED" in _codes(seo, _generated(*OFF_TOPIC), tenant=False)
