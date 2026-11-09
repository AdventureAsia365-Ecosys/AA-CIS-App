"""AA-900 (S271, root ADR 0020 d8) — CIS 385 shared.llm_cassette and the gateway recorder on real Postgres.

The table itself (CHECKs, pii tag, grants): test_aa900_migration_387.py.
- The recorder (cassette.record_attempt, its background write on its own connection): the row of a returned reply
  carries the links; the caller's ledger row (call_log.record_call_with_pool with billed_fields) is found again from
  its id (get_by_call_log_id: route_trace + attempt_no, a truncation retry told apart); get_by_request_sha replays a
  whole reply; (S272) a billed but unusable reply is a marked row its billed_unusable ledger row finds, never replayed.
  A failed write does not raise, LLM_CASSETTE=off writes nothing, a row is written as cis_app.

Invented names and texts only; every row of this module has stage adhoc_aa900_cassette and is deleted at the end.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.llm_client import call_log, cassette  # noqa: E402
from shared.llm_client import client as client_mod  # noqa: E402
from shared.llm_client.models import LLMRequest, LLMResponse  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
STAGE = "adhoc_aa900_cassette"
JOB = "3c2b1a09-8f7e-4d6c-9b5a-4c3b2a1f0e9d"
TENANT = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
PIECE = "2f1e0d9c-8b7a-4c6d-9e5f-0a1b2c3d4e5f"
MIGRATION = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "385_llm_cassette.sql")


@pytest_asyncio.fixture
async def pool(monkeypatch):
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    async with p.acquire() as c:
        with open(MIGRATION, encoding="utf-8") as f:
            await c.execute(f.read())                  # idempotent: a test before this one may have dropped it
        await _cleanup(c)

    async def connect():                               # the local test DB has no TLS
        return await asyncpg.connect(DSN)
    monkeypatch.setattr(cassette, "_connect", connect)
    monkeypatch.setenv(cassette.SWITCH_ENV, cassette.MODE_RECORD)
    monkeypatch.setattr(client_mod, "stage_pii_class", lambda request: "content")
    cassette.reset_stats()
    yield p
    async with p.acquire() as c:
        await _cleanup(c)
    await p.close()
    cassette.reset_stats()


async def _cleanup(c):
    await c.execute("DELETE FROM shared.llm_cassette WHERE stage = $1", STAGE)
    await c.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)


def _request(**kw):
    base = dict(system_prompt="You describe the Opal Coast in plain words.", user_prompt="Day one: Harbor walk.",
                max_tokens=1200, temperature=0.3, stage=STAGE)
    base.update(kw)
    return LLMRequest(**base)


def _reply(scope, *, content="Morning walk along the Opal harbor.", attempt_no=1, request_id="req-opal-1",
           stop_reason="end_turn", **kw):
    return LLMResponse(content=content, model_used="satellite-sonnet-5-5", provider="bedrock-satellite",
                       input_tokens=40, output_tokens=12, cost_usd=0.0004, stop_reason=stop_reason,
                       connection_key="acc3", deployment_wire_id="global.anthropic.claude-sonnet-5-5",
                       request_id=request_id, effort="medium", attempt_no=attempt_no,
                       prompt_hash=scope.prompt_hash, route_trace=scope.trace_id, route_position=scope.position,
                       latency_ms=900, **kw)


def _record(request, resp, scope, *, retry=False):
    cassette.record_attempt(request, resp, model="sonnet-5-5", connection_key="acc3",
                            wire_id="global.anthropic.claude-sonnet-5-5", provider="bedrock-satellite", scope=scope,
                            truncation_retry=retry)
    call_log.wait_for_pending_writes()


async def _ledger(pool, resp, *, quality_signal=None):
    """The caller's ledger row, through the real ledger write (billed_fields carries route_trace + attempt_no)."""
    await call_log.record_call_with_pool(
        pool, stage=STAGE, role="writer", model=resp.model_used, tokens_in=resp.input_tokens,
        tokens_out=resp.output_tokens, cost_usd=resp.cost_usd, quality_signal=quality_signal or {"passed": True},
        content_piece_id=PIECE, stop_reason=resp.stop_reason, **call_log.billed_fields(resp))
    async with pool.acquire() as c:
        return str(await c.fetchval("SELECT id FROM shared.llm_call_log WHERE stage = $1 AND request_id = $2 "
                                    "ORDER BY created_at DESC LIMIT 1", STAGE, resp.request_id))


# ── the recorder, linked to the ledger ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_reply_is_found_again_from_its_ledger_row_and_its_request_sha(pool):
    scope = client_mod._AttemptScope(role="writer", position=1, prompt_hash=client_mod.prompt_hash(_request()),
                                     prompt_version="opal-v2")
    req = _request()
    resp = _reply(scope, provider_model="claude-sonnet-5-5-20260901")
    with call_log.bind_job(JOB), call_log.bind_tenant(TENANT), cassette.bind_owner(content_piece_id=PIECE):
        _record(req, resp, scope)
    assert cassette.stats()["written"] == 1
    ledger_id = await _ledger(pool, resp)
    async with pool.acquire() as c:
        got = await cassette.get_by_call_log_id(c, ledger_id)
        assert got is not None and got["call_log_id"] == ledger_id
        assert (got["reply_text"], got["model_key"], got["answered_model"], got["rung"], got["attempt_no"]) == \
            ("Morning walk along the Opal harbor.", "sonnet-5-5", "claude-sonnet-5-5-20260901", 1, 1)
        assert (got["route_trace"], got["provider_request_id"], got["connection_key"]) == \
            (scope.trace_id, "req-opal-1", "acc3")
        assert (got["job_id"], got["tenant_id"], got["content_piece_id"], got["ledger_content_piece_id"]) == \
            (JOB, TENANT, PIECE, PIECE)
        assert (got["prompt_sha256"], got["prompt_version"], got["effort"], got["max_tokens"]) == \
            (scope.prompt_hash, "opal-v2", "medium", 1200)
        sha = cassette.request_sha(req, model_key="sonnet-5-5", effort="medium")
        assert got["request_sha256"] == sha
        replay = await cassette.get_by_request_sha(c, sha)
        assert replay["id"] == got["id"] and replay["reply_text"] == got["reply_text"]
        assert await cassette.get_by_request_sha(c, sha, model_key="haiku-5-5") is None
        assert await cassette.get_by_call_log_id(c, str(uuid.uuid4())) is None


@pytest.mark.asyncio
async def test_the_truncation_retry_reply_is_told_apart_from_the_cut_one(pool):
    scope = client_mod._AttemptScope(role="writer", position=0)
    req = _request()
    cut = _reply(scope, content="Morning walk along the", stop_reason="max_tokens", request_id=None)
    retry = _reply(scope, content="Morning walk along the Opal harbor, then lunch.", request_id=None,
                   truncation_retry_max_tokens=2400)
    _record(req, cut, scope)
    _record(req.model_copy(update={"max_tokens": 2400}), retry, scope, retry=True)    # same trace, same attempt_no
    await _ledger(pool, retry, quality_signal={"passed": True})
    async with pool.acquire() as c:
        ledger_id = str(await c.fetchval(
            "SELECT id FROM shared.llm_call_log WHERE stage = $1 AND quality_signal->>'truncation_retry' = 'true'",
            STAGE))
        got = await cassette.get_by_call_log_id(c, ledger_id)
        assert got["is_truncation_retry"] is True and got["reply_text"].endswith("then lunch.")
        assert got["max_tokens"] == 2400
        # the cut reply stays the recorded answer of its own request (the retry was a different request: 2x ceiling)
        assert (await cassette.get_by_request_sha(
            c, cassette.request_sha(req, model_key="sonnet-5-5", effort="medium")))["reply_text"] == \
            "Morning walk along the"


@pytest.mark.asyncio
async def test_an_unusable_reply_is_found_from_its_billed_ledger_row_but_never_replayed(pool):
    """S272: the billed but unusable reply is a marked row; its billed_unusable ledger row (route_trace + attempt_no
    from the enriched billed record) finds it; replay by request sha does not serve it."""
    scope = client_mod._AttemptScope(role="judge", position=2)
    req = _request(json_schema={"name": "opal_check", "schema": {"type": "object"}})
    billed = {"model": "satellite-sonnet-5-5", "account": "acc3", "tokens_in": 40, "tokens_out": 9,
              "cost_usd": 0.0003, "stop_reason": "max_tokens", "provider_requests": 1,
              "attempt_no": 1, "route_trace": scope.trace_id, "route_position": 2, "connection_key": "acc3"}
    exc = client_mod._billed_unusable("sonnet-5-5 returned invalid JSON", [billed], reply='{"opal": "half',
                                      request_id="req-opal-9")
    cassette.record_unusable_attempt(req, exc, model="sonnet-5-5", connection_key="acc3",
                                     wire_id="global.anthropic.claude-sonnet-5-5", provider="bedrock-satellite",
                                     scope=scope, attempt_no=1)
    call_log.wait_for_pending_writes()
    assert cassette.stats()["written"] == 1 and cassette.stats()["unusable"] == 1
    (row,) = call_log.billed_unusable_rows(stage=STAGE, role="judge", billed=[billed], error=str(exc),
                                           quality_signal_extra={"gateway_attempt": True})
    await call_log.record_call_with_pool(pool, **row)             # the gateway's billed_unusable row
    async with pool.acquire() as c:
        ledger_id = str(await c.fetchval("SELECT id FROM shared.llm_call_log WHERE stage = $1 "
                                         "AND status = 'billed_unusable'", STAGE))
        got = await cassette.get_by_call_log_id(c, ledger_id)
        assert got is not None and got["ledger_status"] == "billed_unusable" and cassette.is_unusable(got)
        assert (got["reply_text"], got["stop_reason"], got["attempt_no"], got["rung"]) == \
            ('{"opal": "half', "billed_unusable:max_tokens", 1, 2)
        assert await cassette.get_by_request_sha(c, got["request_sha256"]) is None


@pytest.mark.asyncio
async def test_a_failed_write_and_the_off_switch_write_nothing_and_never_raise(pool, monkeypatch):
    scope = client_mod._AttemptScope()

    async def refuse():
        raise OSError("database unreachable")
    with monkeypatch.context() as m:
        m.setattr(cassette, "_connect", refuse)
        _record(_request(), _reply(scope), scope)
    assert cassette.stats()["write_failed"] == 1
    monkeypatch.setenv(cassette.SWITCH_ENV, "off")
    _record(_request(), _reply(scope), scope)
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) AS n FROM shared.llm_cassette WHERE stage = $1", STAGE) == 0


@pytest.mark.asyncio
async def test_the_row_is_written_as_cis_app(pool):
    async with pool.acquire() as c:
        if await c.fetchval("SELECT count(*) AS n FROM pg_roles WHERE rolname = 'cis_app'") == 0:
            pytest.skip("no cis_app role in this database")
        async with c.transaction():
            await c.execute("GRANT USAGE ON SCHEMA shared TO cis_app")    # other modules' teardowns may revoke it
            await c.execute("SET LOCAL ROLE cis_app")
            scope = client_mod._AttemptScope(position=0)
            row = cassette.build_row(_request(), _reply(scope), model="sonnet-5-5", scope=scope)
            assert await cassette.write_row(row, conn=c) is True
            got = await c.fetchrow("SELECT stage, rung, reply_bytes FROM shared.llm_cassette "
                                   "WHERE route_trace = $1::uuid", scope.trace_id)
            assert (got["stage"], got["rung"], got["reply_bytes"]) == (STAGE, 0, len(row["reply_text"]))
