"""AA-1106 — GET /admin/platform/catalogue (api/routers/admin_product_catalogue.py, api/services/product_catalogue.py).
Admin only (staff_authz POLICY ops.read), the bundle read from the docs reader prefix, shape checks (bad rows dropped,
unknown sections left out), the account-ID mask, 404 / 503 mapping and the cache. The fixture is invented: this repo
is public and ships no catalogue content.
"""
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.core import staff_authz
from api.routers import admin
from api.services import docs_reader, product_catalogue

SECRET = "the-real-secret"
PATH = "/admin/platform/catalogue"
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}
KEY = "docs-reader/catalogue.json"


def _bundle(**over) -> dict:
    b = {
        "format": 1,
        "generated_at": "2026-11-14T03:00:00+00:00",
        "source": {"repo": "AA-Ecosys", "sha": "a" * 40, "ref": "master", "extra": "dropped"},
        "apps": [{"id": "cis", "name": "CIS", "features": 1}],
        "features": [
            {"id": "CIS-F001", "app": "cis", "name": "Tours", "status": "live", "surface": ["page:/admin/tours"],
             "conditions": {"settings": ["cis.a"]}, "uat": ["CIS-01"], "integrations": ["INT-001"]},
            {"name": "no id: dropped"},
            "not an object",
        ],
        "integrations": [{"id": "INT-001", "name": "Catalogue", "producer": "cis", "consumers": ["crm"]}],
        "journeys": [{"id": "J-01", "name": "Story", "steps": []}],
        "scenarios": {"CIS-01": {"story": "Tours", "status": "live"}},
        "settings": {"cis.a": {"app": "cis", "area": "content"}},
        "counts": {"features": 1},
        "secret_section": {"x": 1},
    }
    b.update(over)
    return b


class FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.calls: list[str] = []

    def get_object(self, Bucket, Key):
        self.calls.append(Key)
        if Key not in self.objects:
            err = Exception("NoSuchKey")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        value = self.objects[Key]
        if isinstance(value, Exception):
            raise value
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DOCS_READER_S3_PREFIX", raising=False)
    product_catalogue.reset_cache()
    with patch.object(admin, "ADMIN_SECRET", SECRET), patch.object(docs_reader, "_bucket", lambda: "bucket"):
        yield
    product_catalogue.reset_cache()


@pytest.fixture
def client():
    return TestClient(main.app)


def _use(fake):
    return patch.object(docs_reader, "_s3_client", lambda: fake)


def test_policy_is_admin_only():
    assert staff_authz.POLICY[("GET", PATH)] == staff_authz.OPS_READ
    for role in ("viewer", "content", "customer_success", "finance"):
        assert not staff_authz.has_perm(role, staff_authz.OPS_READ), role


def test_missing_secret_and_non_admin_roles_403_without_storage_reads(client):
    fake = FakeS3({KEY: _bundle()})
    with _use(fake):
        assert client.get(PATH, headers={"x-admin-role": "admin"}).status_code == 403
        for role in ("viewer", "content", "tenant"):
            assert client.get(PATH, headers={"X-Admin-Secret": SECRET, "x-admin-role": role}).status_code == 403
    assert fake.calls == []


def test_admin_reads_the_bundle_with_bad_rows_dropped(client):
    with _use(FakeS3({KEY: _bundle()})):
        res = client.get(PATH, headers=ADMIN)
    assert res.status_code == 200
    assert res.headers["cache-control"] == "private, no-store"
    body = res.json()
    assert [f["id"] for f in body["features"]] == ["CIS-F001"]
    assert body["features"][0]["uat"] == ["CIS-01"]
    assert body["integrations"][0]["id"] == "INT-001"
    assert body["journeys"][0]["id"] == "J-01"
    assert body["settings"] == {"cis.a": {"app": "cis", "area": "content"}}
    assert body["source"] == {"repo": "AA-Ecosys", "sha": "a" * 40, "ref": "master"}
    assert "secret_section" not in body
    assert body["cache_ttl_s"] == docs_reader.CACHE_TTL_S


def test_account_id_is_masked(client):
    b = _bundle()
    b["features"][0]["name"] = "Tours in 123456789012"
    with _use(FakeS3({KEY: b})):
        body = client.get(PATH, headers=ADMIN).json()
    assert body["features"][0]["name"] == f"Tours in {docs_reader.ACCOUNT_ID_MARKER}"
    assert "123456789012" not in json.dumps(body)


def test_missing_bundle_is_404(client):
    with _use(FakeS3({})):
        res = client.get(PATH, headers=ADMIN)
    assert res.status_code == 404
    assert "Docs sync" in res.json()["detail"]


@pytest.mark.parametrize("stored", [b"not json", {"format": 2, "features": []}, {"format": 1, "features": "x"},
                                    RuntimeError("boom")])
def test_bad_bundle_or_storage_error_is_503(client, stored):
    with _use(FakeS3({KEY: stored})):
        assert client.get(PATH, headers=ADMIN).status_code == 503


def test_bundle_is_cached_and_refresh_respects_the_floor(client):
    fake = FakeS3({KEY: _bundle()})
    with _use(fake):
        assert client.get(PATH, headers=ADMIN).status_code == 200
        assert client.get(PATH, headers=ADMIN).status_code == 200
        assert client.get(f"{PATH}?refresh=1", headers=ADMIN).status_code == 200
    assert fake.calls == [KEY]


def test_prefix_follows_the_docs_reader(monkeypatch, client):
    monkeypatch.setenv("DOCS_READER_S3_PREFIX", "other-prefix")
    fake = FakeS3({"other-prefix/catalogue.json": _bundle()})
    with _use(fake):
        assert client.get(PATH, headers=ADMIN).status_code == 200
    assert fake.calls == ["other-prefix/catalogue.json"]
