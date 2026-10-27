"""S251 (AA-835 live run) — a json_schema request on a legacy haiku/sonnet key never loses its schema.

The legacy chain calls the Anthropic Messages API (invoke_claude), which has no tool / response_format here, so before
S251 the schema was dropped silently and supply_content_writer ('sonnet', CIS 278) got free text ("reply is not
JSON"). Now: the schema is asked for in the system prompt, the reply's JSON object is taken (``` fence tolerated) and
conformed with _checked_json; an unusable reply raises BilledUnusableReply, which _call_key_logged writes as a
billed_unusable llm_call_log row, and a route moves to its next model. A request WITHOUT json_schema is unchanged.

Guards (no AWS, no paid call: invoke_claude / the satellite session are stubs):
1. legacy + schema, fenced reply -> the conformed JSON string; the caller's system prompt stays first.
2. legacy + schema, prose / schema mismatch -> BilledUnusableReply + one gateway billed_unusable row.
3. legacy + schema in a route -> an unusable reply moves to the next model.
4. legacy without schema -> the request object and the Bedrock body are exactly what they were before S251.
5. one warning per stage.
6. SAFE_DEFAULTS: a stage whose caller sends json_schema has a catalog (Converse / OpenAI) primary, except the
   listed, known legacy check stage.
7. review fixes: a route where every model answered unusably raises RouteUnusableReply (not on an outage); no live
   text for a structured request on the legacy path; an empty zero-token legacy reply writes no billed row.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import bedrock_satellite as bs
from shared.llm_client import client as client_mod
from shared.llm_client.catalog import LEGACY_KEYS
from shared.llm_client.client import (
    BilledUnusableReply,
    LLMClient,
    RouteUnusableReply,
    UnusableReply,
    unusable_reply_cost,
)
from shared.llm_client.models import LLMRequest
from shared.llm_client.role_config import SAFE_DEFAULTS, StageConfig

SCHEMA = {"name": "supply_content", "schema": {
    "type": "object", "additionalProperties": False, "required": ["description", "highlights"],
    "properties": {"description": {"type": "string"}, "highlights": {"type": "array", "items": {"type": "string"}}}}}
GOOD = {"description": "A calm hotel.", "highlights": ["Pool"]}
STAGE_CFG = StageConfig("supply_content_writer", "writer", "claude", "sonnet", "acc3")


@pytest.fixture(autouse=True)
def _fresh_warning_memory():
    client_mod._LEGACY_SCHEMA_WARNED.clear()
    yield
    client_mod._LEGACY_SCHEMA_WARNED.clear()


def _client():
    return LLMClient.__new__(LLMClient)   # no boto3 / openai clients: every provider call below is a stub


def _result(text, model="sonnet-4-6"):
    return MagicMock(text=text, model_used=model, latency_ms=5.0, stop_reason="end_turn",
                     usage={"input_tokens": 900, "output_tokens": 150, "cache_read_input_tokens": 0,
                            "cache_creation_input_tokens": 0})


def _request(**kw):
    base = {"system_prompt": "You write hotel copy.", "user_prompt": "facts", "stage": "supply_content_writer",
            "temperature": 0.4, "max_tokens": 1200, "json_schema": SCHEMA}
    base.update(kw)
    return LLMRequest(**base)


def _generate(request, replies, stage_cfg=STAGE_CFG):
    """generate() with invoke_claude answering `replies` in order; returns (resp or exc, invoke kwargs list,
    gateway billed rows)."""
    billed_rows = []
    with patch("shared.llm_client.bedrock_satellite.invoke_claude", side_effect=list(replies)) as inv, \
            patch("shared.llm_client.client.get_stage_config_sync", return_value=stage_cfg), \
            patch("shared.llm_client.client.log_gateway_billed_failure",
                  side_effect=lambda req, **kw: billed_rows.append(kw)):
        try:
            out = _client().generate(request)
        except Exception as e:  # noqa: BLE001 — returned for the test to assert on
            out = e
    return out, [c.kwargs for c in inv.call_args_list], billed_rows


# ── 1. fenced reply -> conformed JSON ───────────────────────────────────────────────────────────────────────────────

def test_fenced_reply_is_conformed_and_the_schema_is_asked_for():
    fenced = "```json\n" + json.dumps(GOOD) + "\n```"
    resp, calls, billed = _generate(_request(), [_result(fenced)])
    assert not isinstance(resp, Exception), resp
    assert json.loads(resp.content) == GOOD
    assert resp.content == json.dumps(GOOD)          # _checked_json's canonical text, no fence
    assert resp.model_used == "satellite-sonnet-4-6" and resp.satellite_account == "acc3"
    assert billed == []
    system = calls[0]["system"]
    assert system.startswith("You write hotel copy.\n\n")             # caller's prompt first, unchanged
    assert "exactly one JSON object" in system and "'supply_content'" in system
    assert json.dumps(SCHEMA["schema"], ensure_ascii=False, sort_keys=True) in system
    assert calls[0]["temperature"] == 0.4 and calls[0]["max_tokens"] == 1200   # explicit fields survive the copy


def test_plain_json_reply_is_accepted():
    resp, _calls, billed = _generate(_request(), [_result(json.dumps(GOOD))])
    assert json.loads(resp.content) == GOOD and billed == []


# ── 2. prose / mismatch -> billed unusable ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reply", [
    "Here is the content:\n```json\n" + json.dumps(GOOD) + "\n```",       # prose before the fence
    "A calm hotel with a pool.",                                         # no JSON at all
    json.dumps({"description": "A calm hotel."}),                        # missing required key
    json.dumps({**GOOD, "price": "$80"}),                                # additionalProperties false
    json.dumps([GOOD]),                                                  # not an object
])
def test_unusable_reply_raises_billed_unusable_and_logs_one_row(reply):
    exc, _calls, billed = _generate(_request(), [_result(reply)])
    assert isinstance(exc, BilledUnusableReply), exc
    assert len(billed) == 1
    row = billed[0]["billed"]
    assert len(row) == 1
    assert row[0]["model"] == "satellite-sonnet-4-6" and row[0]["account"] == "acc3"
    assert row[0]["tokens_in"] == 900 and row[0]["tokens_out"] == 150 and row[0]["cost_usd"] > 0
    assert row[0]["provider"] == "bedrock-satellite"
    assert billed[0]["role"] == "writer" and billed[0]["extra"] == {"position": 0}


def test_billed_rows_are_built_like_the_converse_path_rows():
    """The real log_gateway_billed_failure turns the attempt into a billed_unusable row with no ok-rate key."""
    from shared.llm_client.call_log import OK_RATE_KEYS
    written = []
    with patch("shared.llm_client.bedrock_satellite.invoke_claude", return_value=_result("not json")), \
            patch("shared.llm_client.client.get_stage_config_sync", return_value=STAGE_CFG), \
            patch("shared.llm_client.call_log._in_background", side_effect=lambda fn, kw: written.extend(kw["rows"])), \
            pytest.raises(BilledUnusableReply):
        _client().generate(_request())
    assert len(written) == 1
    row = written[0]
    assert row["stage"] == "supply_content_writer" and row["model"] == "satellite-sonnet-4-6"
    assert row["quality_signal"]["billed_unusable"] is True and row["quality_signal"]["gateway_attempt"] is True
    assert not set(row["quality_signal"]) & set(OK_RATE_KEYS)


# ── 3. in a route, an unusable legacy reply moves on ────────────────────────────────────────────────────────────────

def test_route_moves_to_the_next_model_after_an_unusable_legacy_reply():
    cfg = StageConfig("supply_content_writer", "writer", "claude", "sonnet", "acc3", fallback_model_ids=("haiku",))
    resp, calls, billed = _generate(_request(), [_result("Sorry, here it is: {...}"),
                                                 _result(json.dumps(GOOD), model="haiku-4-5")], stage_cfg=cfg)
    assert not isinstance(resp, Exception), resp
    assert resp.model_used == "satellite-haiku-4-5" and resp.fallback_used is True
    assert [c["model"] for c in calls] == ["sonnet", "haiku"]
    assert len(billed) == 1 and billed[0]["extra"] == {"position": 0}


# ── 4. no schema -> unchanged ───────────────────────────────────────────────────────────────────────────────────────

def test_request_without_schema_reaches_the_legacy_chain_unchanged():
    req = _request(json_schema=None)
    c = _client()
    with patch("shared.llm_client.client.get_stage_config_sync", return_value=STAGE_CFG), \
            patch.object(c, "_legacy_chain", return_value=MagicMock(fallback_used=False)) as chain, \
            patch.object(c, "_legacy_structured") as structured:
        c.generate(req)
    structured.assert_not_called()
    assert chain.call_args.args[0] is req                  # the same object, not a copy with a longer prompt


def test_request_without_schema_sends_the_pre_s251_satellite_call_and_returns_the_raw_text():
    fenced = "```json\n" + json.dumps(GOOD) + "\n```"
    req = _request(json_schema=None)
    resp, calls, billed = _generate(req, [_result(fenced)])
    assert resp.content == fenced                           # no post-processing without a schema
    assert billed == []
    assert calls == [{"model": "sonnet", "max_tokens": 1200, "system": "You write hotel copy.", "account": "acc3",
                      "on_delta": None, "temperature": 0.4}]


def test_request_without_schema_sends_a_byte_identical_bedrock_body():
    """One level down: the invoke_model body equals what invoke_claude builds from the caller's own fields."""
    def body_of(run):
        rt = MagicMock()
        payload = {"content": [{"text": "ok"}], "usage": {"input_tokens": 1, "output_tokens": 1},
                   "stop_reason": "end_turn"}
        rt.invoke_model.side_effect = lambda **kw: {"body": __import__("io").BytesIO(json.dumps(payload).encode())}
        session = MagicMock()
        session.client.return_value = rt
        with patch.object(bs, "_get_satellite_session", return_value=session):
            run()
        return rt.invoke_model.call_args.kwargs

    req = _request(json_schema=None)

    def via_gateway():
        with patch("shared.llm_client.client.get_stage_config_sync", return_value=STAGE_CFG):
            _client().generate(req)

    def direct():
        bs.invoke_claude(req.user_prompt, model="sonnet", max_tokens=req.max_tokens, system=req.system_prompt,
                         account="acc3", on_delta=None, temperature=req.temperature)

    assert body_of(via_gateway) == body_of(direct)


# ── 5. one warning per stage ────────────────────────────────────────────────────────────────────────────────────────

def test_warns_once_per_stage():
    with patch("shared.llm_client.client.logger") as log:
        for _ in range(3):
            _generate(_request(), [_result(json.dumps(GOOD))])
        _generate(_request(stage="a3_place_dedupe_check"), [_result(json.dumps(GOOD))])
    events = [c for c in log.warning.call_args_list if c.args and c.args[0] == "llm_legacy_key_structured_output"]
    assert [e.kwargs["stage"] for e in events] == ["supply_content_writer", "a3_place_dedupe_check"]


# ── 6. SAFE_DEFAULTS guard ──────────────────────────────────────────────────────────────────────────────────────────

# Every stage whose caller builds an LLMRequest with json_schema= (S251 inventory of AA-CIS-App).
STRUCTURED_STAGES = ("supply_content_writer", "a3_place_dedupe", "a3_place_dedupe_check", "s1_source_facts",
                     "s1_brand_audit", "t2_brand_audit", "writing_memory_mine", "writing_memory_eval")
# Known legacy primaries on a structured stage: the schema is asked by prompt only (this file's gateway path). Move a
# stage to a catalog key, then drop it from here; never add one without a decision.
LEGACY_STRUCTURED_ALLOWED = {"a3_place_dedupe_check"}


@pytest.mark.parametrize("stage", STRUCTURED_STAGES)
def test_structured_stage_primary_is_a_catalog_key(stage):
    cfg = SAFE_DEFAULTS[stage]
    if stage in LEGACY_STRUCTURED_ALLOWED:
        assert cfg.model_id in LEGACY_KEYS, f"{stage} moved off its legacy key: drop it from the allow list"
        return
    assert cfg.model_id not in LEGACY_KEYS, f"{stage}: legacy primary {cfg.model_id!r} has no structured output"


def test_supply_content_writer_default_is_sonnet_5_5_with_the_legacy_sonnet_last():
    cfg = SAFE_DEFAULTS["supply_content_writer"]
    assert (cfg.model_id, cfg.fallback_model_ids, cfg.role, cfg.account_route) == \
        ("sonnet-5-5", ("sonnet",), "writer", "acc3")


# ── 7. review fixes ─────────────────────────────────────────────────────────────────────────────────────────────────

ROUTE_CFG = StageConfig("supply_content_writer", "writer", "claude", "sonnet", "acc3", fallback_model_ids=("haiku",))


def test_route_with_only_unusable_replies_raises_route_unusable_with_the_logged_usage():
    exc, _calls, billed = _generate(_request(), [_result("no json"), _result("still prose", model="haiku-4-5")],
                                    stage_cfg=ROUTE_CFG)
    assert isinstance(exc, RouteUnusableReply) and isinstance(exc, UnusableReply), exc
    assert str(exc).startswith("All models in the route failed for stage 'supply_content_writer'")
    assert len(billed) == 2                                   # the gateway wrote one row per attempt
    assert [u["model"] for u in exc.logged_usage] == ["satellite-sonnet-4-6", "satellite-haiku-4-5"]
    assert not getattr(exc, "billed_usage", ())               # never re-logged through _billed_usage_of
    assert client_mod._billed_usage_of(exc) == []
    assert unusable_reply_cost(exc) == pytest.approx(sum(r["billed"][0]["cost_usd"] for r in billed))


def test_route_with_an_outage_keeps_the_plain_runtime_error():
    exc, _calls, billed = _generate(_request(), [_result("no json"), bs.BedrockUnavailable("down")],
                                    stage_cfg=ROUTE_CFG)
    assert type(exc) is RuntimeError and "All models in the route failed" in str(exc), exc
    assert len(billed) == 1


def test_unusable_reply_cost_of_other_errors_is_zero():
    assert unusable_reply_cost(RuntimeError("down")) == 0.0
    assert issubclass(BilledUnusableReply, UnusableReply) and issubclass(UnusableReply, RuntimeError)


def test_structured_request_on_the_legacy_satellite_streams_nothing():
    with patch("shared.llm_client.client.stream_sink.delta_callback", return_value=lambda t: None):
        _resp, calls, _billed = _generate(_request(), [_result(json.dumps(GOOD))])
        _resp2, calls2, _billed2 = _generate(_request(json_schema=None), [_result("text")])
    assert calls[0]["on_delta"] is None                       # structured: no live raw JSON
    assert calls2[0]["on_delta"] is not None                  # free text keeps its live view


def test_structured_request_on_legacy_bedrock_streams_nothing():
    chunks = [{"type": "message_start", "message": {"usage": {"input_tokens": 9}}},
              {"type": "content_block_delta", "delta": {"type": "text_delta", "text": json.dumps(GOOD)}},
              {"type": "message_delta", "usage": {"output_tokens": 5}, "delta": {"stop_reason": "end_turn"}}]

    def run(request):
        seen = []
        c = _client()
        c._bedrock = MagicMock()
        c._bedrock.invoke_model_with_response_stream.return_value = {
            "body": [{"chunk": {"bytes": json.dumps(ch).encode()}} for ch in chunks]}
        with patch("shared.llm_client.client.stream_sink.delta_callback", return_value=seen.append):
            c._call_bedrock(request, "us.anthropic.claude-sonnet-4-6")
        return seen

    assert run(_request()) == []
    assert run(_request(json_schema=None)) == [json.dumps(GOOD)]


def test_empty_zero_token_legacy_reply_writes_no_billed_row():
    empty = MagicMock(text="", model_used="sonnet-4-6", latency_ms=5.0, stop_reason="end_turn",
                      usage={"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0})
    exc, _calls, billed = _generate(_request(), [empty])
    assert type(exc) is RuntimeError and "empty content" in str(exc), exc
    assert billed == []
