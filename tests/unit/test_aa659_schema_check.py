"""AA-659 — structured outputs are checked against their schema; a mismatch moves the route on."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services.content_generation.brand_audit_node import BRAND_AUDIT_SCHEMA
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient
from shared.llm_client.models import LLMRequest
from shared.llm_client.role_config import StageConfig
from shared.llm_client.schema_check import SchemaMismatch, conform

GOOD_AUDIT = {"brand_audit": {
    "status": "flagged", "publish_ready": False, "fields_to_fix": ["seo_meta"],
    "failure_codes": ["META_TOO_SHORT"], "issues": ["meta too short"],
    "scores": {"brand_fit": 1, "human_read": 1, "seo_fit": 0, "trip_type_accuracy": 1,
               "publish_readiness": 0},
    "notes": "n",
    "lessons_extracted": [{"pattern": "p", "failure_code": "META_TOO_SHORT", "field": "seo_meta",
                           "example_before": "x", "severity": "low"}],
}}


def test_valid_output_passes_unchanged():
    assert conform(GOOD_AUDIT, BRAND_AUDIT_SCHEMA) == GOOD_AUDIT


def test_nested_object_as_json_string_is_decoded():
    wrapped = {"brand_audit": json.dumps(GOOD_AUDIT["brand_audit"])}
    assert conform(wrapped, BRAND_AUDIT_SCHEMA) == GOOD_AUDIT


@pytest.mark.parametrize("mutate,needle", [
    (lambda b: b.pop("failure_codes"), "missing required key 'failure_codes'"),
    (lambda b: b.update(status="ok"), "not in"),
    (lambda b: b.update(extra=1), "unexpected keys"),
    (lambda b: b["scores"].update(brand_fit=True), "expected integer"),
    (lambda b: b.update(issues="one issue"), "non-JSON string"),
])
def test_mismatches_raise(mutate, needle):
    bad = json.loads(json.dumps(GOOD_AUDIT))
    mutate(bad["brand_audit"])
    with pytest.raises(SchemaMismatch, match=needle):
        conform(bad, BRAND_AUDIT_SCHEMA)


SCHEMA = {"name": "brand_audit_result", "schema": BRAND_AUDIT_SCHEMA}
LUNA = CatalogModel(model_key="gpt-5.6-luna", label="l", vendor="openai", provider="bedrock",
                    api_style="converse", bedrock_profile_ids={"acc3": "global.openai.gpt-5.6-luna"},
                    callable_via=("llm_client",), supports_temperature=False,
                    price_in_per_mtok=0.2, price_out_per_mtok=1.2, enabled=True)


def _client():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        return LLMClient()


def _converse_with_tool_input(tool_input):
    rt = MagicMock()
    rt.converse.return_value = {
        "output": {"message": {"content": [{"toolUse": {"name": "brand_audit_result", "input": tool_input}}]}},
        "usage": {"inputTokens": 10, "outputTokens": 5}, "stopReason": "tool_use",
    }
    return rt


def test_converse_bad_tool_input_raises():
    rt = _converse_with_tool_input({"brand_audit": {"status": "pass"}})
    with patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt), \
            pytest.raises(RuntimeError, match="does not match schema"):
        _client()._call_bedrock_converse(
            LLMRequest(system_prompt="s", user_prompt="u", json_schema=SCHEMA), LUNA, "acc3")


def test_converse_string_encoded_tool_input_is_repaired():
    rt = _converse_with_tool_input({"brand_audit": json.dumps(GOOD_AUDIT["brand_audit"])})
    with patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt):
        resp = _client()._call_bedrock_converse(
            LLMRequest(system_prompt="s", user_prompt="u", json_schema=SCHEMA), LUNA, "acc3")
    assert json.loads(resp.content) == GOOD_AUDIT


def test_openai_invalid_structured_output_raises():
    c = _client()
    c._openai = MagicMock()
    c._openai.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"brand_audit": {}}'),
                                 finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )
    with pytest.raises(RuntimeError, match="does not match schema"):
        c._call_openai(LLMRequest(system_prompt="s", user_prompt="u", json_schema=SCHEMA),
                       model="gpt-4.1")


def test_route_moves_on_when_the_primary_output_is_malformed():
    c = _client()
    cfg = StageConfig("s1_brand_audit", "judge", "openai", "gpt-5.6-luna", None,
                      fallback_model_ids=("gpt-6-luna-openai",))
    good = MagicMock(fallback_used=False)

    def fake(request, key, *_):
        if key == "gpt-5.6-luna":
            raise RuntimeError("gpt-5.6-luna output does not match schema")
        return good
    with patch("shared.llm_client.client.get_stage_config_sync", return_value=cfg), \
            patch("shared.llm_client.client.get_model_sync", return_value=LUNA), \
            patch.object(c, "_call_key", side_effect=fake):
        resp = c.generate(LLMRequest(system_prompt="s", user_prompt="u", stage="s1_brand_audit",
                                     json_schema=SCHEMA))
    assert resp is good and resp.fallback_used is True
