"""AA-1077 — the two read-only scripts: the $0 re-measure (scripts/aa1077/remeasure_length.py) and the capped extend
trial (scripts/aa1077/extend_trial.py). Pure parts only: no DB, no model (the extend call is a fake).
Invented places only (Vellmar Gorge, Lake Orrin, Tessaly).
"""
from services.acp_content_writing import length_contract as lc
from services.acp_content_writing import quality_gates as qg
from services.quality import registry as qr
from scripts.aa1077 import extend_trial as trial
from scripts.aa1077 import remeasure_length as rm

PARA = ("The trail climbs above Vellmar Gorge through pine and juniper, and the river noise fades as the path turns "
        "towards the ridge where the old shepherd huts stand in a loose row.")


def _text(chars: int) -> str:
    out = "## Vellmar Gorge\n\n"
    while len(out) < chars:
        out += PARA + "\n\n"
    return out[:chars]


def _row(pid, status, chars, ledger, repair_log=(), held_reason=None):
    return {"piece_id": pid + "-0000", "tenant": "audit-x", "status": status, "content_text": _text(chars),
            "gate_ledger": list(ledger), "repair_log": list(repair_log), "held_reason": held_reason}


F4_IN = {"gate": qr.F4_EXTREME_LENGTH, "passed": True, "violations": [], "blocking": True}
F4_OLD = {"gate": qr.F4_EXTREME_LENGTH, "passed": False, "blocking": True, "repairable": True,
          "violations": ["content is only 4586 chars — effectively empty"]}
F17 = {"gate": qr.F17_TRIP_FACT, "passed": False, "blocking": True, "repairable": True,
       "violations": ["optional sold as planned: 'The huts are part of the plan.'"]}


def test_readings_of_the_ledger_and_the_trace():
    assert rm.f4_reading([F4_IN]) == "in" and rm.f4_reading([F4_OLD]) == "4586" and rm.f4_reading([]) == "-"
    wi = {"length_trace": {"step": "length_trace", "chars": {"loop": 5012, "final_integrity": 4890}}}
    assert rm.before_normalizer(wi, [F4_IN]) == "5012"
    assert rm.before_normalizer({}, [F4_OLD]) == "4586" and rm.before_normalizer({}, [F4_IN]) == "-"
    assert rm.stored_label([{"step": "x"}, {"reason_codes": ["length"]}]) == ["length"]


def test_an_approved_piece_stored_under_the_floor_is_held_by_the_recheck():
    rows = [rm.measure_piece(_row("aaaaaaaa", "approved", 4426, [F4_IN])),
            rm.measure_piece(_row("bbbbbbbb", "approved", 5200, [F4_IN])),
            rm.measure_piece(_row("cccccccc", "held", 4289, [F4_OLD], [{"reason_codes": ["length"]}], "F4: x")),
            rm.measure_piece(_row("dddddddd", "held", 4100, [F4_OLD, F17], held_reason="F4: x")),
            rm.measure_piece({"piece_id": "eeeeeeee-0", "tenant": "audit-x", "status": "failed", "content_text": "",
                              "gate_ledger": [], "repair_log": []})]
    by = {r["piece"]: r for r in rows}
    assert by["aaaaaaaa"]["new_status"] == "held" and by["aaaaaaaa"]["codes"] == ["length_short"]
    assert by["bbbbbbbb"]["new_status"] == "approved" and by["bbbbbbbb"]["verdict"] == "in"
    assert by["cccccccc"]["length_only"] and by["cccccccc"]["label"] == ["length"]
    assert not by["dddddddd"]["length_only"]   # F4 + F17: not a length-only piece
    s = rm.summarize(rows)
    assert s["approved_under_floor_stored"] == ["aaaaaaaa"] and s["approved_under_floor_after"] == []
    # a piece the re-check holds on length alone is a length-only piece too
    assert s["held_by_recheck"] == ["aaaaaaaa"] and s["length_only"] == ["aaaaaaaa", "cccccccc"]
    assert s["ledger_vs_stored_drift"] == 2 and s["verdicts"]["empty"] == 1


def test_the_cap_check():
    assert trial.may_spend(0.0, 0.0, 0.10)              # first piece: guess 0.05
    assert trial.may_spend(0.05, 0.05, 0.10)
    assert not trial.may_spend(0.06, 0.05, 0.10)
    assert not trial.may_spend(0.0, 0.0, 0.04)


SOURCE = ("Vellmar Gorge is a limestone canyon cut by the Ast river over many thousands of years.\n"
          "The shepherd huts on the ridge are built from flat river stones without any mortar.\n")
CTA = "Write to us to plan your days at Lake Orrin."
BODY = ("Vellmar Gorge rewards a slow walk.\n\n## Above Vellmar Gorge\n\nThe trail climbs above the gorge through pine "
        "and juniper.\n\n## Lake Orrin\n\nLake Orrin lies still below the ridge.\n\n" + CTA
        + "\n\n## FAQ\n\n**Q: Where is Lake Orrin?**\nA: Lake Orrin lies below the ridge.\n")


def test_trial_piece_runs_base_extend_after_with_stored_f9(monkeypatch):
    from services.acp_content_writing import length_extend
    monkeypatch.setenv(length_extend.EXTEND_ENV, "on")
    calls = []

    def fake(*, sections, facts, voice=None, **kw):
        calls.append(kw)
        return (["The canyon below was cut by the Ast river over many thousands of years."]
                + [""] * (len(sections) - 1), 0.02, "writer-model")
    f4 = qg.gate_extreme_length(qg.strip_citation_tags(BODY), "blog")
    row = {"piece_id": "ffffffff-0000", "tenant_id": "t-1", "tenant": "audit-x", "status": "held",
           "content_text": BODY, "gate_ledger": [f4],
           "writer_input": {"seed": SOURCE, "channel": "blog", "cta": CTA,
                            "brand_pack": {"brand_name": "Orrin Trails"}}}
    out = trial.trial_piece(row, call=fake, live_f9=False)
    assert "length_short" in out["base_codes"] and out["cost_usd"] == 0.02
    assert out["extend"]["added"] == 1 and out["chars_after"] > out["chars_before"]
    assert out["would_be"] == "held" and "length_short" in out["after_codes"]   # still short: honest
    assert calls[0]["content_piece_id"] == "ffffffff-0000"
    s = trial.summarize([out, {"piece": "gggggggg", "skip": "not found"}], 0.02, 0.10, [])
    assert s["trialled"] == 1 and s["skipped"] == ["gggggggg"] and s["would_be_approved"] == 0


def test_trial_piece_skips_without_a_seed():
    out = trial.trial_piece({"piece_id": "hhhhhhhh-0", "writer_input": {}}, call=None, live_f9=False)
    assert out["skip"] == "no writer_input seed"


def test_floor_is_the_contract():
    assert rm.summarize([])["floor_chars"] == lc.floor_chars()


# ---------------------------------------------------------------- round 2 review: the F13_leak replay (leak_replay.py)

def test_leak_replay_old_pattern_is_mains_and_counts_only_new_hits():
    from scripts.aa1077 import leak_replay as lr
    from services.acp_content_writing import hygiene
    assert hygiene.LEAK_RE.pattern != lr.OLD_LEAK_PATTERN
    rows = [
        {"piece_id": "aaaaaaaa-1", "tenant": "audit-x", "channel": "blog",
         "content_text": "## Vellmar Gorge\n\nThe itinerary says the huts at Vellmar Gorge are open in summer. "
                         "The itinerary gives you a free afternoon at Lake Orrin.\n"},
        {"piece_id": "bbbbbbbb-2", "tenant": "audit-x", "channel": "linkedin",
         "content_text": "The source notes name the huts above Tessaly. Lake Orrin is calm in the morning.\n"},
        {"piece_id": "cccccccc-3", "tenant": "audit-y", "channel": "blog",
         "content_text": "## Tessaly\n\nOur itinerary lists two nights in Tessaly.\n"},
    ]
    lines, summary = lr.summarize(rows, hygiene.LEAK_RE)
    # only the provenance phrase main did not catch counts; 'source notes' was already main's, reader copy never
    assert [(x["piece"], x["phrases"]) for x in lines] == [("aaaaaaaa", ["the itinerary says"])]
    assert summary["approved_pieces"] == 3 and summary["pieces_with_new_hit"] == 1
    assert summary["new_hit_sentences"] == 1 and summary["by_channel"] == {"blog": 2, "linkedin": 1}
