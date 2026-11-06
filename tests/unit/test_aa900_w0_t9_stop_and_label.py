"""AA-900 W0 (root ADR 0020 D6, audit critic F5 / F15) — T9 attempt 3 is "stop and label" at $0, every failing gate
of every attempt is stored, and the F9 classes keep infrastructure and text failures out of the fidelity count.

Red on base (the legacy loop, `T9_LAST_ATTEMPT_MODE=legacy`, which is what main runs today): attempt 3 pays for a
second sentence repair and the F9 judge again, or cuts the flagged sentence in code; a repair_log entry has no
`failing` list and a held piece no typed reason. Green on this branch (the default mode)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.acp_content_writing import f9_policy, gate_eval, service
from services.quality import registry as qr
from tests.unit.test_aa777_f9_sticky_cut import CHEF, CTA, LONG_BLOG, SHORT_BLOG, _run_blog

F1_FAIL = {"gate": qr.F1_GROUNDING, "passed": False, "blocking": True, "repairable": True,
           "violations": ["sentence states 1888 not present in the content seed: 'It opened in 1888.'"]}


# ---------------------------------------------------------------- stop and label: attempt 3 makes no paid call

@pytest.mark.asyncio
async def test_attempt_three_makes_no_paid_call_and_never_cuts():
    """The chef claim survives attempt 2's repair: attempt 3 is the $0 label — no second repair, no third judge
    call (the gates run on attempts 1 and 2 only), no truth cut; the piece keeps attempt 2's text, held."""
    kw, rep, rqg = await _run_blog(LONG_BLOG)
    assert rep.call_count == 1          # attempt 2's sentence repair only
    assert rqg.call_count == 2          # the F9 judge is not paid a third time
    assert kw["attempt_number"] == 2 and kw["status"] == "held"
    assert CHEF in kw["content_text"] and CTA in kw["content_text"]  # nothing cut, the tenant's CTA untouched
    modes = [e.get("repair_mode") for e in kw["repair_log"]]
    assert not {"truth_cut", "truth_cut+sentences", "sentences_cut", "f9_truth_cut"} & set(modes)
    label = kw["repair_log"][-1]
    assert label["repair_mode"] == "stop_and_label" and label["attempt"] == 3 and label["cost_usd"] == 0.0
    assert label["kept_attempt"] == 2 and label["needs_review"] is True
    assert label["reason_codes"] == ["unsupported_trip_promise"]
    assert label["gate_targeted"] == "stop_and_label" and qr.gate(label["gate_targeted"]) is None
    assert "gate_ledger" not in label  # not an attempt (the admin drawer lists repair_log[].gate_ledger)


@pytest.mark.asyncio
async def test_legacy_mode_still_pays_for_attempt_three(monkeypatch):
    """The rollback switch: the base behaviour (a paid attempt 3 with a code truth cut on the blog)."""
    monkeypatch.setenv(service.LAST_ATTEMPT_MODE_ENV, service.LEGACY_LAST_ATTEMPT)
    kw, rep, rqg = await _run_blog(LONG_BLOG)
    assert rqg.call_count == 3 and kw["attempt_number"] == 3
    assert any(e.get("repair_mode") == "truth_cut" for e in kw["repair_log"])


@pytest.mark.asyncio
async def test_short_blog_also_stops_at_zero_cost():
    """A blog under its minimum took a second paid repair on base (the refused cut fell back to it)."""
    kw, rep, rqg = await _run_blog(SHORT_BLOG)
    assert rep.call_count == 1 and rqg.call_count == 2
    assert kw["status"] == "held" and kw["held_reason"].startswith(qr.F9_CTA_FACT)
    assert not any(e.get("truth_cut_refused") for e in kw["repair_log"])


def _ctx(channel="linkedin"):
    return {"atom_text": "seed", "goal": {"key": "promotion", "name": "P", "description": "d", "logic": "l"},
            "channel_style": {"channel": channel}, "brand_audience": {}, "chosen": {"name": "a"}, "cta": "Ask",
            "destination": None, "trip_name": None, "brand_rubric_text": "r", "channel": channel,
            "atom_id": "a1", "tenant_id": None}


def _outcome(*entries):
    ledger = [dict(e) for e in entries]
    first = next((g for g in ledger if not g["passed"] and g.get("blocking", True)), None)
    return {"passed": first is None, "gate_ledger": ledger, "first_failure": first, "flags": []}


F9_PROMISE = {"gate": qr.F9_CTA_FACT, "passed": False, "blocking": True, "repairable": True,
              "violations": ['audit flagged: FACT_CHECK_MANUAL_CHECK — unsupported claim(s): '
                             '"You will ride a camel at dawn." (claims: a camel ride; source: silent)']}
F9_OUTAGE = {"gate": qr.F9_CTA_FACT, "passed": False, "blocking": False, "repairable": False,
             "degraded": "f9_judge_unavailable", "violations": ["judge unavailable: timeout — not checked"]}


@pytest.mark.asyncio
async def test_linkedin_stop_and_label_has_no_truth_cut():
    """LinkedIn cut its flagged F9 sentences on the last attempt (AA-786); in W0 it stops and labels instead."""
    text = "Opening line here. You will ride a camel at dawn. End line."
    item = [{"start": 19, "end": 50, "sentence": "You will ride a camel at dawn.", "problem": "p",
             "gate": qr.F9_CTA_FACT, "gates": [qr.F9_CTA_FACT]}]
    with patch.object(service, "write_content", return_value=(text, 0.01, {}, None)), \
            patch.object(service, "rewrite_with_feedback") as rewrite, \
            patch.object(service, "repair_sentences", return_value=(["You will ride a camel at dawn."], 0.01)) as rep, \
            patch.object(service, "run_quality_gates",
                         side_effect=[_outcome(F9_PROMISE), _outcome(F9_PROMISE), _outcome()]) as rqg, \
            patch.object(service.sentence_repair, "plan", side_effect=lambda *a, **k: [dict(i) for i in item]), \
            patch.object(service, "_finalize_piece", new=AsyncMock()) as fin:
        await service.run_write_background(uuid4(), uuid4(), _ctx(), pool=MagicMock())
    rewrite.assert_not_called()
    assert rep.call_count == 1 and rqg.call_count == 2
    kw = fin.await_args.kwargs
    assert kw["status"] == "held" and "camel" in kw["content_text"]
    assert not any(e.get("repair_mode") in ("f9_truth_cut", "f9_cut_refused") for e in kw["repair_log"])
    assert kw["repair_log"][-1]["repair_mode"] == "stop_and_label"


# ---------------------------------------------------------------- every failing gate per attempt

@pytest.mark.asyncio
async def test_every_failing_gate_of_every_attempt_is_stored():
    """held_reason names only the first gate; each attempt's repair_log entry lists every failing gate, the label
    every hold reason (F1 and F9 here), and the outage marker counts as judge_unavailable, not as fidelity."""
    a1 = _outcome(F1_FAIL, F9_PROMISE)
    a2 = _outcome(F1_FAIL, F9_PROMISE)
    with patch.object(service, "write_content", return_value=("Text one. It opened in 1888.", 0.01, {}, None)), \
            patch.object(service, "rewrite_with_feedback", return_value=("Text two.", 0.01, {}, None)), \
            patch.object(service, "run_quality_gates", side_effect=[a1, a2]), \
            patch.object(service.sentence_repair, "plan", return_value=None), \
            patch.object(service, "_finalize_piece", new=AsyncMock()) as fin:
        await service.run_write_background(uuid4(), uuid4(), _ctx(), pool=MagicMock())
    kw = fin.await_args.kwargs
    assert kw["held_reason"].startswith(qr.F1_GROUNDING)
    attempts = [e for e in kw["repair_log"] if "gate_ledger" in e]
    assert [e["attempt"] for e in attempts] == [1, 2]
    for e in attempts:
        assert [r["source"] for r in e["failing"]] == [qr.F1_GROUNDING, qr.F9_CTA_FACT]
        assert all(r["may_hold"] for r in e["failing"])
        assert e["failing"][1]["classes"] == {f9_policy.TRIP_PROMISE: 1}
    label = kw["repair_log"][-1]
    assert label["repair_mode"] == "label"  # a full-rewrite failure at attempt 2: base held here too
    assert label["reason_codes"] == ["unsupported_number", "unsupported_trip_promise"]
    assert label["failing_schema"] == gate_eval.FAILING_SCHEMA


def test_failing_gates_splits_judge_unavailable_and_text_integrity_from_fidelity():
    ledger = [
        {"gate": qr.F1_GROUNDING, "passed": True, "blocking": True, "violations": []},
        F9_OUTAGE,
        {"gate": qr.F9_BRAND_STYLE, "passed": True, "blocking": False, "violations": []},
    ]
    rows = gate_eval.failing_gates(ledger)
    assert rows == [{"gate": qr.F9_CTA_FACT, "source": qr.F9_CTA_FACT, "blocking": False, "may_hold": False, "n": 1,
                     "classes": {f9_policy.JUDGE_UNAVAILABLE: 1}, "degraded": "f9_judge_unavailable"}]
    counts = gate_eval.f9_class_counts(rows)
    assert not set(counts) & f9_policy.FIDELITY_CLASSES
    # the pre-AA-925 outage text ('… treated as fail') and TEXT_INTEGRITY are classed apart too
    old = {"gate": qr.F9_CTA_FACT, "passed": False, "blocking": True,
           "violations": ["judge unavailable: empty content at max_tokens — treated as fail",
                          "TEXT_INTEGRITY: 'The the road ends.'"]}
    assert gate_eval.f9_class_counts(gate_eval.failing_gates([old])) == {f9_policy.JUDGE_UNAVAILABLE: 1,
                                                                         f9_policy.TEXT_INTEGRITY: 1}


def test_ledger_snapshot_keeps_the_f9_classes():
    snap = gate_eval.ledger_snapshot([F9_PROMISE, F9_OUTAGE])
    assert snap[0]["classes"] == {f9_policy.TRIP_PROMISE: 1}
    assert snap[1]["classes"] == {f9_policy.JUDGE_UNAVAILABLE: 1} and snap[1]["degraded"] == "f9_judge_unavailable"


def test_hold_reason_codes_cover_every_hold_capable_gate_and_degraded():
    style = {"gate": qr.F15_VOICE, "passed": False, "blocking": True, "violations": ["stock phrase 'x'"]}
    assert gate_eval.hold_reason_codes([F1_FAIL, style, F9_PROMISE]) == ["unsupported_number",
                                                                         "unsupported_trip_promise"]
    assert gate_eval.hold_reason_codes([], "degraded: f9_judge_unavailable") == [gate_eval.DETECTOR_DEGRADED]


def test_stop_label_is_none_for_an_approved_piece():
    assert service._stop_label([], "approved", None, stopped=False, kept_attempt=1) is None
    stopped = service._stop_label([], "approved", None, stopped=True, kept_attempt=2)
    assert stopped["repair_mode"] == "stop_and_label" and stopped["reason_codes"] == []
