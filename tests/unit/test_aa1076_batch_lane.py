"""AA-1076 — the gateway batch lane (shared/llm_client/batch_lane.py) and its two provider adapters.

- target: the stage route's first direct-API deployment (Anthropic or OpenAI), never a Bedrock one; none = refused;
- guards before submit: lane switch, tenant stages, a switched-off stage, the cost cap (the gateway's estimate at the
  batch rate), custom ids;
- request bodies: the on-demand adapters' own builders (Anthropic build_params, client.openai_chat_body);
- pricing + ledger: one row per request, pricing_mode '+batch' at half the on-demand cost, **billed_fields, 0 USD rows for
  errored / expired / canceled / missing items;
- the hand-off: a reply is used only for the exact prompt; a prefetched reply is never ledgered twice;
- the Anthropic adapter end to end on the real SDK over a fake transport, the OpenAI adapter on a fake client.
No AWS, no vendor, no DB, $0.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx2
import pytest

from shared.llm_client import batch_lane, call_log, pricing
from shared.llm_client.adapters import anthropic_batches, anthropic_messages, openai_batches
from shared.llm_client.batch_lane import BatchCapExceeded, BatchItem, BatchTarget, BatchUnavailable, ItemResult
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.deployments import ModelDeployment
from shared.llm_client.vendor_route import VendorDeployment
from tests.unit.aa1002_fake_anthropic import FakeAnthropic, anthropic_connection, install

SONNET = CatalogModel(model_key="sonnet-5-5", label="Sonnet 5.5", vendor="anthropic", provider="bedrock",
                      api_style="converse", bedrock_profile_ids={"acc3": "global.anthropic.claude-sonnet-5-5"},
                      callable_via=("llm_client",), supports_temperature=False, max_output_tokens=128000,
                      price_in_per_mtok=2.0, price_out_per_mtok=10.0, price_cache_read_per_mtok=0.2,
                      price_cache_write_per_mtok=2.5, enabled=True)
LUNA = CatalogModel(model_key="gpt-6-luna-openai", label="Luna", vendor="openai", provider="openai",
                    api_style="openai_chat", bedrock_profile_ids={}, callable_via=("llm_client",),
                    supports_temperature=False, price_in_per_mtok=0.1, price_out_per_mtok=0.5, enabled=True)
RATES = {"sonnet-5-5": {"in": 0.002, "out": 0.010, "cache_read": 0.0002, "cache_write": 0.0025},
         "gpt-6-luna-openai": {"in": 0.0001, "out": 0.0005, "cache_read": 0.00001, "cache_write": 0.000125}}


def _conn(key, kind, scope="vendor_direct"):
    if kind == "anthropic":
        return anthropic_connection()
    return SimpleNamespace(connection_key=key, kind=kind, data_scope=scope, enabled=True, served_roles=None)


def _dep(model_key, conn_key, wire):
    return ModelDeployment(model_key=model_key, connection_key=conn_key, wire_id=wire, priority=10, enabled=True,
                           origin="seed")


def _cfg(stage="s1_generate", model="sonnet-5-5", fallbacks=(), role="writer", enabled=True):
    return SimpleNamespace(stage=stage, model_id=model, fallback_model_ids=tuple(fallbacks), role=role,
                           allowed_scopes=None, pii_class="content", enabled=enabled, disabled_reason=None)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(batch_lane.SWITCH_ENV, raising=False)
    monkeypatch.setattr("shared.llm_client.pricing._catalog_rates",
                        lambda m: RATES.get(pricing._catalog_key(m)))
    monkeypatch.setattr("shared.llm_client.pricing.deployment_for", lambda *a, **k: None)
    monkeypatch.setattr(batch_lane, "get_model_sync", {"sonnet-5-5": SONNET, "gpt-6-luna-openai": LUNA}.get)
    monkeypatch.setattr("shared.llm_client.client.get_model_sync", {"sonnet-5-5": SONNET,
                                                                     "gpt-6-luna-openai": LUNA}.get)
    monkeypatch.setattr(batch_lane, "get_stage_config_sync", lambda stage: _cfg(stage=stage))
    monkeypatch.setattr(batch_lane.app_profile, "refusal", lambda stage, app=None: None)
    monkeypatch.setattr(batch_lane.central_budget, "active", lambda: False)
    monkeypatch.setattr("shared.llm_client.stage_params.params_for", lambda *a, **k: None)
    monkeypatch.setattr("shared.llm_client.effort_rule.stage_effort", lambda *a, **k: None)
    yield


ANTH = BatchTarget(stage="s1_generate", role="writer", model_key="sonnet-5-5", connection_key="anthropic-direct",
                   provider="anthropic", wire_id="claude-sonnet-5-5")
OAI = BatchTarget(stage="a3_atomize", role="validate", model_key="gpt-6-luna-openai", connection_key="openai-direct",
                  provider="openai", wire_id="gpt-6-luna")


def _item(cid="s1-aaa", system="SYS invented", user="USER Day 1 Lake Verano", max_tokens=1000, tour="t-1"):
    return BatchItem(custom_id=cid, system=system, user=user, max_tokens=max_tokens, tour_id=tour)


# ── target ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_target_is_the_first_direct_api_deployment_of_the_route(monkeypatch):
    plan = [VendorDeployment(_dep("sonnet-5-5", "acc3", "global.x"), _conn("acc3", "bedrock", "aws_only")),
            VendorDeployment(_dep("sonnet-5-5", "anthropic-direct", "claude-sonnet-5-5"), _conn("a", "anthropic"))]
    monkeypatch.setattr(batch_lane, "deployment_plan", lambda m, s, role=None, pii_class=None: plan)
    t = batch_lane.resolve_target("s1_generate")
    assert (t.provider, t.connection_key, t.wire_id, t.model_key, t.role) == (
        "anthropic", "anthropic-direct", "claude-sonnet-5-5", "sonnet-5-5", "writer")
    assert t.label == "anthropic-api-sonnet-5-5"


def test_a_route_with_only_aws_deployments_cannot_batch(monkeypatch):
    plan = [VendorDeployment(_dep("sonnet-5-5", "acc3", "global.x"), _conn("acc3", "bedrock", "aws_only"))]
    monkeypatch.setattr(batch_lane, "deployment_plan", lambda m, s, role=None, pii_class=None: plan)
    with pytest.raises(BatchUnavailable, match="cannot batch"):
        batch_lane.resolve_target("s1_generate")


def test_the_route_falls_through_to_a_later_model_with_an_openai_deployment(monkeypatch):
    monkeypatch.setattr(batch_lane, "get_stage_config_sync",
                        lambda stage: _cfg(stage=stage, model="sonnet-5-5", fallbacks=("gpt-6-luna-openai",)))
    plans = {"sonnet-5-5": [], "gpt-6-luna-openai": [
        VendorDeployment(_dep("gpt-6-luna-openai", "openai-direct", "gpt-6-luna"), _conn("openai-direct", "openai"))]}
    monkeypatch.setattr(batch_lane, "deployment_plan", lambda m, s, role=None, pii_class=None: plans[m.model_key])
    t = batch_lane.resolve_target("s1_generate")
    assert (t.provider, t.model_key, t.label) == ("openai", "gpt-6-luna-openai", "gpt-6-luna-openai")


# ── guards ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_tenant_stages_stay_synchronous():
    t2 = BatchTarget(**{**ANTH.as_dict(), "stage": "t2_generate"})
    with pytest.raises(BatchUnavailable, match="tenant"):
        batch_lane.guard(t2, [_item()], 1.0)


def test_a_switched_off_stage_and_the_lane_switch_refuse(monkeypatch):
    monkeypatch.setattr(batch_lane, "get_stage_config_sync", lambda stage: _cfg(stage=stage, enabled=False))
    with pytest.raises(BatchUnavailable, match="switched off"):
        batch_lane.guard(ANTH, [_item()], 1.0)
    monkeypatch.setenv(batch_lane.SWITCH_ENV, "off")
    with pytest.raises(BatchUnavailable, match="LLM_BATCH_LANE"):
        batch_lane.guard(ANTH, [_item()], 1.0)


def test_the_cap_is_the_gateway_estimate_at_the_batch_rate(monkeypatch):
    from shared.llm_client import client as client_mod
    items = [_item(f"s1-{i}") for i in range(3)]
    each = client_mod.estimate_call_usd(batch_lane.build_request(ANTH, items[0])[0], "sonnet-5-5")
    assert each > 0
    est = batch_lane.estimate_usd(ANTH, items)
    assert est == pytest.approx(3 * each * 0.5, rel=1e-6)
    assert batch_lane.guard(ANTH, items, est + 0.0001) == est
    with pytest.raises(BatchCapExceeded, match="above its cap"):
        batch_lane.guard(ANTH, items, est / 2)


@pytest.mark.parametrize("cap", [0, -1, 101, None, "x"])
def test_a_batch_needs_a_cap_within_bounds(cap):
    with pytest.raises(BatchCapExceeded):
        batch_lane.guard(ANTH, [_item()], cap)


def test_custom_ids_are_checked():
    with pytest.raises(BatchUnavailable, match="custom ids must match"):
        batch_lane.guard(ANTH, [_item("bad id with spaces")], 1.0)
    with pytest.raises(BatchUnavailable, match="unique"):
        batch_lane.guard(ANTH, [_item("a"), _item("a")], 1.0)


def test_a_central_budget_refusal_stops_the_batch(monkeypatch):
    monkeypatch.setattr(batch_lane.central_budget, "active", lambda: True)

    def refuse(stage, est):
        raise batch_lane.central_budget.CentralBudgetExceeded("llm/stage:s1_generate daily cap")
    monkeypatch.setattr(batch_lane.central_budget, "check", refuse)
    with pytest.raises(BatchCapExceeded, match="central LLM budget"):
        batch_lane.guard(ANTH, [_item()], 1.0)


# ── request bodies ───────────────────────────────────────────────────────────────────────────────────────────────

def test_the_anthropic_body_is_the_on_demand_adapters_build_params():
    body = batch_lane.provider_body(ANTH, _item())
    # the gateway's request shaping first: the reasoning model's token floor raises a low ceiling, as on demand
    shaped = batch_lane.build_request(ANTH, _item())[0]
    assert shaped.max_tokens >= 1000
    assert body == anthropic_messages.build_params(wire_id="claude-sonnet-5-5", system="SYS invented",
                                                   user="USER Day 1 Lake Verano", max_tokens=shaped.max_tokens,
                                                   stage="s1_generate", json_schema=None, request_defaults=None)
    cached = batch_lane.provider_body(ANTH, _item(), cache_system=True)
    assert cached["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_the_openai_body_is_the_on_demand_chat_body():
    from shared.llm_client.client import openai_chat_body
    from shared.llm_client.models import LLMRequest
    body = batch_lane.provider_body(OAI, _item(max_tokens=4096))
    want, _ = openai_chat_body(LLMRequest(system_prompt="SYS invented", user_prompt="USER Day 1 Lake Verano",
                                          stage="a3_atomize", max_tokens=4096), "gpt-6-luna", LUNA)
    assert body == want and body["max_completion_tokens"] == 4096 and body["model"] == "gpt-6-luna"


# ── pricing + ledger ─────────────────────────────────────────────────────────────────────────────────────────────

def _ok(cid="s1-aaa", **kw):
    base = dict(custom_id=cid, outcome="succeeded", text='{"name": "x"}', stop_reason="end_turn",
                input_tokens=1000, output_tokens=500, provider_model="claude-sonnet-5-5", request_id="msg_1")
    base.update(kw)
    return ItemResult(**base)


def test_a_batch_request_costs_half_of_the_same_request_on_demand():
    r = _ok(cache_read_tokens=200)
    q = batch_lane.price(ANTH, r)
    on_demand = pricing.quote("sonnet-5-5", pricing.Usage(input_tokens=1000, output_tokens=500, cache_read_tokens=200))
    assert q.cost_usd == pytest.approx(on_demand.cost_usd / 2)
    assert q.pricing_mode.endswith("+batch")


def test_one_ledger_row_per_request_with_billed_fields_and_failures_at_zero():
    items = [{"custom_id": c, "tour_id": f"t-{c}", "prompt_sha": "x"} for c in ("a", "b", "c", "d", "e", "f")]
    results = {"a": _ok("a"), "b": _ok("b", stop_reason="max_tokens"), "c": _ok("c", stop_reason="refusal"),
               "d": ItemResult(custom_id="d", outcome="expired", error="expired"),
               "e": ItemResult(custom_id="e", outcome="errored", error_type="rate_limit_error", error="busy")}
    rows = batch_lane.ledger_rows(ANTH, results, items, batch_id="B1", provider_batch_id="msgbatch_1",
                                  consumer="s1_attempt1")
    assert len(rows) == 6
    by = {r["quality_signal"]["custom_id"]: r for r in rows}
    assert by["a"]["status"] == "ok" and by["a"]["pricing_mode"].endswith("+batch")
    assert by["a"]["connection_key"] == "anthropic-direct" and by["a"]["model"] == "anthropic-api-sonnet-5-5"
    assert by["a"]["tokens_in"] == 1000 and by["a"]["cost_usd"] > 0 and by["a"]["deployment_wire_id"]
    assert by["b"]["status"] == "truncated"
    assert by["c"]["status"] == "billed_unusable" and by["c"]["quality_signal"]["billed_unusable"]
    assert by["d"]["cost_usd"] == 0 and by["d"]["error_class"] == "deadline" and by["d"]["status"] == "error"
    assert by["e"]["error_class"] == "throttled"
    assert by["f"]["quality_signal"]["batch_outcome"] == "missing" and by["f"]["cost_usd"] == 0
    for r in rows:
        assert r["quality_signal"]["batch_id"] == "B1" and r["quality_signal"]["provider_batch_id"] == "msgbatch_1"
        assert r["unit_kind"] == batch_lane.UNIT_KIND and r["units"] == 1 and r["stage"] == "s1_generate"
        assert set(r) <= set(call_log._ROW_FIELDS)        # record_calls accepts every row


def test_an_adhoc_batch_logs_under_its_own_stage():
    t = BatchTarget(**{**ANTH.as_dict(), "ledger_stage": "adhoc_aa1076"})
    rows = batch_lane.ledger_rows(t, {"a": _ok("a")}, [{"custom_id": "a"}], batch_id="B", provider_batch_id="p",
                                  consumer="adhoc")
    assert rows[0]["stage"] == "adhoc_aa1076"
    assert BatchTarget.from_dict(t.as_dict()).log_stage == "adhoc_aa1076"


def test_the_ledger_row_writer_passes_billed_fields():
    import inspect
    src = inspect.getsource(batch_lane.ledger_rows)
    assert "**call_log.billed_fields(resp)" in src


# ── hand-off ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_reply_is_used_only_for_the_exact_prompt_and_never_ledgered_twice():
    sha = batch_lane.prompt_sha("s1_generate", "SYS", "USER")
    rec = batch_lane.reply_record(ANTH, _ok(), sha, batch_id="B1")
    assert json.loads(json.dumps(rec)) == rec                     # it travels in a job payload
    assert batch_lane.prefetched("s1_generate", "SYS", "USER") is None    # nothing bound
    with batch_lane.bind_replies([rec]):
        got = batch_lane.prefetched("s1_generate", "SYS", "USER")
        assert got is not None and got.content == '{"name": "x"}' and batch_lane.already_ledgered(got)
        assert got.pricing_mode.endswith("+batch")
        assert batch_lane.prefetched("s1_generate", "SYS", "USER changed") is None      # stale prompt: live
        assert batch_lane.prefetched("a3_atomize", "SYS", "USER") is None               # another stage: live
    assert batch_lane.prefetched("s1_generate", "SYS", "USER") is None
    assert not batch_lane.already_ledgered(SimpleNamespace(provider_requests=1))


def test_usable_means_succeeded_with_text_and_not_cut_or_refused():
    assert _ok().usable
    assert not _ok(stop_reason="max_tokens").usable and not _ok(stop_reason="refusal").usable
    assert not _ok(text="").usable
    assert not ItemResult(custom_id="x", outcome="expired").usable


def test_results_fill_items_the_provider_did_not_name(monkeypatch):
    fake = SimpleNamespace(results=lambda conn, pbid: [anthropic_batches.ItemOutcome(custom_id="a",
                                                                                     outcome="succeeded",
                                                                                     text="t", input_tokens=1)])
    monkeypatch.setattr(batch_lane, "_adapter", lambda t: fake)
    monkeypatch.setattr(batch_lane, "_connection", lambda t: None)
    out = batch_lane.results(ANTH, "p", ["a", "b"])
    assert out["a"].outcome == "succeeded" and out["b"].outcome == "missing"
    out = batch_lane.results(OAI, "p", ["a", "b"], batch_status="expired")
    assert out["b"].outcome == "expired"


# ── Anthropic adapter on the real SDK (fake transport) ───────────────────────────────────────────────────────────

BATCH_JSON = {"id": "msgbatch_test", "type": "message_batch", "processing_status": "ended",
              "request_counts": {"processing": 0, "succeeded": 1, "errored": 1, "canceled": 0, "expired": 1},
              "created_at": "2026-11-11T00:00:00Z", "ended_at": "2026-11-11T00:10:00Z",
              "expires_at": "2026-11-12T00:00:00Z", "cancel_initiated_at": None, "archived_at": None,
              "results_url": "https://api.anthropic.com/v1/messages/batches/msgbatch_test/results"}

RESULT_LINES = [
    {"custom_id": "s1-a", "result": {"type": "succeeded", "message": {
        "id": "msg_a", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": "{\"name\": \"Lake Verano trail\"}"}], "stop_reason": "end_turn",
        "stop_sequence": None, "usage": {"input_tokens": 900, "output_tokens": 300, "cache_read_input_tokens": 100,
                                         "cache_creation_input_tokens": 0}}}},
    {"custom_id": "s1-b", "result": {"type": "errored", "error": {"type": "error", "error": {
        "type": "invalid_request_error", "message": "bad request"}}}},
    {"custom_id": "s1-c", "result": {"type": "expired"}},
]


def _anthropic_handler(fake: FakeAnthropic):
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content.decode() or "{}") if request.content else {}
        fake.requests.append({"url": str(request.url), "method": request.method, "body": body})
        url = str(request.url)
        if url.endswith("/results"):
            text = "\n".join(json.dumps(x) for x in RESULT_LINES) + "\n"
            return httpx2.Response(200, headers={"content-type": "application/binary"}, content=text.encode())
        if url.endswith("/cancel"):
            return httpx2.Response(200, json={**BATCH_JSON, "processing_status": "canceling"})
        if request.method == "POST":
            return httpx2.Response(200, json={**BATCH_JSON, "processing_status": "in_progress"})
        return httpx2.Response(200, json=BATCH_JSON)
    return handler


def test_anthropic_adapter_create_status_results(monkeypatch):
    import anthropic
    fake = FakeAnthropic()
    install(monkeypatch, fake)
    monkeypatch.setattr(anthropic_messages, "_http_client_factory",
                        lambda: anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(_anthropic_handler(fake))))
    conn = anthropic_connection()
    reqs = [anthropic_batches.BatchRequest("s1-a", {"model": "claude-sonnet-5-5", "max_tokens": 10,
                                                    "messages": [{"role": "user", "content": "hi"}]})]
    assert anthropic_batches.submit(conn, reqs) == "msgbatch_test"
    sent = fake.requests[0]
    assert sent["url"].endswith("/v1/messages/batches") and sent["body"]["requests"][0]["custom_id"] == "s1-a"
    st = anthropic_batches.status(conn, "msgbatch_test")
    assert st.ended and st.counts["expired"] == 1
    out = {o.custom_id: o for o in anthropic_batches.results(conn, "msgbatch_test")}
    assert out["s1-a"].outcome == "succeeded" and out["s1-a"].text.startswith("{") and out["s1-a"].input_tokens == 900
    assert out["s1-a"].cache_read_tokens == 100 and out["s1-a"].provider_model == "claude-sonnet-5-5"
    assert out["s1-b"].outcome == "errored" and out["s1-b"].error_type == "invalid_request_error"
    assert out["s1-c"].outcome == "expired"
    anthropic_batches.cancel(conn, "msgbatch_test")
    assert fake.requests[-1]["url"].endswith("/cancel")


def test_anthropic_adapter_refuses_a_non_anthropic_connection():
    from shared.llm_client import connections
    with pytest.raises(connections.ConnectionCredentialError):
        anthropic_batches.submit(SimpleNamespace(kind="openai", connection_key="x", secret_name="s",
                                                 credential_env=None), [anthropic_batches.BatchRequest("a", {})])


# ── OpenAI adapter on a fake client ──────────────────────────────────────────────────────────────────────────────

def _openai_client(status="completed", output=None, errors=None):
    c = MagicMock()
    c.files.create.return_value = SimpleNamespace(id="file-in")
    c.batches.create.return_value = SimpleNamespace(id="batch_oai")
    c.batches.retrieve.return_value = SimpleNamespace(status=status, output_file_id="file-out" if output else None,
                                                      error_file_id="file-err" if errors else None,
                                                      request_counts=SimpleNamespace(total=2, completed=1, failed=1))
    texts = {"file-out": "\n".join(json.dumps(x) for x in output or []),
             "file-err": "\n".join(json.dumps(x) for x in errors or [])}
    c.files.content.side_effect = lambda fid: SimpleNamespace(text=texts[fid])
    return c


def test_openai_adapter_file_lines_and_results(monkeypatch):
    out_line = {"custom_id": "a3-x-1", "response": {"status_code": 200, "request_id": "req_1", "body": {
        "model": "gpt-6-luna", "choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 800, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 300},
                  "completion_tokens_details": {"reasoning_tokens": 20}}}}, "error": None}
    err_line = {"custom_id": "a3-x-2", "response": {"status_code": 400, "body": {"error": {
        "code": "invalid_request", "message": "bad"}}}, "error": None}
    client = _openai_client(output=[out_line], errors=[err_line])
    monkeypatch.setattr(openai_batches.openai_chat, "client_for", lambda conn: client)
    conn = SimpleNamespace(kind="openai", connection_key="openai-direct")
    assert openai_batches.submit(conn, [("a3-x-1", {"model": "gpt-6-luna"}), ("a3-x-2", {"model": "gpt-6-luna"})],
                                 metadata={"aa_batch_id": "B"}) == "batch_oai"
    sent = client.files.create.call_args.kwargs
    assert sent["purpose"] == "batch"
    lines = [json.loads(x) for x in sent["file"][1].decode().splitlines()]
    assert lines[0] == {"custom_id": "a3-x-1", "method": "POST", "url": "/v1/chat/completions",
                        "body": {"model": "gpt-6-luna"}}
    assert client.batches.create.call_args.kwargs["completion_window"] == "24h"
    st = openai_batches.status(conn, "batch_oai")
    assert st.ended and st.counts["failed"] == 1
    got = {o.custom_id: o for o in openai_batches.results(conn, "batch_oai")}
    assert got["a3-x-1"].outcome == "succeeded" and got["a3-x-1"].input_tokens == 500
    assert got["a3-x-1"].cache_read_tokens == 300 and got["a3-x-1"].extra["reasoning_tokens"] == 20
    assert got["a3-x-2"].outcome == "errored" and got["a3-x-2"].error_type == "invalid_request"


def test_openai_adapter_needs_an_openai_connection_and_a_batch_capable_sdk(monkeypatch):
    from shared.llm_client import connections
    with pytest.raises(connections.ConnectionCredentialError):
        openai_batches.status(SimpleNamespace(kind="azure_openai", connection_key="az"), "b")
    monkeypatch.setattr(openai_batches.openai_chat, "client_for", lambda conn: SimpleNamespace(files=None))
    with pytest.raises(openai_batches.OpenAIBatchError, match="no Batch API"):
        openai_batches.status(SimpleNamespace(kind="openai", connection_key="o"), "b")


def test_openai_input_refuses_duplicate_ids():
    with pytest.raises(ValueError, match="duplicate"):
        openai_batches.input_jsonl([("a", {}), ("a", {})])
