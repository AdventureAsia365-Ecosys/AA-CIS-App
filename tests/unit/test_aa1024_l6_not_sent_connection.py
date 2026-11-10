"""S276 lane l6 (AA-1024 follow-up) — a refused ledger row of a deployment the gateway skipped names its connection.

Live symptom (10/11/2026): the judge stage of the T7 debate wrote, per hour, its answered rows on the OpenAI API plus
~10 % extra rows ``status refused / error_class access_denied`` on the shadow model with ``connection_key NULL``. The
shadow model's only deployment (acc3) sat behind an open access_denied circuit, so its plan was empty and
``breaker.circuit_error`` was raised before any attempt; the gateway's net row (``_logged_failure_net``) had no
connection. Such a row never matches the expected-refusal rule (keyed by connection) and counted as an error. The
same NULL row came from a deferred route rung tried last or logged after the deadline, a single-model request and a
hard-budget skip.

Now (``client.not_sent_facts``): the row carries the first skipped connection, that deployment's wire id and provider,
every skipped connection when there are several, and ``quality_signal.refusal = circuit_open`` for a circuit skip (the
mark the root gateway status tool counts apart from errors). Still one row per request that was refused (ledger v2);
no provider call.

The S3 fake world (tests/unit/test_aa1002_s3_deployment_routing.py) with the in-process breaker; no AWS, no vendor,
no DB, no paid call. Model and connection names other than the fixture's are invented.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from shared.llm_client import breaker, call_log, catalog, connection_budget, vendor_route
from shared.llm_client import client as client_mod
from shared.llm_client import shadow as shadow_mod
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.errors import LLMAccessDenied
from shared.llm_client.models import LLMRequest, LLMResponse
from shared.llm_client.role_config import StageConfig
from tests.unit.test_aa1002_s3_deployment_routing import World, _dep, _org

T0 = datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)
ERROR_002 = RuntimeError("AccessDeniedException: Error 002: Access to Bedrock models is not allowed for this account")
LANTERN = CatalogModel(model_key="lantern-mini", label="Lantern Mini", vendor="openai", provider="bedrock",
                       api_style="converse", bedrock_profile_ids={"bedrock-llm-org": "us.example.lantern-mini"},
                       callable_via=("llm_client",), supports_temperature=False, price_in_per_mtok=0.1,
                       price_out_per_mtok=0.4, enabled=True)
STAGE = "adhoc_l6_judge"


@pytest.fixture
def rows(monkeypatch):
    seen: list = []

    async def record_calls(rows):
        seen.extend(rows)
    monkeypatch.setattr(call_log, "record_calls", record_calls)
    yield seen


@pytest.fixture
def clock(monkeypatch):
    now = {"t": T0}
    monkeypatch.setattr(breaker, "_now", lambda: now["t"])
    return now


@pytest.fixture
def gw(monkeypatch, clock):
    """gpt-6-luna and sonnet-5-5 on acc3 (the fixture's seed), lantern-mini on a second Bedrock connection."""
    hook = vendor_route.circuit_open
    w = World(monkeypatch)
    w.conns["bedrock-llm-org"] = _org()
    w.deps.append(_dep("lantern-mini", "bedrock-llm-org", "us.example.lantern-mini", 10))
    catalog._models["lantern-mini"] = LANTERN
    w.reset()
    yield w
    w.reset()
    catalog.invalidate()
    vendor_route.circuit_open = hook


def _stage(monkeypatch, *keys, shadow=None):
    cfg = StageConfig(stage=STAGE, role="judge", provider="openai", model_id=keys[0], account_route="acc3",
                      fallback_model_ids=tuple(keys[1:]), shadow_model_id=shadow,
                      shadow_sample_pct=100 if shadow else 0)
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda stage: cfg)
    return cfg


def _refused_rows(rows):
    return [r for r in rows if r["status"] == "refused"]


# ── the live case: the shadow model behind an open access_denied circuit ───────────────────────────────────────────

@pytest.mark.real_breaker
def test_the_shadow_behind_an_open_refusal_circuit_names_its_connection(gw, monkeypatch, rows):
    _stage(monkeypatch, "lantern-mini", shadow="gpt-6-luna")
    breaker.record_failure("acc3", "gpt-6-luna", ERROR_002)               # the circuit opens (access_denied)
    shadows: list = []
    monkeypatch.setattr(shadow_mod, "record_shadow_sync", lambda **kw: shadows.append(kw))
    # the shadow runs in a thread in production; run it in line here (same method, same scopes)
    monkeypatch.setattr(gw.client, "_start_shadow",
                        lambda request, cfg, primary, acct: gw.client._run_shadow(request.model_copy(), cfg,
                                                                                  primary, acct))
    resp = gw.generate(stage=STAGE)
    call_log.wait_for_pending_writes()
    assert resp.content == "from bedrock-llm-org"
    assert [c[1] for c in gw.calls] == ["bedrock-llm-org"]                # nothing sent to acc3
    (row,) = _refused_rows(rows)                                          # one row for the shadow request
    assert (row["model"], row["status"], row["error_class"], row["is_shadow"]) == \
        ("gpt-6-luna", "refused", "access_denied", True)
    assert row["connection_key"] == "acc3"                                # was NULL before S276
    assert row["deployment_wire_id"] == "global.openai.gpt-6-luna"
    assert row["provider"] == "bedrock-satellite" and row["account"] == "acc3"
    assert row["quality_signal"]["refusal"] == "circuit_open"
    assert row["quality_signal"]["shadow"] is True
    assert "skipped_connections" not in row["quality_signal"]
    assert (row["cost_usd"], row["latency_ms"], row["attempt_no"]) == (0.0, 0, None)   # not an attempt
    assert shadows and "circuit open" in shadows[0]["error"]


@pytest.mark.real_breaker
def test_a_single_model_request_behind_an_open_circuit_names_its_connection(gw, rows):
    breaker.record_failure("acc3", "sonnet-5-5", ERROR_002)
    with pytest.raises(LLMAccessDenied):
        gw.generate(model_tier="sonnet-5-5")
    call_log.wait_for_pending_writes()
    (row,) = rows
    assert (row["model"], row["status"], row["connection_key"], row["deployment_wire_id"]) == \
        ("sonnet-5-5", "refused", "acc3", "global.anthropic.claude-sonnet-5-5")
    assert row["quality_signal"]["refusal"] == "circuit_open"
    assert gw.calls == []


@pytest.mark.real_breaker
def test_a_budget_skip_names_its_connection_without_the_circuit_mark(gw, monkeypatch, rows):
    over = SimpleNamespace(month_spent_usd=12.0, monthly_budget_usd=10.0, stops=True)
    monkeypatch.setattr(connection_budget, "stops", lambda conn: over if conn.connection_key == "acc3" else None)
    _stage(monkeypatch, "gpt-6-luna", "lantern-mini")
    resp = gw.generate(stage=STAGE)
    call_log.wait_for_pending_writes()
    assert resp.route_position == 1
    (row,) = rows
    assert (row["model"], row["status"], row["error_class"], row["connection_key"]) == \
        ("gpt-6-luna", "refused", "budget", "acc3")
    assert "refusal" not in row["quality_signal"]


# ── not_sent_facts ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_several_skipped_connections_keep_the_first_and_list_them_all(gw):
    exc = breaker.circuit_error("sonnet-5-5", [("acc3", "access_denied"), ("anthropic-direct", "credential")])
    assert exc.skips == (("acc3", "access_denied"), ("anthropic-direct", "credential"))
    conn, wire, provider, extra = client_mod.not_sent_facts(exc, "sonnet-5-5")
    assert (conn, wire, provider) == ("acc3", "global.anthropic.claude-sonnet-5-5", "bedrock-satellite")
    assert extra == {"refusal": "circuit_open", "skipped_connections": ["acc3", "anthropic-direct"]}


def test_a_circuit_refusal_inside_an_attempt_keeps_its_connection_and_gets_the_mark(gw):
    exc = breaker.circuit_error("gpt-6-luna", [("acc3", "access_denied")])      # breaker.guarded's own error
    assert client_mod.not_sent_facts(exc, "gpt-6-luna", "acc3") == (None, None, None, {"refusal": "circuit_open"})


def test_a_connection_not_in_the_registry_still_names_it(gw):
    exc = breaker.circuit_error("model-unlisted", [("invented-conn", "access_denied")])
    assert client_mod.not_sent_facts(exc, "model-unlisted") == \
        ("invented-conn", None, None, {"refusal": "circuit_open"})


@pytest.mark.parametrize("exc", [
    RuntimeError("Model 'x' is not in shared.llm_model_catalog"),
    breaker.CircuitOpen("no skips known", "access_denied"),
    connection_budget.exceeded("no keys"),
])
def test_other_failures_add_nothing_but_the_circuit_mark(gw, exc):
    conn, wire, provider, extra = client_mod.not_sent_facts(exc, "gpt-6-luna")
    assert (conn, wire, provider) == (None, None, None)
    assert extra == ({"refusal": "circuit_open"} if isinstance(exc, breaker.CircuitOpen) else {})


def test_not_sent_facts_never_raises(monkeypatch):
    from shared.llm_client import deployments
    monkeypatch.setattr(deployments, "deployments_for_sync", lambda *a, **k: 1 / 0)
    exc = breaker.circuit_error("gpt-6-luna", [("acc3", "access_denied")])
    assert client_mod.not_sent_facts(exc, "gpt-6-luna") == (None, None, None, {})


def test_the_row_of_an_unrelated_failure_keeps_a_null_connection(gw, rows):
    request = LLMRequest(system_prompt="s", user_prompt="u", stage=STAGE)
    client_mod.log_gateway_failed_attempt(request, RuntimeError("not in catalog"), model="model-gone", latency_ms=0)
    call_log.wait_for_pending_writes()
    (row,) = rows
    assert row["connection_key"] is None and "refusal" not in row["quality_signal"]


def test_an_answered_reply_is_untouched(gw):
    resp = LLMResponse(content="x", model_used="m", provider="bedrock")
    assert "refusal" not in call_log.billed_fields(resp)
