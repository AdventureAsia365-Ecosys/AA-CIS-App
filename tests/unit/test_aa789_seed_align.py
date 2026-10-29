"""AA-789 tier 1 — seed alignment (services/acp_content_writing/seed_align), root plan
docs/audits/2026-10-29-S257-aa789-design/PLAN.md.

What is pinned here:
- the mode switch: off by default, per-class env over the global one, `hold` reads as `repair` until a class is
  promoted (HOLD_PROMOTED is empty: nothing may hold before the S246 freeze ends and G3 is met);
- invariants: I1 (the candidate layer imports no gate builder), I2 (every class off → the outcome of
  run_quality_gates is what it was, and spaCy is never loaded; shadow never changes the ledger; an error or a
  missing model fails open), the never_holds clause for `sa:` findings;
- per class: synthetic positives in the shape of the panel examples (the known misses of test_aa789_t10_recall
  are caught here) and synthetic negatives, plus every round-3 verifier clean sentence with its own seed;
- error injection for the hold-capable classes: take away the seed relation the finding rests on and the check
  must abstain.

All copy is synthetic or from the verifier rounds already in the repo. The real eval set (405 items) is outside the
repo; scripts/aa789_seed_align_eval.py scores it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services.acp_content_writing import quality_gates as qg
from services.acp_content_writing import service as svc
from services.acp_content_writing import trip_facts as tf
from services.acp_content_writing.hygiene import HygieneContext
from services.acp_content_writing.seed_align import checks as sa
from services.acp_content_writing.seed_align import modes, nlp, seed_graph
from services.quality import registry as qr
from tests.unit.test_aa768_s234_round3 import PUNAKHA_SEED, ROUTE_SEED, TERRA_AUDIENCE, TERRA_CTA
from tests.unit.test_aa789_t10_recall import ROUND3_CLEAN

spacy_ready = pytest.mark.skipif(not nlp.available(), reason="spaCy / en_core_web_sm not installed")

_PKG = Path(sa.__file__).resolve().parent

BOAT_SEED = """Khövsgöl Lake — take boat rides on the water
Day 5 of the trip
Source itinerary: Optional activities include boat rides on the water, horseback riding across surrounding terrain, \
visits to local nomadic families
Status: OFFERED, NOT INCLUDED (the itinerary says: "Optional activities include boat rides on the water."). \
Describe it as available, never as something the reader will do.

Khövsgöl Lake — trek along shorelines and forest paths
Day 5 of the trip
Source itinerary: trekking along shorelines and forest paths"""

WORLD_SEED = """Lake Khövsgöl — visit the lakeshore
Day 4 of the trip
Source itinerary: Arrive at the lakeshore camp.
Place background (checked facts about the place itself — you may use these; they say nothing about what this trip does):
- It holds about 0.4 percent of the world's fresh surface water."""


def _cands(text: str, seed: str = ROUTE_SEED, briefs=()) -> list:
    g = seed_graph.build(seed, list(briefs))
    found = sa.find_candidates(text, g, names=set(g.place_names))
    assert found is not None
    return found


def _classes(text: str, seed: str = ROUTE_SEED, briefs=()) -> set[str]:
    return {c.cls for c in _cands(text, seed, briefs)}


@pytest.fixture
def no_sa_env(monkeypatch):
    for c in sa.ALL_CLASSES:
        monkeypatch.delenv(f"{modes.MODE_ENV}_{c.upper()}", raising=False)
    monkeypatch.delenv(modes.MODE_ENV, raising=False)
    return monkeypatch


# ---------------------------------------------------------------- modes

def test_every_class_is_off_by_default(no_sa_env):
    assert modes.active_modes() == {}
    assert all(modes.class_mode(c) == modes.OFF for c in sa.ALL_CLASSES)


def test_global_env_and_per_class_override(no_sa_env):
    no_sa_env.setenv(modes.MODE_ENV, "shadow")
    no_sa_env.setenv(f"{modes.MODE_ENV}_NEAR_AS_AT", "repair")
    no_sa_env.setenv(f"{modes.MODE_ENV}_ORPHAN_OPENER", "off")
    m = modes.active_modes()
    assert m[sa.NEAR_AS_AT] == modes.REPAIR and m[sa.WORLD_SHARE] == modes.SHADOW
    assert sa.ORPHAN_OPENER not in m
    # classes without a check yet are never active
    assert sa.PARAPHRASED_REPEAT not in m and sa.FAQ_NON_ANSWER not in m


@pytest.mark.parametrize("value", ["", "on", "true", "1", "warn", "HOLDING"])
def test_unknown_values_read_as_off(no_sa_env, value):
    no_sa_env.setenv(modes.MODE_ENV, value)
    assert modes.active_modes() == {}


@pytest.mark.parametrize("cls", sa.BUILT_CLASSES)
def test_hold_reads_as_repair_until_a_promotion_pr(no_sa_env, cls):
    no_sa_env.setenv(modes.MODE_ENV, " HOLD ")
    assert modes.HOLD_PROMOTED == frozenset()  # nothing may hold before the S246 freeze ends (~18/11) and G3 is met
    assert modes.class_mode(cls) == modes.REPAIR


def test_only_the_four_fact_classes_may_ever_hold():
    assert modes.HOLD_CAPABLE == {sa.OPTIONAL_AS_PLANNED, sa.PLANNED_AS_OPTIONAL, sa.NEAR_AS_AT, sa.WORLD_SHARE}


def test_findings_sit_under_existing_gates_only():
    assert set(modes.CLASS_GATE.values()) <= {qr.F17_TRIP_FACT, qr.F18_RARITY, qr.F13_LEAK, qr.F13_REPEAT}
    assert set(sa.BUILT_CLASSES) <= set(modes.CLASS_GATE)


# ---------------------------------------------------------------- I1: the candidate layer builds no gate result

@pytest.mark.parametrize("module", ["checks.py", "seed_graph.py", "nlp.py"])
def test_candidate_layer_imports_no_gate_builder(module):
    tree = ast.parse((_PKG / module).read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m and ("quality_gates" in m or m.startswith("services.quality")) for m in imported), imported


# ---------------------------------------------------------------- I2: off / shadow / error never move the ledger

def _gates(text: str, seed: str = ROUTE_SEED, channel: str = "blog"):
    hctx = HygieneContext(seed=seed, brand_name="Pretend Brand", cta=TERRA_CTA, audience=TERRA_AUDIENCE)
    tctx = tf.TripFactContext(seed=seed, audience=TERRA_AUDIENCE, cta=TERRA_CTA, channel=channel)
    return qg.run_quality_gates(content_text=text, atom_text=seed, cta=TERRA_CTA, brand_rubric_text="",
                                channel=channel, hygiene_ctx=hctx, trip_ctx=tctx, f9_results=[])


PIECE = ("## Day four\n\nLunch is at Mörön, the provincial capital. Families cross the lake by boat, and walk the "
         "shore, and optionally ride horses. The Tsaatan visit is optional.\n\n" + TERRA_CTA)


def test_off_leaves_the_outcome_unchanged_and_never_loads_spacy(no_sa_env, monkeypatch):
    calls = []
    monkeypatch.setattr(nlp, "parse_many", lambda texts: calls.append(texts))
    out = _gates(PIECE)
    assert "seed_align" not in out and calls == []
    assert not any(str(v).startswith(modes.FINDING_PREFIX) for g in out["gate_ledger"]
                   for v in g.get("violations") or [])


@spacy_ready
def test_shadow_records_but_never_changes_the_ledger(no_sa_env):
    off = _gates(PIECE)
    no_sa_env.setenv(modes.MODE_ENV, "shadow")
    shadow = _gates(PIECE)
    assert shadow["gate_ledger"] == off["gate_ledger"] and shadow["flags"] == off["flags"]
    assert shadow["passed"] == off["passed"]
    rec = shadow["seed_align"]
    assert rec["status"] == "ok" and rec["nlp"]
    got = {c["cls"] for c in rec["candidates"]}
    assert {sa.NEAR_AS_AT, sa.OPTIONAL_AS_PLANNED, sa.PLANNED_AS_OPTIONAL} <= got
    assert all(c["mode"] == modes.SHADOW for c in rec["candidates"])


def test_a_missing_model_fails_open(no_sa_env, monkeypatch):
    off = _gates(PIECE)
    no_sa_env.setenv(modes.MODE_ENV, "repair")
    monkeypatch.setattr(nlp, "parse_many", lambda texts: None)
    out = _gates(PIECE)
    assert out["gate_ledger"] == off["gate_ledger"]
    assert out["seed_align"]["status"] == "nlp_unavailable"


def test_an_error_fails_open(no_sa_env, monkeypatch):
    off = _gates(PIECE)
    no_sa_env.setenv(modes.MODE_ENV, "repair")

    def boom(*a, **k):
        raise RuntimeError("parser exploded")
    monkeypatch.setattr(sa, "find_candidates", boom)
    out = _gates(PIECE)
    assert out["gate_ledger"] == off["gate_ledger"]
    assert out["seed_align"]["status"] == "error"


# ---------------------------------------------------------------- repair routing and the hold rule

@spacy_ready
def test_repair_mode_blocks_under_the_existing_gate_and_never_holds(no_sa_env):
    no_sa_env.setenv(f"{modes.MODE_ENV}_NEAR_AS_AT", "hold")  # reads as repair: not promoted
    out = _gates(PIECE)
    f17 = next(g for g in qg.expand_ledger(out["gate_ledger"]) if g["gate"] == qr.F17_TRIP_FACT)
    sa_vs = [v for v in f17["violations"] if v.startswith("sa:near_as_at: ")]
    assert sa_vs and not f17["passed"] and f17["blocking"] and f17["repairable"]
    assert sa_vs[0].endswith("'Lunch is at Mörön, the provincial capital.'")  # sentence_repair locates the quote
    only_sa = {**f17, "violations": sa_vs}
    assert qg.never_holds(only_sa)
    assert qg.hold_failure([only_sa]) is None


def test_a_mixed_result_still_holds_on_its_deterministic_finding():
    g = {"gate": qr.F17_TRIP_FACT, "passed": False, "blocking": True, "repairable": True,
         "violations": ["sa:near_as_at: x: 'A.'", "gives a time the source does not: 'B.'"]}
    assert not qg.never_holds(g)


def test_an_sa_finding_never_turns_a_warn_only_result_into_a_hold(no_sa_env):
    """Review blocker (S258): gate_trip_facts writes F17 / F18 with blocking=False when only 'title …' / 'meta …'
    findings are left (service._fit_seo_meta already refused that meta). Appending an sa: finding makes the result
    blocking, so it gets the sentence repair, but neither the meta warning nor the sa: finding may hold the piece."""
    no_sa_env.setenv(f"{modes.MODE_ENV}_NEAR_AS_AT", "repair")
    warn = {"gate": qr.F17_TRIP_FACT, "passed": False, "blocking": False, "repairable": True,
            "violations": ["meta gives a time the source does not: 'Sunrise at 5 am.'"]}
    assert qg.hold_failure(qg.merge_ledger([warn])) is None
    run = modes.SeedAlignRun(live={qr.F17_TRIP_FACT: ["sa:near_as_at: r: 'Lunch is at Mörön.'"]})
    (out,) = modes.apply([warn], run)
    assert out["blocking"] and not out["passed"] and out["repairable"] and out[modes.BASE_BLOCKING_KEY] is False
    assert out["violations"][-1].startswith("sa:near_as_at: ")
    assert qg.never_holds(out)
    ledger = qg.merge_ledger([out])
    assert qg.hold_failure(ledger) is None
    # the repair still runs on it: it is the first blocking failure
    assert next(g for g in qg.expand_ledger(ledger) if not g["passed"] and g["blocking"])["gate"] == qr.F17_TRIP_FACT
    # what is left after a failed repair ships as a flag, never held
    status, reason, flags = svc._hold_or_ship(ledger, [])
    assert (status, reason) == ("approved", None) and any(f["gate"] == qr.F17_TRIP_FACT for f in flags)


def test_an_sa_finding_keeps_a_blocking_result_holding_on_its_own_finding(no_sa_env):
    no_sa_env.setenv(f"{modes.MODE_ENV}_NEAR_AS_AT", "repair")
    body = {"gate": qr.F17_TRIP_FACT, "passed": False, "blocking": True, "repairable": True,
            "violations": ["gives a time the source does not: 'We leave at 5 am.'"]}
    run = modes.SeedAlignRun(live={qr.F17_TRIP_FACT: ["sa:near_as_at: r: 'Lunch is at Mörön.'"]})
    (out,) = modes.apply([body], run)
    assert out[modes.BASE_BLOCKING_KEY] is True
    assert not qg.never_holds(out) and qg.hold_failure(qg.merge_ledger([out]))["gate"] == qr.F17_TRIP_FACT


def test_an_sa_finding_on_a_passed_gate_never_holds(no_sa_env):
    no_sa_env.setenv(f"{modes.MODE_ENV}_NEAR_AS_AT", "repair")
    clean = {"gate": qr.F17_TRIP_FACT, "passed": True, "blocking": True, "repairable": True, "violations": []}
    run = modes.SeedAlignRun(live={qr.F17_TRIP_FACT: ["sa:near_as_at: r: 'Lunch is at Mörön.'"]})
    (out,) = modes.apply([clean], run)
    assert out[modes.BASE_BLOCKING_KEY] is False and qg.never_holds(out)
    assert qg.hold_failure(qg.merge_ledger([out])) is None


def test_an_sa_finding_new_on_the_repaired_attempt_is_an_aa528_regression(no_sa_env):
    """A repair must not introduce a new sa: finding unnoticed: one that appears only on the next attempt, on a gate
    that passed before, is a new blocking failure for the AA-528 guard (it reverts when the attempt is not net
    better). Only reachable once a class is in repair."""
    no_sa_env.setenv(f"{modes.MODE_ENV}_NEAR_AS_AT", "repair")
    passed = {"gate": qr.F17_TRIP_FACT, "passed": True, "blocking": True, "repairable": True, "violations": []}
    other = {"gate": qr.F13_LEAK, "passed": False, "blocking": True, "repairable": True,
             "violations": ["writer instruction: 'A sentence on the ruins.'"]}
    prev = qg.merge_ledger([passed, other])
    run = modes.SeedAlignRun(live={qr.F17_TRIP_FACT: ["sa:near_as_at: r: 'Lunch is at Mörön.'"]})
    cur = qg.merge_ledger(modes.apply([passed, other], run))
    assert svc._blocking_failures(cur) - svc._blocking_failures(prev) == {qr.F17_TRIP_FACT}


def test_a_promoted_class_in_hold_mode_may_hold(no_sa_env, monkeypatch):
    v = "sa:near_as_at: the source has the lunch near Moron: 'Lunch is at Mörön.'"
    g = {"gate": qr.F17_TRIP_FACT, "passed": False, "blocking": True, "repairable": True, "violations": [v]}
    assert qg.never_holds(g)
    monkeypatch.setattr(modes, "HOLD_PROMOTED", frozenset({sa.NEAR_AS_AT}))
    no_sa_env.setenv(f"{modes.MODE_ENV}_NEAR_AS_AT", "hold")
    assert not qg.never_holds(g) and qg.hold_failure([g]) is g


def test_apply_skips_a_sentence_a_gate_already_quotes():
    run = modes.SeedAlignRun(live={qr.F17_TRIP_FACT: ["sa:near_as_at: r: 'Lunch is at Mörön.'",
                                                      "sa:optional_as_planned: r: 'We go by boat.'"]})
    src = [{"gate": qr.F17_TRIP_FACT, "passed": False, "blocking": True, "repairable": True,
            "violations": ["puts the lunch at Mörön: 'Lunch is at Mörön.'"]},
           {"gate": qr.F18_RARITY, "passed": True, "blocking": True, "repairable": True, "violations": []}]
    out = modes.apply(src, run)
    assert out[0]["violations"] == ["puts the lunch at Mörön: 'Lunch is at Mörön.'",
                                    "sa:optional_as_planned: r: 'We go by boat.'"]
    assert out[1] is src[1]
    assert modes.finding_class(out[0]["violations"][1]) == sa.OPTIONAL_AS_PLANNED


# ---------------------------------------------------------------- per class: positives (panel shapes)

POSITIVES = [
    # the known misses of test_aa789_t10_recall (main's gates still miss them)
    (sa.OPTIONAL_AS_PLANNED, "Families cross the lake by boat, and walk the shore, and optionally ride horses.",
     ROUTE_SEED),
    (sa.PLANNED_AS_OPTIONAL, "Visiting the Tsaatan herders is optional on day five.", ROUTE_SEED),
    (sa.PLANNED_AS_OPTIONAL, "The Tsaatan visit is optional.", ROUTE_SEED),
    (sa.DIFFICULTY_PROMISE, "The caldera walk is not a technical climb.", ROUTE_SEED),
    (sa.NEAR_AS_AT, "Lunch is at Mörön, the provincial capital.", ROUTE_SEED),
    (sa.NEAR_AS_AT, "A stop in Mörön breaks the drive.", ROUTE_SEED),
    (sa.SCOPELESS_SUPERLATIVE, "This is one of the most coherent family routes available.", ROUTE_SEED),
    (sa.WORLD_SHARE, "The lake holds a significant fraction of the world's fresh water.", WORLD_SEED),
    (sa.BRIEF_RECIPE_LEAK, "## Ruins\n\nOne old wall, one year, one fact.", ROUTE_SEED),
    (sa.NAMING_NOTE_LEAK, "## Mörön\n\nMörön has one phonetic approximation in English that locals dislike.",
     ROUTE_SEED),
    (sa.ORPHAN_OPENER, "## Into the Taiga\n\nAccess requires a multi-hour horse or reindeer ride.", ROUTE_SEED),
    # more synthetic shapes
    (sa.OPTIONAL_AS_PLANNED, "On day five the family heads out by boat in the morning.", BOAT_SEED),
    (sa.OPTIONAL_AS_PLANNED, "Our day on the lake includes a boat ride and an afternoon in the saddle.", BOAT_SEED),
    (sa.PLANNED_AS_OPTIONAL, "The excursion to the Tsaatan settlements can be added for families who want it.",
     ROUTE_SEED),
    (sa.PLANNED_AS_OPTIONAL, "A visit to the courtyards and assembly hall of Punakha Dzong is available on request.",
     PUNAKHA_SEED),
    (sa.NEAR_AS_AT, "Mörön is on the way north. A lunch stop here gives everyone a break.", ROUTE_SEED),
    (sa.NEAR_AS_AT, "The midday stop is Mörön, a provincial capital.", ROUTE_SEED),
    (sa.WORLD_SHARE, "The lake holds about two percent of the world's fresh surface water.", WORLD_SEED),
    (sa.WORLD_SHARE, "The herders keep a large share of the world's domesticated reindeer.", ROUTE_SEED),
    (sa.DIFFICULTY_PROMISE, "The caldera walk is a gentle stroll for every child.", ROUTE_SEED),
    (sa.DIFFICULTY_PROMISE, "No part of the caldera hike is strenuous.", ROUTE_SEED),
    (sa.SCOPELESS_SUPERLATIVE, "Uran Togoo is the only volcano in the world you can walk into.", ROUTE_SEED),
    (sa.SCOPELESS_SUPERLATIVE, "A way of life that most of the world has never seen survives here.", ROUTE_SEED),
    (sa.BRIEF_RECIPE_LEAK, "## A Single Sentence on the Ruins", ROUTE_SEED),
    (sa.BRIEF_RECIPE_LEAK, "The ruins get one sentence here: they were destroyed in 1937.", ROUTE_SEED),
    (sa.ORPHAN_OPENER, "## Lake Khövsgöl\n\nThey mean that this one lake holds most of the country's water.",
     ROUTE_SEED),
    (sa.ORPHAN_OPENER, "## Sod Camp\n\nThis is why the second night matters.", ROUTE_SEED),
]


@spacy_ready
@pytest.mark.parametrize("cls,text,seed", POSITIVES)
def test_panel_shapes_are_found(cls, text, seed):
    assert cls in _classes(text, seed), text


@spacy_ready
def test_audience_range_and_brief_quote_as_copy():
    briefs = ["Audience: affluent families with children aged 7-16.",
              "Angle brief: the confluence of the 'two named rivers' below the dzong."]
    assert sa.BRIEF_RECIPE_LEAK in _classes("Written for parents of children aged 7 to 16, this guide stays calm.",
                                            PUNAKHA_SEED, briefs)
    assert sa.BRIEF_RECIPE_LEAK in _classes("The two named rivers meet below the dzong walls.", PUNAKHA_SEED, briefs)


# ---------------------------------------------------------------- per class: negatives

NEGATIVES = [
    # hedged, or the activity is not the traveller's
    ("Boat rides on the lake are an optional extra on day five, weather permitting.", BOAT_SEED),
    ("If you would like to see the lake from the water, a boat ride can be added on day five.", BOAT_SEED),
    ("Supplies for the camp come in by boat once a week.", BOAT_SEED),
    ("Families walk the shoreline, and anyone who wants to can take a boat out.", BOAT_SEED),
    ("Optional activities include boat rides on the water and horseback riding.", BOAT_SEED),
    # a planned moment named as planned; only a sub-activity optional; a person opts out of a stop that happens
    ("The Tsaatan day is part of the plan, not an optional extra.", ROUTE_SEED),
    ("At the Tsaatan settlements, riding a reindeer is optional.", ROUTE_SEED),
    ("Anyone who prefers not to descend can wait at the rim of Uran Togoo.", ROUTE_SEED),
    ("Visiting the mineral spring near Sod Camp is optional.", ROUTE_SEED),
    # near stays near; a time phrase; a bus, not the group
    ("Lunch on day four is near Mörön, before the drive continues to the lake.", ROUTE_SEED),
    ("After lunch, the drive continues to the airport in Mörön.", ROUTE_SEED),
    ("Long-distance buses make a stop in Mörön on the way to Khatgal.", ROUTE_SEED),
    ("The group stops for lunch on the outskirts of Mörön.", ROUTE_SEED),
    # a caveat is not a promise; equipment is not the walk
    ("The hike is strenuous in places, so good boots matter.", ROUTE_SEED),
    ("No technical climbing gear is needed on the caldera walk.", ROUTE_SEED),
    # the seed's own figure; a share of the trip; a share of the country
    ("The lake holds about 0.4 percent of the world's fresh surface water.", WORLD_SEED),
    ("It holds approximately zero point four percent of the world's fresh surface water.", WORLD_SEED),
    ("A significant share of the trip is spent outdoors.", WORLD_SEED),
    ("Travellers from most of the world can get a visa on arrival.", WORLD_SEED),
    # a superlative the seed makes; a practical one; an idiom; a ranking inside the trip
    ("The Tsaatan are one of the most distinct ethnic groups you can meet.", ROUTE_SEED),
    ("Their reindeer herding is one of the last traditions of its kind in the world.", ROUTE_SEED),
    ("The best time to visit the lake is July.", ROUTE_SEED),
    ("The reindeer are, in the fullest sense, companions of the herders.", ROUTE_SEED),
    ("The route's most remote stop is the Tsaatan camp.", ROUTE_SEED),
    # surface forms of the recipe used naturally
    ("## Camp\n\nEach family gets one tent, one stove and one lantern.", ROUTE_SEED),
    ("One morning on the lake, one afternoon in the forest: day five splits in two.", ROUTE_SEED),
    ("There is one thing worth knowing before the hike: cameras stay at the checkpoint.", ROUTE_SEED),
    # naming advice, variants, pronunciation
    ("Mörön, also written Murun or Muren, is the capital of Khövsgöl Province.", ROUTE_SEED),
    ("## Practical notes\n\nCheck that Mörön is correctly spelled on your domestic flight ticket.", ROUTE_SEED),
    ("## FAQ\n\n**How do you say Mörön?**\n\nRoughly 'MUR-urn'; Muren is only a phonetic approximation.", ROUTE_SEED),
    # a section that names its subject; a definite bridging noun phrase
    ("## Into the Taiga\n\nThe Tsaatan camps lie deep in the taiga; access requires a long ride.", ROUTE_SEED),
    ("## Uran Togoo\n\nThe approach road is paved for most of the way.", ROUTE_SEED),
    ("## Practical notes\n\nEntry fees are included in the price.", ROUTE_SEED),
]


@spacy_ready
@pytest.mark.parametrize("text,seed", NEGATIVES)
def test_near_misses_stay_clean(text, seed):
    assert _cands(text, seed) == [], text


# every round-3 verifier clean sentence with its own seed, minus the one the eval set relabelled as a defect
# ('a gentle gradual ascent': 'gentle' is not in the seed, so it is a difficulty promise)
_RELABELLED = ("The trail is a gentle gradual ascent",)


@spacy_ready
@pytest.mark.parametrize("channel,seed,text", [r for r in ROUND3_CLEAN if not r[2].startswith(_RELABELLED)])
def test_round3_clean_sentences_get_no_candidate(channel, seed, text):
    assert _cands(text, seed) == [], text


# ---------------------------------------------------------------- error injection (hold-capable classes)

@spacy_ready
def test_without_the_near_relation_near_as_at_abstains():
    at_seed = ROUTE_SEED.replace("Stop for lunch near Moron", "Stop for lunch at Moron")
    assert sa.NEAR_AS_AT not in _classes("Lunch is at Mörön, the provincial capital.", at_seed)


@spacy_ready
def test_a_seed_that_also_puts_an_event_at_the_place_makes_near_as_at_abstain():
    seed = "Yen Duc village (day 3): Stop for lunch near Yen Duc village; water puppet show at the village."
    assert sa.NEAR_AS_AT not in _classes("Lunch on day three is in Yen Duc village.", seed)


@spacy_ready
def test_an_unresolved_place_makes_near_as_at_abstain():
    assert sa.NEAR_AS_AT not in _classes("Lunch is at Khatgal, the lakeside town.", ROUTE_SEED)


@spacy_ready
def test_a_planned_boat_is_not_an_optional_one():
    planned = BOAT_SEED.replace("Optional activities include boat rides on the water, ", "Boat rides on the water; ")
    planned = "\n".join(ln for ln in planned.splitlines() if not ln.startswith("Status:"))
    assert sa.OPTIONAL_AS_PLANNED not in _classes("On day five the family heads out by boat in the morning.", planned)


@spacy_ready
def test_a_moment_the_seed_offers_may_be_called_optional():
    offered = ROUTE_SEED.replace("Source itinerary: excursions to Tsaatan reindeer herders' settlements",
                                 "Source itinerary: optional excursions to Tsaatan reindeer herders' settlements")
    assert sa.PLANNED_AS_OPTIONAL not in _classes("The Tsaatan visit is optional.", offered)


@spacy_ready
def test_the_seed_figure_clears_a_world_share():
    seed = WORLD_SEED.replace("0.4 percent", "2 percent")
    assert sa.WORLD_SHARE not in _classes("The lake holds about two percent of the world's fresh surface water.",
                                          seed)


@spacy_ready
def test_a_long_sentence_abstains_on_fact_classes():
    long = ("Lunch is at Mörön, the provincial capital, " + "and the road keeps going north past the hills "
            * 5 + "before the lake.")
    assert not _classes(long) & {sa.NEAR_AS_AT, sa.OPTIONAL_AS_PLANNED, sa.PLANNED_AS_OPTIONAL, sa.WORLD_SHARE}


# ---------------------------------------------------------------- the seed graph

def test_seed_graph_reads_the_t9_seed_blocks():
    g = seed_graph.build(ROUTE_SEED)
    assert [i.anchor for i in g.optional_items] == ["boat", "horseback", "nomadic"]
    assert [(r.place, set(r.events) >= {"stop"}) for r in g.near] == [("Moron", True)]
    assert {"distinct", "last"} <= g.superlatives
    tsaatan = next(m for m in g.moments if m.name.startswith("Tsaatan"))
    assert tsaatan.names == {"tsaatan"} and "excursion" in tsaatan.heads
    lake = next(m for m in g.moments if m.name.startswith("Lake"))
    assert "boat" not in lake.heads  # the offered label 'take boat rides' plans nothing


@pytest.mark.parametrize("text,value", [("0.4", 0.4), ("seventy", 70.0), ("twenty-five", 25.0),
                                        ("zero point four", 0.4), ("half", 50.0), ("many", None)])
def test_number_values(text, value):
    assert seed_graph.number_value(text) == value
