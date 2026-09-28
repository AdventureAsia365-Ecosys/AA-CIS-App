"""AA-658 / ADR 0005 — model catalog, Converse adapter, catalog-driven pricing and dropdown."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from shared.llm_client import catalog, pricing
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest


def _model(key, **kw):
    base = dict(model_key=key, label=key, vendor="anthropic", provider="bedrock",
                api_style="converse", bedrock_profile_ids={"acc3": f"global.anthropic.{key}"},
                callable_via=("llm_client",), price_in_per_mtok=2.0, price_out_per_mtok=10.0,
                enabled=True)
    base.update(kw)
    return CatalogModel(**base)


SONNET5 = _model("sonnet-5")
LUNA = _model("gpt-5.6-luna", vendor="openai", supports_temperature=False,
              bedrock_profile_ids={"acc3": "global.openai.gpt-5.6-luna"},
              price_in_per_mtok=0.1, price_out_per_mtok=0.5)
OPUS = _model("opus-5-5", enabled=False, price_in_per_mtok=None, price_out_per_mtok=None,
              blocked_reason="Price not verified yet")
GPT41 = _model("gpt-4.1", vendor="openai", provider="openai", api_style="openai_chat",
               bedrock_profile_ids={}, callable_via=("llm_client", "openai_direct", "pinned"),
               price_in_per_mtok=2.0, price_out_per_mtok=8.0)
HAIKU = _model("haiku", api_style="anthropic_native", price_in_per_mtok=1.0, price_out_per_mtok=5.0)
EMBED = _model("cohere-embed-v4", vendor="cohere", api_style="embed", callable_via=("embed",),
               enabled=False, price_in_per_mtok=None, price_out_per_mtok=None)
ALL = [SONNET5, LUNA, OPUS, GPT41, HAIKU, EMBED]


@pytest.fixture(autouse=True)
def _reset_catalog_cache():
    catalog.invalidate()
    catalog._models = {}
    yield
    catalog.invalidate()
    catalog._models = {}


# ── catalog read side ─────────────────────────────────────────────────────────────────────────

def test_failed_load_is_negative_cached():
    with patch.object(catalog, "_run_fetch_sync", side_effect=OSError("no db")) as fetch:
        assert catalog.get_model_sync("sonnet-5") is None
        assert catalog.get_model_sync("sonnet-5") is None
    assert fetch.call_count == 1


def test_loaded_catalog_is_cached_and_strips_satellite_prefix():
    with patch.object(catalog, "_run_fetch_sync", return_value={"sonnet-5": SONNET5}) as fetch:
        assert catalog.get_model_sync("sonnet-5") is SONNET5
        assert catalog.get_model_sync("satellite-sonnet-5") is SONNET5
    assert fetch.call_count == 1


def test_account_for_prefers_requested_account_when_served():
    m = _model("x", bedrock_profile_ids={"acc3": "a", "acc1": "b"})
    assert m.account_for("acc1") == "acc1"
    assert SONNET5.account_for("acc1") == "acc3"


# ── pricing ───────────────────────────────────────────────────────────────────────────────────

def test_calc_cost_uses_catalog_price():
    with patch("shared.llm_client.catalog.get_model_sync", return_value=SONNET5):
        assert pricing.calc_cost("sonnet-5", 1000, 1000) == pytest.approx(0.012)


def test_calc_cost_catalog_cache_prices_default_to_multipliers():
    with patch("shared.llm_client.catalog.get_model_sync", return_value=SONNET5):
        # 1000 cache-write tokens at 1.25 x $2/1M = $0.0025; 1000 reads at 0.1x = $0.0002
        assert pricing.calc_cost("sonnet-5", 0, 0, cache_read=1000, cache_write=1000) == \
            pytest.approx(0.0027)


def test_calc_cost_falls_back_to_cost_table_when_catalog_unreachable():
    with patch("shared.llm_client.catalog.get_model_sync", return_value=None):
        assert pricing.calc_cost("haiku", 1000, 1000) == pytest.approx(0.006)
        assert pricing.calc_cost(pricing.BEDROCK_SONNET, 1000, 1000) == pytest.approx(0.018)


def test_calc_cost_unknown_model_warns_and_uses_sonnet_rates():
    with patch("shared.llm_client.catalog.get_model_sync", return_value=None), \
            patch.object(pricing, "logger") as log:
        assert pricing.calc_cost("mystery-model", 1000, 1000) == pytest.approx(0.018)
    log.warning.assert_called_once()


def test_calc_cost_maps_legacy_ids_to_catalog_keys():
    seen = []

    def fake(key):
        seen.append(key)
        return None
    with patch("shared.llm_client.catalog.get_model_sync", side_effect=fake):
        pricing.calc_cost(pricing.BEDROCK_HAIKU, 1, 1)
        pricing.calc_cost("satellite-sonnet-4-6", 1, 1)
    assert seen == ["haiku", "sonnet"]


# ── LLMClient Converse path ───────────────────────────────────────────────────────────────────

def _client():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        return LLMClient()


def _converse_response(text="hello", usage=None, stop="end_turn"):
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": usage or {"inputTokens": 1000, "outputTokens": 1000},
        "stopReason": stop,
    }


def _run(request, model, rt):
    c = _client()
    with patch("shared.llm_client.client.get_model_sync", return_value=model), \
            patch("shared.llm_client.pricing._catalog_rates",
                  return_value={"in": model.price_in_per_mtok / 1000,
                                "out": model.price_out_per_mtok / 1000,
                                "cache_read": 0, "cache_write": 0}), \
            patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt) as gsc, \
            patch("shared.llm_client.client.stream_sink.delta_callback", return_value=None):
        resp = c.generate(request)
    return resp, gsc


def test_converse_model_is_called_on_acc3_and_priced_from_catalog():
    rt = MagicMock()
    rt.converse.return_value = _converse_response()
    resp, gsc = _run(LLMRequest(system_prompt="s", user_prompt="u", model_tier="sonnet-5"), SONNET5, rt)
    gsc.assert_called_once_with("bedrock-runtime", account="acc3")
    kwargs = rt.converse.call_args.kwargs
    assert kwargs["modelId"] == "global.anthropic.sonnet-5"
    assert kwargs["system"] == [{"text": "s"}]
    assert kwargs["messages"] == [{"role": "user", "content": [{"text": "u"}]}]
    assert "temperature" not in kwargs["inferenceConfig"]
    assert resp.content == "hello"
    assert resp.model_used == "satellite-sonnet-5"
    assert resp.provider == "bedrock-satellite"
    assert resp.satellite_account == "acc3"
    assert resp.fallback_used is False
    assert resp.cost_usd == pytest.approx(0.012)
    assert resp.stop_reason == "end_turn"


def test_temperature_forwarded_only_when_set_and_supported():
    rt = MagicMock()
    rt.converse.return_value = _converse_response()
    _run(LLMRequest(system_prompt="s", user_prompt="u", model_tier="sonnet-5", temperature=0.2),
         SONNET5, rt)
    assert rt.converse.call_args.kwargs["inferenceConfig"]["temperature"] == 0.2

    rt2 = MagicMock()
    rt2.converse.return_value = _converse_response()
    _run(LLMRequest(system_prompt="s", user_prompt="u", model_tier="gpt-5.6-luna", temperature=0.2),
         LUNA, rt2)
    assert "temperature" not in rt2.converse.call_args.kwargs["inferenceConfig"]


def test_converse_stream_feeds_live_progress_sink():
    rt = MagicMock()
    rt.converse_stream.return_value = {"stream": [
        {"contentBlockDelta": {"delta": {"text": "hel"}}},
        {"contentBlockDelta": {"delta": {"text": "lo"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 2}}},
    ]}
    seen = []
    c = _client()
    with patch("shared.llm_client.client.get_model_sync", return_value=SONNET5), \
            patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt), \
            patch("shared.llm_client.client.stream_sink.delta_callback", return_value=seen.append):
        resp = c.generate(LLMRequest(system_prompt="s", user_prompt="u", model_tier="sonnet-5"))
    assert seen == ["hel", "lo"]
    assert resp.content == "hello"
    assert (resp.input_tokens, resp.output_tokens) == (10, 2)
    rt.converse.assert_not_called()


@pytest.mark.parametrize("model,match", [
    (None, "not in shared.llm_model_catalog"),
    (OPUS, "disabled"),
])
def test_unknown_or_disabled_model_raises_without_fallback(model, match):
    c = _client()
    with patch("shared.llm_client.client.get_model_sync", return_value=model), \
            patch.object(c, "_call_bedrock") as native, \
            patch.object(c, "_call_openai") as oai, \
            pytest.raises(RuntimeError, match=match):
        c.generate(LLMRequest(system_prompt="s", user_prompt="u", model_tier="opus-5-5"))
    native.assert_not_called()
    oai.assert_not_called()


def test_converse_failure_raises_without_fallback():
    rt = MagicMock()
    rt.converse.side_effect = RuntimeError("AccessDeniedException")
    c = _client()
    with patch("shared.llm_client.client.get_model_sync", return_value=SONNET5), \
            patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt), \
            patch("shared.llm_client.client.stream_sink.delta_callback", return_value=None), \
            patch.object(c, "_call_bedrock") as native, \
            patch.object(c, "_call_openai") as oai, \
            pytest.raises(RuntimeError, match="AccessDenied"):
        c.generate(LLMRequest(system_prompt="s", user_prompt="u", model_tier="sonnet-5"))
    native.assert_not_called()
    oai.assert_not_called()


def test_legacy_tiers_never_touch_the_catalog_path():
    c = _client()
    with patch.object(c, "_call_catalog_model") as cat, \
            patch.object(c, "_call_bedrock", return_value=MagicMock(fallback_used=False)):
        c.generate(LLMRequest(system_prompt="s", user_prompt="u", model_tier="haiku"))
    cat.assert_not_called()


# ── admin dropdown / PATCH validation ─────────────────────────────────────────────────────────

def _available(options):
    return {o["model_id"] for o in options if o["available"]}


def test_writer_stage_offers_anthropic_converse_models_only():
    from api.routers.admin_llm_ops import _catalog_options
    opts = _catalog_options("writer", "t9_write", ALL)
    assert _available(opts) == {"sonnet-5", "haiku"}
    reasons = {o["model_id"]: o.get("reason") for o in opts}
    assert "vendor" in reasons["gpt-5.6-luna"]
    assert reasons["opus-5-5"] == "Price not verified yet"
    assert "cohere-embed-v4" not in reasons


@pytest.mark.parametrize("stage", ["s1_judge", "t10_judge", "n7_judge", "s1_brand_audit"])
def test_judge_stages_offer_non_anthropic_models(stage):
    # AA-659: judges run through the gateway route, so any enabled non-Anthropic model works.
    from api.routers.admin_llm_ops import _catalog_options
    opts = _catalog_options("judge", stage, ALL)
    assert _available(opts) == {"gpt-4.1", "gpt-5.6-luna"}
    sonnet5 = next(o for o in opts if o["model_id"] == "sonnet-5")
    assert "vendor" in sonnet5["reason"]


def test_catalog_unreachable_falls_back_to_static_lists():
    from api.routers.admin_llm_ops import _options_for
    assert _available(_options_for("writer", "t9_write", None)) == {"haiku", "sonnet"}
    assert _available(_options_for("judge", "t10_judge", None)) == {"gpt-4.1"}


@pytest.mark.asyncio
async def test_patch_rejects_model_the_stage_cannot_execute():
    from api.routers import admin_llm_ops as ops
    rows = [{"stage": "t10_judge", "role": "judge"}]

    async def fake_list():
        return rows

    async def fake_catalog():
        return ALL

    with patch.object(ops, "verify_admin_secret"), \
            patch.object(ops, "list_stage_configs", side_effect=fake_list), \
            patch.object(ops, "_load_catalog", side_effect=fake_catalog), \
            patch.object(ops, "set_stage_config") as setter:
        with pytest.raises(HTTPException) as exc:
            await ops.patch_llm_config("t10_judge", ops.LlmConfigPatch(model_id="sonnet-5"),
                                       x_admin_secret="x", x_admin_user_id="u")
        assert exc.value.status_code == 422
        with pytest.raises(HTTPException) as exc:
            await ops.patch_llm_config("nope", ops.LlmConfigPatch(model_id="gpt-4.1"),
                                       x_admin_secret="x", x_admin_user_id="u")
        assert exc.value.status_code == 404
    setter.assert_not_called()
