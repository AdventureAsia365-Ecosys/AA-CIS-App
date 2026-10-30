"""AA-788 — the $0 promotion replay script (scripts/aa788/promotion_replay.py): the stored reading, the default vs
promoted replay through the real PieceReplay, and the summary. No DB, no LLM."""
import json
import os

from scripts.aa788 import promotion_replay as pr
from services.acp_content_writing import quality_gates as qg
from services.quality import registry as qr
from tests.unit.test_aa788_t10_promotions import (BRAND, CLEAN, CTA, F9_STYLE_VIOLATION, OUT_OF_SCOPE, SCOPE, SEED,
                                                  STYLE_SENTENCE, TRIP)

WI = {"seed": SEED, "scope": SCOPE, "trip": TRIP, "brand_pack": {"brand_name": BRAND}, "cta": CTA, "channel": "email"}


def _ledger(*entries):
    return [qg._result(qr.F9_CTA_FACT, [])] + list(entries)


def _piece(text, ledger, *, attempt=1, status="approved", discarded=None, repair_log=None, wi=WI, pid="p1"):
    return {"piece_id": pid, "status": status, "channel": "email", "attempt_number": attempt, "content_text": text,
            "gate_ledger": json.dumps(ledger), "repair_log": json.dumps(repair_log or []),
            "discarded_attempts": json.dumps(discarded or []), "writer_input": json.dumps(wi) if wi else None}


def test_attempt_one_sources():
    a1 = _ledger(qg._result(qr.F12_SCOPE, ["names 'Gobi', outside this piece's scope: 'x y z w'"], blocking=False))
    assert pr.attempt_one(_piece("final", a1)) == ("final", a1)
    d = [{"attempt_number": 1, "content_text": "first", "gate_ledger": a1}]
    assert pr.attempt_one(_piece("final", [], attempt=2, discarded=d)) == ("first", a1)
    rl = [{"attempt": 1, "gate_targeted": "F1_grounding", "gate_ledger": a1}]
    assert pr.attempt_one(_piece("final", [], attempt=2, repair_log=rl)) == (None, a1)
    assert pr.attempt_one(_piece("final", [], attempt=3)) == (None, None)


def test_f12_where_and_kinds():
    body = "names 'Gobi', outside this piece's scope: 'The dunes of the Gobi glow.'"
    title = "title names 'Gobi', outside this piece's scope: 'Gobi Guide'"
    assert pr.f12_where({"passed": False, "violations": [title]}) == "title_meta"
    assert pr.f12_where({"passed": False, "violations": [title, body]}) == "body"
    assert pr.f12_where({"passed": True, "violations": []}) is None and pr.f12_where(None) is None
    assert pr.f12_finding_kind(body) == ("body_place", "Gobi")
    assert pr.f12_finding_kind(title) == ("title_meta_place", "Gobi")
    assert pr.f12_finding_kind("states the whole trip's length ('15 days'), but …: 'x'") == ("trip_length", None)
    assert pr.f12_finding_kind("says 'concludes', but the trip goes on after this piece's last day: 'x'")[0] \
        == "journey_end"


def test_stored_stats_reads_each_check():
    text = f"{CLEAN} {STYLE_SENTENCE} {CTA}"
    ledger = _ledger(
        qg._result(qr.F12_SCOPE, [f"names 'Gobi', outside this piece's scope: '{OUT_OF_SCOPE}'"], blocking=False),
        qg._result(qr.F9_BRAND_STYLE, [F9_STYLE_VIOLATION], repairable=False, blocking=False))
    s = pr.stored_stats(_piece(text, ledger), qg.brand_style_sentences)
    assert s["f12_final"] == s["f12_attempt1"] == "body" and s["f12_findings"] == [("body_place", "Gobi")]
    assert s["f9_style_attempt1_fail"] and s["f9_style_attempt1_locatable"] is True and not s["f14_floor_attempt1"]
    s2 = pr.stored_stats(_piece(CLEAN, ledger), qg.brand_style_sentences)  # the phrase is not in this text
    assert s2["f9_style_attempt1_locatable"] is False


def test_env_restores_the_environment(monkeypatch):
    monkeypatch.delenv("T10_F12_SCOPE_MODE", raising=False)
    monkeypatch.setenv("T10_F9_BRAND_STYLE_MODE", "warn")
    with pr.env(pr.PROMOTED_ENV):
        assert qg.f12_scope_mode() == "block" and qg.f9_brand_style_mode() == "repair"
    assert "T10_F12_SCOPE_MODE" not in os.environ and os.environ["T10_F9_BRAND_STYLE_MODE"] == "warn"


def test_replay_finds_a_new_f12_hold_and_an_extra_style_repair():
    text = f"{CLEAN} {OUT_OF_SCOPE} {CTA}"
    stored = _ledger(qg._result(qr.F9_BRAND_STYLE, [], blocking=False))
    held = pr.replay_piece(_piece(text, stored, attempt=2), pr.make_runner(_piece(text, stored)))
    assert held["error"] is None and held["base_hold"] is None and held["promoted_hold"] == qr.F12_SCOPE
    assert held["a1_ran"] is False  # attempt 1's text is not stored

    style_text = f"{CLEAN} {STYLE_SENTENCE} {CTA}"
    style = _ledger(qg._result(qr.F9_BRAND_STYLE, [F9_STYLE_VIOLATION], repairable=False, blocking=False))
    p = _piece(style_text, style)
    out = pr.replay_piece(p, pr.make_runner(p))
    assert out["base_hold"] is None and out["promoted_hold"] is None  # never a hold
    assert out["a1_ran"] and out["a1_base_first"] is None and out["a1_promoted_first"] == qr.F9_BRAND_STYLE

    clean = _piece(f"{CLEAN} {CTA}", _ledger(qg._result(qr.F9_BRAND_STYLE, [], blocking=False)))
    same = pr.replay_piece(clean, pr.make_runner(clean))
    assert same["base_hold"] is None and same["promoted_hold"] is None and same["a1_promoted_first"] is None
    assert pr.replay_piece(_piece("x", [], wi=None), lambda *a: []) is None


def test_summarize_counts_new_holds_and_panel_light():
    def row(pid, promoted_hold, a1p=None, f12="body"):
        return {"piece_id": pid, "status": "approved", "channel": "blog",
                "stored": {"has_attempt1": True, "f12_final": f12, "f12_attempt1": f12,
                           "f12_findings": [("body_place", "Gobi")] if f12 else [],
                           "f9_style_final_fail": False, "f9_style_attempt1_fail": bool(a1p),
                           "f9_style_attempt1_locatable": bool(a1p) or None, "f14_floor_attempt1": False,
                           "f14_under_final": False},
                "replay": {"error": None, "base_hold": None, "promoted_hold": promoted_hold, "a1_ran": True,
                           "a1_base_first": None, "a1_promoted_first": a1p}}
    rows = [row("a", qr.F12_SCOPE), row("b", None, qr.F9_BRAND_STYLE, f12=None), row("c", None, f12=None),
            {"piece_id": "d", "status": "held", "channel": "linkedin", "replay": None,
             "stored": {**row("x", None)["stored"], "f12_final": None, "f12_findings": []}}]
    out = pr.summarize(rows, {"a": ["light", "light", "heavy"], "c": ["as_is"]})
    assert out["pieces"] == 4 and out["stored"]["f12_final_body"] == 1 and out["stored"]["f12_places"] == {"Gobi": 1}
    r = out["replay"]
    assert r["replayed"] == 3 and r["no_writer_input"] == 1 and r["new_holds"] == 1
    assert r["new_hold_rate_pct"] == 33.3 and r["new_holds_by_gate"] == {qr.F12_SCOPE: 1}
    assert r["new_holds_on_panel_light"] == ["a"] and r["labelled_light_replayed"] == 2
    assert r["attempt1_extra_repairs_by_gate"] == {qr.F9_BRAND_STYLE: 1}


def test_majority_label():
    assert pr.majority_label(["light", "light", "heavy"]) == "light"
    assert pr.majority_label(["light", "heavy"]) is None and pr.majority_label([]) is None
