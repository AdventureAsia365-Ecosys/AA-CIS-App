"""AA-714 (S222) — judge SAFE_DEFAULTS prefer Bedrock. S235: GPT-5.6 Luna -> GPT-6 Luna (Bedrock acc3) ->
GPT-6 Luna on the OpenAI API as the last resort; the legacy GPT-4.1 key is retired.

AA-1000: a cheap-judge exception keeps the same three models and the same rules (Bedrock acc3 first, the OpenAI API
only last, no legacy key, no shadow) but starts on GPT-6 Luna, the cheaper of the two Bedrock Lunas."""
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from shared.llm_client.catalog import LEGACY_KEYS, CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest, LLMResponse
from shared.llm_client.role_config import SAFE_DEFAULTS

# Judge stages that start on GPT-6 Luna on purpose, with the reason (the stage's migration carries the same route).
CHEAP_JUDGES = {
    # AA-1000 (migration 308): the T10 tier-2 adjudicator — up to 3 calls a piece, a short span check that never holds
    # a piece; cost: GPT-6 Luna first ($0.10 / $0.50 per Mtok) instead of GPT-5.6 Luna ($0.20 / $1.20).
    "t10_adjudicate": "cost: GPT-6 Luna first",
}
MIGRATIONS = Path(__file__).resolve().parents[2] / "api" / "migrations"
CHEAP_JUDGE_ROUTE = ("gpt-6-luna", ("gpt-5.6-luna", "gpt-6-luna-openai"))
ALL_JUDGES = tuple(s for s, c in SAFE_DEFAULTS.items() if c.role == "judge")
JUDGES = tuple(s for s in ALL_JUDGES if s not in CHEAP_JUDGES)


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
    assert set(CHEAP_JUDGES) <= set(ALL_JUDGES)
    assert set(JUDGES) == {"s1_judge", "s1_brand_audit", "t2_judge", "t2_brand_audit",
                           "t7_debate_judge", "t10_judge", "writing_memory_mine",   # AA-917
                           "writing_memory_eval"}   # AA-919 WM-19


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


@pytest.mark.parametrize("stage", sorted(CHEAP_JUDGES))
def test_cheap_judge_is_bedrock_gpt6_luna_first_openai_api_last(stage):
    cfg = SAFE_DEFAULTS[stage]
    assert CHEAP_JUDGES[stage].startswith("cost:")
    assert (cfg.model_id, cfg.fallback_model_ids) == CHEAP_JUDGE_ROUTE and cfg.account_route == "acc3"
    assert cfg.provider != "claude"  # ADR 0008: a judge stage is non-Anthropic
    assert not set(cfg.fallback_model_ids) & LEGACY_KEYS
    assert cfg.shadow_model_id is None


@pytest.mark.parametrize("stage", sorted(CHEAP_JUDGES))
def test_cheap_judge_calls_bedrock_first_and_the_openai_api_last(stage):
    calls, resp = _run(stage, CATALOG.get)
    assert calls == ["gpt-6-luna"] and resp.fallback_used is False
    calls, resp = _run(stage, CATALOG.get, fail=("gpt-6-luna",))
    assert calls == ["gpt-6-luna", "gpt-5.6-luna"] and resp.fallback_used is True
    calls, resp = _run(stage, CATALOG.get, fail=("gpt-6-luna", "gpt-5.6-luna"))
    assert calls == ["gpt-6-luna", "gpt-5.6-luna", "gpt-6-luna-openai"] and resp.model_used == "gpt-6-luna-openai"


@pytest.mark.parametrize("stage", sorted(CHEAP_JUDGES))
def test_cheap_judge_route_matches_its_migration(stage):
    """The seeding migration carries the same route as SAFE_DEFAULTS (both places hold the cost choice)."""
    model, fallbacks = CHEAP_JUDGE_ROUTE
    row = re.compile(rf"\('{re.escape(stage)}',\s*'judge',\s*'openai',\s*'{re.escape(model)}',\s*'acc3',\s*true,\s*"
                     rf"ARRAY\[{re.escape(', '.join(repr(k) for k in fallbacks))}\]::text\[\]")
    assert any(row.search(p.read_text()) for p in MIGRATIONS.glob("*.sql")), f"no migration seeds {stage}'s route"


@pytest.mark.parametrize("stage", ALL_JUDGES)
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
