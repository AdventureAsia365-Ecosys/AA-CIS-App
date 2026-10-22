"""AA-779 (S247, ADR 0011) — the optional LLM answer-fit check of the final blog FAQ (faq_answer_fit.py).

Same flag as the question filter (T9_FAQ_LLM_FILTER): off = no call, FAQ unchanged; shadow = verdicts recorded,
FAQ unchanged; on = pairs marked NON_ANSWER / WRONG_SUBJECT dropped, the rest kept, below the minimum recorded
(never a hold, never a regeneration). Any gateway error, unusable reply or timeout leaves the FAQ unchanged. Every
reply writes one llm_call_log row on stage t9_faq_answer_fit, role validate (AA-927 shape).
"""
import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from services.acp_content_writing import faq_answer_fit as fit
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service as svc
from shared.llm_client.models import LLMResponse
from shared.llm_client.role_config import SAFE_DEFAULTS

BODY = "## Paro valley\n\nThe valley opens below the dzong.\n\nBook your trip with us."
PAIRS = [
    ("How high is Paro?", "Paro sits at about 2,200 metres above sea level in western Bhutan."),
    ("What is Paro called in English?", "Paro refers to both the district and the main town in the valley."),
    ("Is Paro worth visiting?", "Yes. Paro has the Tiger's Nest monastery and the valley's old farmhouses."),
    # writer-invented (in no search-question pool): still checked
    ("What is the Royal Academy of Performing Arts?", "The institute trains students in traditional crafts."),
]
TEXT = qg.replace_faq_section(BODY, PAIRS)
CONTEXT = {"tenant_id": "t-1", "channel": "blog", "search_questions": [{"q": "How high is Paro?",
                                                                        "source": "place_paa"}]}


def _reply(verdicts, **extra):
    items = [{"n": i, "verdict": v, "reason": "x"} for i, v in enumerate(verdicts, 1)]
    return LLMResponse(content=json.dumps({"items": items}), model_used="satellite-haiku-4-5", provider="bedrock",
                       input_tokens=700, output_tokens=90, cost_usd=0.0012, satellite_account="acc3", **extra)


def _client(resp=None, exc=None):
    client = MagicMock()
    if exc is not None:
        client.generate.side_effect = exc
    else:
        client.generate.return_value = resp
    return client


FAIL_TWO = ["ANSWERS", "NON_ANSWER", "ANSWERS", "WRONG_SUBJECT"]


def _apply(mode, client=None, text=TEXT, with_unusable=False):
    with patch.object(fit, "LLMClient", return_value=client or _client(_reply(FAIL_TWO))), \
            patch.object(fit, "record_call_sync") as rec, patch.object(fit, "record_unusable_sync") as unusable:
        out = asyncio.run(fit.apply_answer_fit(text, context=CONTEXT, piece_id="p-1", request_id="r-1", mode=mode))
    return (out, rec, unusable) if with_unusable else (out, rec)


# ---------------------------------------------------------------- flag

def test_flag_off_leaves_the_faq_and_calls_nothing(monkeypatch):
    monkeypatch.delenv("T9_FAQ_LLM_FILTER", raising=False)
    client = _client(_reply(FAIL_TWO))
    (text, record), rec = _apply(None, client=client)
    assert text == TEXT and record == {}
    client.generate.assert_not_called()
    rec.assert_not_called()


def test_the_flag_is_the_question_filter_flag(monkeypatch):
    monkeypatch.setenv("T9_FAQ_LLM_FILTER", "shadow")
    client = _client(_reply(FAIL_TWO))
    (text, record), _ = _apply(None, client=client)
    assert record["mode"] == "shadow" and text == TEXT
    client.generate.assert_called_once()


# ---------------------------------------------------------------- shadow / on

def test_shadow_records_verdicts_and_delivers_unchanged():
    (text, record), rec = _apply("shadow")
    assert text == TEXT
    assert record["fallback"] is False and record["stage"] == "t9_faq_answer_fit" and record["pairs"] == 4
    assert [v["verdict"] for v in record["verdicts"]] == FAIL_TWO
    assert record["failing"] == [PAIRS[1][0], PAIRS[3][0]]
    assert "dropped" not in record
    assert record["model"] == "satellite-haiku-4-5" and record["cost_usd"] == 0.0012
    rec.assert_called_once()


def test_on_drops_only_the_failing_pairs_including_writer_questions():
    (text, record), _ = _apply("on")
    assert qg.faq_pairs(text) == [PAIRS[0], PAIRS[2]]
    assert text.partition("## FAQ")[0] == TEXT.partition("## FAQ")[0]  # the body is untouched
    assert record["dropped"] == [PAIRS[1][0], PAIRS[3][0]]  # PAIRS[3] is a writer-invented question
    assert record["pairs_after"] == 2 and record["below_min"] is True


def test_on_with_every_pair_passing_changes_nothing():
    (text, record), _ = _apply("on", client=_client(_reply(["ANSWERS"] * 4)))
    assert text == TEXT and record["failing"] == [] and "dropped" not in record


def test_on_at_or_above_the_minimum_is_not_below_min():
    text5 = qg.replace_faq_section(BODY, PAIRS + [("When is the Paro festival?", "In spring, usually March.")])
    (text, record), _ = _apply("on", client=_client(_reply(["ANSWERS", "NON_ANSWER", "ANSWERS", "ANSWERS",
                                                            "ANSWERS"])), text=text5)
    assert qg.faq_pair_count(text) == 4 and record["below_min"] is False


def test_no_faq_means_no_call():
    client = _client(_reply([]))
    (text, record), rec = _apply("on", client=client, text=BODY)
    assert text == BODY and record == {"mode": "on", "fit_mode": fit.FIT_MODE, "stage": "t9_faq_answer_fit",
                                      "pairs": 0}
    client.generate.assert_not_called()


def test_one_call_per_piece_with_every_pair_numbered():
    client = _client(_reply(FAIL_TWO))
    _apply("on", client=client)
    client.generate.assert_called_once()
    req = client.generate.call_args.args[0]
    assert req.stage == fit.FIT_STAGE and req.model_tier is None and req.temperature == 0.0
    for n, (q, a) in enumerate(PAIRS, 1):
        assert f"{n}. Q: {q}" in req.user_prompt and f"   A: {a}" in req.user_prompt


def test_pairs_past_the_cap_are_never_sent_or_dropped(monkeypatch):
    monkeypatch.setattr(fit, "FIT_MAX_PAIRS", 2)
    (text, record), _ = _apply("on", client=_client(_reply(["ANSWERS", "NON_ANSWER"])))
    assert record["dropped"] == [PAIRS[1][0]]
    assert qg.faq_pairs(text) == [PAIRS[0], PAIRS[2], PAIRS[3]]


# ---------------------------------------------------------------- on: the drop cuts blocks, nothing moves

STRAY = "It sits at 2,200 m and the town is the gateway to the valley."
MULTI = (f"{BODY}\n\n## FAQ\n\n**Q: {PAIRS[0][0]}**\nA: {PAIRS[0][1]}\n\n"
         f"**Q: {PAIRS[1][0]}**\nA: {PAIRS[1][1]}\n\n{STRAY}\n\n"
         f"**Q: {PAIRS[2][0]}**\nA: {PAIRS[2][1]}\n\nThe dzong is open on weekdays.\n\n"
         f"**Q: {PAIRS[3][0]}**\nA: {PAIRS[3][1]}\n")


def test_on_drop_takes_a_dropped_answers_later_paragraph_with_it():
    # S247 review: replace_faq_section moved a dropped pair's second paragraph above "## FAQ" (into the body).
    (text, record), _ = _apply("on", text=MULTI)
    assert STRAY not in text
    assert text.partition("## FAQ")[0] == MULTI.partition("## FAQ")[0]  # the body is untouched
    assert qg.faq_pairs(text) == [PAIRS[0], PAIRS[2]]
    assert record["dropped"] == [PAIRS[1][0], PAIRS[3][0]] and "kept_multi_paragraph" not in record


def test_on_drop_keeps_every_kept_pair_verbatim_in_place():
    (text, _), _ = _apply("on", text=MULTI)
    faq = text.partition("## FAQ")[2]
    # a kept pair's own later paragraph stays under it, inside the FAQ
    assert f"A: {PAIRS[2][1]}\n\nThe dzong is open on weekdays." in faq
    assert text == (f"{BODY}\n\n## FAQ\n\n**Q: {PAIRS[0][0]}**\nA: {PAIRS[0][1]}\n\n"
                    f"**Q: {PAIRS[2][0]}**\nA: {PAIRS[2][1]}\n\nThe dzong is open on weekdays.\n")


def test_on_keeps_a_failing_last_pair_whose_tail_may_be_a_cta():
    text_cta = TEXT.rstrip() + "\n\nBook the Paro valley trip with us today.\n"
    (text, record), _ = _apply("on", text=text_cta)
    assert "Book the Paro valley trip with us today." in text.partition("## FAQ")[2]
    assert [q for q, _a in qg.faq_pairs(text)] == [PAIRS[0][0], PAIRS[2][0], PAIRS[3][0]]
    assert record["dropped"] == [PAIRS[1][0]] and record["kept_multi_paragraph"] == [PAIRS[3][0]]
    assert record["pairs_after"] == 3 and record["below_min"] is False
    lines = fit.fit_log_lines(record)
    assert any("dropped" in v and PAIRS[1][0] in v for v in lines)
    assert any("kept FAQ pair" in v and PAIRS[3][0] in v for v in lines)
    assert not any("dropped" in v and PAIRS[3][0] in v for v in lines)


def test_on_dropping_every_pair_removes_the_heading_and_keeps_the_body():
    (text, record), _ = _apply("on", client=_client(_reply(["NON_ANSWER"] * 4)))
    assert "## FAQ" not in text and text == BODY + "\n"
    assert record["pairs_after"] == 0 and record["below_min"] is True


def test_drop_faq_pairs_with_nothing_to_drop_is_the_identity():
    assert qg.drop_faq_pairs(MULTI, set()) == (MULTI, [])
    assert qg.drop_faq_pairs(BODY, {1}) == (BODY, [])


# ---------------------------------------------------------------- fail open

def test_gateway_error_leaves_the_faq_unchanged():
    (text, record), rec, unusable = _apply("on", client=_client(exc=RuntimeError("bedrock down")),
                                           with_unusable=True)
    assert text == TEXT and record["fallback"] is True and "bedrock down" in record["error"]
    rec.assert_not_called()
    unusable.assert_not_called()  # the gateway logs its own billed failures


def test_unusable_reply_leaves_the_faq_unchanged_and_writes_a_billed_unusable_row():
    bad = LLMResponse(content="not json", model_used="satellite-haiku-4-5", provider="bedrock", cost_usd=0.001)
    (text, record), rec, unusable = _apply("on", client=_client(bad), with_unusable=True)
    assert text == TEXT and record["fallback"] is True and "FitReplyError" in record["error"]
    rec.assert_not_called()
    args, kw = unusable.call_args
    assert args[0] is bad and kw["stage"] == "t9_faq_answer_fit" and kw["role"] == "validate"
    assert kw["content_piece_id"] == "p-1" and kw["angle_gate_request_id"] == "r-1" and kw["tenant_id"] == "t-1"


def test_timeout_leaves_the_faq_unchanged(monkeypatch):
    monkeypatch.setattr(fit, "TIMEOUT_S", 0.05)

    def slow(*_a, **_k):
        import time
        time.sleep(0.3)
        return fit.FitOutcome(verdicts=[{"n": 1, "q": "x", "verdict": "NON_ANSWER", "reason": ""}])
    with patch.object(fit, "check_answer_fit", side_effect=slow):
        text, record = asyncio.run(fit.apply_answer_fit(TEXT, context=CONTEXT, mode="on"))
    assert text == TEXT and record["fallback"] is True and "Timeout" in record["error"]


# ---------------------------------------------------------------- call-log row

def test_usable_reply_writes_one_row_on_the_stage_and_role():
    (_text, _record), rec = _apply("on")
    rec.assert_called_once()
    kw = rec.call_args.kwargs
    assert kw["stage"] == "t9_faq_answer_fit" and kw["role"] == "validate"
    assert kw["model"] == "satellite-haiku-4-5" and kw["cost_usd"] == 0.0012
    assert kw["tokens_in"] == 700 and kw["tokens_out"] == 90 and kw["account"] == "acc3"
    assert kw["content_piece_id"] == "p-1" and kw["tenant_id"] == "t-1"
    qs = kw["quality_signal"]
    assert qs["json_parsed"] is True and qs["issue"] == "AA-779" and qs["mode"] == "on" and qs["n_pairs"] == 4
    assert qs["verdicts"] == {"ANSWERS": 2, "NON_ANSWER": 1, "WRONG_SUBJECT": 1}


def test_adhoc_stage_and_model_tier_pass_through():
    client = _client(_reply(["ANSWERS"]))
    with patch.object(fit, "record_call_sync") as rec:
        out = fit.check_answer_fit(PAIRS[:1], stage="adhoc_aa779", model_tier="haiku", client=client)
    req = client.generate.call_args.args[0]
    assert req.stage == "adhoc_aa779" and req.model_tier == "haiku"
    assert rec.call_args.kwargs["stage"] == "adhoc_aa779" and rec.call_args.kwargs["role"] == "validate"
    assert out.verdicts == [{"n": 1, "q": PAIRS[0][0], "verdict": "ANSWERS", "reason": "x"}]


# ---------------------------------------------------------------- parsing

@pytest.mark.parametrize("content,err", [
    ("no json here", "no JSON"),
    ('{"items": "x"}', "no items"),
    ('{"items": [{"n": 1, "verdict": "PARTIAL"}]}', "bad item"),
    ('{"items": [{"n": 4, "verdict": "ANSWERS"}]}', "out of range"),
    ('{"items": [{"n": 1, "verdict": "ANSWERS"}]}', "no verdict"),
])
def test_parse_rejects_unusable_replies(content, err):
    with pytest.raises(fit.FitReplyError, match=err):
        fit.parse_fit_verdicts(content, 2)


def test_parse_reads_lowercase_verdicts_and_caps_the_reason():
    out = fit.parse_fit_verdicts('Here: {"items": [{"n": 1, "verdict": "wrong_subject", "reason": "' + "y" * 300
                                 + '"}]}', 1)
    assert out == [{"verdict": "WRONG_SUBJECT", "reason": "y" * 200}]


def test_prompt_strips_citation_tags():
    prompt = fit.build_user_prompt([("How high is Paro [R:a1]?", "About 2,200 m [F:f2].")])
    assert prompt == "FAQ PAIRS:\n1. Q: How high is Paro?\n   A: About 2,200 m."


# ---------------------------------------------------------------- service glue

def test_service_glue_records_and_flags_what_on_dropped():
    ctx = dict(CONTEXT)
    existing = [{"gate": "F7_faq_autofix", "passed": False, "blocking": False, "repairable": False,
                 "violations": ["earlier line"]}]
    with patch.object(fit, "LLMClient", return_value=_client(_reply(FAIL_TWO))), \
            patch.object(fit, "record_call_sync"), patch.object(fit, "faq_filter_mode", return_value="on"):
        text, flags, cost = asyncio.run(svc._apply_faq_answer_fit(TEXT, existing, context=ctx, piece_id="p-1",
                                                                  request_id="r-1"))
    assert cost == 0.0012 and qg.faq_pair_count(text) == 2
    assert ctx["faq_answer_fit"]["dropped"] == [PAIRS[1][0], PAIRS[3][0]]
    f7 = [f for f in flags if f["gate"] == "F7_faq_autofix"]
    assert len(f7) == 1 and f7[0]["blocking"] is False
    assert f7[0]["violations"][0] == "earlier line"
    assert any("answer fit" in v and PAIRS[3][0] in v for v in f7[0]["violations"])
    assert any("under the 3-pair minimum" in v for v in f7[0]["violations"])


def test_service_glue_off_changes_nothing(monkeypatch):
    monkeypatch.delenv("T9_FAQ_LLM_FILTER", raising=False)
    ctx = dict(CONTEXT)
    text, flags, cost = asyncio.run(svc._apply_faq_answer_fit(TEXT, [], context=ctx))
    assert (text, flags, cost) == (TEXT, [], 0.0) and "faq_answer_fit" not in ctx


def test_service_glue_shadow_adds_no_flag():
    ctx = dict(CONTEXT)
    with patch.object(fit, "LLMClient", return_value=_client(_reply(FAIL_TWO))), \
            patch.object(fit, "record_call_sync"), patch.object(fit, "faq_filter_mode", return_value="shadow"):
        text, flags, _ = asyncio.run(svc._apply_faq_answer_fit(TEXT, [], context=ctx))
    assert text == TEXT and flags == [] and ctx["faq_answer_fit"]["mode"] == "shadow"


def test_writer_input_carries_the_fit_record_only_when_present():
    base = {"channel": "blog", "real_questions": [], "search_questions": []}
    assert "faq_answer_fit" not in svc.writer_input_snapshot(base)
    rec = {"mode": "shadow", "fallback": False, "verdicts": []}
    assert svc.writer_input_snapshot(dict(base, faq_answer_fit=rec))["faq_answer_fit"] == rec


def test_run_write_background_calls_the_step_after_the_tag_strip_and_before_final_integrity():
    src = Path(svc.__file__).read_text()
    i_strip = src.index("content_text = strip_citation_tags(content_text)\n        gate_ledger = deep_strip")
    i_fit = src.index("await _apply_faq_answer_fit(")
    i_final = src.index("content_text, gate_ledger, flags, status, held_reason = _final_integrity(")
    assert i_strip < i_fit < i_final


# ---------------------------------------------------------------- gateway row

def test_safe_default_and_migration_267_agree():
    cfg = SAFE_DEFAULTS["t9_faq_answer_fit"]
    assert (cfg.role, cfg.provider, cfg.model_id, cfg.account_route) == ("validate", "claude", "sonnet", "acc3")
    assert cfg.fallback_model_ids == () and cfg.shadow_model_id is None
    sql = (Path(__file__).resolve().parents[2] / "api" / "migrations" / "267_t9_faq_answer_fit_stage.sql").read_text()
    assert ("('t9_faq_answer_fit', 'validate', 'claude', 'sonnet', 'acc3', true, ARRAY[]::text[], NULL, 0,\n"
            "     'migration-267-aa779')") in sql
    assert "ON CONFLICT (stage) DO NOTHING" in sql and "VALUES ('267'" in sql


# ---------------------------------------------------------------- isolated mode (one call per pair)

ISO_VERDICTS = dict(zip([q for q, _a in PAIRS], FAIL_TWO))


def _pair_client(verdicts=None, fail=None, bad=None, delay=None, inflight=None):
    """generate() answers by the one question in the request: `fail` = questions whose call raises, `bad` =
    questions that get an unusable (billed) reply, `delay` = {question: seconds}, `inflight` = a dict tracking
    the peak number of calls running at once."""
    import threading
    import time
    lock = threading.Lock()
    verdicts = verdicts or ISO_VERDICTS

    def generate(request):
        q = next(q for q in verdicts if f"1. Q: {q}" in request.user_prompt)
        if inflight is not None:
            with lock:
                inflight["now"] = inflight.get("now", 0) + 1
                inflight["peak"] = max(inflight.get("peak", 0), inflight["now"])
        try:
            time.sleep((delay or {}).get(q, 0.02 if inflight is not None else 0))
            if q in (fail or ()):
                raise RuntimeError(f"bedrock down for {q}")
            if q in (bad or ()):
                return LLMResponse(content="not json", model_used="satellite-haiku-4-5", provider="bedrock",
                                   cost_usd=0.001)
            return _reply([verdicts[q]])
        finally:
            if inflight is not None:
                with lock:
                    inflight["now"] -= 1
    client = MagicMock()
    client.generate.side_effect = generate
    return client


def _apply_iso(client, mode="on", text=TEXT):
    with patch.object(fit, "LLMClient", return_value=client), patch.object(fit, "record_call_sync") as rec, \
            patch.object(fit, "record_unusable_sync") as unusable:
        out = asyncio.run(fit.apply_answer_fit(text, context=CONTEXT, piece_id="p-1", request_id="r-1", mode=mode,
                                               fit_mode="isolated"))
    return out, rec, unusable


def test_production_default_is_batched_and_adr_0011_names_it():
    assert fit.FIT_MODE == fit.FIT_MODE_BATCHED and fit.FIT_MODES == ("batched", "isolated")
    adr = (Path(__file__).resolve().parents[2] / "docs" / "adr" / "0011-t9-faq-question-filter-llm-step.md").read_text()
    assert f'FIT_MODE = "{fit.FIT_MODE}"' in adr


def test_isolated_makes_one_call_per_pair_that_sees_only_that_pair():
    client = _pair_client()
    (text, record), rec, _ = _apply_iso(client, mode="shadow")
    assert text == TEXT and client.generate.call_count == len(PAIRS)
    seen = set()
    for call in client.generate.call_args_list:
        req = call.args[0]
        assert req.system_prompt == fit.ISOLATED_SYSTEM_PROMPT and req.max_tokens == fit.ISOLATED_MAX_TOKENS
        assert req.stage == fit.FIT_STAGE and req.model_tier is None  # the stage row picks the model
        assert req.user_prompt.startswith("FAQ PAIR:\n1. Q: ") and req.user_prompt.count("Q: ") == 1
        assert "2. Q:" not in req.user_prompt
        seen.add(req.user_prompt.split("1. Q: ", 1)[1].split("\n", 1)[0])
    assert seen == {q for q, _a in PAIRS}
    assert "other pairs" not in fit.ISOLATED_SYSTEM_PROMPT
    # one llm_call_log row per call, each for one pair
    assert rec.call_count == len(PAIRS)
    qs = [c.kwargs["quality_signal"] for c in rec.call_args_list]
    assert all(s["fit_mode"] == "isolated" and s["n_pairs"] == 1 and s["mode"] == "shadow" for s in qs)
    assert sorted(s["pair_n"] for s in qs) == [1, 2, 3, 4]
    assert record["fit_mode"] == "isolated" and record["calls"] == 4 and record["fallback"] is False
    assert [v["verdict"] for v in record["verdicts"]] == FAIL_TWO
    assert [v["n"] for v in record["verdicts"]] == [1, 2, 3, 4]
    assert record["cost_usd"] == round(4 * 0.0012, 6) and "pair_errors" not in record


def test_isolated_on_drops_only_the_failing_pairs():
    (text, record), _, _ = _apply_iso(_pair_client())
    assert qg.faq_pairs(text) == [PAIRS[0], PAIRS[2]]
    assert record["dropped"] == [PAIRS[1][0], PAIRS[3][0]] and record["below_min"] is True


def test_isolated_a_failing_pair_is_kept_and_recorded_and_the_others_still_count():
    # PAIRS[1] would be NON_ANSWER, but its call fails: it is kept; PAIRS[3] (WRONG_SUBJECT) is still dropped.
    (text, record), rec, unusable = _apply_iso(_pair_client(fail={PAIRS[1][0]}))
    assert qg.faq_pairs(text) == [PAIRS[0], PAIRS[1], PAIRS[2]]
    assert record["fallback"] is False and record["dropped"] == [PAIRS[3][0]]
    assert record["pair_errors"] == [{"n": 2, "q": PAIRS[1][0], "error": f"RuntimeError: bedrock down for "
                                                                         f"{PAIRS[1][0]}"}]
    row = record["verdicts"][1]
    assert row["n"] == 2 and row["verdict"] is None and "bedrock down" in row["error"]
    assert PAIRS[1][0] not in record["failing"]
    assert rec.call_count == 3 and not unusable.called  # the gateway logs its own billed failures


def test_isolated_an_unusable_reply_keeps_that_pair_and_writes_its_billed_unusable_row():
    (text, record), rec, unusable = _apply_iso(_pair_client(bad={PAIRS[3][0]}))
    assert qg.faq_pairs(text) == [PAIRS[0], PAIRS[2], PAIRS[3]]
    assert [e["n"] for e in record["pair_errors"]] == [4] and "FitReplyError" in record["pair_errors"][0]["error"]
    unusable.assert_called_once()
    assert unusable.call_args.kwargs["quality_signal_extra"]["pair_n"] == 4
    assert rec.call_count == 3
    assert record["cost_usd"] == round(3 * 0.0012 + 0.001, 6)  # the billed unusable reply is counted


def test_isolated_every_pair_failing_leaves_the_faq_unchanged():
    (text, record), _, _ = _apply_iso(_pair_client(fail={q for q, _a in PAIRS}))
    assert text == TEXT and record["fallback"] is True and "FitAllPairsFailed" in record["error"]


@pytest.mark.parametrize("bound", [1, 2, 3])
def test_isolated_runs_concurrently_within_the_bound(bound):
    inflight: dict = {}
    pairs = PAIRS + [("When is the Paro festival?", "In spring, usually March."),
                     ("Is there an airport in Paro?", "Yes, Paro has the country's international airport.")]
    verdicts = {q: "ANSWERS" for q, _a in pairs}
    client = _pair_client(verdicts=verdicts, inflight=inflight)
    with patch.object(fit, "record_call_sync"):
        out = fit.check_answer_fit(pairs, fit_mode="isolated", client=client, concurrency=bound)
    assert client.generate.call_count == 6 and out.calls == 6 and out.errors == []
    assert inflight["peak"] == bound


def test_isolated_default_bound_is_fit_concurrency(monkeypatch):
    monkeypatch.setattr(fit, "FIT_CONCURRENCY", 2)
    inflight: dict = {}
    client = _pair_client(inflight=inflight)
    with patch.object(fit, "record_call_sync"):
        fit.check_answer_fit(PAIRS, fit_mode="isolated", client=client)
    assert inflight["peak"] == 2


def test_isolated_a_pair_past_the_deadline_is_kept():
    client = _pair_client(delay={PAIRS[2][0]: 0.5})
    import time
    with patch.object(fit, "record_call_sync"):
        t0 = time.monotonic()
        out = fit.check_answer_fit(PAIRS, fit_mode="isolated", client=client, deadline_s=0.2)
        assert time.monotonic() - t0 < 0.45  # returned at the deadline, not after the slow pair
        time.sleep(0.5)  # let the late call finish (and write its row) while record_call_sync is still patched
    assert [v["verdict"] for v in out.verdicts] == ["ANSWERS", "NON_ANSWER", None, "WRONG_SUBJECT"]
    assert out.errors[0]["n"] == 3 and "TimeoutError" in out.errors[0]["error"]


def test_isolated_calls_count_only_the_calls_started_before_the_deadline():
    # one call at a time: pair 1 replies, pair 2 is still running at the deadline, pairs 3-4 are queued and
    # cancelled, so they are never called and not counted.
    client = _pair_client(delay={PAIRS[1][0]: 0.5})
    import time
    with patch.object(fit, "record_call_sync"):
        out = fit.check_answer_fit(PAIRS, fit_mode="isolated", client=client, concurrency=1, deadline_s=0.2)
        time.sleep(0.5)  # let the late call finish while record_call_sync is still patched
    assert out.calls == 2 and client.generate.call_count == 2
    assert [e["n"] for e in out.errors] == [2, 3, 4]
    assert [v["verdict"] for v in out.verdicts] == ["ANSWERS", None, None, None]


def test_isolated_calls_see_the_callers_contextvars():
    import contextvars
    var = contextvars.ContextVar("aa779_test_var", default=None)
    seen = []
    inner = _pair_client()

    def generate(request):
        seen.append(var.get())
        return inner.generate(request)
    client = MagicMock()
    client.generate.side_effect = generate
    var.set("tenant-1")
    with patch.object(fit, "record_call_sync"):
        fit.check_answer_fit(PAIRS, fit_mode="isolated", client=client)
    assert seen == ["tenant-1"] * len(PAIRS)


def test_isolated_model_tier_override_and_adhoc_stage_pass_through():
    client = _pair_client()
    with patch.object(fit, "record_call_sync") as rec:
        out = fit.check_answer_fit(PAIRS[:2], fit_mode="isolated", stage="adhoc_aa779", model_tier="haiku",
                                   client=client)
    assert {c.args[0].model_tier for c in client.generate.call_args_list} == {"haiku"}
    assert {c.args[0].stage for c in client.generate.call_args_list} == {"adhoc_aa779"}
    assert {c.kwargs["stage"] for c in rec.call_args_list} == {"adhoc_aa779"}
    assert out.verdicts[0] == {"n": 1, "q": PAIRS[0][0], "verdict": "ANSWERS", "reason": "x",
                               "model": "satellite-haiku-4-5"}


def test_unknown_fit_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown answer-fit mode"):
        fit.check_answer_fit(PAIRS, fit_mode="parallel", client=_client(_reply(FAIL_TWO)))


def test_batched_record_names_its_mode_and_one_call():
    (_text, record), rec = _apply("shadow")
    assert record["fit_mode"] == "batched" and record["calls"] == 1
    assert rec.call_args.kwargs["quality_signal"]["fit_mode"] == "batched"
