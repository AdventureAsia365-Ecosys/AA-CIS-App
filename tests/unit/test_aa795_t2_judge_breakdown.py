"""AA-795 — a T2 version stores its judge breakdown (CIS 300) and the brand-claim fidelity check of its text.

Report 12 (S234) F-f: brand fit, cross-brand distinctness, mission, feedback and the binding axis per version. F-g:
the brand claims of the saved text the A1 source does not support ("children aged seven to sixteen",
"consent-based"), report-only. Mock-only: no AWS, no DB, no LLM.
"""
from __future__ import annotations

import json

import pytest

from services.content_generation import t2_brand_claims as bc
from services.content_generation import t2_judge_breakdown as jb
from services.content_generation import t2_repair

SOURCE = ("Day 1: Arrive in Paro and walk 12 km to the village. Day 2: Visit a 300-year-old dzong with licensed "
          "local guides. The farmhouse lunch is part of the booking.")


# ── brand-claim check (F-g) ────────────────────────────────────────────────────────────────────

def _unsupported(rc, source=SOURCE, brand=""):
    return [(c["kind"], c["claim"]) for c in bc.check(rc, source, brand)["unsupported"]]


def test_the_report_12_examples_are_flagged():
    rc = {"summary": "A family trek for children aged seven to sixteen.",
          "itineraries": "Day 2: Join a consent-based community visit in the village."}
    assert _unsupported(rc) == [("age", "children aged seven to sixteen"), ("ethos", "consent-based")]


@pytest.mark.parametrize("claim", ["kids aged 7-16", "ages 12+", "under-12s", "minimum age 10",
                                   "teenagers aged 13 to 17", "10-year-olds", "suits an 8-year-old child"])
def test_age_claims_are_read(claim):
    kinds = [k for k, _ in _unsupported({"summary": f"Great for {claim} and their parents."})]
    assert kinds == ["age"]


@pytest.mark.parametrize("claim", ["small groups of up to 12", "groups of no more than 8",
                                   "never more than 10 travellers", "a maximum of 6 guests"])
def test_group_size_claims_are_read(claim):
    assert [k for k, _ in _unsupported({"summary": f"We travel in {claim}."})] == ["group_size"]


@pytest.mark.parametrize("text", [
    "A 300-year-old temple above the river.",              # a building's age, no person
    "A 50-year-old tree shades the courtyard.",            # an age, but not a person's
    "Travellers from 20 countries walk this trail.",       # a count of countries
    "Walk 12 km to the village.",                          # a distance
    "Visit a group of 3 temples.",                         # a group, not a group-size promise
    "No riding experience is needed.",                     # not an animal-welfare claim
    "The farmhouse lunch is part of the booking.",         # not a profit share
    "Guests over 2 days explore the valley.",              # a duration after "over"
])
def test_background_and_logistics_are_not_claims(text):
    assert bc.check({"summary": text}, "")["unsupported"] == []


def test_a_claim_the_source_states_is_supported():
    source = SOURCE + " Suitable for children aged 7 to 16. A consent-based homestay visit. Small groups of up to 12."
    rc = {"summary": "For children aged seven to sixteen, in small groups of up to 12.",
          "highlights": ["A consent based homestay visit"]}
    out = bc.check(rc, source)
    assert out["unsupported"] == [] and out["checked"] == out["supported"] == 3


def test_different_numbers_are_not_supported():
    out = bc.check({"summary": "For children aged 5 to 16."}, "Suitable for children aged 7 to 16.")
    assert [c["kind"] for c in out["unsupported"]] == ["age"]


def test_each_claim_names_its_field_sentence_and_brand_origin():
    rc = {"highlights": ["Lunch with a women-led cooperative. Then a swim."]}
    [c] = bc.check(rc, SOURCE, "Our trips back women-led businesses.")["unsupported"]
    assert c == {"kind": "ethos", "claim": "women-led", "field": "highlights[0]",
                 "sentence": "Lunch with a women-led cooperative.", "in_brand_pack": True}


def test_list_shaped_itineraries_are_read():
    rc = {"itineraries": [{"day": 1, "title": "Paro", "body": "A carbon-neutral drive to the valley."}]}
    [c] = bc.check(rc, SOURCE)["unsupported"]
    assert (c["kind"], c["claim"], c["field"]) == ("ethos", "carbon-neutral", "itineraries[0].body")


def test_brand_text_reads_the_brand_rules():
    text = bc.brand_text({"core_idea": "Consent-first travel", "voice_examples": ["warm", "plain"], "x": "no"})
    assert "Consent-first travel" in text and "warm" in text and "no" not in text.split()


@pytest.mark.parametrize("value,on", [(None, True), ("on", True), ("off", False), ("0", False), ("false", False)])
def test_kill_switch(monkeypatch, value, on):
    if value is None:
        monkeypatch.delenv("T2_BRAND_CLAIM_CHECK", raising=False)
    else:
        monkeypatch.setenv("T2_BRAND_CLAIM_CHECK", value)
    assert bc.enabled() is on


# ── binding axis (F-f) ─────────────────────────────────────────────────────────────────────────

def _axes(**kw):
    base = dict(gate="brand_fit_mission", brand_fit=8.0, distinct=5.0, mission_present=True, has_mission=True,
                mission_cap=6.0, mission_source="tenant_core_idea", judge_score_raw=8.0, judge_score=8.0,
                validate_score=9.5, tiebreak=None)
    return jb.axes(**{**base, **kw})


@pytest.mark.parametrize("kw,binding", [
    ({}, "brand_fit"),
    ({"brand_fit": 5.0, "judge_score_raw": 5.0, "judge_score": 5.0}, "brand_fit"),
    ({"mission_present": False, "judge_score_raw": 6.0, "judge_score": 6.0}, "mission"),
    ({"mission_present": False, "brand_fit": 6.0, "judge_score_raw": 6.0, "judge_score": 6.0}, "brand_fit"),  # tie
    ({"mission_present": False, "has_mission": False}, "brand_fit"),                     # no core idea: no cap
    ({"gate": "brand_fit", "mission_present": False}, "brand_fit"),                     # A1-style gate: no cap
    ({"gate": "combined", "judge_score_raw": 5.0, "judge_score": 5.0}, "cross_brand"),
    ({"validate_score": 7.5}, "validate"),
    ({"validate_score": 8.0}, "brand_fit"),                                              # equal: the judge axis
    ({"brand_fit": 6.8, "judge_score_raw": 6.8, "judge_score": 7.0,
      "tiebreak": {"zone": "accept", "before": 6.8, "after": 7.0}}, "tiebreak"),
    ({"brand_fit": 6.8, "judge_score_raw": 6.8, "judge_score": 6.8,
      "tiebreak": {"zone": "uncertain", "before": 6.8, "after": 6.8}}, "brand_fit"),     # Jev left it as is
])
def test_binding_axis(kw, binding):
    assert _axes(**kw)["binding"] == binding


def test_axes_carry_the_numbers_behind_the_verdict():
    assert _axes(mission_present=False, judge_score_raw=6.0, judge_score=6.0) == {
        "gate": "brand_fit_mission", "validate_score": 9.5, "judge_score_raw": 6.0, "mission_cap": 6.0,
        "mission_source": "tenant_core_idea", "binding": "mission"}
    assert _axes(has_mission=False, mission_source="none")["mission_cap"] is None


def _result(**kw):
    return {"judge_brand_fit": 8.0, "judge_cross_brand_distinct": 4.0, "judge_mission_present": False,
            "judge_score": 6.0, "judge_feedback": "The days read like a catalogue.",
            "judge_t2_axes": _axes(mission_present=False, judge_score_raw=6.0, judge_score=6.0), **kw}


def test_build_is_the_stored_breakdown():
    claims = bc.check({"summary": "For kids aged 7-16."}, SOURCE)
    out = jb.build(_result(), final_score=6.0, brand_claims=claims, chosen_attempt=1)
    assert out == {"version": 1, "judged": True, "brand_fit": 8.0, "cross_brand_distinct": 4.0,
                   "mission_present": False, "judge_score": 6.0, "validate_score": 9.5, "final_score": 6.0,
                   "score_source": "rewrite", "gate": "brand_fit_mission", "mission_source": "tenant_core_idea",
                   "binding": "mission", "feedback": "The days read like a catalogue.", "mission_cap": 6.0,
                   "attempt": 1, "brand_claims": claims}
    json.dumps(out)


def test_an_unjudged_draft_says_so():
    out = jb.build({"quality_score": 8.5}, final_score=8.5)
    assert out["judged"] is False and out["binding"] == "unjudged" and out["feedback"] is None
    assert "brand_claims" not in out and "attempt" not in out


def test_feedback_is_capped_and_a_tiebreak_keeps_the_raw_score():
    r = _result(judge_feedback="x" * 5000, judge_score=7.0,
                judge_t2_axes=_axes(brand_fit=6.8, judge_score_raw=6.8, judge_score=7.0, tiebreak={"zone": "a"}))
    out = jb.build(r, final_score=7.0, score_source="source_fallback")
    assert len(out["feedback"]) == jb.FEEDBACK_MAX and out["judge_score_raw"] == 6.8
    assert out["binding"] == "tiebreak" and out["score_source"] == "source_fallback"


def test_a_restored_best_draft_restores_its_own_axes(monkeypatch):
    monkeypatch.setenv("T2_BEST_DRAFT", "on")
    assert "judge_t2_axes" in t2_repair.DRAFT_KEYS
    best_axes, last_axes = _axes(), _axes(validate_score=5.0)
    best = {"generated": {"name": "A"}, "quality_score": 8.0, "judge_t2_axes": best_axes, "attempt": 0}
    state = {"generated": {"name": "B"}, "quality_score": 5.0, "judge_t2_axes": last_axes, "best_draft": best}
    assert t2_repair.restore_best_node(state)["judge_t2_axes"] == best_axes


# ── the judge node records the axes on a T2 draft only ─────────────────────────────────────────

def test_the_t2_judge_returns_its_axes(monkeypatch):
    monkeypatch.delenv("T2_JUDGE_GATE", raising=False)
    from tests.unit.test_aa792_t2_tenant_voice import _T2, _judge_resp, _run_judge
    out, _, _ = _run_judge(_T2, _judge_resp(fit=8, mission=False))
    assert out["judge_score"] == 6.0
    assert out["judge_t2_axes"] == {"gate": "brand_fit_mission", "validate_score": 9.75, "judge_score_raw": 6.0,
                                    "mission_cap": 6.0, "mission_source": "tenant_core_idea", "binding": "mission"}


def test_an_axes_error_never_costs_the_verdict(monkeypatch):
    monkeypatch.delenv("T2_JUDGE_GATE", raising=False)
    from tests.unit.test_aa792_t2_tenant_voice import _T2, _judge_resp, _run_judge
    from services.content_generation import judge_node as jn

    def boom(**_):
        raise ValueError("bad")
    monkeypatch.setattr(jn.t2_breakdown, "axes", boom)
    out, _, _ = _run_judge(_T2, _judge_resp(fit=8, mission=True))
    assert out["judge_score"] == 8.0 and "judge_t2_axes" not in out and "degraded" not in out


def test_the_a1_judge_returns_no_axes():
    from unittest.mock import MagicMock, patch
    from tests.unit.test_aa792_t2_tenant_voice import _A1_JUDGE_PROFILE, _GEN
    from services.content_generation import judge_node as jn
    with patch.object(jn, "score_brand_fit") as score, patch.object(jn, "record_call_sync"):
        score.return_value = MagicMock(brand_fit_score=8.0, cross_brand_distinct=3.0, mission_present=False,
                                       judge_score=3.0, feedback="", model_used="m", input_tokens=1,
                                       output_tokens=1, cost_usd=0.0)
        out = jn.judge_node(dict(_A1_JUDGE_PROFILE, is_tenant_rewrite=False, generated=_GEN, quality_score=9.75,
                                 feedback=""))
    assert "judge_t2_axes" not in out


def test_the_graph_state_declares_the_axes():
    from services.content_generation.graph import ContentState
    assert "judge_t2_axes" in ContentState.__annotations__


# ── the T2 job stores it; nothing else changes ─────────────────────────────────────────────────

def _run_save(monkeypatch, summary, *, check="on", judge=True):
    monkeypatch.setenv("T2_BRAND_CLAIM_CHECK", check)
    import asyncio
    from unittest.mock import AsyncMock, patch
    from tests.unit.test_aa778_t2_em_dash import SOURCE as T2_SOURCE, _pool
    result = {"status": "success", "quality_score": 8.0,
              "generated": {"name": "Ridge walk", "subtitle": "s", "summary": summary, "highlights": ["A ridge"],
                            "itineraries": "Day 1: Walk.", "seo_title": "t", "seo_meta": "m", "trip_type": "a"}}
    if judge:
        result.update(_result(judge_score=8.0, judge_mission_present=True,
                              judge_t2_axes=_axes(validate_score=8.0)))
    qa = {"result": result, "passed": True, "attempts": 0, "structural_issues": [], "grounding_issues": [],
          "chosen_attempt": 0}
    conn = AsyncMock()
    conn.fetchrow.return_value = None
    conn.fetchval.return_value = 5.0
    tour = dict(T2_SOURCE, subtitle="", description="", seo_title="", seo_meta="", country="Nepal", duration="2 Days")
    from services.jobs import t2_rewrite_job as t2
    with patch("api.routers.v1_pipeline._rewrite_tour", AsyncMock(return_value=result)), \
         patch("services.acp_produce.tenant_pipeline.run_t3_qa_gate", AsyncMock(return_value=qa)), \
         patch("services.seo_intelligence.s1_prefetch.tenant_market_seo", AsyncMock(return_value=("US", None))), \
         patch("services.writing_memory.insertion.resolve", AsyncMock(return_value=None)):
        diag = asyncio.run(t2._rewrite_and_save(_pool(conn), {"tour_id": "tour-1"}, tour,
                                                {"core_idea": "Consent-first travel"}, "tenant-1", "v-1", "p-1"))
    [save] = [c.args for c in conn.execute.call_args_list if "SET rewritten_content" in c.args[0]]
    return save, diag


def test_the_t2_save_stores_the_judge_breakdown(monkeypatch):
    save, diag = _run_save(monkeypatch, "Join a consent-based visit with kids aged 7-16.")
    sql, args = save[0], save[1:]
    assert "judge_breakdown = $9::jsonb" in sql
    stored = json.loads(args[8])
    assert stored["binding"] == "brand_fit" and stored["brand_fit"] == 8.0 and stored["final_score"] == 8.0
    assert stored["attempt"] == 0 and stored["feedback"] == "The days read like a catalogue."
    claims = stored["brand_claims"]["unsupported"]
    assert [(c["kind"], c["in_brand_pack"]) for c in claims] == [("age", False), ("ethos", True)]
    assert diag["judge"] == {"binding": "brand_fit", "brand_claims_unsupported": 2}


def test_flagged_claims_change_nothing_else_in_the_save(monkeypatch):
    on, _ = _run_save(monkeypatch, "Join a consent-based visit with kids aged 7-16.")
    off, _ = _run_save(monkeypatch, "Join a consent-based visit with kids aged 7-16.", check="off")
    assert on[1:8] == off[1:8]                                      # text, status, score, QA, version id, meta
    assert "brand_claims" not in json.loads(off[9])


def test_an_unjudged_version_is_stored_as_unjudged(monkeypatch):
    save, _ = _run_save(monkeypatch, "Two quiet days.", judge=False)
    stored = json.loads(save[9])
    assert stored["judged"] is False and stored["binding"] == "unjudged"
    assert stored["brand_claims"]["unsupported"] == []


def test_a_breakdown_error_saves_null_and_the_version(monkeypatch):
    from services.content_generation import t2_brand_claims

    def boom(*a, **k):
        raise RuntimeError("bad pattern")
    monkeypatch.setattr(t2_brand_claims, "check", boom)
    save, diag = _run_save(monkeypatch, "Two quiet days.")
    assert save[9] is None and save[2] == "ai_generated" and "judge" not in diag


# ── admin-only ─────────────────────────────────────────────────────────────────────────────────

def test_the_tenant_never_sees_the_breakdown():
    from api.routers._tenant_projection import TENANT_FORBIDDEN_KEY_RE, TENANT_HIDDEN_VERSION_FIELDS
    assert "judge_breakdown" in TENANT_HIDDEN_VERSION_FIELDS and TENANT_FORBIDDEN_KEY_RE.search("judge_breakdown")


def test_readiness_needs_300():
    from api.core import readiness
    assert readiness.REQUIRED_CIS_SCHEMA >= 300


# ── admin read (Content Quality → Tour versions drawer) ────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["json", None])
async def test_the_admin_version_detail_returns_the_breakdown(monkeypatch, stored):
    from api.routers import admin_content_quality as cq
    from tests.unit.test_aa802_silent_correction import SECRET, VERSION_ID, _Conn, _request, _version_row
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", SECRET)
    b = jb.build(_result(), final_score=6.0, brand_claims=bc.check({"summary": "For kids aged 7-16."}, SOURCE))
    row = {**_version_row(), "judge_breakdown": json.dumps(b) if stored else None}
    out = await cq.get_tour_version(VERSION_ID, _request(_Conn(row)), x_admin_secret=SECRET)
    assert out["judge_breakdown"] == (b if stored else None)
    assert "ttv.judge_breakdown" in cq._VERSION_DETAIL_SQL and "judge_binding" in cq._JUDGE_LIST_COLUMNS
