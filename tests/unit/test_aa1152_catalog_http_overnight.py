"""AA-1152 follow-up (contract catalog.http.v1 1.4) — ``TourDay.overnight_destination_id``.

Each served day carries the CIS destination where the travellers sleep after it, read from
``catalog.v_tour_day_route.overnight_destination_id`` (the value TripPlanner's day cards read), or null. It may be a
place that is not among the day's ``places``: the partner CRM's add-on drawer offers add-ons there too. Fake views
only (``tests/unit/aa976_catalog_fake.py``); the real view and its grant run in
``tests/integration/test_aa976_catalog_http_reader.py``.
"""
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from api.core import m2m_auth
from services.catalog_http import mapping, reader
from tests.unit.aa976_catalog_fake import DEST_HANOI, DEST_ISLET, SID_A, FakePool, catalog_app, seeded
from tests.unit.aa976_m2m import install_m2m

BASE = "https://api.example.invalid"
CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "catalog.http.v1.openapi.yaml"
DOC = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


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


def _auth(issuer):
    return {"Authorization": f"Bearer {issuer.token()}"}


def _body(views, sid=SID_A, **override):
    d = {**views.details[sid], **override}
    return mapping.tour_body(views.tours[sid], days=d["days"], places=d["places"], photos=d["photos"],
                             components=d["components"], routes=d["routes"], photo_base=BASE)


def test_each_day_carries_its_overnight_destination_or_null(views):
    days = _body(views)["days"]
    assert [d["overnight_destination_id"] for d in days] == [DEST_HANOI, DEST_ISLET, None]


def test_overnight_may_be_a_place_that_is_not_one_of_the_days_stops(views):
    day2 = _body(views)["days"][1]
    assert day2["overnight_destination_id"] == DEST_ISLET
    assert DEST_ISLET not in [p["destination_id"] for p in day2["places"]]
    assert day2["time_zone"] == "Asia/Ho_Chi_Minh"            # the day's zone still follows its last stop


def test_without_route_rows_every_day_is_null_and_the_key_is_kept(views):
    body = _body(views, routes=[])
    assert all("overnight_destination_id" in d and d["overnight_destination_id"] is None for d in body["days"])


def test_overnight_is_part_of_the_content_hash(views):
    first = _body(views)["content_hash"]
    views.details[SID_A]["routes"][0]["overnight_destination_id"] = DEST_HANOI
    assert _body(views)["content_hash"] != first


def test_route_of_a_day_that_is_not_served_is_ignored(views):
    routes = [{"source_id": SID_A, "day_index": 0, "overnight_destination_id": DEST_ISLET},
              {"source_id": SID_A, "day_index": 9, "overnight_destination_id": DEST_ISLET}]
    body = _body(views, routes=routes)
    assert [d["overnight_destination_id"] for d in body["days"]] == [None, None, None]


def test_overnights_skips_rows_without_a_day_or_a_destination_and_keeps_one_per_day():
    rows = [
        {"day_index": None, "overnight_destination_id": DEST_ISLET},
        {"day_index": 1, "overnight_destination_id": None},
        {"day_index": 2, "overnight_destination_id": DEST_ISLET},
        {"day_index": 2, "overnight_destination_id": DEST_HANOI},
    ]
    assert mapping.overnights(rows) == {2: min(DEST_ISLET, DEST_HANOI)}
    assert mapping.overnights([]) == {}


def test_served_tour_validates_against_the_1_4_contract(client, issuer):
    resp = client.get(f"/catalog/v1/tours/cis/{SID_A}", headers=_auth(issuer))
    assert resp.status_code == 200
    body = resp.json()
    assert body["days"][1]["overnight_destination_id"] == DEST_ISLET
    schema = {"$ref": "#/components/schemas/Tour", "components": DOC["components"]}
    assert sorted(e.message for e in Draft202012Validator(schema).iter_errors(body)) == []


def test_contract_declares_the_field_optional_and_nullable():
    day = DOC["components"]["schemas"]["TourDay"]
    assert "overnight_destination_id" not in day["required"]
    assert day["properties"]["overnight_destination_id"]["type"] == ["string", "null"]
    assert DOC["info"]["version"] == "1.4"


def test_reader_reads_the_day_route_view_by_tour_id_and_only_rows_with_a_night():
    sql = reader._DETAIL_SQL["routes"].format(cond=reader.DETAIL_COND)  # noqa: SLF001
    assert "FROM catalog.v_tour_day_route" in sql
    assert "overnight_destination_id IS NOT NULL" in sql
    assert sql.endswith("tour_id = ANY($1::uuid[])")
