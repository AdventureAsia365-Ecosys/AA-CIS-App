"""AA-788 (S287) — the 20-piece T10 pilot (scripts/aa788/pilot.py) and the rules the F12_scope flip now depends on:
no code switch (the mode is only the setting), the F14 under-length reading as `length_short` (length_contract, no floor
of its own), F9_brand_style never holding, and the evidence a `block` proposal must cite (quality_settings). No DB, no
model call: the loop's piece writes are captured from fakes. Invented places and brands only."""
import uuid
from unittest.mock import MagicMock

import pytest

from scripts.aa788 import pilot
from services.acp_content_writing import gate_eval, length_contract
from services.acp_content_writing import hygiene as h
from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service as writing_service
from services.acp_shared.channel_spec import linkedin_spec
from services.quality import quality_settings
from services.quality import registry as qr
from services.quality import settings_keys
from tests.unit.settings_serve import serve

TRIP = "Norland: Selin Lake and Varra Desert — 15 Days"
SCOPE = "SCOPE: this piece covers days 3–4 of the trip, in this order: Orin Camp → Kessa Rock → Tarrow."
SEED = ("Orin Camp — overnight in a simple tent camp\n"
        "Location: Orin Camp, Selin Lake area, Selin Province, Norland\n"
        "Day 3 of the trip\n"
        "Source itinerary: Arrive at Orin Camp in the afternoon. Overnight in a simple tent camp.\n\n"
        "Kessa Rock — trek into the caldera of a dormant volcano\n"
        "Location: Kessa Rock Volcano, Hale Province, Norland\n"
        "Day 3 of the trip\n"
        "Source itinerary: Journey to Kessa Rock, a dormant volcano.")
BRAND = "Example Trails Co"
CTA = "Ask Example Trails Co to plan this trip for your family."
OUT_OF_SCOPE = "The dunes of the Varra are far from this quiet hill."
CLEAN = "The trek climbs into the caldera of Kessa Rock, a dormant volcano above open grassland."
F9_STYLE = 'audit flagged: GENERIC_AI_WORDING — exact flagged phrase(s): "plinth marvel"'
TRAIL = {"brand_name": "Trail Pulse", "brand_type": "Young active adventure travel brand",
         "voice": ["Energetic", "Fresh", "Direct"]}
SHORT_POST = "Day 3 ends at Orin Camp.\n\nThe tent camp sits on the lake shore.\n\nAsk us for the dates."
F12_KEY = settings_keys.MODES["t10_f12_scope"]
RUN = "p202611220101abcd"


def _ctx() -> h.HygieneContext:
    return h.HygieneContext(seed=SEED, scope=SCOPE, trip_name=TRIP, brand_name=BRAND, cta=CTA)


def _f12(text: str, title=None) -> dict:
    with serve({F12_KEY: "block"}):
        return qg.gate_scope(text, _ctx(), title, None)


def _f9(style=(), blocking=False) -> dict:
    return qg._result(qr.F9_BRAND_STYLE, list(style), repairable=bool(style), blocking=blocking)


def _cap(*, status="approved", attempts=1, final=(), repair_log=(), held_reason=None) -> dict:
    return {"piece_id": str(uuid.uuid4()), "status": status, "attempt_number": attempts, "gate_ledger": list(final),
            "repair_log": list(repair_log), "held_reason": held_reason}


def _failed_attempt(attempt: int, ledger: list, mode: str = "sentences") -> dict:
    return {"attempt": attempt, "gate_ledger": ledger, "failing": gate_eval.failing_gates(ledger),
            "repair_mode": mode}


# ---------------------------------------------------------------- the F12 mode is a setting only

def test_f12_has_no_code_switch_and_reads_the_setting_at_gate_time():
    assert not hasattr(qg, "F12_SCOPE_BLOCKING")
    assert qg.f12_scope_mode() == qg.WARN_MODE
    assert qg.gate_scope(f"{CLEAN} {OUT_OF_SCOPE}", _ctx())["blocking"] is False
    assert _f12(f"{CLEAN} {OUT_OF_SCOPE}")["blocking"] is True
    with serve({F12_KEY: "nonsense"}):
        assert qg.f12_scope_mode() == qg.WARN_MODE


@pytest.mark.parametrize("violation,body", [
    ("names 'Varra' — outside the scope: 'x'", True),
    ("title names 'Varra'", False),
    ("meta names 'Varra'", False),
])
def test_is_body_finding(violation, body):
    assert pilot.is_body_finding(violation) is body


# ---------------------------------------------------------------- evidence of a holding flip

def test_block_needs_a_pilot_record_reference():
    ok = f"{quality_settings.F12_PILOT_EVIDENCE_PREFIX}{RUN}+p202611230202beef:0123456789ab"
    assert quality_settings.evidence_problem(F12_KEY, "block", ok) is None
    for bad in (None, "", "replay run 42", f"{quality_settings.F12_PILOT_EVIDENCE_PREFIX}{RUN}",
                f"{quality_settings.F12_PILOT_EVIDENCE_PREFIX}{RUN}:XYZ456789abc"):
        assert "measurement record" in quality_settings.evidence_problem(F12_KEY, "block", bad)
    # warn (the way back) and every other key keep the generic evidence rule
    assert quality_settings.evidence_problem(F12_KEY, "warn", "replay run 42") is None
    assert quality_settings.evidence_problem(settings_keys.MODES["t10_f9_brand_style"], "repair", "x") is None


# ---------------------------------------------------------------- F14 under-length reads as length_short

def test_f14_word_count_violations_read_as_length_codes():
    spec = linkedin_spec(TRAIL)
    under = qg.gate_social_shape(SHORT_POST, spec, floor_blocking=True)
    assert under["blocking"] and length_contract.code_for(under) == length_contract.CODE_SHORT
    assert length_contract.ledger_length_codes([under]) == [length_contract.CODE_SHORT]
    lo, hi = spec.words
    long_text = " ".join(["word"] * (hi + 5))
    assert length_contract.ledger_length_codes([qg.gate_social_shape(long_text, spec)]) == [
        length_contract.CODE_LONG]
    assert length_contract.ledger_length_codes([qg.gate_social_shape(" ".join(["w"] * (lo + 2)), spec)]) == []
    # the blog side is unchanged: F4 short / long by its own wording
    f4 = qg._result(qr.F4_EXTREME_LENGTH, [length_contract.short_message(4000, 690)])
    assert length_contract.ledger_length_codes([f4, under]) == [length_contract.CODE_SHORT]


def test_an_f14_violation_never_changes_the_f4_hold_code():
    spec = linkedin_spec(TRAIL)
    under = qg.gate_social_shape(SHORT_POST, spec, floor_blocking=True)
    assert gate_eval.hold_reason_codes([under]) == []  # the floor block never holds (AA-780)


# ---------------------------------------------------------------- the pilot's reading of one piece

def test_inputs_labelled_first_round_robin_and_explicit_ids():
    rows = [{"piece_id": f"{i:08x}" + "0" * 28, "tenant": t, "labels": lab, "created_at": f"2026-11-{10 + i}"}
            for i, (t, lab) in enumerate([("a", None), ("a", ["light"]), ("b", None), ("b", ["heavy"]),
                                          ("a", ["as_is"])])]
    got = [r["piece_id"][:8] for r in pilot.pick_inputs(rows, n=4, pieces=[], skip=[])]
    assert got == ["00000004", "00000003", "00000001", "00000000"]  # labelled (a, b, a), then unlabelled (a)
    assert [r["piece_id"][:8] for r in pilot.pick_inputs(rows, n=9, pieces=["00000002", "00000000"], skip=[])] == [
        "00000002", "00000000"]
    assert "00000004" not in [r["piece_id"][:8] for r in pilot.pick_inputs(rows, n=9, pieces=[], skip=["00000004"])]


def test_an_f12_hold_after_its_repair():
    a1 = [_f12(f"{CLEAN} {OUT_OF_SCOPE}", title="Varra and Kessa Rock"), _f9()]
    final = [_f12(f"{CLEAN} {OUT_OF_SCOPE}"), _f9()]
    cap = _cap(status="held", attempts=2, final=final, repair_log=[_failed_attempt(1, a1)])
    v = pilot.piece_verdicts(cap)
    assert v["status"] == "held" and v["held_gate"] == qr.F12_SCOPE and v["f12"]["held"]
    assert v["f12"]["blocked_attempts"] == [1, 2] and v["f12"]["sentence_repairs"] == 1 and v["f12"]["left_in_body"]
    assert v["hold_codes"] == gate_eval.hold_reason_codes(final)
    fires = pilot.f12_fires(cap, "aaaaaaaa", RUN)
    assert [f["fire_id"] for f in fires] == [f"{RUN}:aaaaaaaa:1", f"{RUN}:aaaaaaaa:2"]
    assert sorted(f["where"] for f in fires) == ["body", "title_meta"]  # the same body finding counted once
    assert {f["place"] for f in fires} == {"Varra"} and all(f["attempt"] == 1 for f in fires)


def test_f9_brand_style_one_repair_never_a_hold():
    with serve({settings_keys.MODES["t10_f9_brand_style"]: "repair"}):
        blocked = qg.promote_brand_style(_f9([F9_STYLE]), f"{CLEAN} Kessa Rock is the plinth marvel of the route, "
                                         "a dormant volcano ringed by pines.", attempt_one=True)
    assert blocked["blocking"]
    still = _f9([F9_STYLE])  # attempt 2: the judge still flags it, as a warning
    cap = _cap(attempts=2, final=[_f12(CLEAN), still], repair_log=[_failed_attempt(1, [_f12(CLEAN), blocked])])
    v = pilot.piece_verdicts(cap)
    assert v["status"] == "approved" and v["held_gate"] is None
    assert v["f9_brand_style"] == {"attempt1_failed": True, "attempt1_blocked": True, "sentence_repairs": 1,
                                   "held": False}
    assert qg.hold_failure(cap["gate_ledger"]) is None


def test_f14_under_length_rewrite_is_read_from_the_ledger():
    under = qg.gate_social_shape(SHORT_POST, linkedin_spec(TRAIL), floor_blocking=True)
    cap = _cap(attempts=2, final=[qg.gate_social_shape(" ".join(["w"] * 140), linkedin_spec(TRAIL))],
               repair_log=[_failed_attempt(1, [under], mode="")])
    v = pilot.piece_verdicts(cap)
    assert v["f14"] == {"attempt1_codes": ["length_short"], "final_codes": [], "attempt1_repair": "rewrite"}


def test_compare_and_summary_done_when_counts():
    approved = {"status": "approved", "hold_codes": [], "f12": {"blocked_attempts": [], "sentence_repairs": 0,
                                                                  "held": False},
                "f14": {"attempt1_codes": ["length_short"], "final_codes": [], "attempt1_repair": "rewrite"},
                "f9_brand_style": {"attempt1_failed": False, "attempt1_blocked": False, "sentence_repairs": 0,
                                   "held": False}}
    held = {**approved, "status": "held", "hold_codes": ["scope"], "f12": {**approved["f12"], "held": True,
                                                                           "blocked_attempts": [1]}}
    light = {"stored_status": "approved", "labels": ["light"]}
    heavy = {"stored_status": "approved", "labels": ["heavy"]}
    pieces = [{"piece": "a", "tenant": "audit-trail-pulse", "verdict": held,
               "compare": pilot.compare_stored(light, held)},
              {"piece": "b", "tenant": "audit-trail-pulse", "verdict": approved,
               "compare": pilot.compare_stored(heavy, approved)},
              {"piece": "c", "tenant": "audit-x", "error": "stopped by the cap"}]
    s = pilot.summarize(pieces, [])
    assert s["done_when"] == {"new_holds_on_light": 1, "false_pass_stored": 1, "false_pass_pilot": 1,
                              "f9_brand_style_holds": 0}
    assert s["written"] == 2 and s["errors"] == ["c"] and s["f12"]["held"] == 1
    assert s["f14"]["under_length_attempt1"] == 2 and s["by_tenant"]["audit-trail-pulse"]["pieces"] == 2


# ---------------------------------------------------------------- the cap

def test_budget_checks_before_every_call_and_every_piece():
    b = pilot.Budget(1.0)
    b.check_call()
    b.add(0.4)
    b.check_call()
    b.add(0.4)  # spent 0.8 + largest 0.4 > 1.0
    with pytest.raises(pilot.PilotCapReached):
        b.check_call()
    assert b.tripped and b.calls == 2
    b2 = pilot.Budget(1.0)
    b2.note_ledger(0.7)  # embeddings / Jev rows seen in the ledger count too
    assert not b2.may_start(0.31) and b2.may_start(0.3)
    assert pilot.piece_estimate(None, 0.2) == 0.2 and pilot.piece_estimate(0.5, 0.2) == 0.5


def test_cap_is_the_setting_or_lower():
    assert pilot.resolve_cap(5, None) == 5.0
    assert pilot.resolve_cap(5, "2.5") == 2.5 and pilot.resolve_cap(5, "9") == 5.0
    for bad in ((None, None), (0, None), (5, "x"), (5, "0")):
        with pytest.raises(ValueError):
            pilot.resolve_cap(*bad)


# ---------------------------------------------------------------- the measurement record

def _record(run_id: str, n_fires: int, sha: str = "abcdef012345") -> dict:
    fires = [{"fire_id": f"{run_id}:p{i:07d}:1", "where": "body"} for i in range(n_fires)]
    return {"schema": pilot.SCHEMA, "run_id": run_id, "detector_sha": sha, "fires": fires,
            "summary": {"written": 20, "done_when": {"new_holds_on_light": 0, "false_pass_stored": 2,
                                                     "false_pass_pilot": 1, "f9_brand_style_holds": 0}}}


def _labels(record: dict, lead: str, panel=None, n=None) -> dict:
    fires = record["fires"][:n] if n is not None else record["fires"]
    return {"labels": {f["fire_id"]: {"tech_lead": lead, "panel": panel if panel else None} for f in fires}}


def test_a_passing_measurement_prints_the_evidence_the_setting_accepts():
    r1, r2 = _record("p1aaaa", 20), _record("p2bbbb", 15)
    m = pilot.measure([r1, r2], [_labels(r1, "tp", "tp"), _labels(r2, "tp", "tp")])
    assert m["passes"] and m["precision"] == 1.0 and m["wilson95_lower"] >= 0.75 and m["agreement"] == 1.0
    assert m["fires"] == 35 and m["labelled_fires"] == 35 and m["done_when"]["false_pass_stored"] == 4
    assert m["evidence_ref"] == f"{quality_settings.F12_PILOT_EVIDENCE_PREFIX}p1aaaa+p2bbbb:{m['record_sha256'][:12]}"
    assert quality_settings.evidence_problem(F12_KEY, "block", m["evidence_ref"]) is None
    assert pilot.measure([r2, r1], [_labels(r2, "tp", "tp"), _labels(r1, "tp", "tp")]) == m  # order-free


def test_a_small_or_imprecise_or_panel_only_measurement_does_not_pass():
    r = _record("p1aaaa", 6)
    m = pilot.measure([r], [_labels(r, "tp", "tp")])
    assert not m["passes"] and m["evidence_ref"] is None and m["more_true_fires_needed"] == 9
    m = pilot.measure([r], [_labels(r, None, "tp")])  # the panel alone is not release evidence
    assert m["labelled_fires"] == 0 and m["precision"] is None and not m["passes"]
    r = _record("p1aaaa", 30)
    labels = _labels(r, "tp", "tp")
    for f in r["fires"][:5]:
        labels["labels"][f["fire_id"]] = {"tech_lead": "fp", "panel": "fp"}
    m = pilot.measure([r], [labels])
    assert m["precision"] == round(25 / 30, 4) and not m["passes"] and len(m["false_fire_ids"]) == 5
    m = pilot.measure([r], [_labels(r, "tp")])  # no second labeller: agreement unknown, the bar needs it
    assert m["agreement"] is None and not m["passes"]


def test_records_of_another_detector_or_twice_do_not_pool():
    with pytest.raises(ValueError, match="detectors"):
        pilot.measure([_record("p1", 3), _record("p2", 3, sha="fedcbafedcba")], [])
    with pytest.raises(ValueError, match="twice"):
        pilot.measure([_record("p1", 3), _record("p1", 3)], [])
    assert len(pilot.detector_sha()) == 12


def test_label_template_lists_every_fire():
    r = _record("p1aaaa", 2)
    assert set(pilot.label_template(r)["labels"]) == {f["fire_id"] for f in r["fires"]}


# ---------------------------------------------------------------- the run's in-process isolation

@pytest.mark.asyncio
async def test_run_piece_writes_nothing_and_runs_under_the_pilot_modes(monkeypatch):
    seen = {}

    async def fake_start(tenant_id, request_id, pool):
        piece = await writing_service._insert_placeholder_piece(pool, tenant_id=tenant_id, request_id=request_id)
        return {"piece": piece, "context": {"channel": "linkedin"}}

    async def fake_loop(request_id, piece_id, context, pool):
        from shared.llm_client.call_log import eval_label
        seen.update(f12=qg.f12_scope_mode(), f9=qg.f9_brand_style_mode(), f14=qg.f14_floor_mode(),
                    label=eval_label())
        await writing_service._finalize_piece(pool, piece_id=piece_id, attempt_number=1, content_text="x",
                                              status="approved", held_reason=None, gate_ledger=[_f12(CLEAN)],
                                              repair_log=[], flags=[])

    monkeypatch.setattr(writing_service, "start_write", fake_start)
    monkeypatch.setattr(writing_service, "run_write_background", fake_loop)
    real_finalize = writing_service._finalize_piece
    row = {"piece_id": "abcdef01" + "0" * 28, "request_id": str(uuid.uuid4()), "tenant_id": str(uuid.uuid4()),
           "tenant": "audit-x", "channel": "linkedin", "stored_status": "approved", "labels": ["light"]}
    with pilot.isolated(pilot.Budget(1.0)):
        out = await pilot.run_piece(row, MagicMock(), run_id=RUN)
    assert seen == {"f12": "block", "f9": "repair", "f14": "repair", "label": (pilot.STAGE, RUN)}
    assert out["verdict"]["status"] == "approved" and out["compare"]["new_hold_on_light"] is False
    assert out["fires"] == [] and not pilot.CAPTURE
    assert writing_service._finalize_piece is real_finalize and qg.f12_scope_mode() == qg.WARN_MODE


def test_isolated_checks_the_cap_before_each_model_call(monkeypatch):
    from shared.llm_client.client import LLMClient
    calls = []
    monkeypatch.setattr(LLMClient, "generate", lambda self, req: calls.append(req) or MagicMock(cost_usd=0.6))
    budget = pilot.Budget(1.0)
    with pilot.isolated(budget):
        client = LLMClient.__new__(LLMClient)
        client.generate("r1")
        with pytest.raises(pilot.PilotCapReached):
            client.generate("r2")  # 0.6 spent + 0.6 largest call > 1.0: refused before the call
    assert calls == ["r1"] and budget.spent() == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_a_paid_run_needs_confirm(monkeypatch, capsys):
    monkeypatch.delenv("CONFIRM", raising=False)
    assert await pilot._main_run(plan_only=False) == 2
    assert "CONFIRM=PAID" in capsys.readouterr().out
