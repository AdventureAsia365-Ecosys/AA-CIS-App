"""AA-900 (S272, root ADR 0020 d8) — the cassette gaps found live in S272 (probe job of 07/11).

1. A billed but unusable reply (BilledUnusableReply raised inside the provider call) is recorded: the reply the
   exception carries (reply_facts), marked stop_reason 'billed_unusable[:<provider stop reason>]', one row per
   attempt, never twice for one exception; replay (get_by_request_sha) never serves it; the recorder still never
   breaks the call (the exception reaches the caller unchanged).
2. Embeddings are not recorded, by design.
3. The link rule: one cassette row per provider attempt that got a reply, joined to its ledger row on
   (route_trace, attempt_no), never on the model name. The probe of acc3's legacy key ``haiku`` and of the catalog key
   ``haiku-4-5`` writes two ledger rows that both say ``haiku-4-5`` and two cassette rows (``haiku`` / ``haiku-4-5``).
No AWS, no vendor, no DB, no paid call. Names and texts are invented.
"""
import contextlib
import json
from unittest.mock import MagicMock

import pytest

from shared.llm_client import call_log, cassette, embed as embed_mod
from shared.llm_client import client as client_mod
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import BilledUnusableReply
from shared.llm_client.models import LLMRequest, LLMResponse
from shared.llm_client.vendor_route import VendorDeployment
from tests.unit.test_aa1002_s3_deployment_routing import World, _anthropic, _dep

HAIKU_WIRE = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
HAIKU45 = CatalogModel(model_key="haiku-4-5", label="Claude Haiku 4.5", vendor="anthropic", provider="bedrock",
                       api_style="converse", bedrock_profile_ids={"acc3": HAIKU_WIRE}, callable_via=("llm_client",),
                       supports_temperature=False, price_in_per_mtok=1.0, price_out_per_mtok=5.0, enabled=True)
SONNET55_API = CatalogModel(model_key="sonnet-5-5", label="Sonnet 5.5", vendor="anthropic", provider="anthropic",
                            api_style="anthropic_messages", bedrock_profile_ids={}, callable_via=("llm_client",),
                            supports_temperature=False, price_in_per_mtok=2.0, price_out_per_mtok=10.0, enabled=True)
STRICT = {"name": "lantern_check", "schema": {"type": "object", "properties": {"lantern": {"type": "string"}},
                                              "required": ["lantern"]}}


@pytest.fixture
def written(monkeypatch):
    seen: list = []

    async def write_row(row, conn=None):
        seen.append(row)
        return True
    monkeypatch.setattr(cassette, "write_row", write_row)
    # no role_config in unit tests: a stage's pii class would read as unread (pii, fail closed) and keep no text
    monkeypatch.setattr(client_mod, "stage_pii_class", lambda request: "content")
    yield seen


@pytest.fixture
def gw(monkeypatch):
    from shared.llm_client import catalog, vendor_route
    hook = vendor_route.circuit_open
    w = World(monkeypatch)
    catalog._models["haiku-4-5"] = HAIKU45
    monkeypatch.setattr(client_mod.LLMClient, "_app_profile_gate", lambda self, request, key: None)
    monkeypatch.setattr(client_mod.breaker, "limit", lambda conn, model_key=None: contextlib.nullcontext())
    yield w
    w.reset()
    catalog.invalidate()
    vendor_route.circuit_open = hook


def _wait():
    call_log.wait_for_pending_writes()


def _req(**kw):
    base = dict(system_prompt="You check the Amber Valley.", user_prompt="Lantern walk in Lumen Bay.",
                max_tokens=400, stage="adhoc_aa900")
    base.update(kw)
    return LLMRequest(**base)


def _client(monkeypatch):
    with monkeypatch.context() as m:
        m.setattr("shared.llm_client.client.boto3", MagicMock())
        return client_mod.LLMClient()


def _billed(model="satellite-sonnet-5-5", stop_reason="end_turn", **extra):
    return {"model": model, "account": "acc3", "tokens_in": 30, "tokens_out": 12, "cost_usd": 0.0002,
            "stop_reason": stop_reason, "provider_requests": 1, **extra}


def _attempt_raising(monkeypatch, exc, *, scope=None, model="sonnet-5-5"):
    cl = _client(monkeypatch)

    def call():
        raise exc
    token = client_mod._ATTEMPT_SCOPE.set(scope)
    try:
        with pytest.raises(type(exc)) as got:
            cl._attempt(_req(), call, model=model, connection_key="acc3",
                        wire_id="global.anthropic.claude-sonnet-5-5", provider="bedrock-satellite")
    finally:
        client_mod._ATTEMPT_SCOPE.reset(token)
    return got.value


# ── 1. the billed but unusable reply ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.real_cassette
def test_an_unusable_reply_is_recorded_marked_and_linked(monkeypatch, written):
    scope = client_mod._AttemptScope(role="judge", position=1, prompt_hash="ab" * 32, prompt_version="lantern-v1")
    exc = client_mod._billed_unusable("sonnet-5-5 returned invalid JSON", [_billed()], reply='{"lantern": ',
                                      request_id="req-amber-7", provider_model="claude-sonnet-5-5-20260901",
                                      effort="low")
    raised = _attempt_raising(monkeypatch, exc, scope=scope)
    _wait()
    assert raised is exc                                              # the caller gets the same error
    (row,) = written
    assert set(row) == set(cassette.COLUMNS)
    assert (row["reply_text"], row["reply_bytes"], row["stop_reason"]) == \
        ('{"lantern": ', len('{"lantern": '), "billed_unusable:end_turn")
    assert cassette.is_unusable(row) is True
    assert (row["model_key"], row["connection_key"], row["deployment_wire_id"], row["provider"]) == \
        ("sonnet-5-5", "acc3", "global.anthropic.claude-sonnet-5-5", "bedrock-satellite")
    assert (row["provider_request_id"], row["answered_model"], row["effort"]) == \
        ("req-amber-7", "claude-sonnet-5-5-20260901", "low")
    assert (row["role"], row["rung"], row["attempt_no"], row["route_trace"]) == ("judge", 1, 1, scope.trace_id)
    assert (row["prompt_sha256"], row["prompt_version"]) == ("ab" * 32, "lantern-v1")
    assert row["request_sha256"] == cassette.request_sha(_req(), model_key="sonnet-5-5", effort="low")
    # the billed record the gateway's billed_unusable ledger row is built from carries the same link
    (billed,) = exc.billed_usage
    assert (billed["route_trace"], billed["route_position"], billed["attempt_no"]) == (scope.trace_id, 1, 1)
    assert cassette.stats()["unusable"] == 1 and cassette.stats()["recorded"] == 1


@pytest.mark.real_cassette
def test_an_unusable_reply_without_text_still_gets_its_row(monkeypatch, written):
    exc = client_mod._billed_unusable("empty content", [_billed(stop_reason=None)])
    assert exc.reply_facts is None
    _attempt_raising(monkeypatch, exc, scope=client_mod._AttemptScope())
    _wait()
    (row,) = written
    assert (row["reply_text"], row["reply_bytes"], row["stop_reason"]) == ("", 0, "billed_unusable")
    assert row["reply_sha256"] == cassette.hashlib.sha256(b"").hexdigest()


@pytest.mark.real_cassette
def test_the_billed_error_is_found_in_a_wrapped_chain_and_recorded_once(monkeypatch, written):
    inner = client_mod._billed_unusable("schema mismatch", [_billed()], reply="half a reply")
    outer = RuntimeError("legacy chain failed")
    outer.__cause__ = inner
    _attempt_raising(monkeypatch, outer, scope=client_mod._AttemptScope())
    _attempt_raising(monkeypatch, outer, scope=client_mod._AttemptScope())     # seen again by an outer attempt
    _wait()
    (row,) = written
    assert row["reply_text"] == "half a reply"


@pytest.mark.real_cassette
def test_a_failure_with_nothing_billed_writes_no_row(monkeypatch, written):
    monkeypatch.setattr(client_mod, "log_gateway_failed_attempt", MagicMock())
    _attempt_raising(monkeypatch, RuntimeError("acc3 outage"), scope=client_mod._AttemptScope())
    _wait()
    assert written == [] and cassette.stats()["unusable"] == 0


@pytest.mark.real_cassette
def test_a_broken_recorder_never_changes_the_error(monkeypatch, written):
    def broken(*a, **kw):
        raise ValueError("bad row")
    monkeypatch.setattr(cassette, "build_row", broken)
    exc = client_mod._billed_unusable("bad", [_billed()], reply="x")
    assert _attempt_raising(monkeypatch, exc, scope=client_mod._AttemptScope()) is exc
    _wait()
    assert written == [] and cassette.stats()["build_failed"] == 1


def test_off_writes_no_unusable_row(monkeypatch, written):
    monkeypatch.setenv(cassette.SWITCH_ENV, "off")
    _attempt_raising(monkeypatch, client_mod._billed_unusable("bad", [_billed()], reply="x"),
                     scope=client_mod._AttemptScope())
    _wait()
    assert written == [] and cassette.stats()["unusable"] == 0


def test_the_mark_fits_the_check_and_is_recognised():
    assert cassette.unusable_stop_reason("max_tokens") == "billed_unusable:max_tokens"
    assert cassette.unusable_stop_reason(None) == "billed_unusable"
    row = cassette.build_row(_req(), cassette._UnusableReply(
        client_mod._billed_unusable("bad", [_billed(stop_reason="x" * 100)]), attempt_no=1, wire_id=None,
        connection_key=None), model="sonnet-5-5")
    assert len(row["stop_reason"]) == 64 and cassette.is_unusable(row)
    assert not cassette.is_unusable({"stop_reason": "end_turn"}) and not cassette.is_unusable({"stop_reason": None})
    assert not cassette.is_unusable({"stop_reason": "billed_unusablex"})


def test_replay_by_request_sha_never_serves_an_unusable_reply():
    sql = cassette._BY_REQUEST_SQL
    assert "c.stop_reason <> 'billed_unusable'" in sql and "c.stop_reason NOT LIKE 'billed_unusable:%'" in sql
    assert "billed_unusable" not in cassette._BY_CALL_LOG_SQL         # a billed_unusable ledger row finds its reply


def test_reply_facts_keep_only_text_values():
    exc = client_mod._billed_unusable("bad", [_billed()], reply="", request_id=None, provider_model=7, effort="high",
                                      other="dropped")
    assert exc.reply_facts == {"effort": "high"}
    assert client_mod._converse_reply_text([{"text": "Dawn at "}, {"toolUse": {"input": {"b": 1, "a": "é"}}}]) == \
        'Dawn at \n{"a": "é", "b": 1}'
    assert client_mod._converse_reply_text(None) is None and client_mod._converse_reply_text("bad") is None


# ── the adapters attach what the provider sent ─────────────────────────────────────────────────────────────────────

@pytest.mark.real_cassette
def test_a_converse_schema_mismatch_records_the_tool_input(gw, written):
    target = VendorDeployment(_dep("haiku-4-5", "acc3", HAIKU_WIRE, 10), gw.conns["acc3"])
    with pytest.raises(BilledUnusableReply):
        gw.client.probe(_req(stage="adhoc_conn_test", json_schema=STRICT), HAIKU45, target, count=False)
    _wait()
    (row,) = written
    assert (row["reply_text"], row["stop_reason"], row["model_key"]) == \
        ('{"score": 3}', "billed_unusable:tool_use", "haiku-4-5")
    assert (row["role"], row["connection_key"], row["attempt_no"]) == ("validate", "acc3", 1)
    # the probe's billed_unusable ledger row now carries the route trace of the cassette row
    (billed,) = client_mod.log_gateway_billed_failure.call_args.kwargs["billed"]
    assert (billed["route_trace"], billed["attempt_no"]) == (row["route_trace"], row["attempt_no"])


@pytest.mark.real_cassette
def test_an_anthropic_reply_that_is_not_json_records_its_text(gw, written):
    conn = _anthropic(True)
    target = VendorDeployment(_dep("sonnet-5-5", "anthropic-direct", "claude-sonnet-5-5", 100), conn)
    with pytest.raises(BilledUnusableReply):
        gw.client.probe(_req(stage="adhoc_conn_test", json_schema=STRICT), SONNET55_API, target, count=False)
    _wait()
    (row,) = written
    assert (row["reply_text"], row["stop_reason"], row["provider_request_id"], row["answered_model"]) == \
        ("from anthropic", "billed_unusable:end_turn", "r", "claude-sonnet-5-5")


# ── 2. embeddings: not recorded, by design ─────────────────────────────────────────────────────────────────────────

def test_embeddings_are_documented_as_not_recorded():
    assert "Not recorded, by design: embeddings" in cassette.__doc__
    assert "writes NO cassette row" in embed_mod.__doc__
    assert not hasattr(embed_mod, "cassette")                         # the embedding path never imports it


# ── 3. the ledger link rule ────────────────────────────────────────────────────────────────────────────────────────

def _ledger_ok_row(resp):
    """What test_connection writes for an answered probe (record_call_with_pool with billed_fields), folded the way
    record_call stores it: the model column, quality_signal.route_trace and attempt_no."""
    fields = call_log.billed_fields(resp)
    model, _ = call_log._normalize_model_provider(resp.model_used, None)
    signal = call_log._with_route({"connection_test": "acc3"}, fields["route_trace"], fields["route_position"])
    return {"model": model, "route_trace": signal.get("route_trace"), "attempt_no": fields["attempt_no"]}


@pytest.mark.real_cassette
def test_two_acc3_probes_of_one_model_are_two_ledger_rows_and_two_cassette_rows(gw, written):
    """The S272 live case: acc3 has the legacy key ``haiku`` (InvokeModel) and the catalog key ``haiku-4-5``
    (Converse) on one wire id. The daily probe calls both. Both ledger rows say ``haiku-4-5``; the cassette says
    which key was called. Joined on (route_trace, attempt_no), each cassette row has exactly one ledger row."""
    from tests.unit.test_aa1002_s3_deployment_routing import HAIKU
    legacy = VendorDeployment(_dep("haiku", "acc3", HAIKU_WIRE, 10, origin="legacy_constant"), gw.conns["acc3"])
    converse = VendorDeployment(_dep("haiku-4-5", "acc3", HAIKU_WIRE, 10), gw.conns["acc3"])
    replies = [gw.client.probe(_req(stage="adhoc_conn_test"), HAIKU, legacy, count=False),
               gw.client.probe(_req(stage="adhoc_conn_test"), HAIKU45, converse, count=False)]
    _wait()
    written.sort(key=lambda w: w["model_key"])                                 # background writes: any order
    ledger = [_ledger_ok_row(r) for r in replies]
    assert [r["model"] for r in ledger] == ["haiku-4-5", "haiku-4-5"]          # by model name: 2 ledger rows ...
    assert [w["model_key"] for w in written] == ["haiku", "haiku-4-5"]         # ... and the cassette keys differ
    assert sum(1 for w in written if w["model_key"] == "haiku-4-5") == 1       # the S272 "2 : 1" reading
    for row in written:                                                        # by attempt: 1 : 1
        matches = [lg for lg in ledger if (lg["route_trace"], lg["attempt_no"]) == (row["route_trace"],
                                                                                     row["attempt_no"])]
        assert len(matches) == 1
    assert len({(w["route_trace"], w["attempt_no"]) for w in written}) == 2    # two gateway requests, no retry


@pytest.mark.real_cassette
def test_a_truncation_retry_shares_the_attempt_number_and_is_told_apart(monkeypatch, written):
    """The one case where two cassette rows share (route_trace, attempt_no): the cut reply and its retry."""
    cl = _client(monkeypatch)
    scope = client_mod._AttemptScope(position=0)
    token = client_mod._ATTEMPT_SCOPE.set(scope)
    try:
        cut = cl._attempt(_req(), lambda: LLMResponse(content="Lantern walk", model_used="satellite-sonnet-5-5",
                                                       provider="bedrock-satellite", stop_reason="max_tokens"), model="sonnet-5-5")
        scope.retry_of = scope.attempts
        again = cl._attempt(_req(max_tokens=800), lambda: LLMResponse(content="Lantern walk at dusk.",
                                                                      model_used="satellite-sonnet-5-5",
                                                                      provider="bedrock-satellite",
                                                                      stop_reason="end_turn"), model="sonnet-5-5")
    finally:
        client_mod._ATTEMPT_SCOPE.reset(token)
    _wait()
    assert cut.attempt_no == again.attempt_no == 1
    assert sorted((w["attempt_no"], w["is_truncation_retry"]) for w in written) == [(1, False), (1, True)]
    assert json.dumps([w["route_trace"] for w in written]) == json.dumps([scope.trace_id] * 2)
