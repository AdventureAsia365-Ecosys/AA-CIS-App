"""AA-927 — one shared.llm_call_log row per billed LLM call (behavioural).

The AA-729 baseline counted 216 LLMClient.generate() returns against 100 logged rows: a judge verdict that
made three calls (AA-714 near-band repeat, AA-801 median3) wrote one row, and a reply billed and then
dropped (malformed JSON, a raise after the call) wrote none. Each case here drives the real caller with a
fake gateway and asserts rows == calls and sum(row cost) == billed cost. Mock-only: no AWS, no DB, no LLM.
"""
import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from services.content_generation import brand_fit as bf
from services.content_generation import judge_node as jn
from shared.llm_client import call_log
from shared.llm_client.catalog import CatalogModel
from shared.llm_client.client import LLMClient, _billed_unusable
from shared.llm_client.models import LLMRequest, LLMResponse


# ── the fake gateway + the row capture ──────────────────────────────────────────────────────────


class Ledger:
    """`script` = what each LLMClient.generate() does in turn: an LLMResponse (returned = billed), or an
    exception instance (raised before anything was billed)."""

    def __init__(self) -> None:
        self.script: list = []
        self.returned: list[LLMResponse] = []
        self.rows: list[dict] = []

    def generate(self, _client, request):
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        self.returned.append(item)
        return item

    @property
    def billed_usd(self) -> float:
        return round(sum(r.cost_usd for r in self.returned), 9)

    @property
    def row_usd(self) -> float:
        return round(sum(float(r.get("cost_usd") or 0.0) for r in self.rows), 9)

    def assert_balanced(self):
        assert len(self.rows) == len(self.returned), (self.rows, self.returned)
        assert self.row_usd == pytest.approx(self.billed_usd)


@pytest.fixture
def llm_ledger(monkeypatch):
    led = Ledger()
    monkeypatch.setattr(LLMClient, "__init__", lambda self: None)
    monkeypatch.setattr(LLMClient, "generate", lambda self, request: led.generate(self, request))

    async def record_call(**kw):
        led.rows.append(kw)

    async def record_calls(rows):
        led.rows.extend(rows)

    # every sync / batch / billed-failure writer ends in one of these two (module attributes, looked up
    # at call time); record_call_with_pool is patched where a test needs it.
    monkeypatch.setattr(call_log, "record_call", record_call)
    monkeypatch.setattr(call_log, "record_calls", record_calls)
    monkeypatch.setattr(jn, "_tie_break", lambda state, generated, score: (score, None))
    monkeypatch.delenv("T2_JUDGE_REPEAT", raising=False)
    monkeypatch.delenv("T2_JUDGE_ANCHORS", raising=False)
    return led


def _resp(content, cost=0.001, model="satellite-gpt-5.6-luna", **kw) -> LLMResponse:
    return LLMResponse(content=content, model_used=model, provider="bedrock-satellite", cost_usd=cost,
                       input_tokens=100, output_tokens=20, satellite_account="acc3", **kw)


def _verdict(fit, mission=True, cost=0.001):
    return _resp(json.dumps({"brand_fit_score": fit, "cross_brand_distinct": 9, "mission_present": mission,
                             "feedback": "fb"}), cost=cost)


GEN = {"name": "Kyoto Days", "subtitle": "Quiet temples", "summary": "Calm.", "highlights": ["Nanzen-ji"],
       "itineraries": "Day 1 — Arrive Kyoto.", "seo_title": "Kyoto", "seo_meta": "A calm Kyoto trip."}
A1 = {"tour": {"tour_id": "x"}, "brand_core_idea": "Movement and freedom.",
      "brand_customer_mindset": "Wants to feel alive.", "brand_voice_examples": ["Energetic"]}
T2 = dict(A1, is_tenant_rewrite=True, tenant_id="00000000-0000-0000-0000-0000000000aa")


def _judge(state, *, quality_score=9.75):
    return jn.judge_node(dict(state, generated=GEN, quality_score=quality_score, feedback=""))


def _verdict_rows(rows):
    return [r for r in rows if "passed" in r["quality_signal"]]


# ── S1 / T2 judge ───────────────────────────────────────────────────────────────────────────────


def test_a_s1_out_of_band_is_one_row(llm_ledger):
    llm_ledger.script = [_verdict(9)]
    with patch.object(bf, "_seeded_model", return_value=False):
        out = _judge(A1)
    llm_ledger.assert_balanced()
    assert len(llm_ledger.rows) == 1 and "judge_calls" not in llm_ledger.rows[0]["quality_signal"]
    assert out["judge_brand_fit"] == 9.0


def test_b_s1_near_band_unseeded_writes_three_rows_one_verdict(llm_ledger):
    llm_ledger.script = [_verdict(7, cost=0.001), _verdict(8, cost=0.002), _verdict(6, cost=0.004)]
    with patch.object(bf, "_seeded_model", return_value=False):
        out = _judge(A1)
    llm_ledger.assert_balanced()
    assert len(llm_ledger.rows) == 3
    verdict = _verdict_rows(llm_ledger.rows)
    assert len(verdict) == 1 and verdict[0]["quality_signal"]["judge_calls"] == 3
    assert verdict[0]["cost_usd"] == pytest.approx(0.001)            # run 1's own cost
    extra = [r["quality_signal"] for r in llm_ledger.rows[1:]]
    assert [q["judge_run"] for q in extra] == [2, 3] and all(q["verdict_row"] is False for q in extra)
    assert not any(set(q) & set(call_log.OK_RATE_KEYS) for q in extra)
    assert out["cost_usd"] == pytest.approx(0.007)                   # state cost unchanged by AA-927


def test_c_t2_median3_writes_three_rows(llm_ledger, monkeypatch):
    monkeypatch.setenv("T2_JUDGE_REPEAT", "median3")
    llm_ledger.script = [_verdict(4), _verdict(8), _verdict(7)]
    _judge(T2)
    llm_ledger.assert_balanced()
    assert len(llm_ledger.rows) == 3 and len(_verdict_rows(llm_ledger.rows)) == 1
    assert {r["stage"] for r in llm_ledger.rows} == {"t2_judge"}
    assert "judge_runs" in _verdict_rows(llm_ledger.rows)[0]["quality_signal"]


def test_d_median3_with_one_billed_failure_writes_a_billed_unusable_row(llm_ledger, monkeypatch):
    monkeypatch.setenv("T2_JUDGE_REPEAT", "median3")
    llm_ledger.script = [_verdict(6), _resp("not json", cost=0.003), _verdict(8)]
    out = _judge(T2)
    llm_ledger.assert_balanced()
    unusable = [r for r in llm_ledger.rows if r["quality_signal"].get("billed_unusable")]
    assert len(llm_ledger.rows) == 3 and len(unusable) == 1
    assert unusable[0]["cost_usd"] == pytest.approx(0.003)
    assert not set(unusable[0]["quality_signal"]) & set(call_log.OK_RATE_KEYS)
    assert out["cost_usd"] == pytest.approx(0.005)


def test_e_near_band_extra_call_raising_after_a_billed_reply(llm_ledger):
    llm_ledger.script = [_verdict(7, cost=0.001), _verdict(8, cost=0.002), _resp("[1, 2]", cost=0.004)]
    with patch.object(bf, "_seeded_model", return_value=False):
        out = _judge(A1)
    llm_ledger.assert_balanced()
    assert len(llm_ledger.rows) == 3
    assert all(r["quality_signal"]["billed_unusable"] for r in llm_ledger.rows)
    assert out["quality_score"] == 9.75                               # non-blocking, as before


def test_e2_near_band_extra_call_failing_before_billing_keeps_the_earlier_rows(llm_ledger):
    llm_ledger.script = [_verdict(7), RuntimeError("route down")]
    with patch.object(bf, "_seeded_model", return_value=False):
        _judge(A1)
    llm_ledger.assert_balanced()
    assert len(llm_ledger.rows) == 1


def test_f_unparseable_judge_reply_is_one_row(llm_ledger):
    llm_ledger.script = [_resp("not json at all", cost=0.002)]
    _judge(A1)
    llm_ledger.assert_balanced()
    q = llm_ledger.rows[0]["quality_signal"]
    assert q["billed_unusable"] is True and "passed" not in q


def test_g_tie_break_raising_after_the_judge_still_writes_the_rows(llm_ledger, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("jev down")
    monkeypatch.setattr(jn, "_tie_break", boom)
    llm_ledger.script = [_verdict(7, cost=0.001), _verdict(7, cost=0.001), _verdict(7, cost=0.001)]
    with patch.object(bf, "_seeded_model", return_value=False):
        out = _judge(A1)
    llm_ledger.assert_balanced()
    assert len(llm_ledger.rows) == 3
    assert all(r["quality_signal"].get("post_call_error") for r in llm_ledger.rows)
    assert not any("passed" in r["quality_signal"] for r in llm_ledger.rows)
    assert out["quality_score"] == 9.75


# ── F9 (t10_judge) ──────────────────────────────────────────────────────────────────────────────

PIECE = "The dzong stands where two rivers meet in Punakha. Ask us about the dates."


def _f9(**kw):
    from services.acp_content_writing import quality_gates as qg
    return qg.gate_brand_voice(PIECE, "Ask us about the dates.", "rubric", source_text="seed", **kw)


def test_h_f9_unparseable_reply_is_one_row_without_verdict_keys(llm_ledger):
    llm_ledger.script = [_resp("<<< not json >>>", cost=0.002)]
    results = _f9()
    llm_ledger.assert_balanced()
    q = llm_ledger.rows[0]["quality_signal"]
    assert q["billed_unusable"] is True and q["f9_gate"] == "F9_brand_voice"
    assert "passed" not in q and "status" not in q and "gate" not in q
    assert {r["gate"] for r in results} == {"F9_cta_fact", "F9_brand_style"}


def test_i_f9_post_parse_raise_still_writes_the_row(llm_ledger):
    from services.acp_content_writing import quality_gates as qg
    llm_ledger.script = [_resp(json.dumps({"status": "pass", "failure_codes": []}), cost=0.002)]
    with patch.object(qg, "_f9_verdict_quotes", side_effect=KeyError("claim")), pytest.raises(KeyError):
        _f9()
    llm_ledger.assert_balanced()
    assert "status" not in llm_ledger.rows[0]["quality_signal"]


def test_i2_f9_success_is_one_verdict_row(llm_ledger):
    llm_ledger.script = [_resp(json.dumps({"status": "pass", "failure_codes": []}), cost=0.002)]
    _f9()
    llm_ledger.assert_balanced()
    assert llm_ledger.rows[0]["quality_signal"]["status"] == "pass"


# ── S1 writer (generate_node + itinerary nudge) ─────────────────────────────────────────────────

_SRC = "\n\n".join(f"Day {d}: Place {d}\n" + " ".join(["word"] * 100) for d in (1, 2))


def _writer_state(**kw):
    base = {
        "tour": {"name": "Two Days", "country": "Nepal", "duration": "2 Days", "itineraries": _SRC},
        "seo": {}, "few_shots": [], "generated": {}, "quality_score": 0.0, "retry_count": 0,
        "feedback": "", "error": "", "cost_usd": 0.0, "model_used": "",
        "brand_system_prompt": "", "brand_style_guide": "", "brand_forbidden_words": [],
        "rewrite_language": "en-US", "model_tier": "haiku", "subtitle_focus": "standard",
        "is_tenant_rewrite": False, "is_branded": True, "failure_codes": [], "sub_scores": {},
        "passed_count": 0, "failed_count": 0,
    }
    base.update(kw)
    return base


def _writer_output(bodies: dict) -> str:
    return json.dumps({
        "name": "Two Days", "subtitle": "2 days", "summary": "A two day trip.", "highlights": ["a", "b", "c"],
        "itineraries": [{"day": d, "title": f"Day {d}", "body": b} for d, b in sorted(bodies.items())],
        "seo_title": "Two Days", "seo_meta": "x" * 145, "trip_type": "cultural",
    })


def test_j_generate_node_post_call_exception_is_one_row_without_json_parsed(llm_ledger):
    from services.content_generation import graph
    llm_ledger.script = [_resp(_writer_output({1: " ".join(["w"] * 100), 2: " ".join(["w"] * 100)}),
                               cost=0.01, cache_read_tokens=0, cache_write_tokens=0)]
    with patch.object(graph, "_apply_seo_keywords_used", side_effect=RuntimeError("bug")):
        out = graph.generate_node(_writer_state())
    llm_ledger.assert_balanced()
    q = llm_ledger.rows[0]["quality_signal"]
    assert q["billed_unusable"] is True and "json_parsed" not in q
    assert out["error"] == "bug"


def test_k_nudge_reply_not_an_object_still_writes_the_nudge_row_and_the_writer_row(llm_ledger):
    from services.content_generation import graph
    llm_ledger.script = [_resp(_writer_output({1: "w w", 2: " ".join(["w"] * 100)}), cost=0.01),
                         _resp("[1, 2]", cost=0.0005)]
    out = graph.generate_node(_writer_state())
    llm_ledger.assert_balanced()
    stages = sorted(r["stage"] for r in llm_ledger.rows)
    assert stages == ["s1_generate", "s1_itinerary_nudge"]
    assert out["error"] == "" and out["itinerary_nudges_used"] == 1


# ── gateway ─────────────────────────────────────────────────────────────────────────────────────

SCHEMA = {"name": "verdict", "schema": {"type": "object", "required": ["score", "why"],
                                        "properties": {"score": {"type": "integer"}, "why": {"type": "string"}}}}
LUNA = CatalogModel(model_key="gpt-5.6-luna", label="Luna", vendor="openai", provider="bedrock",
                    api_style="converse", bedrock_profile_ids={"acc3": "us.openai.luna"}, enabled=True)


def _converse(*tool_inputs, tokens=(100, 10)):
    rt = MagicMock()
    rt.converse.side_effect = [
        {"output": {"message": {"content": [{"toolUse": {"name": "verdict", "input": ti}}]}},
         "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1]}, "stopReason": "tool_use"}
        for ti in tool_inputs]
    return rt


def _client():
    with patch("shared.llm_client.client.boto3"), patch("shared.llm_client.client.openai"):
        return LLMClient()


@pytest.fixture
def flat_price(monkeypatch):
    monkeypatch.setattr(LLMClient, "_calc_cost",
                        lambda self, model, i, o, cache_read=0, cache_write=0: (i + o + cache_read) / 1000)


def test_l_converse_schema_retry_ok_prices_both_requests(flat_price):
    rt = _converse({"score": 7}, {"score": 7, "why": "ok"})
    with patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt):
        resp = _client()._call_bedrock_converse(
            LLMRequest(system_prompt="s", user_prompt="u", json_schema=SCHEMA), LUNA, "acc3")
    assert rt.converse.call_count == 2 and resp.provider_requests == 2
    assert resp.input_tokens == 200 and resp.output_tokens == 20
    assert resp.cost_usd == pytest.approx(0.22)                        # both requests, one response


def test_l2_converse_retry_failing_carries_both_billed_requests(flat_price):
    rt = _converse({"score": 7}, {"score": 7})
    with patch("shared.llm_client.bedrock_satellite.get_satellite_client", return_value=rt), \
            pytest.raises(RuntimeError, match="does not match schema") as info:
        _client()._call_bedrock_converse(
            LLMRequest(system_prompt="s", user_prompt="u", json_schema=SCHEMA), LUNA, "acc3")
    (billed,) = info.value.billed_usage
    assert billed["provider_requests"] == 2 and billed["cost_usd"] == pytest.approx(0.22)


def test_m_route_fallback_writes_one_gateway_attempt_row(monkeypatch):
    rows: list = []

    async def record_calls(rows_):
        rows.extend(rows_)
    monkeypatch.setattr(call_log, "record_calls", lambda rows: record_calls(rows))
    good = _resp("ok", cost=0.002)

    def fake(request, key, *_):
        if key == "gpt-5.6-luna":
            raise _billed_unusable("returned empty content", [{
                "model": "satellite-gpt-5.6-luna", "account": "acc3", "tokens_in": 100, "tokens_out": 0,
                "cost_usd": 0.001, "stop_reason": "max_tokens", "provider_requests": 1}])
        return good
    c = _client()
    with patch("shared.llm_client.client.get_model_sync", return_value=LUNA), \
            patch.object(c, "_call_key", side_effect=fake):
        resp = c._call_route(LLMRequest(system_prompt="s", user_prompt="u", stage="t10_judge"),
                             ["gpt-5.6-luna", "gpt-6-luna"], "acc3", "acc1", role="judge")
    call_log.wait_for_pending_writes()
    assert resp is good and resp.fallback_used is True
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "t10_judge" and row["role"] == "judge" and row["cost_usd"] == 0.001
    q = row["quality_signal"]
    assert q["gateway_attempt"] is True and q["billed_unusable"] is True and q["position"] == 0
    assert not set(q) & set(call_log.OK_RATE_KEYS)


def test_m2_legacy_chain_wrapped_billed_failure_is_found_through_the_cause(monkeypatch):
    rows: list = []

    async def record_calls(rows_):
        rows.extend(rows_)
    monkeypatch.setattr(call_log, "record_calls", lambda rows: record_calls(rows))
    inner = _billed_unusable("bad json", [{"model": "gpt-4.1", "cost_usd": 0.01}])
    c = _client()

    def fake(*_a, **_k):
        raise RuntimeError("All LLM providers failed") from inner
    with patch.object(c, "_call_key", side_effect=fake), pytest.raises(RuntimeError):
        c._call_key_logged(LLMRequest(system_prompt="s", user_prompt="u", stage="s1_generate"), "haiku",
                           "acc3", "acc1", role="writer", position=0)
    call_log.wait_for_pending_writes()
    assert [r["cost_usd"] for r in rows] == [0.01]


def test_n_shadow_cost_row_lands_when_the_shadow_log_insert_fails(monkeypatch):
    from shared.llm_client import shadow
    rows = []

    async def record_call(**kw):
        rows.append(kw)

    async def connect(*_a, **_k):
        raise OSError("db down")
    monkeypatch.setattr(shadow, "record_call", record_call)
    monkeypatch.setattr(shadow.asyncpg, "connect", connect)
    monkeypatch.setattr(shadow, "get_database_url", lambda: "postgresql://x")
    shadow.record_shadow_sync(stage="s1_judge", role="judge",
                              request=LLMRequest(system_prompt="s", user_prompt="u"),
                              primary=_resp("p"), shadow_model="gpt-6-luna", shadow=_resp("s", cost=0.003),
                              error=None, latency_ms=10)
    assert len(rows) == 1 and rows[0]["cost_usd"] == 0.003 and rows[0]["quality_signal"]["shadow"] is True


def test_o_record_call_sync_under_a_running_loop_lands_with_the_bound_job(monkeypatch):
    rows = []

    async def record_call(**kw):
        rows.append((kw, call_log.current_job_id()))
    monkeypatch.setattr(call_log, "record_call", record_call)

    async def main():
        with call_log.bind_job("11111111-1111-1111-1111-111111111111"):
            call_log.record_call_sync(stage="s1_judge", role="judge", model="m", tokens_in=1, tokens_out=1,
                                      cost_usd=0.001, quality_signal={"passed": True})
    asyncio.run(main())                      # the caller's loop closes right away
    call_log.wait_for_pending_writes()
    assert len(rows) == 1 and rows[0][1] == "11111111-1111-1111-1111-111111111111"


def test_billed_unusable_rows_never_carry_ok_rate_keys():
    rows = call_log.billed_unusable_rows(stage="s", role="writer", error="x" * 500,
                                         billed=[{"model": "m", "cost_usd": 0.1}],
                                         quality_signal_extra={"passed": True, "status": "pass", "attempt": 2})
    q = rows[0]["quality_signal"]
    assert q == {"attempt": 2, "billed_unusable": True, "error": "x" * 300}
