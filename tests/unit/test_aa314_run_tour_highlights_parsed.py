"""AA-314: _execute_run_tour fed row["src_highlights"] straight into the `tour`
dict without parsing it. Since no asyncpg connection in this app registers a jsonb
type codec, src_highlights arrives as a JSON-encoded str, not a list — and
prompts.py::build_rewrite_prompt interpolated it directly into the LLM prompt
(`{tour.get('highlights')}`), leaking raw JSON syntax (["A", "B"]) into every
tour rewrite instead of a clean, readable list.

Fix: json.loads() src_highlights before building `tour` (mirrors the isinstance-guard
idiom already used elsewhere in this file, e.g. GET /tours/{id}/source).

Same all-I/O-patched harness as AA-233/AA-237: asyncpg.connect faked, _rewrite_tour
patched so we can capture the `tour` dict actually built and handed to it.
"""
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio
import json

import pytest

from api.routers import admin_pipeline
from services.content_generation.prompts import build_rewrite_prompt

FAKE_UUID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _db_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/test")
    monkeypatch.setenv("AUTO_UPGRADE_THRESHOLD", "8.5")


def _raw_row(src_highlights):
    return {
        "src_name": "Test Tour", "src_subtitle": "", "src_summary": "",
        "src_description": "", "src_highlights": src_highlights, "src_itineraries": "",
        "country": "Vietnam", "duration": "7 days", "price_raw": "1000",
        "inclusions": "", "exclusions": "", "source_status": None,
        "activities": None,
    }


def _req(**over):
    base = dict(tour_id=FAKE_UUID, batch_id="b-1", tenant_id="aa_internal")  # AA-1024 A2: no model_tier
    base.update(over)
    return admin_pipeline.TourRunRequest(**base)


def _run(src_highlights):
    """Runs the real _execute_run_tour with I/O faked; returns the `tour` dict
    actually handed to _rewrite_tour (captured via the patched mock)."""
    conn = AsyncMock()
    conn.fetchrow.return_value = _raw_row(src_highlights)
    conn.fetch.return_value = []
    rt = AsyncMock(return_value={
        "status": "success", "quality_score": 9.0, "model_used": "haiku-4.5",
        "generated": {}, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
    })
    with ExitStack() as stack:
        stack.enter_context(patch("api.routers.admin_pipeline.asyncpg.connect",
                                  AsyncMock(return_value=conn)))
        stack.enter_context(patch("api.routers.admin_pipeline._resolve_brand_rule",
                                  AsyncMock(return_value=None)))
        stack.enter_context(patch("api.routers.admin_pipeline._rewrite_tour", rt))
        stack.enter_context(patch("services.seo_intelligence.handler.process_seo",
                                  AsyncMock(return_value={"data": {}, "status": "skipped"})))
        stack.enter_context(patch("services.seo_intelligence.seed_builder.build_seed",
                                  MagicMock(return_value="Vietnam tours")))
        asyncio.run(admin_pipeline._execute_run_tour(_req()))
    return rt.call_args_list[0].args[0]  # tour dict (first positional arg to _rewrite_tour)


def test_src_highlights_json_string_parsed_to_list():
    # This is what asyncpg actually returns for a jsonb column with no codec.
    tour = _run(json.dumps(["Visit temple", "See elephants"]))
    assert isinstance(tour["highlights"], list), (
        f"expected list, got {type(tour['highlights']).__name__}: {tour['highlights']!r}"
    )
    assert tour["highlights"] == ["Visit temple", "See elephants"]


def test_src_highlights_empty_string_becomes_empty_list():
    tour = _run("")
    assert tour["highlights"] == []


def test_src_highlights_none_becomes_empty_list():
    tour = _run(None)
    assert tour["highlights"] == []


def test_src_highlights_already_a_list_passed_through():
    # Defensive path (isinstance guard) — must not double-parse if ever a list already.
    tour = _run(["Already", "A list"])
    assert tour["highlights"] == ["Already", "A list"]


# ── prompts.py: confirm the list renders as readable text, not raw JSON syntax ──

def _base_tour(**over):
    tour = {
        "name": "Test Tour", "country": "Vietnam", "duration": "7 days",
        "summary": "s", "description": "d",
        "itineraries": "Day 1", "inclusions": "", "exclusions": "",
    }
    tour.update(over)
    return tour


def test_prompt_renders_clean_joined_highlights_not_raw_json_syntax():
    prompt = build_rewrite_prompt(_base_tour(highlights=["Visit temple", "See elephants"]), seo={})
    assert "- Highlights: Visit temple, See elephants" in prompt
    # regression guard: no raw JSON/list syntax should leak into the prompt
    assert '["' not in prompt
    assert "['" not in prompt


def test_prompt_handles_empty_highlights_list():
    prompt = build_rewrite_prompt(_base_tour(highlights=[]), seo={})
    assert "- Highlights: \n" in prompt or prompt.count("- Highlights:") == 1
