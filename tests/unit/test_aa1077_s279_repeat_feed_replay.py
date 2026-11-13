"""AA-1077 (S279, lane f13-drop) — the $0 replay of the repeat drop fed by both F13 gates
(scripts/aa1077/repeat_feed_replay.py): pure parts with a fake gate run, and one stored-shape row through the real
PieceReplay (no DB, no model: the F9 verdicts are the stored ones). Invented words and places only."""
from __future__ import annotations

import json

from scripts.aa1077 import repeat_feed_replay as rp
from services.acp_content_writing import repeat_drop
from services.quality import registry as qr
from tests.unit import aa791_t9_fault_set as fs
from tests.unit.test_aa1077_s279_repeat_drop import _hyg, _with_twin
from tests.unit.test_aa1077_s279_repeat_feed import VALLEY, _place_phrase_blog


def _gates(text: str) -> list:
    """A fake replayed ledger: F13_repeat fails while the text holds a repeat, F4 always passes."""
    from services.acp_content_writing import hygiene
    rep = hygiene.find_issues(text, _hyg())["repeat"]
    return [{"gate": qr.F4_EXTREME_LENGTH, "passed": True, "blocking": True, "violations": []},
            {"gate": qr.F13_REPEAT, "passed": not rep, "blocking": True, "violations": rep}]


def test_a_near_exact_repeat_is_dropped_and_only_f13_changes():
    text, _first = _with_twin("{first} tovelin.")
    r = rp.piece_result("aaaaaaaa", text, _hyg(), _gates)
    assert r["drops"] == 1 and r["old_changed"] and not r["feed_changed"]
    assert r["counts_before"][qr.F13_REPEAT] == 1 and r["counts_after"][qr.F13_REPEAT] == 0
    assert r["f13_changes"] == [qr.F13_REPEAT] and r["other_changes"] == []
    assert r["blocking_before"] == [qr.F13_REPEAT] and r["blocking_after"] == []
    assert [t["source"] for t in r["trace"]] == [qr.F13_REPEAT]


def test_a_place_phrase_warning_stays_and_is_counted():
    r = rp.piece_result("bbbbbbbb", _place_phrase_blog(), _hyg(), _gates)
    assert r["drops"] == 0 and not r["feed_changed"]
    assert r["counts_before"][qr.F13_HYGIENE] == r["counts_after"][qr.F13_HYGIENE] == 1
    assert r["trace"][0]["outcome"] == repeat_drop.NOT_NEAR_EXACT and VALLEY not in json.dumps(r)


def test_summary_counts_and_flags_other_verdict_changes():
    text, _first = _with_twin("{first} tovelin.")
    a = rp.piece_result("aaaaaaaa", text, _hyg(), _gates)
    b = rp.piece_result("bbbbbbbb", _place_phrase_blog(), _hyg(), _gates)
    gone = dict(b, piece="cccccccc", counts_after={qr.F13_REPEAT: 0, qr.F13_HYGIENE: 0}, feed_changed=True)
    bad = dict(a, piece="dddddddd", other_changes=[qr.F17_TRIP_FACT])
    s = rp.summarize([a, b, gone, bad], ["eeeeeeee KeyError: x"])
    assert s["pieces"] == 4 and s["errors"] == 1
    assert s["pieces_with_hygiene_repeat"] == 2 and s["hygiene_repeat_warnings"] == 2
    assert s["pieces_hygiene_repeats_gone"] == ["cccccccc"] and s["pieces_feed_changed"] == ["cccccccc"]
    assert s["outcomes"][qr.F13_REPEAT] == {repeat_drop.DROPPED: 2}
    assert s["outcomes"][qr.F13_HYGIENE] == {repeat_drop.NOT_NEAR_EXACT: 2}
    assert sum(s["refused_dice_bands"][qr.F13_HYGIENE].values()) == 2
    assert s["other_verdict_changes"] == {"dddddddd": [qr.F17_TRIP_FACT]}
    assert s["blocking_cleared"] == ["aaaaaaaa", "dddddddd"]
    json.dumps(s)   # the SUMMARY line is JSON


def test_dice_bands():
    assert [rp.dice_band(d) for d in (None, 0.2, 0.5, 0.65, 0.89, 0.9, 1.0)] == \
        ["n/a", "<0.5", "0.5-0.6", "0.6-0.7", "0.8-0.9", ">=0.9", ">=0.9"]


def test_replay_row_rebuilds_the_writer_gates_and_changes_no_other_verdict():
    text, first = _with_twin("{first} tovelin.")
    wi = {"seed": fs.SEED, "route_moments": [], "facts": "", "scope": fs.SCOPE, "brand_pack": dict(fs.PACK),
          "brand_rubric": "rubric", "angle": {"name": "First night"}, "goal": "awareness", "channel": "blog",
          "keyword": None, "cta": fs.CTA, "trip": fs.TRIP, "destination": "Mongolia"}
    row = {"piece_id": "ffffffff-0000", "tenant": "audit-x", "status": "held", "channel": "blog",
           "content_text": text, "gate_ledger": json.dumps([]), "writer_input": wi, "seo_title": None,
           "meta_description": None, "slug": None}
    r = rp.replay_row(row)
    assert r["piece"] == "ffffffff"
    assert r["other_changes"] == []
    # the writer's own CTA differs from the invented blog's closing line, so other findings may remain: the twin goes
    assert r["drops"] == 1 and r["counts_after"][qr.F13_REPEAT] == r["counts_before"][qr.F13_REPEAT] - 1
    assert first[:-1] + " tovelin." not in json.dumps(r)
    line = rp._line(r, "audit-x", "held")
    assert line.startswith("PIECE ffffffff audit-x status=held") and first not in line
