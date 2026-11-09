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
  reads /v1/quota; nobody reads /v1/billing), and only the staff BFF gets the token (x-view-as-client: bff);
- (S268 fix round 2) the gate re-checks the tenant with the start's rule (offboarded, not live, internal: the view
  ends); every path that ends a session writes one tenant.view_as_ended row under that session (end route, a new
  start, the gate); the gate's own per-session rate limit cuts a loop off before any check or audit row.
- (S268 fix round 3) the session is bound to the role it was started with (the signed srl claim, the verified group
  role): the start refuses a member whose directory row differs from it; the gate evaluates scope and reads with it and
  ends the session (cause role_changed) when the DB row or the role the staff BFF forwards is no longer it, so a row
  that drifted above the group never keeps a view whose scope was lost.

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
ROLES = ("admin", "content_lead", "content", "tenant_qa", "customer_success", "support", "finance",
         "ai_ops", "viewer")
VIEW_AS_ROLES = {"admin", "customer_success", "support"}


def token(**over: Any) -> str:
    claims = {"sub": MINE, "name": "Quill Harbor", "plan_tier": "starter", "role": "tenant", "client": "portal",
              "amr": view_as.VIEW_AS_AMR, "csr": "viewer", "vas": SESSION, "stf": STAFF, "srl": "customer_success",
              "iat": datetime.now(timezone.utc), "exp": datetime.now(timezone.utc) + timedelta(minutes=30)}
    claims.update(over)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, JWT_SECRET, algorithm=JWT_ALG)


class FakeDb:
    """Pool and connection at once. STAFF's book = {MINE}, the pool = {POOLED}; one session SESSION of STAFF on MINE
    whose state the test sets; audit rows are recorded, never executed."""

    def __init__(self) -> None:
        self.session = {"open": True, "not_ended": True, "staff_active": True, "staff_role": "customer_success",
                        "tenant_cancelled_at": None, "tenant_status": "active", "tenant_kind": "agency",
                        "tenant_active": True}
        self.tenant_state: dict = {}            # TENANT_SQL overrides for the start (account_status, kind...)
        self.others: list[dict] = []            # the caller's other open sessions END_OTHERS_SQL closes
        self.audit: list[tuple] = []
        self.expired: list[str] = []
        self.closed: list[tuple] = []           # (session, end_reason asked) of every CLOSE_SQL
        self.already_closed = False             # CLOSE_SQL finds no open row (another request closed it first)
        self.ended: list[tuple] = []
        self.inserted: list[tuple] = []
        self.synthetic = True
        self.fail_audit = False
        self.sql: list[str] = []
        self.session_owner = STAFF
        self.staff_db_role = "admin"            # shared.admin_users.role of the caller of the start (STAFF_STATE_SQL)

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
        if sql == e2e_staff.SYNTHETIC_TENANT_SQL:
            return self.synthetic
        if sql == view_as.CLOSE_SQL:
            self.closed.append(args)
            if args[1] == "expired":
                self.expired.append(args[0])
            return None if self.already_closed else args[1]
        raise AssertionError(f"unexpected fetchval {sql}")

    async def fetch(self, sql: str, *args: Any) -> list:
        self.sql.append(sql)
        if sql == va.END_OTHERS_SQL:
            return [dict(r) for r in self.others]
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
                    "cancelled_at": NOW if tid == CLOSED else None,
                    "account_status": "offboarding" if tid == CLOSED else "active", "kind": "agency",
                    "is_active": tid != CLOSED, **self.tenant_state}
        if sql == va.STAFF_STATE_SQL:
            return {"is_active": True, "role": self.staff_db_role}
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
    # no Redis: the gate's per-session limit fails open, so the whole-app walk (300+ refusals in one session) is never
    # cut off by it, whatever an earlier test left on app.state (the limit has its own tests below)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
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
        "/v1/account/add-ons",
    )
    for banned in ("/v1/slate", "/v1/tours/versions/{version_id}/export-docx", "/v1/account/activity/export",
                   "/v1/integrations/wordpress", "/v1/integrations/facebook", "/admin/brand-identity",
                   "/v1/planning/trip-reallocation/suggest", "/v1/billing", "/v1/account/add-on-requests"):
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
    # AA-1036 R3: the agency's add-ons are read; asking for one (the owner's POST) never goes through a view-as session
    ("GET", "/v1/account/add-ons", True), ("POST", "/v1/account/add-ons", False),
    ("POST", "/v1/account/add-on-requests", False), ("GET", "/v1/account/add-on-requests", False),
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


# AA-1042 (CIS #1301) added these staff routes after slice 2 was written: pinned so the walk above provably covers them.
AA1042_ROUTES = (
    ("GET", "/admin/ai-gateway/route-trace/requests"), ("GET", "/admin/ai-gateway/route-trace/requests/{trace_id}"),
    ("GET", "/admin/ai-gateway/route-trace/rungs"), ("GET", "/admin/ai-gateway/route-trace/fallback-content"),
    ("POST", "/admin/ai-gateway/route-trace/test"), ("GET", "/admin/ai-gateway/route-policy"),
    ("PUT", "/admin/ai-gateway/route-policy/{stage}"), ("DELETE", "/admin/ai-gateway/route-policy/{stage}"),
    ("GET", "/admin/content-quality/fallback-writes"),
)


def test_the_route_trace_and_route_policy_routes_are_refused_under_view_as(world):
    client, db, app = world
    assert set(AA1042_ROUTES) <= set(_all_routes(app))
    for method, path in AA1042_ROUTES:
        res = client.request(method, _concrete(path), headers={"Authorization": f"Bearer {token()}"},
                             json={} if method not in ("GET", "DELETE") else None)
        assert res.status_code == 403, (method, path, res.status_code)
        assert res.json()["detail"] == view_as.READ_ONLY_DETAIL, (method, path)
    assert len(db.audit) == len(AA1042_ROUTES)


# AA-1045 added the usage explorer (staff money pages: ai.spend.read / ai.ops.write). None is a view-as read: pinned so
# the walk above provably covers them, reads included, and so a later READ_ROUTES edit cannot open one silently.
AA1045_ROUTES = (
    ("GET", "/admin/usage-explorer/query"), ("GET", "/admin/usage-explorer/options"),
    ("GET", "/admin/usage-explorer/invoices"), ("GET", "/admin/usage-explorer/views"),
    ("POST", "/admin/usage-explorer/views"), ("DELETE", "/admin/usage-explorer/views/{view_id}"),
    ("GET", "/admin/usage-explorer/rollup/check"), ("POST", "/admin/usage-explorer/rollup/run"),
)


def test_the_usage_explorer_routes_are_refused_under_view_as(world):
    client, db, app = world
    assert set(AA1045_ROUTES) <= set(_all_routes(app))
    assert not any(path.startswith("/admin/usage-explorer") for path in view_as.READ_ROUTES)
    for method, path in AA1045_ROUTES:
        assert view_as.route_allowed(method, _concrete(path)) is False, (method, path)
        res = client.request(method, _concrete(path), headers={"Authorization": f"Bearer {token()}"},
                             json={} if method not in ("GET", "DELETE") else None)
        assert res.status_code == 403, (method, path, res.status_code)
        assert res.json()["detail"] == view_as.READ_ONLY_DETAIL, (method, path)
    assert len(db.audit) == len(AA1045_ROUTES)


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


def _run(db: FakeDb, auth: str, method: str = "GET", path: str = "/v1/quota",
         role_hint: str | None = None) -> tuple[int, _Recorder, list]:
    inner = _Recorder()
    gate = ViewAsGateMiddleware(inner)
    sent: list = []
    headers = [(b"authorization", auth.encode())]
    if role_hint is not None:
        headers.append((view_as.ROLE_HINT_HEADER.encode(), role_hint.encode()))
    scope = {"type": "http", "method": method, "path": path, "query_string": b"limit=5",
             "headers": headers, "app": SimpleNamespace(state=SimpleNamespace(pool=db))}

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


def _requests(db: FakeDb) -> list[tuple]:
    return [a for a in db.audit if a[2] == view_as.AUDIT_ACTION_REQUEST]


def _ended(db: FakeDb) -> list[tuple]:
    return [a for a in db.audit if a[2] == "tenant.view_as_ended"]


@pytest.mark.parametrize("state,reason", [
    ({"open": False, "not_ended": False}, "ended"),
    ({"open": False, "not_ended": True}, "expired"),
    ({"staff_active": False}, "staff_inactive"),
    ({"staff_role": "finance"}, "role_lost_view_as"),
    ({"staff_role": "content"}, "role_lost_view_as"),
    # S268 fix round 2: the tenant rule of the start, re-checked on every request
    ({"tenant_cancelled_at": NOW, "tenant_status": "offboarding", "tenant_active": False}, "tenant_inactive"),
    ({"tenant_status": "closed", "tenant_active": False}, "tenant_inactive"),
    ({"tenant_status": "suspended", "tenant_active": False}, "tenant_inactive"),
    ({"tenant_kind": "internal"}, "tenant_inactive"),
])
def test_a_closed_session_or_a_lost_right_ends_the_view(state, reason):
    db = FakeDb()
    db.session.update(state)
    status, inner, sent = _run(db, f"Bearer {token()}")
    assert status == 401 and inner.calls == 0
    assert b"view_as_ended" in sent[1]["body"]
    (req,) = _requests(db)
    assert f'"outcome": "refused_{reason}"' in req[5]
    assert db.expired == ([SESSION] if reason == "expired" else [])
    if reason == "ended":                     # already closed: nothing to end, no second ended row
        assert db.closed == [] and _ended(db) == []
        return
    # the session is ended for good (a lost right never comes back to it) with one tenant.view_as_ended row
    assert db.closed == [(SESSION, "expired" if reason == "expired" else "revoked")]
    (ended,) = _ended(db)
    assert ended[0] == MINE and ended[1] == view_as.GATE_ACTOR and ended[3] == "staff_view_as_session"
    assert ended[4] == SESSION
    assert f'"end_reason": "{db.closed[0][1]}"' in ended[5] and f'"cause": "{reason}"' in ended[5]
    assert f'"session_staff": "{STAFF}"' in ended[5]
    assert db.audit.index(ended) < db.audit.index(req)   # closed (and logged) before the refusal row


def test_a_session_another_request_closed_first_gets_no_second_ended_row():
    db = FakeDb()
    db.session.update({"staff_active": False})
    db.already_closed = True
    status, inner, _ = _run(db, f"Bearer {token()}")
    assert status == 401 and inner.calls == 0 and _ended(db) == [] and len(_requests(db)) == 1


def test_a_failed_close_still_refuses_the_request():
    db = FakeDb()
    db.session.update({"tenant_status": "offboarding", "tenant_cancelled_at": NOW})

    async def broken(sql, *a):
        if sql == view_as.CLOSE_SQL:
            raise OSError("db blip")
        return await FakeDb.fetchval(db, sql, *a)
    db.fetchval = broken
    status, inner, _ = _run(db, f"Bearer {token()}")
    assert status == 401 and inner.calls == 0 and _ended(db) == []
    assert '"outcome": "refused_tenant_inactive"' in _requests(db)[0][5]


@pytest.mark.parametrize("kw,expected", [
    ({}, None),
    ({"tenant_id": ts.AA_INTERNAL_TENANT}, "internal"),
    ({"kind": "internal"}, "internal"),
    ({"cancelled_at": NOW}, "offboarded"),
    ({"account_status": "offboarding"}, "offboarded"),
    ({"account_status": "closed", "is_active": False}, "offboarded"),
    ({"account_status": "suspended", "is_active": False}, "inactive"),
    ({"account_status": "applied", "is_active": False}, "inactive"),
    ({"is_active": False}, "inactive"),
    ({"account_status": "trial"}, None), ({"account_status": "past_due"}, None),
    ({"account_status": None, "kind": None, "is_active": None}, None),
])
def test_tenant_refusal_is_one_rule_for_start_and_gate(kw, expected):
    args = {"tenant_id": MINE, "cancelled_at": None, "account_status": "active", "kind": "agency", "is_active": True}
    args.update(kw)
    tid = args.pop("tenant_id")
    assert view_as.tenant_refusal(tid, **args) == expected


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
    assert status == 401 and inner.calls == 0 and '"refused_out_of_scope"' in _requests(db2)[0][5]
    (ended,) = _ended(db2)                    # the view ends for good, logged under the session
    assert ended[4] == SESSION and '"cause": "out_of_scope"' in ended[5] and '"end_reason": "revoked"' in ended[5]


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
    status, inner, _ = _run(db, f"Bearer {token(sub=POOLED, srl='support')}", path="/v1/marketplace")
    assert status == 200 and inner.calls == 1


# ── the session is bound to its start role (S268 fix round 3, review finding) ──────────────────────────────────────

def _not_in_book(db: FakeDb) -> list:
    """The tenant left the member's book; returns the list of scope checks made."""
    checks: list = []

    async def fetchval(sql, *a):
        if sql == ts.SQL_IN_BOOK:
            checks.append(a)
            return False
        return await FakeDb.fetchval(db, sql, *a)
    db.fetchval = fetchval
    return checks


def test_a_db_role_above_the_start_role_never_keeps_a_view_whose_scope_was_lost():
    """The reported exploit: a member demoted in Cognito (group customer_success) whose directory row still says admin
    starts a view on a tenant of its book; the assignment is then removed. Before, the gate computed the scope of the
    DB role (admin: every tenant) and kept serving. Now the session ends at the next request (role_changed), for
    good."""
    db = _as_role("admin")
    checks = _not_in_book(db)
    status, inner, sent = _run(db, f"Bearer {token(srl='customer_success')}", path="/v1/account/members")
    assert status == 401 and inner.calls == 0 and view_as.ENDED_DETAIL.encode() in sent[1]["body"]
    assert '"outcome": "refused_role_changed"' in _requests(db)[0][5]
    assert db.closed == [(SESSION, "revoked")]
    (ended,) = _ended(db)
    assert ended[4] == SESSION and '"cause": "role_changed"' in ended[5] and '"end_reason": "revoked"' in ended[5]
    assert checks == []                               # never evaluated with the drifted role's scope


def test_scope_and_reads_come_from_the_start_role():
    """DB role = start role: the start role's scope decides (customer_success: its book only) and its reads."""
    db = _as_role("customer_success")
    checks = _not_in_book(db)
    status, inner, _ = _run(db, f"Bearer {token(srl='customer_success')}", path="/v1/account/members")
    assert status == 401 and inner.calls == 0 and '"refused_out_of_scope"' in _requests(db)[0][5]
    assert checks == [(STAFF, MINE)]


@pytest.mark.parametrize("start,db_role,cause", [
    ("customer_success", "admin", "role_changed"),     # the row drifted above the start role
    ("admin", "customer_success", "role_changed"),     # demoted in the directory during the session
    ("support", "customer_success", "role_changed"),
    ("customer_success", "finance", "role_lost_view_as"),
    ("customer_success", "reviewer", "role_lost_view_as"),   # the removed alias holds no role (CIS 375)
])
def test_a_role_other_than_the_start_role_ends_the_view(start, db_role, cause):
    db = _as_role(db_role)
    status, inner, _ = _run(db, f"Bearer {token(srl=start)}", path="/v1/marketplace")
    assert status == 401 and inner.calls == 0
    (ended,) = _ended(db)
    assert f'"cause": "{cause}"' in ended[5]


@pytest.mark.parametrize("hint,ok,cause", [
    (None, True, None), ("", True, None), ("customer_success", True, None),
    ("support", False, "role_changed"),               # the group moved during the session; the DB row did not
    ("admin", False, "role_changed"),                 # a hint never widens: it can only end the view
    ("finance", False, "role_lost_view_as"),          # the group lost tenant.view_as
    ("nonsense", False, "role_lost_view_as"),
])
def test_the_role_the_bff_forwards_can_only_end_the_view(hint, ok, cause):
    db = _as_role("customer_success")
    status, inner, _ = _run(db, f"Bearer {token(srl='customer_success')}", path="/v1/marketplace", role_hint=hint)
    if ok:
        assert status == 200 and inner.calls == 1 and _ended(db) == []
        return
    assert status == 401 and inner.calls == 0
    (ended,) = _ended(db)
    assert f'"cause": "{cause}"' in ended[5] and '"end_reason": "revoked"' in ended[5]


def test_the_role_hint_reaches_the_gate_through_the_real_app(world):
    client, db, _ = world
    bearer = {"Authorization": f"Bearer {token()}"}
    res = client.get("/v1/marketplace", headers={**bearer, view_as.ROLE_HINT_HEADER: "support"})
    assert res.status_code == 401 and res.json()["detail"] == view_as.ENDED_DETAIL
    assert any('"cause": "role_changed"' in a[5] for a in _ended(db))


@pytest.mark.parametrize("db_role", ["admin", "support", "finance", "content"])
def test_start_refuses_a_member_whose_directory_role_differs_from_the_sign_in_role(world, db_role):
    """The session would end at its first read: refused at the start (409, before any row, no token)."""
    client, db, _ = world
    db.staff_db_role = db_role
    res = client.post(f"/admin/tenants/{MINE}/view-as", json=BODY, headers=_h("customer_success"))
    assert res.status_code == 409 and res.json()["detail"] == view_as.ROLE_MISMATCH_MESSAGE
    assert "token" not in res.json() and db.inserted == []
    assert not any(str(a[2]).startswith("tenant.view_as") for a in db.audit)
    assert va.END_OTHERS_SQL not in db.sql             # not even the caller's other sessions are touched


def test_start_compares_the_directory_role_as_it_is():
    """AA-1035 close-out: the reviewer alias is gone (CIS 375), so a stray 'reviewer' never reads as content."""
    assert view_as.start_role_mismatch("content", "content") is False
    assert view_as.start_role_mismatch("reviewer", "content") is True
    assert view_as.start_role_mismatch("reviewer", "customer_success") is True
    assert view_as.start_role_mismatch("customer_success", "customer_success") is False
    assert view_as.start_role_mismatch(None, "admin") is True


# ── the role x read matrix (S268 fix pass: view-as never widens a role's own reads) ───────────────────────────────

# Every view-as role x every allowed read. support holds no money capability: never the plan / usage read.
READS_BY_ROLE = {
    "admin": set(view_as.READ_ROUTES),
    "customer_success": set(view_as.READ_ROUTES),
    # AA-1036 review: the add-ons read carries subscription statuses and staff decision notes (Subscription tab data)
    "support": set(view_as.READ_ROUTES) - {"/v1/quota", "/v1/account/add-ons"},
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
    status, inner, sent = _run(db, f"Bearer {token(srl=role)}", path=_concrete(template))
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
    got, inner, sent = _run(db, f"Bearer {token(srl=role)}", path="/v1/billing")
    assert got == status and inner.calls == 0 and view_as.READ_ONLY_DETAIL.encode() in sent[1]["body"]
    assert '"outcome": "refused_read_only"' in db.audit[0][5]


def test_support_quota_is_refused_with_the_real_app(world):
    """The real app: a support view-as token gets 403 on /v1/quota, /v1/account/add-ons and /v1/billing (logged)."""
    client, db, _ = world
    db.session["staff_role"] = "support"
    for path, detail in (("/v1/quota", view_as.NOT_FOR_ROLE_DETAIL),
                         ("/v1/account/add-ons", view_as.NOT_FOR_ROLE_DETAIL),
                         ("/v1/billing", view_as.READ_ONLY_DETAIL)):
        res = client.get(path, headers={"Authorization": f"Bearer {token(srl='support')}"})
        assert res.status_code == 403 and res.json()["detail"] == detail, path
    outcomes = [a[5] for a in db.audit if a[2] == view_as.AUDIT_ACTION_REQUEST]
    assert len(outcomes) == 3 and all('"refused_route_not_for_role"' in o for o in outcomes[:2])
    assert '"refused_read_only"' in outcomes[2]


# ── the gate's own rate limit (S268 fix round 2: a refused request may be limited before it is logged) ───────────

class _Redis:
    def __init__(self, fail: bool = False) -> None:
        self.counts: dict[str, int] = {}
        self.fail = fail

    async def incr(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> bool:
        return True


def _run_with_redis(db: FakeDb, redis: Any, n: int, method: str = "GET", path: str = "/v1/marketplace") -> list[int]:
    from api.middleware import view_as as gate_mod

    statuses = []
    inner = _Recorder()
    gate = gate_mod.ViewAsGateMiddleware(inner)
    for _ in range(n):
        sent: list = []
        scope = {"type": "http", "method": method, "path": path, "query_string": b"",
                 "headers": [(b"authorization", f"Bearer {token()}".encode())],
                 "app": SimpleNamespace(state=SimpleNamespace(pool=db, redis=redis))}

        async def receive():
            return {"type": "http.request", "body": b""}

        async def send(msg, sent=sent):
            sent.append(msg)

        asyncio.run(gate(scope, receive, send))
        statuses.append(sent[0]["status"])
    return statuses


@pytest.mark.parametrize("method,path,ok", [("GET", "/v1/marketplace", 200), ("POST", "/v1/slate", 403)])
def test_a_session_over_its_rate_is_cut_off_before_any_check_or_row(monkeypatch, method, path, ok):
    from api.middleware import view_as as gate_mod

    monkeypatch.setattr(gate_mod, "VIEW_AS_RPM", 3)
    db = FakeDb()
    statuses = _run_with_redis(db, _Redis(), 5, method, path)
    assert statuses == [ok, ok, ok, 429, 429]
    assert len(_requests(db)) == 3                 # the two limited requests: no audit row, no session read
    assert db.sql.count(view_as.SESSION_STATE_SQL) == (3 if ok == 200 else 0)


def test_the_rate_limit_fails_open_and_every_request_is_still_logged(monkeypatch):
    from api.middleware import view_as as gate_mod

    monkeypatch.setattr(gate_mod, "VIEW_AS_RPM", 1)
    db = FakeDb()
    assert _run_with_redis(db, _Redis(fail=True), 3) == [200, 200, 200]
    assert len(_requests(db)) == 3


def test_the_limit_is_per_session(monkeypatch):
    from api.middleware import view_as as gate_mod

    monkeypatch.setattr(gate_mod, "VIEW_AS_RPM", 1)
    redis = _Redis()
    scope = {"app": SimpleNamespace(state=SimpleNamespace(redis=redis))}
    assert asyncio.run(gate_mod.rate_limited(scope, "a", now=120.0)) is None
    assert asyncio.run(gate_mod.rate_limited(scope, "b", now=120.0)) is None
    assert asyncio.run(gate_mod.rate_limited(scope, "a", now=150.0)) == 30
    assert asyncio.run(gate_mod.rate_limited(scope, "a", now=180.0)) is None   # next minute
    assert asyncio.run(gate_mod.rate_limited({"app": None}, "a")) is None    # no Redis: open


@pytest.mark.parametrize("over", [{"vas": "not-a-uuid"}, {"stf": "x"}, {"sub": "y"}, {"role": "admin"},
                                  {"srl": None}, {"srl": "root"}, {"srl": ["admin"]}])
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
    # AA-1036 R3: the add-ons list reads, the add-on request (a write) is refused
    tenant_role.enforce(SimpleNamespace(method="GET", url=SimpleNamespace(path="/v1/account/add-ons")), payload)
    ask = SimpleNamespace(method="POST", url=SimpleNamespace(path="/v1/account/add-on-requests"))
    with pytest.raises(HTTPException) as exc:
        tenant_role.enforce(ask, payload)
    assert exc.value.status_code == 403 and exc.value.detail == view_as.READ_ONLY_DETAIL


def test_the_token_is_a_read_only_unmetered_tenant_token():
    exp = datetime.now(timezone.utc) + timedelta(minutes=20)
    t = view_as.mint_token(session_id=SESSION, tenant_id=MINE, staff_id=STAFF, start_role="support",
                           name="Quill Harbor", plan_tier="starter", expires_at=exp)
    p = jwt.decode(t, JWT_SECRET, algorithms=[JWT_ALG])
    assert p["role"] == "tenant" and p["sub"] == MINE and p["csr"] == "viewer" and p["client"] == "portal"
    assert p["vas"] == SESSION and p["stf"] == STAFF and p["amr"] == "staff_view_as" and p["srl"] == "support"
    assert p["exp"] == int(exp.timestamp())
    assert "mid" not in p


# ── start / read / end per role (the new routes x every role) ──────────────────────────────────────────────────────

BODY = {"reason": "Partner asked why a tour is missing", "ticket_ref": "SUP-1", "minutes": 30}


@pytest.mark.parametrize("role", ROLES)
def test_start_per_role_on_the_callers_book(world, role):
    client, db, _ = world
    db.staff_db_role = role
    res = client.post(f"/admin/tenants/{MINE}/view-as", json=BODY, headers=_h(role))
    if role in VIEW_AS_ROLES:
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["read_only"] is True and body["session_id"] == SESSION
        assert set(body["allowed_reads"]) == READS_BY_ROLE[role]
        p = jwt.decode(body["token"], JWT_SECRET, algorithms=[JWT_ALG])
        assert p["vas"] == SESSION and p["stf"] == STAFF and p["sub"] == MINE
        assert p["srl"] == role                          # the role the start was authorised with, signed
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
    db.staff_db_role = role
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


@pytest.mark.parametrize("state,detail", [
    ({"account_status": "suspended", "is_active": False}, "not active"),
    ({"account_status": "applied", "is_active": False}, "not active"),
    ({"account_status": "closed", "is_active": False}, "offboarded"),
    ({"kind": "internal"}, "not a partner"),
])
def test_start_refuses_a_tenant_the_gate_would_end(world, state, detail):
    """The start and the gate apply one rule (view_as.tenant_refusal): a session the gate would end at once is never
    opened."""
    client, db, _ = world
    db.tenant_state = state
    res = client.post(f"/admin/tenants/{MINE}/view-as", json=BODY, headers=_h("admin"))
    assert res.status_code == 409 and detail in res.json()["detail"] and db.inserted == []


def test_a_new_start_writes_one_ended_row_per_session_it_closes(world):
    client, db, _ = world
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    db.others = [{"session_id": first, "tenant_id": POOLED, "end_reason": "ended"},
                 {"session_id": second, "tenant_id": OTHER, "end_reason": "expired"}]
    res = client.post(f"/admin/tenants/{MINE}/view-as", json=BODY, headers=_h("admin"))
    assert res.status_code == 201
    ended = _ended(db)
    # one row each, under that session's own id and tenant, with its end reason; then the start row
    assert [(a[0], a[4]) for a in ended] == [(POOLED, first), (OTHER, second)]
    assert '"end_reason": "ended"' in ended[0][5] and '"end_reason": "expired"' in ended[1][5]
    assert all('"cause": "replaced"' in a[5] and f'"replaced_by": "{SESSION}"' in a[5] for a in ended)
    assert all(a[1] == f"admin:{STAFF}" for a in ended)
    started = [a for a in db.audit if a[2] == "tenant.view_as_started"]
    assert len(started) == 1 and db.audit.index(started[0]) > db.audit.index(ended[-1])


def test_the_end_route_names_its_cause(world):
    client, db, _ = world
    client.post(f"/admin/view-as/sessions/{SESSION}/end", headers=_h("customer_success"))
    (own,) = _ended(db)
    assert own[4] == SESSION and '"cause": "ended_by_staff"' in own[5]
    db.session_owner = OTHER_STAFF            # an admin ending another member's session: revoked
    db.audit.clear()
    client.post(f"/admin/view-as/sessions/{SESSION}/end", headers=_h("admin"))
    (revoked,) = _ended(db)
    assert '"cause": "revoked_by_admin"' in revoked[5] and f'"session_staff": "{OTHER_STAFF}"' in revoked[5]


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
    ident = SimpleNamespace(auth="cognito", admin_id=STAFF, role="admin", is_owner=False, e2e_read_only=True,
                            e2e_allowlisted_writes=True)
    monkeypatch.setattr(staff_authz, "request_staff_identity", lambda r: ident)
    monkeypatch.setattr(va, "request_staff_identity", lambda r: ident)
    monkeypatch.setattr(ts, "request_staff_identity", lambda r: ident)
    db.synthetic = False
    res = client.post(f"/admin/tenants/{MINE}/view-as", json=BODY, headers=_h("admin"))
    assert res.status_code == 403 and res.json()["detail"] == e2e_staff.E2E_READ_ONLY and db.inserted == []
    db.synthetic = True
    assert client.post(f"/admin/tenants/{MINE}/view-as", json=BODY, headers=_h("admin")).status_code == 201


def _e2e_admin(monkeypatch):
    ident = SimpleNamespace(auth="cognito", admin_id=STAFF, role="admin", is_owner=False, e2e_read_only=True,
                            e2e_allowlisted_writes=True)
    monkeypatch.setattr(staff_authz, "request_staff_identity", lambda r: ident)
    monkeypatch.setattr(va, "request_staff_identity", lambda r: ident)
    monkeypatch.setattr(ts, "request_staff_identity", lambda r: ident)


def test_the_e2e_admin_lists_reads_and_ends_only_its_own_sessions(world, monkeypatch):
    """S268 follow-up (A10b-2): the Dev-only E2E admin holds the admin role, but its evidence run never lists, reads or
    ends a real member's session: its list is filtered to its own id and another member's session is a 404."""
    client, db, _ = world
    _e2e_admin(monkeypatch)
    asked: list = []
    real_fetch = db.fetch

    async def fetch(sql, *args):
        if sql == va.LIST_SQL:
            asked.append(args)
        return await real_fetch(sql, *args)
    monkeypatch.setattr(db, "fetch", fetch)
    listed = client.get("/admin/view-as/sessions?open_only=true", headers=_h("admin"))
    assert listed.status_code == 200 and listed.json()["scope"] == "own"
    assert asked and asked[0][0] == STAFF                                        # the staff filter: its own id
    db.session_owner = OTHER_STAFF                                               # a real member's session
    assert client.get(f"/admin/view-as/sessions/{SESSION}", headers=_h("admin")).status_code == 404
    assert client.post(f"/admin/view-as/sessions/{SESSION}/end", headers=_h("admin")).status_code == 404
    assert db.ended == []
    db.session_owner = STAFF                                                     # its own: read and end as usual
    assert client.get(f"/admin/view-as/sessions/{SESSION}", headers=_h("admin")).status_code == 200


def test_a_real_admin_still_sees_and_revokes_every_session(world):
    client, db, _ = world
    db.session_owner = OTHER_STAFF
    assert client.get("/admin/view-as/sessions", headers=_h("admin")).json()["scope"] == "all"
    end = client.post(f"/admin/view-as/sessions/{SESSION}/end", headers=_h("admin"))
    assert end.status_code == 200 and db.ended == [(uuid.UUID(SESSION), "revoked")]


def test_verify_tenant_refuses_a_view_as_token_and_still_answers_a_tenant_session(world):
    """S268 follow-up: POST /auth/verify-tenant reads the token from the body, so the gate never sees it; a view-as
    token (vas / amr staff_view_as) is answered 403 "not a tenant session", never valid."""
    client, db, _ = world
    for tok in (token(), token(vas=None, stf=None, srl=None), token(amr=None)):
        res = client.post("/auth/verify-tenant", json={"token": tok})
        assert res.status_code == 403 and res.json()["detail"] == view_as.NOT_A_TENANT_SESSION, tok
    plain = token(vas=None, stf=None, srl=None, amr=None, csr=None)
    ok = client.post("/auth/verify-tenant", json={"token": plain})
    assert ok.status_code == 200 and ok.json() == {"valid": True, "tenant_id": MINE, "name": "Quill Harbor",
                                                   "plan_tier": "starter"}
    assert not any(a[2] == view_as.AUDIT_ACTION_REQUEST for a in db.audit)


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
    # every read carries the member's verified role (the gate ends a session whose role changed since the start)
    assert f'export const VIEW_AS_ROLE_HEADER = "{view_as.ROLE_HINT_HEADER}";' in src
    reads = (Path(__file__).resolve().parents[2] / "frontend" / "app" / "api" / "view-as" / "v1" / "[...path]"
             / "route.ts").read_text()
    assert "[VIEW_AS_ROLE_HEADER]: auth.role" in reads
    assert "viewAsForward(backendPath, auth.role, claims.startRole)" in reads
    roles = re.search(r"export const VIEW_AS_ROLES = \[(.*?)\] as const;", src, re.S)
    assert set(re.findall(r'"([a-z_]+)"', roles.group(1))) == {
        r for r, perms in staff_authz.ROLE_PERMS.items() if staff_authz.TENANT_VIEW_AS in perms}
