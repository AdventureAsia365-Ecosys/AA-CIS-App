"""AA-978 — /tenant/membership/v1 conforms to the contract tenant.membership.v1 (OpenAPI 3.1, JSON Schema 2020-12).

``docs/contracts/tenant.membership.v1.openapi.yaml`` is a byte-identical copy of the root contract (AA-Ecosys
``docs/contracts/``, the source of truth, checked there by ``tools/contracts/check_contracts.py``). This repo cannot
see the root repo in CI, so :data:`ROOT_CONTRACT_SHA256` pins the root file's sha256.

Every scenario calls the real router (a scripted fake connection, a local M2M issuer) and validates the answer
against the schema the contract declares (``additionalProperties: false`` everywhere, so an extra field fails). No
response may carry ``crm_role``, ``role``, ``email`` or ``display_name`` (the root checker's policy for this contract,
run here on the real bodies). Also: the auth outcomes (401 / 403 for a ``cis.catalog/read``-only token or another
client / 503), the snapshot ETag and 304, the feed cursor and limits, 503 before CIS 276 is applied.
"""
from __future__ import annotations

import base64
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
from api.routers.tenant_membership import router
from services.tenant_membership import feed, reader
from tests.unit.aa976_m2m import CRM, OTHER_CLIENT, install_m2m

CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "tenant.membership.v1.openapi.yaml"
DOC = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
# sha256 of AA-Ecosys docs/contracts/tenant.membership.v1.openapi.yaml (info.version 1.2, AA-979:
# TenantMembers.is_synthetic for the CRM's A10b check; 1.1 added activateInvitation).
# Update the root file first, copy it here byte for byte, then set this to `sha256sum` of the root file.
ROOT_CONTRACT_SHA256 = "1a2d5fc97bfae930fc92ff6e2fa44ae921a396b18325458aeaef9b9fd9807145"

# AA-993: fixed UUIDs. TENANT and UNKNOWN_TENANT appear in parametrize IDs; a uuid4() there gives a different test ID
# on every collection, and pytest-xdist refuses to run when its workers collect different IDs.
TENANT = "978a0000-0000-4000-8000-000000000001"
UNKNOWN_TENANT = "978a0000-0000-4000-8000-0000000000ff"
ISS = "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_zz978partner"
T0 = datetime(2026, 10, 24, 10, 0, 0, 123456, tzinfo=timezone.utc)
FORBIDDEN_KEYS = {"crm_role", "role", "email", "display_name"}
TENANT_SCOPE = f"{m2m_auth.SCOPE_TENANT_READ} {m2m_auth.SCOPE_CATALOG_READ}"


def _member(status="active", role="owner", sub="sub-1", invitation=None) -> dict:
    return {"membership_id": uuid.uuid4(), "tenant_id": uuid.UUID(TENANT),
            "cognito_iss": None if status == "invited" else ISS,
            "cognito_sub": None if status == "invited" else sub, "content_studio_role": role, "status": status,
            "invitation_id": invitation, "updated_at": T0}


class FakeConn:
    """Answers the reader / feed SQL from in-memory rows."""

    def __init__(self) -> None:
        self.tenant: Optional[dict] = {"tenant_id": uuid.UUID(TENANT), "is_active": True, "cancelled_at": None,
                                        "is_canary": False}
        self.members = [_member(), _member(status="invited", role="editor", invitation=uuid.uuid4())]
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
        assert sql == reader.TENANT_SQL
        return self.tenant if self.tenant and str(self.tenant["tenant_id"]) == args[0] else None

    async def fetch(self, sql: str, *args: Any):
        if self.fail:
            raise self.fail
        if sql == reader.TENANT_MEMBERS_SQL:
            return [m for m in self.members if str(m["tenant_id"]) == args[0] and m["status"] != "removed"]
        if sql == reader.IDENTITY_SQL:
            return [m for m in self.members
                    if (m["cognito_iss"], m["cognito_sub"]) == args and m["status"] != "removed"]
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
def client(conn):
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(m2m_auth.M2MProblem, m2m_auth.m2m_problem_handler)
    app.state.pool = FakePool(conn)
    return TestClient(app)


def _auth(issuer, scope=TENANT_SCOPE, **claims) -> dict:
    return {"Authorization": f"Bearer {issuer.token(scope=scope, **claims)}"}


def _schema(name: str) -> Draft202012Validator:
    schema = {"$ref": f"#/components/schemas/{name}", "components": DOC["components"]}
    return Draft202012Validator(schema)


def _valid(name: str, body: Any) -> None:
    errors = sorted(_schema(name).iter_errors(body), key=str)
    assert not errors, [e.message for e in errors]
    assert not _keys(body) & FORBIDDEN_KEYS


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | set().union(*(_keys(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_keys(v) for v in node)) if node else set()
    return set()


def _event(seq: int, txid: int, type_: str, membership: Optional[dict] = None) -> dict:
    return {"seq": seq, "txid_text": str(txid), "event_id": uuid.uuid4(), "type": type_, "tenant_id": uuid.UUID(TENANT),
            "membership": membership, "occurred_at": T0}


def test_copy_is_the_pinned_root_contract():
    assert hashlib.sha256(CONTRACT.read_bytes()).hexdigest() == ROOT_CONTRACT_SHA256, (
        "docs/contracts/tenant.membership.v1.openapi.yaml differs from the pinned root contract: change the AA-Ecosys "
        "root file first, copy it here unchanged, then update ROOT_CONTRACT_SHA256")


def test_the_router_serves_exactly_the_contract_paths():
    served = {route.path.removeprefix("/tenant/membership/v1") for route in router.routes}
    assert served == set(DOC["paths"])
    assert DOC["servers"] == [{"url": "/tenant/membership/v1"}]
    assert DOC["components"]["securitySchemes"]["m2m"]["flows"]["clientCredentials"]["scopes"] == {
        m2m_auth.SCOPE_TENANT_READ: "Read tenants and memberships",
        m2m_auth.SCOPE_TENANT_ACTIVATE: "Bind an accepted invitation's partner-pool identity (1.1)"}


# ── auth ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/events", f"/tenants/{TENANT}/members", f"/memberships?iss={ISS}&sub=s"])
def test_no_token_is_401(client, issuer, path):
    r = client.get(f"/tenant/membership/v1{path}")
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert r.headers["WWW-Authenticate"] == 'Bearer realm="tenant.membership.v1"'
    _valid("Problem", r.json())


def test_a_catalog_only_token_is_403(client, issuer):
    r = client.get("/tenant/membership/v1/events", headers=_auth(issuer, scope=m2m_auth.SCOPE_CATALOG_READ))
    assert r.status_code == 403 and r.json() == {"error": "forbidden",
                                                 "message": "The token lacks the scope cis.tenant/read."}


def test_another_client_is_403(client, issuer):
    r = client.get("/tenant/membership/v1/events", headers=_auth(issuer, client_id=OTHER_CLIENT, sub=OTHER_CLIENT))
    assert r.status_code == 403
    assert r.json()["message"] == "This client may not read tenant memberships."


def test_a_bad_token_is_401(client, issuer):
    r = client.get("/tenant/membership/v1/events", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


def test_invalid_m2m_config_is_503(client, issuer, monkeypatch):
    m2m_auth.set_m2m_verifier(None)
    monkeypatch.setenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, "bad id")
    r = client.get("/tenant/membership/v1/events", headers=_auth(issuer))
    assert r.status_code == 503 and r.json()["error"] == "unavailable"


def test_the_catalog_routes_still_refuse_a_tenant_only_token(issuer):
    """The refactor into require_m2m(scope): each route class checks its own scope."""
    from tests.unit.aa976_catalog_fake import FakePool as CatalogPool, catalog_app, seeded
    c = TestClient(catalog_app(CatalogPool(seeded())))
    r = c.get("/catalog/v1/tours", headers=_auth(issuer, scope=m2m_auth.SCOPE_TENANT_READ))
    assert r.status_code == 403 and r.json()["message"] == "The token lacks the scope cis.catalog/read."
    assert c.get("/catalog/v1/tours").headers["WWW-Authenticate"] == 'Bearer realm="catalog.http.v1"'


# ── snapshot ────────────────────────────────────────────────────────────────

def test_snapshot_conforms_and_lists_invited_and_active_only(client, issuer, conn):
    conn.members.append(_member(status="removed", sub="gone"))
    r = client.get(f"/tenant/membership/v1/tenants/{TENANT}/members", headers=_auth(issuer))
    assert r.status_code == 200
    body = r.json()
    _valid("TenantMembers", body)
    assert body["tenant_state"] == "active"
    assert body["is_synthetic"] is False
    assert sorted(m["status"] for m in body["members"]) == ["active", "invited"]
    invited = next(m for m in body["members"] if m["status"] == "invited")
    assert invited["identity"] is None and invited["invitation_id"]
    active = next(m for m in body["members"] if m["status"] == "active")
    assert active["identity"] == {"iss": ISS, "sub": "sub-1"}
    assert active["updated_at"] == "2026-10-24T10:00:00.123456Z"
    assert r.headers["Cache-Control"] == "private, no-cache" and r.headers["Vary"] == "Authorization"


def test_snapshot_etag_ignores_as_of_and_answers_304(client, issuer, conn):
    url = f"/tenant/membership/v1/tenants/{TENANT}/members"
    first = client.get(url, headers=_auth(issuer))
    etag = first.headers["ETag"]
    again = client.get(url, headers={**_auth(issuer), "If-None-Match": etag})
    assert again.status_code == 304 and again.content == b"" and again.headers["ETag"] == etag
    conn.members[0]["content_studio_role"] = "viewer"
    changed = client.get(url, headers={**_auth(issuer), "If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["ETag"] != etag


@pytest.mark.parametrize("is_canary, served", [(True, True), (False, False), (None, False)])
def test_snapshot_is_synthetic_reads_is_canary_and_is_never_null(client, issuer, conn, is_canary, served):
    """1.2 (AA-979): is_synthetic is shared.tenants.is_canary, a NULL there served as false (CIS 281 made the
    column nullable); the CRM's A10b check requires it to be true."""
    conn.tenant["is_canary"] = is_canary
    body = client.get(f"/tenant/membership/v1/tenants/{TENANT}/members", headers=_auth(issuer)).json()
    _valid("TenantMembers", body)
    assert body["is_synthetic"] is served


def test_snapshot_etag_moves_when_is_synthetic_changes(client, issuer, conn):
    url = f"/tenant/membership/v1/tenants/{TENANT}/members"
    etag = client.get(url, headers=_auth(issuer)).headers["ETag"]
    conn.tenant["is_canary"] = True
    changed = client.get(url, headers={**_auth(issuer), "If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["ETag"] != etag and changed.json()["is_synthetic"] is True


def test_the_contract_requires_is_synthetic_as_a_boolean():
    schema = DOC["components"]["schemas"]["TenantMembers"]
    assert "is_synthetic" in schema["required"] and schema["properties"]["is_synthetic"]["type"] == "boolean"
    body = {"tenant_id": TENANT, "tenant_state": "active", "members": [], "as_of": "2026-10-26T00:00:00Z"}
    assert list(_schema("TenantMembers").iter_errors(body))
    assert list(_schema("TenantMembers").iter_errors({**body, "is_synthetic": None}))
    assert not list(_schema("TenantMembers").iter_errors({**body, "is_synthetic": True}))


@pytest.mark.parametrize("is_active, cancelled, state", [(False, None, "locked"), (False, T0, "deleted"),
                                                        (True, T0, "deleted")])
def test_snapshot_tenant_state(client, issuer, conn, is_active, cancelled, state):
    conn.tenant.update(is_active=is_active, cancelled_at=cancelled)
    body = client.get(f"/tenant/membership/v1/tenants/{TENANT}/members", headers=_auth(issuer)).json()
    assert body["tenant_state"] == state
    _valid("TenantMembers", body)


@pytest.mark.parametrize("tenant", [UNKNOWN_TENANT, "not-a-uuid"])
def test_unknown_tenant_is_404(client, issuer, tenant):
    r = client.get(f"/tenant/membership/v1/tenants/{tenant}/members", headers=_auth(issuer))
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    _valid("Problem", r.json())


# ── lookup ──────────────────────────────────────────────────────────────────

def test_lookup_by_iss_and_sub(client, issuer):
    r = client.get("/tenant/membership/v1/memberships", params={"iss": ISS, "sub": "sub-1"}, headers=_auth(issuer))
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    body = r.json()
    _valid("IdentityMemberships", body)
    assert body["identity"] == {"iss": ISS, "sub": "sub-1"} and len(body["memberships"]) == 1
    other = client.get("/tenant/membership/v1/memberships", params={"iss": ISS + "x", "sub": "sub-1"},
                       headers=_auth(issuer)).json()
    assert other["memberships"] == []                                   # the same sub under another issuer


@pytest.mark.parametrize("params", [{"sub": "s"}, {"iss": ISS}, {"iss": "http://plain.example", "sub": "s"},
                                    {"iss": ISS, "sub": "s" * 129}, {"iss": "https://" + "a" * 300, "sub": "s"}])
def test_lookup_refuses_a_partial_or_bad_identity(client, issuer, params):
    r = client.get("/tenant/membership/v1/memberships", params=params, headers=_auth(issuer))
    assert r.status_code == 400 and r.json()["error"] == "invalid"
    _valid("Problem", r.json())


# ── feed ────────────────────────────────────────────────────────────────────

def _membership_json(status="removed") -> dict:
    return {"membership_id": str(uuid.uuid4()), "tenant_id": TENANT, "identity": {"iss": ISS, "sub": "sub-1"},
            "content_studio_role": "owner", "status": status, "invitation_id": None,
            "updated_at": "2026-10-24T10:00:00.123456Z"}


def test_feed_conforms_and_pages_by_txid_then_seq(client, issuer, conn):
    conn.events = [_event(5, 900, "tenant.created"), _event(3, 901, "tenant.locked"),
                   _event(4, 901, "membership.removed", json.dumps(_membership_json())),
                   _event(9, 905, "tenant.unlocked")]
    r = client.get("/tenant/membership/v1/events", params={"limit": "2"}, headers=_auth(issuer))
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    page = r.json()
    _valid("MembershipEventPage", page)
    assert [e["type"] for e in page["items"]] == ["tenant.created", "tenant.locked"]
    assert page["items"][0]["membership"] is None
    page2 = client.get("/tenant/membership/v1/events", params={"after": page["next_cursor"], "limit": "2"},
                       headers=_auth(issuer)).json()
    _valid("MembershipEventPage", page2)
    assert [e["type"] for e in page2["items"]] == ["membership.removed", "tenant.unlocked"]
    assert page2["items"][0]["membership"]["status"] == "removed"
    assert conn.feed_args[1][:2] == ("901", 3)
    end = client.get("/tenant/membership/v1/events", params={"after": page2["next_cursor"]},
                     headers=_auth(issuer)).json()
    assert end == {"items": [], "next_cursor": page2["next_cursor"]}     # nothing new: keep the cursor


def test_feed_sorts_by_the_xid8_column_not_by_its_text_output():
    """The fake above sorts numerically, so it cannot catch a text sort: check the statement itself. Postgres resolves
    a bare ``ORDER BY`` name to an OUTPUT column first, so ``txid::text AS txid`` + ``ORDER BY txid`` sorted '10'
    before '9' (pages served rows twice, ``limit=1`` skipped rows: the flaky integration test of S256). Every
    ORDER BY term must be a qualified input column, and no output column may reuse an input column name with
    another expression."""
    sql = " ".join(feed.FEED_SQL.split())
    select_list = sql.split(" FROM ")[0].removeprefix("SELECT ")
    order_by = sql.split(" ORDER BY ")[1].split(" LIMIT ")[0]
    assert [t.strip() for t in order_by.split(",")] == ["e.txid", "e.seq"]
    aliases = {item.split(" AS ")[1].strip() for item in select_list.split(",") if " AS " in item}
    assert aliases == {"txid_text"}
    assert not aliases & {"txid", "seq"}


def test_feed_pages_txids_of_different_lengths_in_numeric_order(client, issuer, conn):
    """txid 9 / 10 / 99 / 100 (text order would be 10, 100, 9, 99) and three rows of one transaction: limit 1 walks
    them in (txid, seq) order with no repeat and no gap, and matches one big page."""
    conn.events = [_event(7, 100, "tenant.unlocked"), _event(2, 9, "tenant.created"), _event(5, 99, "tenant.locked"),
                   _event(3, 10, "tenant.locked"), _event(4, 10, "tenant.unlocked"), _event(6, 10, "tenant.locked")]
    expected = [(9, 2), (10, 3), (10, 4), (10, 6), (99, 5), (100, 7)]
    seen, after = [], None
    for _ in range(len(expected) + 1):
        params = {"limit": "1"} | ({"after": after} if after else {})
        page = client.get("/tenant/membership/v1/events", params=params, headers=_auth(issuer)).json()
        if not page["items"]:
            break
        seen += [feed.decode_cursor(e["cursor"]) for e in page["items"]]
        after = page["next_cursor"]
    assert seen == expected
    whole = client.get("/tenant/membership/v1/events", params={"limit": "500"}, headers=_auth(issuer)).json()
    assert [feed.decode_cursor(e["cursor"]) for e in whole["items"]] == expected


def test_feed_from_the_start_with_no_events_still_returns_a_cursor(client, issuer):
    page = client.get("/tenant/membership/v1/events", headers=_auth(issuer)).json()
    _valid("MembershipEventPage", page)
    assert page["items"] == [] and feed.decode_cursor(page["next_cursor"]) == feed.START


@pytest.mark.parametrize("params", [{"limit": "0"}, {"limit": "501"}, {"limit": "ten"}, {"after": "!!"},
                                    {"after": feed.encode_cursor(1, 1)[:-2] + "zz"}, {"after": "x" * 201},
                                    {"after": base64.urlsafe_b64encode(b"v2:1:1").decode()}])
def test_feed_refuses_a_bad_limit_or_cursor(client, issuer, params):
    r = client.get("/tenant/membership/v1/events", params=params, headers=_auth(issuer))
    assert r.status_code == 400 and r.json()["error"] == "invalid"


def test_feed_limit_is_capped_at_500_and_defaults_to_200(client, issuer, conn):
    client.get("/tenant/membership/v1/events", params={"limit": "500"}, headers=_auth(issuer))
    client.get("/tenant/membership/v1/events", headers=_auth(issuer))
    assert [a[2] for a in conn.feed_args] == [500, 200]


def test_cursor_round_trips():
    for txid, seq in ((0, 0), (1, 2), (2 ** 64 - 1, 2 ** 63 - 1)):
        assert feed.decode_cursor(feed.encode_cursor(txid, seq)) == (txid, seq)
    with pytest.raises(reader.InvalidRequest):
        feed.decode_cursor(feed.encode_cursor(2 ** 64, 1))


# ── storage outages ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("exc", [asyncpg.UndefinedTableError("relation does not exist"), OSError("down")])
def test_storage_outage_is_503(client, issuer, conn, exc):
    conn.fail = exc
    for path in ("/events", f"/tenants/{TENANT}/members"):
        r = client.get(f"/tenant/membership/v1{path}", headers=_auth(issuer))
        assert r.status_code == 503 and r.headers["Retry-After"] == "5"


def test_crm_client_constant_is_the_dev_client():
    assert CRM == m2m_auth.DEV_M2M_CRM_CLIENT_ID
