"""AA-293: _revalidate_tour's own asyncpg.connect() has no jsonb type codec
registered (same as every connection in this app — none registers one), so jsonb
columns (aa_highlights, seo_keywords_used, og_tags, metadata, keyword_ideas,
top_keywords) come back as raw JSON text instead of list/dict. This silently broke
downstream validate_node checks reading through /revalidate — e.g. HIGHLIGHTS_NOT_LIST
false-fired on every tour (isinstance(str, list) is False) and DFS_INTENT_UNDERUSED's
keyword loop iterated the raw string character-by-character instead of its keywords.

Fix: explicit per-field json.loads() via a local _jsonb() helper (same idiom as
admin_settings.py's _jsonb()), not a connection-level codec — this same connection
also binds json.dumps(...) params to ::jsonb columns in the INSERT below it, and a
codec's encoder would double-encode those already-dumped strings.

These tests call the real _revalidate_tour with a faked asyncpg connection (fetchrow
returns jsonb columns as raw JSON strings, mirroring production with no codec) and
capture the state actually built and handed to the revalidation graph.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import admin_pipeline

FAKE_UUID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _db_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")


def _gc_row():
    return {
        "id": FAKE_UUID, "tour_id": FAKE_UUID, "tenant_id": FAKE_UUID, "version_num": 1,
        "aa_name": "Test Tour", "aa_subtitle": "A test subtitle",
        "aa_summary": "A test summary", "aa_description": "A test description",
        # jsonb columns as raw JSON TEXT — this is what asyncpg returns without a codec
        "aa_highlights": json.dumps(["Highlight one", "Highlight two", "Highlight three"]),
        "aa_itineraries": "Day 1 -- Arrive",   # TEXT column, never jsonb — must pass through untouched
        "mobile_card_text": None,
        "seo_title": "Test Tour | Adventure Asia", "seo_meta": "m" * 150,
        "seo_keywords_used": json.dumps(["vietnam tours", "hiking"]),
        "og_tags": json.dumps({"og:title": "Test Tour"}),
        "brand_rules_version": 1,
        "metadata": json.dumps({"brand_name": "AA"}),
        "src_name": "Test Tour", "country": "Vietnam", "duration": "5 days",
        # AA-741 (S229): the source the edit is judged against
        "src_subtitle": "Src subtitle", "src_summary": "Src summary", "src_description": None,
        "src_highlights": json.dumps(["Src highlight"]),
        "src_itineraries": "Day 1: Arrive. Dinner at the lodge.", "price_raw": None,
        "inclusions": "Breakfast and dinner daily", "exclusions": None,
    }


def _seo_row():
    return {
        "keyword_search": "Vietnam tours", "provider": "dataforseo",
        "keyword_ideas": json.dumps([{"keyword": "vietnam tours", "search_volume": 100}]),
        "top_keywords": json.dumps(["vietnam tours", "hiking vietnam"]),
    }


def _fake_graph_result():
    return {
        "revalidate_passed": True, "quality_score": 9.0, "sub_scores": {},
        "failure_codes": [], "passed_count": 4, "failed_count": 0,
        "brand_audit_status": "pass", "brand_audit_codes": [], "brand_audit_issues": [],
        "brand_audit_fields": [], "lessons_extracted": [],
    }


def _run(gc_row=None, seo_row=None):
    """Runs the real _revalidate_tour with I/O faked; returns the initial_state
    actually handed to graph.invoke() (captured via the mocked graph)."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [gc_row or _gc_row(), seo_row or _seo_row()]
    conn.execute = AsyncMock()

    captured = {}
    fake_graph = MagicMock()
    fake_graph.invoke.side_effect = lambda state: (captured.update(state=state), _fake_graph_result())[1]

    with patch("api.routers.admin_pipeline.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("api.routers.admin_pipeline._resolve_brand_rule", AsyncMock(return_value=None)), \
         patch("services.content_generation.graph.build_revalidation_graph",
               MagicMock(return_value=fake_graph)):
        asyncio.run(admin_pipeline._revalidate_tour(FAKE_UUID))

    return captured["state"]


def test_highlights_decoded_to_list_not_string():
    state = _run()
    highlights = state["generated"]["highlights"]
    assert isinstance(highlights, list), f"expected list, got {type(highlights).__name__}: {highlights!r}"
    assert highlights == ["Highlight one", "Highlight two", "Highlight three"]


def test_seo_keywords_used_decoded_to_list():
    state = _run()
    assert state["generated"]["seo_keywords_used"] == ["vietnam tours", "hiking"]


def test_og_tags_decoded_to_dict():
    state = _run()
    assert state["generated"]["og_tags"] == {"og:title": "Test Tour"}


def test_top_keywords_decoded_to_list_not_iterated_as_chars():
    state = _run()
    top_kws = state["seo"]["top_keywords"]
    assert isinstance(top_kws, list)
    assert top_kws == ["vietnam tours", "hiking vietnam"]
    # regression guard: a raw string would silently iterate as single characters
    assert all(len(k) > 1 for k in top_kws)


def test_keyword_ideas_decoded_to_list_of_dicts():
    state = _run()
    ideas = state["seo"]["keyword_ideas"]
    assert isinstance(ideas, list)
    assert ideas[0]["keyword"] == "vietnam tours"


def test_itineraries_text_column_passed_through_unchanged():
    # aa_itineraries is TEXT, not jsonb — must NOT be run through json.loads.
    state = _run()
    assert state["generated"]["itineraries"] == "Day 1 -- Arrive"


def test_metadata_brand_name_resolved_from_decoded_dict():
    # Exercises the pre-existing metadata decode path (kept working, not regressed):
    # _resolve_brand_rule is called with brand_name parsed OUT of jsonb metadata.
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_gc_row(), _seo_row()]
    conn.execute = AsyncMock()
    resolve_mock = AsyncMock(return_value=None)
    fake_graph = MagicMock()
    fake_graph.invoke.return_value = _fake_graph_result()

    with patch("api.routers.admin_pipeline.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("api.routers.admin_pipeline._resolve_brand_rule", resolve_mock), \
         patch("services.content_generation.graph.build_revalidation_graph",
               MagicMock(return_value=fake_graph)):
        asyncio.run(admin_pipeline._revalidate_tour(FAKE_UUID))

    # _resolve_brand_rule(conn, tenant_uuid, None, brand_name) — brand_name is arg index 3
    called_brand_name = resolve_mock.call_args[0][3]
    assert called_brand_name == "AA"


def test_missing_seo_context_row_yields_empty_seo_without_crash():
    # seo_row is None (no seo_context row) — must not KeyError/AttributeError.
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_gc_row(), None]
    conn.execute = AsyncMock()
    fake_graph = MagicMock()
    captured = {}
    fake_graph.invoke.side_effect = lambda state: (captured.update(state=state), _fake_graph_result())[1]

    with patch("api.routers.admin_pipeline.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("api.routers.admin_pipeline._resolve_brand_rule", AsyncMock(return_value=None)), \
         patch("services.content_generation.graph.build_revalidation_graph",
               MagicMock(return_value=fake_graph)):
        asyncio.run(admin_pipeline._revalidate_tour(FAKE_UUID))

    assert captured["state"]["seo"] == {}


def test_source_tour_is_passed_to_the_graph():
    """AA-741 (S229): before, `tour` had name/country/duration only, so every source-grounded
    check (judge, brand audit, A1-3 meal/time) judged the edit against an EMPTY source."""
    tour = _run()["tour"]
    assert tour["itineraries"] == "Day 1: Arrive. Dinner at the lodge."
    assert tour["inclusions"] == "Breakfast and dinner daily"
    assert tour["summary"] == "Src summary"
    assert tour["highlights"] == ["Src highlight"]
