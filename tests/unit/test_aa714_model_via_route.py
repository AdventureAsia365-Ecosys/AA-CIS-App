"""AA-714 — admin LLM settings say where each model really runs (Bedrock acc3 vs OpenAI API) and
show the whole stage route (primary → fallbacks, shadow)."""
from api.routers.admin_llm_ops import _catalog_options, _route_view, model_via
from shared.llm_client.catalog import CatalogModel


def _m(key, label, provider, api_style="converse", profiles=None, vendor="openai"):
    return CatalogModel(model_key=key, label=label, vendor=vendor, provider=provider, api_style=api_style,
                        bedrock_profile_ids=profiles or {}, callable_via=("llm_client",))


LUNA_56 = _m("gpt-5.6-luna", "GPT-5.6 Luna", "bedrock", profiles={"acc3": "openai.gpt-5.6-luna"})
LUNA_6 = _m("gpt-6-luna", "GPT-6 Luna", "bedrock", profiles={"acc3": "openai.gpt-6-luna"})
LUNA_6_OAI = _m("gpt-6-luna-openai", "GPT-6 Luna (OpenAI)", "openai", api_style="openai_chat")
GPT41 = _m("gpt-4.1", "GPT-4.1", "openai", api_style="openai_chat")
HAIKU = _m("haiku", "Claude Haiku 4.5", "bedrock", profiles={"acc1": "p1", "acc3": "p3"}, vendor="anthropic")
MODELS = [LUNA_56, LUNA_6, LUNA_6_OAI, GPT41, HAIKU]


def test_same_model_name_on_two_providers_is_told_apart():
    assert model_via(LUNA_6) == "Bedrock acc3"
    assert model_via(LUNA_6_OAI) == "OpenAI API"
    assert model_via(GPT41) == "OpenAI API"


def test_bedrock_accounts_listed_in_try_order():
    assert model_via(HAIKU) == "Bedrock acc3 → acc1"


def test_judge_options_carry_via():
    opts = {o["model_id"]: o for o in _catalog_options("judge", "s1_judge", MODELS)}
    assert opts["gpt-5.6-luna"]["via"] == "Bedrock acc3"
    assert opts["gpt-6-luna-openai"]["via"] == "OpenAI API"


def test_route_view_lists_chain_in_order_and_shadow():
    row = {"model_id": "gpt-5.6-luna", "fallback_model_ids": ["gpt-6-luna", "gpt-6-luna-openai"],
           "shadow_model_id": "gpt-6-luna", "shadow_sample_pct": 100}
    r = _route_view(row, MODELS)
    assert [(i["model_id"], i["via"]) for i in r["chain"]] == [
        ("gpt-5.6-luna", "Bedrock acc3"), ("gpt-6-luna", "Bedrock acc3"), ("gpt-6-luna-openai", "OpenAI API")]
    assert r["shadow"] == {"model_id": "gpt-6-luna", "label": "GPT-6 Luna", "via": "Bedrock acc3", "sample_pct": 100}


def test_route_view_without_catalog_or_shadow():
    r = _route_view({"model_id": "gpt-4.1", "fallback_model_ids": None, "shadow_model_id": None}, None)
    assert r == {"chain": [{"model_id": "gpt-4.1", "label": "gpt-4.1", "via": "OpenAI API"}], "shadow": None}
