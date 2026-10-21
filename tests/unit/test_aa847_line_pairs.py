"""AA-847 (owner decision S246, D2: duplicates merged automatically) — the deterministic LINE duplicate rule
(services/places/shape.same_line_place) measured on labelled pairs (tests/unit/fixtures/aa847_line_pairs.json).

A false merge (two different rivers / roads / trails made one) is the costly error: it must stay 0. A missed merge
leaves two drawn lines for one feature until the Problem 2 engine's model check (or a later OSM id) joins them; the
count is pinned so a rule change that loses recall shows up here. Lesson 125 / AA-779: measure false links before
shipping a rule, and say the set is author-labelled (a regression floor, not a field accuracy)."""
from __future__ import annotations

import json
import os

import pytest

from services.places import shape as S

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "aa847_line_pairs.json")
with open(FIX, encoding="utf-8") as _fh:
    PAIRS = json.load(_fh)["pairs"]


def _place(d: dict) -> S.LinePlace:
    d = dict(d)
    d["osm_names"] = tuple(d.get("osm_names") or ())
    return S.LinePlace(**d)


def _confusion():
    out = {"tp": [], "fp": [], "fn": [], "tn": []}
    for p in PAIRS:
        same, why = S.same_line_place(_place(p["a"]), _place(p["b"]))
        key = ("tp" if same else "fn") if p["label"] == "same" else ("fp" if same else "tn")
        out[key].append((p["why"], why))
    return out


def test_no_false_merge():
    c = _confusion()
    assert c["fp"] == []


def test_recall_is_pinned():
    c = _confusion()
    assert (len(c["tp"]), len(c["fn"]), len(c["tn"])) == (9, 4, 11), {k: v for k, v in c.items() if k == "fn"}


@pytest.mark.parametrize("p", PAIRS, ids=[p["why"][:50] for p in PAIRS])
def test_rule_is_symmetric(p):
    assert S.same_line_place(_place(p["a"]), _place(p["b"]))[0] == S.same_line_place(_place(p["b"]),
                                                                                      _place(p["a"]))[0]


def test_other_country_is_never_merged_even_on_the_same_relation():
    p = next(p for p in PAIRS if p["a"]["destination_id"] == "c1")
    assert S.same_line_place(_place(p["a"]), _place(p["b"])) == (False, "country")
