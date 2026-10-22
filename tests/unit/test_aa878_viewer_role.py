"""AA-878 (step C1) — the read-only staff role 'viewer' (CIS migration 249) for the UI-smoke E2E account.

Covers:
- the capability map: viewer holds content.read only; it is a staff role everywhere the code lists them
  (staff_authz, the legacy auth whitelist, frontend/lib/rbac.ts) but never an assignable one;
- the decision table: viewer is refused on EVERY non-GET POLICY route (even the notification PUTs, whose
  capability it holds) and on every admin-only GET, allowed on the open GETs; a write with no POLICY entry is
  refused too (READ_ONLY_ROLES);
- the real app: viewer gets 403 before the endpoint on every live POLICY write and admin-only read, and the open
  read GET /admin/llm-config answers 200;
- legacy login: /auth/verify-admin accepts a viewer row; POST /auth/booking-token answers 403 for it before any
  signing key is read;
- role-filtered open reads: the notification bell and the ⌘K search treat viewer like reviewer/content;
- the staff directory: viewer is not assignable (service + request models), a viewer row is listed and counted,
  a role change away from viewer never touches a cis-viewer Cognito group, the reconcile report serialises it;
- scripts/create_staff_user.py is retired (AA-816 D1: the Staff page replaced it);
- frontend/lib/rbac.ts + lib/auth-server.ts: the BFF refuses viewer writes centrally;
- migration 249: one transaction, ADD VALUE only, the new value never used in it.
"""
from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.core import staff_authz
from api.core.staff_authz import (
    AWAITING_ROUTE,
    CONTENT_READ,
    NO_BOOKING_ROLES,
    POLICY,
    READ_ONLY_ROLES,
    ROLE_PERMS,
    STAFF_ROLES,
    denial_reason,
    has_perm,
    principal_from_role,
)
from api.main import app, get_pool
from tests.unit.test_aa882_staff_authz import _concrete, _headers, client  # noqa: F401 (pytest fixture)

REPO = Path(__file__).resolve().parents[2]
VIEWER = "viewer"
ADMIN_ID = "6f1c2c4e-8a51-4c1e-9d0b-2f3c4d5e6f71"

_WRITE_KEYS = sorted(k for k in POLICY if k[0] != "GET")
_ADMIN_GET_KEYS = sorted(k for k in POLICY if k[0] == "GET")


# ── Capability map ───────────────────────────────────────────────────────────────────────────────

def test_viewer_holds_content_read_only():
    assert ROLE_PERMS[VIEWER] == frozenset({CONTENT_READ})
    assert VIEWER in STAFF_ROLES
    assert READ_ONLY_ROLES == frozenset({VIEWER})
    assert NO_BOOKING_ROLES == frozenset({VIEWER})


def test_viewer_is_a_staff_role_for_the_legacy_login():
    from api.routers.auth import STAFF_ROLES as AUTH_STAFF_ROLES

    assert set(AUTH_STAFF_ROLES) == set(ROLE_PERMS)


def test_viewer_is_not_assignable_from_the_staff_page():
    from api.routers.admin_platform import ChangeRolesBody, CreateStaffBody
    from api.services import staff_directory

    assert VIEWER not in staff_directory.CIS_ROLES
    assert set(staff_directory.CIS_ROLES) == set(ROLE_PERMS) - {VIEWER}
    with pytest.raises(staff_directory.StaffDirectoryError) as exc:
        staff_directory.require_cis_role(VIEWER)
    assert exc.value.status == 400
    with pytest.raises(ValidationError):
        CreateStaffBody(email="a@adventure.asia", display_name="A", role=VIEWER)
    with pytest.raises(ValidationError):
        ChangeRolesBody(role=VIEWER)


def test_viewer_has_no_cognito_group():
    """cis-viewer is deferred to AA-816 E1: a viewer is a legacy password row only."""
    from api.core.staff_identity import GROUP_ROLES

    assert VIEWER not in {role for _, role in GROUP_ROLES}


# ── Decision table ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", _WRITE_KEYS)
def test_viewer_is_refused_every_policy_write(key):
    reason = denial_reason(principal_from_role(VIEWER), *key)
    assert reason is not None, key


def test_viewer_is_refused_the_notification_puts_although_it_holds_their_capability():
    for key in (("PUT", "/admin/notifications/read-all"), ("PUT", "/admin/notifications/{notif_id}/read")):
        assert POLICY[key] == CONTENT_READ
        assert denial_reason(principal_from_role(VIEWER), *key) == "Role 'viewer' is read-only"
        assert denial_reason(principal_from_role("reviewer"), *key) is None


@pytest.mark.parametrize("key", _ADMIN_GET_KEYS)
def test_viewer_is_refused_every_admin_only_read(key):
    assert denial_reason(principal_from_role(VIEWER), *key) is not None, key


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post"])
def test_read_only_rule_covers_a_write_without_a_policy_entry(method):
    assert denial_reason(principal_from_role(VIEWER), method, "/admin/aa878-no-such-route") is not None
    # the other roles keep the old rule: no entry, no refusal (the route-policy test keeps every write listed)
    assert denial_reason(principal_from_role("reviewer"), method, "/admin/aa878-no-such-route") is None


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_viewer_reads_the_open_routes(method):
    for path in ("/admin/llm-config", "/admin/review-queue", "/admin/notifications", "/admin/search",
                 "/admin/tenants/{tenant_id}/details", "/admin/tours/{tour_id}/detail"):
        assert (method, path) not in POLICY
        assert denial_reason(principal_from_role(VIEWER), method, path) is None


def test_padded_role_header_is_still_read_only():
    assert denial_reason(principal_from_role("  viewer "), "PATCH", "/admin/tours/{tour_id}/trash") is not None


# ── The real app ─────────────────────────────────────────────────────────────────────────────────

_LIVE_REFUSED = sorted(k for k in [*_WRITE_KEYS, *_ADMIN_GET_KEYS] if k not in AWAITING_ROUTE)


@pytest.mark.parametrize("key", _LIVE_REFUSED)
def test_app_refuses_viewer_before_the_endpoint(client, key):  # noqa: F811
    method, path = key
    resp = client.request(method, _concrete(path), json={}, headers=_headers(VIEWER))
    assert resp.status_code == 403, (key, resp.status_code, resp.text)
    assert resp.json()["detail"].startswith("Role 'viewer' ")


def test_app_viewer_reads_llm_config(client, monkeypatch):  # noqa: F811
    from api.routers import admin_llm_ops

    monkeypatch.setattr(admin_llm_ops, "list_stage_configs", AsyncMock(return_value=[]))
    monkeypatch.setattr(admin_llm_ops, "_load_catalog", AsyncMock(return_value=[]))
    resp = client.get("/admin/llm-config", headers=_headers(VIEWER))
    assert resp.status_code == 200, resp.text


# ── Role-filtered open reads ─────────────────────────────────────────────────────────────────────

def test_notification_bell_scope_for_viewer_is_the_content_scope():
    from api.routers.admin import _NOTIF_CONTENT_SCOPE, notification_scope

    assert notification_scope(VIEWER) == _NOTIF_CONTENT_SCOPE
    assert notification_scope("reviewer") == _NOTIF_CONTENT_SCOPE


def test_viewer_search_has_no_tenants_or_jobs():
    assert not has_perm(VIEWER, staff_authz.TENANT_READ)
    assert not has_perm(VIEWER, staff_authz.OPS_READ)
    assert not has_perm(VIEWER, staff_authz.STAFF_READ)
    assert not has_perm(VIEWER, staff_authz.SPEND_READ)


# ── Legacy login + Booking token ─────────────────────────────────────────────────────────────────

class _Conn:
    def __init__(self, row):
        self.row = row

    async def fetchrow(self, sql, *args):
        return self.row


class _Pool:
    def __init__(self, row):
        self.conn = _Conn(row)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


@pytest.fixture
def pool_client():
    import api.main as main_mod

    def make(row):
        fake = _Pool(row)
        app.dependency_overrides[get_pool] = lambda: fake
        main_mod._staff_state_cache.clear()
        return TestClient(app)

    yield make
    app.dependency_overrides.pop(get_pool, None)
    main_mod._staff_state_cache.clear()


def _viewer_row():
    return {"id": ADMIN_ID, "username": "e2e-claude-code-viewer", "role": VIEWER, "email": None,
            "display_name": None}


def test_verify_admin_accepts_a_viewer(pool_client, monkeypatch):
    from api.routers.auth import _create_admin_jwt

    # AA-816 S2: the legacy HS256 path is the rollback only (off by default); this covers it when switched on.
    monkeypatch.setenv("LEGACY_ADMIN_LOGIN_ENABLED", "true")
    c = pool_client(_viewer_row())
    token = _create_admin_jwt(ADMIN_ID, "e2e-claude-code-viewer", VIEWER)
    res = c.post("/auth/verify-admin", json={"token": token})
    assert res.status_code == 200, res.text
    assert res.json()["role"] == VIEWER and res.json()["auth"] == "legacy"


def test_admin_login_returns_the_viewer_role(pool_client):
    import bcrypt

    pw = "aa878-viewer-test-password"
    row = {"id": ADMIN_ID, "username": "e2e-claude-code-viewer", "role": VIEWER, "is_active": True,
           "password_hash": bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=4)).decode()}
    c = pool_client(row)
    res = c.post("/auth/admin-login", json={"username": "e2e-claude-code-viewer", "password": pw})
    assert res.status_code == 200, res.text
    assert res.json()["role"] == VIEWER


@pytest.mark.parametrize("session_role", [VIEWER, "admin"])
def test_booking_token_is_403_for_a_viewer_row(pool_client, monkeypatch, session_role):
    """The DB role decides (a demoted admin session too); refused before any signing key is read."""
    from api.core import booking_jwt
    from api.routers.auth import _create_admin_jwt

    monkeypatch.setattr(booking_jwt, "get_signing_keys_async",
                        AsyncMock(side_effect=AssertionError("no key read for a viewer")))
    c = pool_client(_viewer_row())
    session = _create_admin_jwt(ADMIN_ID, "e2e-claude-code-viewer", session_role)
    res = c.post("/auth/booking-token", headers={"Authorization": f"Bearer {session}"})
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == "This staff role has no access to Booking"


# ── Staff directory + reconcile ──────────────────────────────────────────────────────────────────

def test_staff_list_counts_viewers():
    from api.routers import admin_platform

    assert VIEWER in admin_platform.STAFF_ROLES


@pytest.mark.asyncio
async def test_role_change_away_from_viewer_skips_the_missing_cognito_group(monkeypatch):
    """A viewer row with an email (never created by the Staff page, but possible by hand) changed to reviewer:
    Cognito gets add_group:cis-reviewer only, never remove_group:cis-viewer (no such group)."""
    from api.services import staff_directory

    actor = "00000000-0000-0000-0000-0000000000a1"
    target = {"id": ADMIN_ID, "username": "v", "role": VIEWER, "email": "v@adventure.asia",
              "display_name": "V", "is_active": True, "is_owner": False}
    monkeypatch.setattr(staff_directory, "_require_owner", AsyncMock())
    monkeypatch.setattr(staff_directory, "_load_target", AsyncMock(return_value=target))
    monkeypatch.setattr(staff_directory, "_audit", AsyncMock())
    captured = {}

    async def fake_finish(pool, actor_id, result, steps, has_email):
        captured["steps"] = [name for name, _ in steps]
        return result

    monkeypatch.setattr(staff_directory, "_finish", fake_finish)

    class _TxConn:
        async def fetchval(self, *a):
            return None

        async def execute(self, *a):
            return None

        def transaction(self):
            @asynccontextmanager
            async def tx():
                yield
            return tx()

    class _P:
        @asynccontextmanager
        async def acquire(self):
            yield _TxConn()

    await staff_directory.change_roles(_P(), object(), actor_id=actor, staff_id=ADMIN_ID, role="reviewer")
    assert captured["steps"] == ["add_group:cis-reviewer"]


def test_reconcile_serialises_a_viewer_row():
    """The legacy-only viewer row (no email, no sub) is a readiness exception, never a crash or a drift."""
    from services import staff_reconcile

    rows = [{"id": ADMIN_ID, "username": "e2e-claude-code-viewer", "role": VIEWER, "is_active": True,
             "email": None, "cognito_iss": None, "cognito_sub": None}]
    report = staff_reconcile.reconcile([], {g: [] for g in staff_reconcile.STAFF_GROUPS}, rows, [],
                                       issuer="https://cognito-idp.example/pool", breakglass="bg@adventure.asia")
    json.dumps(report, default=str)
    assert report["readiness"]["missing_email"] == ["e2e-claude-code-viewer"]
    assert report["counts"]["group_drift"] == 0 and report["counts"]["rows_unknown_to_cognito"] == 0


def test_create_staff_user_script_is_retired():
    """AA-816 D1: the Staff page creates staff (Cognito); the legacy password-row script is gone for good."""
    assert not (REPO / "scripts" / "create_staff_user.py").exists()


# ── Frontend: rbac.ts + the BFF ──────────────────────────────────────────────────────────────────

def _rbac() -> str:
    return (REPO / "frontend" / "lib" / "rbac.ts").read_text()


def _ts_list(src: str, name: str) -> list[str]:
    m = re.search(rf"export const {name}[^=]*=\s*\[([^\]]*)\]", src)
    assert m, name
    return re.findall(r'"([a-z_]+)"', m.group(1))


def test_rbac_read_only_and_assignable_roles():
    src = _rbac()
    assert _ts_list(src, "READ_ONLY_ROLES") == sorted(READ_ONLY_ROLES)
    from api.services import staff_directory

    assert _ts_list(src, "ASSIGNABLE_STAFF_ROLES") == list(staff_directory.CIS_ROLES)


def test_rbac_viewer_pages():
    """Viewer reads Master Content, Review and Settings; Upload and S1 (write-only pages) are not its own."""
    src = _rbac()
    rules = dict(re.findall(r'\{ prefix: "(/admin/[a-z0-9\-/]+)", roles: (\w+|\[[^\]]*\]) \}', src))
    assert rules["/admin/master-content"] == rules["/admin/review"] == rules["/admin/settings"] == "ALL_STAFF"
    assert rules["/admin/upload"] == rules["/admin/s1-rewrite"] == "CONTENT_WORKERS"
    assert re.search(r'const ALL_STAFF: StaffRole\[\] = \[[^\]]*"viewer"', src)
    assert not re.search(r'const CONTENT_WORKERS: StaffRole\[\] = \[[^\]]*"viewer"', src)
    for prefix, roles in rules.items():
        if roles.startswith("["):
            assert "viewer" not in roles, prefix      # admin-only pages stay admin-only


def test_bff_refuses_read_only_writes_centrally():
    src = (REPO / "frontend" / "lib" / "auth-server.ts").read_text()
    body = re.search(r"export async function requireAdmin\((.*?)\n\}", src, re.S).group(1)
    assert "isReadOnlyRole(result.role) && !isReadMethod(request.method)" in body
    assert "forbidden()" in body
    assert re.search(r"if \(isReadOnlyRole\(role\)\) return false;", _rbac())   # canWriteAdminPath


def test_staff_page_never_offers_viewer():
    src = (REPO / "frontend" / "app" / "admin" / "platform" / "staff" / "StaffManage.tsx").read_text()
    assert "ASSIGNABLE_STAFF_ROLES.map(" in src
    assert "STAFF_ROLES.map(" not in src.replace("ASSIGNABLE_STAFF_ROLES.map(", "")


# ── Migration 249 ────────────────────────────────────────────────────────────────────────────────

def _sql_body() -> str:
    sql = (REPO / "api" / "migrations" / "249_admin_role_viewer.sql").read_text()
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


def test_migration_249_is_one_transaction_that_only_adds_the_value():
    body = _sql_body()
    assert body.count("BEGIN;") == 1 and body.count("COMMIT;") == 1
    assert body.index("BEGIN;") < body.index("ADD VALUE") < body.index("COMMIT;")
    assert "ALTER TYPE shared.admin_role ADD VALUE IF NOT EXISTS 'viewer';" in body
    assert not re.search(r"\bSET\s+(LOCAL\s+)?ROLE\b", body, re.I)        # role lint (243, owners.yaml)
    # 55P04: the new value can not be used in the transaction that adds it — only the ADD VALUE names it.
    assert body.count("'viewer'") == 1
    assert "VALUES ('249'" in body and "ON CONFLICT (version) DO NOTHING" in body
