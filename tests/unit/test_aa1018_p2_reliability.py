"""AA-1018 part 2 (S262 plan A4) — route deadline, the legacy / vendor / catalog-fallback paths inside the breaker,
the truncated refusal class, the caller's role on adhoc stages, the prompt version on replies, the connection's
access_state, the stored failure domain, the probe stamp and the connection test's output budget.

Every network seam is faked (the S3 World: no AWS, no Anthropic, no OpenAI, no DB). NULL / empty registry values must
change nothing: the deadline-off and breaker-off cases assert the same calls as before.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

from shared.llm_client import breaker, call_log, invariant, prompt_registry
from shared.llm_client import client as client_mod
from shared.llm_client.client import LLMClient, attempt_outcome
from shared.llm_client.errors import TRUNCATED, LLMAccessDenied, RouteDeadlineExceeded, classify
from shared.llm_client.models import LLMRequest
from shared.llm_client.role_config import StageConfig
from shared.llm_client.vendor_route import VendorDeployment
from tests.unit.test_aa1002_s3_deployment_routing import SONNET55, World, _anthropic, _dep

ERROR_002 = RuntimeError("An error occurred (AccessDeniedException) when calling the Converse operation: "
                         "Error 002: Access to Bedrock models is not allowed for this account")
# The live drill of 31/10/2026 (S262): OpenAI's 400 for a reply that hit max_tokens, logged as 'other' before.
OPENAI_LENGTH_400 = RuntimeError(
    "Error code: 400 - {'error': {'message': 'Could not finish the message because max_tokens or model output limit "
    "was reached. Please try again with higher max_tokens.', 'type': 'invalid_request_error', 'param': None, "
    "'code': None}}")


class Clock:
    """A monotonic clock the test moves (the gateway's deadline reads client.time.monotonic)."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def world(monkeypatch):
    from shared.llm_client import catalog, vendor_route
    hook = vendor_route.circuit_open
    w = World(monkeypatch)
    yield w
    w.reset()
    catalog.invalidate()
    vendor_route.circuit_open = hook


@pytest.fixture
def rows(monkeypatch):
    seen: list = []

    async def record_calls(rows):
        seen.extend(rows)
    monkeypatch.setattr(call_log, "record_calls", record_calls)
    yield seen


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    fake_time = MagicMock(wraps=client_mod.time)
    fake_time.monotonic = c.monotonic
    monkeypatch.setattr(client_mod, "time", fake_time)
    return c


def _stage(monkeypatch, *keys, deadline=None, role="writer", stage="adhoc_p2"):
    cfg = StageConfig(stage=stage, role=role, provider="claude", model_id=keys[0], account_route="acc3",
                      fallback_model_ids=tuple(keys[1:]), route_deadline_s=deadline)
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda s: cfg)
    return cfg


def _slow_failure(world, clock, seconds, exc):
    """acc3 fails after ``seconds`` of (fake) wall time."""
    def maybe_fail(where):
        if where == "acc3":
            clock.now += seconds
            raise exc
    world._maybe_fail = maybe_fail


# ── route deadline ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_no_deadline_tries_the_whole_route_as_before(world, monkeypatch, clock):
    _stage(monkeypatch, "sonnet-5-5", "gpt-6-luna", deadline=None)
    _slow_failure(world, clock, 500, RuntimeError("acc3 outage"))
    with pytest.raises(RuntimeError, match="All models in the route failed"):
        world.generate(stage="adhoc_p2")
    assert [c[2] for c in world.calls] == ["global.anthropic.claude-sonnet-5-5", "global.openai.gpt-6-luna"]
    assert client_mod._DEADLINE.get() is None


def test_a_passed_deadline_stops_the_route_before_the_next_model(world, monkeypatch, clock, rows):
    _stage(monkeypatch, "sonnet-5-5", "gpt-6-luna", deadline=30)
    _slow_failure(world, clock, 31, RuntimeError("acc3 outage"))
    with pytest.raises(RouteDeadlineExceeded, match="route deadline of 30 s .* before route position 1"):
        world.generate(stage="adhoc_p2")
    assert len(world.calls) == 1                                   # gpt-6-luna was never called
    call_log.wait_for_pending_writes()
    deadline_rows = [r for r in rows if r["error_class"] == "deadline"]
    assert len(deadline_rows) == 1
    row = deadline_rows[0]
    assert (row["status"], row["model"], row["cost_usd"]) == ("error", "gpt-6-luna", 0.0)
    assert client_mod._DEADLINE.get() is None                     # unbound after the request


def test_time_left_lets_the_next_model_run(world, monkeypatch, clock):
    _stage(monkeypatch, "sonnet-5-5", "gpt-6-luna", deadline=30)
    _slow_failure(world, clock, 5, RuntimeError("acc3 outage"))
    with pytest.raises(RuntimeError):
        world.generate(stage="adhoc_p2")
    assert len(world.calls) == 2


def test_a_deadline_is_not_an_account_level_refusal():
    exc = RouteDeadlineExceeded("route deadline of 5 s passed")
    assert classify(exc) == "deadline"
    assert attempt_outcome(exc) == ("error", "deadline")
    assert not isinstance(exc, LLMAccessDenied)


def test_the_deadline_is_the_openai_per_request_timeout(world, monkeypatch, clock):
    monkeypatch.setattr(client_mod, "openai_platform_allowed", lambda key: True)
    monkeypatch.setattr(client_mod, "openai_scope_allowed", lambda scopes, conn=None: True)
    req = LLMRequest(system_prompt="s", user_prompt="u")
    create = world.client._openai.chat.completions.create
    with pytest.raises(RuntimeError):
        world.client._call_openai(req, model="gpt-4.1")
    assert "timeout" not in create.call_args.kwargs                # no deadline: the request is unchanged
    token = client_mod._DEADLINE.set((clock.now + 12.5, 20))
    try:
        with pytest.raises(RuntimeError):
            world.client._call_openai(req, model="gpt-4.1")
    finally:
        client_mod._DEADLINE.reset(token)
    assert create.call_args.kwargs["timeout"] == 12.5


def test_the_deadline_is_the_anthropic_per_request_timeout(world, clock):
    conn = _anthropic(True)
    target = VendorDeployment(_dep("sonnet-5-5", "anthropic-direct", "claude-sonnet-5-5", 100), conn)
    req = LLMRequest(system_prompt="s", user_prompt="u")
    world.client._call_anthropic_messages(req, SONNET55, target)
    assert "timeout" not in world.anthropic_send.call_args.kwargs
    token = client_mod._DEADLINE.set((clock.now + 0.2, 20))
    try:
        world.client._call_anthropic_messages(req, SONNET55, target)
    finally:
        client_mod._DEADLINE.reset(token)
    assert world.anthropic_send.call_args.kwargs["timeout"] == client_mod.DEADLINE_MIN_TIMEOUT_S


def test_role_config_reads_the_deadline_column():
    from shared.llm_client import role_config
    row = {"stage": "t9_write", "role": "writer", "provider": "claude", "model_id": "sonnet", "account_route": "acc3",
           "fallback_model_ids": [], "shadow_model_id": None, "shadow_sample_pct": 0, "allowed_scopes": None,
           "route_deadline_s": 240}
    assert role_config._row_to_config(row).route_deadline_s == 240
    assert role_config._row_to_config({**row, "route_deadline_s": None}).route_deadline_s is None
    row.pop("route_deadline_s")
    assert role_config._row_to_config(row).route_deadline_s is None


# ── the legacy chain, the S2 vendor path and the catalog fallbacks inside the breaker ─────────────────────────────

@pytest.mark.real_breaker
def test_a_blocked_connection_stops_the_legacy_chain_without_a_call(world, monkeypatch):
    monkeypatch.setenv("LLM_LEGACY_ALIASES", "off")
    breaker.record_failure("acc3", "gpt-6-luna", ERROR_002)
    breaker.record_failure("acc3", "sonnet-5-5", ERROR_002)            # two models: the acc3 connection is open
    assert breaker.connection_open("acc3") == "access_denied"
    with pytest.raises(LLMAccessDenied) as info:
        world.generate(model_tier="haiku")
    assert world.calls == []
    assert isinstance(info.value.__cause__ or info.value.__context__, breaker.CircuitOpen) or any(
        isinstance(link, breaker.CircuitOpen) for link in client_mod._causes(info.value))


@pytest.mark.real_breaker
def test_a_legacy_failure_is_counted_on_the_model_it_called(world, monkeypatch):
    monkeypatch.setenv("LLM_LEGACY_ALIASES", "off")
    world.fail = {"acc3": ERROR_002}
    with pytest.raises(LLMAccessDenied):
        world.generate(model_tier="sonnet")                        # T1.5a sonnet, then T2.5a haiku, both refused
    counted = {(h.connection_key, h.model_key): h.last_error_class for h in breaker.snapshot()}
    assert counted == {("acc3", "sonnet"): "access_denied", ("acc3", "haiku"): "access_denied"}


def test_breaker_off_keeps_the_legacy_chain_calls(world, monkeypatch):
    monkeypatch.setenv("LLM_LEGACY_ALIASES", "off")
    world.reset()
    resp = world.generate(model_tier="haiku")
    assert [c[0] for c in world.calls] == ["invoke_model"] and resp.satellite_account == "acc3"


def test_the_legacy_chain_is_logged_as_deprecated_once_per_stage(world, monkeypatch):
    monkeypatch.setenv("LLM_LEGACY_ALIASES", "off")
    monkeypatch.setattr(client_mod, "_DEPRECATED_LOGGED", set())
    with capture_logs() as logs:
        world.generate(model_tier="haiku", stage="adhoc_dep")
        world.generate(model_tier="haiku", stage="adhoc_dep")
    used = [e for e in logs if e["event"] == "llm_deprecated_path_used"]
    assert len(used) == 1 and used[0]["path"] == "legacy_chain" and used[0]["deprecated_since"] == "2026-10-31"
    assert used[0]["remove_after"]


@pytest.mark.real_breaker
def test_the_s2_vendor_path_runs_inside_the_breaker(world, monkeypatch):
    monkeypatch.setenv("LLM_DEPLOYMENT_ROUTING", "off")
    world.conns["anthropic-direct"] = _anthropic(True)
    world.deps = [_dep("sonnet-5-5", "anthropic-direct", "claude-sonnet-5-5", 5, origin="seed")]
    world.reset()
    resp = world.generate(model_tier="sonnet-5-5")
    assert resp.content == "from anthropic"
    assert ("anthropic-direct", "sonnet-5-5") in {(h.connection_key, h.model_key) for h in breaker.snapshot()}
    world.fail = {"anthropic-direct": RuntimeError("credit balance is too low")}
    world.calls.clear()
    with pytest.raises(LLMAccessDenied):
        world.generate(model_tier="sonnet-5-5")                    # refused once: quota_exhausted opens it
    with pytest.raises(LLMAccessDenied, match="circuit open on anthropic-direct"):
        world.generate(model_tier="sonnet-5-5")
    assert len(world.calls) == 1


@pytest.mark.real_breaker
def test_the_catalog_converse_fallback_runs_inside_the_breaker(world, monkeypatch):
    monkeypatch.setenv("LLM_DEPLOYMENT_ROUTING", "off")
    world.fail = {"acc3": ERROR_002}
    with pytest.raises(LLMAccessDenied):
        world.generate(model_tier="gpt-6-luna")
    with pytest.raises(LLMAccessDenied, match="circuit open on acc3"):
        world.generate(model_tier="gpt-6-luna")
    assert len(world.calls) == 1


# ── the truncated refusal ──────────────────────────────────────────────────────────────────────────────────────────

def test_openais_max_tokens_400_is_truncated_and_never_counted():
    assert classify(OPENAI_LENGTH_400) == TRUNCATED
    assert breaker.failure_class(OPENAI_LENGTH_400) == breaker.REQUEST
    assert attempt_outcome(OPENAI_LENGTH_400) == ("truncated", "truncated")
    row = call_log.gateway_failure_row(stage="adhoc_s262_drill", role="judge", model="gpt-6-luna-openai",
                                       status="truncated", error_class="truncated", error=str(OPENAI_LENGTH_400))
    assert (row["status"], row["error_class"], row["cost_usd"]) == ("truncated", "truncated", 0.0)


@pytest.mark.real_breaker
def test_a_truncated_refusal_leaves_the_circuit_closed():
    for _ in range(10):
        breaker.record_failure("openai-direct", "gpt-6-luna-openai", OPENAI_LENGTH_400)
    assert breaker.snapshot() == []


# ── the role of an adhoc stage ─────────────────────────────────────────────────────────────────────────────────────

def test_an_adhoc_stage_logs_the_role_the_request_states(world, monkeypatch, rows):
    world.fail = {"acc3": RuntimeError("acc3 outage")}
    with pytest.raises(RuntimeError):
        world.generate(model_tier="gpt-6-luna", stage="adhoc_s262_drill", role="judge")
    call_log.wait_for_pending_writes()
    assert rows and {r["role"] for r in rows} == {"judge"}


def test_a_stage_row_role_wins_over_the_request():
    cfg = StageConfig(stage="t10_judge", role="judge", provider="openai", model_id="gpt-6-luna", account_route="acc3")
    assert client_mod._role_of(LLMRequest(system_prompt="s", user_prompt="u", role="writer"), cfg) == "judge"
    unknown = StageConfig(stage="unknown", role="writer", provider="claude", model_id="haiku", account_route="acc3")
    assert client_mod._role_of(LLMRequest(system_prompt="s", user_prompt="u", role="validate"), unknown) == "validate"
    assert client_mod._role_of(LLMRequest(system_prompt="s", user_prompt="u", role="boss"), unknown) == "writer"
    assert client_mod._role_of(LLMRequest(system_prompt="s", user_prompt="u"), None) == "writer"


# ── prompt version on the reply ────────────────────────────────────────────────────────────────────────────────────

def test_a_registered_prompt_version_rides_on_the_reply(world, monkeypatch):
    monkeypatch.setattr(prompt_registry, "version_for", lambda stage: "v7" if stage == "adhoc_pv" else None)
    resp = world.generate(model_tier="gpt-6-luna", stage="adhoc_pv")
    assert resp.prompt_version == "v7" and call_log.billed_fields(resp)["prompt_version"] == "v7"
    assert resp.prompt_hash == client_mod.prompt_hash(LLMRequest(system_prompt="s", user_prompt="other tour"))
    assert world.generate(model_tier="gpt-6-luna", stage="adhoc_other").prompt_version is None


# ── access_state ───────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.real_breaker
def test_access_state_follows_the_connection_circuit_and_the_next_answer(monkeypatch):
    written = []
    monkeypatch.setattr(breaker, "set_access_state", lambda key, state: written.append((key, state)))
    breaker.record_failure("acc3", "gpt-6-luna", ERROR_002)
    assert written == []                                           # one model refused: not account-wide
    breaker.record_failure("acc3", "sonnet-5-5", ERROR_002)
    breaker.record_failure("acc3", "haiku", ERROR_002)
    assert written == [("acc3", "access_denied")]                  # once, not on every failure
    breaker.record_success("acc3", "cohere-embed-v4-acc3")
    assert written[-1] == ("acc3", "ok")


@pytest.mark.real_breaker
def test_a_missing_key_marks_the_connection_unconfigured(monkeypatch):
    from shared.llm_client.connections import ConnectionCredentialError
    written = []
    monkeypatch.setattr(breaker, "set_access_state", lambda key, state: written.append((key, state)))
    breaker.record_failure("anthropic-direct", "sonnet-5-5", ConnectionCredentialError("no secret yet"))
    assert written == [("anthropic-direct", "unconfigured")]


@pytest.mark.real_breaker
def test_an_access_state_write_error_never_reaches_the_call(monkeypatch):
    def boom(key, state):
        raise RuntimeError("column access_state does not exist")
    monkeypatch.setattr(breaker, "set_access_state", boom)
    breaker.record_success("acc3", "gpt-6-luna")                   # no exception
    assert breaker._access_written == {}                            # tried again next time


@pytest.mark.real_breaker
def test_a_failing_access_state_write_is_tried_once_per_back_off_window(monkeypatch):
    # Review S262: record_success runs on every answered call; a refused UPDATE must not open a DB connection each time.
    calls = []

    def refused(key, state):
        calls.append((key, state))
        raise RuntimeError("permission denied for table llm_provider_connection")
    monkeypatch.setattr(breaker, "set_access_state", refused)
    for _ in range(50):
        breaker.record_success("acc3", "gpt-6-luna")
    assert calls == [("acc3", "ok")]                                # one try, then the back-off
    wait = breaker._access_failed_until["acc3"] - breaker.time.monotonic()
    assert breaker.DB_RETRY_S - 5 < wait <= breaker.DB_RETRY_S
    breaker.note_access_state("acc2", "ok")                         # the back-off is per connection
    assert calls[-1] == ("acc2", "ok")
    breaker._access_failed_until["acc3"] = breaker.time.monotonic() - 1   # the window has passed
    monkeypatch.setattr(breaker, "set_access_state", lambda key, state: calls.append((key, state)))
    breaker.record_success("acc3", "gpt-6-luna")
    breaker.record_success("acc3", "gpt-6-luna")
    assert calls[-1] == ("acc3", "ok") and calls.count(("acc3", "ok")) == 2
    assert breaker._access_written["acc3"] == "ok" and "acc3" not in breaker._access_failed_until


@pytest.mark.real_breaker
def test_a_missing_access_state_column_waits_the_long_back_off(monkeypatch):
    class UndefinedColumnError(Exception):
        pass

    def missing(key, state):
        raise UndefinedColumnError('column "access_state" does not exist')
    monkeypatch.setattr(breaker, "set_access_state", missing)
    breaker.note_access_state("acc3", "ok")
    wait = breaker._access_failed_until["acc3"] - breaker.time.monotonic()
    assert breaker.DB_MISSING_RETRY_S - 5 < wait <= breaker.DB_MISSING_RETRY_S


@pytest.mark.real_breaker
def test_no_access_state_write_while_the_breaker_db_back_off_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(breaker, "set_access_state", lambda key, state: calls.append((key, state)))
    monkeypatch.setattr(breaker, "_db_down_until", breaker.time.monotonic() + 30)
    breaker.note_access_state("acc3", "ok")
    assert calls == [] and breaker._access_written == {}            # read only: the back-off is not extended
    monkeypatch.setattr(breaker, "_db_down_until", 0.0)
    breaker.note_access_state("acc3", "ok")
    assert calls == [("acc3", "ok")]


@pytest.mark.real_breaker
def test_one_access_state_write_per_connection_at_a_time(monkeypatch):
    import threading
    started, release, calls = threading.Event(), threading.Event(), []

    def slow(key, state):
        calls.append((key, state))
        started.set()
        release.wait(5)
    monkeypatch.setattr(breaker, "set_access_state", slow)
    t = threading.Thread(target=breaker.note_access_state, args=("acc3", "ok"))
    t.start()
    assert started.wait(5)
    for _ in range(20):
        breaker.note_access_state("acc3", "ok")                     # another call thread: skipped, no second connect
    release.set()
    t.join(5)
    assert calls == [("acc3", "ok")]


def test_breaker_off_writes_no_access_state(monkeypatch):
    written = []
    monkeypatch.setattr(breaker, "set_access_state", lambda key, state: written.append((key, state)))
    breaker.note_access_state("acc3", "ok")
    assert written == []


# ── failure domain ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_stored_failure_domain_wins_over_the_derivation():
    conn = {"connection_key": "acc2", "kind": "bedrock", "data_scope": "aws_only", "account_id": None,
            "account_id_env": None}
    assert invariant.failure_domain(conn) == "bedrock:acc2:aws_only"
    assert invariant.failure_domain({**conn, "failure_domain": "trueidc-payer"}) == "trueidc-payer"
    assert invariant.failure_domain({**conn, "failure_domain": None}) == "bedrock:acc2:aws_only"


def test_two_connections_with_one_stored_domain_are_one_domain():
    routes = [{"stage": "t9_write", "role": "writer", "model_id": "m", "fallback_model_ids": ["n"],
               "allowed_scopes": None, "is_active": True}]
    deps = [{"model_key": "m", "connection_key": "a", "priority": 10, "enabled": True},
            {"model_key": "n", "connection_key": "b", "priority": 10, "enabled": True}]
    conns = [{"connection_key": k, "kind": "bedrock", "enabled": True, "data_scope": "aws_only", "account_id": None,
              "account_id_env": None, "failure_domain": "same-payer"} for k in ("a", "b")]
    assert invariant.check(routes, [], deps, conns)["ok"] is False
    conns[1]["failure_domain"] = "other-payer"
    assert invariant.check(routes, [], deps, conns)["ok"] is True


def test_the_invariant_read_falls_back_before_migration_322():
    import asyncpg

    async def fetch(sql):
        if "failure_domain" in sql:
            raise asyncpg.exceptions.UndefinedColumnError("column failure_domain does not exist")
        return []
    conn = MagicMock(fetch=fetch)
    out = asyncio.run(invariant.read(conn))
    assert out["ready"] is True and out["stages_checked"] == 0


# ── the probe's stamp and the connection test's output budget ──────────────────────────────────────────────────────

def test_the_probe_stamps_every_connection_it_reached():
    from services.jobs import llm_probe_job as probe
    db = MagicMock(execute=AsyncMock())
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=db)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock(acquire=MagicMock(return_value=acquire))

    async def test(pool, key, *, model_key, actor):
        if key == "broken":
            raise RuntimeError("cannot start")
        return {"ok": key == "acc2", "error_class": None if key == "acc2" else "access_denied"}
    plan = {"probes": [{"connection_key": "acc2", "model_key": "nova-lite"},
                       {"connection_key": "acc3", "model_key": "gpt-6-luna"},
                       {"connection_key": "broken", "model_key": "x"}]}
    results = asyncio.run(probe.run_probes(pool, plan, test=test))
    assert [r["ok"] for r in results] == [True, False, False]
    sql, keys = db.execute.call_args.args
    assert "SET last_probe_at = now()" in sql and keys == ["acc2", "acc3"]


def test_the_connection_test_takes_an_output_budget_up_to_512(monkeypatch):
    from api.routers.admin_llm_ops import LlmConnectionTest
    from pydantic import ValidationError
    assert LlmConnectionTest(model_key="kimi-k3", max_tokens=512).max_tokens == 512
    with pytest.raises(ValidationError):
        LlmConnectionTest(max_tokens=513)
    assert LlmConnectionTest().max_tokens is None


def test_the_connection_test_sends_the_asked_budget(monkeypatch):
    from api.services import llm_connections as svc
    from shared.llm_client.catalog import CatalogModel
    from shared.llm_client.connections import BUILTIN_CONNECTIONS
    from shared.llm_client.deployments import ModelDeployment

    kimi = CatalogModel(model_key="kimi-k3", label="Kimi K3", vendor="moonshot", provider="bedrock",
                        api_style="converse", bedrock_profile_ids={"acc2": "us.moonshotai.kimi-k3"},
                        callable_via=("llm_client",), supports_temperature=False, price_in_per_mtok=3.3,
                        price_out_per_mtok=16.5, enabled=True)
    dep = ModelDeployment("kimi-k3", "acc2", "us.moonshotai.kimi-k3", 20, True, "bedrock_profile")
    db = MagicMock(fetchrow=AsyncMock(return_value={"connection_key": "acc2"}), execute=AsyncMock())
    acquire = MagicMock()
    acquire.__aenter__ = AsyncMock(return_value=db)
    acquire.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock(acquire=MagicMock(return_value=acquire))
    monkeypatch.setattr(svc.conn_registry, "_row_to_connection", lambda row: BUILTIN_CONNECTIONS["acc2"])
    monkeypatch.setattr(svc.dep_registry, "fetch_deployments", AsyncMock(return_value=[dep]))
    monkeypatch.setattr(svc, "_testable", lambda conn, deps: [(dep, kimi)])
    monkeypatch.setattr(svc, "record_call_with_pool", AsyncMock(), raising=False)
    seen = {}

    class Client:
        def probe(self, request, model, target):
            seen["max_tokens"] = request.max_tokens
            raise RuntimeError("fake outage")
    out = asyncio.run(svc.test_connection(pool, "acc2", model_key="kimi-k3", actor="t", client=Client(),
                                          max_tokens=512))
    assert seen["max_tokens"] == 512 and out["ok"] is False and out["cost_usd"] == 0.0
    asyncio.run(svc.test_connection(pool, "acc2", model_key="kimi-k3", actor="t", client=Client()))
    assert seen["max_tokens"] == svc.TEST_MAX_TOKENS
    asyncio.run(svc.test_connection(pool, "acc2", model_key="kimi-k3", actor="t", client=Client(), max_tokens=9999))
    assert seen["max_tokens"] == svc.TEST_MAX_TOKENS_LIMIT


def test_llm_client_class_is_unchanged_for_callers():
    assert LLMClient.generate is not None and hasattr(client_mod, "check_deadline")
