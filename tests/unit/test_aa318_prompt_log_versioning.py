"""AA-318 (gộp AA-289) — log the FULL prompt text sent to the LLM, not just prompt_len, and
tie it to prompt_version for over-time comparison.

Covers 3 things:
  1. services/content_generation/graph.py::generate_node() — llm_prompt_built now carries
     system_prompt_text/user_prompt_text/prompt_version, not just prompt_len.
  2. services/content_generation/s1_from_atom.py::generate_s1_from_atom() — same log point
     added (this pipeline never logged the prompt at all before this task).
  3. api/routers/admin_pipeline.py::get_tour_history() — a real bug found while wiring this up:
     the query selected a bare gc.prompt_version column that has never existed on
     silver_aa_internal.generated_content (only the JSONB metadata.prompt_version does) —
     every real call would have raised UndefinedColumnError. Fixed to read
     gc.metadata->>'prompt_version'.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from structlog.testing import capture_logs

from services.content_generation.graph import generate_node
from shared.llm_client.models import LLMResponse

_FAKE_LLM_OUTPUT = json.dumps({
    "name": "Halong Bay", "subtitle": "A private cruise", "summary": "Three nights.",
    "highlights": ["Kayaking"], "itineraries": "Day 1: board.",
    "seo_title": "Halong Bay Cruise", "seo_meta": "Private cruise through Halong Bay.",
    "seo_keywords_used": [],
})


def _make_state(**kwargs):
    base = {
        "tour": {"name": "Halong Bay", "country": "Vietnam"}, "seo": {}, "few_shots": [],
        "generated": {}, "quality_score": 0.0, "retry_count": 0, "feedback": "", "error": "",
        "cost_usd": 0.0, "model_used": "", "brand_system_prompt": "", "brand_style_guide": "",
        "brand_forbidden_words": [], "rewrite_language": "en-US", "model_tier": "haiku",
        "subtitle_focus": "standard", "is_tenant_rewrite": False, "is_branded": True,
        "failure_codes": [], "sub_scores": {}, "passed_count": 0, "failed_count": 0,
    }
    base.update(kwargs)
    return base


def _fake_response():
    return LLMResponse(
        content=_FAKE_LLM_OUTPUT, model_used="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        provider="bedrock", input_tokens=100, output_tokens=50, cost_usd=0.0002,
    )


# ── 1. graph.py logs full prompt text + prompt_version ─────────────────────────────────────────

def test_generate_node_logs_full_system_and_user_prompt_text():
    brand_text = "You are writing for Atlas & Hearth, a luxury cultural travel brand."
    state = _make_state(brand_system_prompt=brand_text)

    with patch("services.content_generation.graph.LLMClient") as MockClient:
        instance = MockClient.return_value
        instance.generate.side_effect = lambda req: _fake_response()
        with capture_logs() as logs:
            generate_node(state)

    built = next(e for e in logs if e.get("event") == "llm_prompt_built")
    assert "system_prompt_text" in built
    assert brand_text in built["system_prompt_text"]
    assert "user_prompt_text" in built
    assert "Halong Bay" in built["user_prompt_text"]  # real tour data, not just length
    assert built.get("prompt_version")
    assert len(built["prompt_version"]) == 8


def test_generate_node_prompt_log_still_carries_prompt_len_for_backward_compat():
    """prompt_len (the pre-AA-318 field) must stay present — CloudWatch queries/dashboards built
    against it before this task shouldn't break."""
    state = _make_state()
    with patch("services.content_generation.graph.LLMClient") as MockClient:
        instance = MockClient.return_value
        instance.generate.side_effect = lambda req: _fake_response()
        with capture_logs() as logs:
            generate_node(state)
    built = next(e for e in logs if e.get("event") == "llm_prompt_built")
    assert isinstance(built.get("prompt_len"), int) and built["prompt_len"] > 0


# ── 2. (AA-611: the s1_from_atom.py prompt-log test was removed with the dead writer module;
#        graph.py's generate_node log point above is the remaining coverage of this contract.) ──


# ── 3. get_tour_history() real bug: bare gc.prompt_version column never existed ────────────────

_TEST_SECRET = "test-admin-secret"


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _TEST_SECRET)


def _pool(rows):
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    return pool, conn


@pytest.mark.asyncio
async def test_get_tour_history_reads_prompt_version_from_metadata_jsonb():
    from api.routers import admin_pipeline

    row = {
        "id": "abc", "version_num": 1, "created_at": None, "status": "approved",
        "model_editorial": "haiku", "brand_rules_version": None,
        "prompt_version": "a1b2c3d4",  # what the fixed query's aliased column returns
        "tenant_id": None, "score_overall": None, "score_brand": None, "score_seo": None,
        "score_structure": None, "cost_usd": None, "brand_name": None,
    }
    pool, conn = _pool([row])
    request = MagicMock()
    request.app.state.pool = pool

    result = await admin_pipeline.get_tour_history("tour_1", request, x_admin_secret=_TEST_SECRET)
    assert result["history"][0]["prompt_version"] == "a1b2c3d4"


@pytest.mark.asyncio
async def test_get_tour_history_query_never_selects_bare_gc_prompt_version():
    """Regression guard for the exact bug: `gc.prompt_version` (no metadata->> unwrap) must
    never appear as a bare column reference in this query again."""
    from api.routers import admin_pipeline

    pool, conn = _pool([])
    request = MagicMock()
    request.app.state.pool = pool

    await admin_pipeline.get_tour_history("tour_1", request, x_admin_secret=_TEST_SECRET)

    sql = conn.fetch.call_args.args[0]
    assert "gc.prompt_version" not in sql
    assert "metadata->>'prompt_version'" in sql
