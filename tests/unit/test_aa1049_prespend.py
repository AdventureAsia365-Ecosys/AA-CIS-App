"""AA-1049 W3 (ADR 0020 "Plan", D2) — pre-spend angle tripwires in REPORT mode. Invented places and brands only."""
from __future__ import annotations

from services.acp_content_writing import prespend, service

SOURCE = ("Dunmere harbour: a stone sea wall runs past the net sheds to an old signal station. "
          "A ferry crosses to Brecca village twice a week; lunch at the quay cafe is included on day 2.")


def test_a_brief_promise_the_source_lacks_is_reported():
    angle = {"name": "Dawn on the Dunmere wall",
             "why_it_works": "Families love it: a private guide and hotel transfers make the walk easy. "
                             "The sea wall is 12 km long.",
             "formula_fit": "", "best_final_style": "warm"}
    rep = prespend.angle_tripwires(angle, SOURCE)
    assert rep["mode"] == "report" and rep["sentences"] == 4
    words = {w for f in rep["findings"] for w in f["promise_words"]}
    assert {"guide", "hotel", "transfers"} <= words
    assert any(f["numbers"] == ["12"] for f in rep["findings"])
    assert all("trip" in f["wires"] or f["numbers"] for f in rep["findings"])


def test_a_brief_that_only_names_what_the_source_says_is_clean():
    angle = {"name": "The Brecca ferry", "why_it_works": "Lunch is included at the quay cafe on day 2.",
             "formula_fit": "Slow travel by sea wall and ferry."}
    assert prespend.angle_tripwires(angle, SOURCE)["findings"] == []


def test_no_angle_or_no_source():
    assert prespend.angle_tripwires(None, SOURCE) == {"mode": "report", "sentences": 0, "findings": []}
    rep = prespend.angle_tripwires({"name": "Guided hotel stay"}, None)
    assert rep["findings"] and rep["findings"][0]["promise_words"] == ["guided", "hotel"]


def test_writer_input_carries_the_report_admin_only():
    ctx = {"channel": "linkedin", "prespend": {"mode": "report", "sentences": 1, "findings": []}}
    assert service.writer_input_snapshot(ctx)["prespend"]["mode"] == "report"
    assert "prespend" not in service.writer_input_snapshot({"channel": "linkedin"})
