"""AA-976 (CRM-B4) — catalog.http.v1 served by CIS: mapping, paging, liveness answers, caching headers and auth.

Card R / E: an unpublished (never served) tour is 404; a removed (retired) tour is 410 so the CRM marks its lines
"no longer offered"; a paused tour is 404 with its reason (ADR 0015 decision 5: hidden, lines not marked).
"""
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from api.core import m2m_auth
from services.catalog_http import geo, mapping, reader
from tests.unit.aa976_catalog_fake import (
    DEST_BALI, DEST_HALONG, DEST_HANOI, SID_A, SID_B, SID_C, SID_PAUSED, SID_RETIRED, FakePool, catalog_app, seeded,
    tour_id_of,
)
from tests.unit.aa976_m2m import OTHER_CLIENT, install_m2m

BASE = "https://api.example.invalid"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PUBLIC_API_BASE", BASE)
    monkeypatch.delenv(m2m_auth.ENV_M2M_ISSUER, raising=False)
    monkeypatch.delenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, raising=False)
    reader.reset_gate()
    issuer, _ = install_m2m()
    yield issuer
    m2m_auth.set_m2m_verifier(None)
    reader.reset_gate()


@pytest.fixture
def issuer(_env):
    return _env


@pytest.fixture
def views():
    return seeded()


@pytest.fixture
def client(views):
    return TestClient(catalog_app(FakePool(views)))


def _auth(issuer, **claims):
    return {"Authorization": f"Bearer {issuer.token(**claims)}"}


# ── geo ───────────────────────────────────────────────────────────────────────

def test_every_zone_exists_in_the_tz_database():
    for zone in geo.all_zones():
        ZoneInfo(zone)


@pytest.mark.parametrize("name, code", [("Vietnam", "VN"), ("Tibet", "CN"), ("sri lanka", "LK"),
                                        ("Viet Nam", "VN"), ("Philippines", "PH"), ("Atlantis", None), (None, None)])
def test_country_code(name, code):
    assert geo.country_code(name) == code


@pytest.mark.parametrize("code, lng, zone", [
    ("VN", 105.8, "Asia/Ho_Chi_Minh"), ("NP", 85.3, "Asia/Kathmandu"), ("MM", 96.1, "Asia/Yangon"),
    ("ID", 106.8, "Asia/Jakarta"), ("ID", 115.26, "Asia/Makassar"), ("ID", 140.7, "Asia/Jayapura"),
    ("MY", 101.7, "Asia/Kuala_Lumpur"), ("MY", 116.1, "Asia/Kuching"), ("MN", 91.6, "Asia/Hovd"),
    ("MN", 106.9, "Asia/Ulaanbaatar"), ("ID", None, "Asia/Jakarta"), ("XX", 1.0, None), (None, None, None),
])
def test_time_zone(code, lng, zone):
    assert geo.time_zone(code, lng) == zone


# Indonesian stops nearest a zone line (lat, lng): province lines are not meridians, so latitude matters.
@pytest.mark.parametrize("place, lat, lng, zone", [
    ("Banyuwangi (East Java)", -8.22, 114.37, "Asia/Jakarta"),
    ("Ketapang ferry (East Java)", -8.14, 114.40, "Asia/Jakarta"),
    ("Gilimanuk (Bali)", -8.17, 114.434, "Asia/Makassar"),
    ("Banjarmasin (South Kalimantan)", -3.32, 114.59, "Asia/Makassar"),
    ("Palangka Raya (Central Kalimantan)", -2.21, 113.92, "Asia/Jakarta"),
    ("Kupang (East Nusa Tenggara)", -10.17, 123.6, "Asia/Makassar"),
    ("Manado (North Sulawesi)", 1.49, 124.84, "Asia/Makassar"),
    ("Bitung (North Sulawesi)", 1.44, 125.19, "Asia/Makassar"),
    ("Lembeh Strait (North Sulawesi)", 1.45, 125.24, "Asia/Makassar"),
    ("Talaud (North Sulawesi)", 4.0, 126.8, "Asia/Makassar"),
    ("Ternate (North Maluku)", 0.79, 127.38, "Asia/Jayapura"),
    ("Buru (Maluku)", -3.4, 126.6, "Asia/Jayapura"),
    ("Wetar (Maluku)", -7.8, 126.0, "Asia/Jayapura"),
    ("Ambon (Maluku)", -3.69, 128.18, "Asia/Jayapura"),
    ("Raja Ampat (Southwest Papua)", -0.23, 130.52, "Asia/Jayapura"),
])
def test_indonesia_zone_lines(place, lat, lng, zone):
    assert geo.time_zone("ID", lng, lat) == zone, place


def test_destination_zone_uses_latitude():
    body = mapping.destination_body({"destination_id": "00000000-0000-0000-0000-000000000001", "name": "Bitung",
                                     "country": "Indonesia", "lat": 1.44, "lng": 125.19})
    assert body["time_zone"] == "Asia/Makassar"


def test_guards_country_spellings_is_the_public_alias_source():
    from services.places import guards
    assert guards.country_spellings("cn") == ["china", "tibet"]
    assert guards.country_spellings("LK") == ["sri lanka", "sri-landka"]
    assert "kyrgyz republic" in guards.country_spellings("kg")
    assert guards.country_spellings("") == [] and guards.country_spellings("ph") == []


def test_spellings_cover_aliases():
    assert geo.spellings("CN") == ["china", "tibet"]
    assert "viet nam" in geo.spellings("VN")
    assert geo.spellings("ZZ") == []


# ── mapping ───────────────────────────────────────────────────────────────────

def test_split_lines_handles_bullets_separators_and_repeats():
    text = "- Guide\n* Boat cruise; Breakfast daily\n1. Guide\n\n• Entrance fees | Water"
    assert mapping.split_lines(text) == ["Guide", "Boat cruise", "Breakfast daily", "Entrance fees", "Water"]
    assert mapping.split_lines(None) == [] and mapping.split_lines("") == []
    assert len(mapping.split_lines("x" * 900)[0]) == 500


@pytest.mark.parametrize("text, code, on_arrival", [
    ("Entrance fees at the pagodas", "entrance_local", True),
    ("National park fees", "entrance_local", True),
    ("Tips for guide and driver", "tips", True),
    ("Gratuities", "tips", True),
    ("Trekking permit and TIMS card", "permit", True),
    ("Camera fees at monuments", "camera_fee", True),
    ("Visa on arrival (USD 35)", "visa_on_arrival", True),
    ("Vietnam visa", "visa", False),
    ("Single supplement", "single_supplement", True),
    ("International flights", "international_flights", False),
    ("Domestic flights", "flights", False),
    ("Travel insurance", "travel_insurance", False),
    ("Drinks", "drinks", False),
    ("Meals not mentioned", "meals", False),
    ("Personal expenses", "personal_expenses", False),
    ("Anything else", None, False),
])
def test_exclusion_rules(text, code, on_arrival):
    assert mapping.classify_exclusion(text) == (code, on_arrival)


@pytest.mark.parametrize("raw, names", [
    (["Kayaking", " Food  tour "], ["food tour", "kayaking"]),
    ('["Hiking"]', ["hiking"]),
    ("Hiking, Cycling", ["cycling", "hiking"]),
    ([{"name": "Rafting"}, {"type": "Trek"}, 3], ["rafting", "trek"]),
    ({"Biking": True, "Diving": False}, ["biking"]),
    (None, []),
])
def test_facets_activities_from_source_json(raw, names):
    assert mapping.facets(raw)["activities"] == names


def test_facets_intensity_is_the_highest_component_level():
    comps = [{"activity": "walk", "intensity_level": "leisurely"}, {"activity": None, "intensity_level": "active"},
             {"activity": "x", "intensity_level": "unknown"}]
    assert mapping.facets([], comps) == {"activities": ["walk", "x"], "intensity": "challenging"}
    assert mapping.facets([], [])["intensity"] is None


def _body(views, sid=SID_A):
    d = views.details[sid]
    return mapping.tour_body(views.tours[sid], days=d["days"], places=d["places"], photos=d["photos"],
                             components=d["components"], photo_base=BASE)


def test_tour_body_days_places_and_time_zones(views):
    body = _body(views)
    assert body["country_code"] == "VN" and body["duration_days"] == 3
    assert [d["day_no"] for d in body["days"]] == [1, 2, 3]
    day2 = body["days"][1]
    # position order, each destination once, unresolved places left out
    assert [p["destination_id"] for p in day2["places"]] == [DEST_HANOI, DEST_HALONG]
    assert {p["time_zone"] for p in day2["places"]} == {"Asia/Ho_Chi_Minh"}
    assert day2["time_zone"] == "Asia/Ho_Chi_Minh"
    assert body["days"][2] == {"day_no": 3, "title": "Depart", "description": None, "time_zone": "Asia/Ho_Chi_Minh",
                               "places": []}
    assert body["inclusions"] == ["Guide", "Boat cruise", "Breakfast daily"]
    assert [e["code"] for e in body["exclusions"]] == ["international_flights", "entrance_local", "tips", "visa"]
    assert body["facets"] == {"activities": ["cycling", "food tour", "kayaking"], "intensity": "challenging"}
    assert body["photos"] == [{"url": f"{BASE}/content/photos/p1?size=large", "alt": None},
                              {"url": f"{BASE}/content/photos/p2?size=large", "alt": "Bay"}]
    assert body["content_hash"].startswith("sha256:") and len(body["content_hash"]) == 71


def test_content_hash_follows_every_served_field(views):
    first = _body(views)["content_hash"]
    assert _body(views)["content_hash"] == first
    views.details[SID_A]["places"][0]["lat"] = 21.5            # a place moved: v_tour.content_hash would not see it
    assert _body(views)["content_hash"] != first


def test_day_zero_and_missing_duration(views):
    views.details[SID_A]["days"].insert(0, {"source_id": SID_A, "day_number": 0, "title": "Before", "body": "x"})
    views.tours[SID_A]["duration_days"] = None
    body = _body(views)
    assert [d["day_no"] for d in body["days"]] == [1, 2, 3]
    assert body["duration_days"] == 3


def test_tour_without_iso_country_is_not_served(views):
    views.tours[SID_A]["country"] = "Atlantis"
    assert _body(views) is None


def test_long_title_is_clipped(views):
    views.tours[SID_A]["title"] = "T" * 400
    assert len(_body(views)["title"]) == 300


def test_out_of_range_coordinates_become_null(views):
    views.details[SID_A]["places"][0].update(lat=123.0, lng=float("nan"))
    place = _body(views)["days"][0]["places"][0]
    assert (place["lat"], place["lng"]) == (None, None)


def test_photo_url_rules():
    assert mapping.photo_url("/content/photos/x", BASE + "/") == f"{BASE}/content/photos/x?size=large"
    assert mapping.photo_url("/p?a=1", BASE) == f"{BASE}/p?a=1&size=large"
    assert mapping.photo_url("/p?size=small", BASE) == f"{BASE}/p?size=small"
    assert mapping.photo_url("relative", BASE) is None and mapping.photo_url("", BASE) is None


def test_destination_body_zone_by_longitude():
    row = {"destination_id": DEST_BALI, "name": "Ubud", "country": "Indonesia", "lat": -8.5, "lng": 115.26}
    assert mapping.destination_body(row) == {"destination_id": DEST_BALI, "name": "Ubud", "country_code": "ID",
                                             "time_zone": "Asia/Makassar", "lat": -8.5, "lng": 115.26}
    unknown = mapping.destination_body({**row, "country": "Atlantis", "lng": None})
    assert (unknown["country_code"], unknown["time_zone"]) == (None, "UTC")


# ── cursor ────────────────────────────────────────────────────────────────────

def test_cursor_round_trip_and_filter_binding():
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    cur = reader.encode_cursor(SID_B, since, "VN")
    assert reader.decode_cursor(cur, since, "VN") == SID_B
    with pytest.raises(reader.InvalidRequest, match="other filters"):
        reader.decode_cursor(cur, None, "VN")
    for bad in ("junk", "v2.abc.12345678", "v1.!!!.x"):
        with pytest.raises(reader.InvalidRequest):
            reader.decode_cursor(bad, None, None)
    assert len(reader.encode_cursor("x" * 128, since, "VN")) <= 200


# ── routes: auth ──────────────────────────────────────────────────────────────

def _problem(resp, status, error):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert body["error"] == error and isinstance(body["message"], str)
    assert set(body) <= {"error", "message", "reason"}
    return body


@pytest.mark.parametrize("path", ["/catalog/v1/tours", f"/catalog/v1/tours/cis/{SID_A}",
                                  f"/catalog/v1/destinations/{DEST_HANOI}"])
def test_no_token_is_401_with_challenge(client, path):
    resp = client.get(path)
    _problem(resp, 401, "unauthorized")
    assert resp.headers["www-authenticate"].startswith("Bearer")


def test_bad_token_is_401(client):
    _problem(client.get("/catalog/v1/tours", headers={"Authorization": "Bearer nope"}), 401, "unauthorized")


def test_wrong_client_is_403(client, issuer):
    _problem(client.get("/catalog/v1/tours", headers=_auth(issuer, client_id=OTHER_CLIENT)), 403, "forbidden")


def test_missing_scope_is_403(client, issuer):
    _problem(client.get("/catalog/v1/tours", headers=_auth(issuer, scope="booking.b2b/read")), 403, "forbidden")


def test_invalid_m2m_config_is_503(client, issuer, monkeypatch):
    m2m_auth.set_m2m_verifier(None)
    monkeypatch.setenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, "bad id")
    _problem(client.get("/catalog/v1/tours", headers=_auth(issuer)), 503, "unavailable")


def test_no_tenant_pool_is_503(issuer):
    resp = TestClient(catalog_app(None)).get("/catalog/v1/tours", headers=_auth(issuer))
    _problem(resp, 503, "unavailable")
    assert resp.headers["retry-after"] == "5"


def test_pool_at_its_limit_is_503(issuer, views):
    pool = FakePool(views, fail=asyncio.TimeoutError())
    _problem(TestClient(catalog_app(pool)).get(f"/catalog/v1/tours/cis/{SID_A}", headers=_auth(issuer)),
             503, "unavailable")


# ── routes: tours ─────────────────────────────────────────────────────────────

def test_get_tour_served_with_etag_and_cache_headers(client, issuer, views):
    resp = client.get(f"/catalog/v1/tours/cis/{SID_A}", headers=_auth(issuer))
    assert resp.status_code == 200
    body = resp.json()
    assert body["source_id"] == SID_A and body["country_code"] == "VN"
    assert resp.headers["etag"] == f'"{body["content_hash"]}"'
    assert resp.headers["cache-control"] == "private, max-age=60"
    assert resp.headers["vary"] == "Authorization"
    # one READ ONLY, REPEATABLE READ transaction with the tenant GUC cleared; the tour by source_id, its details
    # by the tour_id of that row (lesson S238: source_id is not pushed below DISTINCT ON / window functions)
    assert views.readonly == [True] and views.isolation == ["repeatable_read"]
    assert any("set_config('app.tenant_id', '', true)" in s for s in views.executed)
    details = [(sql, args) for sql, args in views.sql if sql != reader.GET_TOUR_SQL]
    assert len(details) == 4
    assert all("tour_id = ANY($1::uuid[])" in sql and "source_id" not in sql.split("WHERE")[1] for sql, _ in details)
    assert all(args == ([tour_id_of(SID_A)],) for _, args in details)


def test_if_none_match_answers_304(client, issuer):
    etag = client.get(f"/catalog/v1/tours/cis/{SID_A}", headers=_auth(issuer)).headers["etag"]
    for value in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        resp = client.get(f"/catalog/v1/tours/cis/{SID_A}", headers={**_auth(issuer), "If-None-Match": value})
        assert resp.status_code == 304 and resp.content == b""
        assert resp.headers["etag"] == etag
    resp = client.get(f"/catalog/v1/tours/cis/{SID_A}", headers={**_auth(issuer), "If-None-Match": '"stale"'})
    assert resp.status_code == 200


def test_etag_changes_when_content_changes(client, issuer, views):
    first = client.get(f"/catalog/v1/tours/cis/{SID_A}", headers=_auth(issuer)).headers["etag"]
    views.tours[SID_A]["summary"] = "New summary"
    resp = client.get(f"/catalog/v1/tours/cis/{SID_A}", headers={**_auth(issuer), "If-None-Match": first})
    assert resp.status_code == 200 and resp.headers["etag"] != first


def test_retired_tour_is_410_gone_with_reason(client, issuer):
    body = _problem(client.get(f"/catalog/v1/tours/cis/{SID_RETIRED}", headers=_auth(issuer)), 410, "gone")
    assert body["reason"] == "source_deleted"


def test_paused_tour_is_404_with_reason(client, issuer):
    body = _problem(client.get(f"/catalog/v1/tours/cis/{SID_PAUSED}", headers=_auth(issuer)), 404, "not_found")
    assert body["reason"] == "master_inactive"


def test_restored_tombstone_without_a_live_row_is_404(client, issuer, views):
    views.retired["k"] = {"state": "restored", "reason": "restore", "changed_at": None}
    _problem(client.get("/catalog/v1/tours/cis/k", headers=_auth(issuer)), 404, "not_found")


def test_never_served_tour_is_404_without_reason(client, issuer):
    body = _problem(client.get("/catalog/v1/tours/cis/unknown", headers=_auth(issuer)), 404, "not_found")
    assert "reason" not in body


def test_served_tour_wins_over_an_old_tombstone(client, issuer, views):
    views.retired[SID_A] = {"state": "retired", "reason": "source_deleted", "changed_at": None}
    assert client.get(f"/catalog/v1/tours/cis/{SID_A}", headers=_auth(issuer)).status_code == 200


@pytest.mark.parametrize("path", ["/catalog/v1/tours/tp/x", "/catalog/v1/tours/cis/" + "x" * 129])
def test_other_source_system_or_too_long_key_is_404(client, issuer, path):
    _problem(client.get(path, headers=_auth(issuer)), 404, "not_found")


def test_list_pages_with_cursor_until_the_end(client, issuer):
    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
        resp = client.get("/catalog/v1/tours", params=params, headers=_auth(issuer))
        assert resp.status_code == 200
        assert resp.headers["etag"].startswith('"sha256:')
        page = resp.json()
        seen += [i["source_id"] for i in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted([SID_A, SID_B, SID_C]) and pages == 2


def test_list_items_carry_the_tour_content_hash(client, issuer):
    item = next(i for i in client.get("/catalog/v1/tours", headers=_auth(issuer)).json()["items"]
                if i["source_id"] == SID_A)
    tour = client.get(f"/catalog/v1/tours/cis/{SID_A}", headers=_auth(issuer)).json()
    assert item["content_hash"] == tour["content_hash"]
    assert item["updated_at"] == "2026-10-20T08:00:00Z"
    assert set(item) == {"source_system", "source_id", "title", "country_code", "duration_days", "content_hash",
                         "updated_at"}


def test_list_reads_details_once_per_page_with_any(client, issuer, views):
    client.get("/catalog/v1/tours", headers=_auth(issuer))
    detail_sql = [sql for sql, _ in views.sql if sql != reader.LIST_TOURS_SQL]
    assert len(detail_sql) == 4 and all(sql.endswith("tour_id = ANY($1::uuid[])") for sql in detail_sql)


def test_list_filters(client, issuer):
    def ids(**params):
        resp = client.get("/catalog/v1/tours", params=params, headers=_auth(issuer))
        assert resp.status_code == 200, resp.text
        return [i["source_id"] for i in resp.json()["items"]]

    assert ids(country_code="ID") == [SID_B]
    assert ids(country_code="KR") == []
    assert ids(updated_since="2026-10-21T00:00:00Z") == [SID_B]
    assert ids(updated_since="2026-10-21T07:00:00+07:00") == [SID_B]


@pytest.mark.parametrize("params", [{"updated_since": "yesterday"}, {"updated_since": "2026-10-21T00:00:00"},
                                    {"country_code": "vn"}, {"country_code": "VNM"}, {"limit": "0"},
                                    {"limit": "51"}, {"limit": "x"}, {"cursor": "junk"}, {"cursor": "x" * 201}])
def test_bad_list_parameters_are_400_invalid(client, issuer, params):
    _problem(client.get("/catalog/v1/tours", params=params, headers=_auth(issuer)), 400, "invalid")


def test_cursor_from_other_filters_is_400(client, issuer):
    page = client.get("/catalog/v1/tours", params={"limit": "1"}, headers=_auth(issuer)).json()
    resp = client.get("/catalog/v1/tours", params={"limit": "1", "cursor": page["next_cursor"],
                                                   "country_code": "VN"}, headers=_auth(issuer))
    _problem(resp, 400, "invalid")


def test_list_page_etag_and_304(client, issuer):
    first = client.get("/catalog/v1/tours", headers=_auth(issuer))
    again = client.get("/catalog/v1/tours", headers={**_auth(issuer), "If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


# ── routes: destinations ──────────────────────────────────────────────────────

def test_destination_200_and_304(client, issuer):
    resp = client.get(f"/catalog/v1/destinations/{DEST_BALI}", headers=_auth(issuer))
    assert resp.status_code == 200
    assert resp.json()["time_zone"] == "Asia/Makassar"
    again = client.get(f"/catalog/v1/destinations/{DEST_BALI}",
                       headers={**_auth(issuer), "If-None-Match": resp.headers["etag"]})
    assert again.status_code == 304


@pytest.mark.parametrize("dest", [DEST_HALONG, "not-a-uuid"])
def test_unknown_or_malformed_destination_is_404(client, issuer, dest):
    _problem(client.get(f"/catalog/v1/destinations/{dest}", headers=_auth(issuer)), 404, "not_found")


# ── wiring in the real app ────────────────────────────────────────────────────

def test_main_app_serves_the_catalog_routes_with_problem_bodies():
    from api.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert {"/catalog/v1/tours", "/catalog/v1/tours/{source_system}/{source_id}",
            "/catalog/v1/destinations/{destination_id}"} <= paths
    resp = TestClient(app).get("/catalog/v1/tours")
    assert resp.status_code == 401 and resp.json()["error"] == "unauthorized"


def test_catalog_paths_are_not_staff_or_tenant_paths():
    from api.middleware.staff_bearer import is_staff_path

    assert not is_staff_path("/catalog/v1/tours")
    assert not "/catalog/v1/tours".startswith("/v1/")
