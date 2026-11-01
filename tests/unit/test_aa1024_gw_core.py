"""AA-1024 lane gw-core (S263, root plan slices A9 / A3 / A5 / I6) — the gateway core hardening, no AWS, no DB.

- A9: an embedding is priced from the deployment override, else the catalog, else a flagged estimate, rounded UP to the
  ledger's micro-dollar (a billed call is never 0 USD); the daily probe's answer carries the attempt facts; a refused
  probe row is attempt 1.
- A3: the SDKs and botocore never retry (tests in the adapter / satellite modules); a gateway request makes at most
  MAX_CHAIN_ATTEMPTS provider attempts (LLMClient and embed); every stage has a route deadline by role in
  SAFE_DEFAULTS; an API adapter's per-request timeout is never longer than the connection's.
- A5: a connection whose served_roles leave the stage's role out is not in the plan (and the invariant says so).
- I6: the catalog capability structured_output (tool / any / auto / prompt) shapes a structured Converse request; the
  contract smoke runs a model that refuses a forced tool when it states a non-forced mode.
Fixtures are invented.
"""
import dataclasses
import io
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from shared.llm_client import call_log, client as client_mod, embed as embed_mod, invariant, vendor_route
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.connections import BUILTIN_CONNECTIONS
from shared.llm_client.errors import MAX_CHAIN_ATTEMPTS, RouteAttemptCapReached, RouteDeadlineExceeded
from shared.llm_client.models import LLMRequest
from shared.llm_client.role_config import ROUTE_DEADLINE_S, SAFE_DEFAULTS, StageConfig
from tests.unit.test_aa1002_s3_deployment_routing import HAIKU, _dep, _org, world  # noqa: F401


def _embed_model(**kw):
    base = dict(model_key="cohere-embed-v4", label="Cohere Embed v4", vendor="cohere", provider="bedrock",
                api_style="embed", bedrock_profile_ids={"acc2": "global.cohere.embed-v4:0"}, callable_via=("embed",),
                price_in_per_mtok=0.12, price_out_per_mtok=0.0, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


# ── A9: embedding price and probe facts ─────────────────────────────────────────────────────────────────────────

def test_a_three_token_probe_is_one_micro_dollar_not_zero():
    with patch("shared.llm_client.pricing.deployment_for", return_value=None):
        cost, mode = embed_mod.price_embedding(_embed_model(), "acc2", 3)        # 3 x 0.12 / 1e6 = 0.00000036
    assert cost == 0.000001 and mode == "catalog"


def test_a_large_batch_is_priced_at_the_catalog_rate():
    with patch("shared.llm_client.pricing.deployment_for", return_value=None):
        cost, _ = embed_mod.price_embedding(_embed_model(), "acc2", 96_000)
    assert cost == pytest.approx(0.01152)


def test_the_deployment_override_wins_and_an_unpriced_model_is_an_estimate():
    dep = MagicMock(price_in_override=0.5)
    with patch("shared.llm_client.pricing.deployment_for", return_value=dep):
        assert embed_mod.price_embedding(_embed_model(), "acc3", 2_000_000) == (1.0, "override")
    with patch("shared.llm_client.pricing.deployment_for", return_value=None):
        cost, mode = embed_mod.price_embedding(_embed_model(price_in_per_mtok=None), "acc2", 1_000_000)
    assert mode == "estimate" and cost == pytest.approx(embed_mod.EMBED_ESTIMATE_PER_MTOK)
    with patch("shared.llm_client.pricing.deployment_for", return_value=None):
        assert embed_mod.price_embedding(_embed_model(), "acc2", 0) == (0.0, "catalog")


def test_the_built_in_row_is_priced_as_code_table():
    fallback = embed_mod._CATALOG_FALLBACK["cohere-embed-v4"]
    with patch("shared.llm_client.pricing.deployment_for", return_value=None):
        assert embed_mod.price_embedding(fallback, "acc2", 1000)[1] == "code_table"


def _runtime(tokens="3"):
    rt = MagicMock()
    rt.invoke_model.return_value = {
        "body": io.BytesIO(json.dumps({"embeddings": {"float": [[0.1, 0.2]]}}).encode()),
        "ResponseMetadata": {"HTTPHeaders": {"x-amzn-bedrock-input-token-count": tokens}}}
    return rt


def test_the_probe_answer_carries_the_attempt_facts_and_a_real_price(monkeypatch):
    monkeypatch.setattr(embed_mod.breaker, "guarded", lambda conn, model_key, call, **kw: call())
    monkeypatch.setattr(embed_mod, "_runtime_for", lambda account: _runtime())
    with patch("shared.llm_client.pricing.deployment_for", return_value=None):
        resp = embed_mod.probe(_embed_model(), BUILTIN_CONNECTIONS["acc2"], "global.cohere.embed-v4:0", ["a hut"])
    fields = call_log.billed_fields(resp)
    assert fields["connection_key"] == "acc2" and fields["attempt_no"] == 1 and fields["latency_ms"] >= 0
    assert fields["deployment_wire_id"] == "global.cohere.embed-v4:0" and fields["pricing_mode"] == "catalog"
    assert resp.cost_usd == 0.000001                    # was 0 on Dev (S263): three tokens rounded away


@pytest.mark.asyncio
async def test_a_refused_embedding_probe_row_is_attempt_one(monkeypatch):
    from botocore.exceptions import ClientError

    from services.jobs import llm_probe_job as probe
    from tests.unit.test_aa642_l2_probe_more import _Pool, _patch_embedding

    err = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "InvokeModel")
    rec = _patch_embedding(monkeypatch, err)
    await probe.probe_embedding(_Pool(), "acc3", "embed-x", "wire-embed-x")
    kw = rec.await_args.kwargs
    assert kw["attempt_no"] == 1 and kw["latency_ms"] is not None and kw["connection_key"] == "acc3"


# ── A3: the chain cap and the route deadline on embed ──────────────────────────────────────────────────────────

@pytest.fixture
def rows(monkeypatch):
    seen: list = []

    async def record_calls(rows):
        seen.extend(rows)
    monkeypatch.setattr(call_log, "record_calls", record_calls)
    yield seen


def test_embed_stops_after_the_attempt_cap(rows, monkeypatch):
    targets = [(f"conn{i}", f"wire{i}", None) for i in range(MAX_CHAIN_ATTEMPTS + 2)]
    tried = []

    def failing(model, account, *a, **kw):
        tried.append(account)
        raise RuntimeError("outage")
    monkeypatch.setattr(embed_mod, "_plan_targets", lambda *a, **kw: (targets, []))
    monkeypatch.setattr(embed_mod, "_call", failing)
    with patch.object(embed_mod, "get_stage_config_sync", return_value=SAFE_DEFAULTS["f10_embed"]), \
         patch.object(embed_mod, "get_model_sync", return_value=_embed_model()):
        with pytest.raises(RouteAttemptCapReached, match="attempt cap of 6"):
            embed_mod.embed("f10_embed", ["a quiet lake"])
    call_log.wait_for_pending_writes()
    assert len(tried) == MAX_CHAIN_ATTEMPTS
    assert sorted(r["attempt_no"] for r in rows if r["status"] == "error" and r["error_class"] == "other") == \
        list(range(1, MAX_CHAIN_ATTEMPTS + 1))          # background writes: any order
    cap = [r for r in rows if r["error_class"] == "deadline"]
    assert len(cap) == 1 and cap[0]["cost_usd"] == 0 and cap[0]["account"] == f"conn{MAX_CHAIN_ATTEMPTS}"


def test_embed_starts_no_attempt_after_the_route_deadline(rows, monkeypatch):
    cfg = dataclasses.replace(SAFE_DEFAULTS["f10_embed"], route_deadline_s=5)
    now = [100.0]
    monkeypatch.setattr(embed_mod.time, "monotonic", lambda: now[0])

    def slow_outage(*a, **kw):
        now[0] += 10                                   # the first attempt takes 10 s, past the 5 s deadline
        raise RuntimeError("slow outage")
    monkeypatch.setattr(embed_mod, "_plan_targets", lambda *a, **kw: ([("a", "w1", None), ("b", "w2", None)], []))
    monkeypatch.setattr(embed_mod, "_call", MagicMock(side_effect=slow_outage))
    with patch.object(embed_mod, "get_stage_config_sync", return_value=cfg), \
         patch.object(embed_mod, "get_model_sync", return_value=_embed_model()):
        with pytest.raises(RouteDeadlineExceeded, match="route deadline of 5 s") as e:
            embed_mod.embed("f10_embed", ["x"])
    assert not isinstance(e.value, RouteAttemptCapReached)
    assert embed_mod._call.call_count == 1


# ── A3: LLMClient chain cap, deadlines by role, timeouts ───────────────────────────────────────────────────────

def test_every_safe_default_has_the_route_deadline_of_its_role():
    assert ROUTE_DEADLINE_S == {"writer": 1200, "judge": 900, "validate": 600, "embed": 120}
    for stage, cfg in SAFE_DEFAULTS.items():
        assert cfg.route_deadline_s == ROUTE_DEADLINE_S[cfg.role], stage


def test_a_route_makes_at_most_six_provider_attempts(world, monkeypatch):
    extra = [f"org{i}" for i in range(4)]
    for i, key in enumerate(extra):
        world.conns[key] = _org(connection_key=key)
        world.deps.append(_dep("sonnet-5-5", key, f"us.anthropic.claude-sonnet-5-5-{i}", 20 + i))
        world.deps.append(_dep("gpt-6-luna", key, f"us.openai.gpt-6-luna-{i}", 20 + i))
    world.reset()
    world.fail = {k: RuntimeError(f"{k} outage") for k in ["acc3", *extra]}
    logged = []
    real = client_mod.log_gateway_failed_attempt
    monkeypatch.setattr(client_mod, "log_gateway_failed_attempt",
                        lambda request, exc, **kw: logged.append((type(exc).__name__, kw.get("model"))) or
                        real(request, exc, **kw))
    cfg = StageConfig(stage="adhoc_cap", role="judge", provider="openai", model_id="sonnet-5-5",
                      account_route="acc3", fallback_model_ids=("gpt-6-luna",))
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda stage: cfg)
    with pytest.raises(RouteAttemptCapReached):
        world.generate(stage="adhoc_cap")
    assert len(world.calls) == MAX_CHAIN_ATTEMPTS == 6
    assert ("RouteAttemptCapReached", "gpt-6-luna") in logged


def test_the_per_request_timeout_never_exceeds_the_connections():
    token = client_mod._DEADLINE.set((time.monotonic() + 900, 900))
    try:
        assert client_mod.request_timeout(300) == 300
        assert 890 < client_mod.request_timeout() <= 900
    finally:
        client_mod._DEADLINE.reset(token)
    assert client_mod.request_timeout(300) is None                      # no deadline: the connection's own


# ── A5: served_roles ──────────────────────────────────────────────────────────────────────────────────────────

def _acc2_haiku(world, served):
    world.conns["acc2"] = dataclasses.replace(BUILTIN_CONNECTIONS["acc2"], served_roles=served)
    world.deps = [d for d in world.deps if not (d.model_key == "haiku" and d.connection_key == "acc2")]
    world.deps.append(_dep("haiku", "acc2", "us.anthropic.claude-haiku-4-5-20251001-v1:0", 20))
    world.reset()


def test_a_writer_plan_leaves_out_a_connection_that_serves_no_writer(world):
    _acc2_haiku(world, ("embed", "judge", "validate"))
    keys = lambda role: [t.connection.connection_key for t in vendor_route.deployment_plan(HAIKU, role=role)]  # noqa
    assert keys("writer") == ["acc3"]
    assert keys("judge") == ["acc3", "acc2"]
    assert keys(None) == ["acc3", "acc2"]                              # no role: no filter


def test_null_served_roles_serve_every_role(world):
    _acc2_haiku(world, None)
    assert [t.connection.connection_key for t in vendor_route.deployment_plan(HAIKU, role="writer")] == \
        ["acc3", "acc2"]


def test_a_writer_stage_never_calls_acc2(world, monkeypatch):
    _acc2_haiku(world, ("embed", "judge", "validate"))
    world.fail = {"acc3": RuntimeError("Error 002")}
    cfg = StageConfig(stage="adhoc_writer", role="writer", provider="claude", model_id="haiku",
                      account_route="acc3")
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda stage: cfg)
    with patch.dict("os.environ", {"LLM_LEGACY_ALIASES": "on"}):
        with pytest.raises(RuntimeError):            # LLMAccessDenied / the last acc3 error: never an acc2 answer
            world.generate(stage="adhoc_writer")
    assert all(c[1] != "native:acc2" for c in world.calls), world.calls


def test_stage_role_reads_the_row_then_the_request():
    judge = StageConfig("t10_judge", "judge", "openai", "gpt-6-luna", "acc3")
    with patch.object(client_mod, "get_stage_config_sync", return_value=judge):
        assert client_mod.stage_role(LLMRequest(system_prompt="s", user_prompt="u", stage="t10_judge")) == "judge"
    unknown = StageConfig("unknown", "writer", "claude", "haiku", "acc3")
    with patch.object(client_mod, "get_stage_config_sync", return_value=unknown):
        assert client_mod.stage_role(LLMRequest(system_prompt="s", user_prompt="u", stage="adhoc_x",
                                                role="validate")) == "validate"
        assert client_mod.stage_role(LLMRequest(system_prompt="s", user_prompt="u", stage="adhoc_x")) is None
    assert client_mod.stage_role(LLMRequest(system_prompt="s", user_prompt="u")) is None


def test_the_invariant_counts_only_connections_that_serve_the_role():
    route = {"stage": "t2_generate", "role": "writer", "model_id": "haiku", "fallback_model_ids": [],
             "allowed_scopes": ["aws_only"], "is_active": True}
    deps = [{"model_key": "haiku", "connection_key": "acc3", "priority": 10, "enabled": True},
            {"model_key": "haiku", "connection_key": "acc2", "priority": 20, "enabled": True}]
    conns = [{"connection_key": "acc3", "kind": "bedrock", "enabled": True, "data_scope": "aws_only",
              "failure_domain": "bedrock:acc3:aws_only", "served_roles": ["writer", "judge", "validate", "embed"]},
             {"connection_key": "acc2", "kind": "bedrock", "enabled": True, "data_scope": "aws_only",
              "failure_domain": "bedrock:acc2:aws_only", "served_roles": ["embed", "judge", "validate"]}]
    out = invariant.check([route], [], deps, conns)
    stage = out["stages"][0]
    assert stage["status"] == "breach" and stage["other_domains"] == []
    assert stage["role_skipped"] == ["haiku@acc2"]
    judge = dict(route, stage="s1_judge", role="judge")
    assert invariant.check([judge], [], deps, conns)["stages"][0]["status"] == "ok"
    assert "served_roles" in invariant.CONNECTIONS_V2_SQL


# ── I6: structured output modes on Converse ───────────────────────────────────────────────────────────────────

MAVERICK = CatalogModel(model_key="llama4-maverick", label="Meta Llama 4 Maverick", vendor="meta",
                        provider="bedrock", api_style="converse",
                        bedrock_profile_ids={"acc2": "us.meta.llama4-maverick-17b-instruct-v1:0"},
                        callable_via=("llm_client",), price_in_per_mtok=0.24, price_out_per_mtok=0.97,
                        enabled=True, family="meta")
SCHEMA = {"name": "verdict", "schema": {"type": "object", "properties": {"verdict": {"type": "string"}},
                                        "required": ["verdict"], "additionalProperties": False}}


@pytest.mark.parametrize("caps,expected", [
    ({}, "tool"),
    ({"forced_tool_choice": False}, "auto"),
    ({"forced_tool_choice": False, "structured_output": "prompt"}, "prompt"),
    ({"forced_tool_choice": False, "structured_output": "any"}, "any"),
    ({"structured_output": "bogus"}, "tool"),
])
def test_structured_mode_reads_the_catalog_row(caps, expected):
    model = dataclasses.replace(MAVERICK, capabilities=caps)
    assert client_mod.structured_mode(model, "us.meta.llama4-maverick-17b-instruct-v1:0") == expected


def _converse(mode, reply_blocks):
    model = dataclasses.replace(MAVERICK, capabilities={"forced_tool_choice": False, "structured_output": mode})
    rt = MagicMock()
    rt.converse.return_value = {"output": {"message": {"content": reply_blocks}},
                                "usage": {"inputTokens": 40, "outputTokens": 9}, "stopReason": "end_turn"}
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = client_mod.LLMClient()
    request = LLMRequest(system_prompt="Say if the two huts are one place.", user_prompt="Varro hut / Varro lodge",
                         json_schema=SCHEMA, max_tokens=200)
    with patch("shared.llm_client.pricing._catalog_rates", lambda m: None), \
         patch("shared.llm_client.client.stream_sink.delta_callback", lambda stage: None):
        resp = c._call_bedrock_converse(request, model, "acc2", profile_id=model.bedrock_profile_ids["acc2"],
                                        runtime=rt)
    return resp, rt.converse.call_args.kwargs


def test_prompt_mode_sends_no_tool_and_takes_the_json_after_prose():
    resp, kw = _converse("prompt", [{"text": 'Here is my answer:\n{"verdict": "different"} Thanks.'}])
    assert "toolConfig" not in kw
    assert "JSON schema 'verdict'" in kw["system"][-1]["text"]
    assert json.loads(resp.content) == {"verdict": "different"}


def test_any_mode_forces_a_tool_without_naming_it():
    resp, kw = _converse("any", [{"toolUse": {"toolUseId": "t", "name": "verdict", "input": {"verdict": "same"}}}])
    assert kw["toolConfig"]["toolChoice"] == {"any": {}}
    assert json.loads(resp.content) == {"verdict": "same"}


def test_a_prompt_mode_reply_that_breaks_the_schema_is_retried_then_billed_unusable():
    with pytest.raises(client_mod.BilledUnusableReply):
        _converse("prompt", [{"text": '{"answer": "maybe"}'}])


def test_the_llama_wording_of_a_refused_tool_choice_is_recognised():
    exc = RuntimeError("An error occurred (ValidationException) when calling the Converse operation: This model "
                       "doesn't support the toolConfig.toolChoice.tool field. Remove toolConfig.toolChoice.tool and "
                       "try again.")
    assert client_mod._tool_choice_rejected(exc)


def test_the_smoke_runs_a_model_that_states_a_non_forced_mode():
    from services.jobs import llm_contract_smoke_job as smoke
    refusing = dataclasses.replace(MAVERICK, capabilities={"forced_tool_choice": False})
    prompt = dataclasses.replace(MAVERICK, capabilities={"forced_tool_choice": False, "structured_output": "prompt"})
    assert smoke.refused_capabilities(refusing, ("forced_tool_choice",)) == ["forced_tool_choice"]
    assert smoke.refused_capabilities(prompt, ("forced_tool_choice",)) == []
