"""AA-685 — gateway embed path (shared/llm_client/embed.py), the column mapper moved onto
LLMClient, and the admin dropdown for `embed` stages."""
import io
import json
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import column_mapper, embed as embed_mod
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.models import LLMResponse
from shared.llm_client.role_config import SAFE_DEFAULTS, StageConfig


def _embed_model(key="cohere-embed-v4", **kw):
    base = dict(model_key=key, label=key, vendor="cohere", provider="bedrock", api_style="embed",
                bedrock_profile_ids={"acc2": f"us.{key}:0"}, callable_via=("embed",),
                price_in_per_mtok=0.12, price_out_per_mtok=0.0, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


def _runtime(vectors, token_header=None):
    client = MagicMock()
    resp = {"body": io.BytesIO(json.dumps({"embeddings": {"float": vectors}}).encode())}
    if token_header is not None:
        resp["ResponseMetadata"] = {"HTTPHeaders": {"x-amzn-bedrock-input-token-count": token_header}}
    client.invoke_model.return_value = resp
    return client


class TestEmbed:
    def test_prices_from_the_catalog_row_and_reads_bedrock_token_count(self):
        with patch.object(embed_mod, "get_stage_config_sync", return_value=SAFE_DEFAULTS["f10_embed"]), \
             patch.object(embed_mod, "get_model_sync", return_value=_embed_model()), \
             patch.object(embed_mod, "_runtime_for", return_value=_runtime([[0.1, 0.2]], "2000000")):
            resp = embed_mod.embed("f10_embed", ["hello"])
        assert resp.vectors == [[0.1, 0.2]]
        assert resp.account == "acc2" and resp.model_used == "cohere-embed-v4"
        assert resp.input_tokens == 2_000_000 and resp.tokens_estimated is False
        assert resp.cost_usd == pytest.approx(0.24)
        assert resp.fallback_used is False

    def test_estimates_tokens_when_bedrock_omits_the_header(self):
        with patch.object(embed_mod, "get_stage_config_sync", return_value=SAFE_DEFAULTS["f10_embed"]), \
             patch.object(embed_mod, "get_model_sync", return_value=_embed_model()), \
             patch.object(embed_mod, "_runtime_for", return_value=_runtime([[0.1]])):
            resp = embed_mod.embed("f10_embed", ["x" * 400])
        assert resp.input_tokens == 100 and resp.tokens_estimated is True

    def test_request_body_is_cohere_shaped(self):
        rt = _runtime([[0.1] * 4])
        with patch.object(embed_mod, "get_stage_config_sync", return_value=SAFE_DEFAULTS["f10_embed"]), \
             patch.object(embed_mod, "get_model_sync", return_value=_embed_model()), \
             patch.object(embed_mod, "_runtime_for", return_value=rt):
            embed_mod.embed("f10_embed", ["q"], input_type="search_query", dimensions=4)
        call = rt.invoke_model.call_args.kwargs
        assert call["modelId"] == "us.cohere-embed-v4:0"
        assert json.loads(call["body"]) == {"texts": ["q"], "input_type": "search_query",
                                            "embedding_types": ["float"], "output_dimension": 4}

    def test_route_falls_back_to_the_next_model(self):
        cfg = StageConfig("f10_embed", "embed", "cohere", "embed-a", None, fallback_model_ids=("embed-b",))
        failing, working = MagicMock(), _runtime([[0.3]])
        failing.invoke_model.side_effect = RuntimeError("throttled")
        with patch.object(embed_mod, "get_stage_config_sync", return_value=cfg), \
             patch.object(embed_mod, "get_model_sync", side_effect=lambda k: _embed_model(k)), \
             patch.object(embed_mod, "_runtime_for", side_effect=[failing, working]):
            resp = embed_mod.embed("f10_embed", ["t"])
        assert resp.model_used == "embed-b" and resp.fallback_used is True

    def test_skips_models_that_are_not_embedding_models(self):
        cfg = StageConfig("f10_embed", "embed", "cohere", "haiku", None,
                          fallback_model_ids=("cohere-embed-v4",))
        text_model = _embed_model("haiku", api_style="anthropic_native", vendor="anthropic")
        with patch.object(embed_mod, "get_stage_config_sync", return_value=cfg), \
             patch.object(embed_mod, "get_model_sync",
                          side_effect=lambda k: text_model if k == "haiku" else _embed_model(k)), \
             patch.object(embed_mod, "_runtime_for", return_value=_runtime([[0.1]])):
            resp = embed_mod.embed("f10_embed", ["t"])
        assert resp.model_used == "cohere-embed-v4"

    def test_uses_the_built_in_row_when_the_catalog_is_unreachable(self):
        with patch.object(embed_mod, "get_stage_config_sync", return_value=SAFE_DEFAULTS["f10_embed"]), \
             patch.object(embed_mod, "get_model_sync", return_value=None), \
             patch.object(embed_mod, "_runtime_for", return_value=_runtime([[0.1]], "1000")):
            resp = embed_mod.embed("f10_embed", ["t"])
        assert resp.cost_usd == pytest.approx(0.00012)  # $0.12/1M, not a Sonnet-rate fallback

    def test_raises_when_every_model_fails(self):
        rt = MagicMock()
        rt.invoke_model.side_effect = RuntimeError("down")
        with patch.object(embed_mod, "get_stage_config_sync", return_value=SAFE_DEFAULTS["f10_embed"]), \
             patch.object(embed_mod, "get_model_sync", return_value=_embed_model()), \
             patch.object(embed_mod, "_runtime_for", return_value=rt), \
             pytest.raises(RuntimeError, match="All models in the route failed"):
            embed_mod.embed("f10_embed", ["t"])

    def test_malformed_response_is_a_failure(self):
        with patch.object(embed_mod, "get_stage_config_sync", return_value=SAFE_DEFAULTS["f10_embed"]), \
             patch.object(embed_mod, "get_model_sync", return_value=_embed_model()), \
             patch.object(embed_mod, "_runtime_for", return_value=_runtime([[0.1, 0.2]])), \
             pytest.raises(RuntimeError):
            embed_mod.embed("f10_embed", ["t"], dimensions=1536)


class TestColumnMapper:
    def _resp(self, content):
        return LLMResponse(content=content, model_used="satellite-haiku", provider="bedrock-satellite",
                           input_tokens=300, output_tokens=40, cost_usd=0.0005,
                           satellite_account="acc3", stop_reason="end_turn")

    def test_goes_through_the_stage_route_and_logs_the_call(self):
        client = MagicMock()
        client.generate.return_value = self._resp('{"Tour Title": "src_name", "Junk": "not_a_field"}')
        with patch.object(column_mapper, "LLMClient", return_value=client), \
             patch.object(column_mapper, "record_call_sync") as m_log:
            mapping = column_mapper.detect_column_mapping(["Tour Title", "Junk"])
        assert mapping == {"Tour Title": "src_name"}
        assert client.generate.call_args.args[0].stage == "a0_column_map"
        log = m_log.call_args.kwargs
        assert log["stage"] == "a0_column_map" and log["role"] == "writer"
        assert log["account"] == "acc3" and log["cost_usd"] == 0.0005
        assert log["quality_signal"] == {"json_parsed": True, "mapped_columns": 1}

    def test_unparseable_reply_still_logs_the_paid_call(self):
        client = MagicMock()
        client.generate.return_value = self._resp("not json")
        with patch.object(column_mapper, "LLMClient", return_value=client), \
             patch.object(column_mapper, "record_call_sync") as m_log:
            assert column_mapper.detect_column_mapping(["A"]) == {}
        assert m_log.call_args.kwargs["quality_signal"]["json_parsed"] is False

    def test_llm_failure_returns_empty_map_and_logs_nothing(self):
        client = MagicMock()
        client.generate.side_effect = RuntimeError("All LLM providers failed")
        with patch.object(column_mapper, "LLMClient", return_value=client), \
             patch.object(column_mapper, "record_call_sync") as m_log:
            assert column_mapper.detect_column_mapping(["A"]) == {}
        m_log.assert_not_called()


class TestAdminEmbedOptions:
    def test_embed_stage_offers_only_embedding_models(self):
        from api.routers.admin_llm_ops import _options_for
        models = [_embed_model(),
                  _embed_model("haiku", api_style="anthropic_native", vendor="anthropic",
                               callable_via=("llm_client",))]
        assert [o["model_id"] for o in _options_for("embed", "f10_embed", models)] == ["cohere-embed-v4"]
        assert [o["model_id"] for o in _options_for("writer", "a0_column_map", models)] == ["haiku"]

    def test_embed_stage_has_no_options_when_the_catalog_is_unreachable(self):
        from api.routers.admin_llm_ops import _options_for
        assert _options_for("embed", "f10_embed", None) == []
