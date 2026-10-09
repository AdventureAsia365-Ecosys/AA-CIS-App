"""AA-714 (S222) — judge SAFE_DEFAULTS prefer Bedrock; GPT-4.1 (legacy, no catalog) is the last resort."""
from unittest.mock import patch

import pytest

from shared.llm_client.catalog import LEGACY_KEYS, CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest, LLMResponse
from shared.llm_client.role_config import SAFE_DEFAULTS

JUDGES = ("s1_judge", "s1_brand_audit", "t10_judge")


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


@pytest.mark.parametrize("stage", JUDGES)
def test_judge_default_is_bedrock_luna_first_with_legacy_last_resort(stage):
    cfg = SAFE_DEFAULTS[stage]
    assert cfg.model_id == "gpt-5.6-luna" and cfg.account_route == "acc3"
    assert cfg.fallback_model_ids[-1] in LEGACY_KEYS  # works with no catalog row
    assert cfg.shadow_model_id is None


@pytest.mark.parametrize("stage", JUDGES)
def test_catalog_readable_calls_bedrock_first(stage):
    calls, resp = _run(stage, CATALOG.get)
    assert calls == ["gpt-5.6-luna"] and resp.fallback_used is False


@pytest.mark.parametrize("stage", JUDGES)
def test_db_down_skips_luna_and_still_judges_with_gpt41(stage):
    calls, resp = _run(stage, lambda _key: None)  # catalog unreachable, nothing cached
    assert calls == ["gpt-4.1"] and resp.model_used == "gpt-4.1" and resp.fallback_used is True


def test_bedrock_failures_fall_through_to_openai_before_legacy():
    calls, resp = _run("s1_judge", CATALOG.get, fail=("gpt-5.6-luna", "gpt-6-luna"))
    assert calls == ["gpt-5.6-luna", "gpt-6-luna", "gpt-6-luna-openai"]
    assert resp.model_used == "gpt-6-luna-openai"
