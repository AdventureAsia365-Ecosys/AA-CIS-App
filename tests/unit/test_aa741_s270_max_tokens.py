"""AA-741 (S270) — the tenant writing stages' token ceilings for the 5.x models and the one budget retry.

Ledger evidence (S270, 7 days): t8_angle_gen cut at 2048 on 7/9 calls, t9_write at 8000 on 1/9, t9_repair at 2048 on
1/1, t9_faq_filter at 3000 on 1/1. Every stage's ceiling must stay at or above the new floor.

S271 W0 (AA-900 l13): the one retry of a cut reply is the GATEWAY's (tests/unit/test_s271_gateway_token_budget.py,
tests/unit/test_aa900_s271_w0_gateway.py). Here the fake client stands for the gateway: a reply it retried carries
``truncation_retry_max_tokens`` (+ reason and the cut attempt's cost); each call site makes ONE gateway call, writes
one row (the cut row is the gateway's) and fails a reply that is still cut.
Fixtures use invented places only; no model is called (fake clients).
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from api.routers import admin_content_quality as cq
from services.acp_angle_gate import generate as t8
from services.acp_angle_gate.generate import AngleGenerationError
from services.acp_angle_gate.goals import get_goal
from services.acp_content_writing import faq_filter
from services.acp_content_writing import generate as t9
from services.acp_content_writing.faq_filter import FaqFilterReplyError, FaqScope
from services.acp_content_writing.generate import SentenceRepairError
from services.acp_shared import token_budget as tb
from shared.llm_client.models import LLMRequest, LLMResponse

GOAL = get_goal("promotion")


def _resp(content: str, stop: str = "end_turn", cost: float = 0.01, *, retried: int | None = None,
          cut_cost: float = 0.0, reason: str = "cut_reply") -> LLMResponse:
    """A gateway reply; ``retried`` = the gateway's retry ceiling when this reply is its truncation retry."""
    r = LLMResponse(content=content, model_used="model-x", provider="bedrock", cost_usd=cost, stop_reason=stop)
    if retried:
        r.truncation_retry_max_tokens, r.truncation_retry_reason, r.truncation_cut_cost_usd = retried, reason, cut_cost
    return r


def _client(*replies) -> MagicMock:
    client = MagicMock()
    client.generate.side_effect = list(replies)
    return client


def _sizes(client: MagicMock) -> list[int]:
    return [c.args[0].max_tokens for c in client.generate.call_args_list]


# ---------------------------------------------------------------- the floors

class TestFloors:
    @pytest.mark.parametrize("stage,floor", [("t8_angle_gen", 8192), ("t9_write", 8192), ("t9_repair", 4096),
                                             ("t9_faq_filter", 6000)])
    def test_stage_floor(self, stage, floor):
        assert tb.STAGE_FLOORS[stage] >= floor

    def test_each_call_site_uses_at_least_its_floor(self):
        assert tb.T8_ANGLE_GEN_MAX_TOKENS >= 8192              # S271 W0: the effort floor of Sonnet 5.5
        assert t9.max_tokens_for("blog") >= 12000
        for channel in ("linkedin", "facebook", "instagram", "x", "email"):
            assert t9.max_tokens_for(channel) >= 8192          # S271 W0
        assert t9._MAX_TOKENS >= 4096                    # sentence repair, SEO fit, FAQ regeneration (t9_repair)
        assert cq._SUGGEST_MAX_TOKENS >= 4096            # admin suggestion (t9_repair)
        assert faq_filter.MAX_TOKENS >= 6000

    def test_repair_defaults_use_the_repair_ceiling(self):
        import inspect
        for fn in (t9.repair_sentences, t9.repair_sentences_detailed):
            assert inspect.signature(fn).parameters["max_tokens"].default >= tb.T9_REPAIR_MAX_TOKENS

    @pytest.mark.parametrize("first,retry", [(2048, 4096), (4096, 8192), (12000, 24000), (16000, 24000),
                                             (30000, 30000)])
    def test_retry_budget_doubles_capped_never_below_first(self, first, retry):
        assert tb.retry_budget(first) == retry


# ---------------------------------------------------------------- the helper

class TestHelper:
    def _req(self, **kw) -> LLMRequest:
        return LLMRequest(system_prompt="s", user_prompt="u", stage="t9_repair", max_tokens=2048, **kw)

    def test_complete_reply_one_call(self):
        client = _client(_resp("ok"))
        reply = tb.generate_with_budget_retry(client.generate, self._req())
        assert client.generate.call_count == 1 and not reply.retried and reply.signal() == {}
        assert reply.cost_usd == pytest.approx(0.01)

    def test_the_request_goes_to_the_gateway_unchanged(self):
        """S271 W0: no opt-out of the gateway retry, no caller-side ceiling change."""
        client = _client(_resp("ok"))
        tb.generate_with_budget_retry(client.generate, self._req(temperature=0.0, model_tier="tier-q"))
        sent = client.generate.call_args.args[0]
        assert sent.budget_retry is True and sent.max_tokens == 2048 and "budget_retry" not in sent.model_fields_set

    def test_a_gateway_retried_reply_carries_the_retry_facts(self):
        client = _client(_resp("ok", "end_turn", 0.03, retried=4096, cut_cost=0.02, reason="billed_unusable_cut"))
        reply = tb.generate_with_budget_retry(client.generate, self._req())
        assert client.generate.call_count == 1 and reply.retried and not reply.still_cut
        assert reply.retry_max_tokens == 4096 and reply.retry_reason == "billed_unusable_cut"
        assert reply.cost_usd == pytest.approx(0.05)                 # the reply + the cut attempt it replaced
        assert reply.signal() == {"truncation_retry": True, "max_tokens": 4096, "retry_reason": "billed_unusable_cut"}

    def test_still_cut_after_the_gateway_retry(self):
        client = _client(_resp("cut again", "length", retried=4096))
        reply = tb.generate_with_budget_retry(client.generate, self._req())
        assert client.generate.call_count == 1 and reply.still_cut and reply.retried
        assert "twice" in str(tb.ReplyStillCut("t9_repair", reply.max_tokens, reply.retry_max_tokens))

    def test_cut_with_no_retry_says_so(self):
        """At the model's output cap or past the route deadline the gateway sends no retry: the message says so."""
        reply = tb.generate_with_budget_retry(_client(_resp("cut", "max_tokens")).generate, self._req())
        assert reply.still_cut and not reply.retried and reply.retry_max_tokens is None
        assert "no retry" in str(tb.ReplyStillCut("t9_repair", reply.max_tokens, reply.retry_max_tokens))

    def test_errors_propagate(self):
        client = _client(RuntimeError("AccessDeniedException"), _resp("never"))
        with pytest.raises(RuntimeError):
            tb.generate_with_budget_retry(client.generate, self._req())
        assert client.generate.call_count == 1

    def test_async_twin(self):
        client = _client(_resp("ok", retried=4096))
        reply = asyncio.run(tb.agenerate_with_budget_retry(client.generate, self._req()))
        assert _sizes(client) == [2048] and reply.resp.content == "ok" and reply.retried

    def test_still_cut_message_names_both_ceilings(self):
        assert "4096" in str(tb.ReplyStillCut("t8_angle_gen", 4096, 8192)) and "8192" in str(
            tb.ReplyStillCut("t8_angle_gen", 4096, 8192))


# ---------------------------------------------------------------- T8 angles

_ANGLES = {"angles": [{"name": n, "why_it_works": "w", "formula_fit": "f", "best_final_style": "s"}
                      for n in ("Ridge walk", "Lantern market", "River ferry")],
           "recommended_index": 0, "recommendation_reason": "r"}
_CUT_ANGLES = '{"angles": [{"name": "Ridge walk", "why_it_works": "w", "formula_fit": "f", "best_final_sty'


class TestT8:
    def _run(self, client):
        rec, unusable = AsyncMock(), AsyncMock()
        with patch.object(t8, "LLMClient", return_value=client), patch.object(t8, "record_call", rec), \
                patch.object(t8, "record_unusable", unusable):
            try:
                out = asyncio.run(t8.generate_angles(content_seed="Ferry to Tamsel Isle", goal=GOAL,
                                                     brand_audience={}, tenant_id="t-1", request_id="r-1"))
            except AngleGenerationError as e:
                out = e
        return out, rec, unusable

    def test_gateway_retried_then_ok(self):
        client = _client(_resp(json.dumps(_ANGLES), "end_turn", 0.03, retried=16384, cut_cost=0.02))
        (angles, _, _, cost), rec, unusable = self._run(client)
        assert len(angles) == 3 and cost == pytest.approx(0.05)
        assert _sizes(client) == [8192]                           # S271 W0: T8 at its effort floor, one call
        assert rec.await_count == 1 and unusable.await_count == 0  # the cut row is the gateway's
        ok_row = rec.await_args.kwargs
        assert ok_row["quality_signal"]["truncation_retry"] is True and ok_row["quality_signal"]["max_tokens"] == 16384
        assert ok_row["angle_gate_request_id"] == "r-1" and ok_row["tenant_id"] == "t-1"

    def test_cut_after_the_gateway_retry_is_a_clear_failure(self):
        client = _client(_resp(_CUT_ANGLES, "max_tokens", retried=16384), _resp("never"))
        err, rec, unusable = self._run(client)
        assert isinstance(err, AngleGenerationError) and "twice" in str(err)
        assert client.generate.call_count == 1
        assert rec.await_count == 0 and unusable.await_count == 1   # the billed reply is in the ledger

    def test_a_salvageable_cut_reply_is_not_used(self):
        # json_repair could salvage a cut reply, but its last field may be cut mid-word: it fails clearly
        client = _client(_resp(json.dumps(_ANGLES)[:-2], "max_tokens"))
        err, _, unusable = self._run(client)
        assert isinstance(err, AngleGenerationError) and unusable.await_count == 1


# ---------------------------------------------------------------- T9 write

_KW = dict(content_seed="Ferry to Tamsel Isle — visit", goal=GOAL, brand_audience={},
           angle={"name": "a", "why_it_works": "w", "best_final_style": "s"}, cta="Ask us")


class TestT9Write:
    def test_gateway_retried_reply_one_row(self):
        from services.acp_angle_gate.channel_style import get_channel_style
        client = _client(_resp("Full post.", "end_turn", 0.2, retried=16384, cut_cost=0.1))
        with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync") as log:
            text, cost, _, _ = t9.write_content(channel_style=get_channel_style("linkedin"), **_KW)
        assert text == "Full post." and cost == pytest.approx(0.3)
        assert _sizes(client) == [8192]                            # S271 W0: the short channels at 8192
        (ok,) = (c.kwargs["quality_signal"] for c in log.call_args_list)
        assert ok["truncation_retry"] is True and ok["retry_reason"] == "cut_reply"

    def test_cut_after_the_retry_returns_the_cut_text_no_second_call(self):
        from services.acp_angle_gate.channel_style import get_channel_style
        client = _client(_resp("cut again", "max_tokens", retried=16384), _resp("never"))
        with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync") as log:
            text, *_ = t9.write_content(channel_style=get_channel_style("facebook"), **_KW)
        assert text == "cut again" and client.generate.call_count == 1 and log.call_count == 1


# ---------------------------------------------------------------- T9 repair (sentence repair, SEO fit, FAQ regen)

_ITEMS = [{"sentence": "The ferry leaves at dawn.", "problem": "time not in source"}]


class TestT9Repair:
    def test_sentence_repair_gateway_retried_then_ok(self):
        client = _client(_resp('{"replacements": ["The ferry crosses to Tamsel Isle."]}', retried=16384,
                               cut_cost=0.01))
        with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync") as log:
            reps, cost = t9.repair_sentences(sentences=_ITEMS, content_seed="seed", channel="blog",
                                             stage="t9_repair")
        assert reps == ["The ferry crosses to Tamsel Isle."] and cost == pytest.approx(0.02)
        assert _sizes(client) == [8192] and log.call_count == 1      # S271: T9_REPAIR_MAX_TOKENS 8192

    def test_sentence_repair_still_cut_raises(self):
        client = _client(_resp('{"replacements": ["b', "max_tokens", retried=16384), _resp("never"))
        with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync") as log:
            with pytest.raises(SentenceRepairError, match="twice"):
                t9.repair_sentences(sentences=_ITEMS, content_seed="seed", channel="blog", stage="t9_repair")
        assert client.generate.call_count == 1 and log.call_count == 1

    def test_seo_fit_still_cut_gives_nothing(self):
        client = _client(_resp('{"seo_title": "Ferry days on Tamsel Is', "max_tokens", retried=16384,
                               cut_cost=0.01))
        with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync"):
            out, cost = t9.fit_seo_fields(body="Body.", seo_title="t", meta_description="m", keyword="tamsel ferry")
        assert out == {"seo_title": None, "meta_description": None} and cost == pytest.approx(0.02)

    def test_faq_regen_gateway_retried_then_ok(self):
        good = json.dumps({"faq": [{"q": "How long is the Tamsel ferry?", "a": "About an hour each way."}]})
        client = _client(_resp(good, retried=16384))
        with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync") as log:
            pairs, _ = t9.write_faq(source="The ferry takes about an hour.", body="Body.")
        assert pairs == [("How long is the Tamsel ferry?", "About an hour each way.")]
        assert log.call_args.kwargs["quality_signal"]["truncation_retry"] is True

    def test_faq_regen_still_cut_gives_no_pairs(self):
        cut = '{"faq": [{"q": "How long is the ferry?", "a": "About an h'
        client = _client(_resp(cut, "max_tokens", retried=16384))
        with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync"):
            pairs, _ = t9.write_faq(source="s", body="Body.")
        assert pairs == []


# ---------------------------------------------------------------- T9 FAQ filter

_SCOPE = FaqScope(country="Norvania", trip="Isles by ferry", scope_text="Tamsel Isle ferry", places=("Tamsel Isle",),
                  source_text="The ferry runs daily.")
_QS = ["How long is the Tamsel Isle ferry?", "Is the Tamsel Isle ferry daily?"]
_VERDICTS = json.dumps({"items": [{"n": i, "label": "KEEP", "reason_code": "on_subject", "confidence": "high"}
                                  for i in (1, 2)]})


class TestFaqFilter:
    def test_gateway_retried_then_ok(self):
        client = _client(_resp(_VERDICTS, cost=0.02, retried=12000, cut_cost=0.01))
        with patch.object(faq_filter, "record_call_sync") as log, patch.object(faq_filter, "record_unusable_sync"):
            out = faq_filter.llm_filter_questions(_QS, _SCOPE, client=client)
        assert out.kept == _QS and out.cost_usd == pytest.approx(0.03)
        assert _sizes(client) == [6000]
        (ok,) = (c.kwargs["quality_signal"] for c in log.call_args_list)
        assert ok["json_parsed"] is True and ok["truncation_retry"] is True

    def test_still_cut_raises_falls_back(self):
        cut = '{"items": [{"n": 1, "label": "KE'
        client = _client(_resp(cut, "max_tokens", retried=12000), _resp("never"))
        with patch.object(faq_filter, "record_call_sync") as log, \
                patch.object(faq_filter, "record_unusable_sync") as unusable:
            with pytest.raises(FaqFilterReplyError, match="twice"):
                faq_filter.llm_filter_questions(_QS, _SCOPE, client=client)
        assert client.generate.call_count == 1 and log.call_count == 0 and unusable.call_count == 1
