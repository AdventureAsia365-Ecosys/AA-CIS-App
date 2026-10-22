"""AA-894 scope 2 — GET /admin/data-model (api/routers/admin_data_model.py, api/services/data_model.py).

Auth (admin only via staff_authz POLICY ops.read), 404 when no model is published, 503 when storage fails or the
object is not a format-1 model, the diff vs previous.json, and the in-process cache. The fixture model is invented
(schemas "alpha" / "beta"): this repo is public and ships no real table list.
"""
from __future__ import annotations

import copy
import io
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from api import main
from api.routers import admin
from api.services import data_model

SECRET = "the-real-secret"
PATH = "/admin/data-model"
ADMIN = {"X-Admin-Secret": SECRET, "x-admin-role": "admin"}


def _model(fingerprint="f" * 64, extra_column=False, drop_table=False, new_fk=False):
    orders_cols = [
        {"name": "id", "type": "uuid", "not_null": True, "pk": True, "fk": False, "uk": False},
        {"name": "customer_id", "type": "uuid", "not_null": True, "pk": False, "fk": True, "uk": False},
        {"name": "email", "type": "text", "not_null": False, "pk": False, "fk": False, "uk": False,
         "tag": "pii:contact"},
    ]
    if extra_column:
        orders_cols.append({"name": "note", "type": "text", "not_null": False, "pk": False, "fk": False, "uk": False})
        orders_cols[2] = dict(orders_cols[2], type="varchar(320)")
    fks = [{"name": "orders_customer_fk", "columns": ["customer_id"], "ref_schema": "alpha",
            "ref_table": "customers", "ref_columns": ["id"], "validated": True}]
    edges = [{"name": "orders_customer_fk", "from": "alpha.orders", "from_columns": ["customer_id"],
              "to": "alpha.customers", "to_columns": ["id"], "validated": True, "cross_schema": False,
              "cross_owner": False}]
    if new_fk:
        fks.append({"name": "orders_item_fk", "columns": ["id"], "ref_schema": "beta", "ref_table": "items",
                    "ref_columns": ["id"], "validated": True})
        edges.append({"name": "orders_item_fk", "from": "alpha.orders", "from_columns": ["id"], "to": "beta.items",
                      "to_columns": ["id"], "validated": True, "cross_schema": True, "cross_owner": True})
    alpha_tables = [
        {"name": "customers", "kind": "table", "owner_role": "o", "rls": False, "rls_forced": False,
         "is_partition": False, "primary_key": ["id"], "unique_keys": [],
         "columns": [{"name": "id", "type": "uuid", "not_null": True, "pk": True, "fk": False, "uk": False}],
         "foreign_keys": []},
        {"name": "orders", "kind": "table", "owner_role": "o", "rls": True, "rls_forced": False,
         "is_partition": False, "primary_key": ["id"], "unique_keys": [], "columns": orders_cols,
         "foreign_keys": fks},
    ]
    beta_tables = [] if drop_table else [
        {"name": "items", "kind": "table", "owner_role": "b", "rls": False, "rls_forced": False,
         "is_partition": False, "primary_key": ["id"], "unique_keys": [],
         "columns": [{"name": "id", "type": "bigint", "not_null": True, "pk": True, "fk": False, "uk": False}],
         "foreign_keys": []},
    ]
    return {"format": 1, "fingerprint": fingerprint, "generated_at": "2026-10-22T03:50:00+00:00",
            "migrations_latest_per_owner": {"cis": "001"},
            "schemas": [{"schema": "alpha", "owner_app": "cis", "owner_role": "o", "tables": alpha_tables},
                        {"schema": "beta", "owner_app": "booking", "owner_role": "b", "tables": beta_tables}],
            "edges": edges}


class _ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """get_object over a dict of key -> dict (JSON), bytes, or an exception to raise."""

    def __init__(self, objects):
        self.objects = objects
        self.calls: list[str] = []

    def get_object(self, Bucket, Key):
        self.calls.append(Key)
        if Key not in self.objects:
            raise _ClientError("NoSuchKey")
        value = self.objects[Key]
        if isinstance(value, Exception):
            raise value
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}


LATEST = "data-model/erd/latest.json"
PREVIOUS = "data-model/erd/previous.json"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("DATA_MODEL_S3_PREFIX", raising=False)
    data_model.reset_cache()
    with patch.object(admin, "ADMIN_SECRET", SECRET):
        yield
    data_model.reset_cache()


@pytest.fixture
def client():
    return TestClient(main.app)   # no lifespan: the route needs no DB / Redis


def _use(fake):
    return patch.object(data_model, "_s3_client", lambda: fake)


# ── auth ────────────────────────────────────────────────────────────────────────────────────────────

def test_policy_is_admin_only():
    from api.core import staff_authz

    assert staff_authz.POLICY[("GET", PATH)] == staff_authz.OPS_READ
    assert staff_authz.has_perm("admin", staff_authz.OPS_READ)
    for role in ("viewer", "reviewer", "content"):
        assert not staff_authz.has_perm(role, staff_authz.OPS_READ), role


def test_missing_or_wrong_secret_403(client):
    fake = FakeS3({LATEST: _model()})
    with _use(fake):
        assert client.get(PATH, headers={"x-admin-role": "admin"}).status_code == 403
        assert client.get(PATH, headers={"X-Admin-Secret": "nope", "x-admin-role": "admin"}).status_code == 403
    assert fake.calls == []


@pytest.mark.parametrize("role", ["viewer", "reviewer", "content", "tenant"])
def test_non_admin_roles_403(client, role):
    fake = FakeS3({LATEST: _model()})
    with _use(fake):
        res = client.get(PATH, headers={"X-Admin-Secret": SECRET, "x-admin-role": role})
    assert res.status_code == 403
    assert fake.calls == [], "a refused caller must not trigger a storage read"


def test_machine_caller_403(client):
    """The secret without a role header is the machine principal: not allowed on a POLICY route."""
    with _use(FakeS3({LATEST: _model()})):
        assert client.get(PATH, headers={"X-Admin-Secret": SECRET}).status_code == 403


def test_admin_gets_the_model(client):
    model = _model()
    with _use(FakeS3({LATEST: model})):
        res = client.get(PATH, headers=ADMIN)
    assert res.status_code == 200
    body = res.json()
    assert body["model"] == model
    assert body["previous"] is None and body["diff"] is None
    assert body["cache_ttl_s"] == data_model.CACHE_TTL_S
    assert res.headers["cache-control"] == "private, no-store"


# ── missing / unavailable ───────────────────────────────────────────────────────────────────────────

def test_latest_missing_404(client):
    with _use(FakeS3({})):
        res = client.get(PATH, headers=ADMIN)
    assert res.status_code == 404
    assert "No data model" in res.json()["detail"]


@pytest.mark.parametrize("failure", [_ClientError("AccessDenied"), RuntimeError("network down")])
def test_storage_failure_503(client, failure):
    with _use(FakeS3({LATEST: failure})):
        res = client.get(PATH, headers=ADMIN)
    assert res.status_code == 503
    assert "network" not in res.json()["detail"], "the raw error stays in the log"


@pytest.mark.parametrize("payload", [b"not json", json.dumps([1, 2]).encode(),
                                     json.dumps({"format": 2, "schemas": [], "edges": []}).encode()])
def test_bad_object_503(client, payload):
    with _use(FakeS3({LATEST: payload})):
        assert client.get(PATH, headers=ADMIN).status_code == 503


def test_unreadable_previous_only_drops_the_diff(client):
    with _use(FakeS3({LATEST: _model(), PREVIOUS: _ClientError("AccessDenied")})):
        res = client.get(PATH, headers=ADMIN)
    assert res.status_code == 200
    assert res.json()["diff"] is None


def test_prefix_from_env(client, monkeypatch):
    monkeypatch.setenv("DATA_MODEL_S3_PREFIX", "/other/erd/")
    fake = FakeS3({"other/erd/latest.json": _model()})
    with _use(fake):
        assert client.get(PATH, headers=ADMIN).status_code == 200
    assert fake.calls[0] == "other/erd/latest.json"


# ── diff ────────────────────────────────────────────────────────────────────────────────────────────

def test_diff_vs_previous(client):
    previous = _model(fingerprint="a" * 64)
    latest = _model(fingerprint="b" * 64, extra_column=True, drop_table=True, new_fk=True)
    with _use(FakeS3({LATEST: latest, PREVIOUS: previous})):
        body = client.get(PATH, headers=ADMIN).json()
    assert body["previous"]["fingerprint"] == "a" * 64
    diff = body["diff"]
    assert diff["tables_added"] == []
    assert diff["tables_removed"] == ["beta.items"]
    orders = diff["tables_changed"]["alpha.orders"]
    assert orders["columns_added"] == ["note"]
    assert orders["columns_changed"] == [{"name": "email", "changes": ["type text -> varchar(320)"]}]
    assert orders["fks_added"] == ["orders_item_fk"]
    assert "alpha.customers" not in diff["tables_changed"]
    assert diff["edges_added"] == ["alpha.orders:orders_item_fk"]
    assert diff["edges_removed"] == []


def test_same_fingerprint_means_no_diff(client):
    with _use(FakeS3({LATEST: _model(), PREVIOUS: _model()})):
        body = client.get(PATH, headers=ADMIN).json()
    assert body["previous"] is None and body["diff"] is None


def test_diff_models_tag_nullability_keys_and_rls():
    old = _model()
    new = copy.deepcopy(old)
    orders = new["schemas"][0]["tables"][1]
    orders["rls_forced"] = True
    orders["primary_key"] = ["id", "customer_id"]
    email = orders["columns"][2]
    email.pop("tag")
    email["not_null"] = True
    email["uk"] = True
    d = data_model.diff_models(old, new)["tables_changed"]["alpha.orders"]
    assert d["columns_changed"] == [{"name": "email", "changes": ["NOT NULL", "tag pii:contact -> none", "UK added"]}]
    assert d["primary_key"] == {"from": ["id"], "to": ["id", "customer_id"]}
    assert d["rls"] == {"from": [True, False], "to": [True, True]}


# ── cache ───────────────────────────────────────────────────────────────────────────────────────────

def test_cached_between_calls_and_refresh_is_rate_limited(client):
    fake = FakeS3({LATEST: _model()})
    with _use(fake):
        assert client.get(PATH, headers=ADMIN).status_code == 200
        reads = len(fake.calls)
        assert client.get(PATH, headers=ADMIN).status_code == 200
        assert client.get(PATH + "?refresh=1", headers=ADMIN).status_code == 200   # younger than MIN_REFRESH_S
        assert len(fake.calls) == reads
        # Age the cache past MIN_REFRESH_S: refresh re-reads, a plain call still does not.
        data_model._cache["at"] -= data_model.MIN_REFRESH_S + 1
        assert client.get(PATH, headers=ADMIN).status_code == 200
        assert len(fake.calls) == reads
        assert client.get(PATH + "?refresh=1", headers=ADMIN).status_code == 200
        assert len(fake.calls) == 2 * reads
        data_model._cache["at"] -= data_model.CACHE_TTL_S + 1
        assert client.get(PATH, headers=ADMIN).status_code == 200
        assert len(fake.calls) == 3 * reads


def test_errors_are_not_cached(client):
    fake = FakeS3({})
    with _use(fake):
        assert client.get(PATH, headers=ADMIN).status_code == 404
        fake.objects[LATEST] = _model()
        assert client.get(PATH, headers=ADMIN).status_code == 200
