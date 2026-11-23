"""AA-1157 (S288) — read-only "view as a lower staff role" for an owner (api/core/staff_view_as.py, the staff bearer
middleware, api/middleware/staff_view_as.py, POST /admin/view-as/roles).

- downward (tech lead Q6): the target's read surface is a subset of the viewer's; admin covers every built-in role and
  the seeded custom role; content_lead -> admin and finance -> ai_ops are refused;
- the token: verifies only with its own audience (the tenant verify_jwt refuses it), expiry and type;
- resolve(): own open session -> the request runs as the target role (capabilities target ∩ viewer, is_owner False);
  a foreign token -> 403, nothing closed; an expired / owner-lost session -> closed with one staff.view_as_ended row;
- the whole app: EVERY non-GET staff route (walked from app.routes, so routes added later are covered) answers 403
  view_as_read_only under a view, before the route, and the staff audit row of the refusal names the session; the
  view-as header on a non-Cognito request is refused; GET /admin/session says read_only / view_as;
- the start: non-owner, legacy session, not the BFF, unknown role, not downward are refused; an owner's start writes
  the row and one staff.view_as_started row with the target role's FULL capability list;
- the ended audit rows by kind (sign-out / new start closing both kinds), the CRM relay suppressed during a view.
"""
from __future__ import annotations

import json
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from api.core import staff_authz, staff_identity, staff_roles, staff_view_as, view_as
from api.core.staff_authz import ROLE_PERMS, STAFF_PATH_PREFIXES, principal_from_role
from api.core.staff_identity import StaffIdentity
from api.routers import admin
from api.routers import admin_view_as as va
from services.acp_shared.audit_log import StaffViewAsAuditAction
from tests.unit.test_s241_admin_routes_require_auth import _served_routes, _walk

SECRET = "aa1157-unit-admin-secret"
OWNER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
SESSION = "33333333-3333-4333-8333-333333333333"
OLD_SESSION = "44444444-4444-4444-8444-444444444444"
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
FAKE_TOKEN = "cognito-access-token-stand-in"


def _identity(*, role="admin", is_owner=True, caps=None, admin_id=OWNER, **kw) -> StaffIdentity:
    return StaffIdentity(
        admin_id=admin_id, username="owner", role=role, db_role=role, email="owner@adventure.asia",
        display_name="Owner Example", iss="https://issuer.example", sub="sub-1", client_id="cis-admin",
        is_owner=is_owner, capabilities=frozenset(ROLE_PERMS[role] if caps is None else caps), **kw)


class FakeDb:
    """Pool and connection at once: one role session SESSION of OWNER, audit rows recorded."""

    def __init__(self) -> None:
        self.state = {"open": True, "not_ended": True, "expires_at": NOW + timedelta(minutes=30)}
        self.view_role = "content"
        self.owner_row = {"is_active": True, "is_owner": True}
        self.others: list[dict] = []
        self.inserted: list[tuple] = []
        self.closed: list[tuple] = []
        self.audit: list[dict] = []
        self.custom = {"custom_content": ("Content", staff_roles.content_seed_capabilities(), False)}
        self.custom_error = False          # the custom-role read fails (a transient database error)
        self.one_rows: list[dict] = []     # successive answers of ONE_SQL (the session read)
        self.executed: list[str] = []      # every execute() SQL, in order (with fetch markers)
        self.expired_own: list[dict] = []  # own role sessions past expiry without an end (CLOSE_EXPIRED_OWN_SQL)
        self.expired_args: list[tuple] = []

    def acquire(self):
        db = self

        @asynccontextmanager
        async def ctx():
            yield db
        return ctx()

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if sql == staff_view_as.STATE_SQL:
            if args == (SESSION, OWNER, self.view_role):
                return dict(self.state)
            return None
        if sql == staff_view_as.CLOSE_SQL:
            self.closed.append(args)
            return {"end_reason": "expired" if not self.state["open"] else args[1], "view_role": self.view_role}
        if sql == staff_roles.SQL_CIS_ROLE:
            if self.custom_error:
                raise ConnectionError("database hiccup")
            hit = self.custom.get(args[0])
            if hit is None:
                return None
            label, caps, archived = hit
            return {"code": args[0], "label": label, "based_on": "content_lead", "archived": archived,
                    "capabilities": sorted(caps)}
        if sql == va.OWNER_STATE_SQL:
            return dict(self.owner_row)
        if sql == va.INSERT_ROLE_SQL:
            self.inserted.append(args)
            return {"session_id": SESSION, "started_at": NOW, "expires_at": NOW + timedelta(minutes=args[3])}
        if sql == va.ONE_SQL:
            return self.one_rows.pop(0) if self.one_rows else None
        raise AssertionError(f"unexpected fetchrow {sql}")

    async def fetch(self, sql: str, *args: Any) -> list:
        if sql == va.END_OTHERS_SQL:
            self.executed.append("END_OTHERS")
            return [dict(r) for r in self.others]
        if sql == view_as.CLOSE_STAFF_SESSIONS_SQL:
            return [dict(r) for r in self.others]
        if sql == staff_view_as.CLOSE_EXPIRED_OWN_SQL:
            self.expired_args.append(args)
            rows, self.expired_own = self.expired_own, []
            return rows
        if sql == va.LIST_SQL:
            return []
        raise AssertionError(f"unexpected fetch {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append(sql)
        if "acp_shared.audit_log" in sql:
            row = {"tenant_id": args[0], "actor": args[1], "action": args[2], "resource_type": args[3],
                   "resource_id": args[4], "details": json.loads(args[5])}
            if len(args) > 6:
                row.update({"http_method": args[7], "route": args[8], "status": args[9]})
            self.audit.append(row)
            return "INSERT 0 1"
        return "OK"


def _actions(db: FakeDb, action: str) -> list[dict]:
    return [r for r in db.audit if r["action"] == action]


# ── downward ─────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("target", sorted(ROLE_PERMS))
def test_admin_covers_every_built_in_role(target):
    ok, missing = staff_view_as.downward(ROLE_PERMS["admin"], ROLE_PERMS[target])
    assert ok, missing


def test_admin_covers_the_seeded_custom_role():
    assert staff_view_as.downward(ROLE_PERMS["admin"], staff_roles.content_seed_capabilities())[0]


@pytest.mark.parametrize("viewer,target", [("content_lead", "admin"), ("finance", "ai_ops"), ("content", "tenant_qa"),
                                           ("support", "customer_success")])
def test_a_wider_target_is_not_downward(viewer, target):
    ok, missing = staff_view_as.downward(ROLE_PERMS[viewer], ROLE_PERMS[target])
    assert not ok and missing


def _gates_a_read(cap: str) -> bool:
    return staff_view_as.is_read_cap(cap) or any(
        m in ("GET", "HEAD") and cap in staff_authz.policy_capabilities(e) for (m, _), e in staff_authz.POLICY.items())


def test_write_only_capabilities_do_not_count_only_reads():
    # a capability that gates no read (no GET route, not a read capability) never makes a target "wider": a view
    # never writes. A capability that gates a GET route (wave.run opens the waves page) does count.
    write_only = sorted(c for c in staff_authz.CAPABILITIES - staff_authz.OWNER_PERMS if not _gates_a_read(c))
    assert write_only
    assert staff_view_as.downward(ROLE_PERMS["content"], ROLE_PERMS["content"] | set(write_only))[0]
    assert not staff_view_as.downward(ROLE_PERMS["content"], ROLE_PERMS["content"] | {"spend.read"})[0]
    assert not staff_view_as.downward(ROLE_PERMS["content"], ROLE_PERMS["content"] | {"wave.run"})[0]


def test_owner_only_routes_are_never_part_of_the_surface():
    surface = staff_view_as.read_surface(staff_authz.CAPABILITIES)
    assert not any("/admin/view-as/roles" in s for s in surface)


# ── token ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _token(**over) -> str:
    kw = dict(session_id=SESSION, staff_id=OWNER, view_role="content", capabilities=["content.read"],
              expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
    kw.update(over)
    return staff_view_as.mint_token(**kw)


def test_token_round_trip_and_tenant_verify_refuses_it():
    from api.routers.auth import verify_jwt

    tok = _token()
    claims = staff_view_as.verify_token(tok)
    assert claims == staff_view_as.TokenClaims(SESSION, OWNER, "content", ("content.read",))
    with pytest.raises(HTTPException):
        verify_jwt(tok)                                   # its audience: never a tenant token


def test_expired_foreign_or_bad_tokens_do_not_verify():
    import jwt as pyjwt

    from api.routers.auth import JWT_ALG, JWT_SECRET

    assert staff_view_as.verify_token(_token(expires_at=datetime.now(timezone.utc) - timedelta(seconds=5))) is None
    assert staff_view_as.verify_token("not-a-jwt") is None
    tenant_like = pyjwt.encode({"svs": SESSION, "stf": OWNER, "vrl": "content", "vcp": [], "typ": "staff_view_as",
                                "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, JWT_SECRET, JWT_ALG)
    assert staff_view_as.verify_token(tenant_like) is None   # no audience
    other_type = pyjwt.encode({"svs": SESSION, "stf": OWNER, "vrl": "content", "vcp": [], "typ": "x",
                               "aud": staff_view_as.AUDIENCE,
                               "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, JWT_SECRET, JWT_ALG)
    assert staff_view_as.verify_token(other_type) is None


# ── resolve ──────────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_resolve_swaps_to_the_target_role():
    db = FakeDb()
    swapped, session = await staff_view_as.resolve(db, _identity(), _token())
    assert (swapped.role, swapped.is_owner, swapped.admin_id) == ("content", False, OWNER)
    assert swapped.capabilities == ROLE_PERMS["content"] & ROLE_PERMS["admin"]
    assert session.session_id == SESSION and session.viewer_role == "admin" and swapped.view_as is session
    assert not db.audit and not db.closed


@pytest.mark.asyncio
async def test_resolve_custom_role_keeps_its_label():
    db = FakeDb()
    db.view_role = "custom_content"
    swapped, session = await staff_view_as.resolve(db, _identity(), _token(view_role="custom_content"))
    assert swapped.role == "custom_content" and swapped.custom_label == "Content" and session.label == "Content"
    assert swapped.capabilities == staff_roles.content_seed_capabilities() & ROLE_PERMS["admin"]


@pytest.mark.asyncio
async def test_a_foreign_token_is_refused_and_closes_nothing():
    db = FakeDb()
    with pytest.raises(staff_view_as.StaffViewAsRefused) as exc:
        await staff_view_as.resolve(db, _identity(admin_id=OTHER), _token())
    assert exc.value.status == 403 and not db.closed and not db.audit


@pytest.mark.asyncio
async def test_an_expired_session_is_closed_once_with_a_staff_ended_row():
    db = FakeDb()
    db.state = {"open": False, "not_ended": True, "expires_at": NOW}
    with pytest.raises(staff_view_as.StaffViewAsRefused) as exc:
        await staff_view_as.resolve(db, _identity(), _token())
    assert (exc.value.status, exc.value.detail) == (409, staff_view_as.ENDED_DETAIL)
    ended = _actions(db, StaffViewAsAuditAction.ENDED)
    assert len(ended) == 1 and ended[0]["tenant_id"] is None and ended[0]["details"]["cause"] == "expired"
    assert ended[0]["details"]["view_role"] == "content"


@pytest.mark.asyncio
async def test_an_ended_session_is_refused_without_a_new_row():
    db = FakeDb()
    db.state = {"open": False, "not_ended": False, "expires_at": NOW}
    with pytest.raises(staff_view_as.StaffViewAsRefused):
        await staff_view_as.resolve(db, _identity(), _token())
    assert not db.audit


@pytest.mark.asyncio
@pytest.mark.parametrize("who,cause", [(dict(is_owner=False), "owner_lost"),
                                       (dict(role="content_lead"), "owner_lost"),
                                       (dict(role="custom_ops", caps=ROLE_PERMS["admin"]), "owner_lost"),
                                       (dict(caps=ROLE_PERMS["admin"] - {"catalog.read"}), "not_downward")])
async def test_a_lost_right_ends_the_session(who, cause):
    db = FakeDb()
    with pytest.raises(staff_view_as.StaffViewAsRefused) as exc:
        await staff_view_as.resolve(db, _identity(**who), _token())
    assert exc.value.status == 409
    ended = _actions(db, StaffViewAsAuditAction.ENDED)
    assert len(ended) == 1 and ended[0]["details"]["cause"] == cause and db.closed[0][1] == "revoked"


@pytest.mark.asyncio
async def test_an_unreadable_custom_role_answers_503_and_keeps_the_session_open():
    """A transient read error of the custom role is not a revocation: 503, nothing closed, no ended row."""
    db = FakeDb()
    db.view_role = "custom_content"
    db.custom_error = True
    with pytest.raises(staff_view_as.StaffViewAsRefused) as exc:
        await staff_view_as.resolve(db, _identity(), _token(view_role="custom_content"))
    assert (exc.value.status, exc.value.reason) == (503, "role_unreadable")
    assert not db.closed and not db.audit
    db.custom_error = False                                     # the next request works again
    swapped, _ = await staff_view_as.resolve(db, _identity(), _token(view_role="custom_content"))
    assert swapped.role == "custom_content"


@pytest.mark.asyncio
async def test_an_archived_or_unknown_custom_role_still_ends_the_session():
    db = FakeDb()
    db.view_role = "custom_content"
    db.custom["custom_content"] = ("Content", staff_roles.content_seed_capabilities(), True)
    with pytest.raises(staff_view_as.StaffViewAsRefused) as exc:
        await staff_view_as.resolve(db, _identity(), _token(view_role="custom_content"))
    assert exc.value.status == 409
    assert _actions(db, StaffViewAsAuditAction.ENDED)[0]["details"]["cause"] == "role_unavailable"


def test_only_the_built_in_admin_role_may_start():
    """An owner on a custom role that holds staff.read (custom_allowed() keeps it) never starts a view: the BFF honours
    the view cookie only on the built-in admin role, so the API must use the same rule (else the banner would say
    read-only while every call ran with the owner's own rights)."""
    assert staff_view_as.may_start(_identity())
    custom = _identity(role="custom_ops", caps={staff_authz.STAFF_READ, "content.read"})
    assert staff_authz.STAFF_READ in custom.capabilities and not staff_view_as.may_start(custom)
    assert not staff_view_as.may_start(_identity(role="custom_ops", caps=ROLE_PERMS["admin"]))
    assert not staff_view_as.may_start(_identity(caps=ROLE_PERMS["admin"] - {staff_authz.STAFF_READ}))


# ── authz pieces ─────────────────────────────────────────────────────────────────────────────────────────────────
def _viewing(role="content", caps=None) -> StaffIdentity:
    view = staff_view_as.StaffViewAs(session_id=SESSION, view_role=role, label=role, viewer_role="admin")
    return _identity(role=role, is_owner=False, caps=caps, view_as=view)


def test_principal_uses_the_swapped_capabilities_for_a_built_in_role():
    narrowed = ROLE_PERMS["content"] - {"review.act"}
    p = principal_from_role("content", _viewing(caps=narrowed))
    assert p.perms == narrowed and p.view_as_session == SESSION and p.is_owner is False


def test_role_perms_reads_the_swapped_identity():
    narrowed = ROLE_PERMS["content"] - {"review.act"}
    reset = staff_identity.current_staff.set(_viewing(caps=narrowed))
    try:
        assert staff_roles.role_perms("content") == narrowed
        assert staff_roles.role_perms("admin") == ROLE_PERMS["admin"]     # another role: the built-in table
    finally:
        staff_identity.current_staff.reset(reset)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_denial_reason_refuses_every_write_in_a_view(method):
    p = principal_from_role("content", _viewing())
    assert staff_authz.denial_reason(p, method, "/admin/upload") == staff_authz.VIEW_AS_READ_ONLY
    assert staff_authz.denial_reason(p, "GET", "/admin/llm-config") is None


def test_session_hint_says_view_as():
    from api.routers.admin_session import session_hint

    hint = session_hint(principal_from_role("content", _viewing()))
    assert (hint.role, hint.read_only, hint.reason) == ("content", True, "view_as")


def test_the_crm_relay_never_runs_in_a_view():
    from types import SimpleNamespace

    from api.services import platform_apps

    view = staff_view_as.StaffViewAs(session_id=SESSION, view_role="content", label="Content", viewer_role="admin")
    req = SimpleNamespace(scope={"state": {"staff_view_as": view, "staff_access_token": "tok"}},
                          headers={"authorization": "Bearer tok"})
    assert platform_apps.relay_authorization(req) is None
    req = SimpleNamespace(scope={"state": {"staff_access_token": "tok"}}, headers={})
    assert platform_apps.relay_authorization(req) == "Bearer tok"
    assert platform_apps._cognito_bearer(None) is None     # every relay then answers "unavailable", never a 500


@pytest.mark.asyncio
async def test_sign_out_closes_both_kinds_with_the_right_actions():
    db = FakeDb()
    db.others = [
        {"session_id": SESSION, "tenant_id": None, "session_staff": OWNER, "end_reason": "revoked",
         "view_role": "content"},
        {"session_id": OLD_SESSION, "tenant_id": str(uuid.uuid4()), "session_staff": OWNER, "end_reason": "revoked",
         "view_role": None},
    ]
    await view_as.close_open_sessions(db, actor=f"admin:{OWNER}", cause="staff_signed_out", staff_id=OWNER)
    assert [r["action"] for r in db.audit] == ["staff.view_as_ended", "tenant.view_as_ended"]
    assert db.audit[0]["tenant_id"] is None and db.audit[1]["tenant_id"] is not None


# ── the whole app ────────────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def world(monkeypatch):
    from api.main import app

    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    db = FakeDb()
    monkeypatch.setattr(app.state, "pool", db, raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    who = {"identity": _identity()}

    async def authenticate(_pool, token, **_kw):
        assert token == FAKE_TOKEN
        return who["identity"]

    real_kind = staff_identity.token_kind
    monkeypatch.setattr(staff_identity, "authenticate", authenticate)
    monkeypatch.setattr(staff_identity, "token_kind",
                        lambda t: staff_identity.AUTH_COGNITO if t == FAKE_TOKEN else real_kind(t))
    return TestClient(app, raise_server_exceptions=False), db, app, who


def _view_headers(token: str | None = None) -> dict:
    return {"Authorization": f"Bearer {FAKE_TOKEN}", staff_view_as.HEADER: token or _token()}


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "77777777-7777-4777-8777-777777777777", path)


def _staff_writes(app) -> list[tuple[str, str]]:
    keys = set()
    for path, route, _ in _walk(_served_routes(app)):
        if isinstance(route, APIRoute) and any(path == p or path.startswith(p + "/") for p in STAFF_PATH_PREFIXES):
            keys |= {(m, path) for m in route.methods - {"GET", "HEAD", "OPTIONS"}}
    return sorted(keys)


def test_every_staff_write_is_refused_in_a_view_before_the_route(world):
    """Walked from app.routes: a route added later (any app change) is covered without editing this test."""
    client, db, app, _ = world
    writes = _staff_writes(app)
    assert len(writes) > 150 and ("POST", "/admin/view-as/roles") in writes
    headers = _view_headers()
    for method, path in writes:
        r = client.request(method, _concrete(path), headers=headers, json={})
        assert (r.status_code, r.json().get("detail")) == (403, staff_view_as.READ_ONLY_DETAIL), (method, path)
    assert not db.inserted and not db.closed
    # every refusal is a staff.request row naming the session (status 403)
    refused = _actions(db, "staff.request")
    assert len(refused) == len(writes)
    assert all(r["status"] == 403 and r["details"]["view_as"] == SESSION and r["details"]["role"] == "content"
               for r in refused)


def test_reads_run_as_the_view_role(world):
    client, *_ = world
    r = client.get("/admin/session", headers=_view_headers())
    assert r.status_code == 200 and r.json() == {"role": "content", "read_only": True, "reason": "view_as"}
    plain = client.get("/admin/session", headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    assert plain.json()["role"] == "admin" and plain.json()["read_only"] is False


def test_the_view_role_cannot_read_what_it_lacks(world):
    client, *_ = world
    r = client.get("/admin/platform/staff", headers=_view_headers())     # staff.read: admin only
    assert r.status_code == 403


def test_the_header_without_a_cognito_session_is_refused(world):
    client, *_ = world
    r = client.get("/admin/session", headers={"X-Admin-Secret": SECRET, "x-admin-role": "admin",
                                              staff_view_as.HEADER: _token()})
    assert (r.status_code, r.json()["detail"]) == (403, staff_view_as.NEEDS_COGNITO_DETAIL)


def test_an_ended_view_answers_409(world):
    client, db, *_ = world
    db.state = {"open": False, "not_ended": False, "expires_at": NOW}
    r = client.get("/admin/session", headers=_view_headers())
    assert (r.status_code, r.json()["detail"]) == (409, staff_view_as.ENDED_DETAIL)


# ── the start ────────────────────────────────────────────────────────────────────────────────────────────────────
BODY = {"role": "content", "reason": "Check what this role sees", "minutes": 15}


def _start(client, *, bff=True, body=None, headers=None):
    h = headers or {"Authorization": f"Bearer {FAKE_TOKEN}"}
    if bff:
        h = {**h, view_as.BFF_HEADER: view_as.BFF_HEADER_VALUE}
    return client.post("/admin/view-as/roles", headers=h, json=body or BODY)


def test_owner_start_writes_the_row_and_the_full_capability_list(world):
    client, db, _, who = world
    db.others = [{"session_id": OLD_SESSION, "tenant_id": str(uuid.uuid4()), "end_reason": "ended",
                  "view_role": None}]
    r = _start(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["view_role"] == "content" and body["kind"] == "role" and body["read_only"] is True
    claims = staff_view_as.verify_token(body["token"])
    assert claims.session_id == SESSION and claims.staff_id == OWNER and claims.view_role == "content"
    assert db.inserted == [(OWNER, "content", "Check what this role sees", 15)]
    started = _actions(db, StaffViewAsAuditAction.STARTED)
    assert len(started) == 1 and started[0]["tenant_id"] is None
    d = started[0]["details"]
    assert d["capabilities"] == sorted(ROLE_PERMS["content"]) and d["viewer_role"] == "admin"
    assert d["view_role"] == "content" and d["ended_sessions"] == [OLD_SESSION]
    assert "Check what this role sees" not in json.dumps(d)
    assert len(_actions(db, "tenant.view_as_ended")) == 1      # the replaced tenant session, under its own kind


def test_the_start_takes_the_member_lock_before_ending_other_sessions(world):
    client, db, *_ = world
    assert _start(client).status_code == 201
    lock = next(i for i, q in enumerate(db.executed) if q == va.START_LOCK_SQL)
    assert lock < db.executed.index("END_OTHERS")


def test_a_start_on_an_unreadable_custom_role_answers_503(world):
    client, db, *_ = world
    db.custom_error = True
    r = _start(client, body={**BODY, "role": "custom_content"})
    assert (r.status_code, r.json()["detail"]) == (503, staff_view_as.UNAVAILABLE_DETAIL)
    assert not db.inserted and not [r for r in db.audit if r["action"].startswith("staff.view_as")]


def _session_row(**over) -> dict:
    row = {"session_id": SESSION, "admin_user_id": OWNER, "tenant_id": None, "tenant_name": None,
           "tenant_slug": None, "staff_name": "Owner Example", "reason": "Check", "ticket_ref": None,
           "started_at": NOW - timedelta(minutes=31), "expires_at": NOW - timedelta(minutes=1), "ended_at": None,
           "end_reason": None, "open": False, "view_role": "content"}
    return {**row, **over}


def test_the_owner_reading_an_expired_role_session_closes_it_with_its_end_row(world):
    """The banner's check at 0 minutes (through the BFF, as the owner): the end row is written then, not at the next
    start or sign-out."""
    client, db, *_ = world
    db.one_rows = [_session_row(), _session_row(ended_at=NOW, end_reason="expired")]
    r = client.get(f"/admin/view-as/sessions/{SESSION}", headers={"Authorization": f"Bearer {FAKE_TOKEN}"})
    assert r.status_code == 200, r.text
    assert r.json()["end_reason"] == "expired" and r.json()["open"] is False
    ended = _actions(db, StaffViewAsAuditAction.ENDED)
    assert len(ended) == 1 and ended[0]["details"]["cause"] == "expired" and ended[0]["actor"] == f"admin:{OWNER}"


def test_the_session_list_closes_the_members_expired_role_sessions(world):
    """A view whose cookie is gone (left to time out): the member's session list, which the BFF's sign-out reads
    first, ends it with its end row; a second read finds nothing more to close."""
    client, db, *_ = world
    gone = "88888888-8888-4888-8888-888888888888"
    db.expired_own = [{"session_id": gone, "view_role": "content"}]
    headers = {"Authorization": f"Bearer {FAKE_TOKEN}"}
    assert client.get("/admin/view-as/sessions?open_only=true", headers=headers).status_code == 200
    assert client.get("/admin/view-as/sessions?open_only=true", headers=headers).status_code == 200
    assert db.expired_args == [(OWNER,), (OWNER,)]
    (ended,) = _actions(db, StaffViewAsAuditAction.ENDED)
    assert ended["resource_id"] == gone and ended["details"]["cause"] == "expired" and ended["actor"] == f"admin:{OWNER}"
    assert ended["tenant_id"] is None


@pytest.mark.asyncio
async def test_closing_expired_sessions_is_best_effort():
    class Broken(FakeDb):
        async def fetch(self, sql: str, *args: Any) -> list:
            raise ConnectionError("database hiccup")

    assert await staff_view_as.close_expired_own(Broken(), OWNER) == []


def test_reading_an_open_or_ended_session_writes_nothing(world):
    client, db, *_ = world
    db.one_rows = [_session_row(open=True, expires_at=NOW + timedelta(minutes=5))]
    assert client.get(f"/admin/view-as/sessions/{SESSION}",
                      headers={"Authorization": f"Bearer {FAKE_TOKEN}"}).status_code == 200
    db.one_rows = [_session_row(ended_at=NOW, end_reason="ended")]
    assert client.get(f"/admin/view-as/sessions/{SESSION}",
                      headers={"Authorization": f"Bearer {FAKE_TOKEN}"}).status_code == 200
    assert not db.closed and not db.audit


def test_custom_role_start(world):
    client, db, *_ = world
    r = _start(client, body={**BODY, "role": "custom_content"})
    assert r.status_code == 201, r.text
    d = _actions(db, StaffViewAsAuditAction.STARTED)[0]["details"]
    assert d["view_label"] == "Content" and d["capabilities"] == sorted(staff_roles.content_seed_capabilities())


def test_a_non_owner_admin_cannot_start(world):
    client, db, _, who = world
    who["identity"] = _identity(is_owner=False)
    r = _start(client)
    assert (r.status_code, r.json()["detail"]) == (403, staff_authz.VIEW_AS_ROLE_NEEDS_OWNER)
    assert not db.inserted and not db.audit or all(a["action"] == "staff.request" for a in db.audit)


def test_a_business_owner_without_the_owner_flag_cannot_start(world):
    client, db, _, who = world
    who["identity"] = _identity(is_owner=False, role="admin")
    assert _start(client).status_code == 403


def test_a_legacy_session_cannot_start(world):
    client, db, *_ = world
    r = _start(client, headers={"X-Admin-Secret": SECRET, "x-admin-role": "admin", "x-admin-user-id": OWNER})
    assert (r.status_code, r.json()["detail"]) == (403, staff_authz.VIEW_AS_ROLE_NEEDS_COGNITO)


def test_only_the_bff_may_start(world):
    client, db, *_ = world
    r = _start(client, bff=False)
    assert (r.status_code, r.json()["detail"]) == (403, view_as.BFF_ONLY_DETAIL) and not db.inserted


def test_an_unknown_role_is_refused(world):
    client, db, *_ = world
    r = _start(client, body={**BODY, "role": "custom_nothing"})
    assert (r.status_code, r.json()["detail"]) == (404, staff_view_as.UNKNOWN_ROLE_DETAIL) and not db.inserted


def test_a_wider_target_is_refused_without_naming_a_capability(world):
    client, db, _, who = world
    who["identity"] = _identity(caps=ROLE_PERMS["admin"] - {"spend.read"})
    r = _start(client, body={**BODY, "role": "finance"})
    assert (r.status_code, r.json()["detail"]) == (403, staff_view_as.NOT_DOWNWARD_DETAIL) and not db.inserted


def test_an_owner_row_lost_in_the_transaction_is_refused(world):
    client, db, *_ = world
    db.owner_row = {"is_active": True, "is_owner": False}
    assert _start(client).status_code == 403 and not db.inserted


def test_starting_inside_a_view_is_refused(world):
    client, db, *_ = world
    r = client.post("/admin/view-as/roles", headers={**_view_headers(), view_as.BFF_HEADER: "bff"}, json=BODY)
    assert (r.status_code, r.json()["detail"]) == (403, staff_view_as.READ_ONLY_DETAIL)


def test_the_gate_middleware_alone():
    import asyncio

    from api.middleware.staff_view_as import StaffViewAsGateMiddleware

    reached: list[str] = []

    async def inner(scope, receive, send):
        reached.append(scope["method"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    gate = StaffViewAsGateMiddleware(inner)
    view = staff_view_as.StaffViewAs(session_id=SESSION, view_role="content", label="Content", viewer_role="admin")
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b""}

    for method in ("GET", "HEAD", "OPTIONS", "POST", "PATCH", "PUT", "DELETE"):
        asyncio.run(gate({"type": "http", "method": method, "path": "/anything", "headers": [],
                          "state": {"staff_view_as": view}}, receive, send))
    assert reached == ["GET", "HEAD", "OPTIONS"]
    statuses = [m["status"] for m in sent if m["type"] == "http.response.start"]
    assert statuses == [200, 200, 200, 403, 403, 403, 403]
