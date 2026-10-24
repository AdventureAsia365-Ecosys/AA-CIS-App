"""AA-714 (S222) — judge SAFE_DEFAULTS prefer Bedrock. S235: GPT-5.6 Luna -> GPT-6 Luna (Bedrock acc3) ->
GPT-6 Luna on the OpenAI API as the last resort; the legacy GPT-4.1 key is retired."""
from unittest.mock import patch

import pytest

from shared.llm_client.catalog import LEGACY_KEYS, CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest, LLMResponse
from shared.llm_client.role_config import SAFE_DEFAULTS

JUDGES = tuple(s for s, c in SAFE_DEFAULTS.items() if c.role == "judge")


def _model(key, **kw):
    base = dict(model_key=key, label=key, vendor="openai", provider="bedrock",
                api_style="converse", bedrock_profile_ids={"acc3": f"global.openai.{key}"},
                callable_via=("llm_client",), supports_temperature=False,
                price_in_per_mtok=0.1, price_out_per_mtok=0.5, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


CATALOG = {m.model_key: m for m in (
    _model("gpt-5.6-luna"), _model("gpt-6-luna"),
    _model("gpt-6-luna-openai", provider="openai", api_style="openai_chat", bedrock_profile_ids={}),
)}


def _client():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        return LLMClient()


def _run(stage, catalog_get, fail=()):
    c = _client()
    calls = []

    def fake(request, key, *_):
        calls.append(key)
        if key in fail:
            raise RuntimeError("down")
        return LLMResponse(content="{}", model_used=key, provider="p")
    with patch("shared.llm_client.client.get_stage_config_sync", return_value=SAFE_DEFAULTS[stage]), \
            patch("shared.llm_client.client.get_model_sync", side_effect=catalog_get), \
            patch.object(c, "_call_key", side_effect=fake):
        resp = c.generate(LLMRequest(system_prompt="s", user_prompt="u", stage=stage))
    return calls, resp


def test_every_judge_stage_is_covered():
    assert set(JUDGES) == {"s1_judge", "s1_brand_audit", "t2_judge", "t2_brand_audit",
                           "t7_debate_judge", "t10_judge", "writing_memory_mine"}   # AA-917


@pytest.mark.parametrize("stage", JUDGES)
def test_judge_default_is_bedrock_luna_first_openai_api_last(stage):
    cfg = SAFE_DEFAULTS[stage]
    assert cfg.model_id == "gpt-5.6-luna" and cfg.account_route == "acc3"
    assert cfg.fallback_model_ids == ("gpt-6-luna", "gpt-6-luna-openai")
    assert not set(cfg.fallback_model_ids) & LEGACY_KEYS
    assert cfg.shadow_model_id is None


@pytest.mark.parametrize("stage", JUDGES)
def test_catalog_readable_calls_bedrock_first(stage):
    calls, resp = _run(stage, CATALOG.get)
    assert calls == ["gpt-5.6-luna"] and resp.fallback_used is False


@pytest.mark.parametrize("stage", JUDGES)
def test_luna_failure_falls_back_to_the_other_luna(stage):
    calls, resp = _run(stage, CATALOG.get, fail=("gpt-5.6-luna",))
    assert calls == ["gpt-5.6-luna", "gpt-6-luna"] and resp.fallback_used is True


@pytest.mark.parametrize("stage", JUDGES)
def test_db_down_raises_with_no_gpt41_last_resort(stage):
    with pytest.raises(RuntimeError, match="All models in the route failed"):
        _run(stage, lambda _key: None)  # catalog unreachable, nothing cached


def test_bedrock_outage_falls_through_to_the_openai_api_last_resort():
    calls, resp = _run("s1_judge", CATALOG.get, fail=("gpt-5.6-luna", "gpt-6-luna"))
    assert calls == ["gpt-5.6-luna", "gpt-6-luna", "gpt-6-luna-openai"]
    assert resp.model_used == "gpt-6-luna-openai" and resp.fallback_used is True


def test_everything_down_raises_and_never_tries_gpt41():
    with pytest.raises(RuntimeError) as exc:
        _run("s1_judge", CATALOG.get, fail=("gpt-5.6-luna", "gpt-6-luna", "gpt-6-luna-openai"))
    assert "gpt-4.1" not in str(exc.value)
