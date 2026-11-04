"""AA-1035 R2 slice 2 (root ADR 0019 d5) — read-only "view as tenant": the role x route matrix and the gate.

Guards (lesson S267: the adversarial re-review found a privilege escalation; the matrix test is the guard):
- every route of the app that is not an allowed read (EVERY write route: /admin, /v1, /auth, /tenant, /catalog...) is
  refused for a view-as token with 403 view_as_read_only, before any route runs, and the refusal is logged;
- every allowed read passes only with an open session of a staff member who still holds tenant.view_as and the tenant;
  the audit row is written BEFORE the read runs, and a read whose row cannot be written is not served;
- the dependency-side check (tenant_role.enforce) refuses a view-as payload on anything but an allowed read;
- the start / read / end routes answer each role per the matrix (admin, customer_success own book, support own book +
  pool; nobody else), need a staff identity, a reason, at most 60 minutes, a partner tenant that is not offboarded;
- the Dev-only E2E admin starts a session on a synthetic tenant only;
- (S268 fix pass) the reads depend on the role: view-as never shows a role more than its own staff pages (support never
  reads /v1/quota; nobody reads /v1/billing), and only the staff BFF gets the token (x-view-as-client: bff).

No network, no AWS, no DB: a fake connection answers exactly the SQL each path runs and fails the test on any other.
"""
from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api.core import e2e_staff, staff_authz, view_as
from api.core import tenant_scope as ts
from api.middleware import tenant_role
from api.middleware.view_as import ViewAsGateMiddleware
from api.routers import admin
from api.routers import admin_view_as as va
from api.routers.auth import JWT_ALG, JWT_SECRET
from tests.unit.test_s241_admin_routes_require_auth import _served_routes, _walk

SECRET = f"aa1035-s2-{uuid.uuid4().hex}"   # a fresh value per run
STAFF = "11111111-1111-4111-8111-111111111111"
OTHER_STAFF = "12121212-1212-4121-8121-121212121212"
MINE = "22222222-2222-4222-8222-222222222222"
POOLED = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"
CLOSED = "55555555-5555-4555-8555-555555555555"
SESSION = "66666666-6666-4666-8666-666666666666"
NOW = datetime.now(timezone.utc).replace(microsecond=0)   # the minted token expires NOW + minutes
ROLES = ("admin", "content_lead", "content", "reviewer", "tenant_qa", "customer_success", "support", "finance",
         "ai_ops", "viewer")
VIEW_AS_ROLES = {"admin", "customer_success", "support"}


def token(**over: Any) -> str:
    claims = {"sub": MINE, "name": "Quill Harbor", "plan_tier": "starter", "role": "tenant", "client": "portal",
              "amr": view_as.VIEW_AS_AMR, "csr": "viewer", "vas": SESSION, "stf": STAFF,
              "iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(minutes=30)}
    claims.update(over)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, JWT_SECRET, algorithm=JWT_ALG)


class FakeDb:
    """Pool and connection at once. STAFF's book = {MINE}, the pool = {POOLED}; one session SESSION of STAFF on MINE
    whose state the test sets; audit rows are recorded, never executed."""

    def __init__(self) -> None:
        self.session = {"open": True, "not_ended": True, "staff_active": True, "staff_role": "customer_success"}
        self.audit: list[tuple] = []
        self.expired: list[str] = []
        self.ended: list[tuple] = []
        self.inserted: list[tuple] = []
        self.synthetic = True
        self.fail_audit = False
        self.sql: list[str] = []
        self.session_owner = STAFF

    def acquire(self):
        db = self

        @asynccontextmanager
        async def ctx():
            yield db

        return ctx()

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.sql.append(sql)
        if sql == ts.SQL_IN_BOOK:
            return str(args[0]) == STAFF and str(args[1]) == MINE
        if sql == ts.SQL_IN_POOL:
            return str(args[0]) == POOLED
        if sql == va.STAFF_ACTIVE_SQL:
            return True
        if sql == e2e_staff.SYNTHETIC_TENANT_SQL:
            return self.synthetic
        raise AssertionError(f"unexpected fetchval {sql}")

    async def fetch(self, sql: str, *args: Any) -> list:
        self.sql.append(sql)
        if sql == va.END_OTHERS_SQL:
            return []
        if sql == va.LIST_SQL:
            return [self._session_row()]
        raise AssertionError(f"unexpected fetch {sql}")

    def _session_row(self, **over: Any) -> dict:
        row = {"session_id": SESSION, "admin_user_id": self.session_owner, "tenant_id": MINE,
               "tenant_name": "Quill Harbor", "tenant_slug": "quill-harbor", "staff_name": "Wren Example",
               "reason": "Partner asked why a tour is missing", "ticket_ref": "SUP-1", "started_at": NOW,
               "expires_at": NOW + timedelta(minutes=30), "ended_at": None, "end_reason": None, "open": True}
        row.update(over)
        return row

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.sql.append(sql)
        if sql == view_as.SESSION_STATE_SQL:
            return dict(self.session) if args == (SESSION, MINE, STAFF) else None
        if sql == va.TENANT_SQL:
            tid = str(args[0])
            return {"tenant_id": args[0], "name": "Quill Harbor", "slug": "quill-harbor", "plan_tier": "starter",
                    "cancelled_at": NOW if tid == CLOSED else None}
        if sql == va.INSERT_SQL:
            self.inserted.append(args)
            return {"session_id": SESSION, "started_at": NOW, "expires_at": NOW + timedelta(minutes=args[4])}
        if sql == va.ONE_SQL:
            return self._session_row() if str(args[0]) == SESSION else None
        if sql == va.END_SQL:
            self.ended.append(args)
            return {"end_reason": args[1], "ended_at": NOW}
        raise AssertionError(f"unexpected fetchrow {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        if "acp_shared.audit_log" in sql:
            if self.fail_audit:
                raise OSError("audit store down")
            self.audit.append(args)
            return "INSERT 0 1"
        if "shared.tenant_api_usage" in sql:
            return "INSERT 0 1"
        if sql == view_as.EXPIRE_SQL:
            self.expired.append(args[0])
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute {sql}")


@pytest.fixture
def world(monkeypatch):
    from api.core import staff_credential
    from api.main import app

    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    monkeypatch.setattr(staff_credential, "principal_verified", lambda: False)
    db = FakeDb()
    monkeypatch.setattr(app.state, "pool", db, raising=False)
    return TestClient(app, raise_server_exceptions=False), db, app


def _h(role: str, staff: str = STAFF, bff: bool = True) -> dict:
    h = {"X-Admin-Secret": SECRET, "x-admin-role": role, "x-admin-user-id": staff}
    if bff:
        h[view_as.BFF_HEADER] = view_as.BFF_HEADER_VALUE
    return h


def _concrete(path: str) -> str:
    """A template with every {param} (also {name:path}) replaced by a UUID."""
    import re
    return re.sub(r"\{[^}]+\}", "77777777-7777-4777-8777-777777777777", path)


def _all_routes(app) -> list[tuple[str, str]]:
    keys = set()
    for path, route, _ in _walk(_served_routes(app)):
        if isinstance(route, APIRoute):
            keys |= {(m, path) for m in route.methods - {"HEAD", "OPTIONS"}}
    return sorted(keys)


# ── the allow-list ──────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_read_allow_list_is_exactly_the_reviewed_routes():
    # Guard: a new entry must be a reviewed change of this test. Never a read that starts work (GET /v1/slate enqueues a
    # paid t7_slate job), an export, channel credentials or the brand branch of /admin/brand-identity.
    assert view_as.READ_ROUTES == (
        "/v1/quota", "/v1/marketplace", "/v1/tours/my-versions", "/v1/tours/versions/{version_id}",
        "/v1/content-writing/reviews", "/v1/content-writing/pieces/{piece_id}", "/v1/publish-log",
        "/v1/publish-log/pending", "/v1/notifications", "/v1/facts", "/v1/account/members", "/v1/account/activity",
    )
    for banned in ("/v1/slate", "/v1/tours/versions/{version_id}/export-docx", "/v1/account/activity/export",
                   "/v1/integrations/wordpress", "/v1/integrations/facebook", "/admin/brand-identity",
                   "/v1/planning/trip-reallocation/suggest", "/v1/billing"):
        assert banned not in view_as.READ_ROUTES


def test_every_allowed_read_is_a_real_get_route(world):
    _, _, app = world
    gets = {p for m, p in _all_routes(app) if m == "GET"}
    assert set(view_as.READ_ROUTES) <= gets


@pytest.mark.parametrize("method,path,ok", [
    ("GET", "/v1/quota", True), ("HEAD", "/v1/quota", True), ("GET", "/v1/quota/", False),
    ("GET", "/v1/quotas", False), ("GET", "/v1/quota/x", False), ("POST", "/v1/quota", False),
    ("GET", f"/v1/tours/versions/{MINE}", True), ("GET", f"/v1/tours/versions/{MINE}/export-docx", False),
    ("PATCH", f"/v1/tours/versions/{MINE}", False), ("OPTIONS", "/v1/quota", False),
    ("GET", "/v1/slate", False), ("GET", "/admin/tenants", False), ("GET", "/v1/notifications", True),
    ("POST", "/v1/notifications/read-all", False),
])
def test_route_allowed(method, path, ok):
    assert view_as.route_allowed(method, path) is ok


# ── the role x route matrix of every write route under view-as (through the real app) ─────────────────────────────

def test_every_route_but_the_allowed_reads_is_refused_with_a_view_as_token(world):
    """EVERY route of the app (all methods, all prefixes) that is not an allowed read: 403 view_as_read_only before the
    route runs, one audit row each. Writes included: no write route is reachable under view-as."""
    client, db, app = world
    refused = 0
    for method, path in _all_routes(app):
        if method in ("GET", "HEAD") and path in view_as.READ_ROUTES:
            continue
        res = client.request(method, _concrete(path), headers={"Authorization": f"Bearer {token()}"},
                             json={} if method not in ("GET", "DELETE") else None)
        assert res.status_code == 403, (method, path, res.status_code)
        assert res.json()["detail"] == view_as.READ_ONLY_DETAIL, (method, path)
        refused += 1
    assert refused > 300                     # the whole app, not a sample
    assert len(db.audit) == refused          # every refusal logged
    assert all(a[2] == view_as.AUDIT_ACTION_REQUEST for a in db.audit)
    assert all(db.sql.count(s) == 0 for s in (view_as.SESSION_STATE_SQL,))   # refused before any session read


@pytest.mark.parametrize("header", [
    lambda t: f"Bearer {t}", lambda t: f"bearer {t}", lambda t: f"Bearer  {t}", lambda t: f"Token {t}",
    lambda t: f"Bearer junk {t}",
])
def test_odd_header_shapes_are_still_view_as(world, header):
    client, _, _ = world
    res = client.post(f"/v1/tours/pool/{MINE}/rewrite", headers={"Authorization": header(token())}, json={})
    assert res.status_code == 403 and res.json()["detail"] == view_as.READ_ONLY_DETAIL


def test_a_view_as_token_with_the_staff_secret_is_still_read_only(world):
    """A staff credential next to the token does not turn the request into a staff write."""
    client, _, _ = world
    res = client.post(f"/admin/tenants/{MINE}/offboard", json={},
                      headers={**_h("admin"), "Authorization": f"Bearer {token()}"})
    assert res.status_code == 403 and res.json()["detail"] == view_as.READ_ONLY_DETAIL


def test_a_plain_tenant_token_is_not_touched_by_the_gate(world):
    client, db, _ = world
    plain = token(vas=None, stf=None, amr="partner", csr="owner")
    res = client.post("/v1/notifications/read-all", headers={"Authorization": f"Bearer {plain}"})
    assert "view_as" not in res.text
    assert db.audit == [] or all(a[2] != view_as.AUDIT_ACTION_REQUEST for a in db.audit)


# ── the gate on an allowed read (middleware around a recording app) ────────────────────────────────────────────────

class _Recorder:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope, receive, send):
        self.calls += 1
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})


def _run(db: FakeDb, auth: str, method: str = "GET", path: str = "/v1/quota") -> tuple[int, _Recorder, list]:
    inner = _Recorder()
    gate = ViewAsGateMiddleware(inner)
    sent: list = []
    scope = {"type": "http", "method": method, "path": path, "query_string": b"limit=5",
             "headers": [(b"authorization", auth.encode())], "app": SimpleNamespace(state=SimpleNamespace(pool=db))}

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        sent.append(msg)

    asyncio.run(gate(scope, receive, send))
    return sent[0]["status"], inner, sent


def test_an_open_session_is_served_and_logged_before_the_read():
    db = FakeDb()
    status, inner, _ = _run(db, f"Bearer {token()}")
    assert status == 200 and inner.calls == 1
    (row,) = db.audit
    # tenant, actor, action, resource_type, resource_id, details, actor_type, http_method, route, status
    assert row[0] == MINE and row[1] == f"admin:{STAFF}" and row[2] == "tenant.view_as_request"
    assert row[3] == "staff_view_as_session" and row[4] == SESSION and row[6] == "staff"
    assert row[7] is None and row[8] == "/v1/quota" and row[9] is None   # logged before the read: no status yet
    assert '"outcome": "served"' in row[5] and '"query_keys": ["limit"]' in row[5] and "5" not in row[5].split(
        "query_keys")[1]


def test_no_unlogged_read_when_the_audit_row_fails():
    db = FakeDb()
    db.fail_audit = True
    status, inner, _ = _run(db, f"Bearer {token()}")
    assert status == 503 and inner.calls == 0


@pytest.mark.parametrize("state,reason", [
    ({"open": False, "not_ended": False}, "ended"),
    ({"open": False, "not_ended": True}, "expired"),
    ({"staff_active": False}, "staff_inactive"),
    ({"staff_role": "finance"}, "role_lost_view_as"),
    ({"staff_role": "content"}, "role_lost_view_as"),
])
def test_a_closed_session_or_a_lost_right_ends_the_view(state, reason):
    db = FakeDb()
    db.session.update(state)
    status, inner, sent = _run(db, f"Bearer {token()}")
    assert status == 401 and inner.calls == 0
    assert b"view_as_ended" in sent[1]["body"]
    assert f'"outcome": "refused_{reason}"' in db.audit[0][5]
    assert db.expired == ([SESSION] if reason == "expired" else [])


def test_a_customer_success_member_whose_assignment_was_removed_loses_the_view():
    db = FakeDb()
    status, inner, _ = _run(db, f"Bearer {token(sub=OTHER)}")     # no session row for OTHER: unknown
    assert status == 401 and inner.calls == 0
    # the session row exists but the tenant left the member's book: in_scope says no
    db2 = FakeDb()

    async def not_in_book(sql, *a):
        return False if sql == ts.SQL_IN_BOOK else await FakeDb.fetchval(db2, sql, *a)
    db2.fetchval = not_in_book
    status, inner, _ = _run(db2, f"Bearer {token()}")
    assert status == 401 and inner.calls == 0 and '"refused_out_of_scope"' in db2.audit[0][5]


def _as_role(role: str) -> FakeDb:
    db = FakeDb()
    db.session["staff_role"] = role

    async def state(sql, *a):
        if sql == view_as.SESSION_STATE_SQL:
            return dict(db.session)
        return await FakeDb.fetchrow(db, sql, *a)
    db.fetchrow = state
    return db


def test_support_keeps_the_view_of_a_pooled_tenant():
    db = _as_role("support")
    status, inner, _ = _run(db, f"Bearer {token(sub=POOLED)}", path="/v1/marketplace")
    assert status == 200 and inner.calls == 1


# ── the role x read matrix (S268 fix pass: view-as never widens a role's own reads) ───────────────────────────────

# Every view-as role x every allowed read. support holds no money capability: never the plan / usage read.
READS_BY_ROLE = {
    "admin": set(view_as.READ_ROUTES),
    "customer_success": set(view_as.READ_ROUTES),
    "support": set(view_as.READ_ROUTES) - {"/v1/quota"},
}


@pytest.mark.parametrize("role", ROLES)
def test_reads_for_role_matches_the_matrix(role):
    assert set(view_as.reads_for_role(role)) == READS_BY_ROLE.get(role, set()), role
    for template in view_as.READ_ROUTES:
        assert view_as.role_may_read(role, template) is (template in READS_BY_ROLE.get(role, set()))
    assert view_as.role_may_read("admin", "/v1/billing") is False      # not an allowed read at all


def test_every_route_cap_is_a_real_capability_and_an_allowed_read():
    for template, caps in view_as.ROUTE_CAPS.items():
        assert template in view_as.READ_ROUTES
        assert caps and set(caps) <= staff_authz.CAPABILITIES


@pytest.mark.parametrize("role", sorted(VIEW_AS_ROLES))
@pytest.mark.parametrize("template", view_as.READ_ROUTES)
def test_each_view_as_role_x_each_read_through_the_gate(role, template):
    """The gate (current DB role) serves exactly READS_BY_ROLE; any other read: 403 view_as_not_for_role, logged."""
    db = _as_role(role)
    status, inner, sent = _run(db, f"Bearer {token()}", path=_concrete(template))
    (row,) = db.audit
    if template in READS_BY_ROLE[role]:
        assert status == 200 and inner.calls == 1, (role, template)
        assert '"outcome": "served"' in row[5]
    else:
        assert status == 403 and inner.calls == 0, (role, template)
        assert view_as.NOT_FOR_ROLE_DETAIL.encode() in sent[1]["body"]
        assert row[8] == template and row[9] == 403
        assert f'"outcome": "refused_{view_as.NOT_FOR_ROLE_REASON}"' in row[5]


@pytest.mark.parametrize("role,status", [("admin", 403), ("customer_success", 403), ("support", 403)])
def test_nobody_reads_billing_in_a_view_as_session(role, status):
    db = _as_role(role)
    got, inner, sent = _run(db, f"Bearer {token()}", path="/v1/billing")
    assert got == status and inner.calls == 0 and view_as.READ_ONLY_DETAIL.encode() in sent[1]["body"]
    assert '"outcome": "refused_read_only"' in db.audit[0][5]


def test_support_quota_is_refused_with_the_real_app(world):
    """The real app: a support view-as token gets 403 on /v1/quota and /v1/billing (logged), 200-path reads pass."""
    client, db, _ = world
    db.session["staff_role"] = "support"
    for path, detail in (("/v1/quota", view_as.NOT_FOR_ROLE_DETAIL), ("/v1/billing", view_as.READ_ONLY_DETAIL)):
        res = client.get(path, headers={"Authorization": f"Bearer {token()}"})
        assert res.status_code == 403 and res.json()["detail"] == detail, path
    outcomes = [a[5] for a in db.audit if a[2] == view_as.AUDIT_ACTION_REQUEST]
    assert len(outcomes) == 2 and '"refused_route_not_for_role"' in outcomes[0] and '"refused_read_only"' in outcomes[1]


@pytest.mark.parametrize("over", [{"vas": "not-a-uuid"}, {"stf": "x"}, {"sub": "y"}, {"role": "admin"}])
def test_bad_claims_are_refused(over):
    db = FakeDb()
    status, inner, _ = _run(db, f"Bearer {token(**over)}")
    assert status == 401 and inner.calls == 0


def test_an_expired_token_is_left_to_the_route():
    db = FakeDb()
    status, inner, _ = _run(db, f"Bearer {token(exp=datetime.now(timezone.utc) - timedelta(minutes=1))}")
    assert inner.calls == 1 and db.audit == []    # the route's own auth answers 401 for an expired token


def test_the_dependency_side_check_refuses_a_view_as_write():
    payload = jwt.decode(token(), JWT_SECRET, algorithms=[JWT_ALG])
    write = SimpleNamespace(method="PATCH", url=SimpleNamespace(path=f"/v1/tours/versions/{MINE}"))
    with pytest.raises(HTTPException) as exc:
        tenant_role.enforce(write, payload)
    assert exc.value.status_code == 403 and exc.value.detail == view_as.READ_ONLY_DETAIL
    other_read = SimpleNamespace(method="GET", url=SimpleNamespace(path="/v1/slate"))
    with pytest.raises(HTTPException):
        tenant_role.enforce(other_read, payload)
    tenant_role.enforce(SimpleNamespace(method="GET", url=SimpleNamespace(path="/v1/quota")), payload)


def test_the_token_is_a_read_only_unmetered_tenant_token():
    exp = datetime.now(timezone.utc) + timedelta(minutes=20)
    t = view_as.mint_token(session_id=SESSION, tenant_id=MINE, staff_id=STAFF, name="Quill Harbor",
                           plan_tier="starter", expires_at=exp)
    p = jwt.decode(t, JWT_SECRET, algorithms=[JWT_ALG])
    assert p["role"] == "tenant" and p["sub"] == MINE and p["csr"] == "viewer" and p["client"] == "portal"
    assert p["vas"] == SESSION and p["stf"] == STAFF and p["amr"] == "staff_view_as"
    assert p["exp"] == int(exp.timestamp())
    assert "mid" not in p


# ── start / read / end per role (the new routes x every role) ──────────────────────────────────────────────────────

BODY = {"reason": "Partner asked why a tour is missing", "ticket_ref": "SUP-1", "minutes": 30}


@pytest.mark.parametrize("role", ROLES)
def test_start_per_role_on_the_callers_book(world, role):
    client, db, _ = world
    res = client.post(f"/admin/tenants/{MINE}/view-as", json=BODY, headers=_h(role))
    if role in VIEW_AS_ROLES:
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["read_only"] is True and body["session_id"] == SESSION
        assert set(body["allowed_reads"]) == READS_BY_ROLE[role]
        p = jwt.decode(body["token"], JWT_SECRET, algorithms=[JWT_ALG])
        assert p["vas"] == SESSION and p["stf"] == STAFF and p["sub"] == MINE
        assert any(a[2] == "tenant.view_as_started" for a in db.audit)
        assert db.inserted == [(STAFF, uuid.UUID(MINE), BODY["reason"], "SUP-1", 30)]
    else:
        assert res.status_code == 403, (role, res.text)
        assert db.inserted == []


@pytest.mark.parametrize("role,tenant,ok", [
    ("customer_success", OTHER, False), ("customer_success", POOLED, False),
    ("support", POOLED, True), ("support", OTHER, False), ("admin", OTHER, True),
])
def test_start_is_scoped(world, role, tenant, ok):
    client, db, _ = world
    res = client.post(f"/admin/tenants/{tenant}/view-as", json=BODY, headers=_h(role))
    assert (res.status_code == 201) is ok, res.text
    if not ok:
        assert res.status_code == 403 and db.inserted == []


@pytest.mark.parametrize("body,status", [
    ({"reason": "  "}, 422), ({"reason": "ok"}, 422), ({"reason": "x" * 501}, 422),
    ({"reason": "Checking a held piece", "minutes": 61}, 422), ({"reason": "Checking a held piece", "minutes": 4}, 422),
    ({"reason": "Checking a held piece", "ticket_ref": "x" * 121}, 422),
    ({"reason": "Checking a held piece", "extra": 1}, 422),
    ({"reason": "Checking a held piece"}, 201),
])
def test_start_needs_a_reason_and_at_most_60_minutes(world, body, status):
    client, _, _ = world
    assert client.post(f"/admin/tenants/{MINE}/view-as", json=body, headers=_h("admin")).status_code == status


def test_start_refuses_the_internal_tenant_an_offboarded_one_and_no_staff_id(world):
    client, db, _ = world
    assert client.post(f"/admin/tenants/{ts.AA_INTERNAL_TENANT}/view-as", json=BODY,
                       headers=_h("admin")).status_code == 409
    assert client.post(f"/admin/tenants/{CLOSED}/view-as", json=BODY, headers=_h("admin")).status_code == 409
    no_id = {"X-Admin-Secret": SECRET, "x-admin-role": "admin", view_as.BFF_HEADER: view_as.BFF_HEADER_VALUE}
    res = client.post(f"/admin/tenants/{MINE}/view-as", json=BODY, headers=no_id)
    assert res.status_code == 403 and res.json()["detail"] == va.NO_STAFF_ID
    machine = {"X-Admin-Secret": SECRET, view_as.BFF_HEADER: view_as.BFF_HEADER_VALUE}
    assert client.post(f"/admin/tenants/{MINE}/view-as", json=BODY, headers=machine).status_code == 403
    assert db.inserted == []


@pytest.mark.parametrize("role", sorted(VIEW_AS_ROLES))
@pytest.mark.parametrize("value", [None, "", "browser", "BFF "])
def test_only_the_staff_bff_gets_a_token(world, role, value):
    """The start answers the token only with x-view-as-client: bff (set by the view-as BFF, never by the generic admin
    proxy): otherwise 403 view_as_bff_only before any row, any audit row or any token."""
    client, db, _ = world
    headers = _h(role, bff=False)
    if value is not None:
        headers[view_as.BFF_HEADER] = value
    res = client.post(f"/admin/tenants/{POOLED if role == 'support' else MINE}/view-as", json=BODY, headers=headers)
    assert res.status_code == 403 and res.json()["detail"] == view_as.BFF_ONLY_DETAIL
    assert "token" not in res.json() and db.inserted == []
    assert not any(a[2] == "tenant.view_as_started" for a in db.audit)


def test_the_e2e_admin_starts_a_view_on_a_synthetic_tenant_only(world, monkeypatch):
    client, db, _ = world
    ident = SimpleNamespace(auth="cognito", admin_id=STAFF, role="admin", is_owner=False, e2e_read_only=True)
    monkeypatch.setattr(staff_authz, "request_staff_identity", lambda r: ident)
    monkeypatch.setattr(va, "request_staff_identity", lambda r: ident)
    monkeypatch.setattr(ts, "request_staff_identity", lambda r: ident)
    db.synthetic = False
    res = client.post(f"/admin/tenants/{MINE}/view-as", json=BODY, headers=_h("admin"))
    assert res.status_code == 403 and res.json()["detail"] == e2e_staff.E2E_READ_ONLY and db.inserted == []
    db.synthetic = True
    assert client.post(f"/admin/tenants/{MINE}/view-as", json=BODY, headers=_h("admin")).status_code == 201


@pytest.mark.parametrize("role", ROLES)
def test_read_and_end_per_role(world, role):
    client, db, _ = world
    got = client.get(f"/admin/view-as/sessions/{SESSION}", headers=_h(role))
    listed = client.get("/admin/view-as/sessions", headers=_h(role))
    end = client.post(f"/admin/view-as/sessions/{SESSION}/end", headers=_h(role))
    if role in VIEW_AS_ROLES:
        assert got.status_code == 200 and got.json()["tenant_name"] == "Quill Harbor"
        assert listed.status_code == 200 and listed.json()["scope"] == ("all" if role == "admin" else "own")
        assert end.status_code == 200 and end.json()["changed"] is True
        assert db.ended == [(uuid.UUID(SESSION), "ended")]       # own session: ended
    else:
        assert (got.status_code, listed.status_code, end.status_code) == (403, 403, 403)
        assert db.ended == []


def test_another_members_session_is_invisible_but_admin_revokes_it(world):
    client, db, _ = world
    db.session_owner = OTHER_STAFF
    assert client.get(f"/admin/view-as/sessions/{SESSION}", headers=_h("customer_success")).status_code == 404
    assert client.post(f"/admin/view-as/sessions/{SESSION}/end", headers=_h("support")).status_code == 404
    assert db.ended == []
    res = client.post(f"/admin/view-as/sessions/{SESSION}/end", headers=_h("admin"))
    assert res.status_code == 200 and db.ended == [(uuid.UUID(SESSION), "revoked")]
    assert any(a[2] == "tenant.view_as_ended" for a in db.audit)


def test_list_of_a_book_role_is_filtered_to_its_own_sessions(world):
    client, db, _ = world
    calls: list = []
    orig = db.fetch

    async def spy(sql, *a):
        calls.append(a)
        return await orig(sql, *a)
    db.fetch = spy
    client.get("/admin/view-as/sessions", headers=_h("support"))
    client.get("/admin/view-as/sessions", headers=_h("admin"))
    assert calls[0][0] == STAFF and calls[1][0] is None


def test_every_new_staff_route_x_every_role_matches_the_matrix():
    """The capability decision of every route this slice adds, for every role (scope comes after, per tenant)."""
    expected = {
        ("POST", "/admin/tenants/{tenant_id}/view-as"): VIEW_AS_ROLES,
        ("GET", "/admin/view-as/sessions"): VIEW_AS_ROLES,
        ("GET", "/admin/view-as/sessions/{session_id}"): VIEW_AS_ROLES,
        ("POST", "/admin/view-as/sessions/{session_id}/end"): VIEW_AS_ROLES,
        ("GET", "/admin/tenant-assignments"): {"admin", "customer_success", "support"},
        ("GET", "/admin/tenant-assignments/staff"): {"admin"},
        ("GET", "/admin/notifications"): set(ROLES),
        ("GET", "/admin/notifications/count"): set(ROLES),
        ("PUT", "/admin/notifications/read-all"): set(ROLES) - {"viewer"},
        ("PUT", "/admin/notifications/{notif_id}/read"): set(ROLES) - {"viewer"},
    }
    for key, roles in expected.items():
        allowed = {r for r in ROLES if staff_authz.denial_reason(staff_authz.principal_from_role(r), *key) is None}
        assert allowed == roles, key
        assert staff_authz.denial_reason(staff_authz.MACHINE, *key) is not None, key


def test_the_view_as_routes_are_tenant_scoped(world):
    _, _, app = world
    for path, route, _ in _walk(_served_routes(app)):
        if isinstance(route, APIRoute) and path == "/admin/tenants/{tenant_id}/view-as":
            assert any(d.call is ts.require_tenant_scope for d in route.dependant.dependencies)
            return
    raise AssertionError("view-as start route not found")


def test_the_frontend_mirror_of_the_read_allow_list_is_the_same():
    """frontend/lib/viewAs.ts VIEW_AS_READ_ROUTES: the BFF forwards only these (the backend stays the authority)."""
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2] / "frontend" / "lib" / "viewAs.ts").read_text()
    block = re.search(r"export const VIEW_AS_READ_ROUTES = \[(.*?)\] as const;", src, re.S)
    assert block, "VIEW_AS_READ_ROUTES not found"
    assert tuple(re.findall(r'"([^"]+)"', block.group(1))) == view_as.READ_ROUTES
    # VIEW_AS_ROUTE_ROLES: the staff roles of each ROUTE_CAPS read (the BFF refuses the others, the page hides them)
    caps = re.search(r"export const VIEW_AS_ROUTE_ROLES: [^=]+= \{(.*?)\};", src, re.S)
    assert caps, "VIEW_AS_ROUTE_ROLES not found"
    mirror = {k: set(re.findall(r'"([a-z_]+)"', v))
              for k, v in re.findall(r'"(/v1/[^"]+)":\s*\[([^\]]*)\]', caps.group(1))}
    assert mirror == {t: {r for r in staff_authz.ROLE_PERMS if view_as.role_may_read(r, t)}
                      for t in view_as.ROUTE_CAPS}
    # the BFF start call carries the header the backend requires (same name and value)
    assert f'export const VIEW_AS_BFF_HEADER = "{view_as.BFF_HEADER}";' in src
    bff = (Path(__file__).resolve().parents[2] / "frontend" / "app" / "api" / "view-as" / "route.ts").read_text()
    assert f'[VIEW_AS_BFF_HEADER]: "{view_as.BFF_HEADER_VALUE}"' in bff
    roles = re.search(r"export const VIEW_AS_ROLES = \[(.*?)\] as const;", src, re.S)
    assert set(re.findall(r'"([a-z_]+)"', roles.group(1))) == {
        r for r, perms in staff_authz.ROLE_PERMS.items() if staff_authz.TENANT_VIEW_AS in perms}
