"""AA-659 / ADR 0006 — stage routes (fallback chain + shadow), structured output, judge unpinning."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest, LLMResponse
from shared.llm_client.role_config import StageConfig


def _model(key, **kw):
    base = dict(model_key=key, label=key, vendor="openai", provider="bedrock",
                api_style="converse", bedrock_profile_ids={"acc3": f"global.openai.{key}"},
                callable_via=("llm_client",), supports_temperature=False,
                price_in_per_mtok=0.2, price_out_per_mtok=1.2, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


LUNA56 = _model("gpt-5.6-luna")
LUNA6_BEDROCK = _model("gpt-6-luna", enabled=False, blocked_reason="agreement not accepted")
LUNA6_OPENAI = _model("gpt-6-luna-openai", provider="openai", api_style="openai_chat",
                      bedrock_profile_ids={}, wire_model="gpt-6-luna",
                      price_in_per_mtok=0.1, price_out_per_mtok=0.5)
CATALOG = {m.model_key: m for m in (LUNA56, LUNA6_BEDROCK, LUNA6_OPENAI)}

JUDGE_ROUTE = StageConfig(
    "t10_judge", "judge", "openai", "gpt-5.6-luna", None,
    fallback_model_ids=("gpt-6-luna", "gpt-6-luna-openai"),
    shadow_model_id="gpt-4.1", shadow_sample_pct=0,
)


def _resp(model="x", **kw):
    return LLMResponse(content='{"ok": true}', model_used=model, provider="p", **kw)


def _client():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        return LLMClient()


def _gen(client, request, cfg=JUDGE_ROUTE):
    with patch("shared.llm_client.client.get_stage_config_sync", return_value=cfg), \
            patch("shared.llm_client.client.get_model_sync", side_effect=CATALOG.get):
        return client.generate(request)


def _req(**kw):
    return LLMRequest(system_prompt="s", user_prompt="u", stage="t10_judge", **kw)


# ── route ─────────────────────────────────────────────────────────────────────────────────────

def test_route_uses_primary_when_it_works():
    c = _client()
    with patch.object(c, "_call_key", return_value=_resp("satellite-gpt-5.6-luna")) as call:
        resp = _gen(c, _req())
    assert [a.args[1] for a in call.call_args_list] == ["gpt-5.6-luna"]
    assert resp.fallback_used is False


def test_route_skips_disabled_model_and_falls_back():
    c = _client()
    calls = []

    def fake(request, key, *_):
        calls.append(key)
        if key == "gpt-5.6-luna":
            raise RuntimeError("AccessDenied")
        return _resp(key)
    with patch.object(c, "_call_key", side_effect=fake):
        resp = _gen(c, _req())
    assert calls == ["gpt-5.6-luna", "gpt-6-luna-openai"]  # gpt-6-luna skipped: disabled
    assert resp.model_used == "gpt-6-luna-openai"
    assert resp.fallback_used is True


def test_route_all_failed_raises_with_every_reason():
    c = _client()
    with patch.object(c, "_call_key", side_effect=RuntimeError("down")), \
            pytest.raises(RuntimeError) as exc:
        _gen(c, _req())
    msg = str(exc.value)
    assert "gpt-5.6-luna: down" in msg
    assert "gpt-6-luna: skipped" in msg
    assert "gpt-6-luna-openai: down" in msg


def test_route_never_falls_back_to_a_model_outside_the_chain():
    c = _client()
    with patch.object(c, "_call_key", side_effect=RuntimeError("down")), \
            patch.object(c, "_legacy_chain") as legacy, pytest.raises(RuntimeError):
        _gen(c, _req())
    legacy.assert_not_called()


def test_explicit_model_tier_bypasses_route_and_shadow():
    c = _client()
    cfg = StageConfig(**{**JUDGE_ROUTE.__dict__, "shadow_sample_pct": 100})
    with patch.object(c, "_call_key", return_value=_resp()) as call, \
            patch.object(c, "_start_shadow") as shadow:
        _gen(c, _req(model_tier="gpt-4.1"), cfg)
    assert [a.args[1] for a in call.call_args_list] == ["gpt-4.1"]
    shadow.assert_not_called()


def test_stage_without_fallbacks_keeps_single_model_behaviour():
    c = _client()
    cfg = StageConfig("t9_write", "writer", "claude", "sonnet", "acc3")
    with patch.object(c, "_call_key", return_value=_resp()) as call, \
            patch.object(c, "_call_route") as route:
        _gen(c, _req(), cfg)
    route.assert_not_called()
    assert call.call_args.args[1] == "sonnet"


# ── shadow ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("pct,expected", [(100, 1), (0, 0)])
def test_shadow_runs_by_sample_pct(pct, expected):
    c = _client()
    cfg = StageConfig(**{**JUDGE_ROUTE.__dict__, "shadow_sample_pct": pct})
    with patch.object(c, "_call_key", return_value=_resp()), \
            patch.object(c, "_start_shadow") as shadow:
        _gen(c, _req(), cfg)
    assert shadow.call_count == expected


def test_shadow_failure_is_recorded_not_raised():
    c = _client()
    primary = _resp("satellite-gpt-5.6-luna")
    with patch.object(c, "_call_key", side_effect=RuntimeError("gpt-4.1 down")), \
            patch("shared.llm_client.shadow.record_shadow_sync") as rec:
        c._run_shadow(_req(), JUDGE_ROUTE, primary, "acc3", "acc1")
    kw = rec.call_args.kwargs
    assert kw["shadow"] is None
    assert "gpt-4.1 down" in kw["error"]
    assert kw["shadow_model"] == "gpt-4.1"
    assert kw["stage"] == "t10_judge"


def test_shadow_success_records_both_outputs():
    c = _client()
    primary, shadow = _resp("satellite-gpt-5.6-luna"), _resp("gpt-4.1")
    with patch.object(c, "_call_key", return_value=shadow), \
            patch("shared.llm_client.shadow.record_shadow_sync") as rec:
        c._run_shadow(_req(), JUDGE_ROUTE, primary, "acc3", "acc1")
    kw = rec.call_args.kwargs
    assert kw["primary"] is primary and kw["shadow"] is shadow and kw["error"] is None


# ── OpenAI catalog models + structured output ─────────────────────────────────────────────────

def _openai_completion(content='{"a": 1}', finish="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=1000),
    )


def test_openai_catalog_model_uses_reasoning_params_and_schema():
    c = _client()
    c._openai = MagicMock()
    c._openai.chat.completions.create.return_value = _openai_completion()
    schema = {"name": "r", "schema": {"type": "object"}}
    with patch("shared.llm_client.pricing._catalog_rates",
               return_value={"in": 0.0001, "out": 0.0005, "cache_read": 0, "cache_write": 0}):
        resp = c._call_openai(_req(temperature=0, seed=7, max_tokens=500, json_schema=schema),
                              model="gpt-6-luna", catalog_model=LUNA6_OPENAI)
    kwargs = c._openai.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "gpt-6-luna"
    assert kwargs["max_completion_tokens"] == 500 and "max_tokens" not in kwargs
    assert "temperature" not in kwargs and "seed" not in kwargs
    assert kwargs["response_format"]["json_schema"] == {"name": "r", "strict": True,
                                                        "schema": {"type": "object"}}
    assert resp.model_used == "gpt-6-luna-openai"
    assert resp.cost_usd == pytest.approx(0.0006)


def test_legacy_gpt41_call_is_unchanged():
    c = _client()
    c._openai = MagicMock()
    c._openai.chat.completions.create.return_value = _openai_completion()
    c._call_openai(_req(temperature=0, seed=7, max_tokens=500), model="gpt-4.1")
    kwargs = c._openai.chat.completions.create.call_args.kwargs
    assert kwargs["max_tokens"] == 500 and kwargs["temperature"] == 0 and kwargs["seed"] == 7
    assert "response_format" not in kwargs


def test_empty_reasoning_output_raises_so_the_route_moves_on():
    c = _client()
    c._openai = MagicMock()
    c._openai.chat.completions.create.return_value = _openai_completion(content=None, finish="length")
    with pytest.raises(RuntimeError, match="empty content"):
        c._call_openai(_req(), model="gpt-6-luna", catalog_model=LUNA6_OPENAI)


def test_converse_schema_uses_forced_tool_and_returns_its_input():
    c = _client()
    rt = MagicMock()
    rt.converse.return_value = {
        "output": {"message": {"content": [{"toolUse": {"name": "r", "input": {"brand_audit": {"x": 1}}}}]}},
        "usage": {"inputTokens": 10, "outputTokens": 5}, "stopReason": "tool_use",
    }
    schema = {"name": "r", "schema": {"type": "object"}}
    # A live-progress sink is bound, but a structured call must still use non-streaming converse.
    with patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt), \
            patch("shared.llm_client.client.stream_sink.delta_callback", return_value=lambda t: None):
        resp = c._call_bedrock_converse(_req(json_schema=schema), LUNA56, "acc3")
    kwargs = rt.converse.call_args.kwargs
    assert kwargs["toolConfig"]["toolChoice"] == {"tool": {"name": "r"}}
    assert kwargs["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"] == {"json": {"type": "object"}}
    assert json.loads(resp.content) == {"brand_audit": {"x": 1}}
    rt.converse_stream.assert_not_called()


def test_converse_schema_without_tool_call_raises():
    c = _client()
    rt = MagicMock()
    rt.converse.return_value = {"output": {"message": {"content": [{"text": "no"}]}}, "usage": {}}
    with patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt), \
            pytest.raises(RuntimeError, match="no tool call"):
        c._call_bedrock_converse(_req(json_schema={"name": "r", "schema": {}}), LUNA56, "acc3")


# ── judge call sites ──────────────────────────────────────────────────────────────────────────

def test_invoke_judge_goes_through_the_route():
    from services.acp_produce import judge_client
    fake = MagicMock()
    fake.generate.return_value = _resp("satellite-gpt-5.6-luna", input_tokens=3, output_tokens=4,
                                       cost_usd=0.01, satellite_account="acc3", fallback_used=False)
    with patch("shared.llm_client.client.LLMClient", return_value=fake):
        raw = judge_client.invoke_judge("sys", "user", stage="t10_judge")
    req = fake.generate.call_args.args[0]
    assert req.stage == "t10_judge" and req.temperature == 0 and req.model_tier is None
    assert raw["model_used"] == "satellite-gpt-5.6-luna"
    assert raw["account"] == "acc3" and raw["cost_usd"] == 0.01


def test_invoke_judge_requires_a_stage():
    import pytest
    from services.acp_produce import judge_client
    with pytest.raises(TypeError):
        judge_client.invoke_judge("sys", "user")


def test_brand_fit_no_longer_pins_gpt41():
    from services.content_generation import brand_fit
    fake = MagicMock()
    fake.generate.return_value = LLMResponse(
        content='{"brand_fit_score": 8, "cross_brand_distinct": 8, "mission_present": true, '
                '"feedback": ""}',
        model_used="satellite-gpt-5.6-luna", provider="bedrock-satellite")
    with patch.object(brand_fit, "LLMClient", return_value=fake):
        brand_fit.score_brand_fit({"brand_name": "X"}, {"name": "t"})
    req = fake.generate.call_args.args[0]
    assert req.model_tier is None and req.stage == "s1_judge"


def test_brand_audit_uses_gateway_with_strict_schema():
    from services.content_generation import brand_audit_node as ban
    payload = {"brand_audit": {"status": "pass", "failure_codes": [], "issues": [],
                               "fields_to_fix": [], "lessons_extracted": []}}
    fake = MagicMock()
    fake.generate.return_value = LLMResponse(content=json.dumps(payload),
                                             model_used="satellite-gpt-5.6-luna",
                                             provider="bedrock-satellite", cost_usd=0.001)
    state = {"generated": {"name": "Bhutan Trek", "subtitle": "A highland journey",
                           "summary": "s", "highlights": ["a", "b", "c", "d"],
                           "itineraries": "Day 1", "seo_title": "t",
                           "seo_meta": "A curated highland trek for discerning travelers."},
             "tour": {}, "seo": {}, "cost_usd": 0.0}
    with patch.object(ban, "LLMClient", return_value=fake), \
            patch.object(ban, "record_call_sync"):
        out = ban.brand_audit_node(state)
    req = fake.generate.call_args.args[0]
    assert req.stage == "s1_brand_audit"
    assert req.json_schema["name"] == "brand_audit_result"
    assert req.json_schema["schema"] is ban.BRAND_AUDIT_SCHEMA
    assert out["brand_audit_status"] == "pass"
    assert out["cost_usd"] == pytest.approx(0.001)


def test_role_config_maps_route_columns():
    from shared.llm_client.role_config import _row_to_config
    row = {"stage": "t10_judge", "role": "judge", "provider": "openai", "model_id": "gpt-5.6-luna",
           "account_route": None, "fallback_model_ids": ["gpt-6-luna", "gpt-6-luna-openai"],
           "shadow_model_id": "gpt-4.1", "shadow_sample_pct": 100}
    cfg = _row_to_config(row)
    assert cfg.fallback_model_ids == ("gpt-6-luna", "gpt-6-luna-openai")
    assert cfg.shadow_model_id == "gpt-4.1" and cfg.shadow_sample_pct == 100
