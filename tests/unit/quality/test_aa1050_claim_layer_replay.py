"""AA-1050 (S294 D6) — the $0 replay of the dormant claim layers (services/quality/claim_layer_replay.py).

Fakes only: no DB, no spaCy, no model. The tier-1 run, the gate-input rebuild and the tier-2 triggers are injected;
`modes.apply` (pure) is the production one. Invented names and places only.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.acp_content_writing.seed_align import modes as sa_modes
from services.quality import claim_layer_replay as clr

GATE = "F17_trip_fact"
SENT = "Every guest rows across Lake Varnel at dawn."


def _ledger(passed=True, violations=()):
    return [{"gate": GATE, "passed": passed, "blocking": True, "repairable": True, "violations": list(violations)}]


def _piece(pid, status="approved", ledger=None, writer_input=None, text=f"Intro line. {SENT}"):
    return {"piece_id": pid, "channel": "blog", "status": status, "content_text": text,
            "gate_ledger": ledger if ledger is not None else _ledger(), "writer_input": writer_input or {"seed": "s"}}


def _run(cands=(), status="ok"):
    rec = {"status": status, "modes": {"optional_as_planned": "repair"},
           "candidates": [{"cls": c, "mode": "repair", "gate": GATE, "reason": "r", "sentence": SENT} for c in cands]}
    live = {GATE: [f"sa:{c}: r: '{SENT}'" for c in cands]} if cands else {}
    return sa_modes.SeedAlignRun(live=live, record=rec)


def _tools(runs, triggers=None, fail=()):
    def inputs(piece):
        if piece["piece_id"] in fail:
            raise KeyError("seed")
        return {"trip_ctx": None, "hygiene_ctx": None, "brief_texts": {}, "seed": "s"}
    return clr.PieceTools(inputs=inputs, tier1=lambda text, inp: runs.pop(0), expand=lambda led: list(led),
                          triggers=lambda text, inp, rec: list((triggers or {}).get(text, [])))


def test_no_candidate_is_zero_diff():
    out = clr.replay_pieces([_piece("p1"), _piece("p2", status="held")], _tools([_run(), _run()]))
    t1 = out["tier1"]
    assert t1["replayed"] == 2 and t1["decisions_changed"] == 0 and t1["pieces_with_candidate"] == 0
    assert clr.seed_align_layer(out)["zero_diff"] is True


def test_candidate_on_published_piece_is_a_changed_decision():
    out = clr.replay_pieces([_piece("p1"), _piece("p2", status="held")],
                            _tools([_run(["optional_as_planned"]), _run(["near_as_at"])]))
    t1 = out["tier1"]
    assert t1["decisions_changed"] == 1 and t1["changed_sample"] == ["p1"] and t1["changed_count"] == 1
    assert t1["decisions_changed_by_status"] == {"approved": 1, "held": 1}
    assert t1["hold_capable_fires"] == {"near_as_at": 1, "optional_as_planned": 1}
    assert t1["holds"] == 0          # HOLD_PROMOTED is empty
    assert clr.seed_align_layer(out)["zero_diff"] is False


def test_sentence_already_quoted_by_the_gate_is_no_new_decision():
    led = _ledger(passed=False, violations=[f"optional told as planned: '{SENT}'"])
    out = clr.replay_pieces([_piece("p1", ledger=led)], _tools([_run(["optional_as_planned"])]))
    assert out["tier1"]["decisions_changed"] == 0 and out["tier1"]["pieces_with_candidate"] == 1


def test_stored_sa_finding_is_stripped_before_the_comparison():
    led = _ledger(passed=False, violations=[f"sa:optional_as_planned: r: '{SENT}'"])
    out = clr.replay_pieces([_piece("p1", ledger=led)], _tools([_run(["optional_as_planned"])]))
    assert out["tier1"]["decisions_changed"] == 1
    assert out["tier1"]["stored"]["live_findings_on_published"] == 1


def test_strip_sa_keeps_own_findings_and_their_blocking():
    g = {"gate": GATE, "passed": False, "blocking": True, sa_modes.BASE_BLOCKING_KEY: False,
         "violations": ["meta mentions a plan", "sa:near_as_at: r: 'x'"]}
    [s] = clr.strip_sa([g])
    assert s["violations"] == ["meta mentions a plan"] and s["passed"] is True and s["blocking"] is False
    assert sa_modes.BASE_BLOCKING_KEY not in s
    [only] = clr.strip_sa([{**g, "violations": ["sa:near_as_at: r: 'x'"]}])
    assert only["violations"] == [] and only["passed"] is True


def test_rebuild_error_is_counted_not_fatal():
    out = clr.replay_pieces([_piece("bad"), _piece("ok")], _tools([_run()], fail={"bad"}))
    assert out["tier1"]["errors"] == {"KeyError": 1} and out["tier1"]["replayed"] == 1


def test_stored_records_and_tier2_live_on_published():
    wi = {"seed": "s", "seed_align": [
        {"attempt": 1, "status": "ok", "candidates": [{"cls": "near_as_at", "mode": "shadow"}],
         "tier2": {"status": "ok", "findings": [{"cls": "naming_note_leak"}], "live": {"cls": "naming_note_leak"},
                   "cost_usd": 0.002}},
        {"attempt": 2, "status": "nlp_unavailable", "candidates": []}]}
    out = clr.replay_pieces([_piece("p1", writer_input=wi), _piece("p2", status="held", writer_input=wi)],
                            _tools([_run(), _run()]))
    st1, st2 = out["tier1"]["stored"], out["tier2"]["stored"]
    assert st1["pieces_with_record"] == 2 and st1["attempts"] == 4
    assert st1["status"] == {"nlp_unavailable": 2, "ok": 2} and st1["candidates_by_mode"] == {"shadow": 2}
    assert st2["live_on_published"] == 1 and st2["live_sample"] == ["p1"] and st2["findings"] == 2
    assert st2["cost_usd"] == pytest.approx(0.004)
    layer = clr.adjudicate_layer(out, clr.ledger_summary([]))
    assert layer["decisions_changed"] == 1 and layer["measurable_at_0_usd"] is False


def test_tier2_triggers_counted_and_capped_per_piece():
    from services.acp_content_writing.seed_align import adjudicate as adj
    text = "Intro line. " + SENT
    trig = [SimpleNamespace(cls="scopeless_superlative")] * (adj.MAX_ITEMS + 2)
    out = clr.replay_pieces([_piece("p1", text=text)], _tools([_run()], triggers={text: trig}))
    t2 = out["tier2"]
    assert t2["pieces_with_trigger"] == 1 and t2["would_send"] == adj.MAX_ITEMS
    assert t2["triggers_by_class"] == {"scopeless_superlative": adj.MAX_ITEMS + 2}


def test_t10_claims_layer_counts_object_calls_only():
    led = clr.ledger_summary([{"stage": "t10_claims", "calls": 4, "object_calls": 0, "pieces": 0, "shadow_calls": 0,
                               "cost_usd": 0.0012, "last_at": None}])
    layer = clr.t10_claims_layer(led, {"t10_claims": {"is_active": True}})
    assert layer["zero_diff"] is True and layer["ledger"]["calls"] == 4
    led2 = clr.ledger_summary([{"stage": "t10_claims", "calls": 4, "object_calls": 1}])
    assert clr.t10_claims_layer(led2, {})["decisions_changed"] == 1
    assert clr.ledger_summary([])["claim_shadow"]["calls"] == 0


def test_shadow_layer_counts_hold_or_review_on_published_objects():
    rows = [{"surface": "t9_blog", "shadow_action": "hold", "object_status": "approved", "object_id": "a"},
            {"surface": "t9_blog", "shadow_action": "review", "object_status": "held", "object_id": "b"},
            {"surface": "t2", "shadow_action": "review", "object_status": "ai_generated", "object_id": "c"},
            {"surface": "t2", "shadow_action": "warn", "object_status": "approved", "object_id": "d"}]
    layer = clr.shadow_layer(rows, clr.ledger_summary([]))
    assert layer["decisions_changed"] == 2 and layer["changed_sample"] == ["a", "c"]
    assert layer["would_change_by_action"] == {"hold": 1, "review": 1} and layer["removal_candidate"] is False


def test_settings_in_force_prefers_the_approved_value():
    rows = [{"key": "cis.quality_modes.t10_seed_align", "default_value": '"off"', "value": '"shadow"'},
            {"key": "cis.quality_modes.t10_adjudicate", "default_value": '"off"', "value": None}]
    s = clr.settings_in_force(rows)
    assert s["cis.quality_modes.t10_seed_align"] == {"value": "shadow", "source": "approved"}
    assert s["cis.quality_modes.t10_adjudicate"] == {"value": "off", "source": "default"}


def test_recommendation_follows_d6():
    zero = {"zero_diff": True, "decisions_changed": 0}
    rec = clr.recommendation({"t10_claims": zero, "seed_align_tier1": zero, "t10_adjudicate": zero,
                              "claim_shadow": zero})
    assert rec["t10_claims"].startswith("remove") and "keep off" in rec["t10_adjudicate"]
    assert rec["claim_shadow"].startswith("keep")
    rec2 = clr.recommendation({"t10_claims": {"zero_diff": False, "decisions_changed": 1},
                               "seed_align_tier1": {"zero_diff": False, "decisions_changed": 3},
                               "t10_adjudicate": zero, "claim_shadow": zero})
    assert rec2["t10_claims"].startswith("keep") and rec2["seed_align_tier1"].startswith("3 published")


def test_args_from_env_and_bounds(monkeypatch):
    monkeypatch.setenv("REPLAY_DAYS", "30")
    monkeypatch.setenv("REPLAY_LIMIT", "50")
    a = clr.parse_args([])
    assert (a.days, a.limit) == (30, 50)
    with pytest.raises(SystemExit):
        clr.parse_args(["--days", "0"])
    with pytest.raises(SystemExit):
        clr.parse_args(["--limit", str(clr.MAX_LIMIT + 1)])


def test_read_only_sql_and_no_model_call():
    """Every statement is a SELECT, the session is read only, and the module imports no model client."""
    for name in ("PIECES_SQL", "LEDGER_SQL", "ROUTES_SQL", "SETTINGS_SQL", "SHADOW_SQL"):
        assert getattr(clr, name).lstrip().upper().startswith("SELECT"), name
    src = Path(clr.__file__).read_text(encoding="utf-8")
    assert '"default_transaction_read_only": "on"' in src
    tree = ast.parse(src)
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
           {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and ("llm_client" in m or "boto3" in m or "anthropic" in m or "openai" in m) for m in mods)
    assert "find_triggers" in src and "adjudicate(" not in src.replace("adjudicate_layer(", "")
