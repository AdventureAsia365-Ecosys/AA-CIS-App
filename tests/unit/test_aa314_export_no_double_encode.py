"""AA-314: services/export/handler.py::process_export() re-json.dumps()'d
aa_highlights/seo_keywords_used/og_tags even though asyncpg already hands them back
as JSON-encoded str (no jsonb codec registered anywhere in this app — same root
cause as AA-293). That wrapped an extra layer of JSON-string encoding around all
three columns on every export — confirmed live: 47/48 gold_aa_internal.published_tours
rows double-encoded (jsonb_typeof = 'string' instead of 'array'/'object').

Fix: pass the already-JSON-encoded str straight through to
PublishedCatalogRepository.insert(), which does not re-serialize either — it hands
the value to asyncpg's default jsonb codec as-is (same idiom AA-293 already
established for reads; this is the analogous write-side fix).

These tests call the real process_export() with a faked asyncpg connection
(fetchrow returns jsonb columns as raw JSON strings, mirroring production with no
codec) and capture the params actually handed to the INSERT INTO published_tours.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from services.export import handler as export_handler

FAKE_UUID = "22222222-2222-2222-2222-222222222222"
FAKE_TOUR_ID = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(autouse=True)
def _db_url(monkeypatch):
    monkeypatch.delenv("SECRET_DB_ARN", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    monkeypatch.delenv("TENANT_SLUG", raising=False)  # defaults to aa_internal


def _gc_row():
    return {
        "id": FAKE_UUID, "tour_id": FAKE_TOUR_ID, "tenant_id": FAKE_UUID,
        "batch_id": None,  # skips the whole tours_passed/ACP-S1 block below
        "aa_name": "Test Tour", "aa_subtitle": "A test subtitle",
        "aa_summary": "A test summary", "aa_description": "A test description",
        # jsonb columns as raw JSON TEXT — this is what asyncpg returns without a codec
        "aa_highlights": json.dumps(["Highlight one", "Highlight two"]),
        "aa_itineraries": "Day 1 -- Arrive",
        "mobile_card_text": "Trek & temples",
        "seo_title": "Test Tour | Adventure Asia", "seo_meta": "m" * 150,
        "seo_keywords_used": json.dumps(["vietnam tours", "hiking"]),
        "og_tags": json.dumps({"og:title": "Test Tour"}),
        "quality_score_id": None, "quality_score": 9.0,
        "country": "Vietnam", "duration": "5 days",
    }


def _run():
    """Runs the real process_export with I/O faked; returns the args tuple actually
    handed to the second conn.fetchrow call (the INSERT INTO published_tours)."""
    conn = AsyncMock()
    conn.fetchrow.side_effect = [_gc_row(), {"id": FAKE_UUID}]
    conn.execute = AsyncMock()
    conn.close = AsyncMock()

    with patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("services.jobs.a3_atomize_job.enqueue_a3_atomize", AsyncMock(return_value="job-1")):
        asyncio.run(export_handler.process_export(FAKE_UUID))

    insert_call = conn.fetchrow.call_args_list[1]
    return insert_call.args  # (query, tour_id, generated_content_id, tenant_id, aa_name, ...)


# Param order per shared/repository/published_catalog_repository.py::insert():
# 0=query, 1=tour_id, 2=generated_content_id, 3=tenant_id, 4=aa_name, 5=aa_subtitle,
# 6=aa_summary, 7=aa_description, 8=aa_highlights, 9=aa_itineraries, 10=mobile_card_text,
# 11=seo_title, 12=seo_meta, 13=seo_keywords_used, 14=og_tags, ...
_AA_HIGHLIGHTS_IDX = 8
_SEO_KEYWORDS_USED_IDX = 13
_OG_TAGS_IDX = 14


def test_aa_highlights_not_double_encoded():
    args = _run()
    passed = args[_AA_HIGHLIGHTS_IDX]
    assert isinstance(passed, str)
    decoded = json.loads(passed)
    assert isinstance(decoded, list), (
        f"double-encoded: json.loads() should yield a list directly, got "
        f"{type(decoded).__name__}: {decoded!r}"
    )
    assert decoded == ["Highlight one", "Highlight two"]


def test_seo_keywords_used_not_double_encoded():
    args = _run()
    passed = args[_SEO_KEYWORDS_USED_IDX]
    decoded = json.loads(passed)
    assert isinstance(decoded, list)
    assert decoded == ["vietnam tours", "hiking"]


def test_og_tags_not_double_encoded():
    args = _run()
    passed = args[_OG_TAGS_IDX]
    decoded = json.loads(passed)
    assert isinstance(decoded, dict)
    assert decoded == {"og:title": "Test Tour"}


def test_null_highlights_defaults_to_empty_array_string_not_python_list():
    """row.get('aa_highlights') is None (NULL in DB) -> must fall back to the JSON
    string "[]", not a native Python list — insert() hands params straight to
    asyncpg's default jsonb codec, which requires str when no codec is registered."""
    gc_row = _gc_row()
    gc_row["aa_highlights"] = None
    gc_row["seo_keywords_used"] = None
    gc_row["og_tags"] = None

    conn = AsyncMock()
    conn.fetchrow.side_effect = [gc_row, {"id": FAKE_UUID}]
    conn.execute = AsyncMock()
    conn.close = AsyncMock()

    with patch("services.export.handler.asyncpg.connect", AsyncMock(return_value=conn)), \
         patch("services.jobs.a3_atomize_job.enqueue_a3_atomize", AsyncMock(return_value="job-1")):
        asyncio.run(export_handler.process_export(FAKE_UUID))

    args = conn.fetchrow.call_args_list[1].args
    assert args[_AA_HIGHLIGHTS_IDX] == "[]"
    assert isinstance(args[_AA_HIGHLIGHTS_IDX], str)
    assert args[_SEO_KEYWORDS_USED_IDX] == "[]"
    assert args[_OG_TAGS_IDX] == "{}"
