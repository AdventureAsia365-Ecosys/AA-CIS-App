"""AA-990: component-id redirects of a place rebuild (TripPlanner saved pins) and the angle-request re-point plan."""
import asyncio
from unittest.mock import AsyncMock

from services.acp_shared import atom_repoint as ar
from services.places import components as cm
from services.places import days as days_mod
from services.places import names
from tests.unit.test_aa988_989_follow import _Conn, _pool


def _comp(day, place, activity="cultural_heritage", card=None, link=None, country="Vietnam"):
    return cm.Component(component_id=names.component_id("src", day, place, country, activity), day_index=day,
                        name=place, link_name=link or place, name_norm=names.name_norm(link or place),
                        country=country, country_norm=names.country_norm(country), locality_norm="", locality=None,
                        activity=activity, intensity_level="leisurely", duration_hint="full_day", season_months=[1],
                        text_extract=place, place_card_id=card)


def _old(cid, day, name, link=None, dest=None, card=None, country="vietnam", activity="cultural_heritage"):
    return {"component_id": cid, "day_index": day, "name": name, "name_norm": names.name_norm(link or name),
            "country_norm": country, "activity": activity, "destination_id": dest, "place_card_id": card}


# ── redirect_dropped ──────────────────────────────────────────────────────────────────────────────────────────────

def test_ambiguous_place_the_carry_over_refused_gets_a_redirect_to_the_nearest_day():
    a, b = _comp(2, "Hoi An Ancient Town"), _comp(5, "Hoi An Ancient Town", activity="culinary")
    old = [_old("old-x", 4, "Hoi An Ancient Town")]
    comps, dest = [a, b], {}
    mapping, _ = cm.carry_over_ids(comps, dest, old)
    assert mapping == {}                                         # AA-989 never guesses a served id
    redirects, counts = cm.redirect_dropped(comps, dest, old)
    assert redirects == {"old-x": b.component_id}                 # day 5 is nearer than day 2
    assert counts == {"ids_redirected": 1, "ids_redirected_by": {"spelling": 1}, "ids_unredirected": 0}


def test_many_old_rows_redirect_to_one_component():
    hanoi = _comp(1, "Hanoi Old Quarter")
    old = [_old("old-d1", 1, "Hanoi Old Quarter", activity="culinary"),
           _old("old-d3", 3, "Hanoi Old Quarter")]
    mapping, _ = cm.carry_over_ids([hanoi], {}, old)
    cm.apply_carry_over([hanoi], {}, mapping)
    redirects, counts = cm.redirect_dropped([hanoi], {}, old)
    assert set(redirects) | set(mapping.values()) == {"old-d1", "old-d3"}
    assert all(t == hanoi.component_id for t in redirects.values())
    assert counts["ids_unredirected"] == 0


def test_rule_order_destination_then_card_then_spelling_then_link_name():
    by_dest = _comp(1, "Thien Mu Pagoda")
    by_card = _comp(2, "Imperial City", card="card-1")
    by_link = _comp(3, "Perfume River Cruise", link="Perfume River")
    dest = {by_dest.component_id: "d-pagoda"}
    old = [_old("o-dest", 7, "Linh Mu Pagoda", dest="d-pagoda"),
           _old("o-card", 7, "Hue Citadel", card="card-1"),
           _old("o-link", 7, "Boat trip", link="Perfume River"),
           _old("o-gone", 7, "Bach Ma National Park", dest="d-elsewhere")]
    redirects, counts = cm.redirect_dropped([by_dest, by_card, by_link], dest, old)
    assert redirects == {"o-dest": by_dest.component_id, "o-card": by_card.component_id,
                         "o-link": by_link.component_id}
    assert counts["ids_redirected_by"] == {"destination": 1, "card": 1, "link_name": 1}
    assert counts["ids_unredirected"] == 1                         # the place the rewrite removed


def test_spelling_needs_the_same_country_unless_the_old_row_has_none():
    hcm = _comp(1, "Ben Thanh Market")
    other_country = [_old("o-1", 1, "Ben Thanh Market", country="cambodia")]
    assert cm.redirect_dropped([hcm], {}, other_country)[0] == {}
    no_country = [_old("o-2", 1, "Ben Thanh Market", country=None)]
    assert cm.redirect_dropped([hcm], {}, no_country)[0] == {"o-2": hcm.component_id}


def test_a_kept_or_carried_id_is_never_redirected():
    same = _comp(1, "Halong Bay")
    old = [_old(same.component_id, 1, "Halong Bay")]
    assert cm.redirect_dropped([same], {}, old) == ({}, {"ids_redirected": 0, "ids_redirected_by": {},
                                                         "ids_unredirected": 0})


def test_tie_on_day_prefers_the_same_activity_then_the_smaller_id():
    a = _comp(3, "Sapa", activity="trekking")
    b = _comp(3, "Sapa", activity="local_immersion")
    old = [_old("o", 3, "Sapa", activity="local_immersion")]
    assert cm.redirect_dropped([a, b], {}, old)[0] == {"o": b.component_id}
    old = [_old("o", 3, "Sapa", activity="culinary")]
    assert cm.redirect_dropped([a, b], {}, old)[0] == {"o": min(a.component_id, b.component_id)}


# ── merge_redirects (cumulative per source) ──────────────────────────────────────────────────────────────────────

def test_merge_repoints_an_older_redirect_whose_target_was_dropped_now():
    final = {"C", "D"}
    previous = {"A": "B", "X": "D", "Y": "Z"}         # B dropped now, D still served, Z gone without a successor
    fresh = {"B": "C"}
    merged, lost = cm.merge_redirects(previous, fresh, final)
    assert merged == {"A": "C", "X": "D", "B": "C"} and lost == 1


def test_merge_drops_an_id_that_is_served_again_and_caps_newest():
    merged, lost = cm.merge_redirects({"A": "B"}, {}, {"A", "B"})
    assert merged == {} and lost == 0
    previous = {f"o{i}": "T" for i in range(5)}
    merged, _ = cm.merge_redirects(previous, {"n1": "T", "n2": "T"}, {"T"}, cap=3)
    assert list(merged) == ["o4", "n1", "n2"]


# ── pipeline: the build counts carry the redirect map, the job result only the counters ──────────────────────────

def test_rebuild_stores_cumulative_redirects_in_the_build_counts(monkeypatch):
    from services.places import pipeline, store
    text = "Day 1 — Hoi An\nOld town.\n\nDay 2 — Hoi An\nMarket and lanterns."
    atoms = [{"atom_id": "v2_a1", "itinerary_day": 1, "place": "Hoi An Ancient Town", "activity_type": "culture",
              "text": "Lantern lanes"},
             {"atom_id": "v2_a2", "itinerary_day": 2, "place": "Hoi An Ancient Town", "activity_type": "food",
              "text": "Cooking class"}]
    build = {"source_id": "src", "tour_id": "t2", "status": "ok", "rule_version": days_mod.RULE_VERSION,
             "days_hash": days_mod.days_hash(text), "components_hash": "v1-hash",
             "counts": {cm.REDIRECTS_KEY: {"very-old": "old-x"}}}
    previous = [_old("old-x", 1, "Hoi An Ancient Town", activity="local_immersion"),
                _old("old-y", 2, "Hoi An Ancient Town", activity="wellness_relaxation")]
    written = {}

    async def _write_build(conn, source_id, **kw):
        written.update(kw)

    fakes = {
        "get_build": AsyncMock(return_value=build), "tour_atoms": AsyncMock(return_value=atoms),
        "has_embedding_column": AsyncMock(return_value=False),
        "existing_days": AsyncMock(return_value=[
            {"day_index": d, "title": "Hoi An", "overnight_place": "Hoi An", "places": [], "extracted_by": "rule",
             "is_transit_day": False} for d in (1, 2)]),
        "resolved_names": AsyncMock(return_value={}), "known_destinations": AsyncMock(return_value={}),
        "existing_places": AsyncMock(return_value=previous), "destination_briefs": AsyncMock(return_value={}),
        "lock_build": AsyncMock(), "live_tour_for_source": AsyncMock(return_value="t2"),
        "lock_destinations": AsyncMock(), "insert_names": AsyncMock(), "replace_source_rows": AsyncMock(),
        "write_build": _write_build, "pending_names_due": AsyncMock(return_value=0),
    }
    for name, fn in fakes.items():
        monkeypatch.setattr(store, name, fn)
    tour = {"tour_id": "t2", "source_id": "src", "country": "Vietnam", "aa_itineraries": text, "aa_name": "HA"}
    res = asyncio.run(pipeline.extract_tour_places(_pool(_Conn()), tour, pipeline.ExtractDeps()))
    assert res["status"] == "ok"
    assert cm.REDIRECTS_KEY not in res                                   # job result: counters only
    stored = written["counts"][cm.REDIRECTS_KEY]
    # old-x / old-y: one candidate per day → AA-989 carries both ids in place; the older redirect to old-x stays valid
    assert res["ids_carried"] == 2 and res["ids_redirected"] == 0
    assert stored == {"very-old": "old-x"}
    assert res["ids_redirects_total"] == len(stored)


# ── angle-gate requests: re-point plan ──────────────────────────────────────────────────────────────────────────

T1 = "11111111-1111-1111-1111-111111111111"


def _req(rid, atom, tenant=T1, status="approved"):
    return {"request_id": rid, "tenant_id": tenant, "atom_id": atom, "status": status}


def _atom(aid, place, day=1, act="culture", scope="platform", card=None, source="S"):
    return {"atom_id": aid, "tour_id": "t", "owner_scope": scope, "itinerary_day": day, "place": place,
            "activity_type": act, "card_id": card, "source_id": source}


def test_repoint_to_the_unique_live_atom_of_the_same_place():
    plan = ar.plan_repoints([_req("r1", "old1")], {"old1": _atom("old1", "Hội An")},
                            [_atom("new1", "Hoi An"), _atom("other", "Da Nang")])
    assert plan[0]["outcome"] == "repoint" and plan[0]["new_atom_id"] == "new1" and plan[0]["rule"] == "place"


def test_several_atoms_of_the_place_narrow_by_day_then_activity():
    old = {"old1": _atom("old1", "Sapa", day=3, act="trek")}
    cands = [_atom("a", "Sapa", day=2, act="trek"), _atom("b", "Sapa", day=3, act="trek"),
             _atom("c", "Sapa", day=3, act="food")]
    plan = ar.plan_repoints([_req("r1", "old1")], old, cands)
    assert plan[0]["new_atom_id"] == "b" and plan[0]["rule"] == "place_day_activity"
    plan = ar.plan_repoints([_req("r1", "old1")], old, cands[:2])
    assert plan[0]["new_atom_id"] == "b" and plan[0]["rule"] == "place_day"


def test_equal_candidates_are_ambiguous_and_never_fall_to_the_card_rule():
    old = {"old1": _atom("old1", "Sapa", day=3, card="k")}
    cands = [_atom("a", "Sapa", day=3), _atom("b", "Sapa", day=3), _atom("c", "Lao Cai", card="k")]
    plan = ar.plan_repoints([_req("r1", "old1")], old, cands)
    assert plan[0]["outcome"] == "ambiguous" and plan[0]["new_atom_id"] is None


def test_respelt_place_falls_back_to_the_place_card():
    old = {"old1": _atom("old1", "Tiger's Nest", card="k")}
    plan = ar.plan_repoints([_req("r1", "old1")], old, [_atom("n", "Taktsang Monastery", card="k")])
    assert plan[0]["new_atom_id"] == "n" and plan[0]["rule"] == "card"


def test_other_tenant_atoms_and_other_tours_are_never_candidates():
    old = {"old1": _atom("old1", "Hue")}
    cands = [_atom("x", "Hue", scope="22222222-2222-2222-2222-222222222222"), _atom("y", "Hue", source="OTHER")]
    plan = ar.plan_repoints([_req("r1", "old1")], old, cands)
    assert plan[0]["outcome"] == "no_match"
    plan = ar.plan_repoints([_req("r1", "old1")], old, [_atom("z", "Hue", scope=T1)])
    assert plan[0]["new_atom_id"] == "z"                           # the request's own tenant atom is fine


def test_unknown_atom_and_summary_counts():
    plan = ar.plan_repoints([_req("r1", "gone"), _req("r2", "old1")], {"old1": _atom("old1", "Hue")},
                            [_atom("n", "Hue")])
    assert [d["outcome"] for d in plan] == ["no_atom", "repoint"]
    s = ar.summary(plan)
    assert s["requests"] == 2 and s["repoint"] == 1 and s["no_atom"] == 1 and s["by_rule"] == {"place": 1}


def test_repoint_sql_is_guarded_on_the_old_atom():
    assert "atom_id = $2" in ar.REPOINT_SQL and "request_id = $1::uuid" in ar.REPOINT_SQL
    assert "route_segment_ids IS NULL" in ar.STALE_REQUESTS_SQL
