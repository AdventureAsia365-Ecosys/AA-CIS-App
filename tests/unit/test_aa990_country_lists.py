"""AA-990 — countries come from shared.countries (ISO 3166-1), not from a hand-made list; places are cross-border.

History: migration 197 added Pakistan and the Philippines to ``chk_raw_tours_country`` while
``services/places/guards.py`` kept 18 countries, so every Pakistan / Philippines place ended ``unknown_to_model``
(F3, #1047). S252 found the same for any place outside the 20-name list: the model answered Kilimanjaro with
``[-3.07, 37.35, "Tanzania"]`` and the guard threw it away, because Tanzania was not "an AA country".

Nghiệp (S252, 27/10): a tour's country is exactly what its source says (the two Kilimanjaro treks come from
raw-inbox INDIA.xlsx, country column INDIA: they stay India); each PLACE gets its own real country from geocoding, and
a place in another country than its tour is a normal cross-border stop. These tests pin that: every list reads
shared/countries, the guards check the PLACE's country (never the tour's), compounds never become a country, S0 takes
the column over the file name, and migration 288 swaps the 20-name CHECK for a foreign key to shared.countries.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.aa_content import validate
from services.acp_shared.place_cards import normalise_card_country
from services.catalog_http import geo
from services.places import geocoder as gc
from services.places import guards, locate, pipeline, shape
from shared import countries
from shared.country_resolver import resolve_country

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "api" / "migrations"
M288 = MIGRATIONS / "288_raw_tours_country_iso.sql"
KEY = "test-key-aa990"


# ── one list ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_every_list_is_the_iso_list():
    iso = {c.iso2.lower() for c in countries.all_countries()}
    assert len(iso) == 249
    assert set(guards.ISO2_COUNTRY) == set(guards.ISO2_ISO3) == set(guards.BBOX) == iso
    assert set(shape.country_isos()) == iso
    assert validate.COUNTRY_CODES == {c.upper() for c in iso}
    assert not hasattr(guards, "AA_ISO2") and not hasattr(geo, "_EXTRA_ISO2")


@pytest.mark.parametrize("country, iso", [("Tanzania", "tz"), ("Russian Federation", "ru"), ("Russia", "ru"),
                                          ("Kazakhstan", "kz"), ("North Korea", "kp"), ("Bangladesh", "bd"),
                                          ("Singapore", "sg"), ("Pakistan", "pk"), ("Philippines", "ph")])
def test_countries_outside_the_old_list_are_place_countries(country, iso):
    assert guards.iso2(country) == iso
    assert guards.canonical_country(country) == countries.by_iso2(iso).name
    assert gc.GeoapifyGeocoder.params("x", iso)["filter"] == f"countrycode:{iso}"


# ── cross-border: the PLACE's country, never the tour's ──────────────────────────────────────────────────────────

KILIMANJARO = (-3.07, 37.35)
MOSHI = (-3.35, 37.34)


def test_guard_checks_the_place_country_not_the_tour_country():
    assert guards.point_ok(*KILIMANJARO, "Tanzania") is None
    assert guards.point_ok(*MOSHI, "tanzania") is None
    assert guards.point_ok(*KILIMANJARO, "India") == "outside_country_bbox"      # the tour's country would be wrong
    assert guards.point_ok(13.36, 103.86, "Cambodia") is None                     # Siem Reap on a Thailand tour
    assert guards.point_ok(*KILIMANJARO, "India and Pakistan") == "unknown_country"
    assert shape.in_country(*KILIMANJARO, "tz") and not shape.in_country(*KILIMANJARO, "in")


def test_model_answer_in_another_country_than_the_tour_is_kept():
    raw = ('{"Mount Kilimanjaro": [-3.07, 37.35, "Tanzania"], "Moshi": [-3.35, 37.34, "United Republic of Tanzania"],'
           ' "Hunza Valley": [36.32, 74.66, "Pakistan"], "Atlantis": [0.1, 0.1, "Atlantis"]}')
    out = locate.parse_answer(raw, ["Mount Kilimanjaro", "Moshi", "Hunza Valley", "Atlantis"])
    assert out["Mount Kilimanjaro"].country == "Tanzania" and out["Moshi"].country == "Tanzania"
    assert out["Hunza Valley"].country == "Pakistan"
    assert out["Atlantis"] is None


def _geoapify(payloads):
    http = MagicMock()
    resp = []
    for p in payloads:
        r = MagicMock()
        r.status_code, r.headers = 200, {}
        r.json.return_value = p
        resp.append(r)
    http.get = AsyncMock(side_effect=resp)
    g = gc.GeoapifyGeocoder(http=http, api_key=KEY, sleep=AsyncMock())
    g._throttle.interval = 0.0
    return g, http


@pytest.mark.asyncio
async def test_kilimanjaro_place_on_an_india_tour_is_located_in_tanzania():
    """A trek sold as India (its Excel row says INDIA) climbs Kilimanjaro: the place is a Tanzanian place, confirmed
    by the geocoder searched in Tanzania, checked against Tanzania's box; the tour's India is only a hint."""
    async def generate(system, user):
        assert "Mount Kilimanjaro | tour country: India" in user     # the tour country is a hint only
        return '{"Mount Kilimanjaro": [-3.07, 37.35, "Tanzania"]}'

    hit = {"results": [{"name": "Mount Kilimanjaro", "result_type": "amenity", "lat": -3.0674, "lon": 37.3556,
                        "country_code": "tz", "rank": {"confidence": 1}}]}
    g, http = _geoapify([hit])
    item = locate.Item(("mount kilimanjaro", "india", ""), "Mount Kilimanjaro", "India", "Kilimanjaro Trek")
    out = await locate.locate_many([item], generate, g)
    got = out[item.key]
    assert got.point is not None, got
    assert got.point.country == "Tanzania"
    assert http.get.call_args[1]["params"]["filter"] == "countrycode:tz"
    assert guards.point_ok(got.point.lat, got.point.lng, got.point.country) is None


@pytest.mark.asyncio
async def test_model_only_kilimanjaro_keeps_the_model_point_in_tanzania():
    async def generate(system, user):
        return '{"Moshi": [-3.35, 37.34, "Tanzania"]}'

    item = locate.Item(("moshi", "india", ""), "Moshi", "India")
    out = await locate.locate_many([item], generate, gc.get_geocoder("model_only"))
    assert out[item.key].point.country == "Tanzania" and out[item.key].point.located_by == "llm"


def test_pipeline_canonical_keeps_compounds_as_identity():
    """A compound card country stays as written in the identity keys (TripPlanner pins, AA-989); a known name is
    canonical. The located destination carries the place's real country."""
    assert pipeline.canonical("tanzania") == "Tanzania"
    assert pipeline.canonical("India and Pakistan") == "India and Pakistan"


# ── compounds never become a country (new place cards) ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, tour, expected", [
    ("Tanzania", "India", "Tanzania"),                     # cross-border: the place's own country
    ("United Republic of Tanzania", "India", "Tanzania"),
    ("India and Pakistan", "India", "India"),              # the tour's country when it is one of the parts
    ("India and Pakistan", "Pakistan", "Pakistan"),
    ("Pakistan, India, China", "Nepal", "Pakistan"),       # else the first part
    ("Nepal and China", "China", "China"),
    ("Thailand and Myanmar", "Laos", "Thailand"),
    ("Viet Nam", "Laos", "Vietnam"),
    ("", "India", "India"), (None, "Bhutan", "Bhutan"),    # nothing from the model: the tour's country
    ("Himalayas", "Nepal", "Nepal"),                       # not a country: the tour's
    ("Himalayas", "", "Himalayas"),
])
def test_normalise_card_country(raw, tour, expected):
    assert normalise_card_country(raw, tour) == expected


# ── S0: the tour's country is what its source says ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, filename, expected", [
    ("INDIA", "raw-inbox/INDIA.xlsx", "India"),            # the Kilimanjaro treks: row 160 / 168 of INDIA.xlsx
    (None, "raw-inbox/INDIA.xlsx", "India"),                # empty column: the file name
    ("", "INDIA.xlsx", "India"),
    ("TANZANIA", "INDIA.xlsx", "Tanzania"),                 # an explicit column value wins over the file name
    ("Singapore", "MALAYSIA.xlsx", "Singapore"),            # any ISO country (the old CHECK refused it)
    ("Tibet", "CHINA.xlsx", "China"),                       # ISO has no Tibet: China, as shared.countries maps it
    ("India & Nepal", "NEPAL.xlsx", "Nepal"),               # a compound: the file's country when it is a part
    ("India & Nepal", "tours.xlsx", None),                  # else unresolved (country_raw_unresolved)
    ("Atlantis", "INDIA.xlsx", None),                       # never the file name over a different column value
    (None, "south_korea_2026.xlsx", "South Korea"),
    (None, "Laos and Vietnam.xlsx", None),                  # two countries: no single one ('and' is not Andorra)
    (None, "Tours in Asia.xlsx", None),                     # 'in' is not India
    (None, None, None),
])
def test_s0_country_follows_the_source(raw, filename, expected):
    assert resolve_country(raw, filename) == expected


# ── migration 288: the 20-name CHECK becomes a foreign key to shared.countries ───────────────────────────────────

def test_288_is_one_transaction_that_registers_itself():
    sql = M288.read_text(encoding="utf-8")
    assert sql.count("\nBEGIN;\n") == 1 and sql.rstrip().endswith("COMMIT;")
    assert "VALUES ('288', now()," in sql and "ON CONFLICT (version) DO NOTHING" in sql
    assert "SET LOCAL lock_timeout" in sql


def test_288_swaps_the_list_for_the_iso_table():
    sql = M288.read_text(encoding="utf-8")
    assert re.search(r"ADD CONSTRAINT raw_tours_country_fkey\s+FOREIGN KEY \(country\) REFERENCES shared\.countries "
                     r"\(name\) NOT VALID", sql)
    assert "VALIDATE CONSTRAINT raw_tours_country_fkey" in sql
    assert re.search(r"CHECK \(\(country IS NULL\) = \(country_iso2 IS NULL\)\) NOT VALID", sql)
    assert "DROP CONSTRAINT IF EXISTS chk_raw_tours_country" in sql
    # it changes no tour's country: no UPDATE of raw_tours at all
    assert not re.search(r"UPDATE\s+silver_aa_internal\.raw_tours", sql, re.I)


def test_no_later_migration_brings_a_country_list_back():
    for path in MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"):
        if int(path.name[:3]) > 288:
            assert not re.search(r"ADD CONSTRAINT chk_raw_tours_country", path.read_text(encoding="utf-8")), path


def test_conftest_replay_skips_288():
    conftest = (REPO / "tests" / "integration" / "conftest.py").read_text(encoding="utf-8")
    assert "not f.startswith('288')" in conftest


# ── content truth: the source country is not written as where the tour happens ───────────────────────────────────

KILI_TOUR = {"name": "Kilimanjaro Machame Route Trek", "country": "India", "duration": "7 days",
             "itineraries": "Day 1: Arrive at Moshi, Tanzania. Day 2: Machame Gate to Machame Camp."}


def test_facts_box_destination_follows_the_source_text():
    from services.content_generation import s1_writer_v2 as v2
    assert v2.destination(KILI_TOUR, "India") == "Tanzania"
    laos_vn = {"name": "Laos to Vietnam by Bicycle", "itineraries": "Luang Prabang, Laos to Hanoi, Vietnam."}
    assert v2.destination(laos_vn, "Laos") == "Laos"                      # the source country is named: kept
    assert v2.destination({"name": "Paro Valley Walk"}, "Bhutan") == "Bhutan"


def test_seo_meta_names_tanzania_so_no_india_tail():
    from services.content_generation.seo_meta_utils import _countries_named, fit_seo_meta_final
    meta = "A seven-day Machame Route climb of Mount Kilimanjaro in Tanzania, with guides, porters and camps."
    assert _countries_named(meta) == {"Tanzania"}
    out = fit_seo_meta_final(meta, KILI_TOUR)
    assert "India" not in out


def test_the_tours_own_title_places_are_never_foreign():
    from services.seo_intelligence.seed_builder import rank_keyword_ideas
    ideas = [{"keyword": "mount kilimanjaro machame route", "search_volume": 900},
             {"keyword": "kilimanjaro trek cost", "search_volume": 400},
             {"keyword": "paro kilimanjaro packages", "search_volume": 50}]
    kept = rank_keyword_ideas(ideas, ["kilimanjaro", "machame"], country="India",
                              foreign_places=frozenset({"mount kilimanjaro", "paro"}))
    assert [i["keyword"] for i in kept] == ["mount kilimanjaro machame route", "kilimanjaro trek cost"]


def test_admin_country_edit_takes_any_iso_country_in_its_canonical_spelling():
    from fastapi import HTTPException

    from api.routers.admin_pipeline import _tour_country
    assert _tour_country("india") == "India" and _tour_country("TZ") == "Tanzania"
    assert _tour_country(" Viet Nam ") == "Vietnam"
    with pytest.raises(HTTPException) as e:
        _tour_country("India and Pakistan")
    assert e.value.status_code == 422
