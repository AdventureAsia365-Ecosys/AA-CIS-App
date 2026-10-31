"""AA-804 (ledger v2, migration 319) — one shared.llm_call_log row per attempt, with its outcome and facts.

- call_log: the v2 values derived for a row (status, app, shadow, experiment, CHECK-safe values), the v2 INSERT and
  its fallbacks (columns missing, a CHECK refusal), the AA-729 rule (both statements shipped or both relabelled).
- Gateway: a refused / failed attempt writes one 0 USD row (status, error_class, latency, connection, wire id,
  attempt number); the caller's success row carries the attempt facts through billed_fields; an aggregate error is
  never logged twice; a request refused before any attempt gets its row from the net; the admin connection test with
  no key writes one refused 'credential' row (the live check).
- Jev: a refused ask (breaker open) is a 0 USD row. DataForSEO: a budget refusal, an HTTP failure and a failed body
  status write rows with status and latency. Geoapify: failed requests are counted.
No AWS, no vendor, no DB, no paid call.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from scripts.aa729 import evalpatch
from shared.llm_client import call_log
from shared.llm_client import client as client_mod
from shared.llm_client.client import LLMClient, attempt_outcome
from shared.llm_client.connections import ConnectionCredentialError
from shared.llm_client.models import LLMRequest, LLMResponse
from shared.llm_client.vendor_route import VendorDeployment
from shared.llm_client import catalog, vendor_route
from tests.unit.test_aa1002_s3_deployment_routing import MODELS, World, _dep, _org, _route

RUN = "6f1c5f8e-1b9e-4c55-9f5e-1a2b3c4d5e6f"
TOUR = "22222222-2222-2222-2222-222222222222"


def _v2(**kw) -> dict:
    return dict(zip(call_log.V2_COLUMNS, call_log._v2_values(**kw)))


@pytest.fixture
def rows(monkeypatch):
    """Every row the gateway hands to call_log.record_calls (its background writes), in order."""
    seen: list = []

    async def record_calls(rows):              # the keyword _in_background passes
        seen.extend(rows)
    monkeypatch.setattr(call_log, "record_calls", record_calls)
    yield seen


def _wait():
    call_log.wait_for_pending_writes()


def _by_attempt(rows: list) -> list:
    """Each background write runs on its own thread, so the arrival order is not fixed; attempt_no carries it."""
    return sorted(rows, key=lambda r: r["attempt_no"] or 0)


@pytest.fixture
def gw(monkeypatch):
    """The S3 routing world (registry + fake providers, test_aa1002_s3_deployment_routing.World)."""
    hook = vendor_route.circuit_open
    w = World(monkeypatch)
    yield w
    w.reset()
    catalog.invalidate()
    vendor_route.circuit_open = hook


# ── call_log: the v2 values ────────────────────────────────────────────────────────────────────────────────────────

def test_a_plain_success_row_is_ok_for_cis():
    v = _v2(stop_reason="end_turn", quality_signal={"passed": True})
    assert (v["app"], v["status"], v["error_class"], v["is_shadow"], v["is_experiment"]) == \
        ("cis", "ok", None, False, False)


@pytest.mark.parametrize("stop", ["max_tokens", "length"])
def test_a_reply_cut_at_its_limit_is_truncated(stop):
    v = _v2(stop_reason=stop, quality_signal={})
    assert (v["status"], v["error_class"]) == ("truncated", "truncated")


def test_billed_unusable_shadow_and_eval_are_read_from_the_signal():
    assert _v2(quality_signal={"billed_unusable": True})["status"] == "billed_unusable"
    assert _v2(quality_signal={"shadow": True})["is_shadow"] is True
    with call_log.bind_eval_stage("writing_memory_eval", RUN):
        assert _v2(quality_signal={})["is_experiment"] is True


def test_values_outside_the_checks_are_written_null():
    v = _v2(quality_signal={}, status="failed", error_class="timeout", latency_ms=-5, attempt_no=0,
            tour_id="not-a-uuid", reasoning_tokens=True)
    assert v["status"] == "ok"              # an unknown status is derived again
    assert (v["error_class"], v["latency_ms"], v["attempt_no"], v["tour_id"], v["reasoning_tokens"]) == \
        (None, None, None, None, None)
    v = _v2(quality_signal={}, tour_id=TOUR, latency_ms=12, attempt_no=2, units=3, unit_kind="question")
    assert (v["tour_id"], v["latency_ms"], v["attempt_no"], v["units"]) == (TOUR, 12, 2, 3)


def test_billed_fields_carries_the_attempt_facts():
    resp = LLMResponse(content="x", model_used="m", provider="bedrock", latency_ms=840, attempt_no=2,
                       deployment_wire_id="global.anthropic.claude-sonnet-5-5", request_id="req-1", effort="high",
                       reasoning_tokens=12, prompt_hash="ab" * 32)
    got = call_log.billed_fields(resp)
    assert {k: got[k] for k in call_log.V2_BILLED_KEYS} == {
        "latency_ms": 840, "attempt_no": 2, "deployment_wire_id": "global.anthropic.claude-sonnet-5-5",
        "request_id": "req-1", "effort": "high", "reasoning_tokens": 12, "prompt_hash": "ab" * 32}
    assert set(got) == set(call_log.BILLED_FIELD_KEYS)
    assert all(v is None for v in call_log.billed_fields(MagicMock()).values())


def test_record_call_refuses_an_unknown_keyword():
    with pytest.raises(TypeError):
        asyncio.run(call_log.record_call(stage="s", role="writer", model="m", tokens_in=1, tokens_out=1,
                                         cost_usd=0.0, quality_signal={}, colour="blue"))


# ── call_log: the v2 statement and its fallbacks ───────────────────────────────────────────────────────────────────

def _record(conn, **kw):
    base = dict(stage="t2_judge", role="judge", model="satellite-gpt-6-luna", tokens_in=10, tokens_out=2,
                cost_usd=0.001, quality_signal={"passed": True}, account="acc3", latency_ms=500, attempt_no=1,
                tour_id=TOUR)
    base.update(kw)
    with patch.object(call_log.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(call_log, "get_database_url", return_value="postgresql://x"):
        asyncio.run(call_log.record_call(**base))


@pytest.fixture(autouse=True)
def _fresh_flags(monkeypatch):
    monkeypatch.setattr(call_log, "_v2_missing_at", 0.0)
    monkeypatch.setattr(call_log, "_s6_missing_at", 0.0)


def test_record_call_writes_the_v2_statement():
    conn = AsyncMock()
    _record(conn)
    sql, *args = conn.execute.call_args.args
    assert sql == call_log._INSERT_V2_SQL and len(args) == 35
    v2 = dict(zip(call_log.V2_COLUMNS, args[18:]))
    assert (v2["app"], v2["status"], v2["latency_ms"], v2["attempt_no"], v2["tour_id"]) == \
        ("cis", "ok", 500, 1, TOUR)
    assert v2["connection_key"] == "acc3"           # a Bedrock row: its account is its connection


def test_missing_columns_fall_back_to_the_pre_319_row():
    conn = AsyncMock()
    conn.execute.side_effect = [asyncpg.exceptions.UndefinedColumnError("column app does not exist"), None]
    _record(conn)
    # the S6 statement (this model is priced by estimate, an S6 field), as before 319
    assert [c.args[0] for c in conn.execute.call_args_list] == [call_log._INSERT_V2_SQL, call_log._INSERT_S6_SQL]
    assert call_log._v2_missing_at > 0               # not tried again for a while
    conn2 = AsyncMock()
    _record(conn2)
    assert conn2.execute.call_args.args[0] == call_log._INSERT_S6_SQL


def test_a_check_refusal_keeps_the_billed_row():
    conn = AsyncMock()
    conn.execute.side_effect = [asyncpg.exceptions.CheckViolationError("llm_call_log_status_check"), None]
    _record(conn)
    assert conn.execute.call_args.args[0] == call_log._INSERT_S6_SQL and call_log._v2_missing_at == 0.0


def test_the_harness_relabels_both_statements_or_v2_is_not_used(monkeypatch):
    conn = AsyncMock()
    monkeypatch.setattr(call_log, "_INSERT_SQL", evalpatch.relabel_sql(call_log._SHIPPED_INSERT_SQL, RUN))
    _record(conn)                                     # an old harness (base only): the relabelled base statement
    assert conn.execute.call_args.args[0] == call_log._INSERT_SQL
    monkeypatch.setattr(call_log, "_INSERT_V2_SQL", evalpatch.relabel_v2_sql(call_log._SHIPPED_INSERT_V2_SQL, RUN))
    conn = AsyncMock()
    _record(conn)
    sql, *args = conn.execute.call_args.args
    assert sql == call_log._INSERT_V2_SQL and "adhoc_aa729" in sql and RUN in sql and len(args) == 35
    assert "($31::boolean OR true)" in sql and "'aa729_tour', $27::uuid::text" in sql


def test_relabel_v2_refuses_a_changed_statement():
    with pytest.raises(RuntimeError):
        evalpatch.relabel_v2_sql(call_log._SHIPPED_INSERT_V2_SQL.replace("unit_kind", "unit_name"), RUN)


# ── gateway: one row per refused / failed attempt ──────────────────────────────────────────────────────────────────

def test_a_route_fallback_writes_one_0_usd_row_for_the_failed_attempt(gw, monkeypatch, rows):
    gw.fail = {"acc3": RuntimeError("An error occurred (AccessDeniedException): Error 002: Access to Bedrock "
                                       "models is not allowed for this account")}
    gw.conns["bedrock-llm-org"] = _org()
    gw.deps = [d for d in gw.deps if d.model_key != "gpt-6-luna"]
    gw.deps.append(_dep("gpt-6-luna", "bedrock-llm-org", "us.openai.gpt-6-luna", 10))
    gw.reset()
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    resp = gw.generate(stage="adhoc_s3")
    _wait()
    assert resp.content == "from bedrock-llm-org" and resp.fallback_used is True
    (row,) = rows
    assert (row["stage"], row["role"], row["model"], row["cost_usd"]) == ("adhoc_s3", "writer", "sonnet-5-5", 0.0)
    assert (row["status"], row["error_class"], row["connection_key"], row["account"]) == \
        ("refused", "access_denied", "acc3", "acc3")
    assert row["deployment_wire_id"] == "global.anthropic.claude-sonnet-5-5" and row["attempt_no"] == 1
    assert row["provider"] == "bedrock-satellite" and isinstance(row["latency_ms"], int)
    assert row["quality_signal"]["gateway_attempt"] is True and row["quality_signal"]["position"] == 0
    assert not set(row["quality_signal"]) & set(call_log.OK_RATE_KEYS)
    # the success the caller logs carries its own attempt facts
    assert (resp.attempt_no, resp.deployment_wire_id) == (2, "us.openai.gpt-6-luna")
    assert resp.latency_ms is not None and resp.prompt_hash == client_mod.prompt_hash(LLMRequest(
        system_prompt="s", user_prompt="u"))
    assert set(row) <= set(call_log._ROW_FIELDS)


def test_every_deployment_down_writes_one_row_each_and_no_row_for_the_aggregate(gw, monkeypatch, rows):
    gw.fail = {"acc3": RuntimeError("acc3 outage")}
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    with pytest.raises(RuntimeError):
        gw.generate(stage="adhoc_s3")
    _wait()
    assert [(r["model"], r["status"], r["error_class"], r["attempt_no"]) for r in _by_attempt(rows)] == [
        ("sonnet-5-5", "error", "other", 1), ("gpt-6-luna", "error", "other", 2)]


def test_a_success_writes_no_gateway_row(gw, monkeypatch, rows):
    _route(monkeypatch, "sonnet-5-5")
    resp = gw.generate(stage="adhoc_s3")
    _wait()
    assert rows == [] and resp.attempt_no == 1


def test_a_model_the_catalog_does_not_know_gets_its_row_from_the_net(gw, rows):
    with pytest.raises(RuntimeError, match="not in shared.llm_model_catalog"):
        gw.generate(stage="adhoc_s3", model_tier="opus-9-9")
    _wait()
    (row,) = rows
    assert (row["model"], row["status"], row["error_class"], row["cost_usd"]) == ("opus-9-9", "error", "other", 0.0)
    assert row["attempt_no"] is None and row["latency_ms"] == 0


def test_the_legacy_chain_logs_each_tier(gw, monkeypatch, rows):
    from shared.llm_client import bedrock_satellite as bs
    monkeypatch.setattr(bs, "invoke_claude", MagicMock(side_effect=bs.BedrockUnavailable("Error 002: Access to "
                                                                                          "Bedrock models is not "
                                                                                          "allowed")))
    with pytest.raises(RuntimeError):
        gw.generate(stage="adhoc_s3", model_tier="sonnet")
    _wait()
    assert [(r["model"], r["status"], r["error_class"], r["deployment_wire_id"]) for r in _by_attempt(rows)] == [
        ("sonnet", "refused", "access_denied", client_mod.BEDROCK_SONNET),
        ("sonnet", "refused", "access_denied", client_mod.BEDROCK_HAIKU)]
    assert [r["attempt_no"] for r in _by_attempt(rows)] == [1, 2]


def test_a_billed_unusable_attempt_keeps_its_billed_row_with_the_attempt_facts(gw, monkeypatch, rows):
    billed: list = []
    monkeypatch.setattr("shared.llm_client.client.log_gateway_billed_failure",
                        lambda request, **kw: billed.append(kw))
    rt = gw.runtime("acc3")
    rt.converse.side_effect = lambda **kw: {"output": {"message": {"content": [{"text": ""}]}},
                                            "usage": {"inputTokens": 10, "outputTokens": 0}, "stopReason": "end_turn"}
    monkeypatch.setattr("shared.llm_client.bedrock_satellite.get_satellite_client",
                        lambda service="bedrock-runtime", account="acc3": rt)
    _route(monkeypatch, "sonnet-5-5")
    with pytest.raises(client_mod.UnusableReply):
        gw.generate(stage="adhoc_s3")
    _wait()
    assert rows == []                                  # no 0 USD row: the attempt was billed
    (b,) = billed[0]["billed"]
    assert b["attempt_no"] == 1 and b["deployment_wire_id"] == "global.anthropic.claude-sonnet-5-5"
    assert b["latency_ms"] is not None and b["prompt_hash"]
    (row,) = call_log.billed_unusable_rows(stage="s", role="writer", billed=[b], error="empty")
    assert row["status"] == "billed_unusable" and row["attempt_no"] == 1


def test_a_shadow_attempt_row_is_flagged_shadow(gw, monkeypatch, rows):
    from shared.llm_client.role_config import StageConfig
    cfg = StageConfig(stage="adhoc_s3", role="judge", provider="claude", model_id="sonnet-5-5",
                      account_route="acc3", fallback_model_ids=(), shadow_model_id="opus-9-9")
    monkeypatch.setattr("shared.llm_client.shadow.record_shadow_sync", lambda **kw: None)
    gw.client._run_shadow_bound(LLMRequest(system_prompt="s", user_prompt="u", stage="adhoc_s3"), cfg,
                                   MagicMock(), "acc3")
    _wait()
    (row,) = rows
    assert row["is_shadow"] is True and row["role"] == "judge" and row["quality_signal"]["shadow"] is True


def test_the_connection_test_without_a_key_is_one_refused_credential_row(gw, monkeypatch, rows):
    """The A2 live check: POST /admin/llm-connections/anthropic-direct/test with no key fails before any HTTP call and
    writes exactly one row, status refused, error_class credential, cost 0."""
    from shared.llm_client.adapters import anthropic_messages
    target = VendorDeployment(_dep("sonnet-5-5", "anthropic-direct", "claude-sonnet-5-5", 100),
                              gw.conns["anthropic-direct"])
    monkeypatch.setattr(anthropic_messages, "send", MagicMock(side_effect=ConnectionCredentialError(
        "connection 'anthropic-direct': secret aa-llm/dev/anthropic-direct not found")))
    request = LLMRequest(system_prompt="s", user_prompt="u", stage="adhoc_conn_test", max_tokens=8)
    with pytest.raises(ConnectionCredentialError):
        gw.client.probe(request, MODELS["sonnet-5-5"], target)
    _wait()
    (row,) = rows
    assert (row["stage"], row["role"], row["model"], row["cost_usd"]) == \
        ("adhoc_conn_test", "validate", "sonnet-5-5", 0.0)
    assert (row["status"], row["error_class"], row["connection_key"], row["provider"]) == \
        ("refused", "credential", "anthropic-direct", "anthropic")
    assert row["quality_signal"]["connection_test"] == "anthropic-direct"


@pytest.mark.parametrize("exc, want", [
    (RuntimeError("Error 002: Access to Bedrock models is not allowed for this account"), ("refused", "access_denied")),
    (RuntimeError("ThrottlingException: Too many requests"), ("refused", "throttled")),
    (RuntimeError("insufficient_quota"), ("refused", "quota_exhausted")),
    (ConnectionCredentialError("no key"), ("refused", "credential")),
    (RuntimeError("read timeout"), ("error", "other")),
    (client_mod.OpenAIPlatformBlocked("blocked"), ("refused", "other")),
])
def test_attempt_outcome(exc, want):
    assert attempt_outcome(exc) == want


def test_a_budget_stop_is_a_refused_budget_row():
    from shared.llm_client import connection_budget
    assert attempt_outcome(connection_budget.exceeded("over", ("acc3",))) == ("refused", "budget")


def test_prompt_hash_is_stable_and_not_the_prompt():
    h = client_mod.prompt_hash(LLMRequest(system_prompt="Write about Lake Varra.", user_prompt="Day 1"))
    assert len(h) == 64 and "Varra" not in h
    assert h == client_mod.prompt_hash(LLMRequest(system_prompt="Write about Lake Varra.", user_prompt="Day 1"))
    assert h != client_mod.prompt_hash(LLMRequest(system_prompt="Write about Lake Varra.Day", user_prompt=" 1"))
    assert client_mod.prompt_hash(MagicMock()) is None


# ── Jev ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _fresh_decide_config(monkeypatch, d):
    """decide() caches its questions for 20 s: a decide test run just before (another module, another question set)
    would leave this test's question unknown, so nothing is asked and nothing logged. Reload from this test's conn."""
    monkeypatch.setattr(d, "_questions_loaded_at", 0.0)
    monkeypatch.setattr(d, "_allowlist_loaded_at", 0.0)
    d.reset_decision_memo()


def test_a_jev_ask_refused_by_the_open_credit_breaker_is_a_row(monkeypatch):
    from shared.llm_client import decide as d
    from tests.unit.test_aa660_decide import _conn, _q
    _fresh_decide_config(monkeypatch, d)
    monkeypatch.setattr(d, "jev_breaker_open", lambda: True)
    conn = _conn([_q()])
    with patch.object(d, "_call_jev", new=AsyncMock()) as m_call, \
            patch.object(d, "record_call_with_pool", new=AsyncMock()) as m_log:
        asyncio.run(d._decide(d._SingleConn(conn), "a3_research", "kw:x", "x", ["kw_belongs"], None))
    m_call.assert_not_awaited()
    row = m_log.await_args.kwargs
    assert (row["status"], row["error_class"], row["cost_usd"], row["model"]) == \
        ("refused", "quota_exhausted", 0.0, d.JEV_MODEL)


def test_an_answered_jev_ask_logs_its_latency_and_questions(monkeypatch):
    from shared.llm_client import decide as d
    from tests.unit.test_aa660_decide import _conn, _q
    _fresh_decide_config(monkeypatch, d)
    monkeypatch.setattr(d, "jev_breaker_open", lambda: False)
    monkeypatch.setattr(d, "jev_budget_blocks", AsyncMock(return_value=False))
    answer = {"model": "jev-latest", "usage": {"input_tokens": 100},
              "answers": {"kw_belongs": {"kind": "noul", "probability": 0.99}}}
    with patch.object(d, "_call_jev", new=AsyncMock(return_value=answer)), \
            patch.object(d, "record_call_with_pool", new=AsyncMock()) as m_log:
        asyncio.run(d._decide(d._SingleConn(_conn([_q()])), "a3_research", "kw:y", "x", ["kw_belongs"], None,
                              use_cache=False))
    row = m_log.await_args.kwargs
    assert row["units"] == 1 and row["unit_kind"] == "question" and row["latency_ms"] is not None
    assert "status" not in row                         # derived: ok


# ── DataForSEO ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _dfs(monkeypatch, budget=None):
    from services.seo_intelligence import dataforseo_client as m
    logged: list = []
    monkeypatch.setattr(m, "record_dfs_call_sync", lambda **kw: logged.append(kw))
    return m, m.DataForSEOClient(login="l", password="p", budget=budget, stage="s1_rewrite"), logged


def test_a_dfs_budget_refusal_is_a_refused_row(monkeypatch):
    from shared.cost_guard import BudgetExceeded
    budget = MagicMock()
    budget.check.side_effect = BudgetExceeded("dfs run budget $0.50 reached")
    m, client, logged = _dfs(monkeypatch, budget)
    with pytest.raises(BudgetExceeded):
        asyncio.run(client.fetch_keywords("lake varra walks", location_code=2840))
    (row,) = logged
    assert (row["status"], row["cost_usd"], row["fetched_live"], row["stage"]) == ("refused", 0.0, False,
                                                                                  "s1_rewrite")
    assert row["error"].startswith("budget:") and row["keyword"] == "lake varra walks"


class _Http:
    def __init__(self, status=200, body=None, exc=None):
        self.status, self.body, self.exc = status, body or {}, exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, auth=None, json=None):
        if self.exc:
            raise self.exc
        resp = MagicMock(status_code=self.status)
        resp.json.return_value = self.body
        if self.status >= 400:
            import httpx
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "bad", request=MagicMock(), response=MagicMock(status_code=self.status))
        return resp


def test_a_dfs_http_failure_is_a_row_with_latency(monkeypatch):
    m, client, logged = _dfs(monkeypatch)
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda timeout=30: _Http(status=402))
    assert asyncio.run(client.fetch_keyword_ideas("lake varra walks")) == []      # the caller's own handling kept
    (row,) = logged
    assert (row["status"], row["cost_usd"]) == ("refused", 0.0) and isinstance(row["latency_ms"], int)
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda timeout=30: _Http(exc=OSError("connection reset")))
    logged.clear()
    assert asyncio.run(client.fetch_keyword_ideas("lake varra walks")) == []
    assert logged[0]["status"] == "error" and "connection reset" in logged[0]["error"]


def test_a_dfs_answer_with_a_failed_status_is_an_error_row(monkeypatch):
    m, client, logged = _dfs(monkeypatch)
    body = {"status_code": 40501, "status_message": "Invalid field", "cost": 0, "tasks": []}
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda timeout=30: _Http(body=body))
    assert asyncio.run(client.fetch_keyword_ideas("lake varra walks")) == []
    (row,) = logged
    assert (row["status"], row["fetched_live"]) == ("error", True) and "40501" in row["error"]
    ok = {"status_code": 20000, "cost": 0.05, "tasks": [{"status_code": 20000, "result": []}]}
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda timeout=30: _Http(body=ok))
    logged.clear()
    asyncio.run(client.fetch_keyword_ideas("lake varra walks"))
    assert logged[0]["status"] == "ok" and logged[0]["cost_usd"] == 0.05 and logged[0]["latency_ms"] is not None


def test_the_dfs_writer_uses_the_v2_statement_and_falls_back():
    from shared.dfs_client import call_log as dfs_log
    dfs_log._v2_missing_at = 0.0
    conn = AsyncMock()
    with patch.object(dfs_log.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(dfs_log, "get_database_url", return_value="postgresql://x"):
        asyncio.run(dfs_log.record_dfs_call(endpoint="search_volume", status="refused", error="budget: x",
                                            cost_usd=0.0, latency_ms=None, stage="s1_rewrite"))
    sql, *args = conn.execute.call_args.args
    assert sql == dfs_log._INSERT_V2_SQL and args[11:] == ["refused", None, "budget: x", "s1_rewrite", "cis"]
    conn = AsyncMock()
    conn.execute.side_effect = [asyncpg.exceptions.UndefinedColumnError("status"), None]
    with patch.object(dfs_log.asyncpg, "connect", AsyncMock(return_value=conn)), \
            patch.object(dfs_log, "get_database_url", return_value="postgresql://x"):
        asyncio.run(dfs_log.record_dfs_call(endpoint="search_volume"))
    assert conn.execute.call_args.args[0] == dfs_log._INSERT_SQL
    dfs_log._v2_missing_at = 0.0


# ── Geoapify ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_geoapify_failed_requests_are_counted():
    from services.places.geocoder import GeoapifyGeocoder, GeocoderError
    responses = iter([MagicMock(status_code=503, headers={}), MagicMock(status_code=500, headers={}),
                      MagicMock(status_code=200, json=lambda: {"results": []})])
    http = MagicMock()
    http.get = AsyncMock(side_effect=lambda *a, **k: next(responses))
    g = GeoapifyGeocoder(http=http, api_key="k", sleep=AsyncMock(), rate_per_s=1000)
    assert asyncio.run(g.search("Lake Varra", "np")) == {"results": []}
    assert (g.requests, g.failed_requests) == (3, 2)
    http.get = AsyncMock(return_value=MagicMock(status_code=400, headers={}))
    with pytest.raises(GeocoderError):
        asyncio.run(g.search("Lake Varra", "np"))
    assert (g.requests, g.failed_requests) == (4, 3)


def test_log_geocode_calls_writes_the_tenant_and_the_failures():
    from services.places import store
    conn = AsyncMock()
    with call_log.bind_tenant("33333333-3333-3333-3333-333333333333"):
        asyncio.run(store.log_geocode_calls(conn, "geoapify", 5, 0.01, None, failed_requests=2))
    sql, *args = conn.execute.call_args.args
    assert sql == store._GEOCODE_CALL_V2_SQL and args[6:] == ["33333333-3333-3333-3333-333333333333", 2]
    conn = AsyncMock()
    conn.execute.side_effect = [asyncpg.exceptions.UndefinedColumnError("tenant_id"), None]
    asyncio.run(store.log_geocode_calls(conn, "geoapify", 5, 0.01, None))
    assert conn.execute.call_args.args[0] == store._GEOCODE_CALL_SQL
    assert json.dumps(args[:6])        # the pre-319 values are unchanged
