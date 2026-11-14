"""AA-1077 (S280) — the post-normalizer lengthen pass (services/acp_content_writing/length_recheck.py).

A mandatory normalizer step (the usage-policy cut, the final integrity pass) may take a blog the loop approved under
the F4 floor; the re-check then holds it. With the lengthening switch on for the piece, ONE targeted lengthen pass
runs on the stored text (the sections the cut shortened first, facts from the piece's own untold seed sentences),
then the final integrity pass and every gate again; it is accepted only when everything passes. Off = main.

- eligibility: held by the re-check, switch on, loop-approved, crossed at a MANDATORY step, an attempt left;
- the loop: a floor-crossing cut → one pass → approved; switch off → the same hold, no call; the pass fails a gate →
  held as before, the normalizer's text byte for byte; a tenant-budget stop is re-raised, never swallowed.

Invented places only (Kestrel Vale, Lake Ombra, Harrow Ford). The model is always mocked.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.acp_content_writing import length_contract as lc, length_extend as le, length_recheck as lr
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service
from services.quality import registry as qr
from tests.unit.test_aa1077_s278_round2 import (ADDS, CTA, PAD, PIECE_ID, REQUEST_ID, SOURCE, _call, _ctx)

KEEP = {"Kestrel Vale": ["Field gates of grey oak open onto the lanes between the walled strips.",
                         "Curlews call over the wet ground near the stream in the early part of the year."],
        "Lake Ombra": ["The water lies very still on calm days and mirrors the birch woods.",
                       "A narrow beach of dark shingle runs along part of the eastern bank."],
        "Harrow Ford": ["Swallows nest under the eaves of the cottages along the main street each summer.",
                        "The river runs shallow and clear over the gravel where the old crossing was made.",
                        "Low stone benches line the edge of the green facing the water and the far fields.",
                        "Ivy covers the walls of the chapel yard on the side that looks towards the river."]}
# The sentences the (stubbed) final integrity pass deletes: the mandatory cut that crosses the floor.
CUT = {"Kestrel Vale": ["The lower fields hold the oldest walls, built of rounded stones lifted from the stream bed.",
                        "Sheepfolds of the same stone sit in the corners where two walls meet at the field edge.",
                        "A footpath crosses the strips by stone stiles set into the walls at the field corners.",
                        "Rushes grow thick in the damp hollows where the stream spreads out across the floor.",
                        "Most of the barns keep their slate roofs and their wide doors facing the farm track.",
                        "Old hedges of holly mark the line where the farmed land gives way to open grazing."],
       "Lake Ombra": ["The boathouses keep their doors open in the summer so the boats can be pulled straight in.",
                      "The gravel path is level for most of its length and dries quickly after a shower of rain.",
                      "Pines on the island lean away from the wind that comes down the length of the valley.",
                      "The reed beds turn the colour of straw in autumn and stand above the water all winter.",
                      "A small stone shelter by the western path gives cover from the rain beside the water.",
                      "Mist often lies on the lake at dawn and clears as the sun reaches the valley floor."]}
CUT_SENTENCES = [s for v in CUT.values() for s in v]


def _section(name: str) -> str:
    sents = PAD[name][:4] + KEEP.get(name, []) + CUT.get(name, []) + PAD[name][4:]
    return f"## {name}\n\n" + " ".join(sents[:6]) + "\n\n" + " ".join(sents[6:])


def _loop_text() -> str:
    intro = ("Kestrel Vale opens slowly as the road climbs from the plain. Few valleys in the region keep their "
             "old field pattern so clearly. Farms, a village, a lake and a moorland ridge sit within a short walk of "
             "each other. This guide follows them in the order the road reaches them.")
    body = "\n\n".join(_section(n) for n in ("The Valley Road", "Kestrel Vale", "Harrow Ford", "Lake Ombra",
                                             "The Ridge"))
    return (f"{intro}\n\n{body}\n\n{CTA}\n\n## FAQ\n\n**Q: Where is Harrow Ford?**\n"
            "A: Harrow Ford lies where the valley road meets the river below Kestrel Vale, a cluster of cottages "
            "around a green beside the old crossing.\n\n**Q: What is Lake Ombra like?**\n"
            "A: Lake Ombra is a dark upland lake at the head of the valley, edged by reed beds, birch woods and a "
            "cove of tarred boathouses.\n")


def _cut(text: str) -> str:
    for s in CUT_SENTENCES:
        text = text.replace(" " + s, "").replace(s + " ", "")
    return text


def _f4(text: str) -> dict:
    return {**qg.gate_extreme_length(qg.strip_citation_tags(text), "blog"), "blocking": True}


def _outcome(ledger):
    first = next((g for g in ledger if not g["passed"] and g.get("blocking", True)), None)
    return {"passed": first is None, "gate_ledger": ledger, "first_failure": first, "flags": []}


F17_FAIL = {"gate": qr.F17_TRIP_FACT, "passed": False, "blocking": True, "repairable": True,
            "violations": ["presents 'a boat ride' as part of the plan, but the source lists it as optional"]}


def _gates(calls: list, *, fail_second_with=None):
    """run_quality_gates stand-in: F4 measured on the real text; the post-normalizer run may add a failing gate."""
    def run(**kw):
        calls.append(kw)
        ledger = [_f4(kw["content_text"])]
        if fail_second_with and len(calls) > 1:
            ledger.append(fail_second_with)
        return _outcome(ledger)
    return run


@pytest.fixture(autouse=True)
def _no_pin():
    le._PINNED.set(None)
    yield
    le._PINNED.set(None)


@pytest.fixture
def _stubs():
    """The loop's model steps stubbed; the REAL F4 re-check after the normalizer; the final integrity pass deletes
    CUT (a mandatory step: duplicates / fragments) and the in-loop / post-normalizer final_pass is the identity."""
    def final_integrity(text, hyg, ledger, flags, status, held_reason):
        return _cut(text), ledger, flags, status, held_reason
    with patch.object(service.hygiene, "ensure_cta", side_effect=lambda text, cta: (text, [])), \
            patch.object(service.text_integrity, "final_pass", side_effect=lambda text, hyg, **kw: (text, [], [])), \
            patch.object(service, "_fit_seo_meta", new=AsyncMock(side_effect=lambda seo, kw, **k: (seo, 0.0))), \
            patch.object(service, "_enforce_faq_contract",
                         new=AsyncMock(side_effect=lambda text, **k: (text, 0.0, []))), \
            patch.object(service, "_apply_faq_answer_fit",
                         new=AsyncMock(side_effect=lambda text, flags, **k: (text, flags, 0.0))), \
            patch.object(service, "_final_integrity", side_effect=final_integrity):
        yield


def test_the_fixture_is_a_floor_crossing_cut():
    loop, cut = _loop_text(), _cut(_loop_text())
    assert lc.measure(loop) >= lc.floor_chars() > lc.measure(cut)
    assert all(s in loop for s in CUT_SENTENCES) and not any(s in cut for s in CUT_SENTENCES)


# ---------------------------------------------------------------- eligibility (pure)

def _trace(**chars):
    return {"held": True, "chars": {"loop": 4800, **chars}}


@pytest.mark.parametrize("chars, step", [({"stock_cut": 4800, "authenticity_cut": 4600}, "authenticity_cut"),
                                         ({"faq_answer_fit": 4800, "final_integrity": 4500}, "final_integrity"),
                                         ({"faq_answer_fit": 4650, "final_integrity": 4600}, "faq_answer_fit"),
                                         ({"final_integrity": 4800}, None)])
def test_crossing_step_is_the_step_that_went_under_the_floor(chars, step):
    assert lr.crossing_step({"loop": 4800, **chars}) == step


@pytest.mark.parametrize("trace, kw, reason", [
    (None, {}, "not_held_by_recheck"),
    ({"held": False, "chars": {"loop": 4800, "final_integrity": 4500}}, {}, "not_held_by_recheck"),
    (_trace(final_integrity=4500), {"switch": "off"}, "off"),
    (_trace(final_integrity=4500), {"loop_status": "held"}, "not_loop_approved"),
    (_trace(faq_answer_fit=4600), {}, "not_a_mandatory_cut"),
    (_trace(final_integrity=4500), {"attempts_left": 0}, "no_attempt_left"),
    (_trace(authenticity_cut=4500), {}, "lengthen"),
    (_trace(final_integrity=4500), {}, "lengthen"),
])
def test_eligibility(trace, kw, reason):
    args = {"loop_status": "approved", "switch": "on", "attempts_left": 1, **kw}
    assert lr.eligibility(trace, **args)[0] == reason


# ---------------------------------------------------------------- the loop

async def _run(ctx, calls, *, ext_side_effect=_call, fail_second_with=None):
    with patch.object(service, "write_content", return_value=(_loop_text(), 0.05, {}, None)), \
            patch.object(service, "rewrite_with_feedback") as rewrite, \
            patch.object(service, "extend_sections", side_effect=ext_side_effect) as ext, \
            patch.object(service, "run_quality_gates", side_effect=_gates(calls, fail_second_with=fail_second_with)), \
            patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(REQUEST_ID, PIECE_ID, ctx, pool=MagicMock())
    rewrite.assert_not_called()
    return ext, fin.call_args.kwargs


@pytest.mark.asyncio
async def test_a_floor_crossing_cut_gets_one_pass_and_is_approved(_stubs, monkeypatch):
    monkeypatch.setenv(le.EXTEND_ENV, "on")
    ctx, calls = _ctx(), []
    ext, kw = await _run(ctx, calls)
    ext.assert_called_once()
    assert ext.call_args.kwargs["step"] == lr.STEP and ext.call_args.kwargs["attempt"] == 2
    asked = {s["heading"] for s in ext.call_args.kwargs["sections"]}
    assert asked <= set(CUT) and all(s.get("hole_chars") for s in ext.call_args.kwargs["sections"])  # the cut first
    assert len(calls) == 2                                          # the loop's gates, then every gate again
    assert calls[1]["content_text"] == kw["content_text"] and calls[1]["stock_blocking"] is False
    assert calls[1]["atom_text"] == calls[0]["atom_text"] and calls[1]["cta"] == calls[0]["cta"]
    assert kw["status"] == "approved" and kw["held_reason"] is None
    assert lc.measure(kw["content_text"]) >= lc.floor_chars()
    assert not any(s in kw["content_text"] for s in CUT_SENTENCES)            # the mandatory cut stands
    assert sum(ADDS[h] in kw["content_text"] for h in asked) == len(asked)
    f4 = next(g for g in kw["gate_ledger"] if g["gate"] == qr.F4_EXTREME_LENGTH)
    assert f4["passed"]
    trace = ctx["length_trace"]
    rec = trace[lr.TRACE_KEY]
    assert rec["reason"] == "lengthen" and rec["crossed_at"] == "final_integrity" and rec["outcome"] == "approved"
    assert rec["extend"]["added"] == len(asked) and rec["cost_usd"] == 0.02 and rec["attempt"] == 2
    assert trace["held"] is False and trace["verdict"] == "in" and trace["extend"] == "on"
    assert trace["chars"][lr.TRACE_KEY] == lc.measure(kw["content_text"])


@pytest.mark.asyncio
async def test_switch_off_keeps_the_hold_unchanged(_stubs, monkeypatch):
    monkeypatch.setenv(le.EXTEND_ENV, "off")
    ctx, calls = _ctx(), []
    ext, kw = await _run(ctx, calls)
    ext.assert_not_called()
    assert len(calls) == 1
    assert kw["status"] == "held" and kw["held_reason"].startswith(qr.F4_EXTREME_LENGTH)
    assert kw["content_text"] == _cut(calls[0]["content_text"])
    assert ctx["length_trace"][lr.TRACE_KEY] == {"reason": "off", "crossed_at": "final_integrity", "attempt": 2}
    assert ctx["length_trace"]["held"] is True


@pytest.mark.asyncio
async def test_a_pass_that_fails_a_gate_is_held_as_before(_stubs, monkeypatch):
    monkeypatch.setenv(le.EXTEND_ENV, "on")
    ctx, calls = _ctx(), []
    ext, kw = await _run(ctx, calls, fail_second_with=F17_FAIL)
    ext.assert_called_once()
    assert len(calls) == 2
    assert kw["status"] == "held" and kw["held_reason"].startswith(qr.F4_EXTREME_LENGTH)
    assert kw["content_text"] == _cut(calls[0]["content_text"])     # the normalizer's text, as before
    assert not any(g["gate"] == qr.F17_TRIP_FACT for g in kw["gate_ledger"])     # the loop's ledger, not the pass's
    rec = ctx["length_trace"][lr.TRACE_KEY]
    assert rec["outcome"] == "held:gates" and rec["failing"] == [qr.F17_TRIP_FACT] and rec["cost_usd"] == 0.02
    assert ctx["length_trace"]["held"] is True


@pytest.mark.asyncio
async def test_nothing_added_keeps_the_hold(_stubs, monkeypatch):
    monkeypatch.setenv(le.EXTEND_ENV, "on")
    ctx, calls = _ctx(), []

    def empty(*, sections, facts, voice=None, **kw):
        return [""] * len(sections), 0.003, "writer-model"
    ext, kw = await _run(ctx, calls, ext_side_effect=empty)
    ext.assert_called_once()
    assert len(calls) == 1 and kw["status"] == "held" and kw["content_text"] == _cut(calls[0]["content_text"])
    assert ctx["length_trace"][lr.TRACE_KEY]["outcome"] == "held:nothing_added"


@pytest.mark.asyncio
async def test_no_attempt_left_in_the_budget_keeps_the_hold(_stubs, monkeypatch):
    monkeypatch.setenv(le.EXTEND_ENV, "on")
    monkeypatch.setattr(service, "max_attempts", lambda: 1)       # the loop approved on its last budgeted attempt
    ctx, calls = _ctx(), []
    ext, kw = await _run(ctx, calls)
    ext.assert_not_called()
    assert kw["status"] == "held" and ctx["length_trace"][lr.TRACE_KEY]["reason"] == "no_attempt_left"


async def _run_approved_at_attempt_two(ctx, calls):
    """The loop fails attempt 1 on a repairable F17 problem (the text over the floor), repairs it and approves attempt
    2; then the mandatory cut takes it under the floor and the re-check holds it."""
    def gates(**kw):
        calls.append(kw)
        ledger = [_f4(kw["content_text"])] + ([F17_FAIL] if len(calls) == 1 else [])
        return _outcome(ledger)
    with patch.object(service, "write_content", return_value=(_loop_text(), 0.05, {}, None)), \
            patch.object(service, "rewrite_with_feedback", return_value=(_loop_text(), 0.05, {}, None)), \
            patch.object(service, "repair_sentences", side_effect=lambda *, sentences, **kw: (
                [it["sentence"] for it in sentences], 0.01)), \
            patch.object(service, "extend_sections", side_effect=_call) as ext, \
            patch.object(service, "run_quality_gates", side_effect=gates), \
            patch.object(service, "_finalize_piece", new=AsyncMock(return_value={})) as fin:
        await service.run_write_background(REQUEST_ID, PIECE_ID, ctx, pool=MagicMock())
    return ext, fin.call_args.kwargs


@pytest.mark.asyncio
async def test_stop_and_label_approval_at_attempt_two_has_no_paid_attempt_left(_stubs, monkeypatch):
    """Review S280: the default mode's third step is the $0 label (root ADR 0020 D6, setting cis.t9.max_attempts), so
    a blog approved at attempt 2 gets no paid extend call, whatever the setting says (default 3)."""
    monkeypatch.setenv(le.EXTEND_ENV, "on")
    monkeypatch.delenv(service.LAST_ATTEMPT_MODE_ENV, raising=False)
    assert service.max_attempts() == 3
    ctx, calls = _ctx(), []
    ext, kw = await _run_approved_at_attempt_two(ctx, calls)
    ext.assert_not_called()
    assert len(calls) == 2                                      # attempts 1 and 2, no post-normalizer gate run
    assert kw["status"] == "held" and kw["held_reason"].startswith(qr.F4_EXTREME_LENGTH)
    assert ctx["length_trace"][lr.TRACE_KEY] == {"reason": "no_attempt_left", "crossed_at": "final_integrity",
                                                 "attempt": 3}


@pytest.mark.asyncio
async def test_legacy_mode_approval_at_attempt_two_keeps_its_paid_third_attempt(_stubs, monkeypatch):
    """Legacy mode keeps a paid attempt 3 (setting at 3): the pass may use it, logged as attempt 3."""
    monkeypatch.setenv(le.EXTEND_ENV, "on")
    monkeypatch.setenv(service.LAST_ATTEMPT_MODE_ENV, service.LEGACY_LAST_ATTEMPT)
    ctx, calls = _ctx(), []
    ext, kw = await _run_approved_at_attempt_two(ctx, calls)
    ext.assert_called_once()
    assert ext.call_args.kwargs["attempt"] == 3 and len(calls) == 3
    assert kw["status"] == "approved"
    assert ctx["length_trace"][lr.TRACE_KEY]["reason"] == "lengthen"


# ---------------------------------------------------------------- the tenant AI budget is never swallowed

class _BudgetStop(Exception):
    pass


@pytest.mark.asyncio
async def test_a_tenant_budget_stop_in_the_pass_is_re_raised(monkeypatch):
    monkeypatch.setattr(lr, "approval_stop_in", lambda e: e if isinstance(e, _BudgetStop) else None)
    loop, cut = _loop_text(), _cut(_loop_text())
    ledger, status, reason, entry = lr.recheck_length(cut, [], "approved", None,
                                                      trace={"loop": lc.measure(loop),
                                                             "final_integrity": lc.measure(cut)})
    assert status == "held" and entry["held"]
    context = {"length_trace": {"attempt": 1, **entry}}
    norm = {"content_text": cut, "gate_ledger": ledger, "flags": [], "status": status, "held_reason": reason,
            "cost_usd": 0.0, "embedding": None, "summary": None, "repair_log": []}

    def call(**kw):
        raise _BudgetStop("tenant budget needs approval")
    gates = MagicMock()
    with pytest.raises(_BudgetStop):
        await lr.lengthen_after_normalizer(
            norm, loop_text=loop, loop_status="approved", switch="on", channel="blog", attempt=1, attempts_left=2,
            source=SOURCE, context=context, gates=gates, call=call, hyg=None, cta=CTA)
    gates.assert_not_called()


@pytest.mark.asyncio
async def test_another_channel_or_no_trace_is_untouched():
    norm = {"content_text": "x", "status": "held"}
    for channel, context in (("linkedin", {"length_trace": {"held": True}}), ("blog", {})):
        out = await lr.lengthen_after_normalizer(
            norm, loop_text="x", loop_status="approved", switch="on", channel=channel, attempt=1, attempts_left=2,
            source=SOURCE, context=context, gates=MagicMock(), call=MagicMock(), hyg=None)
        assert out is norm and lr.TRACE_KEY not in context.get("length_trace", {})


# ---------------------------------------------------------------- the $0 replay's post-normalizer column

def test_the_replay_says_what_the_pass_would_do_with_each_recheck_hold():
    from scripts.aa1077 import extend_plan_replay as rp
    cut = _cut(_loop_text())
    f4 = _f4(cut)

    def row(pid, chars, **trace):
        return {"piece_id": pid, "tenant": "audit-x", "status": "held", "held_reason": None, "content_text": cut,
                "gate_ledger": [f4], "repair_log": [],
                "writer_input": {"seed": SOURCE, "length_trace": {"held": True, "attempt": 1, "chars": chars,
                                                                  **trace}}}
    mandatory = rp.replay_piece(row("aaaaaaaa-1", {"loop": 4800, "final_integrity": 4400}),
                                source_of=lambda wi, r: SOURCE)
    optional = rp.replay_piece(row("bbbbbbbb-2", {"loop": 4800, "faq_answer_fit": 4400}),
                               source_of=lambda wi, r: SOURCE)
    spent = rp.replay_piece(row("cccccccc-3", {"loop": 4800, "authenticity_cut": 4400},
                                post_normalizer={"reason": "lengthen", "outcome": "held:gates", "cost_usd": 0.02,
                                                 "failing": ["F17_trip_fact"]}),
                            source_of=lambda wi, r: SOURCE, paid_attempts=1)
    assert mandatory["post_normalizer"] == {"reason": "lengthen", "crossed_at": "final_integrity", "recorded": {}}
    assert optional["post_normalizer"]["reason"] == "not_a_mandatory_cut"
    assert spent["post_normalizer"]["reason"] == "no_attempt_left"
    assert spent["post_normalizer"]["recorded"]["outcome"] == "held:gates"
    s = rp.summarize([mandatory, optional, spent])
    assert s["post_normalizer"] == {"lengthen": 1, "not_a_mandatory_cut": 1, "no_attempt_left": 1}
    assert s["post_normalizer_reachable"] == ["aaaaaaaa"]


def test_the_replay_uses_the_paid_budget_so_a_stop_and_label_attempt_two_approval_is_not_eligible():
    from scripts.aa1077 import extend_plan_replay as rp
    cut = _cut(_loop_text())
    row = {"piece_id": "dddddddd-4", "tenant": "audit-x", "status": "held", "held_reason": None, "content_text": cut,
           "gate_ledger": [_f4(cut)], "repair_log": [],
           "writer_input": {"seed": SOURCE, "length_trace": {"held": True, "attempt": 2,
                                                            "chars": {"loop": 4800, "final_integrity": 4400}}}}
    assert rp.replay_piece(row, source_of=lambda wi, r: SOURCE)["post_normalizer"]["reason"] == "no_attempt_left"
    assert rp.replay_piece(row, source_of=lambda wi, r: SOURCE, paid_attempts=3)["post_normalizer"]["reason"] == (
        "lengthen")
