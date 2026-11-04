"""AA-1042 (S268, lane trace) — Route trace, Alerts, Quality guard.

- Gateway: every attempt row of one request and the answered reply share one route trace id; the answered reply
  carries the route position; the route test's forced refusal refuses route position 0 of its own stage only, with
  no provider call; a writer answered by a fallback model notes it for the quality guard (a judge does not).
- call_log: billed_fields passes the route keys; record_call / record_calls fold them into quality_signal.
- Quality guard: the fallback_writer degraded entry is "count" by default and "hard" with the stage's hold switch;
  A1 holds through DEGRADED_DETECTOR, T9 through the held label, T2 through the version's admin-only mark
  (judge_breakdown.fallback_writer).
No AWS, no vendor, no DB, no paid call.
"""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.quality import degraded, route_guard
from shared.llm_client import call_log, catalog, route_watch, vendor_route
from shared.llm_client import client as client_mod
from shared.llm_client.models import LLMResponse
from tests.unit.test_aa1002_s3_deployment_routing import World, _dep, _org, _route


@pytest.fixture
def rows(monkeypatch):
    seen: list = []

    async def record_calls(rows):
        seen.extend(rows)
    monkeypatch.setattr(call_log, "record_calls", record_calls)
    yield seen


@pytest.fixture
def gw(monkeypatch):
    hook = vendor_route.circuit_open
    w = World(monkeypatch)
    w.conns["bedrock-llm-org"] = _org()
    w.deps = [d for d in w.deps if d.model_key != "gpt-6-luna"]
    w.deps.append(_dep("gpt-6-luna", "bedrock-llm-org", "us.openai.gpt-6-luna", 10))
    w.reset()
    yield w
    w.reset()
    catalog.invalidate()
    vendor_route.circuit_open = hook


def _hold(monkeypatch, stage: str, on: bool = True):
    route_watch.reset()
    monkeypatch.setattr(route_watch, "loader", lambda: {stage: {"stage": stage, "hold_fallback_writes": on}})


# ── gateway: one trace id per request ───────────────────────────────────────────────────────────────────────────────

def test_the_refused_attempt_and_the_answer_share_one_route_trace(gw, monkeypatch, rows):
    gw.fail = {"acc3": RuntimeError("An error occurred (AccessDeniedException): Error 002")}
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    resp = gw.generate(stage="adhoc_s3")
    call_log.wait_for_pending_writes()
    (row,) = rows
    assert uuid.UUID(resp.route_trace) and row["quality_signal"]["route_trace"] == resp.route_trace
    assert row["quality_signal"]["position"] == 0 and resp.route_position == 1 and resp.attempt_no == 2
    # the caller's row gets both keys through billed_fields
    f = call_log.billed_fields(resp)
    assert (f["route_trace"], f["route_position"]) == (resp.route_trace, 1)


def test_two_requests_get_two_trace_ids(gw, monkeypatch, rows):
    _route(monkeypatch, "sonnet-5-5")
    a, b = gw.generate(stage="adhoc_s3"), gw.generate(stage="adhoc_s3")
    assert a.route_trace and b.route_trace and a.route_trace != b.route_trace
    assert (a.route_position, a.attempt_no) == (0, 1)


def test_forced_refusal_refuses_the_first_model_without_calling_it(gw, monkeypatch, rows):
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    with client_mod.forced_primary_refusal("adhoc_s3"):
        resp = gw.generate(stage="adhoc_s3")
    call_log.wait_for_pending_writes()
    assert resp.content == "from bedrock-llm-org" and resp.fallback_used is True
    assert [c[2] for c in gw.calls] == ["us.openai.gpt-6-luna"]          # sonnet was never sent
    (row,) = rows
    assert (row["model"], row["status"], row["error_class"], row["attempt_no"], row["cost_usd"]) == \
        ("sonnet-5-5", "refused", "access_denied", 1, 0.0)
    assert "route test" in row["quality_signal"]["error"]
    assert row["quality_signal"]["route_trace"] == resp.route_trace


def test_forced_refusal_is_scoped_to_its_stage_and_context(gw, monkeypatch, rows):
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    with client_mod.forced_primary_refusal("another_stage"):
        resp = gw.generate(stage="adhoc_s3")
    assert resp.route_position == 0 and resp.fallback_used is False
    resp = gw.generate(stage="adhoc_s3")
    assert resp.route_position == 0


def test_forced_refusal_keeps_the_breaker_and_the_circuit_untouched(gw, monkeypatch, rows):
    from shared.llm_client import breaker
    recorded = MagicMock()
    monkeypatch.setattr(breaker, "record_failure", recorded)
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    with client_mod.forced_primary_refusal("adhoc_s3"):
        gw.generate(stage="adhoc_s3")
    recorded.assert_not_called()


def test_a_writer_answered_by_a_fallback_model_is_noted_for_the_quality_guard(gw, monkeypatch, rows):
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    with route_guard.collect_output() as box, degraded.collect() as job_box:
        with client_mod.forced_primary_refusal("adhoc_s3"):
            gw.generate(stage="adhoc_s3")
    assert box == [{"stage": "adhoc_s3", "model": "gpt-6-luna", "position": 1, "severity": "count"}]
    assert job_box == {"fallback_writer:adhoc_s3:count": 1}


def test_a_primary_answer_notes_nothing(gw, monkeypatch, rows):
    _route(monkeypatch, "sonnet-5-5", "gpt-6-luna")
    with route_guard.collect_output() as box:
        gw.generate(stage="adhoc_s3")
    assert box == []


def test_a_judge_answered_by_a_fallback_model_is_not_a_fallback_write(monkeypatch):
    noted = MagicMock()
    monkeypatch.setattr(route_guard, "note_fallback_write", noted)
    client_mod._note_fallback_write(MagicMock(stage="s"), "judge", "m", 1)
    noted.assert_not_called()
    client_mod._note_fallback_write(MagicMock(stage="s"), "writer", "m", 1)
    noted.assert_called_once_with("s", "m", 1)


# ── call_log: the route keys ────────────────────────────────────────────────────────────────────────────────────────

def test_billed_fields_reads_the_route_keys_defensively():
    resp = LLMResponse(content="", model_used="m", provider="p", route_trace="t-1", route_position=2)
    f = call_log.billed_fields(resp)
    assert (f["route_trace"], f["route_position"]) == ("t-1", 2)
    bad = MagicMock(route_trace=5, route_position="x")
    assert call_log.billed_fields(bad)["route_trace"] is None and call_log.billed_fields(bad)["route_position"] is None
    assert set(call_log.ROUTE_KEYS) <= set(call_log.BILLED_FIELD_KEYS)


def test_with_route_keeps_a_key_the_signal_already_has():
    assert call_log._with_route({"a": 1}) == {"a": 1}
    assert call_log._with_route({"a": 1}, "t", 0) == {"a": 1, "route_trace": "t", "route_position": 0}
    assert call_log._with_route({"route_trace": "mine"}, "t", None) == {"route_trace": "mine"}
    assert call_log._with_route(None, "t") == {"route_trace": "t"}


def test_record_call_folds_the_route_keys_into_quality_signal(monkeypatch):
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.close = AsyncMock()
    conn.is_in_transaction = MagicMock(return_value=False)
    monkeypatch.setattr(call_log.asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(call_log, "get_database_url", lambda: "postgresql://x")
    resp = LLMResponse(content="ok", model_used="m", provider="openai", route_trace="trace-1", route_position=1)
    asyncio.run(call_log.record_call(stage="s", role="writer", model="m", tokens_in=1, tokens_out=1, cost_usd=0.1,
                                     quality_signal={"passed": True}, **call_log.billed_fields(resp)))
    sql, *args = conn.execute.call_args.args
    signal = json.loads(args[7])
    assert signal == {"passed": True, "route_trace": "trace-1", "route_position": 1}


def test_record_calls_accepts_and_folds_the_route_keys(monkeypatch):
    seen = []

    async def execute_rows(conn, sql, base_rows, extras, *, many, v2=None):
        seen.extend(base_rows)
    conn = MagicMock()
    conn.close = AsyncMock()
    conn.transaction = MagicMock(return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))
    monkeypatch.setattr(call_log.asyncpg, "connect", AsyncMock(return_value=conn))
    monkeypatch.setattr(call_log, "get_database_url", lambda: "postgresql://x")
    monkeypatch.setattr(call_log, "_execute_rows", execute_rows)
    asyncio.run(call_log.record_calls([{"stage": "s", "role": "judge", "model": "m", "quality_signal": {"x": 1},
                                        "route_trace": "t-9", "route_position": 0}]))
    (row,) = seen
    assert json.loads(row[7]) == {"x": 1, "route_trace": "t-9", "route_position": 0}


def test_billed_unusable_rows_carry_the_route_trace():
    rec = call_log.billed_record(LLMResponse(content="", model_used="m", provider="p", route_trace="t-2",
                                             route_position=0))
    (row,) = call_log.billed_unusable_rows(stage="s", role="writer", billed=[rec], error="bad json")
    assert row["quality_signal"]["route_trace"] == "t-2" and row["quality_signal"]["billed_unusable"] is True


# ── quality guard ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_fallback_writes_are_marked_by_default_and_held_with_the_switch(monkeypatch):
    assert route_guard.severity("t9_write") == "count"
    _hold(monkeypatch, "t9_write")
    assert route_guard.severity("t9_write") == "hard" and route_guard.severity("s1_generate") == "count"


def test_the_star_row_sets_the_hold_for_every_stage(monkeypatch):
    route_watch.reset()
    monkeypatch.setattr(route_watch, "loader", lambda: {"*": {"stage": "*", "hold_fallback_writes": True},
                                                        "t9_write": {"stage": "t9_write",
                                                                     "hold_fallback_writes": False}})
    assert route_guard.severity("s1_generate") == "hard" and route_guard.severity("t9_write") == "count"


def test_an_unreadable_policy_marks_without_holding(monkeypatch):
    route_watch.reset()

    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(route_watch, "loader", boom)
    assert route_guard.severity("t9_write") == "count"


def test_entries_merge_per_stage_and_severity():
    notes = [{"stage": "t9_write", "model": "m2", "position": 1, "severity": "count"},
             {"stage": "t9_write", "model": "m2", "position": 1, "severity": "count"},
             {"stage": "t9_sentence_fix", "model": "m3", "position": 2, "severity": "hard"}]
    lst = route_guard.entries(notes)
    assert [(e["detector"], e["severity"], e["count"]) for e in lst] == [
        ("route:t9_write", "count", 2), ("route:t9_sentence_fix", "hard", 1)]
    assert route_guard.entries([]) == [] and route_guard.entries() == []


def test_a1_marks_a_fallback_writer_and_holds_it_with_the_switch(monkeypatch):
    state = {"fallback_used": True, "model_used": "m2", "degraded": [], "failure_codes": []}
    marked = degraded.apply_a1_hold(route_guard.with_a1_entries(state))
    assert marked["degraded"][0]["name"] == "fallback_writer" and marked["degraded"][0]["severity"] == "count"
    assert degraded.HOLD_CODE not in marked["failure_codes"]
    _hold(monkeypatch, "s1_generate")
    held = degraded.apply_a1_hold(route_guard.with_a1_entries(state))
    assert degraded.HOLD_CODE in held["failure_codes"] and held["brand_audit_status"] == "manual_check"
    assert "written by a fallback model" in held["feedback"] and "did not run" not in held["feedback"]


def test_t2_graph_state_is_marked_never_blocked(monkeypatch):
    # review fix: the T2 hold is the version's admin-only label (t2_mark), never a hard code inside the graph
    _hold(monkeypatch, "t2_generate")
    state = {"fallback_used": True, "generate_stage": "t2_generate", "is_tenant_rewrite": True, "degraded": [],
             "failure_codes": []}
    out = degraded.apply_a1_hold(route_guard.with_a1_entries(state))
    (e,) = out["degraded"]
    assert (e["detector"], e["severity"]) == ("route:t2_generate", "count")
    assert degraded.HOLD_CODE not in out["failure_codes"]


def test_t2_mark_is_held_with_the_stage_switch():
    assert route_guard.t2_mark([]) is None and route_guard.t2_mark(None) is None
    count = route_guard.t2_mark([{"stage": "t2_generate", "model": "m2", "position": 1, "severity": "count"}])
    assert count["held"] is False and count["entries"][0]["detector"] == "route:t2_generate"
    hard = route_guard.t2_mark([{"stage": "t2_generate", "model": "m2", "position": 1, "severity": "count"},
                                {"stage": "t2_repair", "model": "m3", "position": 2, "severity": "hard"}])
    assert hard["held"] is True and len(hard["entries"]) == 2


def _t2_save(monkeypatch, *, fallback: bool, hold: bool = False):
    """Run the T2 save (tests/unit/test_aa795_t2_judge_breakdown harness) with a writer the gateway reports as
    answered by a fallback model (route_guard.note_fallback_write, as client._note_fallback_write does)."""
    from unittest.mock import patch
    from tests.unit.test_aa778_t2_em_dash import SOURCE as T2_SOURCE, _pool
    from services.jobs import t2_rewrite_job as t2
    if hold:
        _hold(monkeypatch, "t2_generate")
    result = {"status": "success", "quality_score": 8.0, "degraded": [],
              "generated": {"name": "Ridge walk", "subtitle": "s", "summary": "Two quiet days.",
                            "highlights": ["A ridge"], "itineraries": "Day 1: Walk.", "seo_title": "t",
                            "seo_meta": "m", "trip_type": "a"}}

    async def rewrite(*a, **k):
        if fallback:
            route_guard.note_fallback_write("t2_generate", "zz-model-two", 1)
        return result
    qa = {"result": result, "passed": True, "attempts": 0, "structural_issues": [], "grounding_issues": [],
          "chosen_attempt": 0}
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    conn.fetchval.return_value = 5.0
    tour = dict(T2_SOURCE, subtitle="", description="", seo_title="", seo_meta="", country="Nepal", duration="2 Days")
    with patch("api.routers.v1_pipeline._rewrite_tour", AsyncMock(side_effect=rewrite)), \
         patch("services.acp_produce.tenant_pipeline.run_t3_qa_gate", AsyncMock(return_value=qa)), \
         patch("services.seo_intelligence.s1_prefetch.tenant_market_seo", AsyncMock(return_value=("US", None))), \
         patch("services.writing_memory.insertion.resolve", AsyncMock(return_value=None)):
        diag = asyncio.run(t2._rewrite_and_save(_pool(conn), {"tour_id": "tour-1"}, tour, {}, "tenant-1", "v-1",
                                                "p-1"))
    [save] = [c.args for c in conn.execute.call_args_list if "SET rewritten_content" in c.args[0]]
    return save, diag


def test_t2_version_written_by_a_fallback_model_stores_the_admin_only_mark(monkeypatch):
    save, diag = _t2_save(monkeypatch, fallback=True)
    stored = json.loads(save[9])
    assert stored["fallback_writer"]["held"] is False
    assert stored["fallback_writer"]["entries"][0]["detector"] == "route:t2_generate"
    assert diag["fallback_writer"]["held"] is False and diag["t3"]["graph_degraded"] == []
    assert save[2] == "ai_generated"                   # the status stays score-based: the tenant gets the text
    assert "fallback_writer" not in save[1]            # never in the tenant-facing text


def test_t2_version_is_held_with_the_switch_and_unmarked_without_a_fallback(monkeypatch):
    save, _ = _t2_save(monkeypatch, fallback=True, hold=True)
    assert json.loads(save[9])["fallback_writer"]["held"] is True and save[2] == "ai_generated"
    route_watch.reset()
    save, diag = _t2_save(monkeypatch, fallback=False)
    assert save[9] is None or "fallback_writer" not in json.loads(save[9])
    assert "fallback_writer" not in diag


def test_with_a1_entries_replaces_its_own_entry_and_leaves_others():
    other = degraded.entry(degraded.JEV_UNAVAILABLE, detector="grounding")
    state = {"fallback_used": True, "degraded": [other]}
    once = route_guard.with_a1_entries(state)
    twice = route_guard.with_a1_entries(once)
    assert twice["degraded"] == once["degraded"] and len(once["degraded"]) == 2
    clean = {"fallback_used": False, "degraded": [other]}
    assert route_guard.with_a1_entries(clean) is clean


def test_t9_holds_a_piece_only_with_the_switch():
    count = route_guard.entries([{"stage": "t9_write", "model": "m", "position": 1, "severity": "count"}])
    assert degraded.apply_t9_hold("approved", None, count) == ("approved", None)
    hard = route_guard.entries([{"stage": "t9_write", "model": "m", "position": 1, "severity": "hard"}])
    assert degraded.apply_t9_hold("approved", None, hard) == ("held", "degraded: fallback_writer")
    assert degraded.t9_flag(count)["blocking"] is False


def test_t9_degraded_final_reads_the_bound_collector():
    from services.acp_content_writing.service import _degraded_final
    with route_guard.collect_output() as box:
        box.append({"stage": "t9_write", "model": "m2", "position": 1, "severity": "count"})
        status, reason, flags, log = _degraded_final([], [], [], "approved", None)
    assert status == "approved" and flags[-1]["gate"] == "degraded"
    assert "fallback_writer" in " ".join(flags[-1]["violations"])
    assert _degraded_final([], [], [], "approved", None)[0] == "approved"
