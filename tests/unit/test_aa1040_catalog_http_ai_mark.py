"""AA-1040 (EU AI Act Art. 50(2), root ADR 0019 d19) — catalog.http.v1 1.3: every served ``Tour`` carries ``ai``,
the machine-readable mark of its AI-written Master text, the same object as the CIS tenant tour reads
(``services.acp_content_writing.ai_marking.tour_mark``).

The mark sits inside the hashed body, so the tour's ETag moves with it (a Master publish moves ``updated_at``) and
stays put otherwise. The router answer is validated against the contract's ``AiMark`` schema.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from api.core import m2m_auth
from api.schemas import public_api
from services.acp_content_writing.ai_marking import CATALOG_GENERATOR, tour_mark
from services.catalog_http import mapping, reader
from tests.unit.aa976_catalog_fake import SID_A, FakePool, catalog_app, seeded
from tests.unit.aa976_m2m import install_m2m
from tests.unit.test_aa976_catalog_http_contract import DOC

BASE = "https://api.example.invalid"
FIRST = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
LAST = datetime(2026, 10, 2, 3, 0, tzinfo=timezone.utc)


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


def _body(views, sid=SID_A):
    d = views.details[sid]
    return mapping.tour_body(views.tours[sid], days=d["days"], places=d["places"], photos=d["photos"],
                             components=d["components"], photo_base=BASE)


def test_contract_is_1_3_with_an_ai_mark_on_tour():
    assert DOC["info"]["version"] == "1.3"
    schemas = DOC["components"]["schemas"]
    assert schemas["Tour"]["properties"]["ai"] == {"$ref": "#/components/schemas/AiMark"}
    assert "ai" not in schemas["Tour"]["required"]  # additive: a 1.2 body stays valid
    assert set(schemas["AiMark"]["required"]) == {"ai_generated", "generator", "model", "generated_at",
                                                  "human_edited"}


def test_tour_body_carries_the_catalog_mark():
    views = seeded()
    views.tours[SID_A].update(updated_at=LAST, published_at=FIRST)
    ai = _body(views)["ai"]
    assert ai == tour_mark(LAST, FIRST)
    assert ai == {"ai_generated": True, "generator": CATALOG_GENERATOR, "model": None,
                  "generated_at": LAST.isoformat(), "human_edited": None}


def test_generated_at_falls_back_to_the_first_publish_then_null():
    views = seeded()
    views.tours[SID_A].update(updated_at=None, published_at=FIRST)
    assert _body(views)["ai"]["generated_at"] == FIRST.isoformat()
    views.tours[SID_A].update(updated_at=None, published_at=None)
    assert _body(views)["ai"]["generated_at"] is None
    assert _body(views)["ai"]["ai_generated"] is True


def test_the_mark_is_inside_the_hash():
    views = seeded()
    views.tours[SID_A].update(updated_at=LAST)
    first = _body(views)["content_hash"]
    assert _body(views)["content_hash"] == first                     # deterministic
    views.tours[SID_A].update(updated_at=datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc))  # a new Master publish
    assert _body(views)["content_hash"] != first


def test_served_tour_answers_with_a_contract_valid_mark(_env):
    views = seeded()
    views.tours[SID_A].update(updated_at=LAST, published_at=FIRST)
    client = TestClient(catalog_app(FakePool(views)))
    resp = client.get(f"/catalog/v1/tours/cis/{SID_A}", headers={"Authorization": f"Bearer {_env.token()}"})
    assert resp.status_code == 200
    ai = resp.json()["ai"]
    assert ai["ai_generated"] is True and ai["generator"] == CATALOG_GENERATOR
    assert ai["generated_at"] == LAST.isoformat()
    validator = Draft202012Validator({"$ref": "#/components/schemas/AiMark", "components": DOC["components"]})
    assert not list(validator.iter_errors(ai))
    public_api.CatalogTour.model_validate(resp.json())


def test_reader_selects_both_publish_times():
    assert "published_at" in reader.GET_TOUR_SQL and "updated_at" in reader.GET_TOUR_SQL
    assert "published_at" in reader.LIST_TOURS_SQL
