"""AA-990: component-id redirects of a place rebuild (TripPlanner saved pins) and the angle-request re-point plan."""
import asyncio
from unittest.mock import AsyncMock

from services.acp_shared import atom_repoint as ar
from services.places import components as cm
from services.places import days as days_mod
from services.places import names
from tests.unit.test_aa988_989_follow import _Conn, _pool


def _comp(day, place, activity="cultural_heritage", card=None, link=None, country="Vietnam", locality=""):
    return cm.Component(component_id=names.component_id("src", day, place, country, activity), day_index=day,
                        name=place, link_name=link or place, name_norm=names.name_norm(link or place),
                        country=country, country_norm=names.country_norm(country), locality_norm=locality,
                        locality=None,
                        activity=activity, intensity_level="leisurely", duration_hint="full_day", season_months=[1],
                        text_extract=place, place_card_id=card)


def _old(cid, day, name, link=None, dest=None, card=None, country="vietnam", activity="cultural_heritage",
         locality=""):
    return {"component_id": cid, "day_index": day, "name": name, "name_norm": names.name_norm(link or name),
            "country_norm": country, "locality_norm": locality, "activity": activity, "destination_id": dest,
            "place_card_id": card}


# ── redirect_dropped ──────────────────────────────────────────────────────────────────────────────────────────────

def test_ambiguous_place_the_carry_over_refused_gets_a_redirect_to_the_nearest_day():
    a, b = _comp(2, "Hoi An Ancient Town"), _comp(5, "Hoi An Ancient Town", activity="culinary")
    old = [_old("old-x", 4, "Hoi An Ancient Town")]
    comps, dest = [a, b], {}
    mapping, _ = cm.carry_over_ids(comps, dest, old)
    assert mapping == {}                                         # AA-989 never guesses a served id
    redirects, counts, rules = cm.redirect_dropped(comps, dest, old)
    assert redirects == {"old-x": b.component_id}                 # day 5 is nearer than day 2
    assert rules == {"old-x": "spelling"}
    assert counts == {"ids_redirected": 1, "ids_redirected_by": {"spelling": 1}, "ids_unredirected": 0,
                      "ids_unredirected_generic": 0}


def test_many_old_rows_redirect_to_one_component():
    hanoi = _comp(1, "Hanoi Old Quarter")
    old = [_old("old-d1", 1, "Hanoi Old Quarter", activity="culinary"),
           _old("old-d3", 3, "Hanoi Old Quarter")]
    mapping, _ = cm.carry_over_ids([hanoi], {}, old)
    cm.apply_carry_over([hanoi], {}, mapping)
    redirects, counts, rules = cm.redirect_dropped([hanoi], {}, old)
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
    redirects, counts, rules = cm.redirect_dropped([by_dest, by_card, by_link], dest, old)
    assert redirects == {"o-dest": by_dest.component_id, "o-card": by_card.component_id,
                         "o-link": by_link.component_id}
    assert counts["ids_redirected_by"] == {"destination": 1, "card": 1, "link_name": 1}
    assert rules == {"o-dest": "destination", "o-card": "card", "o-link": "link_name"}
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
                                                         "ids_unredirected": 0, "ids_unredirected_generic": 0}, {})


def test_tie_on_day_prefers_the_same_activity_then_the_smaller_id():
    a = _comp(3, "Sapa", activity="trekking")
    b = _comp(3, "Sapa", activity="local_immersion")
    old = [_old("o", 3, "Sapa", activity="local_immersion")]
    assert cm.redirect_dropped([a, b], {}, old)[0] == {"o": b.component_id}
    old = [_old("o", 3, "Sapa", activity="culinary")]
    assert cm.redirect_dropped([a, b], {}, old)[0] == {"o": min(a.component_id, b.component_id)}


# ── generic names ('Night Market'): same locality only ─────────────────────────────────────────────────────────────

def test_generic_name_never_redirects_to_the_same_name_in_another_city():
    # the Chiang Mai stop was rewritten away; only the Chiang Rai market is left in the tour
    chiang_rai = _comp(4, "Night Market", country="Thailand", locality="chiang rai")
    old = [_old("o-cm", 2, "Night Market", country="thailand", locality="chiang mai")]
    redirects, counts, rules = cm.redirect_dropped([chiang_rai], {}, old)
    assert redirects == {}
    assert counts["ids_unredirected"] == 1 and counts["ids_unredirected_generic"] == 0


def test_generic_name_redirects_within_the_same_city_any_day():
    cm_market = _comp(3, "Night Market", country="Thailand", locality="chiang mai")
    cr_market = _comp(2, "Night Market", country="Thailand", locality="chiang rai")
    old = [_old("o-cm", 2, "Night Market", country="thailand", locality="chiang mai")]
    redirects, _, _ = cm.redirect_dropped([cm_market, cr_market], {}, old)
    assert redirects == {"o-cm": cm_market.component_id}         # same city wins over the nearer day


def test_generic_name_without_a_locality_is_counted_never_guessed():
    chiang_rai = _comp(2, "Night Market", country="Thailand", locality="chiang rai")
    old = [_old("o-?", 2, "Night Market", country="thailand", locality=cm.UNRESOLVABLE_LOCALITY),
           _old("o-none", 2, "Night Market", country=None, locality=None)]
    redirects, counts, rules = cm.redirect_dropped([chiang_rai], {}, old)
    assert redirects == {}
    assert counts["ids_unredirected"] == 2 and counts["ids_unredirected_generic"] == 2
    unknown = _comp(2, "Night Market", country="Thailand", locality=None)      # the new side has no locality either
    old = [_old("o-cm", 2, "Night Market", country="thailand", locality="chiang mai")]
    assert cm.redirect_dropped([unknown], {}, old)[0] == {}


def test_generic_spelling_of_a_card_linked_place_needs_the_card_or_destination():
    # spelt "Night Market", linked to a distinctive card name → locality_norm '' on both sides: no spelling match
    cr = _comp(4, "Night Market", link="Chiang Rai Night Bazaar", country="Thailand", card="card-cr")
    old = [_old("o-cm", 2, "Night Market", link="Chiang Mai Night Bazaar", country="thailand", card="card-cm")]
    assert cm.redirect_dropped([cr], {}, old)[0] == {}
    old = [_old("o-cr", 2, "Night Market", link="Chiang Rai Night Bazaar", country="thailand", card="card-cr")]
    assert cm.redirect_dropped([cr], {}, old)[0] == {"o-cr": cr.component_id}


# ── pipeline: the redirects are written with the place rows (geo.component_redirect, CIS 285) ─────────────────────

def _rebuild(monkeypatch, previous):
    from services.places import pipeline, store
    text = "Day 1 — Hoi An\nOld town.\n\nDay 2 — Hoi An\nMarket and lanterns."
    atoms = [{"atom_id": "v2_a1", "itinerary_day": 1, "place": "Hoi An Ancient Town", "activity_type": "culture",
              "text": "Lantern lanes"},
             {"atom_id": "v2_a2", "itinerary_day": 2, "place": "Hoi An Ancient Town", "activity_type": "food",
              "text": "Cooking class"}]
    build = {"source_id": "src", "tour_id": "t2", "status": "ok", "rule_version": days_mod.RULE_VERSION,
             "days_hash": days_mod.days_hash(text), "components_hash": "v1-hash", "counts": {}}
    calls = []

    async def _replace(conn, source_id, *a, **kw):
        calls.append("replace_source_rows")

    async def _write_redirects(conn, source_id, redirects, rules, served):
        calls.append(("write_redirects", source_id, dict(redirects), dict(rules), sorted(served)))
        return 1

    async def _write_build(conn, source_id, **kw):
        calls.append(("write_build", kw["counts"]))

    fakes = {
        "get_build": AsyncMock(return_value=build), "tour_atoms": AsyncMock(return_value=atoms),
        "has_embedding_column": AsyncMock(return_value=False),
        "existing_days": AsyncMock(return_value=[
            {"day_index": d, "title": "Hoi An", "overnight_place": "Hoi An", "places": [], "extracted_by": "rule",
             "is_transit_day": False} for d in (1, 2)]),
        "resolved_names": AsyncMock(return_value={}), "known_destinations": AsyncMock(return_value={}),
        "existing_places": AsyncMock(return_value=previous), "destination_briefs": AsyncMock(return_value={}),
        "lock_build": AsyncMock(), "live_tour_for_source": AsyncMock(return_value="t2"),
        "lock_destinations": AsyncMock(), "insert_names": AsyncMock(), "replace_source_rows": _replace,
        "write_redirects": _write_redirects, "write_build": _write_build,
        "pending_names_due": AsyncMock(return_value=0),
    }
    for name, fn in fakes.items():
        monkeypatch.setattr(store, name, fn)
    tour = {"tour_id": "t2", "source_id": "src", "country": "Vietnam", "aa_itineraries": text, "aa_name": "HA"}
    res = asyncio.run(pipeline.extract_tour_places(_pool(_Conn()), tour, pipeline.ExtractDeps()))
    return res, calls


def test_rebuild_writes_redirects_after_the_place_rows_in_the_same_transaction(monkeypatch):
    # old-x / old-y: one candidate per day → AA-989 carries both; old-z (day 3, gone) → redirect to the day-2 row
    previous = [_old("old-x", 1, "Hoi An Ancient Town", activity="local_immersion"),
                _old("old-y", 2, "Hoi An Ancient Town", activity="wellness_relaxation"),
                _old("old-z", 3, "Hoi An Ancient Town", activity="trekking")]
    res, calls = _rebuild(monkeypatch, previous)
    assert res["status"] == "ok"
    names_ = [c if isinstance(c, str) else c[0] for c in calls]
    assert names_ == ["replace_source_rows", "write_redirects", "write_build"]
    _, source_id, redirects, rules, served = calls[1]
    assert source_id == "src"
    assert {"old-x", "old-y"} <= set(served)                       # carried ids are served: their rows are cleared
    assert set(redirects) == {"old-z"} and redirects["old-z"] in served
    assert set(rules) == {"old-z"} and rules["old-z"] in cm.REDIRECT_RULES
    assert res["ids_redirected"] == 1 and res["ids_redirects_cleared"] == 1
    assert "id_redirects" not in calls[2][1]                       # the map is a table now, not build counts


def test_write_redirects_clears_served_ids_then_upserts_dropped_ones():
    from services.places import store

    class _C:
        def __init__(self):
            self.ops = []

        async def execute(self, sql, *args):
            self.ops.append(("execute", " ".join(sql.split()), args))
            return "DELETE 2"

        async def executemany(self, sql, rows):
            self.ops.append(("executemany", " ".join(sql.split()), list(rows)))

    c = _C()
    n = asyncio.run(store.write_redirects(c, "src", {"b": "n2", "a": "n1"}, {"a": "card", "b": "spelling"},
                                          ["n2", "n1"]))
    assert n == 2
    assert c.ops[0][0] == "execute" and "DELETE FROM geo.component_redirect" in c.ops[0][1]
    assert c.ops[0][2] == (["n1", "n2"],)
    assert c.ops[1][0] == "executemany" and "ON CONFLICT (old_id) DO UPDATE" in c.ops[1][1]
    assert c.ops[1][2] == [("a", "n1", "src", "card"), ("b", "n2", "src", "spelling")]
    c = _C()
    assert asyncio.run(store.write_redirects(c, "src", {}, {}, [])) == 0 and c.ops == []


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


# ── D4: AA-989 carry-over keeps a generic name's id only in the same town ────────────────────────────────────────

def test_carry_over_never_moves_a_generic_id_to_another_town():
    # Chiang Mai "Night Market" (day 2) rewritten away; the only unmatched new market is in Chiang Rai (day 4)
    chiang_rai = _comp(4, "Night Market", country="Thailand", locality="chiang rai")
    old = [_old("o-cm", 2, "Night Market", country="thailand", locality="chiang mai")]
    mapping, counts = cm.carry_over_ids([chiang_rai], {}, old)
    assert mapping == {} and counts["ids_dropped"] == 1


def test_carry_over_keeps_a_generic_id_in_the_same_town_any_day():
    market = _comp(3, "Night Market", country="Thailand", locality="chiang mai")
    old = [_old("o-cm", 2, "Night Market", country="thailand", locality="chiang mai")]
    mapping, counts = cm.carry_over_ids([market], {}, old)
    assert mapping == {market.component_id: "o-cm"} and counts["ids_carried_by"] == {"link_name_any_day": 1}


def test_carry_over_generic_without_a_town_needs_destination_or_card():
    market = _comp(2, "Night Market", country="Thailand", locality=None, card="card-1")
    old = [_old("o-?", 2, "Night Market", country="thailand", locality=cm.UNRESOLVABLE_LOCALITY)]
    assert cm.carry_over_ids([market], {}, old)[0] == {}           # same day + same spelling: no town, no key
    old = [_old("o-card", 2, "Night Market", country="thailand", locality=cm.UNRESOLVABLE_LOCALITY, card="card-1")]
    assert cm.carry_over_ids([market], {}, old)[0] == {market.component_id: "o-card"}


def test_carry_over_distinctive_names_are_unchanged():
    hoi_an = _comp(5, "Hoi An Ancient Town")
    old = [_old("o-ha", 2, "Hoi An Ancient Town")]
    assert cm.carry_over_ids([hoi_an], {}, old)[0] == {hoi_an.component_id: "o-ha"}


# ── D2: computed atom_replaced state on the tenant's request reads ───────────────────────────────────────────────

class _StateConn:
    def __init__(self, states, old, cands):
        self.states, self.old, self.cands, self.calls = states, old, cands, []

    async def fetch(self, sql, *args):
        from services.acp_shared import atom_replaced as arx
        self.calls.append((sql, args))
        if sql == arx._STATE_SQL:
            return self.states
        if sql == ar.OLD_ATOMS_SQL:
            return [o for o in self.old if o["atom_id"] in args[0]]
        if sql == ar.CANDIDATES_SQL:
            return [c for c in self.cands if c["source_id"] in args[0]]
        raise AssertionError(sql)


class _StatePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Ctx()


def _st(rid, atom, live, route=False):
    return {"request_id": rid, "tenant_id": T1, "atom_id": atom, "status": "approved", "is_route": route,
            "live": live}


def test_atom_state_flags_replaced_requests_and_hints_the_unique_replacement():
    from services.acp_shared import atom_replaced as arx
    conn = _StateConn(
        [_st("r-live", "a-live", True), _st("r-uniq", "a-old", False), _st("r-amb", "a-old2", False),
         _st("r-route", None, False, route=True)],
        [_atom("a-old", "Hoi An Ancient Town", day=2), _atom("a-old2", "Sapa", day=1)],
        [_atom("a-new", "Hoi An Ancient Town", day=3), _atom("s1", "Sapa", day=2), _atom("s2", "Sapa", day=3)])
    items = [{"request_id": r} for r in ("r-live", "r-uniq", "r-amb", "r-route", "r-other")]
    asyncio.run(arx.with_atom_state(items, T1, _StatePool(conn)))
    got = {i["request_id"]: (i["atom_replaced"], i["atom_replacement"]) for i in items}
    assert got["r-live"] == (False, None)
    assert got["r-uniq"] == (True, {"atom_id": "a-new", "place": "Hoi An Ancient Town", "itinerary_day": 3})
    assert got["r-amb"] == (True, None)                            # two Sapa atoms on other days: no guess
    assert got["r-route"] == (False, None)                         # a Route request has its own 409
    assert got["r-other"] == (False, None)                         # not the tenant's: defaults only
    sql, args = conn.calls[0]
    assert "r.tenant_id = $1::uuid" in sql and args == (T1, sorted(i["request_id"] for i in items))


def test_atom_state_is_fail_soft_and_reads_nothing_for_no_items():
    from services.acp_shared import atom_replaced as arx

    class _Boom(_StateConn):
        async def fetch(self, sql, *args):
            raise RuntimeError("db down")
    items = [{"request_id": "r1"}]
    asyncio.run(arx.with_atom_state(items, T1, _StatePool(_Boom([], [], []))))
    assert items == [{"request_id": "r1", "atom_replaced": False, "atom_replacement": None}]
    conn = _StateConn([], [], [])
    assert asyncio.run(arx.atom_states(_StatePool(conn), T1, [])) == {} and conn.calls == []
