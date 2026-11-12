"""AA-1028 R1 — /tenant/v1 conforms to the contract tenant.entitlements.v1 (OpenAPI 3.1, JSON Schema 2020-12).

``docs/contracts/tenant.entitlements.v1.openapi.yaml`` is a byte-identical copy of the root contract (AA-Ecosys
``docs/contracts/``, the source of truth, checked there by ``tools/contracts/check_contracts.py``). This repo cannot
see the root repo in CI, so :data:`ROOT_CONTRACT_SHA256` pins the root file's sha256.

Every scenario calls the real router (a scripted fake connection, a local M2M issuer) and validates the answer
against the schema the contract declares (``additionalProperties: false`` everywhere, so an extra field fails). No
response may carry a bill-to field, a price, a billing reference, a reason or an actor (the root checker's policy,
run here on the real bodies). Also: the auth outcomes (401 / 403 / 503), the snapshot ETag and 304, in_service, the
feed cursor, limits and the change bodies, 503 before CIS 353-355 are applied.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import asyncpg
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from api.core import m2m_auth
from api.routers.tenant_account import router
from services.tenant_account import feed, reader
from tests.unit.aa976_m2m import OTHER_CLIENT, install_m2m

CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "tenant.entitlements.v1.openapi.yaml"
DOC = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
# sha256 of AA-Ecosys docs/contracts/tenant.entitlements.v1.openapi.yaml (info.version 1.5, AA-1038 R5 slice 3).
# Update the root file first, copy it here byte for byte, then set this to `sha256sum` of the root file.
ROOT_CONTRACT_SHA256 = "c92808038ee70e8193772ec2292bd1b829f6868e912cc365f4539cb365afe9b0"

# Fixed UUIDs (AA-993: parametrize IDs must not change between pytest-xdist workers).
TENANT = "1028a000-0000-4000-8000-000000000001"
UNKNOWN_TENANT = "1028a000-0000-4000-8000-0000000000ff"
T0 = datetime(2026, 11, 3, 10, 0, 0, 123456, tzinfo=timezone.utc)
FORBIDDEN_KEYS = {"billing_email", "legal_entity_name", "tax_id", "billing_address", "billing_country", "billing_ref",
                  "paid_through", "price_usd_monthly", "price_extra_seat_usd", "actor", "email", "updated_by",
                  "granted_by", "reason"}


def _ent(key, product, kind, unit, limit, source="plan", expires=None) -> dict:
    return {"feature_key": key, "product": product, "value_kind": kind, "unit": unit, "limit_text": limit,
            "source": source, "expires_at": expires}


class FakeConn:
    """Answers the reader / feed SQL from in-memory rows."""

    def __init__(self) -> None:
        self.tenant: Optional[dict] = {"tenant_id": uuid.UUID(TENANT), "account_status": "active",
                                        "suspend_reason": None, "kind": "agency", "is_pilot": False,
                                        "data_region": "us", "time_zone": "Asia/Ho_Chi_Minh"}
        self.subscriptions = [{"product": "content", "plan_key": "content.growth", "status": "active",
                               "trial_ends_at": None, "seats": None},
                              {"product": "crm", "plan_key": "crm.core", "status": "trialing", "trial_ends_at": T0,
                               "seats": 3}]
        self.entitlements = [_ent("ai.budget_usd_monthly", "platform", "limit", "usd_month", "40.00"),
                             _ent("content.social", "content", "flag", None, None),
                             _ent("crm.seats", "crm", "seats", "seats", "3.00", "trial", T0)]
        self.events: list[dict] = []
        self.fail: Optional[BaseException] = None
        self.feed_args: list[tuple] = []

    def transaction(self, readonly: bool = False):
        conn = self

        class _Tx:
            async def __aenter__(self):
                assert readonly
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Tx()

    async def fetchrow(self, sql: str, *args: Any):
        if self.fail:
            raise self.fail
        assert sql == reader.ACCOUNT_SQL
        return self.tenant if self.tenant and str(self.tenant["tenant_id"]) == args[0] else None

    async def fetch(self, sql: str, *args: Any):
        if self.fail:
            raise self.fail
        if sql == reader.SUBSCRIPTIONS_SQL:
            return self.subscriptions
        if sql == reader.ENTITLEMENTS_SQL:
            return self.entitlements
        assert sql == feed.FEED_SQL
        self.feed_args.append(args)
        txid, seq, limit = int(args[0]), args[1], args[2]
        rows = sorted((e for e in self.events if (int(e["txid_text"]), e["seq"]) > (txid, seq)),
                      key=lambda e: (int(e["txid_text"]), e["seq"]))
        return rows[:limit]


class FakePool:
    def __init__(self, conn: FakeConn, fail: Optional[BaseException] = None) -> None:
        self.conn, self.fail, self.released = conn, fail, 0

    async def acquire(self, timeout: Optional[float] = None):
        if self.fail:
            raise self.fail
        return self.conn

    async def release(self, conn: Any) -> None:
        self.released += 1


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(m2m_auth.ENV_M2M_ISSUER, raising=False)
    monkeypatch.delenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, raising=False)
    yield
    m2m_auth.set_m2m_verifier(None)


@pytest.fixture
def issuer():
    issuer, _ = install_m2m()
    return issuer


@pytest.fixture
def conn():
    return FakeConn()


@pytest.fixture
def pool(conn):
    return FakePool(conn)


@pytest.fixture
def client(pool):
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(m2m_auth.M2MProblem, m2m_auth.m2m_problem_handler)
    app.state.pool = pool
    return TestClient(app)


def _auth(issuer, scope=m2m_auth.SCOPE_TENANT_READ, **claims) -> dict:
    return {"Authorization": f"Bearer {issuer.token(scope=scope, **claims)}"}


def _schema(name: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": DOC["components"]})


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | set().union(*(_keys(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_keys(v) for v in node)) if node else set()
    return set()


def _valid(name: str, body: Any) -> None:
    errors = sorted(_schema(name).iter_errors(body), key=str)
    assert not errors, [e.message for e in errors]
    assert not _keys(body) & FORBIDDEN_KEYS


def _account(client, issuer, **headers):
    return client.get(f"/tenant/v1/tenants/{TENANT}/account", headers={**_auth(issuer), **headers})


def test_copy_is_the_pinned_root_contract():
    assert hashlib.sha256(CONTRACT.read_bytes()).hexdigest() == ROOT_CONTRACT_SHA256, (
        "docs/contracts/tenant.entitlements.v1.openapi.yaml differs from the pinned root contract: change the "
        "AA-Ecosys root file first, copy it here unchanged, then update ROOT_CONTRACT_SHA256")


def test_the_router_serves_exactly_the_contract_paths():
    served = {route.path.removeprefix("/tenant/v1") for route in router.routes}
    assert served == set(DOC["paths"])
    assert DOC["servers"] == [{"url": "/tenant/v1"}]
    assert set(DOC["components"]["securitySchemes"]["m2m"]["flows"]["clientCredentials"]["scopes"]) == {
        m2m_auth.SCOPE_TENANT_READ}


def test_the_feed_change_bodies_match_the_migration_triggers():
    """The keys CIS 353 / 354 put into tenant_account_event.change are exactly the contract's change schemas."""
    schemas = DOC["components"]["schemas"]
    assert set(schemas["StatusChange"]["required"]) == {"status", "previous_status", "suspend_reason"}
    assert set(schemas["SubscriptionChange"]["required"]) == {"op", "product", "plan_key", "status", "previous_status",
                                                              "previous_plan_key", "seats", "trial_ends_at"}
    assert set(schemas["EntitlementChange"]["required"]) == {"op", "feature_key", "product", "limit", "source",
                                                             "expires_at"}
    migrations = Path(__file__).resolve().parents[2] / "api" / "migrations"
    sql = (migrations / "353_tenant_control_plane_tables.sql").read_text() + \
        (migrations / "354_tenant_account_columns.sql").read_text()
    for schema in ("StatusChange", "SubscriptionChange", "EntitlementChange"):
        for key in schemas[schema]["required"]:
            assert f"'{key}'," in sql, f"{schema}.{key} is not built by a CIS 353 / 354 trigger"


# ── auth ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/account-events", f"/tenants/{TENANT}/account"])
def test_no_token_is_401(client, issuer, path):
    r = client.get(f"/tenant/v1{path}")
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert r.headers["WWW-Authenticate"] == 'Bearer realm="tenant.entitlements.v1"'
    _valid("Problem", r.json())


def test_a_catalog_only_token_is_403(client, issuer):
    r = client.get("/tenant/v1/account-events", headers=_auth(issuer, scope=m2m_auth.SCOPE_CATALOG_READ))
    assert r.status_code == 403 and r.json() == {"error": "forbidden",
                                                 "message": "The token lacks the scope cis.tenant/read."}


def test_another_client_is_403(client, issuer):
    r = client.get("/tenant/v1/account-events", headers=_auth(issuer, client_id=OTHER_CLIENT, sub=OTHER_CLIENT))
    assert r.status_code == 403 and r.json()["message"] == "This client may not read tenant accounts."


def test_invalid_m2m_config_is_503(client, issuer, monkeypatch):
    m2m_auth.set_m2m_verifier(None)
    monkeypatch.setenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, "bad id")
    r = client.get("/tenant/v1/account-events", headers=_auth(issuer))
    assert r.status_code == 503 and r.json()["error"] == "unavailable"


# ── snapshot ────────────────────────────────────────────────────────────────

def test_snapshot_conforms(client, issuer, pool):
    r = _account(client, issuer)
    assert r.status_code == 200
    body = r.json()
    _valid("TenantAccount", body)
    assert body["in_service"] is True and body["kind"] == "agency" and body["data_region"] == "us"
    assert [s["product"] for s in body["subscriptions"]] == ["content", "crm"]
    assert body["subscriptions"][1]["trial_ends_at"] == "2026-11-03T10:00:00.123456Z"
    budget = next(e for e in body["entitlements"] if e["feature_key"] == "ai.budget_usd_monthly")
    assert budget == {"feature_key": "ai.budget_usd_monthly", "product": "platform", "kind": "limit",
                      "unit": "usd_month", "limit": "40.00", "source": "plan", "expires_at": None}
    assert r.headers["Cache-Control"] == "private, no-cache" and r.headers["Vary"] == "Authorization"
    assert pool.released == 1


@pytest.mark.parametrize("status, in_service", [("trial", True), ("active", True), ("past_due", True),
                                                ("lead", False), ("applied", False), ("suspended", False),
                                                ("offboarding", False), ("closed", False)])
def test_in_service_follows_the_account_status(client, issuer, conn, status, in_service):
    conn.tenant["account_status"] = status
    if status == "suspended":
        conn.tenant["suspend_reason"] = "billing"
    body = _account(client, issuer).json()
    _valid("TenantAccount", body)
    assert body["in_service"] is in_service


def test_snapshot_etag_ignores_as_of_and_answers_304(client, issuer, conn):
    etag = _account(client, issuer).headers["ETag"]
    again = _account(client, issuer, **{"If-None-Match": etag})
    assert again.status_code == 304 and again.content == b"" and again.headers["ETag"] == etag
    assert _account(client, issuer, **{"If-None-Match": "*"}).status_code == 304
    conn.entitlements[0]["limit_text"] = "80.00"
    changed = _account(client, issuer, **{"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["ETag"] != etag


@pytest.mark.parametrize("tenant", [UNKNOWN_TENANT, "not-a-uuid"])
def test_unknown_tenant_is_404(client, issuer, tenant):
    r = client.get(f"/tenant/v1/tenants/{tenant}/account", headers=_auth(issuer))
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    _valid("Problem", r.json())


@pytest.mark.parametrize("error", [asyncpg.UndefinedTableError("missing"), asyncpg.UndefinedColumnError("missing"),
                                   asyncpg.UndefinedFunctionError("missing")])
def test_before_the_migrations_is_503(client, issuer, conn, error):
    conn.fail = error
    r = _account(client, issuer)
    assert r.status_code == 503 and r.json()["error"] == "unavailable" and r.headers["Retry-After"] == "5"
    _valid("Problem", r.json())


def test_no_pool_is_503(issuer):
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(m2m_auth.M2MProblem, m2m_auth.m2m_problem_handler)
    r = TestClient(app).get(f"/tenant/v1/tenants/{TENANT}/account", headers=_auth(issuer))
    assert r.status_code == 503


# ── feed ────────────────────────────────────────────────────────────────────

def _event(seq: int, txid: int, type_: str, change: dict) -> dict:
    return {"seq": seq, "txid_text": str(txid), "event_id": uuid.uuid4(), "type": type_,
            "tenant_id": uuid.UUID(TENANT), "change": json.dumps(change), "occurred_at": T0}


STATUS = {"status": "suspended", "previous_status": "active", "suspend_reason": "billing"}
SUB = {"op": "created", "product": "content", "plan_key": "content.starter", "status": "active",
       "previous_status": None, "previous_plan_key": None, "seats": None, "trial_ends_at": None}
ENT = {"op": "added", "feature_key": "ai.budget_usd_monthly", "product": "platform", "limit": "15.00",
       "source": "plan", "expires_at": None}


def test_feed_pages_conform_and_follow_the_cursor(client, issuer, conn):
    conn.events = [_event(1, 100, "account.status_changed", STATUS), _event(2, 101, "subscription.changed", SUB),
                   _event(3, 101, "entitlement.changed", ENT)]
    r = client.get("/tenant/v1/account-events", params={"limit": "2"}, headers=_auth(issuer))
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    page = r.json()
    _valid("AccountEventPage", page)
    assert [e["type"] for e in page["items"]] == ["account.status_changed", "subscription.changed"]
    assert page["items"][0]["change"] == STATUS
    nxt = client.get("/tenant/v1/account-events", params={"after": page["next_cursor"]}, headers=_auth(issuer)).json()
    _valid("AccountEventPage", nxt)
    assert [e["change"] for e in nxt["items"]] == [ENT]
    empty = client.get("/tenant/v1/account-events", params={"after": nxt["next_cursor"]}, headers=_auth(issuer)).json()
    assert empty == {"items": [], "next_cursor": nxt["next_cursor"]}
    assert conn.feed_args[0] == ("0", 0, 2)


def test_empty_feed_from_the_start_returns_the_start_cursor(client, issuer):
    body = client.get("/tenant/v1/account-events", headers=_auth(issuer)).json()
    assert body == {"items": [], "next_cursor": feed.encode_cursor(0, 0)}


@pytest.mark.parametrize("params", [{"limit": "0"}, {"limit": "501"}, {"limit": "x"}, {"after": "not-a-cursor"}])
def test_feed_refuses_bad_parameters(client, issuer, params):
    r = client.get("/tenant/v1/account-events", params=params, headers=_auth(issuer))
    assert r.status_code == 400 and r.json()["error"] == "invalid"
    _valid("Problem", r.json())


def test_a_change_body_of_two_shapes_is_refused_by_the_contract():
    bad = {"event_id": str(uuid.uuid4()), "cursor": "c", "type": "entitlement.changed",
           "occurred_at": "2026-11-03T00:00:00Z", "tenant_id": TENANT, "change": {**ENT, "status": "active"}}
    assert list(_schema("AccountEvent").iter_errors(bad))
    assert list(_schema("Entitlement").iter_errors({"feature_key": "a.b", "product": "crm", "kind": "limit",
                                                    "unit": "count_month", "limit": 3, "source": "plan",
                                                    "expires_at": None}))


def test_the_feed_never_selects_the_actor():
    assert "actor" not in feed.FEED_SQL
    for sql in (reader.ACCOUNT_SQL, reader.SUBSCRIPTIONS_SQL, reader.ENTITLEMENTS_SQL):
        assert not {"billing", "price", "tax_id", "legal_entity", "reason ", "actor"} & set(sql.split()), sql
        assert "billing_" not in sql and "price" not in sql and "legal_entity" not in sql
