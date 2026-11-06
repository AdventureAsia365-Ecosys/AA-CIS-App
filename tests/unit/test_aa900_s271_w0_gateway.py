"""S271 W0 (AA-900 / AA-741, lane l13-w0-gateway) — the gateway part of the writing-quality restructure.

1. One owner of the truncation retry. An EMPTY reply cut at max_tokens (a billed unusable reply: the reasoning used
   every token) on a T8 / T9 stage is retried once on the SAME model at double the ceiling before the route moves on;
   before W0 token_budget sent those stages with budget_retry=False, so the route swapped Sonnet 5.5 for Sonnet 4.6
   silently (3 of 3 in the 06/11 ledger). The retry records why (retry_reason).
2. Degraded marks for the measuring roles: a judge / validate / decide answer from a fallback rung (> 0) or from
   nova-lite, and a judge of the same model family as a writer of the same output, are marked on the reply
   (LLMResponse.route_degraded, ledger quality_signal.degraded) and in the quality guard's collectors (count: no hold).
3. token_floor(model, effort). The CI lint built on it (every call site of every routed stage, read from the code and
   the migrations) is tests/unit/test_aa900_s271_effort_floor_lint.py.

No AWS, no vendor, no DB, no paid call. Model keys are the catalog's or invented; no fixture text from real data.
"""
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from services.acp_shared import token_budget as tb
from services.quality import degraded, route_guard
from shared.llm_client import call_log, family_rule, stage_params, token_floor
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient, _billed_unusable, _billed_usage, _mark_degraded_answer
from shared.llm_client.models import LLMRequest, LLMResponse
from shared.llm_client.role_config import StageConfig

ROOT = Path(__file__).resolve().parents[2]


def _model(key, vendor, **kw):
    base = dict(model_key=key, label=key, vendor=vendor, provider="bedrock", api_style="converse",
                bedrock_profile_ids={"acc3": f"global.{vendor}.{key}"}, enabled=True)
    base.update(kw)
    return CatalogModel(**base)


SONNET55 = _model("sonnet-5-5", "anthropic")
SONNET46 = _model("sonnet-4-6", "anthropic")
LUNA = _model("gpt-6-luna", "openai")
LUNA_OPENAI = _model("gpt-6-luna-openai", "openai", provider="openai", api_style="openai_chat", bedrock_profile_ids={})
NOVA = _model("nova-lite", "amazon")
CATALOG = {m.model_key: m for m in (SONNET55, SONNET46, LUNA, LUNA_OPENAI, NOVA)}


@pytest.fixture(autouse=True)
def no_db(monkeypatch):
    """No stage_model_params table, the catalog from CATALOG (also for the family lookup)."""
    monkeypatch.setattr(stage_params, "params_for", lambda stage, model_key: None)
    monkeypatch.setattr("shared.llm_client.catalog.get_model_sync", CATALOG.get)


@pytest.fixture
def rows(monkeypatch):
    seen: list = []

    async def record_calls(rows):
        seen.extend(rows)
    monkeypatch.setattr(call_log, "record_calls", record_calls)
    return seen


def _client():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        return LLMClient()


def _resp(model, content="ok", stop="end_turn", cost=0.01):
    return LLMResponse(content=content, model_used=model, provider="p", cost_usd=cost, input_tokens=10,
                       output_tokens=5, stop_reason=stop)


def _empty_cut(model, cost=0.13):
    """What the Anthropic Messages / Converse / OpenAI paths raise for an empty reply cut at its ceiling."""
    return _billed_unusable(f"{model} returned empty content (stop_reason=max_tokens)", [_billed_usage(
        f"satellite-{model}", "acc3", {"tokens_in": 900, "tokens_out": 12000}, cost, "max_tokens", 1)])


# ── 1. one owner of the truncation retry ───────────────────────────────────────────────────────────────────────────

def test_an_empty_t9_write_cut_is_retried_on_the_same_model_before_the_route_moves_on(rows):
    """Red on base: token_budget sent budget_retry=False, the route answered from sonnet-4-6."""
    c = _client()
    tried: list = []

    def fake(request, key, *_):
        tried.append((key, request.max_tokens))
        if len(tried) == 1:
            raise _empty_cut(key)
        return _resp(f"satellite-{key}", "Full post.", cost=0.2)

    def generate(request):          # the gateway's route for t9_write (migration 376, first two rungs)
        return c._call_route(request, ["sonnet-5-5", "sonnet-4-6"], "acc3", role="writer")

    with patch("shared.llm_client.client.get_model_sync", side_effect=CATALOG.get), patch.object(c, "_call_key", fake):
        reply = tb.generate_with_budget_retry(generate, LLMRequest(system_prompt="s", user_prompt="u",
                                                                   stage="t9_write", max_tokens=12000))
    call_log.wait_for_pending_writes()
    assert tried == [("sonnet-5-5", 12000), ("sonnet-5-5", 24000)]       # same model, double, never sonnet-4-6
    assert reply.resp.model_used == "satellite-sonnet-5-5" and reply.resp.fallback_used is False
    assert reply.retried and reply.retry_reason == token_floor.RETRY_BILLED_UNUSABLE_CUT and not reply.still_cut
    assert reply.cost_usd == pytest.approx(0.33)                         # the retry + the billed empty cut
    (cut_row,) = rows                                                    # the gateway's billed_unusable row
    assert cut_row["quality_signal"]["billed_unusable"] is True and cut_row["stop_reason"] == "max_tokens"
    # the caller's row of the retry: retry facts through billed_fields
    fields = call_log.billed_fields(reply.resp)
    assert fields["truncation_retry_max_tokens"] == 24000
    assert fields["truncation_retry_reason"] == "billed_unusable_cut"
    assert call_log._with_route({}, truncation_retry_max_tokens=24000, truncation_retry_reason="billed_unusable_cut") \
        == {"truncation_retry": True, "max_tokens": 24000, "retry_reason": "billed_unusable_cut"}


def test_the_token_budget_request_keeps_the_gateway_retry_on():
    seen: list = []
    tb.generate_with_budget_retry(lambda r: seen.append(r) or _resp("m"),
                                  LLMRequest(system_prompt="s", user_prompt="u", stage="t8_angle_gen"))
    assert seen[0].budget_retry is True


@pytest.mark.parametrize("stop", ["max_tokens", "length", "model_context_window_exceeded"])
def test_an_empty_cut_of_every_provider_is_a_cut_failure(stop):
    exc = _billed_unusable("x returned empty content", [_billed_usage("x", "acc3", {"tokens_out": 8}, 0.05, stop, 1)])
    assert token_floor.is_cut_failure(exc)
    assert token_floor.cut_failure_reason(exc) == "billed_unusable_cut"
    assert token_floor.cut_failure_cost(exc) == pytest.approx(0.05)


def test_an_unusable_reply_that_was_not_cut_is_not_retried():
    exc = _billed_unusable("x returned no JSON", [_billed_usage("x", "acc3", {"tokens_out": 8}, 0.05, "end_turn", 1)])
    assert not token_floor.is_cut_failure(exc)


# ── 2. degraded marks of the measuring roles ───────────────────────────────────────────────────────────────────────

def _cfg(stage, role, primary, *fallbacks):
    return StageConfig(stage, role, "x", primary, None, fallback_model_ids=tuple(fallbacks))


def _gen(c, request, cfg):
    with patch("shared.llm_client.client.get_stage_config_sync", return_value=cfg), \
            patch("shared.llm_client.client.get_model_sync", side_effect=CATALOG.get):
        return c.generate(request)


def _answer_from(*answering):
    def fake(request, key, *_):
        if key not in answering:
            raise RuntimeError("ThrottlingException: slow down")
        return _resp(f"satellite-{key}")
    return fake


@pytest.mark.parametrize("role", ["judge", "validate", "decide"])
def test_a_measuring_answer_from_a_fallback_rung_is_marked_degraded(rows, role):
    """Red on base: no route_degraded on the reply, nothing noted."""
    c = _client()
    with degraded.collect() as job, route_guard.collect_output() as notes, \
            patch.object(c, "_call_key", _answer_from("gpt-6-luna-openai")):
        resp = _gen(c, LLMRequest(system_prompt="s", user_prompt="u", stage="zz_check"),
                    _cfg("zz_check", role, "gpt-6-luna", "gpt-6-luna-openai"))
        lst = route_guard.entries()
    (mark,) = resp.route_degraded
    assert (mark["name"], mark["reason"], mark["model"], mark["position"]) == \
        ("fallback_instrument", "rung", "gpt-6-luna-openai", 1)
    assert call_log.billed_fields(resp)["route_degraded"][0]["name"] == "fallback_instrument"
    assert call_log._with_route({}, route_degraded=resp.route_degraded)["degraded"][0]["reason"] == "rung"
    assert notes[0]["name"] == "fallback_instrument" and job == {"fallback_instrument:zz_check:count": 1}
    assert [e["name"] for e in lst] == ["fallback_instrument"] and lst[0]["severity"] == "count"
    assert degraded.apply_t9_hold("approved", None, lst) == ("approved", None)       # W0: marked, never held


def test_a_judge_answered_by_the_first_rung_is_clean():
    c = _client()
    with route_guard.collect_output() as notes, patch.object(c, "_call_key", _answer_from("gpt-6-luna")):
        resp = _gen(c, LLMRequest(system_prompt="s", user_prompt="u", stage="zz_judge"),
                    _cfg("zz_judge", "judge", "gpt-6-luna", "gpt-6-luna-openai"))
    assert resp.route_degraded is None and notes == []


def test_nova_lite_on_an_instrument_stage_is_marked_even_as_the_only_model():
    c = _client()
    with route_guard.collect_output() as notes, patch.object(c, "_call_key", _answer_from("nova-lite")):
        resp = _gen(c, LLMRequest(system_prompt="s", user_prompt="u", stage="zz_check", model_tier="nova-lite"),
                    _cfg("zz_check", "validate", "gpt-6-luna"))
    assert [(m["name"], m["reason"]) for m in resp.route_degraded] == [("fallback_instrument", "uncalibrated_model")]
    assert "uncalibrated instrument" in route_guard.entries(notes)[0]["detail"]


def test_a_writer_fallback_is_marked_on_the_reply_and_noted_as_before(monkeypatch):
    c = _client()
    noted: list = []
    monkeypatch.setattr(route_guard, "note_fallback_write", lambda *a: noted.append(a))
    with route_guard.collect_output() as notes, patch.object(c, "_call_key", _answer_from("sonnet-4-6")):
        resp = _gen(c, LLMRequest(system_prompt="s", user_prompt="u", stage="zz_write"),
                    _cfg("zz_write", "writer", "sonnet-5-5", "sonnet-4-6"))
    assert [(m["name"], m["reason"]) for m in resp.route_degraded] == [("fallback_writer", "rung")]
    assert noted == [("zz_write", "sonnet-4-6", 1)] and notes == []    # the writer note is the AA-1042 one


def test_a_same_family_writer_and_judge_of_one_output_are_marked():
    """The writer route ends on GPT (its last resort), the judge is GPT: the judge's verdict scores its own family."""
    c = _client()
    writer = _cfg("zz_write", "writer", "sonnet-5-5", "gpt-6-luna-openai")
    judge = _cfg("zz_judge", "judge", "gpt-6-luna")
    with route_guard.collect_output() as notes:
        with patch.object(c, "_call_key", _answer_from("gpt-6-luna-openai")):
            _gen(c, LLMRequest(system_prompt="s", user_prompt="u", stage="zz_write"), writer)
        with patch.object(c, "_call_key", _answer_from("gpt-6-luna")):
            verdict = _gen(c, LLMRequest(system_prompt="s", user_prompt="u", stage="zz_judge"), judge)
    (mark,) = [m for m in verdict.route_degraded if m["name"] == "same_family_pair"]
    assert mark["families"] == ["openai"] and mark["writers"] == ["gpt-6-luna-openai"]
    assert [n["name"] for n in route_guard.instrument_notes(notes)] == ["same_family_pair"]


def test_a_writer_and_judge_of_different_families_are_clean():
    c = _client()
    with route_guard.collect_output() as notes:
        with patch.object(c, "_call_key", _answer_from("sonnet-5-5")):
            _gen(c, LLMRequest(system_prompt="s", user_prompt="u", stage="zz_write"),
                 _cfg("zz_write", "writer", "sonnet-5-5"))
        with patch.object(c, "_call_key", _answer_from("gpt-6-luna")):
            verdict = _gen(c, LLMRequest(system_prompt="s", user_prompt="u", stage="zz_judge"),
                           _cfg("zz_judge", "judge", "gpt-6-luna"))
    assert verdict.route_degraded is None and notes == []


def test_no_bound_output_no_same_family_mark():
    """Production traffic outside a job's output (no trace): only the rung / nova-lite rules can mark."""
    assert route_guard.answer_marks("zz", "judge", "satellite-gpt-6-luna", 0, []) == []
    assert family_rule.current_trace() is None


def test_an_eval_runners_trace_is_kept_inside_a_jobs_output_binding():
    with family_rule.trace_models() as outer:
        with route_guard.collect_output():
            family_rule.note_model("writer", "satellite-sonnet-5-5")
        assert outer == [("writer", "satellite-sonnet-5-5")]


def test_a_mark_never_breaks_the_call():
    req = LLMRequest(system_prompt="s", user_prompt="u", stage="zz")
    with patch.object(route_guard, "answer_marks", side_effect=RuntimeError("boom")):
        _mark_degraded_answer(req, "judge", _resp("m"))       # no raise


# ── what each caller does with an instrument mark (W0: count, never a hold) ───────────────────────────────────────

_INSTRUMENT_NOTE = {"name": "fallback_instrument", "stage": "t10_judge", "model": "gpt-6-luna-openai", "position": 3,
                    "severity": "count", "reason": "rung"}
_WRITER_NOTE = {"stage": "t2_generate", "model": "sonnet-4-6", "position": 1, "severity": "count"}


def test_t9_degraded_final_flags_an_instrument_mark_without_a_hold():
    from services.acp_content_writing import service
    with route_guard.collect_output() as notes:
        notes.append(dict(_INSTRUMENT_NOTE))
        status, reason, flags, log = service._degraded_final([], [], [], "approved", None)
    assert status == "approved" and reason is None
    assert flags[-1]["gate"] == "degraded" and flags[-1]["counts"] == {"fallback_instrument": 1}
    assert log[-1]["held"] is False


def test_t2_keeps_instrument_marks_apart_from_the_held_label():
    notes = [dict(_INSTRUMENT_NOTE), dict(_WRITER_NOTE)]
    assert [e["name"] for e in route_guard.t2_mark(notes)["entries"]] == ["fallback_writer"]
    assert [e["name"] for e in route_guard.t2_instrument_mark(notes)["entries"]] == ["fallback_instrument"]
    assert route_guard.t2_instrument_mark([dict(_WRITER_NOTE)]) is None


def test_a1_state_gets_the_instrument_entries_and_no_hold():
    state = {"generate_stage": "s1_generate", "degraded": [], "failure_codes": []}
    with route_guard.collect_output() as notes:
        notes.append(dict(_INSTRUMENT_NOTE, stage="s1_judge"))
        out = route_guard.with_a1_entries(state)
        again = route_guard.with_a1_entries(out)                 # the graph may finish twice: no double count
        t2 = route_guard.with_a1_entries({"generate_stage": "t2_generate", "is_tenant_rewrite": True})
    assert [(e["name"], e["detector"], e["count"]) for e in again["degraded"]] == \
        [("fallback_instrument", "route:s1_judge", 1)]
    assert degraded.apply_a1_hold(again)["failure_codes"] == []    # count: no DEGRADED_DETECTOR
    assert "degraded" not in t2 or not t2["degraded"]


# ── 3. token_floor(model, effort) (the CI lint: test_aa900_s271_effort_floor_lint.py) ────────────────────────────

def test_the_effort_floor():
    assert token_floor.effort_floor("sonnet-5-5") == 8192                      # default effort (high)
    assert token_floor.effort_floor("haiku-5-5", "low") == 4096
    assert token_floor.effort_floor("sonnet-5-5", "max") == 16000
    assert token_floor.effort_floor("gpt-6-luna", "low", {"min_output_tokens": 8192}) == 8192   # the row wins
    assert token_floor.effort_floor("sonnet-5-5", "max", model_cap=12000) == 12000
    assert token_floor.effort_floor("sonnet-4-6", "high") is None and token_floor.effort_floor("nova-lite") is None
