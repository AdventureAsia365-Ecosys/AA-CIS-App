"""AA-671 S258 — matcher version 2 and the pool cover chain, unit level.

- match.resolve_destination version 2: a tie between places on one spot is broken (most tour days, earliest day, id);
  places further apart, or without coordinates, stay ambiguous; a label that IS the name of a place near the tour
  matches it (nearby_exact) where version 1 said weak / no_match; far or generic labels do not; version 1 unchanged.
- photo_destination_match job {"version": 2}: reads the tour places and the nearby names, writes nearby_exact and
  tie-broken answers, counts them; version 1 never reads them.
- covers: the chain levels per pool (the tenant pool reads only catalog views), the coverage report (levels, none per
  country, to_place_after_apply), reported on every run and after an apply, a failed read never fails the job.
The SQL runs on real Postgres in tests/integration/test_aa671_pool_facets.py and test_aa671_photo_destination_match.py.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from api.routers import admin_photos
from api.routers import v1_tours
from services.jobs import photo_destination_match_job as J
from services.photos import covers as C
from services.photos import match as M
from tests.unit.test_aa671_photo_destination_match import CANDS, T1, T2, _JobPool, _enqueue, _photo, _run

P = M.Place
PARO_TOWN = P(27.4300, 89.4100)
TAKTSANG = P(27.4920, 89.3630)          # ~8 km from the town
NEXT_DOOR = P(27.4330, 89.4120)         # ~0.4 km from the town
FAR = P(27.6000, 89.4100)               # ~19 km north


# ── resolve_destination version 2: same-spot tie-break ───────────────────────────────────────────────────────────────

def test_a_tie_between_places_on_one_spot_goes_to_the_place_with_most_days():
    cands = [("a", "Night Bazaar"), ("b", "night bazaar")]
    places = {"a": P(27.43, 89.41, days=1, first_day=3), "b": P(27.4310, 89.4105, days=2, first_day=5)}
    got = M.resolve_destination("Night Bazaar", cands, places)
    assert (got.outcome, got.destination_id, got.rule, got.tie_break) == (M.MATCHED, "b", M.RULE_EXACT,
                                                                          M.TIE_SAME_SPOT)
    assert got.candidates == ("a", "b")
    assert M.resolve_destination("Night Bazaar", list(reversed(cands)), places) == got        # order-independent


def test_same_days_go_to_the_earliest_day_then_the_id():
    cands = [("b", "Brass Gate"), ("a", "Iron Gate")]
    places = {"a": P(27.43, 89.41, days=1, first_day=2), "b": P(27.4302, 89.4101, days=1, first_day=1)}
    got = M.resolve_destination("Brass Gate and Iron Gate", cands, places)
    assert (got.outcome, got.destination_id, got.rule) == (M.MATCHED, "b", M.RULE_NAME_IN_LABEL)
    places["b"] = P(27.4302, 89.4101, days=1, first_day=2)
    assert M.resolve_destination("Brass Gate and Iron Gate", cands, places).destination_id == "a"


@pytest.mark.parametrize("places", [
    {"a": PARO_TOWN, "b": FAR},                                   # two towns: two Night Bazaars
    {"a": PARO_TOWN, "b": P(None, None)},                         # one place not located
    {"a": PARO_TOWN},                                             # one place unknown
])
def test_a_tie_between_places_apart_or_not_located_stays_ambiguous(places):
    got = M.resolve_destination("Night Bazaar", [("a", "Night Bazaar"), ("b", "Night Bazaar")], places)
    assert (got.outcome, got.destination_id, got.candidates) == (M.AMBIGUOUS, None, ("a", "b"))


def test_version_1_is_unchanged_without_places():
    cands = [("a", "Night Bazaar"), ("b", "Night Bazaar")]
    assert M.resolve_destination("Night Bazaar", cands).outcome == M.AMBIGUOUS
    assert M.resolve_destination("Paro", [("tn", "Paro Taktsang")], None, [("town", "Paro", PARO_TOWN)]).outcome \
        == M.WEAK


# ── resolve_destination version 2: nearby_exact ──────────────────────────────────────────────────────────────────────

def test_a_town_label_matches_the_town_near_the_tour_not_its_landmark():
    got = M.resolve_destination("Paro (3)", [("tn", "Paro Taktsang")], {"tn": TAKTSANG},
                                [("town", "Paro", PARO_TOWN)])
    assert (got.outcome, got.destination_id, got.rule) == (M.MATCHED, "town", M.RULE_NEARBY_EXACT)
    assert 5 < got.km < 10


def test_nearby_exact_answers_a_no_match_and_a_label_in_name_tie():
    got = M.resolve_destination("Paro", [("pd", "Punakha Dzong")], {"pd": P(27.44, 89.42)},
                                [("town", "Paro", PARO_TOWN)])
    assert (got.outcome, got.destination_id) == (M.MATCHED, "town")
    got = M.resolve_destination("Taktsor", [("a", "Paro Taktsor"), ("b", "Taktsor Hill")],
                                {"a": TAKTSANG, "b": P(27.50, 89.37)}, [("t", "Taktsor", P(27.495, 89.365))])
    assert (got.outcome, got.destination_id, got.rule) == (M.MATCHED, "t", M.RULE_NEARBY_EXACT)


@pytest.mark.parametrize("label,cands,places,nearby", [
    # too far from every place of the tour
    ("Paro", [("tn", "Paro Taktsang")], {"tn": TAKTSANG}, [("town", "Paro", P(28.5, 90.5))]),
    # the tour's places have no coordinates: nothing to measure from
    ("Paro", [("tn", "Paro Taktsang")], {"tn": P(None, None)}, [("town", "Paro", PARO_TOWN)]),
    # a generic label names a place in every town
    ("Night Market", [("tn", "Paro Taktsang")], {"tn": TAKTSANG}, [("nm", "Night Market", PARO_TOWN)]),
    # only a part of the nearby name: never by containment
    ("Paro", [("tn", "Paro Taktsang")], {"tn": TAKTSANG}, [("x", "Paro Valley View", PARO_TOWN)]),
])
def test_nearby_exact_needs_a_whole_name_near_a_located_place_of_the_tour(label, cands, places, nearby):
    got = M.resolve_destination(label, cands, places, nearby)
    assert got.rule != M.RULE_NEARBY_EXACT and got.destination_id is None


def test_a_stronger_answer_of_the_tour_wins_over_a_nearby_name():
    got = M.resolve_destination("Paro", [("own", "Paro"), ("tn", "Paro Taktsang")],
                                {"own": PARO_TOWN, "tn": TAKTSANG}, [("other", "Paro", NEXT_DOOR)])
    assert (got.destination_id, got.rule) == ("own", M.RULE_EXACT)


def test_two_nearby_places_on_one_spot_are_tie_broken_apart_ambiguous():
    cands, places = [("tn", "Paro Taktsang")], {"tn": TAKTSANG}
    got = M.resolve_destination("Paro", cands, places, [("t1", "Paro", PARO_TOWN), ("t2", "Paro", NEXT_DOOR)])
    assert (got.outcome, got.destination_id, got.tie_break) == (M.MATCHED, "t1", M.TIE_SAME_SPOT)
    got = M.resolve_destination("Paro", cands, places, [("t1", "Paro", PARO_TOWN), ("t2", "Paro", FAR)])
    assert (got.outcome, got.rule, got.candidates) == (M.AMBIGUOUS, M.RULE_NEARBY_EXACT, ("t1", "t2"))


def test_km_between():
    assert M.km_between(PARO_TOWN, PARO_TOWN) == 0
    assert 18 < M.km_between(PARO_TOWN, FAR) < 20
    assert M.km_between(PARO_TOWN, P(None, 1.0)) is None


# ── the job, version 2 ───────────────────────────────────────────────────────────────────────────────────────────────

V2_CANDS = {T1: [("d-tn", "Paro Taktsang"), ("d-m1", "Night Bazaar"), ("d-m2", "Night Bazaar")]}
V2_PLACES = {T1: [("d-tn", 27.4920, 89.3630, 2, 1), ("d-m1", 27.43, 89.41, 1, 3), ("d-m2", 27.4305, 89.4102, 1, 2)]}
V2_NEARBY = [("d-town", "Paro", "paro", 27.43, 89.41), ("d-far", "Paro", "paro", 10.0, 100.0)]


def _v2_rows():
    return [_photo(1, "Paro"), _photo(2, "Night Bazaar"), _photo(3, "Quellin")]


def test_version_2_writes_nearby_and_tie_broken_answers():
    pool = _JobPool(_v2_rows(), V2_CANDS, places=V2_PLACES, nearby=V2_NEARBY)
    res, _ = _run(pool, {"dry_run": False, "version": 2})
    assert res["version"] == 2 and res["written"] == 2
    assert res["rules"] == {M.RULE_NEARBY_EXACT: 1, M.RULE_EXACT: 1} and res["tie_breaks"] == {M.TIE_SAME_SPOT: 1}
    assert [r["destination_id"] for r in pool.rows] == ["d-town", "d-m2", None]   # d-m2: the earlier day
    near = next(s for s in res["samples"]["matched"] if s["rule"] == M.RULE_NEARBY_EXACT)
    assert near["destination"] == "Paro" and near["km"] < 1
    tie = next(s for s in res["samples"]["matched"] if s.get("tie_break"))
    assert [t["destination_id"] for t in tie["tied"]] == ["d-m1", "d-m2"]
    assert ("nearby", ("night bazaar", "paro", "quellin")) in pool.reads and "places" in pool.reads


def test_version_1_is_the_default_and_never_reads_places_or_nearby():
    pool = _JobPool(_v2_rows(), V2_CANDS, places=V2_PLACES, nearby=V2_NEARBY)
    res, _ = _run(pool)
    assert res["version"] == 1 and res["outcomes"]["weak"] == 1 and res["outcomes"]["ambiguous"] == 1
    assert res["tie_breaks"] == {} and "places" not in pool.reads
    assert not any(isinstance(r, tuple) for r in pool.reads)


def test_enqueue_passes_version_2_only_when_asked(monkeypatch):
    _, seen = _enqueue(monkeypatch, admin_photos.DestinationMatchRequest(version=2))
    assert seen["payload"]["version"] == 2
    _, seen = _enqueue(monkeypatch, admin_photos.DestinationMatchRequest())
    assert "version" not in seen["payload"]
    with pytest.raises(ValidationError):
        admin_photos.DestinationMatchRequest(version=3)


# ── coverage report ──────────────────────────────────────────────────────────────────────────────────────────────────

def _cov(tour, country, tour_path=None, place_path=None, near_path=None, near_km=None, dests=()):
    return {"tour_id": tour, "country": country, "tour_path": tour_path, "place_path": place_path,
            "near_path": near_path, "near_km": near_km, "destination_ids": list(dests)}


COVERAGE = [
    _cov("t1", "Bhutan", tour_path="/content/photos/1", dests=["d1"]),
    _cov("t2", "Bhutan", place_path="/content/photos/2", dests=["d2"]),
    _cov("t3", "Bhutan", near_path="/content/photos/3", near_km=4.2, dests=["d3"]),
    _cov("t4", "Laos", near_path="/content/photos/3", near_km=300.0, dests=["d4"]),
    _cov("t5", "Laos", near_path="/content/photos/3", near_km=310.0, dests=["d5", "d-planned"]),
    _cov("t6", "Vietnam", dests=["d6"]),
    _cov("t7", "Vietnam"),
    _cov("t8", None),
]


def test_summarise_counts_each_level_and_what_is_left():
    out = C.summarise(COVERAGE, ["d-planned", "d2"])
    assert out["pool_tours"] == 8 and out["with_cover"] == 5 and out["none"] == 3
    assert out["levels"] == {"tour": 1, "place": 1, "nearby": 1, "country": 2}
    assert out["distinct_photos"] == {"tour": 1, "place": 1, "nearby": 1, "country": 1}   # one photo on 2 cards
    assert out["none_by_country"] == {"Vietnam": 2, "?": 1}
    assert out["no_places"] == 2
    assert out["to_place_after_apply"] == 1          # t5 has a place the run gives a photo; t2 is already a place cover


def test_every_run_reports_coverage_and_an_apply_reports_it_again():
    pool = _JobPool(_v2_rows(), V2_CANDS, coverage=COVERAGE)
    res, _ = _run(pool)
    assert res["covers_coverage"]["with_cover"] == 5 and "covers_coverage_after" not in res
    res, _ = _run(pool, {"dry_run": False})
    assert res["covers_coverage_after"]["pool_tours"] == 8 and pool.reads.count("coverage") == 3


def test_a_failed_coverage_read_is_reported_and_the_run_goes_on():
    pool = _JobPool(_v2_rows(), V2_CANDS, places=V2_PLACES, nearby=V2_NEARBY, coverage=RuntimeError("boom"))
    res, _ = _run(pool, {"dry_run": False, "version": 2})
    assert res["covers_coverage"] == {"error": "RuntimeError"} and res["written"] == 2


# ── the cover chain SQL ──────────────────────────────────────────────────────────────────────────────────────────────

def test_the_tenant_pool_chain_reads_only_catalog_views_and_the_admin_pool_none():
    tenant = C.cover_cte(True) + C.cover_join(True)
    admin = C.cover_cte(False) + C.cover_join(False)
    for rel in ("shared.", "geo.", "silver_aa_internal."):
        assert rel not in tenant                      # aa_app_user holds no grant there (AA-859)
    assert "catalog." not in admin
    for sql in (tenant, admin):
        assert sql.count("(SELECT") >= 3 and "cover_place" in sql and f"<= {C.NEARBY_COVER_KM}" in sql
        assert "{" not in sql and "}" not in sql          # every placeholder filled


def test_the_pool_rows_statement_defines_the_cte_its_cover_join_reads():
    for pool in (True, False):
        sql = v1_tours._POOL_ROWS_SQL.format(
            rewritten_cond="true", facet_join=v1_tours._POOL_FACET_JOIN[pool],
            cover_join=v1_tours._POOL_COVER_JOIN[pool], cover_with=v1_tours._POOL_COVER_WITH[pool],
            where="WHERE true", order_by="pt.published_at DESC", limit=1, offset=2)
        assert sql.lstrip().startswith("WITH cover_place AS MATERIALIZED (")
        assert "cover.path AS cover_photo_path" in sql


def test_the_coverage_statement_reads_the_pool_tours_with_the_admin_pool_chain():
    sql = J.coverage_sql()
    for cond in v1_tours._POOL_BASE_CONDITIONS:
        assert cond in sql
    assert sql.startswith("WITH cover_place AS MATERIALIZED (") and "catalog." not in sql
    assert "near.km AS near_km" in sql


@pytest.mark.parametrize("row,level", [
    (_cov("t", "x", tour_path="/a", place_path="/b"), "tour"),
    (_cov("t", "x", place_path="/b", near_path="/c", near_km=1.0), "place"),
    (_cov("t", "x", near_path="/c", near_km=C.NEARBY_COVER_KM), "nearby"),
    (_cov("t", "x", near_path="/c", near_km=C.NEARBY_COVER_KM + 0.1), "country"),
    (_cov("t", "x"), None),
])
def test_level_of(row, level):
    assert C.level_of(row) == level


def test_t2_is_untouched_by_version_2_rows():
    # a tour without places in a version 2 run: no candidate, nothing nearby is anchored
    pool = _JobPool([_photo(1, "Paro", tour=T2)], CANDS, places=V2_PLACES, nearby=V2_NEARBY)
    res, _ = _run(pool, {"version": 2})
    assert res["outcomes"]["no_candidate"] == 1 and res["outcomes"]["matched"] == 0
