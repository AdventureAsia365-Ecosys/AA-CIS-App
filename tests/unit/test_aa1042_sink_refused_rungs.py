"""AA-1042 (S270, lane sink-open-refused-rungs) — the gateway defers route rungs behind an open refusal circuit.

A route rung whose every usable deployment sits behind a circuit opened by a refusal (access_denied, quota_exhausted,
credential; open, or half-open with the probe lease held by another call) makes no provider call. Before S270 it still
wrote a 0 USD refused row per request; now the route defers it: no row in place, the answered row lists it in
``quality_signal.route_skips`` and keeps its own route position. When no live rung answers, the deferred rungs run
last in route order, exactly as before (same rows, same final error). Kept as before: a half-open circuit with a free
probe lease (its one probe call), the route test's forced refusal, budget / role / throttled skips, the breaker off.

The S3 fake world (tests/unit/test_aa1002_s3_deployment_routing.py) with the in-process breaker; no AWS, no vendor,
no DB, no paid call. Model and connection names other than the fixture's are invented.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services import llm_route_trace as trace_svc
from services.content_generation.brand_fit import BrandFitResult
from shared.llm_client import breaker, call_log, catalog, connection_budget, route_watch, vendor_route
from shared.llm_client import client as client_mod
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.errors import LLMAccessDenied, RouteDeadlineExceeded
from shared.llm_client.models import LLMResponse
from shared.llm_client.role_config import StageConfig
from shared.llm_client.vendor_route import DeploymentPlan, sunk_skips
from tests.unit.test_aa1002_s3_deployment_routing import World, _dep, _org

T0 = datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc)
ERROR_002 = RuntimeError("AccessDeniedException: Error 002: Access to Bedrock models is not allowed for this account")
LANTERN = CatalogModel(model_key="lantern-mini", label="Lantern Mini", vendor="openai", provider="bedrock",
                       api_style="converse", bedrock_profile_ids={"bedrock-llm-org": "us.example.lantern-mini"},
                       callable_via=("llm_client",), supports_temperature=False, price_in_per_mtok=0.1,
                       price_out_per_mtok=0.4, enabled=True)
ROUTE = ("sonnet-5-5", "gpt-6-luna", "lantern-mini")
STAGE = "adhoc_s3"


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
def gw(monkeypatch):
    """The fake world: sonnet-5-5 and gpt-6-luna on acc3, lantern-mini on a second Bedrock connection."""
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


def _route(monkeypatch, *keys, role="judge", deadline=None):
    cfg = StageConfig(stage=STAGE, role=role, provider="claude", model_id=keys[0], account_route="acc3",
                      fallback_model_ids=tuple(keys[1:]), route_deadline_s=deadline)
    monkeypatch.setattr("shared.llm_client.client.get_stage_config_sync", lambda stage: cfg)


def _refuse(*models, conn="acc3"):
    """Open the access_denied circuit of each model on ``conn`` (one refused call each, in process)."""
    for m in models:
        breaker.record_failure(conn, m, ERROR_002)


def _rows_view(rows):
    return sorted((r["quality_signal"].get("position"), r["model"], r["status"], r["error_class"],
                   r.get("connection_key")) for r in rows)


def _generate(gw):
    out = gw.generate(stage=STAGE)
    call_log.wait_for_pending_writes()
    return out


# ── (1) two refused rungs, then a live one ──────────────────────────────────────────────────────────────────────────

@pytest.mark.real_breaker
def test_two_open_refusal_rungs_are_deferred_and_listed_on_the_answer(gw, monkeypatch, rows):
    _route(monkeypatch, *ROUTE)
    _refuse("sonnet-5-5", "gpt-6-luna")
    resp = _generate(gw)
    assert resp.content == "from bedrock-llm-org"
    assert [c[1] for c in gw.calls] == ["bedrock-llm-org"]                # no provider call for the refused rungs
    assert rows == []                                                    # and no refused row of their own
    assert (resp.route_position, resp.attempt_no, resp.fallback_used) == (2, 1, True)
    skipped = resp.route_skips
    assert [(s["model"], s["position"]) for s in skipped] == [("sonnet-5-5", 0), ("gpt-6-luna", 1)]
    assert {s["connection"] for s in skipped} == {"acc3"}
    assert all(s["reason"] == "circuit_open:access_denied" for s in skipped)
    # the caller's answered row carries them; the fallback rule still reads position 2 as a fallback
    f = call_log.billed_fields(resp)
    assert f["route_skips"] == skipped and f["route_position"] == 2
    assert route_watch.is_fallback(resp.attempt_no, f["route_position"], resp.fallback_used) is True


@pytest.mark.real_breaker
def test_a_writer_answered_after_deferred_rungs_still_notes_the_fallback_write(gw, monkeypatch, rows):
    noted = MagicMock()
    monkeypatch.setattr(client_mod, "_note_fallback_write", noted)
    _route(monkeypatch, *ROUTE, role="writer")
    _refuse("sonnet-5-5", "gpt-6-luna")
    _generate(gw)
    (call,) = noted.call_args_list
    assert call.args[1:] == ("writer", "lantern-mini", 2)


@pytest.mark.real_breaker
def test_a_held_probe_lease_sinks_the_rung_too(gw, monkeypatch, rows, clock):
    _route(monkeypatch, *ROUTE)
    _refuse("sonnet-5-5")
    clock["t"] = T0 + timedelta(seconds=301)                             # half-open ...
    assert breaker.admit("acc3", "sonnet-5-5") is True                   # ... and another task holds the probe
    resp = _generate(gw)
    assert rows == [] and resp.route_position == 1                       # gpt-6-luna on acc3 answered
    assert [(s["model"], s["position"]) for s in resp.route_skips] == [("sonnet-5-5", 0)]


# ── (2) nothing live answers: the deferred rungs run last, exactly as before ────────────────────────────────────────

def _run_both_ways(gw, monkeypatch, rows, fail):
    """(rows, error) of one request with the sink on, then with it switched off (the pre-S270 route)."""
    out = []
    for sink in (True, False):
        breaker.reset()
        _refuse("sonnet-5-5", "gpt-6-luna")
        gw.fail = {"bedrock-llm-org": fail}
        gw.calls.clear()
        rows.clear()
        if not sink:
            monkeypatch.setattr(client_mod, "sunk_skips", lambda plan: None)
        with pytest.raises(Exception) as err:
            gw.generate(stage=STAGE)
        call_log.wait_for_pending_writes()
        out.append((_rows_view(rows), type(err.value), str(err.value), list(gw.calls)))
    return out


@pytest.mark.real_breaker
def test_all_rungs_down_tries_the_deferred_ones_last_with_the_same_rows_and_error(gw, monkeypatch, rows):
    _route(monkeypatch, *ROUTE)
    (now_rows, now_type, now_msg, now_calls), (old_rows, old_type, old_msg, old_calls) = \
        _run_both_ways(gw, monkeypatch, rows, RuntimeError("org outage"))
    assert now_rows == old_rows
    # S276 (lane l6): the not-called rungs name the connection whose circuit left them out (was NULL)
    assert now_rows == [(0, "sonnet-5-5", "refused", "access_denied", "acc3"),
                        (1, "gpt-6-luna", "refused", "access_denied", "acc3"),
                        (2, "lantern-mini", "error", "other", "bedrock-llm-org")]
    assert now_type is old_type is RuntimeError and now_msg == old_msg        # the route order in the message
    assert now_msg.index("sonnet-5-5") < now_msg.index("gpt-6-luna") < now_msg.index("lantern-mini")
    assert now_calls == old_calls == [("converse", "bedrock-llm-org", "us.example.lantern-mini")]


@pytest.mark.real_breaker
def test_all_rungs_refused_at_account_level_still_ends_in_llm_access_denied(gw, monkeypatch, rows):
    _route(monkeypatch, *ROUTE)
    (now_rows, now_type, now_msg, _), (old_rows, old_type, old_msg, _) = \
        _run_both_ways(gw, monkeypatch, rows, ERROR_002)
    assert now_type is old_type is LLMAccessDenied and now_msg == old_msg
    assert now_rows == old_rows and len(now_rows) == 3


@pytest.mark.real_breaker
def test_a_deferred_rung_whose_circuit_turned_half_open_gets_its_probe_last(gw, monkeypatch, rows, clock):
    """The deferred rungs are re-planned when they run last: a circuit whose open period ended meanwhile gets its one
    probe call (here it answers), exactly as the rung would have been called in place before S270."""
    _route(monkeypatch, *ROUTE)
    _refuse("sonnet-5-5", "gpt-6-luna")
    real = gw._maybe_fail

    def org_outage_then_half_open(where):
        if where == "bedrock-llm-org":
            clock["t"] = T0 + timedelta(seconds=301)        # the acc3 circuits' open period ends meanwhile
            raise RuntimeError("org outage")
        real(where)
    monkeypatch.setattr(gw, "_maybe_fail", org_outage_then_half_open)
    resp = _generate(gw)
    assert resp.content == "from acc3" and resp.route_position == 0 and resp.route_skips is None
    assert [c[1] for c in gw.calls] == ["bedrock-llm-org", "acc3"]
    assert [(r["model"], r["status"]) for r in rows] == [("lantern-mini", "error")]


@pytest.mark.real_breaker
def test_a_route_deadline_writes_the_deferred_rows_before_it_stops(gw, monkeypatch, rows):
    _route(monkeypatch, *ROUTE)
    _refuse("sonnet-5-5", "gpt-6-luna")

    def passed(request, before):
        raise RouteDeadlineExceeded(f"route deadline passed before {before}")
    monkeypatch.setattr(client_mod, "check_deadline", passed)
    with pytest.raises(RouteDeadlineExceeded):
        gw.generate(stage=STAGE)
    call_log.wait_for_pending_writes()
    assert gw.calls == []
    refused = [r for r in rows if r["status"] == "refused"]
    assert _rows_view(refused) == [(0, "sonnet-5-5", "refused", "access_denied", "acc3"),
                                   (1, "gpt-6-luna", "refused", "access_denied", "acc3")]
    assert all(r["quality_signal"]["refusal"] == "circuit_open" for r in refused)      # S276: nothing was sent
    # the deadline row names the rung not started (its position stays the scope's last one, as before S270)
    assert [(r["model"], r["status"], r["error_class"]) for r in rows if r["status"] != "refused"] == \
        [("lantern-mini", "error", "deadline")]


@pytest.mark.real_breaker
def test_after_the_deadline_the_deferred_rungs_are_logged_not_called(gw, monkeypatch, rows):
    _route(monkeypatch, *ROUTE)
    _refuse("sonnet-5-5", "gpt-6-luna")
    gw.fail = {"bedrock-llm-org": RuntimeError("org outage")}
    monkeypatch.setattr(client_mod, "deadline_remaining", lambda: -1.0)
    key_logged = MagicMock(wraps=gw.client._call_key_logged)
    monkeypatch.setattr(gw.client, "_call_key_logged", key_logged)
    with pytest.raises(RuntimeError, match="All models in the route failed"):
        gw.generate(stage=STAGE)
    assert [c.args[1] for c in key_logged.call_args_list] == ["lantern-mini"]     # the deferred ones: not called
    call_log.wait_for_pending_writes()
    assert [c[1] for c in gw.calls] == ["bedrock-llm-org"]
    assert [r[:3] for r in _rows_view(rows)] == [(0, "sonnet-5-5", "refused"), (1, "gpt-6-luna", "refused"),
                                                 (2, "lantern-mini", "error")]


# ── (3) a half-open circuit with a free probe lease is called in place ───────────────────────────────────────────────

@pytest.mark.real_breaker
def test_a_half_open_rung_with_a_free_lease_makes_its_probe_in_place(gw, monkeypatch, rows, clock):
    _route(monkeypatch, *ROUTE)
    _refuse("sonnet-5-5")
    clock["t"] = T0 + timedelta(seconds=301)
    assert not breaker.is_open("acc3", "sonnet-5-5")                     # half-open, nobody probing
    resp = _generate(gw)
    assert resp.content == "from acc3" and [c[2] for c in gw.calls] == ["global.anthropic.claude-sonnet-5-5"]
    assert (resp.route_position, resp.attempt_no, resp.fallback_used, resp.route_skips) == (0, 1, False, None)
    assert route_watch.is_fallback(resp.attempt_no, resp.route_position, resp.fallback_used) is False   # PRIMARY
    (h,) = [h for h in breaker.snapshot() if h.model_key == "sonnet-5-5"]
    assert breaker.state_of(h, clock["t"]) == breaker.CLOSED              # the probe closed the circuit
    assert rows == []


# ── (4) the route test's forced refusal keeps its row ───────────────────────────────────────────────────────────────

@pytest.mark.real_breaker
def test_the_forced_primary_refusal_still_logs_its_position_0_row(gw, monkeypatch, rows):
    _route(monkeypatch, *ROUTE)
    _refuse("sonnet-5-5", "gpt-6-luna")
    with client_mod.forced_primary_refusal(STAGE):
        resp = _generate(gw)
    assert resp.route_position == 2
    (row,) = rows
    assert (row["model"], row["status"], row["error_class"], row["quality_signal"]["position"]) == \
        ("sonnet-5-5", "refused", "access_denied", 0)
    assert "route test" in row["quality_signal"]["error"]
    assert [s["model"] for s in resp.route_skips] == ["gpt-6-luna"]      # position 1 was deferred as usual


# ── (5) budget, role and non-refusal circuits keep today's behaviour ────────────────────────────────────────────────

@pytest.mark.real_breaker
def test_a_budget_stop_keeps_its_refused_row_in_place(gw, monkeypatch, rows):
    over = SimpleNamespace(month_spent_usd=12.0, monthly_budget_usd=10.0, stops=True)
    monkeypatch.setattr(connection_budget, "stops", lambda conn: over if conn.connection_key == "acc3" else None)
    _route(monkeypatch, *ROUTE)
    resp = _generate(gw)
    assert resp.route_position == 2 and resp.route_skips is None
    assert [(r[0], r[1], r[2], r[3]) for r in _rows_view(rows)] == [(0, "sonnet-5-5", "refused", "budget"),
                                                                    (1, "gpt-6-luna", "refused", "budget")]


@pytest.mark.parametrize("plan, expected", [
    (None, None),                                                                      # registry does not describe it
    (DeploymentPlan([object()]), None),                                                # a deployment to call
    (DeploymentPlan([], [("acc3", "access_denied")]), [("acc3", "access_denied")]),
    (DeploymentPlan([], [("acc3", "quota_exhausted"), ("org", "credential")]),
     [("acc3", "quota_exhausted"), ("org", "credential")]),
    (DeploymentPlan([], [("acc3", "access_denied")], role_skips=[("acc2", "writer")]),
     [("acc3", "access_denied")]),                                                     # role skips beside: still sunk
    (DeploymentPlan([], [("acc3", "throttled")]), None),                               # throttling is not a refusal
    (DeploymentPlan([], [("acc3", "other")]), None),                                   # an outage
    (DeploymentPlan([], [("acc3", None)]), None),                                      # class unknown
    (DeploymentPlan([], [("acc3", "access_denied"), ("org", "other")]), None),         # mixed
    (DeploymentPlan([], [("acc3", "access_denied")], [("org", object())]), None),      # a budget skip beside
    (DeploymentPlan([], budget_skips=[("org", object())]), None),
    (DeploymentPlan([], role_skips=[("acc2", "writer")]), None),
    (DeploymentPlan([]), None),
    ([], None),                                                                        # a plain empty list
])
def test_sunk_skips_only_for_refusing_circuits(plan, expected):
    assert sunk_skips(plan) == expected


@pytest.mark.real_breaker
def test_an_outage_circuit_keeps_its_row_in_place(gw, monkeypatch, rows):
    _route(monkeypatch, *ROUTE)
    for _ in range(5):
        breaker.record_failure("acc3", "sonnet-5-5", RuntimeError("Read timed out"))
    resp = _generate(gw)
    assert resp.route_position == 1 and resp.route_skips is None
    assert [r[:4] for r in _rows_view(rows)] == [(0, "sonnet-5-5", "refused", "other")]


@pytest.mark.real_breaker
def test_a_single_model_request_is_never_deferred(gw, monkeypatch, rows):
    _refuse("sonnet-5-5")
    with pytest.raises(LLMAccessDenied):
        gw.generate(model_tier="sonnet-5-5")
    call_log.wait_for_pending_writes()
    assert [r[:3] for r in _rows_view(rows)] == [(0, "sonnet-5-5", "refused")]


# ── (6) breaker off: nothing sinks ──────────────────────────────────────────────────────────────────────────────────

def test_with_the_breaker_off_nothing_is_deferred(gw, monkeypatch, rows):
    """LLM_CIRCUIT_BREAKER=off (the conftest default): even a plan hook that reports an open access_denied circuit
    (a test double; the real is_open answers False when off) never defers a rung."""
    assert not breaker.enabled()
    vendor_route.circuit_open = lambda conn, model: conn == "acc3"
    monkeypatch.setattr(breaker, "open_class", lambda conn, model: "access_denied")
    _route(monkeypatch, *ROUTE)
    resp = _generate(gw)
    assert resp.route_position == 2 and resp.route_skips is None
    assert [r[:4] for r in _rows_view(rows)] == [(0, "sonnet-5-5", "refused", "access_denied"),
                                                 (1, "gpt-6-luna", "refused", "access_denied")]


# ── ledger: route_skips travels with the answered row ───────────────────────────────────────────────────────────────

SKIPS = [{"model": "model-one", "connection": "acc-x", "reason": "circuit_open:access_denied", "position": 0}]


def test_route_signal_keeps_well_formed_skips_only():
    assert call_log.route_signal("t", 1, SKIPS) == {"route_trace": "t", "route_position": 1, "route_skips": SKIPS}
    bad = [{"model": ""}, "x", {"model": "m" * 500, "connection": 3, "reason": None, "position": -1}]
    (only,) = call_log.route_signal(None, None, bad)["route_skips"]
    assert only == {"model": "m" * 120, "connection": None, "reason": None, "position": None}
    assert call_log.route_signal(None, None, "nope") == {}
    assert len(call_log.route_signal(None, None, SKIPS * 40)["route_skips"]) == 16


def test_billed_fields_and_billed_record_pass_route_skips():
    resp = LLMResponse(content="x", model_used="m", provider="bedrock", route_trace="t", route_position=1,
                       route_skips=SKIPS)
    assert call_log.billed_fields(resp)["route_skips"] == SKIPS
    assert call_log.billed_fields(MagicMock())["route_skips"] is None        # a test double: nothing
    assert call_log.billed_fields(LLMResponse(content="x", model_used="m", provider="p"))["route_skips"] is None
    rec = call_log.billed_record(resp)
    assert rec["route_skips"] == SKIPS
    (row,) = call_log.billed_unusable_rows(stage="s", role="judge", billed=[rec], error="bad")
    assert row["quality_signal"]["route_skips"] == SKIPS


def test_record_call_folds_route_skips_into_quality_signal(monkeypatch):
    conn = MagicMock()
    conn.close = AsyncMock()
    seen = {}

    async def execute_rows(c, sql, rows, extras, *, many, v2):
        seen["signal"] = rows[0][7]
    monkeypatch.setattr(call_log.asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(call_log, "get_database_url", lambda: "postgresql://x")
    monkeypatch.setattr(call_log, "_execute_rows", execute_rows)
    resp = LLMResponse(content="x", model_used="m", provider="bedrock", route_trace="t", route_position=2,
                       route_skips=SKIPS)
    asyncio.run(call_log.record_call(stage="s", role="judge", model="m", tokens_in=1, tokens_out=1, cost_usd=0.0,
                                     quality_signal={"passed": True}, **call_log.billed_fields(resp)))
    import json
    signal = json.loads(seen["signal"])
    assert signal["route_skips"] == SKIPS and signal["route_position"] == 2 and signal["passed"] is True


def test_record_calls_rows_accept_route_skips():
    folded = call_log._fold_route({"stage": "s", "role": "judge", "quality_signal": {}, "route_skips": SKIPS})
    assert "route_skips" not in folded and folded["quality_signal"]["route_skips"] == SKIPS
    assert "route_skips" in call_log.BILLED_FIELD_KEYS


def test_a_brand_fit_result_takes_every_billed_field():
    resp = LLMResponse(content="x", model_used="m", provider="bedrock", route_skips=SKIPS)
    r = BrandFitResult(brand_fit_score=1.0, cross_brand_distinct=1.0, mission_present=True, feedback="",
                       judge_score=1.0, model_used="m", cost_usd=0.0, input_tokens=1, output_tokens=1,
                       stop_reason="end_turn", account=None, fallback_used=False, **call_log.billed_fields(resp))
    assert r.route_skips == SKIPS


# ── Route trace: the deferred rungs are skipped attempts ────────────────────────────────────────────────────────────

NOW = datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)


def test_trace_view_shows_skipped_rungs_as_attempts_in_route_order():
    skips = [{"model": "model-one", "connection": "acc-x", "reason": "circuit_open:access_denied", "position": 0},
             {"model": "model-two", "connection": "acc-x", "reason": "circuit_open:quota_exhausted", "position": 1}]
    ok = {"created_at": NOW, "stage": "stage_a", "role": "judge", "model": "model-three", "status": "ok",
          "attempt_no": 1, "latency_ms": 400, "cost_usd": 0.0003, "position": 2, "fallback_used": True,
          "connection_key": "api-x", "route_skips": skips}
    v = trace_svc.trace_view("t", [ok], {"stage": "stage_a", "role": "judge", "model_id": "model-one",
                                         "fallback_model_ids": ["model-two", "model-three"], "is_active": True})
    assert [(a["model"], a["status"], a["position"]) for a in v["attempts"]] == \
        [("model-one", "skipped", 0), ("model-two", "skipped", 1), ("model-three", "ok", 2)]
    first = v["attempts"][0]
    assert (first["cost_usd"], first["latency_ms"], first["error_class"], first["connection"]) == \
        (0.0, None, "access_denied", "acc-x")
    assert v["attempts"][1]["error_class"] == "quota_exhausted"
    assert v["outcome"] == "fallback" and v["served"]["model"] == "model-three"
    assert v["cost_usd"] == 0.0003 and v["latency_ms"] == 400


def test_trace_view_reads_route_skips_as_json_text_and_ignores_junk():
    ok = {"created_at": NOW, "stage": "s", "role": "judge", "model": "m", "status": "ok", "attempt_no": 1,
          "position": 1, "route_skips": '[{"model": "model-one", "position": 0, "reason": "circuit_open:credential"}]'}
    v = trace_svc.trace_view("t", [ok], None)
    assert [a["status"] for a in v["attempts"]] == ["skipped", "ok"]
    for junk in ("not json", "{}", None, [1, "x"]):
        v = trace_svc.trace_view("t", [{**ok, "route_skips": junk}], None)
        assert [a["status"] for a in v["attempts"]] == ["ok"]


def test_requests_count_skipped_attempts_and_keep_the_fallback_filter():
    sql = trace_svc.requests_sql(stage=None, fallback_only=True)
    assert "route_skips" in sql and "skipped_attempts" in sql and "served_fallback" in sql
    base = {"trace": "t", "answered": True, "served_attempt": 1, "served_fallback": True, "rows": 1,
            "skipped_attempts": 2}
    v = trace_svc.request_view(base)
    assert v["outcome"] == "fallback" and v["skipped_attempts"] == 2
    assert trace_svc.request_view({**base, "skipped_attempts": None})["skipped_attempts"] == 0
    assert "route_skips" in trace_svc.TRACE_SQL
