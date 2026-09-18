"""AA-620 (epic AA-616) — split a dedicated `t2_generate` stage for the tenant (T2) rewrite,
separate from the shared `s1_generate`, and thread tenant_id through the S1 graph so record_call
logs the real tenant for T2 (NULL for A1 admin).

No live Bedrock/DB — LLMClient and record_call_sync are mocked, same convention as
test_aa353_itinerary_compression.py / test_content_graph.py.
"""
import json
from unittest.mock import patch

from shared.llm_client.models import LLMResponse
from shared.llm_client.role_config import SAFE_DEFAULTS
from services.content_generation.graph import generate_node


# ── SAFE_DEFAULTS: t2_generate seeded, matches s1_generate (Haiku) ────────────

def test_t2_generate_in_safe_defaults_seeded_haiku():
    """t2_generate must exist in SAFE_DEFAULTS so a DB miss resolves to Haiku (identical to
    s1_generate today), not the generic fallback. Migration 156 seeds the same values."""
    assert "t2_generate" in SAFE_DEFAULTS
    cfg = SAFE_DEFAULTS["t2_generate"]
    assert cfg.model_id == "haiku"
    assert cfg.provider == "claude"
    assert cfg.account_route == "acc3"
    assert cfg.role == "writer"


def test_t2_generate_matches_s1_generate_at_seed_time():
    """AA-620 only creates the lever — t2_generate is seeded identical to s1_generate. Any future
    Sonnet move is a config change via the admin UI, NOT a code/seed change."""
    s1 = SAFE_DEFAULTS["s1_generate"]
    t2 = SAFE_DEFAULTS["t2_generate"]
    assert (t2.model_id, t2.provider, t2.account_route) == (s1.model_id, s1.provider, s1.account_route)


# ── generate_node: stage + tenant_id threaded from state ──────────────────────

_MIN_OUTPUT = json.dumps({
    "name": "X", "subtitle": "s", "summary": "sum",
    "highlights": ["a", "b", "c"], "itineraries": "Day 1 — T\nBody",
    "seo_title": "t", "seo_meta": "x" * 145, "trip_type": "cultural",
})


def _resp() -> LLMResponse:
    return LLMResponse(
        content=_MIN_OUTPUT, model_used="satellite-haiku-4-5",
        provider="bedrock", input_tokens=100, output_tokens=50, cost_usd=0.0002,
        cache_read_tokens=0, cache_write_tokens=0,
        fallback_used=False, satellite_account="acc3",
    )


def _base_state(**kw):
    base = {
        "tour": {"name": "X", "country": "Laos", "duration": "1 Days", "itineraries": "Day 1: a\nb"},
        "seo": {}, "few_shots": [], "generated": {}, "quality_score": 0.0, "retry_count": 0,
        "feedback": "", "error": "", "cost_usd": 0.0, "model_used": "",
        "brand_system_prompt": "brand", "brand_style_guide": "", "brand_forbidden_words": [],
        "rewrite_language": "en-US", "model_tier": None, "subtitle_focus": "standard",
        "is_tenant_rewrite": False, "is_branded": True,
    }
    base.update(kw)
    return base


def _run_capture(state):
    """Run generate_node with LLMClient + record_call_sync mocked; return (llm_request, log_kwargs_list)."""
    log_calls = []
    with patch("services.content_generation.graph.LLMClient") as MockClient, \
         patch("services.content_generation.graph.record_call_sync",
               side_effect=lambda **kw: log_calls.append(kw)):
        instance = MockClient.return_value
        instance.generate.side_effect = [_resp()]
        generate_node(state)
        request = instance.generate.call_args.args[0]
    return request, log_calls


def test_t2_rewrite_uses_t2_generate_stage_and_logs_tenant():
    """A tenant (T2) rewrite: initial_state carries generate_stage='t2_generate' + a real
    tenant_id → the LLMRequest resolves the t2_generate stage config AND record_call logs that
    tenant under stage='t2_generate'."""
    tid = "11111111-1111-1111-1111-111111111111"
    state = _base_state(generate_stage="t2_generate", tenant_id=tid, is_tenant_rewrite=True)
    request, log_calls = _run_capture(state)

    assert request.stage == "t2_generate"
    assert len(log_calls) == 1
    assert log_calls[0]["stage"] == "t2_generate"
    assert log_calls[0]["tenant_id"] == tid


def test_a1_admin_rewrite_keeps_s1_generate_and_null_tenant():
    """A1 admin: no generate_stage/tenant_id in state → falls back to s1_generate + tenant_id
    None, exactly the pre-AA-620 behaviour (A1 writes stay tenant-agnostic in llm_call_log)."""
    state = _base_state()  # no generate_stage, no tenant_id
    request, log_calls = _run_capture(state)

    assert request.stage == "s1_generate"
    assert len(log_calls) == 1
    assert log_calls[0]["stage"] == "s1_generate"
    assert log_calls[0]["tenant_id"] is None
