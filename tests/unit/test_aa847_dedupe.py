"""AA-847 Problem 2 — the automatic duplicate-place decision (services/places/dedupe.py), gated on a labelled pair set.

The pair set (fixtures/aa847_dedupe_pairs.json) holds the 22 S246 validity pairs, the brief's cases, 79 pairs built
from real places of AA countries, the S246 skeptic review's adversarial / linear pairs, the revised plan's adversarial
keeps (same admin-area id for a ward and a landmark, temple vs stupa, a tributary touching its river) and the homonym
regression list (lesson 125). It is a regression set: a rule change that auto-merges a KEEP pair fails here in every
metadata mode. Change the rules only from labelled failures (a new pair here), never by patching one case (lesson
S245 / AA-779). Every undo of a merge on Dev is a new labelled keep pair for this file.

Rules recall floor: the revised plan (§6) asked for ≥ 0.75; its own change §0.1 (a shared geocoder id no longer
auto-merges a pair whose names are not exact / fuzzy) moves Van Mieu, Wat Arun and Tiger's Nest to the model band, so
the measured rules recall on this set is 0.689 (42/61) with full metadata and 0.967 if the two-model check answers the
band right. The floor below pins the measured value, it is not a target.

Degraded modes: the same pairs with less metadata than the live rows will have — no ids / no overnight stops /
no cards; every point the model's; every Geoapify row on one shared CITY-level id (the geocode-fallback defect);
no shapes. A pair whose label needs the stored metadata ({"label_needs": "metadata"}) is gated in the full run only.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from services.places import dedupe as dd
from services.places import geomatch

FIXTURE = Path(__file__).parent / "fixtures" / "aa847_dedupe_pairs.json"
RULES_RECALL_FLOOR = 0.68

DEGRADE = {
    None: lambda p: p,
    "no_ids_no_stops": lambda p: {k: v for k, v in p.items() if k not in ("pid", "stop", "card")},
    "all_model_points": lambda p: {**{k: v for k, v in p.items() if k not in ("pid", "stop", "card", "rt", "gname")},
                                   "by": "llm", "prec": "line" if p.get("prec") == "line" else "area"},
    "shared_city_id": lambda p: ({**p, "rt": "city", "pid": "g-one-city", "gname": "Some City", "stop": False}
                                if p.get("by") == "geoapify" else p),
    "no_shapes": lambda p: {k: v for k, v in p.items() if k != "shape"},
}


def _shape(s):
    if not s:
        return None
    line = tuple(tuple(x) for x in s["line"])
    osm = s.get("osm") or [None, None]
    return dd.Shape(s["status"], osm[0], osm[1], (line,), dd._bbox((line,)), line_class=s.get("class"))


def _row(pid: str, p: dict) -> dd.Row:
    by = p.get("by")
    return dd.Row(pid, p["name"], p["country"], p["lat"], p["lng"], located_by=by, precision=p.get("prec"),
                  result_type=p.get("rt"), geocoder_name=p.get("gname", p["name"] if by == "geoapify" else None),
                  place_id=p.get("pid"), stop=bool(p.get("stop")), card=p.get("card"), shape=_shape(p.get("shape")))


def load(degrade=None):
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fix = DEGRADE[degrade]
    rows = {pid: _row(pid, fix(dict(p))) for pid, p in data["places"].items()}
    pairs = [tuple(x[:5]) + ((x[5] if len(x) > 5 else {}),) for x in data["pairs"]]
    if degrade is not None:
        pairs = [x for x in pairs if x[5].get("label_needs") != "metadata"]
    return rows, pairs


def evaluate(degrade=None) -> dict:
    rows, pairs = load(degrade)
    locs = dd.localities_of(rows.values())
    out = {"pairs": len(pairs), "merge": 0, "keep": 0, "matrix": {}, "false_merges": [], "rows": []}
    for a_id, b_id, label, src, _note, _ in pairs:
        a, b = rows[a_id], rows[b_id]
        dec = dd.decide(a, b, dd.Ctx(localities=locs.get(a.country.lower(), frozenset())))
        out[label] += 1
        key = f"{label}->{dec.decision}"
        out["matrix"][key] = out["matrix"].get(key, 0) + 1
        if label == "keep" and dec.decision == dd.AUTO:
            out["false_merges"].append((a.name, b.name, dec.reason))
        out["rows"].append((label, dec.decision, dec.score, dec.km, a.name, b.name, a.country, dec.reason, src))
    tp = out["matrix"].get("merge->auto_merge", 0)
    band_m, band_k = out["matrix"].get("merge->llm", 0), out["matrix"].get("keep->llm", 0)
    out["rules_recall"] = round(tp / out["merge"], 3)
    out["rules_precision"] = round(tp / (tp + len(out["false_merges"])), 3) if tp + len(out["false_merges"]) else None
    out["recall_if_check_right"] = round((tp + band_m) / out["merge"], 3)
    out["llm_band"] = {"merge": band_m, "keep": band_k}
    return out


# ── the gate ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("degrade", list(DEGRADE))
def test_no_false_merge_in_any_metadata_mode(degrade):
    ev = evaluate(degrade)
    assert ev["false_merges"] == [], ev["false_merges"]


def test_rules_recall_floor_with_full_metadata():
    ev = evaluate()
    assert ev["rules_recall"] >= RULES_RECALL_FLOOR, ev["matrix"]
    assert ev["recall_if_check_right"] >= 0.95, ev["matrix"]


def test_decide_is_symmetric_on_every_pair():
    rows, pairs = load()
    for a_id, b_id, *_ in pairs:
        assert dd.decide(rows[a_id], rows[b_id]) == dd.decide(rows[b_id], rows[a_id])


# ── adversarial pairs (named, so a regression says which) ──────────────────────────────────────────────────────────

ADVERSARIAL = [
    ("adv-bt", "adv-hcmc"), ("adv-bitexco", "adv-bt"), ("adv-hgloop", "adv-hg"), ("adv-tkloop", "adv-tk"),
    ("adv-hcmtrail", "adv-hcmroad"), ("adv-hcmtrail-ns", "adv-hcmroad-ns"), ("adv-haivan", "adv-haivanroad"),
    ("adv-namou-pt", "adv-nongkhiaw-bridge"), ("lin-namkhan", "lin-namou-river"),
    # revised plan §6.1
    ("sk-lb-ward", "sk-lb-bridge"), ("sk-wtl", "sk-ptl"), ("sk-mekong-pakou", "lin-namou-river"),
    ("sk-laocai", "sk-laochai"), ("sk-laocai", "sk-laochai-near"), ("sk-shwezigon", "sk-shwesandaw"),
    ("sk-shwezigon", "sk-shwesandaw-llm"), ("sk-dhammayangyi", "sk-dhammayazika"),
    ("sk-dhammayangyi", "sk-dhammayazika-llm"), ("sk-cuchi", "sk-cuchi-tunnels"), ("sk-cuchi", "sk-cuchi-tunnels-llm"),
    ("sk-mekong-river-vn", "sk-mekong-delta"), ("sk-mekong-river-vn-pt", "sk-mekong-delta"),
]


@pytest.mark.parametrize("a,b", ADVERSARIAL)
def test_adversarial_keeps_never_merge(a, b):
    for degrade in DEGRADE:
        rows, _ = load(degrade)
        assert dd.decide(rows[a], rows[b]).decision != dd.AUTO, (degrade, a, b)


@pytest.mark.parametrize("a,b", [("sk-hk-ward", "sk-hk-lake"), ("sk-lb-ward", "sk-lb-bridge")])
def test_a_shared_id_never_bypasses_the_caps(a, b):
    """§0.1: the same geocoder object for "Hoan Kiem" (ward) and "Hoan Kiem Lake" was an auto-merge in the
    prototype. A shared admin-area id is no evidence; the soft settlement caps the pair at the model."""
    rows, _ = load()
    dec = dd.decide(rows[a], rows[b])
    assert dec.decision == dd.LLM and "cap:soft_settlement" in dec.reason and dec.basis is None


def test_a_shared_poi_id_auto_merges_only_with_a_strong_name_and_no_cap():
    k = R("k", "Kuang Si Falls", 19.7491, 101.9935, "Laos", place_id="g1")
    exact = R("e", "Kuang Si Waterfall", 19.7492, 101.9936, "Laos", place_id="g1")
    other = R("o", "Temple of Dawn", 13.7437, 100.4888, "Thailand", place_id="g2")
    arun = R("a", "Wat Arun", 13.7437, 100.4889, "Thailand", place_id="g2")
    assert dd.decide(k, exact).basis == "provider_id"
    d = dd.decide(other, arun)
    assert d.decision == dd.LLM and "shared_object" in d.evidence
    museum = R("m", "Royal Palace Museum", 19.8917, 102.1336, "Laos", place_id="g3")
    palace = R("p", "Royal Palace", 19.8918, 102.1337, "Laos", place_id="g3")
    d = dd.decide(museum, palace)                         # heritage_museum cap: the shared id does not lift it
    assert d.decision == dd.LLM and "cap:kind:heritage_museum" in d.reason


def test_a_shared_card_is_capped_like_an_id():
    a = R("a", "Ben Thanh Market", 10.7725, 106.6980, card="c1")
    b = R("b", "Ben Thanh", 10.7726, 106.6981, located_by="llm", precision="area", result_type=None, card="c1")
    d = dd.decide(a, b)
    assert d.decision == dd.LLM and "shared_card" in d.evidence
    c = R("c", "Ben Thanh Market", 10.7727, 106.6982, located_by="llm", precision="area", result_type=None,
          card="c1")
    assert dd.decide(a, c).decision == dd.AUTO


def test_point_vs_line_is_at_most_llm():
    rows, _ = load()
    assert dd.decide(rows["adv-hgloop"], rows["adv-hg"]).decision == dd.KEEP
    assert dd.decide(rows["adv-haivan"], rows["adv-haivanroad"]).decision == dd.KEEP
    pt = R("pt", "Nam Ou", 20.6, 102.6, "Laos", located_by="llm", precision="area", result_type=None)
    d = dd.decide(pt, rows["lin-namou-river"])
    assert d.decision == dd.LLM and "cap:geometry_mismatch" in d.reason


def test_linear_pairs_decide_on_lines():
    rows, _ = load()
    d1 = dd.decide(rows["lin-namou"], rows["lin-namou-river"])
    assert d1.decision == dd.AUTO and d1.basis == "line_overlap" and d1.km > 100
    assert d1.reason == "same_line_place/overlap"                  # the lines lane's rule decides
    assert dd.decide(rows["lin-namou-osm"], rows["lin-namou-river"]).basis == "osm_id"
    assert dd.decide(rows["lin-mekong-stretch"], rows["lin-mekong-river"]).decision == dd.AUTO
    assert dd.decide(rows["lin-namkhan"], rows["lin-namou-river"]).decision == dd.KEEP
    touching = dd.decide(rows["sk-mekong-pakou"], rows["lin-namou-river"])
    assert touching.decision == dd.KEEP and not touching.hard      # a soft keep: lines only touch
    assert dd.decide(rows["adv-hcmtrail"], rows["adv-hcmroad"]).reason == "linear_subtype_differs"


def test_a_line_without_a_drawn_shape_never_auto_merges():
    rows, _ = load()
    d = dd.decide(rows["lin-namou-noshape"], rows["lin-namou-river"])
    assert d.decision != dd.AUTO
    d = dd.decide(rows["lin-mekong-approx"], rows["lin-mekong-stretch"])  # an approximate (anchor) shape
    assert d.decision == dd.LLM and "cap:shape_not_drawn" in d.reason


def test_the_linear_point_radius_never_auto_merges():
    a = R("a", "Nam Ou", 20.6, 102.6, "Laos", located_by="llm", precision="area", result_type=None)
    b = R("b", "Nam Ou", 20.9, 102.55, "Laos", located_by="llm", precision="area", result_type=None)
    d = dd.decide(a, b)
    assert d.decision == dd.LLM and "cap:linear_point_radius" in d.reason


def test_shared_city_id_is_a_geocode_fallback_not_evidence():
    rows, _ = load()
    bt, hcmc = rows["adv-bt"], rows["adv-hcmc"]
    assert bt.geocode_fallback and not hcmc.geocode_fallback and hcmc.settlement == "hard"
    assert bt.settlement is None                          # the city result does not make the POI a town
    dec = dd.decide(rows["adv-bitexco"], bt)
    assert dec.decision != dd.AUTO and "provider" not in (dec.basis or "")


def test_embedding_only_promotes_a_soft_keep_to_the_model():
    a = R("a", "Temple of Literature", 21.0293, 105.8355, place_id="g-vm")
    b = R("b", "Van Mieu - Quoc Tu Giam", 21.0280, 105.8360, located_by="llm", precision="area", result_type=None)
    plain = dd.decide(a, b)
    assert plain.decision == dd.KEEP and plain.embed_candidate
    lo, hi = dd.pair_key(a.id, b.id)
    promoted = dd.decide(a, b, dd.Ctx(embedding_cos={(lo, hi): 0.86}))
    assert promoted.decision == dd.LLM and "embedding" in promoted.evidence
    assert dd.decide(a, b, dd.Ctx(embedding_cos={(lo, hi): 0.79})).decision == dd.KEEP
    # never toward auto: an auto pair stays auto, a capped pair stays at the model
    k = R("k", "Kuang Si Falls", 19.7491, 101.9935, "Laos")
    m = R("m", "Tat Kuang Si", 19.7480, 101.9920, "Laos", located_by="llm", precision="area", result_type=None)
    assert dd.decide(k, m, dd.Ctx(embedding_cos={dd.pair_key("k", "m"): 0.1})).decision == dd.AUTO
    far = R("f", "Van Mieu - Quoc Tu Giam", 21.0400, 105.8360, located_by="llm", precision="area", result_type=None)
    assert dd.decide(a, far, dd.Ctx(embedding_cos={dd.pair_key("a", "f"): 0.95})).decision == dd.KEEP


# ── homonym regression list (lesson S245 AA-779): same / similar words, different places ───────────────────────────

def R(i, name, lat, lng, country="Vietnam", **kw):
    kw.setdefault("located_by", "geoapify")
    kw.setdefault("precision", "point")
    kw.setdefault("result_type", "amenity")
    kw.setdefault("geocoder_name", name)
    return dd.Row(i, name, country, lat, lng, **kw)


HOMONYMS = [
    (R("h1", "Ho Chi Minh City", 10.7769, 106.7009, result_type="city", stop=True),
     R("h2", "Ho Chi Minh Mausoleum", 10.78, 106.70)),
    (R("h3", "Ho Chi Minh Mausoleum", 21.0368, 105.8346), R("h4", "Ho Chi Minh Museum", 21.0362, 105.8331)),
    (R("h5", "Night Market", 19.889, 102.136, "Laos", locality_norm="luang prabang"),
     R("h6", "Night Market", 19.890, 102.137, "Laos", locality_norm="vientiane")),
    (R("h7", "Royal Palace", 19.8917, 102.1336, "Laos"), R("h8", "Royal Palace", 11.5637, 104.9310, "Cambodia")),
    (R("h9", "Long Bien", 21.045, 105.86, result_type="suburb"), R("h10", "Long Bien Bridge", 21.043, 105.859)),
    (R("h11", "Phong Nha", 17.59, 106.28, result_type="city", stop=True), R("h12", "Phong Nha Cave", 17.58, 106.28)),
    (R("h13", "Ban Na", 20.10, 102.30, "Laos", located_by="llm", precision="area", result_type=None),
     R("h14", "Ban Na", 18.20, 102.40, "Laos", located_by="llm", precision="area", result_type=None)),
    (R("h15", "Wat Phu", 14.8466, 105.8222, "Laos"), R("h16", "Phu Si", 19.8924, 102.1352, "Laos")),
    (R("h17", "Bagan", 21.17, 94.86, "Myanmar", result_type="city", stop=True),
     R("h18", "Old Bagan", 21.17, 94.86, "Myanmar", located_by="llm", precision="area", result_type=None)),
    (R("h19", "Plain of Jars Site 1", 19.43, 103.15, "Laos"), R("h20", "Plain of Jars Site 2", 19.40, 103.10, "Laos")),
    (R("h21", "Kuang Si Falls", 19.7491, 101.9935, "Laos"),
     R("h22", "Kuang Si Butterfly Park", 19.752, 101.99, "Laos")),
    (R("h23", "Hoi An", 15.88, 108.33, result_type="city", stop=True), R("h24", "Hoi An Old Town", 15.877, 108.326)),
    (R("h25", "Ha Long", 20.95, 107.07, result_type="city", stop=True), R("h26", "Ha Long Bay", 20.91, 107.18)),
    (R("h27", "Mandalay", 21.96, 96.08, "Myanmar", result_type="city", stop=True),
     R("h28", "Mandalay Hill", 22.00, 96.11, "Myanmar")),
    (R("h29", "Dong Ba Market", 16.4732, 107.588), R("h30", "Dong Ba Bridge", 16.4745, 107.586)),
    (R("h31", "Luang Prabang", 19.8856, 102.1347, "Laos", result_type="city", stop=True),
     R("h32", "Luang Prabang Night Market", 19.889, 102.136, "Laos")),
    (R("h33", "Wat That Luang", 17.9772, 102.6346, "Laos"), R("h34", "Pha That Luang", 17.9763, 102.6363, "Laos")),
    (R("h35", "Shwezigon Pagoda", 21.1944, 94.8939, "Myanmar"), R("h36", "Shwesandaw Pagoda", 21.1628, 94.8728,
                                                                    "Myanmar")),
    (R("h37", "Cu Chi", 10.9733, 106.4931, result_type="suburb"), R("h38", "Cu Chi Tunnels", 11.1426, 106.4637)),
]


@pytest.mark.parametrize("a,b", HOMONYMS, ids=[f"{a.name}|{b.name}" for a, b in HOMONYMS])
def test_homonyms_never_auto_merge(a, b):
    assert dd.decide(a, b).decision != dd.AUTO
    shared = [dd.Row(**{**r.__dict__, "result_type": "city", "place_id": "g-city", "geocoder_name": "Other City"})
              for r in (a, b)]
    assert dd.decide(*shared).decision != dd.AUTO
    suburb = [dd.Row(**{**r.__dict__, "result_type": "suburb", "place_id": "g-ward", "geocoder_name": r.name})
              for r in (a, b)]
    assert dd.decide(*suburb).decision != dd.AUTO


# ── plan: clusters, keeper, invariants ──────────────────────────────────────────────────────────────────────────────

def test_plan_keeper_order_and_no_chaining():
    a = R("a", "Kuang Si Falls", 19.7490, 101.9935, "Laos", components=5, place_id="g1")
    b = R("b", "Kuang Si", 19.7490, 101.9960, "Laos", located_by="llm", precision="area", result_type=None)
    c = R("c", "Kuang Si", 19.7490, 102.0120, "Laos", located_by="llm", precision="area", result_type=None)
    assert dd.decide(b, c).decision == dd.AUTO and dd.decide(a, c).decision != dd.AUTO    # c is b's, not a's
    plan = dd.plan_country([a, b, c])
    merged = {m["merged_id"]: m["keeper_id"] for m in plan.merges}
    assert merged.get("b") == "a" and "c" not in merged


def test_keeper_prefers_a_poi_over_a_ward_centroid():
    ward = R("w", "Ben Thanh", 10.7726, 106.6981, result_type="suburb", components=20)
    market = R("m", "Ben Thanh Market", 10.7725, 106.6980)
    assert dd.keeper_of([ward, market]).id == "m"


def test_an_existing_keeper_is_never_swapped_out():
    old = R("old", "Kuang Si", 19.7491, 101.9935, "Laos", located_by="llm", precision="area", result_type=None,
            merged_members=2)
    poi = R("poi", "Kuang Si Falls", 19.7491, 101.9936, "Laos", components=9)
    plan = dd.plan_country([old, poi])
    assert [(m["merged_id"], m["keeper_id"]) for m in plan.merges] == [("poi", "old")]


def test_keeper_of_a_linear_cluster_is_the_drawn_line():
    rows, _ = load()
    plan = dd.plan_country([rows["lin-namou"], rows["lin-namou-river"], rows["lin-namou-osm"]])
    assert {m["keeper_id"] for m in plan.merges} == {"lin-namou-river"}
    assert {m["merged_id"] for m in plan.merges} == {"lin-namou", "lin-namou-osm"}


def test_two_stretches_of_one_river_both_merge_into_it():
    rows, _ = load()
    plan = dd.plan_country([rows["lin-mekong-river"], rows["lin-mekong-stretch"], rows["lin-mekong-slowboat"]])
    assert sorted(m["merged_id"] for m in plan.merges) == ["lin-mekong-slowboat", "lin-mekong-stretch"]
    assert plan.conflicts == []


def test_manual_row_is_never_merged_away_and_two_manual_rows_never_merge():
    m1 = R("m1", "Kuang Si", 19.7491, 101.9935, "Laos", geo_status="manual", located_by="manual", result_type=None)
    g = R("g", "Kuang Si Falls", 19.7491, 101.9936, "Laos", components=9)
    plan = dd.plan_country([m1, g])
    assert [(m["merged_id"], m["keeper_id"]) for m in plan.merges] == [("g", "m1")]
    m2 = R("m2", "Kuang Si Falls", 19.7491, 101.9937, "Laos", geo_status="manual")
    assert dd.decide(m1, m2).reason == "both_manual"
    plan2 = dd.plan_country([m1, m2])
    assert plan2.merges == []


def test_a_hard_keep_between_two_members_cuts_the_cluster():
    # K = the falls; X = a model point 400 m west, Y = one 450 m east: each is the falls by rules, but X and Y are
    # 850 m apart and Y is named as a cave — X/Y clash on the kind matrix, so only one of them merges.
    k = R("k", "Kuang Si Falls", 19.7491, 101.9935, "Laos")
    x = R("x", "Tat Kuang Si", 19.7491, 101.9897, "Laos", located_by="llm", precision="area", result_type=None)
    y = R("y", "Kuang Si", 19.7491, 101.9978, "Laos", located_by="llm", precision="area", result_type=None)
    plan = dd.plan_country([k, x, y], verdicts={})
    assert {m["merged_id"] for m in plan.merges} == {"x", "y"}            # no clash: both merge
    plan = dd.plan_country([k, x, y], verdicts={dd.pair_key("x", "y"): "different"})
    assert len(plan.merges) == 1 and len(plan.conflicts) == 1
    assert plan.conflicts[0]["clash"]["reason"] == "verdict_different"


def test_a_new_member_must_not_clash_with_rows_already_merged_into_the_keeper():
    k = R("k", "Kuang Si Falls", 19.7491, 101.9935, "Laos", merged_members=1)
    old = R("old", "Kuang Si", 19.7491, 101.9978, "Laos", located_by="llm", precision="area", result_type=None)
    new = R("new", "Tat Kuang Si", 19.7491, 101.9897, "Laos", located_by="llm", precision="area", result_type=None)
    ok = dd.plan_country([k, new], merged={"k": [old]})
    assert [m["merged_id"] for m in ok.merges] == ["new"]
    blocked = dd.plan_country([k, new], merged={"k": [old]}, verdicts={dd.pair_key("new", "old"): "different"})
    assert blocked.merges == [] and blocked.conflicts[0]["destination_id"] == "new"


def test_merging_a_keeper_flattens_its_members():
    k = R("k", "Kuang Si Falls", 19.7491, 101.9935, "Laos", place_id="g1", merged_members=1)
    k2 = R("k2", "Tat Kuang Si", 19.7480, 101.9920, "Laos", located_by="llm", precision="area", result_type=None,
           merged_members=1)
    m2 = R("m2", "Kuang Si Waterfall", 19.7485, 101.9925, "Laos", located_by="llm", precision="area",
           result_type=None)
    plan = dd.plan_country([k, k2], merged={"k2": [m2]})
    assert plan.merges[0]["merged_id"] == "k2" and plan.merges[0]["flatten"] == ["m2"]


def test_llm_band_merges_only_with_a_same_verdict():
    bt = R("bt", "Ben Thanh Market", 10.7725, 106.6980, place_id="g-btmarket")
    bare = R("b2", "Ben Thanh", 10.7726, 106.6981, located_by="llm", precision="area", result_type=None)
    plan = dd.plan_country([bt, bare])
    assert plan.merges == [] and plan.llm_needed and plan.llm_needed[0][:2] == ("b2", "bt")
    plan_same = dd.plan_country([bt, bare], verdicts={("b2", "bt"): "same"})
    assert [(m["merged_id"], m["decided_by"]) for m in plan_same.merges] == [("b2", "llm")]
    plan_unsure = dd.plan_country([bt, bare], verdicts={("b2", "bt"): "unsure"})
    assert plan_unsure.merges == [] and plan_unsure.possible
    assert dd.plan_country([bt, bare], verdicts={("b2", "bt"): "same"}, rules_only=True).merges == []


def test_a_geocode_defect_is_reported():
    a = R("a", "Long Bien Bridge", 21.043, 105.859, place_id="g-x")
    b = R("b", "Long Bien Market", 21.0396, 105.851, place_id="g-x")
    plan = dd.plan_country([a, b])
    assert plan.merges == [] and plan.defects and plan.defects[0]["reason"].endswith("/geocode_defect")


def test_in_tour_remap_uses_rules_only():
    a = R("a", "Kuang Si Falls", 19.7491, 101.9935, "Laos")
    b = R("b", "Tat Kuang Si", 19.7480, 101.9920, "Laos", located_by="llm", precision="area", result_type=None)
    assert dd.in_tour_remap([a, b]) == {"b": "a"}


def test_fingerprint_changes_with_the_plan():
    a = R("a", "Kuang Si Falls", 19.7491, 101.9935, "Laos")
    b = R("b", "Tat Kuang Si", 19.7480, 101.9920, "Laos", located_by="llm", precision="area", result_type=None)
    c = R("c", "Pak Ou Caves", 20.05, 102.219, "Laos")
    assert dd.plan_country([a, b]).fingerprint() == dd.plan_country([a, b, c]).fingerprint()
    assert dd.plan_country([a, b]).fingerprint() != dd.plan_country([a, c]).fingerprint()


def test_derive_card():
    assert dd.derive_card(["c1"], {"c2": 5}) == "c1"
    assert dd.derive_card(["c1", "c2"], {}) is None
    assert dd.derive_card([], {"c1": 3, "c2": 1}) == "c1"
    assert dd.derive_card([], {"c1": 1, "c2": 1}) is None


def test_breaker_counts_only_changes_outside_the_approved_diff():
    assert dd.breaker_limit(100) == 5 and dd.breaker_limit(1000) == 10
    approved = [["m1", "k1"], ["m2", "k1"]]
    assert dd.outside_diff([["m1", "k1"], ["m3", "k2"], ["undo", "m9"]], approved) == [["m3", "k2"], ["undo", "m9"]]


def test_recheck_undoes_only_class_changes_and_far_moves_of_job_merges():
    k = R("k", "Kuang Si Falls", 19.7491, 101.9935, "Laos")
    m = R("m", "Tat Kuang Si", 19.7480, 101.9920, "Laos", located_by="llm", precision="area", result_type=None)
    assert dd.recheck(k, m, "rule") is None
    moved = dd.Row(**{**m.__dict__, "country": "Thailand"})
    assert dd.recheck(k, moved, "rule") == ("undo", "other_country")
    assert dd.recheck(k, moved, "admin") == ("conflict", "other_country")
    near = dd.Row(**{**m.__dict__, "lat": 19.7491 + 0.045})     # ~5 km: past the 4 km radius, not 2 × it
    assert dd.recheck(k, near, "rule")[0] == "conflict"
    far = dd.Row(**{**m.__dict__, "lat": 19.9})                 # ~17 km: past 2 × the radius
    assert dd.recheck(k, far, "llm")[0] == "undo"


# ── the two-model check (stubbed: no paid call in tests) ────────────────────────────────────────────────────────────

def _stub(*answers):
    calls = []
    seq = list(answers)

    async def gen(system, user):
        calls.append(json.loads(user))
        v = seq.pop(0) if len(seq) > 1 else seq[0]
        verdict, conf = v if isinstance(v, tuple) else (v, 0.9)
        return json.dumps({"verdict": verdict, "confidence": conf, "reason": "stub"})
    gen.calls = calls
    return gen


@pytest.mark.parametrize("p,p_swapped,c,verdict,n_primary,n_check", [
    ("same", "same", "same", "same", 2, 1),
    ("same", "same", "different", "different", 2, 1),
    ("same", "same", "unsure", "unsure", 2, 1),
    ("same", "different", "same", "different", 2, 0),       # the primary flips when A and B are swapped
    ("same", ("same", 0.6), "same", "unsure", 2, 0),         # low confidence on the swap
    ("different", "same", "same", "different", 1, 0),
    ("unsure", "same", "same", "unsure", 1, 0),
    (("same", 0.7), "same", "same", "unsure", 1, 0),
])
def test_adjudicate_needs_two_agreeing_models_and_a_stable_order(p, p_swapped, c, verdict, n_primary, n_check):
    primary, check = _stub(p, p_swapped), _stub(c)
    inp = {"country": "Laos", "a": {"name": "A"}, "b": {"name": "B"}}
    out = asyncio.run(dd.adjudicate(inp, primary, check))
    assert out["verdict"] == verdict
    assert len(primary.calls) == n_primary and len(check.calls) == n_check
    if n_primary == 2:
        assert primary.calls[1]["a"]["name"] == "B" and primary.calls[1]["b"]["name"] == "A"


def test_parse_verdict_is_conservative():
    assert dd.parse_verdict('{"verdict": "SAME", "confidence": 0.9, "reason": "x"}') == ("same", 0.9)
    assert dd.parse_verdict("garbage") == ("unsure", 0.0)
    assert dd.parse_verdict('{"verdict": "maybe"}') == ("unsure", 0.0)
    assert dd.parse_verdict('{"verdict": "same"}') == ("same", 0.0)          # no confidence: not a merge
    assert dd.parse_verdict('{"verdict": "same", "confidence": "high"}') == ("same", 0.0)


def test_input_hash_changes_when_a_point_moves_not_when_the_day_text_does():
    a = R("a", "Ben Thanh Market", 10.7725, 106.6980)
    b = R("b", "Ben Thanh", 10.7726, 106.6981, located_by="llm", precision="area", result_type=None)
    dec = dd.decide(a, b)
    h1 = dd.input_hash(dd.pair_input(a, b, dec), ["m1", "m2"])
    assert h1 == dd.input_hash(dd.pair_input(b, a, dec), ["m2", "m1"])
    assert h1 == dd.input_hash(dd.pair_input(a, b, dec, {"a": ["Shop at Ben Thanh."]}), ["m1", "m2"])
    b2 = dd.Row(**{**b.__dict__, "lat": 10.79})
    assert h1 != dd.input_hash(dd.pair_input(a, b2, dd.decide(a, b2)), ["m1", "m2"])
    assert h1 != dd.input_hash(dd.pair_input(a, b, dec), ["m1", "m3"])


def test_pair_input_carries_the_evidence_and_the_sentences():
    a = R("a", "Wat Arun", 13.7437, 100.4889, "Thailand", place_id="g2")
    b = R("b", "Temple of Dawn", 13.7437, 100.4888, "Thailand", place_id="g2")
    inp = dd.pair_input(a, b, dd.decide(a, b), {"a": ["x" * 400, "y", "z"]})
    assert inp["shared"] == ["shared_object"]
    assert len(inp["a"]["tour_sentences"]) == 2 and len(inp["a"]["tour_sentences"][0]) == 300


def test_geojson_lines():
    g = {"type": "MultiLineString", "coordinates": [[[102.1, 21.6], [102.2, 21.5]], [[102.3, 21.4], [102.4, 21.3]]]}
    assert dd.geojson_lines(g) == (((21.6, 102.1), (21.5, 102.2)), ((21.4, 102.3), (21.3, 102.4)))
    assert dd.geojson_lines(json.dumps({"type": "Point", "coordinates": [1, 2]})) == ()


# ── one lexicon ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_every_geomatch_kind_word_has_a_kind_class():
    """The dedupe reads kinds from geomatch.KIND_OF_WORD: a feature word added to geomatch without a class would be
    invisible to the same-place rule."""
    words = (geomatch.FEATURE_WORDS | geomatch.LINEAR_FEATURE_WORDS | geomatch.AREA_FEATURE_WORDS
             | geomatch.POINT_ON_FEATURE_WORDS)
    # read as phrases / settlement / facility; "complexes" is generic ("temple complexes")
    no_kind = {"hot", "national", "city", "village", "visitor", "complexes"} & words
    missing = sorted(w for w in words - no_kind if w not in geomatch.KIND_OF_WORD)
    assert missing == []


def test_linear_subtypes_are_the_linear_words():
    """A linear subtype per line word of geomatch (lines lane: range is an area)."""
    subs = {geomatch.KIND_OF_WORD[w] for w in geomatch.LINEAR_FEATURE_WORDS}
    assert subs <= geomatch.LINEAR_SUBTYPES and "range" not in geomatch.LINEAR_SUBTYPES


def test_name_normalisation_is_names_name_norm():
    from services.places.names import name_norm
    assert dd.clean_name("Hội An (Quảng Nam)") == name_norm("Hội An")
    assert dd.clean_name("Ho Chi Minh's Mausoleum") == name_norm("Ho Chi Minh Mausoleum")
    assert dd.clean_name("", "kuang si") == "kuang si"
