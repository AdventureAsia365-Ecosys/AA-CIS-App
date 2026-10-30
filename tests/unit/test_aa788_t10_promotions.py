"""AA-788 — the three promoted T10 checks behind their per-check modes: F12_scope (`T10_F12_SCOPE_MODE=block`: a body
finding blocks for one sentence repair and holds when left), F9_brand_style (`T10_F9_BRAND_STYLE_MODE=repair`: attempt 1
blocks for one sentence repair, never a hold) and the AA-780 under-floor F14 block (`T10_F14_FLOOR_MODE=warn` is its
kill switch; the rewrite now gets the source's unused facts). The default modes keep today's ledger byte for byte
(the AA-926 snapshot only gains the routing-set members). No LLM: the F9 verdicts are given (`f9_results`)."""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from services.acp_content_writing import hygiene as h
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import sentence_repair as sr
from services.acp_content_writing import service as writing_service
from services.acp_shared.channel_spec import linkedin_spec
from services.quality import registry as qr

TRIP = "Norland: Selin Lake and Varra Desert — 15 Days"
SCOPE = ("SCOPE: this piece covers days 3–4 of the trip, in this order: Orin Camp → Kessa Rock → Tarrow.")
SEED = ("Orin Camp — overnight in a simple tent camp\n"
        "Location: Orin Camp, Selin Lake area, Selin Province, Norland\n"
        "Day 3 of the trip\n"
        "Source itinerary: Arrive at Orin Camp in the afternoon. Overnight in a simple tent camp.\n\n"
        "Kessa Rock — trek into the caldera of a dormant volcano\n"
        "Location: Kessa Rock Volcano, Hale Province, Norland\n"
        "Day 3 of the trip\n"
        "Source itinerary: Journey to Kessa Rock, a dormant volcano. The old volcano stands above wide grassland "
        "and pine woods.")
BRAND = "Example Trails Co"
CTA = "Ask Example Trails Co to plan this trip for your family."
OUT_OF_SCOPE = "The dunes of the Varra are far from this quiet hill."
CLEAN = "The trek climbs into the caldera of Kessa Rock, a dormant volcano above open grassland."
STYLE_SENTENCE = "Kessa Rock is the plinth marvel of the route, a dormant volcano ringed by pines."
F9_STYLE_VIOLATION = 'audit flagged: GENERIC_AI_WORDING — exact flagged phrase(s): "plinth marvel"'


def _ctx() -> h.HygieneContext:
    return h.HygieneContext(seed=SEED, scope=SCOPE, trip_name=TRIP, brand_name=BRAND, cta=CTA)


def _f9(style: list[str] = ()) -> list[dict]:
    return [qg._result(qr.F9_CTA_FACT, []), qg._result(qr.F9_BRAND_STYLE, list(style), repairable=False,
                                                       blocking=False)]


def _gates(text: str, *, style: list[str] = (), stock_blocking: bool = True, title=None) -> dict:
    """run_quality_gates on a blog-less channel (no SEO / FAQ contract), the F9 verdicts given."""
    return qg.run_quality_gates(content_text=text, atom_text=SEED, cta=CTA, brand_rubric_text="r",
                                channel="email", hygiene_ctx=_ctx(), stock_blocking=stock_blocking,
                                f9_results=_f9(style), seo_title=title)


def _entry(outcome: dict, gate: str) -> dict:
    return next(g for g in qg.expand_ledger(outcome["gate_ledger"]) if g["gate"] == gate)


@pytest.fixture
def modes(monkeypatch):
    for env in (qg.F12_SCOPE_MODE_ENV, qg.F9_BRAND_STYLE_MODE_ENV, qg.F14_FLOOR_MODE_ENV):
        monkeypatch.delenv(env, raising=False)

    def set_(**kw):
        for env, value in kw.items():
            monkeypatch.setenv(env, value)
    return set_


# ---------------------------------------------------------------- defaults: today's behaviour

def test_default_modes_are_todays_behaviour(modes):
    assert qg.f12_scope_mode() == qg.WARN_MODE and qg.F12_SCOPE_BLOCKING is False
    assert qg.f9_brand_style_mode() == qg.WARN_MODE
    assert qg.f14_floor_mode() == qg.REPAIR_MODE and qg.F14_FLOOR_BLOCKING is True


@pytest.mark.parametrize("value", ["", "on", "BLOCKX", "hold"])
def test_an_unknown_mode_reads_as_the_default(modes, value):
    modes(T10_F12_SCOPE_MODE=value, T10_F9_BRAND_STYLE_MODE=value, T10_F14_FLOOR_MODE=value)
    assert (qg.f12_scope_mode(), qg.f9_brand_style_mode(), qg.f14_floor_mode()) == ("warn", "warn", "repair")


def test_default_warn_mode_keeps_both_checks_advisory(modes):
    out = _gates(f"{OUT_OF_SCOPE} {STYLE_SENTENCE} {CTA}", style=[F9_STYLE_VIOLATION])
    f12, f9 = _entry(out, qr.F12_SCOPE), _entry(out, qr.F9_BRAND_STYLE)
    assert not f12["passed"] and f12["blocking"] is False and f12["repairable"] is True
    assert not f9["passed"] and f9["blocking"] is False and f9["repairable"] is False
    assert out["passed"]  # only advisory failures: the piece passes, both are flags
    assert {g["gate"] for g in qg.expand_ledger(out["flags"])} >= {qr.F12_SCOPE, qr.F9_BRAND_STYLE}


# ---------------------------------------------------------------- F12_scope block mode

class TestF12Block:
    def test_body_finding_blocks_and_is_sentence_repairable(self, modes):
        modes(T10_F12_SCOPE_MODE="block")
        text = f"{CLEAN} {OUT_OF_SCOPE} {CTA}"
        out = _gates(text)
        f12 = _entry(out, qr.F12_SCOPE)
        assert not f12["passed"] and f12["blocking"] and f12["repairable"]
        assert not out["passed"] and out["first_failure"]["gate"] == qr.F12_SCOPE
        items = sr.plan(text, out["gate_ledger"])
        assert [i["sentence"] for i in items] == [OUT_OF_SCOPE] and items[0]["gate"] == qr.F12_SCOPE

    def test_left_after_the_repair_holds_the_piece(self, modes):
        modes(T10_F12_SCOPE_MODE="block")
        out = _gates(f"{CLEAN} {OUT_OF_SCOPE} {CTA}", stock_blocking=False)
        assert qg.hold_failure(out["gate_ledger"])["gate"] == qr.F12_SCOPE
        status, reason, _ = writing_service._hold_or_ship(out["gate_ledger"], out["flags"])
        assert status == "held" and reason.startswith(f"{qr.F12_SCOPE}: names 'Varra'")

    def test_a_passing_piece_is_unaffected(self, modes):
        modes(T10_F12_SCOPE_MODE="block")
        out = _gates(f"{CLEAN} {CTA}")
        f12 = _entry(out, qr.F12_SCOPE)
        assert f12["passed"] and out["passed"] and qg.hold_failure(out["gate_ledger"]) is None
        modes(T10_F12_SCOPE_MODE="warn")
        assert _gates(f"{CLEAN} {CTA}")["gate_ledger"] == [
            g if g["gate"] != qr.F12_SCOPE else {**g, "blocking": False} for g in out["gate_ledger"]]

    def test_a_title_only_finding_stays_a_warning(self, modes):
        modes(T10_F12_SCOPE_MODE="block")
        f12 = qg.gate_scope(f"{CLEAN} {CTA}", _ctx(), "Varra and Kessa Rock in Three Days", None)
        assert not f12["passed"] and f12["blocking"] is False
        assert qg.hold_failure([f12]) is None

    def test_the_decision_is_logged(self, modes):
        modes(T10_F12_SCOPE_MODE="block")
        with capture_logs() as logs:
            qg.gate_scope(f"{CLEAN} {OUT_OF_SCOPE}", _ctx())
            qg.gate_scope(f"{CLEAN} {CTA}", _ctx())  # a pass logs nothing
        rows = [r for r in logs if r["event"] == qg.PROMOTION_LOG_EVENT]
        assert len(rows) == 1
        assert rows[0]["gate"] == qr.F12_SCOPE and rows[0]["mode"] == "block" and rows[0]["blocking"] is True
        assert rows[0]["why"] == "body finding"

    def test_warn_mode_logs_nothing(self, modes):
        with capture_logs() as logs:
            qg.gate_scope(f"{CLEAN} {OUT_OF_SCOPE}", _ctx())
        assert not [r for r in logs if r["event"] == qg.PROMOTION_LOG_EVENT]


# ---------------------------------------------------------------- F9_brand_style repair mode

class TestF9BrandStyleRepair:
    def test_attempt_1_blocks_for_one_cheap_sentence_repair(self, modes):
        modes(T10_F9_BRAND_STYLE_MODE="repair")
        text = f"{CLEAN} {STYLE_SENTENCE} {CTA}"
        out = _gates(text, style=[F9_STYLE_VIOLATION])
        f9 = _entry(out, qr.F9_BRAND_STYLE)
        assert f9["blocking"] and f9["repairable"] and not out["passed"]
        assert out["first_failure"]["gate"] == qr.F9_BRAND_STYLE
        items = sr.plan(text, out["gate_ledger"])
        assert [i["sentence"] for i in items] == [STYLE_SENTENCE]
        assert "'plinth marvel'" in items[0]["problem"] and items[0]["gate"] == qr.F9_BRAND_STYLE
        assert writing_service._style_only_repair(out["gate_ledger"])  # the t9_repair stage

    def test_never_holds_and_ships_with_the_flag(self, modes):
        modes(T10_F9_BRAND_STYLE_MODE="repair")
        out = _gates(f"{CLEAN} {STYLE_SENTENCE} {CTA}", style=[F9_STYLE_VIOLATION])
        assert qg.never_holds(_entry(out, qr.F9_BRAND_STYLE)) and qg.hold_failure(out["gate_ledger"]) is None
        status, reason, flags = writing_service._hold_or_ship(out["gate_ledger"], out["flags"])
        assert status == "approved" and reason is None
        assert any(f["gate"] == qr.F9_BRAND_STYLE and f["blocking"] is False for f in qg.expand_ledger(flags))
        waived = writing_service._waive_style_only(out["gate_ledger"], "approved")
        assert _entry({"gate_ledger": waived}, qr.F9_BRAND_STYLE)["waived"] is True

    def test_later_attempts_only_warn(self, modes):
        modes(T10_F9_BRAND_STYLE_MODE="repair")
        out = _gates(f"{CLEAN} {STYLE_SENTENCE} {CTA}", style=[F9_STYLE_VIOLATION], stock_blocking=False)
        f9 = _entry(out, qr.F9_BRAND_STYLE)
        assert not f9["passed"] and f9["blocking"] is False and out["passed"]

    def test_a_phrase_in_no_body_sentence_stays_a_warning(self, modes):
        modes(T10_F9_BRAND_STYLE_MODE="repair")
        v = 'audit flagged: GENERIC_AI_WORDING — exact flagged phrase(s): "zorbly moonwalk"'
        out = _gates(f"{CLEAN} {CTA}", style=[v])
        assert _entry(out, qr.F9_BRAND_STYLE)["blocking"] is False and out["passed"]
        # a code with no quoted phrase at all
        assert not qg.brand_style_sentences(CLEAN, "audit flagged: GENERIC_AI_WORDING")

    def test_a_passing_piece_is_unaffected(self, modes):
        modes(T10_F9_BRAND_STYLE_MODE="repair")
        out = _gates(f"{CLEAN} {CTA}")
        f9 = _entry(out, qr.F9_BRAND_STYLE)
        assert f9["passed"] and f9["blocking"] is False and out["passed"]

    def test_the_decision_is_logged(self, modes):
        modes(T10_F9_BRAND_STYLE_MODE="repair")
        with capture_logs() as logs:
            _gates(f"{CLEAN} {STYLE_SENTENCE} {CTA}", style=[F9_STYLE_VIOLATION])
            _gates(f"{CLEAN} {STYLE_SENTENCE} {CTA}", style=[F9_STYLE_VIOLATION], stock_blocking=False)
        rows = [r for r in logs if r["event"] == qg.PROMOTION_LOG_EVENT and r["gate"] == qr.F9_BRAND_STYLE]
        assert [(r["blocking"], r["why"]) for r in rows] == [(True, "phrase in a body sentence, attempt 1"),
                                                            (False, "not attempt 1")]

    def test_brand_style_sentences_case_insensitive_one_per_sentence(self):
        text = f"{CLEAN} Glimmer nook after glimmer nook waits on this glimmer nook of a trail. {CTA}"
        v = 'audit flagged: GENERIC_AI_WORDING — exact flagged phrase(s): "glimmer nook"; "of a trail"'
        got = qg.brand_style_sentences(text, v)
        assert got == [("Glimmer nook after glimmer nook waits on this glimmer nook of a trail.", "glimmer nook")]


# ---------------------------------------------------------------- the T9 loop: one repair, never a hold

def _loop_gates():
    """A fake run_quality_gates: F9_brand_style flagged on every attempt (the judge never changes its mind), through
    the real promotion; everything else passes."""
    def fake(*, content_text, stock_blocking=True, **_):
        style = qg.promote_brand_style(_f9([F9_STYLE_VIOLATION])[1], content_text, attempt_one=stock_blocking) \
            if "plinth marvel" in content_text else qg._result(qr.F9_BRAND_STYLE, [], blocking=False)
        ledger = [qg._result(qr.F9_CTA_FACT, []), style]
        first = next((g for g in ledger if not g["passed"] and g.get("blocking", True)), None)
        return {"passed": first is None, "gate_ledger": ledger, "first_failure": first,
                "flags": [g for g in ledger if not g["passed"] and not g.get("blocking", True)]}
    return MagicMock(side_effect=fake)


async def _run_loop(first_text, repaired):
    ctx = {"atom_text": SEED, "goal": {"key": "promotion", "name": "P", "description": "d", "logic": "l"},
           "channel_style": {"channel": "email"}, "brand_audience": {"brand_name": BRAND}, "chosen": {"name": "a"},
           "cta": CTA, "destination": None, "trip_name": None, "brand_rubric_text": "r",
           "channel": "email", "atom_id": "a1", "tenant_id": None}
    with patch.object(writing_service, "write_content", return_value=(first_text, 0.01, {}, None)), \
            patch.object(writing_service, "rewrite_with_feedback", return_value=(first_text, 0.01, {}, None)) as rw, \
            patch.object(writing_service, "repair_sentences", return_value=([repaired], 0.001)) as repair, \
            patch.object(writing_service, "run_quality_gates", new=_loop_gates()), \
            patch.object(writing_service, "_finalize_piece", new=AsyncMock()) as fin:
        await writing_service.run_write_background(uuid.uuid4(), uuid.uuid4(), ctx, pool=MagicMock())
    return rw, repair, fin.await_args.kwargs


@pytest.mark.asyncio
async def test_loop_repairs_the_style_sentence_once_and_ships(modes):
    modes(T10_F9_BRAND_STYLE_MODE="repair")
    text = f"{CLEAN} {STYLE_SENTENCE} {CTA}"
    rw, repair, kw = await _run_loop(text, "Kessa Rock is a dormant volcano ringed by pines.")
    assert rw.call_count == 0 and repair.call_count == 1
    assert repair.call_args.kwargs["sentences"][0]["sentence"] == STYLE_SENTENCE
    assert kw["status"] == "approved" and "plinth marvel" not in kw["content_text"]


@pytest.mark.asyncio
async def test_loop_never_holds_when_the_repair_keeps_the_phrase(modes):
    modes(T10_F9_BRAND_STYLE_MODE="repair")
    text = f"{CLEAN} {STYLE_SENTENCE} {CTA}"
    rw, repair, kw = await _run_loop(text, "Kessa Rock is a real plinth marvel, a dormant volcano ringed by pines.")
    assert rw.call_count == 0 and repair.call_count == 1  # one repair only: attempt 2 warns
    assert kw["status"] == "approved" and kw["held_reason"] is None
    assert any(f["gate"] == qr.F9_BRAND_STYLE for f in qg.expand_ledger(kw["flags"]))


# ---------------------------------------------------------------- F14 floor: kill switch + unused seed facts

TRAIL = {"brand_name": "Trail Pulse", "brand_type": "Young active adventure travel brand",
         "voice": ["Energetic", "Fresh", "Direct"]}
SHORT_POST = ("Day 3 ends at Orin Camp.\n\nThe tent camp sits on the lake shore and the evening is yours.\n\n"
              "Ask us for the dates.")


def test_f14_floor_kill_switch(modes):
    def floor_blocks():
        out = qg.run_quality_gates(content_text=SHORT_POST, atom_text=SEED, cta="Ask us for the dates.",
                                   brand_rubric_text="r", channel="linkedin", brand_pack=TRAIL, f9_results=_f9())
        return _entry(out, qr.F14_SOCIAL_SHAPE)["blocking"]
    assert floor_blocks() is True
    modes(T10_F14_FLOOR_MODE="warn")
    assert floor_blocks() is False


def test_f14_floor_rewrite_feedback_lists_unused_source_facts(modes):
    floor = qg.gate_social_shape(SHORT_POST, linkedin_spec(TRAIL), floor_blocking=True)
    fb = writing_service._repair_feedback([floor], "linkedin", source=SEED, content_text=SHORT_POST)
    assert fb[0].startswith(f"{qr.F14_SOCIAL_SHAPE}: post is") and len(fb) == 2
    assert fb[1].startswith(f"{qr.F14_SOCIAL_SHAPE}: source facts this post does not tell yet")
    assert "'The old volcano stands above wide grassland and pine woods.'" in fb[1]
    # without a source, or without the floor block, the feedback is what it was
    assert writing_service._repair_feedback([floor], "linkedin") == fb[:1]
    warn = qg.gate_social_shape(SHORT_POST, linkedin_spec(TRAIL))
    assert writing_service._repair_feedback([warn], "linkedin", source=SEED, content_text=SHORT_POST) == []
