"""AA-990 F3 — the CIS place country list is one list, and it follows the raw_tours country constraint.

Migration 197 added Pakistan and the Philippines to ``chk_raw_tours_country`` but ``services/places/guards.py`` kept
its 18 countries, so ``canonical_country('Pakistan')`` was None and every Pakistan / Philippines place name ended
``unknown_to_model`` (no destination, no map, no country in the planner list). These tests fail the next time the
constraint and the guard list part ways, and keep every other CIS country list reading from the guard module.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from services.aa_content import validate
from services.catalog_http import geo
from services.places import geocoder as gc
from services.places import guards
from services.places import shape

REPO = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO / "api" / "migrations"
# The constraint as a migration writes it: "ADD CONSTRAINT chk_raw_tours_country CHECK (" (150, 197) or, in a
# reconcile, the name and its pg_get_constraintdef text "'chk_raw_tours_country', 'CHECK (" (281).
_DEFINES = re.compile(r"chk_raw_tours_country\W+CHECK\b")


def _latest_constraint_countries() -> tuple[str, set[str]]:
    found = []
    for path in MIGRATIONS.glob("[0-9][0-9][0-9]*_*.sql"):
        sql = path.read_text()
        m = _DEFINES.search(sql)
        if not m:
            continue
        start = sql.index("ARRAY[", m.end())
        block = sql[start:sql.index("]", start)]
        found.append((int(re.match(r"\d+", path.name).group()), path.name,
                      set(re.findall(r"'([A-Z][A-Za-z ]+)'", block))))
    assert found, "no migration defines chk_raw_tours_country"
    _, name, countries = max(found)
    return name, countries


LATEST, ALLOWED = _latest_constraint_countries()


def test_the_constraint_parse_is_sound():
    # 281 is the latest definition today (the Dev form of 197's list); both list the same 20 countries.
    assert len(ALLOWED) >= 20, LATEST
    assert {"Pakistan", "Philippines", "Vietnam", "South Korea", "Sri Lanka"} <= ALLOWED


@pytest.mark.parametrize("country", sorted(ALLOWED))
def test_every_constraint_country_is_an_aa_place_country(country):
    assert guards.canonical_country(country) == country
    assert guards.canonical_country(country.upper()) == country
    iso = guards.iso2(country)
    assert iso and iso in guards.BBOX and iso in guards.ISO2_ISO3 and iso in guards.AA_ISO2
    assert iso.upper() in validate.AA_COUNTRY_CODES
    assert geo.country_code(country) == iso.upper()
    assert geo.time_zone(iso.upper()) is not None
    assert iso in shape.country_isos()
    assert gc.GeoapifyGeocoder.params("x", iso)["filter"] == f"countrycode:{iso}"


def test_no_aa_country_outside_the_constraint():
    assert set(guards.ISO2_COUNTRY.values()) == ALLOWED
    assert set(guards.ISO2_COUNTRY) == set(guards.BBOX) == set(guards.ISO2_ISO3) == set(guards.AA_ISO2)


def test_one_source_of_truth():
    # catalog_http.geo adds only neighbours that are NOT AA countries; the guard module owns every AA country.
    assert not set(geo._EXTRA_ISO2.values()) & {c.upper() for c in guards.AA_ISO2}
    assert not set(geo._EXTRA_ISO2) & set(guards.COUNTRY_ISO2)
    assert validate.AA_COUNTRY_CODES == frozenset(c.upper() for c in guards.AA_ISO2)
    # the offline polygon builder keeps a literal copy (it runs with python3 -I); it must equal the guard's
    spec = importlib.util.spec_from_file_location("aa847_country_polygons",
                                                  REPO / "scripts" / "aa847_country_polygons.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    assert builder.ISO3_ISO2 == guards.ISO3_ISO2
    assert set(shape.country_isos()) == set(guards.ISO2_COUNTRY)


@pytest.mark.parametrize("lat, lng, country", [
    (33.69, 73.06, "Pakistan"),        # Islamabad
    (24.86, 67.01, "Pakistan"),        # Karachi
    (25.12, 62.32, "Pakistan"),        # Gwadar
    (36.32, 74.66, "Pakistan"),        # Karimabad, Hunza
    (35.88, 76.51, "Pakistan"),        # K2
    (14.60, 120.98, "Philippines"),    # Manila
    (20.45, 121.97, "Philippines"),    # Basco, Batanes
    (5.03, 119.77, "Philippines"),     # Bongao, Tawi-Tawi
    (11.20, 119.41, "Philippines"),    # El Nido, Palawan
    (9.85, 126.05, "Philippines"),     # Siargao
])
def test_pakistan_and_philippines_points_pass_the_guard(lat, lng, country):
    assert guards.point_ok(lat, lng, country) is None
    assert guards.point_ok(lat, lng, country.lower()) is None


def test_pakistan_and_philippines_reject_the_wrong_side():
    # BBOX is a rectangle: New Delhi (28.61, 77.21) is inside Pakistan's box; the polygon (shape) tells them apart
    assert guards.point_ok(27.70, 85.32, "Pakistan") == "outside_country_bbox"       # Kathmandu
    assert guards.point_ok(34.16, 77.58, "Pakistan") == "outside_country_bbox"       # Leh, Ladakh (India)
    assert guards.point_ok(34.55, 77.55, "Pakistan") == "outside_country_bbox"       # Diskit, Nubra (India)
    assert not shape.in_country(28.61, 77.21, "pk") and shape.in_country(28.61, 77.21, "in")
    assert guards.point_ok(22.32, 114.17, "Philippines") == "outside_country_bbox"   # Hong Kong
    assert guards.canonical_country("Republic of the Philippines") == "Philippines"
    assert guards.canonical_country("Islamic Republic of Pakistan") == "Pakistan"


def test_model_answers_for_pakistan_and_philippines_parse():
    """The locate step drops an answer whose country is not AA: before AA-990 every PK/PH answer became None."""
    from services.places import locate
    raw = '{"Hunza Valley": [36.32, 74.66, "Pakistan"], "Intramuros": [14.59, 120.97, "Philippines"]}'
    out = locate.parse_answer(raw, ["Hunza Valley", "Intramuros"])
    assert out["Hunza Valley"] is not None and out["Hunza Valley"].country == "Pakistan"
    assert out["Intramuros"] is not None and out["Intramuros"].country == "Philippines"


def test_polygons_hold_the_capitals():
    assert shape.in_country(33.69, 73.06, "pk") and shape.in_country(14.60, 120.98, "ph")
    assert not shape.in_country(14.60, 120.98, "pk")


def test_dedupe_drops_every_guard_country_word():
    """'Badshahi Mosque Pakistan' and 'Badshahi Mosque' must share a core, like the other AA countries already do."""
    from services.places import dedupe
    assert dedupe.parse(dedupe.clean_name("Badshahi Mosque Pakistan")).core == \
        dedupe.parse(dedupe.clean_name("Badshahi Mosque")).core
    for country in guards.ISO2_COUNTRY.values():
        for word in country.lower().split():
            assert word in dedupe.COUNTRY_WORDS or word in {"south"}, word   # "south" stays a qualifier
