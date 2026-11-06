"""AA-900 (S271, root ADR 0020 d8) — the gateway's cassette recorder (shared/llm_client/cassette.py), record-only.

- request_sha: stable over key order, moves on any message or param; capped_reply: 256 KiB at a character boundary,
  sha and size of the whole reply.
- The gateway: one row per provider attempt that returned a reply (none for a failed attempt), with its links:
  route trace + attempt number (the ledger row's), rung, model, connection, wire id, prompt sha / version, the bound
  job / tenant / eval run / owner; the truncation retry and a shadow are marked; a pii stage keeps no text.
- Best-effort: a write that fails, a missing table and a row that cannot be built never fail the call (counted);
  LLM_CASSETTE=off writes nothing.
No AWS, no vendor, no DB, no paid call. Names and texts are invented.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest

from shared.llm_client import call_log, cassette
from shared.llm_client import client as client_mod
from shared.llm_client.models import LLMRequest, LLMResponse
from tests.unit.test_aa1002_s3_deployment_routing import World, _dep, _org, _route

TENANT = "5b0c3f4e-7a1d-4e2b-9c8d-0a1b2c3d4e5f"
JOB = "0d9e8f7a-6b5c-4d3e-8f2a-1b2c3d4e5f60"
PIECE = "7e6d5c4b-3a29-4180-9f7e-6d5c4b3a2918"
TOUR = "1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9"
RUN = "eval-run-lantern-01"


@pytest.fixture
def written(monkeypatch):
    """Every row the recorder hands to its background write, instead of a DB insert."""
    seen: list = []

    async def write_row(row, conn=None):
        seen.append(row)
        return True
    monkeypatch.setattr(cassette, "write_row", write_row)
    yield seen


@pytest.fixture
def gw(monkeypatch):
    from shared.llm_client import catalog, vendor_route
    hook = vendor_route.circuit_open
    w = World(monkeypatch)
    yield w
    w.reset()
    catalog.invalidate()
    vendor_route.circuit_open = hook


def _wait():
    call_log.wait_for_pending_writes()


def _req(**kw):
    base = dict(system_prompt="You write about the Amber Valley.", user_prompt="Two days in Lumen Bay.",
                max_tokens=800, temperature=0.2, stage="adhoc_aa900")
    base.update(kw)
    return LLMRequest(**base)


# ── request sha and reply cap ──────────────────────────────────────────────────────────────────────────────────────

def test_request_sha_is_stable_and_moves_on_every_part():
    base = cassette.request_sha(_req(), model_key="sonnet-5-5", effort="high")
    assert base == cassette.request_sha(_req(), model_key="sonnet-5-5", effort="high")
    assert len(base) == 64
    others = {
        cassette.request_sha(_req(system_prompt="Other"), model_key="sonnet-5-5", effort="high"),
        cassette.request_sha(_req(user_prompt="Other"), model_key="sonnet-5-5", effort="high"),
        cassette.request_sha(_req(max_tokens=801), model_key="sonnet-5-5", effort="high"),
        cassette.request_sha(_req(temperature=0.3), model_key="sonnet-5-5", effort="high"),
        cassette.request_sha(_req(seed=7), model_key="sonnet-5-5", effort="high"),
        cassette.request_sha(_req(json_schema={"name": "v", "schema": {"type": "object"}}),
                             model_key="sonnet-5-5", effort="high"),
        cassette.request_sha(_req(few_shots=[{"role": "user", "content": "x"}]), model_key="sonnet-5-5",
                             effort="high"),
        cassette.request_sha(_req(), model_key="haiku-5-5", effort="high"),
        cassette.request_sha(_req(), model_key="sonnet-5-5", effort=None),
    }
    assert base not in others and len(others) == 9


def test_request_sha_ignores_dict_key_order():
    a = _req(json_schema={"name": "v", "schema": {"type": "object", "required": ["a"]}})
    b = _req(json_schema={"schema": {"required": ["a"], "type": "object"}, "name": "v"})
    assert cassette.request_sha(a, model_key="m") == cassette.request_sha(b, model_key="m")


def test_a_reply_over_the_cap_is_cut_on_a_character_boundary():
    text = "é" * (cassette.REPLY_CAP_BYTES // 2 + 10)           # two bytes each
    stored, sha, size, cut = cassette.capped_reply(text)
    assert cut is True and size == len(text.encode("utf-8"))
    assert len(stored.encode("utf-8")) <= cassette.REPLY_CAP_BYTES and stored == "é" * (cassette.REPLY_CAP_BYTES // 2)
    assert sha == cassette.hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert cassette.capped_reply("short") == ("short", cassette.hashlib.sha256(b"short").hexdigest(), 5, False)
    assert cassette.capped_reply(None)[2] == 0


def test_the_switch_defaults_to_record(monkeypatch):
    monkeypatch.delenv(cassette.SWITCH_ENV, raising=False)
    assert cassette.mode() == "record"
    for off in ("off", "OFF", "0", "false", "no"):
        monkeypatch.setenv(cassette.SWITCH_ENV, off)
        assert cassette.enabled() is False
    monkeypatch.setenv(cassette.SWITCH_ENV, "record")
    assert cassette.enabled() is True


# ── the gateway records each returned reply ────────────────────────────────────────────────────────────────────────

@pytest.mark.real_cassette
def test_a_fallback_answer_is_one_row_linked_to_its_ledger_facts(gw, monkeypatch, written):
    monkeypatch.setattr(call_log, "record_calls", AsyncMock())          # the failed attempt's ledger row
    gw.fail = {"acc3": RuntimeError("acc3 outage")}
    gw.conns["bedrock-llm-org"] = _org()
    gw.deps = [d for d in gw.deps if d.model_key != "gpt-6-luna"]
    gw.deps.append(_dep("gpt-6-luna", "bedrock-llm-org", "us.openai.gpt-6-luna", 10))
    gw.reset()
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    with call_log.bind_job(JOB), call_log.bind_tenant(TENANT), cassette.bind_owner(content_piece_id=PIECE,
                                                                                    tour_id=TOUR):
        resp = gw.generate(stage="adhoc_s3", max_tokens=900)
    _wait()
    assert resp.content == "from bedrock-llm-org"
    (row,) = written                                   # the failed acc3 attempt returned nothing: no row
    assert set(row) == set(cassette.COLUMNS)
    assert (row["stage"], row["role"], row["model_key"], row["connection_key"], row["deployment_wire_id"]) == \
        ("adhoc_s3", "writer", "gpt-6-luna", "bedrock-llm-org", "us.openai.gpt-6-luna")
    # the same two facts the caller's ledger row gets through billed_fields (route_trace + attempt_no)
    assert (row["route_trace"], row["attempt_no"], row["rung"]) == (resp.route_trace, resp.attempt_no, 1)
    assert (row["reply_text"], row["reply_bytes"], row["reply_truncated"], row["reply_withheld"]) == \
        ("from bedrock-llm-org", len("from bedrock-llm-org"), False, None)
    assert row["prompt_sha256"] == resp.prompt_hash == client_mod.prompt_hash(LLMRequest(system_prompt="s",
                                                                                         user_prompt="u"))
    # the request as SENT: the token floor raised the asked 900 to the model's floor before the attempt
    assert row["max_tokens"] > 900
    assert row["request_sha256"] == cassette.request_sha(LLMRequest(system_prompt="s", user_prompt="u",
                                                                     max_tokens=row["max_tokens"]),
                                                         model_key="gpt-6-luna", effort=resp.effort)
    assert (row["tenant_id"], row["job_id"], row["content_piece_id"], row["tour_id"]) == (TENANT, JOB, PIECE, TOUR)
    assert row["is_shadow"] is False and row["is_truncation_retry"] is False
    assert cassette.stats()["recorded"] == 1


@pytest.mark.real_cassette
def test_a_success_on_the_first_rung_is_rung_0_with_the_eval_run(gw, monkeypatch, written):
    _route(monkeypatch, "sonnet-5-5")
    with call_log.bind_eval_stage("writing_memory_eval", RUN), cassette.bind_owner(eval_item="item-3"):
        resp = gw.generate(stage="adhoc_s3")
    _wait()
    (row,) = written
    assert (row["rung"], row["attempt_no"], row["eval_run"], row["eval_item"]) == (0, 1, RUN, "item-3")
    assert row["stage"] == "adhoc_s3"            # the real stage (the ledger relabels it, the cassette does not)
    assert row["route_trace"] == resp.route_trace


def _attempt(monkeypatch, resp, *, scope=None, request=None):
    """One LLMClient._attempt with a fixed reply, inside ``scope`` (a gateway request)."""
    with monkeypatch.context() as m:
        m.setattr("shared.llm_client.client.boto3", MagicMock())
        cl = client_mod.LLMClient()
    token = client_mod._ATTEMPT_SCOPE.set(scope)
    try:
        return cl._attempt(request or _req(), lambda: resp, model="sonnet-5-5", connection_key="acc3",
                           wire_id="global.anthropic.claude-sonnet-5-5", provider="bedrock-satellite")
    finally:
        client_mod._ATTEMPT_SCOPE.reset(token)


def _resp(**kw):
    base = dict(content="A calm morning by the Jade Lagoon.", model_used="satellite-sonnet-5-5",
                provider="bedrock-satellite", stop_reason="end_turn", effort="medium",
                provider_model="claude-sonnet-5-5-20260901", request_id="req-lantern-1")
    base.update(kw)
    return LLMResponse(**base)


@pytest.mark.real_cassette
def test_the_truncation_retry_and_a_shadow_are_marked(monkeypatch, written):
    scope = client_mod._AttemptScope(role="judge", position=2, extra={"shadow": True}, attempts=1, retry_of=1)
    resp = _attempt(monkeypatch, _resp(), scope=scope)
    _wait()
    (row,) = written
    assert (row["role"], row["rung"], row["attempt_no"], row["is_truncation_retry"], row["is_shadow"]) == \
        ("judge", 2, 1, True, True)
    assert (row["answered_model"], row["provider_request_id"], row["effort"], row["stop_reason"]) == \
        ("claude-sonnet-5-5-20260901", "req-lantern-1", "medium", "end_turn")
    assert row["route_trace"] == scope.trace_id == resp.route_trace


@pytest.mark.real_cassette
def test_a_pii_stage_keeps_the_sha_but_no_text(monkeypatch, written):
    monkeypatch.setattr(client_mod, "stage_pii_class", lambda request: "pii")
    _attempt(monkeypatch, _resp(content="Guest note"), scope=client_mod._AttemptScope())
    _wait()
    (row,) = written
    assert (row["reply_text"], row["reply_withheld"], row["reply_bytes"]) == (None, "pii_stage", len("Guest note"))
    assert row["reply_sha256"] == cassette.hashlib.sha256(b"Guest note").hexdigest()


def test_off_writes_nothing(monkeypatch, written):
    monkeypatch.setenv(cassette.SWITCH_ENV, "off")
    resp = _attempt(monkeypatch, _resp(), scope=client_mod._AttemptScope())
    _wait()
    assert written == [] and resp.content.startswith("A calm") and cassette.stats()["recorded"] == 0


# ── best-effort: nothing here fails the call ───────────────────────────────────────────────────────────────────────

@pytest.mark.real_cassette
def test_a_failed_write_is_counted_and_the_call_goes_on(monkeypatch):
    async def refuse():
        raise OSError("database unreachable")
    monkeypatch.setattr(cassette, "_connect", refuse)
    resp = _attempt(monkeypatch, _resp(), scope=client_mod._AttemptScope())
    _wait()
    assert resp.content.startswith("A calm") and resp.attempt_no == 1
    assert cassette.stats()["write_failed"] == 1 and cassette.stats()["written"] == 0


@pytest.mark.real_cassette
def test_a_row_that_cannot_be_built_is_counted_and_the_call_goes_on(monkeypatch, written):
    def broken(*a, **kw):
        raise ValueError("bad row")
    monkeypatch.setattr(cassette, "build_row", broken)
    resp = _attempt(monkeypatch, _resp(), scope=client_mod._AttemptScope())
    _wait()
    assert resp.content.startswith("A calm") and written == [] and cassette.stats()["build_failed"] == 1


@pytest.mark.real_cassette
def test_a_missing_table_is_skipped_for_a_while(monkeypatch):
    conn = AsyncMock()
    conn.execute.side_effect = asyncpg.exceptions.UndefinedTableError('relation "shared.llm_cassette" does not exist')

    async def connect():
        return conn
    monkeypatch.setattr(cassette, "_connect", connect)
    _attempt(monkeypatch, _resp(), scope=client_mod._AttemptScope())
    _wait()
    assert cassette.stats()["missing_table"] == 1 and conn.execute.await_count == 1 and conn.close.await_count == 1
    _attempt(monkeypatch, _resp(), scope=client_mod._AttemptScope())
    _wait()
    assert cassette.stats()["missing_table"] == 2 and conn.execute.await_count == 1      # not tried again


def test_write_row_sends_every_column_in_order():
    conn = AsyncMock()
    row = cassette.build_row(_req(), _resp(), model="sonnet-5-5", scope=client_mod._AttemptScope(position=0))
    assert asyncio.run(cassette.write_row(row, conn=conn)) is True
    sql, *args = conn.execute.call_args.args
    assert sql == cassette._INSERT_SQL and len(args) == len(cassette.COLUMNS)
    assert dict(zip(cassette.COLUMNS, args))["model_key"] == "sonnet-5-5"
    assert "$13::uuid" in sql                 # route_trace
    conn.close.assert_not_awaited()           # the caller's connection is left open


def test_long_values_are_cut_to_the_check_limits():
    row = cassette.build_row(_req(stage="s" * 300), _resp(stop_reason="x" * 100), model="m" * 400)
    assert (len(row["stage"]), len(row["model_key"]), len(row["stop_reason"])) == (128, 200, 64)
    assert row["route_trace"] is None and row["rung"] is None        # outside a gateway request


def test_bind_owner_nests_and_restores():
    with cassette.bind_owner(content_piece_id=PIECE):
        with cassette.bind_owner(tour_id=TOUR):
            assert cassette.current_owner() == {"content_piece_id": PIECE, "tour_id": TOUR}
        assert cassette.current_owner() == {"content_piece_id": PIECE}
    assert cassette.current_owner() == {}


def test_the_read_helpers_return_none_for_a_bad_id():
    conn = AsyncMock()
    assert asyncio.run(cassette.get_by_call_log_id(conn, "not-a-uuid")) is None
    conn.fetchrow.assert_not_awaited()
    conn.fetchrow.return_value = None
    assert asyncio.run(cassette.get_by_request_sha(conn, "ab" * 32)) is None
