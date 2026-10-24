"""AA-976 — the /catalog/v1 routes conform to the contract catalog.http.v1 (OpenAPI 3.1, JSON Schema 2020-12).

``docs/contracts/catalog.http.v1.openapi.yaml`` is a byte-identical copy of the root contract
(AA-Ecosys ``docs/contracts/``, the source of truth, checked there by ``tools/contracts/check_contracts.py``, whose
rule M1 compares this copy with the root file in the workspace). This repo cannot see the root repo in CI, so
:data:`ROOT_CONTRACT_SHA256` pins the root file's sha256: a change to the copy alone fails here. Every
scenario below calls the real router (fake catalog views, local M2M issuer) and validates the answer against the
response the contract declares for that operation and status: the body schema (``additionalProperties: false``
everywhere, so an extra field fails), the declared status codes, and the ETag header where the contract lists one.
"""
import hashlib
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from api.core import m2m_auth
from services.catalog_http import reader
from tests.unit.aa976_catalog_fake import (
    DEST_BALI, DEST_HANOI, SID_A, SID_PAUSED, SID_RETIRED, FakePool, catalog_app, seeded,
)
from tests.unit.aa976_m2m import OTHER_CLIENT, install_m2m

CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "catalog.http.v1.openapi.yaml"
DOC = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
# sha256 of AA-Ecosys docs/contracts/catalog.http.v1.openapi.yaml (info.version 1.2). Update the root file first,
# copy it here byte for byte, then set this to `sha256sum` of the root file.
ROOT_CONTRACT_SHA256 = "3a90bebd161593684e67899e3e2f91d4daaf07a0046bfc30dc5c71b559d3f23c"


def test_copy_is_the_pinned_root_contract():
    assert hashlib.sha256(CONTRACT.read_bytes()).hexdigest() == ROOT_CONTRACT_SHA256, (
        "docs/contracts/catalog.http.v1.openapi.yaml differs from the pinned root contract: change the AA-Ecosys "
        "root file first, copy it here unchanged, then update ROOT_CONTRACT_SHA256")

# The operations the router serves (the /aa-content ones are AA-977, checked in test_aa977_catalog_aa_content.py).
SERVED = {
    "listTours": ("/tours", "get"),
    "getTour": ("/tours/{source_system}/{source_id}", "get"),
    "getDestination": ("/destinations/{destination_id}", "get"),
    "listAaContent": ("/aa-content", "get"),
    "getAaContentVersion": ("/aa-content/{content_id}/versions/{version}", "get"),
}


def _operation(op_id):
    path, method = SERVED[op_id]
    op = DOC["paths"][path][method]
    assert op["operationId"] == op_id
    return op


def _deref(node):
    while isinstance(node, dict) and "$ref" in node:
        target = DOC
        for part in node["$ref"][2:].split("/"):
            target = target[part]
        node = target
    return node


def _validator(schema):
    return Draft202012Validator({**schema, "components": DOC["components"]})


def _check(op_id, resp):
    responses = _operation(op_id)["responses"]
    status = str(resp.status_code)
    assert status in responses, f"{op_id} answered {status}, the contract declares {sorted(responses)}"
    declared = _deref(responses[status])
    if status == "304":
        assert resp.content == b""
    else:
        schema = declared["content"]["application/json"]["schema"]
        errors = sorted(_validator(schema).iter_errors(resp.json()), key=str)
        assert not errors, f"{op_id} {status}: " + "; ".join(e.message for e in errors)
    for header in declared.get("headers", {}):
        assert header.lower() in resp.headers, f"{op_id} {status}: header {header} missing"
    return resp


@pytest.fixture(autouse=True)
def issuer(monkeypatch):
    monkeypatch.setenv("PUBLIC_API_BASE", "https://api.example.invalid")
    monkeypatch.delenv(m2m_auth.ENV_M2M_ISSUER, raising=False)
    monkeypatch.delenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, raising=False)
    reader.reset_gate()
    local, _ = install_m2m()
    yield local
    m2m_auth.set_m2m_verifier(None)
    reader.reset_gate()


@pytest.fixture
def client():
    return TestClient(catalog_app(FakePool(seeded())))


def _h(issuer, **claims):
    return {"Authorization": f"Bearer {issuer.token(**claims)}"}


def test_contract_copy_declares_the_served_operations_and_scope():
    assert DOC["info"]["x-aa-contract"] == "catalog.http.v1"
    assert DOC["servers"] == [{"url": "/catalog/v1"}]
    assert DOC["security"] == [{"m2m": [m2m_auth.SCOPE_CATALOG_READ]}]
    scopes = DOC["components"]["securitySchemes"]["m2m"]["flows"]["clientCredentials"]["scopes"]
    assert m2m_auth.SCOPE_CATALOG_READ in scopes
    for op_id in SERVED:
        _operation(op_id)
    for schema in DOC["components"]["schemas"].values():
        Draft202012Validator.check_schema(schema)


def test_router_paths_match_the_contract_paths():
    from api.routers.catalog_http import router

    served = {(r.path, m.lower()) for r in router.routes for m in r.methods if m != "HEAD"}
    assert served == {(f"/catalog/v1{path}", method) for path, method in SERVED.values()}


@pytest.mark.parametrize("params", [{}, {"limit": "1"}, {"country_code": "ID"},
                                    {"updated_since": "2030-01-01T00:00:00Z"}])
def test_list_tours_200(client, issuer, params):
    _check("listTours", client.get("/catalog/v1/tours", params=params, headers=_h(issuer)))


def test_list_tours_errors(client, issuer):
    _check("listTours", client.get("/catalog/v1/tours"))
    _check("listTours", client.get("/catalog/v1/tours", headers=_h(issuer, client_id=OTHER_CLIENT)))
    _check("listTours", client.get("/catalog/v1/tours", params={"limit": "99"}, headers=_h(issuer)))
    _check("listTours", TestClient(catalog_app(None)).get("/catalog/v1/tours", headers=_h(issuer)))
    etag = client.get("/catalog/v1/tours", headers=_h(issuer)).headers["etag"]
    _check("listTours", client.get("/catalog/v1/tours", headers={**_h(issuer), "If-None-Match": etag}))


def test_get_tour_every_status(client, issuer):
    ok = _check("getTour", client.get(f"/catalog/v1/tours/cis/{SID_A}", headers=_h(issuer)))
    assert ok.status_code == 200
    _check("getTour", client.get(f"/catalog/v1/tours/cis/{SID_A}",
                                 headers={**_h(issuer), "If-None-Match": ok.headers["etag"]}))
    assert _check("getTour", client.get(f"/catalog/v1/tours/cis/{SID_RETIRED}",
                                        headers=_h(issuer))).status_code == 410
    assert _check("getTour", client.get(f"/catalog/v1/tours/cis/{SID_PAUSED}",
                                        headers=_h(issuer))).status_code == 404
    assert _check("getTour", client.get("/catalog/v1/tours/cis/never", headers=_h(issuer))).status_code == 404
    _check("getTour", client.get(f"/catalog/v1/tours/cis/{SID_A}"))
    _check("getTour", client.get(f"/catalog/v1/tours/cis/{SID_A}", headers=_h(issuer, scope="x/y")))
    _check("getTour", TestClient(catalog_app(None)).get(f"/catalog/v1/tours/cis/{SID_A}", headers=_h(issuer)))


def test_get_destination_every_status(client, issuer):
    ok = _check("getDestination", client.get(f"/catalog/v1/destinations/{DEST_BALI}", headers=_h(issuer)))
    _check("getDestination", client.get(f"/catalog/v1/destinations/{DEST_HANOI}", headers=_h(issuer)))
    _check("getDestination", client.get(f"/catalog/v1/destinations/{DEST_BALI}",
                                        headers={**_h(issuer), "If-None-Match": ok.headers["etag"]}))
    _check("getDestination", client.get("/catalog/v1/destinations/nope", headers=_h(issuer)))
    _check("getDestination", client.get(f"/catalog/v1/destinations/{DEST_BALI}"))
    _check("getDestination", client.get(f"/catalog/v1/destinations/{DEST_BALI}",
                                        headers=_h(issuer, client_id=OTHER_CLIENT)))
    _check("getDestination", TestClient(catalog_app(None)).get(f"/catalog/v1/destinations/{DEST_BALI}",
                                                               headers=_h(issuer)))


def test_served_tour_never_carries_a_price_or_person_field(client, issuer):
    body = client.get(f"/catalog/v1/tours/cis/{SID_A}", headers=_h(issuer)).json()
    text = str(body).lower()
    for forbidden in ("b2b_price", "supplier_net", "price", "email", "phone"):
        assert forbidden not in text


def test_openapi_doc_models_match_the_contract_and_the_served_bodies(client, issuer):
    """AA-899: the Catalog* models (documentation only, docs/api/openapi.json) name the contract's fields exactly and
    accept what the router serves, so the exported document cannot drift from catalog.http.v1."""
    from api.schemas import public_api as m

    pairs = {"TourPage": m.CatalogTourPage, "TourSummary": m.CatalogTourSummary, "Tour": m.CatalogTour,
             "TourDay": m.CatalogTourDay, "Place": m.CatalogPlace, "Exclusion": m.CatalogExclusion,
             "Facets": m.CatalogFacets, "Photo": m.CatalogPhoto, "Destination": m.CatalogDestination}
    for name, model in pairs.items():
        schema = DOC["components"]["schemas"][name]
        assert set(model.model_fields) == set(schema["properties"]), name
        required = {f for f, info in model.model_fields.items() if info.is_required()}
        assert required == set(schema.get("required", [])), name
    m.CatalogTourPage.model_validate(client.get("/catalog/v1/tours", headers=_h(issuer)).json())
    m.CatalogTour.model_validate(client.get(f"/catalog/v1/tours/cis/{SID_A}", headers=_h(issuer)).json())
    dest = client.get(f"/catalog/v1/destinations/{DEST_BALI}", headers=_h(issuer)).json()
    m.CatalogDestination.model_validate(dest)
