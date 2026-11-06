"""AA-741 (S270) — the tenant writing stages' token ceilings for the 5.x models and the one budget retry.

Ledger evidence (S270, 7 days): t8_angle_gen cut at 2048 on 7/9 calls, t9_write at 8000 on 1/9, t9_repair at 2048 on
1/1, t9_faq_filter at 3000 on 1/1. Every stage's ceiling must stay at or above the new floor, and a cut reply is asked
once more with double the ceiling (capped), each billed reply written to the ledger, never a third call.
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
from shared.llm_client.call_log import OK_RATE_KEYS
from shared.llm_client.models import LLMRequest, LLMResponse

GOAL = get_goal("promotion")


def _resp(content: str, stop: str = "end_turn", cost: float = 0.01) -> LLMResponse:
    return LLMResponse(content=content, model_used="model-x", provider="bedrock", cost_usd=cost, stop_reason=stop)


def _client(*replies) -> MagicMock:
    client = MagicMock()
    client.generate.side_effect = list(replies)
    return client


def _sizes(client: MagicMock) -> list[int]:
    return [c.args[0].max_tokens for c in client.generate.call_args_list]


class _CutRefusal(Exception):
    """A provider that refused to return a reply cut at its ceiling (the OpenAI 400 text the gateway classifies)."""

    def __init__(self):
        super().__init__("Could not parse response content as the length limit was reached")


# ---------------------------------------------------------------- the floors

class TestFloors:
    @pytest.mark.parametrize("stage,floor", [("t8_angle_gen", 4096), ("t9_write", 4096), ("t9_repair", 4096),
                                             ("t9_faq_filter", 6000)])
    def test_stage_floor(self, stage, floor):
        assert tb.STAGE_FLOORS[stage] >= floor

    def test_each_call_site_uses_at_least_its_floor(self):
        assert tb.T8_ANGLE_GEN_MAX_TOKENS >= 4096
        assert t9.max_tokens_for("blog") >= 12000
        for channel in ("linkedin", "facebook", "instagram", "x", "email"):
            assert t9.max_tokens_for(channel) >= 4096
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

    def test_cut_then_ok_retries_once_with_double(self):
        client = _client(_resp("cut", "max_tokens", 0.02), _resp("ok", "end_turn", 0.03))
        seen = []
        reply = tb.generate_with_budget_retry(client.generate, self._req(temperature=0.0, model_tier="tier-q"),
                                              on_cut=lambda r, m, rm: seen.append((r.content, m, rm)))
        assert _sizes(client) == [2048, 4096]
        assert seen == [("cut", 2048, 4096)]          # the cut reply's row is written before the retry
        assert reply.resp.content == "ok" and reply.retried and not reply.still_cut
        assert reply.cost_usd == pytest.approx(0.05)
        assert reply.signal() == {"truncation_retry": True, "max_tokens": 4096}
        retry = client.generate.call_args_list[1].args[0]
        assert retry.temperature == 0.0 and retry.model_tier == "tier-q" and retry.stage == "t9_repair"
        assert "max_tokens" in retry.model_fields_set   # a stage_model_params row never overrides the retry

    def test_cut_twice_no_third_call(self):
        client = _client(_resp("cut", "max_tokens"), _resp("cut again", "length"), _resp("never"))
        reply = tb.generate_with_budget_retry(client.generate, self._req())
        assert client.generate.call_count == 2 and reply.still_cut

    def test_cut_row_written_even_when_the_retry_raises(self):
        client = _client(_resp("cut", "max_tokens"), RuntimeError("provider down"))
        seen = []
        with pytest.raises(RuntimeError):
            tb.generate_with_budget_retry(client.generate, self._req(), on_cut=lambda *a: seen.append(a))
        assert len(seen) == 1

    def test_truncated_refusal_retried_once(self):
        client = _client(_CutRefusal(), _resp("ok"))
        reply = tb.generate_with_budget_retry(client.generate, self._req())
        assert _sizes(client) == [2048, 4096] and reply.retried and reply.cut_first is None

    def test_truncated_refusal_twice_raises_no_loop(self):
        client = _client(_CutRefusal(), _CutRefusal(), _resp("never"))
        with pytest.raises(_CutRefusal):
            tb.generate_with_budget_retry(client.generate, self._req())
        assert client.generate.call_count == 2

    def test_other_error_not_retried(self):
        client = _client(RuntimeError("AccessDeniedException"), _resp("never"))
        with pytest.raises(RuntimeError):
            tb.generate_with_budget_retry(client.generate, self._req())
        assert client.generate.call_count == 1

    def test_async_twin(self):
        client = _client(_resp("cut", "max_tokens"), _resp("ok"))
        on_cut = AsyncMock()
        reply = asyncio.run(tb.agenerate_with_budget_retry(client.generate, self._req(), on_cut=on_cut))
        assert _sizes(client) == [2048, 4096] and reply.resp.content == "ok"
        on_cut.assert_awaited_once()

    def test_cut_signal_has_no_ok_rate_key(self):
        assert not set(tb.cut_signal(2048, 4096)) & set(OK_RATE_KEYS)

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

    def test_truncated_then_ok(self):
        client = _client(_resp(_CUT_ANGLES, "max_tokens", 0.02), _resp(json.dumps(_ANGLES), "end_turn", 0.03))
        (angles, _, _, cost), rec, unusable = self._run(client)
        assert len(angles) == 3 and cost == pytest.approx(0.05)
        assert _sizes(client) == [4096, 8192]
        assert rec.await_count == 2 and unusable.await_count == 0
        cut_row, ok_row = (c.kwargs for c in rec.await_args_list)
        assert cut_row["stop_reason"] == "max_tokens" and cut_row["quality_signal"]["budget_cut"] is True
        assert not set(cut_row["quality_signal"]) & set(OK_RATE_KEYS)
        assert cut_row["angle_gate_request_id"] == "r-1" and cut_row["tenant_id"] == "t-1"
        assert ok_row["quality_signal"]["truncation_retry"] is True and ok_row["quality_signal"]["max_tokens"] == 8192

    def test_truncated_twice_clear_failure(self):
        client = _client(_resp(_CUT_ANGLES, "max_tokens"), _resp(_CUT_ANGLES, "max_tokens"), _resp("never"))
        err, rec, unusable = self._run(client)
        assert isinstance(err, AngleGenerationError) and "twice" in str(err)
        assert client.generate.call_count == 2
        assert rec.await_count == 1 and unusable.await_count == 1   # both billed replies are in the ledger

    def test_salvaged_cut_reply_is_still_retried(self):
        # json_repair could salvage a cut reply, but the last field may be cut mid-word: the retry runs anyway
        client = _client(_resp(json.dumps(_ANGLES)[:-2], "max_tokens"), _resp(json.dumps(_ANGLES)))
        (angles, *_), _, _ = self._run(client)
        assert client.generate.call_count == 2 and len(angles) == 3


# ---------------------------------------------------------------- T9 write

_KW = dict(content_seed="Ferry to Tamsel Isle — visit", goal=GOAL, brand_audience={},
           angle={"name": "a", "why_it_works": "w", "best_final_style": "s"}, cta="Ask us")


class TestT9Write:
    def test_cut_row_then_retry_row(self):
        from services.acp_angle_gate.channel_style import get_channel_style
        client = _client(_resp("cut", "max_tokens", 0.1), _resp("Full post.", "end_turn", 0.2))
        with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync") as log:
            text, cost, _, _ = t9.write_content(channel_style=get_channel_style("linkedin"), **_KW)
        assert text == "Full post." and cost == pytest.approx(0.3)
        cut, ok = (c.kwargs["quality_signal"] for c in log.call_args_list)
        assert cut["budget_cut"] is True and cut["retry_max_tokens"] == 8192
        assert ok["truncation_retry"] is True

    def test_cut_twice_returns_the_cut_text_no_third_call(self):
        from services.acp_angle_gate.channel_style import get_channel_style
        client = _client(_resp("cut", "max_tokens"), _resp("cut again", "max_tokens"), _resp("never"))
        with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync") as log:
            text, *_ = t9.write_content(channel_style=get_channel_style("facebook"), **_KW)
        assert text == "cut again" and client.generate.call_count == 2 and log.call_count == 2


# ---------------------------------------------------------------- T9 repair (sentence repair, SEO fit, FAQ regen)

_ITEMS = [{"sentence": "The ferry leaves at dawn.", "problem": "time not in source"}]


class TestT9Repair:
    def test_sentence_repair_truncated_then_ok(self):
        client = _client(_resp('{"replacements": ["The fer', "max_tokens"),
                         _resp('{"replacements": ["The ferry crosses to Tamsel Isle."]}'))
        with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync") as log:
            reps, cost = t9.repair_sentences(sentences=_ITEMS, content_seed="seed", channel="blog",
                                             stage="t9_repair")
        assert reps == ["The ferry crosses to Tamsel Isle."] and cost == pytest.approx(0.02)
        assert _sizes(client) == [8192, 16384] and log.call_count == 2      # S271: T9_REPAIR_MAX_TOKENS 8192

    def test_sentence_repair_truncated_twice_raises(self):
        client = _client(_resp('{"replacements": ["a', "max_tokens"), _resp('{"replacements": ["b', "max_tokens"),
                         _resp("never"))
        with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync") as log:
            with pytest.raises(SentenceRepairError, match="twice"):
                t9.repair_sentences(sentences=_ITEMS, content_seed="seed", channel="blog", stage="t9_repair")
        assert client.generate.call_count == 2 and log.call_count == 2

    def test_seo_fit_truncated_twice_gives_nothing(self):
        client = _client(_resp('{"seo_title": "Ferry days on Tams', "max_tokens"),
                         _resp('{"seo_title": "Ferry days on Tamsel Is', "max_tokens"))
        with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync"):
            out, cost = t9.fit_seo_fields(body="Body.", seo_title="t", meta_description="m", keyword="tamsel ferry")
        assert out == {"seo_title": None, "meta_description": None} and cost == pytest.approx(0.02)

    def test_faq_regen_truncated_then_ok(self):
        good = json.dumps({"faq": [{"q": "How long is the Tamsel ferry?", "a": "About an hour each way."}]})
        client = _client(_resp('{"faq": [{"q": "How long', "max_tokens"), _resp(good))
        with patch.object(t9, "LLMClient", return_value=client), patch.object(t9, "record_call_sync") as log:
            pairs, _ = t9.write_faq(source="The ferry takes about an hour.", body="Body.")
        assert pairs == [("How long is the Tamsel ferry?", "About an hour each way.")]
        assert log.call_args_list[0].kwargs["quality_signal"]["budget_cut"] is True

    def test_faq_regen_truncated_twice_gives_no_pairs(self):
        cut = '{"faq": [{"q": "How long is the ferry?", "a": "About an h'
        client = _client(_resp(cut, "max_tokens"), _resp(cut, "max_tokens"))
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
    def test_truncated_then_ok(self):
        client = _client(_resp('{"items": [{"n": 1, "label": "KE', "max_tokens", 0.01), _resp(_VERDICTS, cost=0.02))
        with patch.object(faq_filter, "record_call_sync") as log, patch.object(faq_filter, "record_unusable_sync"):
            out = faq_filter.llm_filter_questions(_QS, _SCOPE, client=client)
        assert out.kept == _QS and out.cost_usd == pytest.approx(0.03)
        assert _sizes(client) == [6000, 12000]
        cut, ok = (c.kwargs["quality_signal"] for c in log.call_args_list)
        assert cut["budget_cut"] is True and "json_parsed" not in cut
        assert ok["json_parsed"] is True and ok["truncation_retry"] is True

    def test_truncated_twice_raises_falls_back(self):
        cut = '{"items": [{"n": 1, "label": "KE'
        client = _client(_resp(cut, "max_tokens"), _resp(cut, "max_tokens"), _resp("never"))
        with patch.object(faq_filter, "record_call_sync") as log, \
                patch.object(faq_filter, "record_unusable_sync") as unusable:
            with pytest.raises(FaqFilterReplyError, match="twice"):
                faq_filter.llm_filter_questions(_QS, _SCOPE, client=client)
        assert client.generate.call_count == 2 and log.call_count == 1 and unusable.call_count == 1
