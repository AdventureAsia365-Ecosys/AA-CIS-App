"""S271 (AA-741 / AA-900, lane l9-token-budget-core) — the gateway owns the output-token budget.

1. The pure rules (shared/aa_llm/tokens.py): which models reason inside max_tokens, their floor, the retry ceiling.
2. The floor on the wire: a reasoning model is never sent less than the floor (also over a call site's explicit
   value), a non-reasoning model keeps the call site's ceiling, a higher ceiling is never lowered.
3. The one truncation retry per route model: a cut reply / a refused cut reply / a billed cut reply is asked once more
   with double the ceiling before the route moves on; never a third call; the ledger rows (cut row by the gateway,
   the retry's facts on the caller's row) and the attempt number; every opt-out.
4. The CI guard: no new literal ceiling below the floor, no new budget_retry=False, every model key the migrations
   name is classified.
5. The cut-rate alert of route_watch.

No AWS, no vendor, no DB, no paid call. Model names are the catalog's keys or invented ("lantern-mini").
"""
import ast
import asyncio
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.acp_shared import token_budget as tb
from shared import ops_checks
from shared.aa_llm import tokens
from shared.llm_client import call_log, route_watch, stage_params, token_floor
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient, _billed_unusable, estimate_call_usd
from shared.llm_client.models import LLMRequest, LLMResponse

ROOT = Path(__file__).resolve().parents[2]

LUNA = CatalogModel(model_key="gpt-6-luna", label="Luna", vendor="openai", provider="bedrock", api_style="converse",
                    bedrock_profile_ids={"acc3": "global.openai.gpt-6-luna"}, enabled=True)
SONNET55 = CatalogModel(model_key="sonnet-5-5", label="Sonnet 5.5", vendor="anthropic", provider="bedrock",
                        api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5-5"},
                        enabled=True, max_output_tokens=16000)
LANTERN = CatalogModel(model_key="lantern-mini", label="Lantern Mini", vendor="amazon", provider="bedrock",
                       api_style="converse", bedrock_profile_ids={"acc3": "us.example.lantern-mini"}, enabled=True)


@pytest.fixture(autouse=True)
def no_stage_params(monkeypatch):
    """No shared.stage_model_params table here (the read would try the database)."""
    monkeypatch.setattr(stage_params, "params_for", lambda stage, model_key: None)


# ── 1. the pure rules ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["sonnet-5-5", "haiku-5-5", "opus-5-5", "sonnet-5", "opus-5", "fable-5-1",
                                 "claude-fable-5", "claude-opus-5", "gpt-5.6-luna", "gpt-6-luna",
                                 "gpt-6-luna-openai", "gpt-6.1-terra", "gpt-6.1-sol", "gpt-6-sol", "gpt-6-astra",
                                 "sonnet-6", "gpt-7-nova"])
def test_reasoning_models_by_the_code_rule(key):
    assert tokens.reasoning_capable(key) and tokens.floor_for(key) == tokens.REASONING_FLOOR


@pytest.mark.parametrize("key", ["sonnet", "haiku", "sonnet-4-5", "sonnet-4-6", "opus-4-6", "opus-4-8", "haiku-4-5",
                                 "gpt-4.1", "nova-lite", "nova-2-lite", "llama4-scout", "llama4-maverick",
                                 "titan-embed-text-v2", "cohere-embed-v4", "jev-latest", "lantern-mini", "", None])
def test_models_that_do_not_reason_get_no_floor(key):
    assert not tokens.reasoning_capable(key) and tokens.floor_for(key) is None


def test_the_catalog_row_states_it_as_data():
    assert not tokens.reasoning_capable("sonnet-5-5", {"reasoning": False})
    assert tokens.reasoning_capable("lantern-mini", {"reasoning": True})
    assert tokens.floor_for("lantern-mini", {"min_output_tokens": 2000}) == 2000
    assert tokens.floor_for("sonnet-5-5", {"min_output_tokens": 8000}) == 8000
    assert tokens.floor_for("sonnet-5-5", {"min_output_tokens": "8000"}) == tokens.REASONING_FLOOR   # not an int
    assert tokens.floor_for("sonnet-5-5", {"reasoning": "yes"}) == tokens.REASONING_FLOOR           # not a bool


def test_the_floor_raises_never_lowers_and_stays_under_the_model_cap():
    assert tokens.floored(16, "gpt-6-luna") == 4096
    assert tokens.floored(12000, "gpt-6-luna") == 12000
    assert tokens.floored(16, "gpt-6-luna", model_cap=2000) == 2000
    assert tokens.floored(400, "nova-lite") == 400


@pytest.mark.parametrize("first,cap,want", [(1200, None, 2400), (4096, None, 8192), (16000, None, 24000),
                                            (24000, None, 24000), (6000, 8192, 8192), (8192, 8192, 8192)])
def test_retry_budget(first, cap, want):
    assert tokens.retry_budget(first, cap) == want
    assert tokens.retry_helps(first, cap) is (want > first)


def test_the_caller_side_helper_uses_the_core_rule():
    assert tb.retry_budget(2048) == tokens.retry_budget(2048) and tb.RETRY_CAP == tokens.RETRY_CAP


# ── 2 / 3. the gateway on the wire (Converse) ───────────────────────────────────────────────────────────────────────

def _reply(text="ok", stop="end_turn", out=20):
    return {"output": {"message": {"content": [{"text": text}] if text else []}},
            "usage": {"inputTokens": 10, "outputTokens": out}, "stopReason": stop}


@pytest.fixture
def rows(monkeypatch):
    seen: list = []

    async def record_calls(rows):
        seen.extend(rows)
    monkeypatch.setattr(call_log, "record_calls", record_calls)
    return seen


def _run(req, model, replies, *, scope_extra=None):
    """``req`` through LLMClient.generate on ``model`` (the catalog Converse path), the provider answering
    ``replies`` in order. Returns (response or exception, [maxTokens sent per call])."""
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    rt = MagicMock()
    rt.converse.side_effect = list(replies)
    out = None
    with patch("shared.llm_client.client.get_model_sync", return_value=model), \
            patch("shared.llm_client.client.get_stage_config_sync", return_value=None), \
            patch("shared.llm_client.client.deployment_plan", return_value=None), \
            patch("shared.llm_client.client.vendor_deployment_for", return_value=None), \
            patch("shared.llm_client.pricing._catalog_rates",
                  return_value={"in": 1.0, "out": 1.0, "cache_read": 0, "cache_write": 0}), \
            patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt), \
            patch("shared.llm_client.client.stream_sink.delta_callback", return_value=None):
        try:
            out = c.generate(req)
        except Exception as e:  # noqa: BLE001 — the test reads it
            out = e
    call_log.wait_for_pending_writes()
    return out, [call.kwargs["inferenceConfig"]["maxTokens"] for call in rt.converse.call_args_list]


def _req(**kw):
    base = dict(system_prompt="s", user_prompt="u", stage="adhoc_s271", model_tier="gpt-6-luna")
    base.update(kw)
    return LLMRequest(**base)


def test_a_reasoning_model_gets_the_floor_over_the_call_sites_value(rows):
    resp, sent = _run(_req(max_tokens=1200), LUNA, [_reply()])
    assert sent == [4096] and resp.content == "ok" and resp.truncation_retry_max_tokens is None and rows == []


def test_the_route_test_ceiling_of_16_is_lifted_too(rows):
    assert _run(_req(max_tokens=16), LUNA, [_reply()])[1] == [4096]


def test_a_higher_ceiling_and_a_model_that_does_not_reason_keep_their_value(rows):
    assert _run(_req(max_tokens=12000), LUNA, [_reply()])[1] == [12000]
    assert _run(_req(max_tokens=400, model_tier="lantern-mini"), LANTERN, [_reply()])[1] == [400]


def test_the_floor_switch(rows, monkeypatch):
    monkeypatch.setenv(token_floor.FLOOR_SWITCH_ENV, "off")
    assert _run(_req(max_tokens=1200), LUNA, [_reply()])[1] == [1200]


def test_a_cut_reply_is_retried_once_with_double_the_ceiling(rows):
    resp, sent = _run(_req(max_tokens=1200), LUNA, [_reply("half", "max_tokens", 4096), _reply("whole")])
    assert sent == [4096, 8192]
    assert resp.content == "whole" and resp.truncation_retry_max_tokens == 8192
    # the retry is the same gateway request: the cut attempt's number, the first route position, not a fallback
    assert resp.attempt_no == 1 and resp.route_position == 0 and resp.fallback_used is False
    (cut,) = rows                                   # the gateway's own row of the cut reply, written before the retry
    q = cut["quality_signal"]
    assert cut["status"] == "truncated" and cut["stop_reason"] == "max_tokens" and cut["tokens_out"] == 4096
    assert q["budget_cut"] is True and q["gateway_attempt"] is True
    assert q["retry_reason"] == "cut_reply" and resp.truncation_retry_reason == "cut_reply"      # S271 W0
    assert resp.truncation_cut_cost_usd == pytest.approx(cut["cost_usd"])
    assert (q["max_tokens"], q["retry_max_tokens"]) == (4096, 8192)
    assert cut["route_trace"] == resp.route_trace and cut["attempt_no"] == 1
    assert not set(q) & set(call_log.OK_RATE_KEYS)
    # the caller's row of the retry: billed_fields carries the retry facts into quality_signal
    fields = call_log.billed_fields(resp)
    assert fields["truncation_retry_max_tokens"] == 8192
    assert call_log._with_route({}, truncation_retry_max_tokens=8192) == {"truncation_retry": True, "max_tokens": 8192}


def test_a_reply_cut_twice_goes_back_cut_and_no_third_call_is_made(rows):
    resp, sent = _run(_req(max_tokens=4096), LUNA, [_reply("a", "max_tokens"), _reply("b", "length")])
    assert sent == [4096, 8192] and resp.content == "b" and resp.stop_reason == "length"
    assert resp.truncation_retry_max_tokens == 8192 and len(rows) == 1


def test_a_billed_unusable_cut_reply_is_retried(rows):
    """An empty reply cut at its ceiling (the reasoning used every token) is a billed unusable reply: retried."""
    resp, sent = _run(_req(max_tokens=1200), LUNA, [_reply("", "max_tokens", 4096), _reply("whole")])
    assert sent == [4096, 8192] and resp.content == "whole"
    assert resp.truncation_retry_reason == "billed_unusable_cut" and resp.truncation_cut_cost_usd > 0    # S271 W0
    assert call_log.billed_fields(resp)["truncation_retry_reason"] == "billed_unusable_cut"


def test_the_retry_stays_under_the_model_cap_and_a_request_at_the_cap_is_not_retried(rows):
    resp, sent = _run(_req(max_tokens=12000, model_tier="sonnet-5-5"), SONNET55,
                      [_reply("a", "max_tokens"), _reply("b")])
    assert sent == [12000, 16000] and resp.content == "b"
    resp, sent = _run(_req(max_tokens=16000, model_tier="sonnet-5-5"), SONNET55, [_reply("a", "max_tokens")])
    assert sent == [16000] and resp.content == "a" and resp.truncation_retry_max_tokens is None


@pytest.mark.parametrize("change", [
    {"budget_retry": False},                         # a test reading one attempt (no production site, S271 W0)
    {"stage": "adhoc_conn_test"},                    # a probe: a cut is its expected answer
])
def test_opt_outs_make_one_call(rows, change):
    resp, sent = _run(_req(max_tokens=4096, **change), LUNA, [_reply("a", "max_tokens"), _reply("b")])
    assert len(sent) == 1 and resp.content == "a" and rows == []


def test_a3_atomize_cut_is_retried_by_the_gateway_at_its_old_ceiling(rows):
    """AA-1053: a3_atomize no longer opts out; its first ceiling stays unfloored (NO_FLOOR_STAGES), so the gateway's
    retry asks 8192, the ceiling a3's own re-ask used before."""
    resp, sent = _run(_req(max_tokens=4096, stage="a3_atomize"), LUNA, [_reply("a", "max_tokens"), _reply("b")])
    assert sent == [4096, 8192] and resp.content == "b" and resp.truncation_retry_max_tokens == 8192


def test_the_retry_switch(rows, monkeypatch):
    monkeypatch.setenv(token_floor.RETRY_SWITCH_ENV, "off")
    resp, sent = _run(_req(max_tokens=4096), LUNA, [_reply("a", "max_tokens"), _reply("b")])
    assert sent == [4096] and resp.content == "a"


def test_a_shadow_call_is_not_retried():
    assert not token_floor.retry_allowed(_req(), {"shadow": True})
    assert token_floor.retry_allowed(_req(), {})


def test_the_probe_stage_gets_no_floor():
    assert token_floor.floored_max_tokens(_req(max_tokens=32, stage="adhoc_conn_test"), LUNA) == 32


# ── 3b. the retry at the route level (a refused cut reply before the route's next model) ───────────────────────────

def _resp(content="ok", stop="end_turn"):
    return LLMResponse(content=content, model_used="satellite-gpt-6-luna", provider="bedrock-satellite",
                       cost_usd=0.001, input_tokens=10, output_tokens=5, stop_reason=stop)


def _route_client(side_effect):
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        c = LLMClient()
    sizes: list = []

    def fake(request, key, *_):
        sizes.append((key, request.max_tokens))
        return side_effect(key, len(sizes))
    return c, fake, sizes


def test_a_refused_cut_reply_is_retried_on_the_same_model_before_the_next_one(rows):
    """The R2 judge case: OpenAI refuses to return a reply cut at its ceiling (400). Before S271 the route moved on to
    a weaker model at once; now the same model is asked once more with double the ceiling."""
    def answer(key, n):
        if n == 1:
            raise RuntimeError("Error code: 400 - max_tokens or model output limit was reached")
        return _resp("verdict")
    c, fake, sizes = _route_client(answer)
    with patch("shared.llm_client.client.get_model_sync", return_value=LUNA), patch.object(c, "_call_key", fake):
        resp = c._call_route(_req(max_tokens=1200, model_tier=None), ["gpt-6-luna", "nova-lite"], "acc3",
                             role="judge")
    assert sizes == [("gpt-6-luna", 1200), ("gpt-6-luna", 4096 * 2)]   # the retry doubles the floored ceiling
    assert resp.content == "verdict" and resp.fallback_used is False and resp.truncation_retry_max_tokens == 8192
    assert resp.truncation_retry_reason == "truncated_refusal" and resp.truncation_cut_cost_usd == 0.0   # S271 W0


def test_a_billed_cut_failure_twice_moves_on_to_the_next_model(rows):
    def answer(key, n):
        if key == "gpt-6-luna":
            raise _billed_unusable("returned empty content", [{
                "model": "satellite-gpt-6-luna", "account": "acc3", "tokens_in": 10, "tokens_out": 4096,
                "cost_usd": 0.01, "stop_reason": "max_tokens", "provider_requests": 1}])
        return _resp("from nova")
    c, fake, sizes = _route_client(answer)
    with patch("shared.llm_client.client.get_model_sync", return_value=LUNA), patch.object(c, "_call_key", fake):
        resp = c._call_route(_req(max_tokens=4096, model_tier=None), ["gpt-6-luna", "nova-lite"], "acc3",
                             role="judge")
    call_log.wait_for_pending_writes()
    assert [k for k, _ in sizes] == ["gpt-6-luna", "gpt-6-luna", "nova-lite"]     # one retry, then the route
    assert resp.content == "from nova" and resp.fallback_used is True
    assert len(rows) == 2 and all(r["quality_signal"]["billed_unusable"] for r in rows)   # one per billed attempt


def test_a_billed_cut_then_an_unbilled_retry_failure_writes_one_row_each(rows):
    """Review S271: the retry is sent outside the cut attempt's except block, so its error never carries the cut
    error as its implicit context. One billed_unusable row for the cut reply, one 0 USD row for the failed retry
    (before the fix: two billed rows of the same cut attempt and no retry row)."""
    retry_errors: list = []

    def answer(key, n):
        if key == "gpt-6-luna" and n == 1:
            raise _billed_unusable("returned empty content", [{
                "model": "satellite-gpt-6-luna", "account": "acc3", "tokens_in": 10, "tokens_out": 4096,
                "cost_usd": 0.01, "stop_reason": "max_tokens", "provider_requests": 1}])
        if key == "gpt-6-luna":
            retry_errors.append(RuntimeError("ThrottlingException: slow down"))
            raise retry_errors[-1]
        return _resp("from nova")
    c, fake, sizes = _route_client(answer)
    with patch("shared.llm_client.client.get_model_sync", return_value=LUNA), patch.object(c, "_call_key", fake):
        resp = c._call_route(_req(max_tokens=4096, model_tier=None), ["gpt-6-luna", "nova-lite"], "acc3",
                             role="judge")
    call_log.wait_for_pending_writes()
    assert [k for k, _ in sizes] == ["gpt-6-luna", "gpt-6-luna", "nova-lite"] and resp.content == "from nova"
    (err,) = retry_errors
    assert err.__context__ is None and err.__cause__ is None
    billed = [r for r in rows if r["quality_signal"].get("billed_unusable")]
    failed = [r for r in rows if not r["quality_signal"].get("billed_unusable")]
    assert len(billed) == 1 and billed[0]["cost_usd"] == 0.01
    assert "empty content" in billed[0]["quality_signal"]["error"]
    assert len(failed) == 1 and failed[0]["status"] == "refused" and float(failed[0]["cost_usd"] or 0) == 0
    assert "ThrottlingException" in failed[0]["quality_signal"]["error"]


def test_a_refused_cut_then_a_5xx_on_the_retry_counts_as_a_deployment_failure(rows):
    """Review S271: an OpenAI 400 cut refusal (class truncated) followed by a 5xx on the retry. The 5xx is classified
    on its own (not truncated through the context): the breaker counts it and its row is an error, not truncated."""
    from shared.llm_client import breaker
    from shared.llm_client.client import attempt_outcome
    retry_errors: list = []

    def answer(key, n):
        if key == "gpt-6-luna" and n == 1:
            raise RuntimeError("Error code: 400 - max_tokens or model output limit was reached")
        if key == "gpt-6-luna":
            retry_errors.append(RuntimeError("Error code: 503 - service unavailable"))
            raise retry_errors[-1]
        return _resp("from nova")
    c, fake, sizes = _route_client(answer)
    with patch("shared.llm_client.client.get_model_sync", return_value=LUNA), patch.object(c, "_call_key", fake):
        resp = c._call_route(_req(max_tokens=1200, model_tier=None), ["gpt-6-luna", "nova-lite"], "acc3",
                             role="judge")
    call_log.wait_for_pending_writes()
    assert [k for k, _ in sizes] == ["gpt-6-luna", "gpt-6-luna", "nova-lite"] and resp.content == "from nova"
    (err,) = retry_errors
    assert err.__context__ is None
    assert breaker.failure_class(err) != breaker.REQUEST
    assert attempt_outcome(err) == ("error", "other")
    assert sorted(r["status"] for r in rows) == ["error", "truncated"]      # background writes: any order


def test_any_other_failure_is_not_retried(rows):
    def answer(key, n):
        if key == "gpt-6-luna":
            raise RuntimeError("ThrottlingException: slow down")
        return _resp("from nova")
    c, fake, sizes = _route_client(answer)
    with patch("shared.llm_client.client.get_model_sync", return_value=LUNA), patch.object(c, "_call_key", fake):
        c._call_route(_req(model_tier=None), ["gpt-6-luna", "nova-lite"], "acc3", role="judge")
    assert [k for k, _ in sizes] == ["gpt-6-luna", "nova-lite"]


def test_the_caller_side_helper_makes_one_call_and_reads_the_gateways_retry():
    """S271 W0 (AA-900 l13): token_budget no longer retries nor opts out; the gateway's retry facts are on the reply."""
    seen: list = []

    def generate(request):
        seen.append((request.max_tokens, request.budget_retry))
        r = _resp("whole")
        r.truncation_retry_max_tokens, r.truncation_retry_reason, r.truncation_cut_cost_usd = 8192, "cut_reply", 0.002
        return r
    reply = tb.generate_with_budget_retry(generate, LLMRequest(system_prompt="s", user_prompt="u", max_tokens=4096))
    assert seen == [(4096, True)] and reply.resp.content == "whole" and reply.retried and not reply.still_cut
    assert reply.signal() == {"truncation_retry": True, "max_tokens": 8192, "retry_reason": "cut_reply"}
    assert reply.cost_usd == pytest.approx(0.003)


def test_the_central_budget_estimate_uses_the_floored_ceiling():
    seen: list = []
    with patch("shared.llm_client.client.get_model_sync", return_value=LUNA), \
            patch("shared.llm_client.client.calc_cost", side_effect=lambda k, i, o: seen.append(o) or 0.0):
        estimate_call_usd(_req(max_tokens=1200), "gpt-6-luna")
    assert seen == [4096]


def test_the_retry_key_is_a_billed_field_every_row_shape_takes():
    assert "truncation_retry_max_tokens" in call_log.BILLED_FIELD_KEYS
    assert "truncation_retry_max_tokens" in call_log._ROW_FIELDS
    row = call_log._fold_route({"stage": "s", "quality_signal": {"a": 1}, "truncation_retry_max_tokens": 9000})
    assert row == {"stage": "s", "quality_signal": {"a": 1, "truncation_retry": True, "max_tokens": 9000}}
    assert call_log.retry_signal(None) == {} and call_log.retry_signal(True) == {}
    assert call_log.retry_signal(9000, "billed_unusable_cut") == {"truncation_retry": True, "max_tokens": 9000,
                                                                  "retry_reason": "billed_unusable_cut"}
    from services.content_generation.brand_fit import BrandFitResult
    assert "truncation_retry_max_tokens" in BrandFitResult.__dataclass_fields__


# ── 4. the CI guard ─────────────────────────────────────────────────────────────────────────────────────────────────
#
# A ceiling below the floor on a reasoning model only cuts (the gateway lifts it at run time, but the constant then
# lies about what the stage gets). A stage's route is data (the admin can move any stage to a reasoning model), so
# the guard does not try to read routes: every literal ceiling below the floor in the code must be on this list with
# its reason. The list only shrinks; a stale entry fails too.

SUB_FLOOR_CEILINGS = {
    "api/services/llm_connections.py:TEST_MAX_TOKENS": "connection probe through LLMClient.probe (adhoc_conn_test)",
    "api/services/llm_route_trace.py:TEST_MAX_TOKENS": "admin route test: 'reply ok'; the floor lifts it on a "
                                                       "reasoning model, the literal bounds a non-reasoning one",
    "services/jobs/llm_probe_job.py:CANDIDATE_MAX_TOKENS": "availability sweep through LLMClient.probe",
    "services/acp_content_writing/seed_align/checks.py:MAX_TOKENS": "seed alignment one-word check",
    "services/acp_shared/place_cards.py:DRAFT_MAX_TOKENS": "place card draft",
    "services/acp_shared/place_cards.py:CHECK_MAX_TOKENS": "place card check verdict",
    "services/content_generation/s1_writer_v2.py:META_MAX_TOKENS": "S1 meta fields",
    "services/jobs/llm_contract_smoke_job.py:364": "contract smoke, short draft",
    "services/places/days.py:MAX_TOKENS": "place day split",
    "services/places/dedupe.py:MAX_TOKENS": "place dedupe yes/no",
    "services/places/locate.py:MAX_TOKENS": "place locate",
    "services/supply_content/writer.py:179": "supplier content writer",
    "shared/llm_client/column_mapper.py:81": "spreadsheet column mapping (JSON object)",
    "shared/llm_client/judge.py:invoke_judge": "T10 judge default (callers pass their own)",
}
SCAN_DIRS = ("api", "services", "shared", "worker", "scripts")


def _int_of(node, consts):
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _sources():
    for d in SCAN_DIRS:
        for p in sorted((ROOT / d).rglob("*.py")):
            if "tests" in p.parts or "node_modules" in p.parts:
                continue
            yield p.relative_to(ROOT).as_posix(), ast.parse(p.read_text(encoding="utf-8"))


def _sub_floor_sites() -> set:
    """Every literal ceiling below the floor: a module constant named *MAX_TOKENS, a max_tokens= keyword (a literal or
    a module constant) and a max_tokens parameter default."""
    found = set()
    for rel, tree in _sources():
        consts: dict = {}
        for n in tree.body:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                v = _int_of(n.value, consts)
                if v is not None:
                    consts[n.targets[0].id] = v
                    if n.targets[0].id.endswith("MAX_TOKENS") and v < tokens.REASONING_FLOOR:
                        found.add(f"{rel}:{n.targets[0].id}")
        for n in ast.walk(tree):
            if isinstance(n, ast.keyword) and n.arg == "max_tokens":
                v = _int_of(n.value, consts)
                if v is not None and v < tokens.REASONING_FLOOR:
                    name = n.value.id if isinstance(n.value, ast.Name) else str(n.value.lineno)
                    found.add(f"{rel}:{name}")
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = n.args
                pos = a.posonlyargs + a.args
                pairs = list(zip(pos[len(pos) - len(a.defaults):], a.defaults))
                pairs += [(k, d) for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
                for arg, default in pairs:
                    v = _int_of(default, consts) if arg.arg == "max_tokens" else None
                    if v is not None and v < tokens.REASONING_FLOOR:
                        found.add(f"{rel}:{n.name}")
    return found


def test_no_new_literal_ceiling_below_the_floor():
    found = _sub_floor_sites()
    new = sorted(found - set(SUB_FLOOR_CEILINGS))
    assert not new, ("a literal max_tokens below the reasoning floor "
                     f"({tokens.REASONING_FLOOR}): raise it (a ceiling is head-room, a complete reply costs the same) "
                     f"or add it to SUB_FLOOR_CEILINGS with the reason: {new}")
    stale = sorted(set(SUB_FLOOR_CEILINGS) - found)
    assert not stale, f"no longer below the floor: remove from SUB_FLOOR_CEILINGS: {stale}"


def test_the_named_stages_are_at_the_floor_now():
    from services.writing_memory import drafter, eval_judge, preference
    assert eval_judge.MAX_TOKENS >= tokens.REASONING_FLOOR
    assert drafter.MAX_TOKENS >= tokens.REASONING_FLOOR and preference.MAX_TOKENS >= tokens.REASONING_FLOOR
    assert tb.T9_REPAIR_MAX_TOKENS >= 8192 and tb.T9_FAQ_FILTER_MAX_TOKENS >= 6000


# Every file that may send budget_retry=False. S271 W0 (AA-900 l13): none. The gateway is the only owner of the
# truncation retry; token_budget.py (T8 / T9 write / repair / FAQ filter) dropped its own retry and opt-out, because
# it never saw an empty cut and the route swapped the model silently.
OWN_RETRY_FILES: dict = {}


def test_only_the_listed_call_sites_bypass_the_gateway_retry():
    found = set()
    for rel, tree in _sources():
        for n in ast.walk(tree):
            if isinstance(n, ast.keyword) and n.arg == "budget_retry":
                found.add(rel)
            if isinstance(n, ast.Dict):
                for k in n.keys:
                    if isinstance(k, ast.Constant) and k.value == "budget_retry":
                        found.add(rel)
    found.discard("shared/llm_client/token_floor.py")
    outside = sorted(found - set(OWN_RETRY_FILES))
    assert not outside, f"budget_retry set outside the allowlist: {outside}"


# Every model key the migrations name must be classified: reasoning by the code rule, or known not to reason. A new
# family outside both fails here: add it to the rule (shared/aa_llm/tokens.py), set capabilities.reasoning on its
# catalog row, or list it below.
NOT_REASONING = re.compile(r"^(?:sonnet|haiku|opus|claude-(?:sonnet|haiku|opus))(?:-4(?:[.-]\d+)*)?$"
                           r"|^gpt-4(?:\.\d+)?$|^nova-|^llama|^titan-|^cohere|^jev-|^claude$")
_MODEL_TOKEN = re.compile(r"'((?:gpt|o[1-9]|sonnet|haiku|opus|fable|claude|nova|llama|mistral|gemini|deepseek|qwen|"
                          r"grok|titan|cohere)[a-z0-9.\-]*)'")


def _migration_model_keys() -> set:
    keys = set()
    for p in sorted((ROOT / "api" / "migrations").glob("*.sql")):
        text = re.sub(r"--[^\n]*", "", p.read_text(encoding="utf-8"))
        keys.update(k for k in _MODEL_TOKEN.findall(text) if not re.search(r"[.:]v\d|anthropic\.|openai\.", k))
    return {k for k in keys if "." not in k or k.startswith("gpt-")}


def test_every_model_key_the_migrations_name_is_classified():
    keys = _migration_model_keys()
    assert {"sonnet-5-5", "gpt-6-luna-openai", "nova-lite"} <= keys          # the scan sees the catalog keys
    unknown = sorted(k for k in keys if not tokens.reasoning_capable(k) and not NOT_REASONING.search(k))
    assert not unknown, f"model key(s) with no reasoning classification: {unknown}"


# ── 5. the cut-rate alert ───────────────────────────────────────────────────────────────────────────────────────────

def test_a_stage_with_more_than_5_pct_cut_replies_alerts():
    (f,) = route_watch.cut_findings({"t9_repair": {"replies": 20, "cut": 3, "cut_cost_usd": 0.12}}, {})
    assert f.check == "route_cut.t9_repair" and f.details["cut_pct"] == 15.0 and f.details["cut"] == 3
    assert route_watch.cut_findings({"t9_repair": {"replies": 20, "cut": 1}}, {}) == []      # 5 %: not above
    assert route_watch.cut_findings({"t9_repair": {"replies": 3, "cut": 3}}, {}) == []       # under min_requests
    assert route_watch.cut_findings({"t9_repair": {"replies": 3, "cut": 3}}, {"*": {"min_requests": 2}}) != []


def test_refused_cuts_and_eval_rows_are_named_in_the_alert():
    """Review S271: the R2 judge case (eval rows, the provider refusing every cut reply) alerts and says so."""
    (f,) = route_watch.cut_findings({"zz_judge": {"replies": 33, "cut": 33, "refused_cut": 33, "eval_rows": 33,
                                                  "cut_cost_usd": 0.0}}, {})
    assert f.details["cut_pct"] == 100.0 and f.details["refused_cut"] == 33 and f.details["eval_rows"] == 33
    assert "33 refused by the provider" in f.summary and "from eval runs" in f.summary


def test_the_cut_check_resolves_and_fits_the_notification_column():
    chk = ops_checks.check_for(ops_checks.ROUTE_CUT_PREFIX + "s" * 64)
    assert chk.page == ops_checks.TRACE_PAGE and chk.bell_every.total_seconds() == 24 * 3600
    assert len(ops_checks.EVENT_TYPE_PREFIX + ops_checks.ROUTE_CUT_PREFIX + "s" * 64) <= 100


def test_the_cut_stats_query_leaves_the_probes_out_and_reads_the_cut_reasons():
    seen: list = []

    class Db:
        async def fetch(self, sql, *args):
            seen.append((sql, args))
            return [{"stage": "t9_repair", "replies": 10, "cut": 2, "cut_cost_usd": 0.1}]
    out = asyncio.run(route_watch.read_cut_stats(Db(), route_watch.datetime.now(route_watch.timezone.utc)))
    sql, args = seen[0]
    assert "shared.llm_call_log" in sql and "billed_unusable" in sql
    assert "max_tokens" in args[1] and "length" in args[1] and "adhoc_conn_test" in args[2]
    assert out["t9_repair"]["cut"] == 2
    # review S271: eval rows are kept (under their real stage) and a refused cut (nothing billed) is counted
    assert "is_experiment, false) = false" not in sql and "real_stage" in sql and "OR status = 'truncated')" in sql
