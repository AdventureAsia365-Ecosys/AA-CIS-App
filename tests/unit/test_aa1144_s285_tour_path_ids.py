"""AA-1144 follow-up (S285 live check): GET /v1/tours/mine answered 500 — "mine" reached the ``/{tour_id}`` catch-all
and the ``$1::uuid`` cast failed in Postgres. Every path id of the tenant tours router that is not a UUID is now a 404
before any database work; a UUID id is passed through unchanged."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.routers import v1_tours

TENANT = {"sub": "00000000-0000-4000-8000-0000000000a1", "mid": "00000000-0000-4000-8000-0000000000b2"}


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(v1_tours.router)
    app.dependency_overrides[v1_tours.get_tenant] = lambda: TENANT
    app.state.pool = MagicMock(side_effect=AssertionError("no database work for a bad id"))
    app.state.pool.acquire = MagicMock(side_effect=AssertionError("no database work for a bad id"))
    return TestClient(app, raise_server_exceptions=True)


@pytest.mark.parametrize("method,path,detail", [
    ("GET", "/v1/tours/mine", "Tour not found"),
    ("GET", "/v1/tours/versions/mine", "Version not found"),
    ("GET", "/v1/tours/versions/not-a-uuid/export-docx", "Version not found"),
    ("POST", "/v1/tours/versions/not-a-uuid/retry", "Version not found"),
], ids=["tour-mine", "version-mine", "export", "retry"])
def test_a_path_id_that_is_not_a_uuid_is_404(client, method, path, detail):
    r = client.request(method, path)
    assert r.status_code == 404, r.text
    assert r.json() == {"detail": detail}


def test_patch_version_with_a_bad_id_is_404(client):
    r = client.patch("/v1/tours/versions/mine", json={"action": "approve"})
    assert r.status_code == 404 and r.json() == {"detail": "Version not found"}


def test_rewrite_with_a_bad_tour_id_is_404(client):
    r = client.post("/v1/tours/pool/mine/rewrite", json={})
    assert r.status_code == 404, r.text
    assert r.json() == {"detail": "Tour not found"}


def test_full_without_credentials_stays_401(client):
    assert client.get("/v1/tours/mine/full").status_code == 401


@pytest.mark.parametrize("value", ["00000000-0000-4000-8000-0000000000c3", "00000000000040008000000000000000"])
def test_require_uuid_returns_the_value_unchanged(value):
    assert v1_tours._require_uuid(value, "x") == value


@pytest.mark.parametrize("value", ["mine", "", "12345", "00000000-0000-4000-8000-0000000000c3x"])
def test_require_uuid_refuses(value):
    with pytest.raises(HTTPException) as exc:
        v1_tours._require_uuid(value, "Tour not found")
    assert exc.value.status_code == 404 and exc.value.detail == "Tour not found"
