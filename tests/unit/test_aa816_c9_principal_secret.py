"""AA-816 C9 — the admin-secret checks are principal-aware, and the Preview credential is capped at viewer.

Plan E1 (root cutover plan) tests, plus: no secret and no principal answers exactly as before, and both secrets are
compared in constant time.

- A verified Cognito Bearer reaches every staff guard WITHOUT X-Admin-Secret: admin.verify_admin_secret (router
  dependency of /admin/platform), admin_settings.verify_admin, v1_pipeline._require_admin, staff_secret
  (/v1/rules, /v1/social), admin_atoms, the brand-identity resolver — and POLICY still applies to its role
  (staff_policy and staff_policy_if_staff).
- The Preview credential (Secrets Manager, code constant name) is always the viewer: a higher x-admin-role is
  lowered, no role header is never the machine, so MACHINE_ROUTES with a POLICY entry and every write refuse it.
- The main secret is unchanged (machine callers, legacy sessions); missing / unreadable Preview secret = off.
- The BFF side is in test_aa816_c9_bff.py.
No network, no AWS, no DB.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from api.core import identity_config, staff_authz, staff_credential, staff_identity
from api.core.staff_authz import MACHINE_ROUTES, POLICY, STAFF_PATH_PREFIXES
from api.main import app as real_app
from api.middleware import rate_limit, staff_audit
from api.middleware.staff_bearer import StaffBearerMiddleware
from api.routers import admin, admin_settings
from api.routers.auth import _create_admin_jwt
from tests.unit.aa854_cognito_staff import ISS, SUB, Directory, install_issuer

REPO = Path(__file__).resolve().parents[2]
SECRET = "aa816-c9-main-admin-secret"
PREVIEW = "aa816-c9-preview-credential"
USER = "11111111-1111-1111-1111-111111111111"


# ── Fixtures ─────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def issuer():
    iss, _ = install_issuer()
    yield iss
    identity_config.reset_cis_staff_verifier()
    staff_identity.clear_cache()


class _Pool(Directory):
    """The staff directory plus empty answers for the few route queries these tests reach."""

    def __init__(self) -> None:
        super().__init__()
        self.route_args: list = []

    async def fetchrow(self, sql, *args):
        if "tenant_brand_rules" in sql:
            self.route_args.append(args)
            return None
        return await super().fetchrow(sql, *args)

    async def fetch(self, sql, *args):
        if "raw_sources" in sql:
            self.route_args.append(args)
            return []
        return await super().fetch(sql, *args)


@pytest.fixture
def pool():
    return _Pool()


@pytest.fixture
def fetches():
    """The Preview fetcher's call log; the fetcher returns PREVIEW unless a test swaps it."""
    return []


@pytest.fixture
def real(monkeypatch, pool, issuer, fetches):
    monkeypatch.setenv("ADMIN_SECRET", SECRET)               # the /v1 guards + rate limiter read it per call
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(real_app.state, "pool", pool, raising=False)
    monkeypatch.setattr(rate_limit, "track_api_call", AsyncMock())
    monkeypatch.setattr("services.acp_shared.audit_log.write_audit_log", AsyncMock())
    monkeypatch.setenv("AWS_EXECUTION_ENV", "AWS_ECS_FARGATE")  # the deployed-API signal: Preview read enabled

    def fetch():
        fetches.append(1)
        return PREVIEW

    staff_credential.reset_preview_cache(fetch)
    yield TestClient(real_app)
    staff_credential.reset_preview_cache(staff_credential._fetch_preview_secret)


def _cognito(issuer, *groups: str) -> dict:
    claims = {"cognito:groups": list(groups)} if groups else {}
    return {"Authorization": f"Bearer {issuer.token(**claims)}"}


def _preview(role: str | None = "admin") -> dict:
    h = {"X-Admin-Secret": PREVIEW, "x-admin-user-id": USER}
    if role is not None:
        h["x-admin-role"] = role
    return h


def _main(role: str | None = None) -> dict:
    h = {"X-Admin-Secret": SECRET}
    if role is not None:
        h["x-admin-role"] = role
    return h


def _concrete(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: "00000000-0000-0000-0000-000000000000"
                  if m.group(1).endswith("_id") else "aa816c9", path)


# ── Cognito principal, no secret ─────────────────────────────────────────────────────────────────

def test_cognito_admin_without_the_secret_reaches_an_admin_only_route(real, pool, issuer):
    uid = pool.add(iss=ISS, sub=SUB)
    with capture_logs() as logs:
        res = real.get("/admin/platform/staff", headers=_cognito(issuer))
    assert res.status_code == 200, res.text
    assert res.json()["total"] == 1
    assert any(e["event"] == "staff_bearer_verified" and e["admin_id"] == uid for e in logs)


def test_cognito_without_the_secret_is_still_bound_by_policy(real, pool, issuer):
    pool.add(role="content", iss=ISS, sub=SUB)
    res = real.get("/admin/platform/staff", headers=_cognito(issuer, "cis-content"))
    assert res.status_code == 403
    assert res.json()["detail"] == "Role 'content' lacks the 'staff.read' permission"


def test_cognito_without_the_secret_on_v1_pipeline(real, pool, issuer):
    pool.add(iss=ISS, sub=SUB)
    res = real.get("/v1/pipeline/sources", headers=_cognito(issuer))
    assert res.status_code == 200, res.text
    assert pool.route_args and pool.route_args[0][0] == "00000000-0000-0000-0000-000000000001"


def test_cognito_without_the_secret_on_admin_atoms_goes_through_policy(real, pool, issuer):
    pool.add(role="reviewer", iss=ISS, sub=SUB)
    res = real.get("/admin/atoms/summary", headers=_cognito(issuer, "cis-reviewer"))
    assert res.status_code == 403
    assert res.json()["detail"] == "Role 'reviewer' lacks the 'tenant.read' permission"


def test_cognito_without_the_secret_on_brand_identity_is_the_staff_call(real, pool, issuer):
    pool.add(iss=ISS, sub=SUB)
    res = real.get("/admin/brand-identity", headers=_cognito(issuer))
    assert res.status_code == 200, res.text
    assert pool.route_args == [("00000000-0000-0000-0000-000000000001",)]


def test_cognito_admin_write_without_the_secret(real, pool, issuer, monkeypatch):
    from services.jobs import retention_job

    pool.add(iss=ISS, sub=SUB)
    monkeypatch.setattr(retention_job, "schedule_all", AsyncMock(return_value={"enqueued": [], "skipped": []}))
    res = real.post("/admin/job-runner/retention/schedule", headers=_cognito(issuer))
    assert res.status_code == 200, res.text


def test_guard_functions_accept_a_verified_principal_only_inside_its_request():
    """verify_admin_secret, admin_settings.verify_admin, v1_pipeline._require_admin and the atoms guard:
    with the contextvar set (the middleware did it) no secret is needed; without it, the old answers."""
    from types import SimpleNamespace

    from api.routers import admin_atoms, v1_pipeline

    req = SimpleNamespace(headers={}, method="GET", url=SimpleNamespace(path="/admin/x"))
    ident = SimpleNamespace(auth="cognito", admin_id="a", is_owner=False)
    for cred, var in ((ident, staff_identity.current_staff), ("preview", staff_credential.current_credential)):
        token = var.set(cred)
        try:
            admin.verify_admin_secret(None)
            admin_settings.verify_admin(None)
            admin_atoms._require_atoms_staff(req, None, None)
            assert v1_pipeline._require_admin(req, None)["role"] == "admin"
        finally:
            var.reset(token)
    assert not staff_credential.principal_verified()


# ── No credential: unchanged ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method, path, headers, status, detail", [
    ("GET", "/admin/platform/staff", {}, 403, "Invalid admin secret"),
    ("GET", "/admin/platform/staff", {"X-Admin-Secret": "wrong", "x-admin-role": "admin"}, 403, "Invalid admin secret"),
    ("GET", "/v1/pipeline/sources", {}, 401, "Not authenticated"),
    ("GET", "/v1/pipeline/sources", {"X-Admin-Secret": "wrong"}, 403, "Invalid admin secret"),
    ("GET", "/v1/rules", {}, 404, "Not Found"),                     # AA-923 (WM-34): retired, the prefix stays staff
    ("PATCH", "/v1/rules/00000000-0000-0000-0000-000000000008", {"X-Admin-Secret": "wrong"}, 404, "Not Found"),
])
def test_no_secret_and_no_principal_answer_as_before(real, pool, method, path, headers, status, detail):
    res = real.request(method, path, headers=headers)
    assert res.status_code == status, (path, res.text)
    assert res.json()["detail"] == detail
    assert pool.sql == []


def test_a_legacy_hs256_bearer_is_not_a_principal(real, pool):
    """Only a Cognito Bearer the middleware verified counts: a legacy staff JWT alone still needs the secret."""
    token = _create_admin_jwt(SUB, "u", "admin")
    res = real.get("/admin/platform/staff", headers={"Authorization": f"Bearer {token}", "x-admin-role": "admin"})
    assert res.status_code == 403 and res.json()["detail"] == "Invalid admin secret"


# ── Main secret: unchanged ───────────────────────────────────────────────────────────────────────

def test_main_secret_machine_and_legacy_unchanged(real, pool, monkeypatch, fetches):
    from services.jobs import retention_job

    pool.add()
    monkeypatch.setattr(retention_job, "schedule_all", AsyncMock(return_value={"enqueued": [], "skipped": []}))
    assert real.post("/admin/job-runner/retention/schedule", headers=_main()).status_code == 200   # machine
    assert real.get("/admin/platform/staff", headers=_main("admin")).status_code == 200            # legacy admin
    assert real.get("/admin/platform/staff", headers=_main("content")).status_code == 403
    assert fetches == []          # the main secret never triggers a Preview read


# ── Preview credential: viewer only ──────────────────────────────────────────────────────────────

def test_preview_with_admin_role_gets_viewer_rights_only(real, pool, monkeypatch):
    from api.routers import admin_llm_ops

    pool.add()
    monkeypatch.setattr(admin_llm_ops, "list_stage_configs", AsyncMock(return_value=[]))
    monkeypatch.setattr(admin_llm_ops, "_load_catalog", AsyncMock(return_value=[]))
    with capture_logs() as logs:
        read = real.get("/admin/llm-config", headers=_preview("admin"))
    assert read.status_code == 200, read.text                      # an every-staff read
    seen = [e for e in logs if e["event"] == "staff_preview_credential"]
    assert seen and seen[0]["requested_role"] == "admin" and seen[0]["role"] == "viewer"

    staff = real.get("/admin/platform/staff", headers=_preview("admin"))
    assert staff.status_code == 403
    assert staff.json()["detail"] == "Role 'viewer' lacks the 'staff.read' permission"

    write = real.patch("/admin/llm-config/s1_writer", json={}, headers=_preview("admin"))
    assert write.status_code == 403
    assert write.json()["detail"] == "Role 'viewer' is read-only"


@pytest.mark.parametrize("key", sorted(k for k in MACHINE_ROUTES if k in POLICY))
@pytest.mark.parametrize("role", [None, "admin"])
def test_preview_is_never_the_machine(real, key, role):
    """No role header must not fall back to the machine principal: every MACHINE_ROUTE with a POLICY entry 403s."""
    method, path = key
    res = real.request(method, _concrete(path), json={}, headers=_preview(role))
    assert res.status_code == 403, (key, res.text)
    assert res.json()["detail"] in ("Role 'viewer' is read-only", f"Role 'viewer' lacks the '{POLICY[key]}' permission")


def test_preview_on_v1_pipeline_read(real, pool):
    res = real.get("/v1/pipeline/sources", headers=_preview("admin"))
    assert res.status_code == 200, res.text


def test_preview_is_refused_outside_the_staff_paths(real):
    """/v1/tours/{id}/full checks only the main secret (no middleware there): the Preview value is not admin."""
    res = real.get("/v1/tours/00000000-0000-0000-0000-000000000000/full", headers=_preview("admin"))
    assert res.status_code == 401


def test_preview_secret_is_cached(real, fetches, monkeypatch):
    from api.routers import admin_llm_ops

    monkeypatch.setattr(admin_llm_ops, "list_stage_configs", AsyncMock(return_value=[]))
    monkeypatch.setattr(admin_llm_ops, "_load_catalog", AsyncMock(return_value=[]))
    for _ in range(3):
        assert real.get("/admin/llm-config", headers=_preview()).status_code == 200
    for _ in range(3):
        assert real.get("/admin/llm-config", headers={"X-Admin-Secret": "wrong"}).status_code == 403
    assert len(fetches) == 1


@pytest.mark.parametrize("fetch", [lambda: None, lambda: "", lambda: (_ for _ in ()).throw(OSError("SM down"))])
def test_missing_or_unreadable_preview_secret_turns_the_feature_off(real, fetch):
    staff_credential.reset_preview_cache(fetch)
    res = real.get("/admin/platform/staff", headers=_preview("admin"))
    assert res.status_code == 403 and res.json()["detail"] == "Invalid admin secret"


def test_preview_read_only_on_the_deployed_api(real, monkeypatch, fetches):
    monkeypatch.delenv("AWS_EXECUTION_ENV")
    res = real.get("/admin/platform/staff", headers=_preview("admin"))
    assert res.status_code == 403 and res.json()["detail"] == "Invalid admin secret"
    assert fetches == []


def test_preview_equal_to_the_main_secret_keeps_the_main_meaning(real, pool):
    """Misconfiguration: the value is the main secret, so it is never capped (Production BFF + machine keep working)."""
    pool.add()
    staff_credential.reset_preview_cache(lambda: SECRET)
    assert real.get("/admin/platform/staff", headers=_main("admin")).status_code == 200


# ── Middleware details (echo app) ────────────────────────────────────────────────────────────────

def _echo(pool):
    app = FastAPI()

    @app.get("/admin/echo")
    async def echo(request: Request):
        return {
            "roles": request.headers.getlist("x-admin-role"),
            "user": request.headers.get("x-admin-user-id"),
            "credential": staff_credential.current_credential.get(),
            "state": (request.scope.get("state") or {}).get("staff_credential"),
            "cognito": getattr(staff_identity.current_staff.get(), "admin_id", None),
            "verified": staff_credential.principal_verified(),
        }

    app.add_middleware(StaffBearerMiddleware)
    app.state.pool = pool
    return app


def test_preview_without_a_role_header_becomes_the_viewer_and_resets(real, pool):
    body = TestClient(_echo(pool)).get("/admin/echo", headers=_preview(None)).json()
    assert body["roles"] == ["viewer"] and body["user"] == USER
    assert body["credential"] == body["state"] == "preview" and body["verified"] is True
    assert staff_credential.current_credential.get() is None


def test_cognito_bearer_wins_over_the_preview_credential(real, pool, issuer):
    uid = pool.add(iss=ISS, sub=SUB)
    body = TestClient(_echo(pool)).get("/admin/echo", headers={**_preview("viewer"), **_cognito(issuer)}).json()
    assert body["roles"] == ["admin"] and body["cognito"] == uid and body["credential"] is None


@pytest.mark.parametrize("headers", [_main("admin"), {"X-Admin-Secret": "wrong", "x-admin-role": "admin"}, {}])
def test_other_requests_are_untouched(real, pool, headers):
    body = TestClient(_echo(pool)).get("/admin/echo", headers=headers).json()
    assert body["roles"] == (["admin"] if headers else []) and body["credential"] is None
    assert body["verified"] is False


# ── Audit trail + rate limiter see the new credentials ───────────────────────────────────────────

def _audit_app(pool):
    app = FastAPI()

    @app.post("/admin/things")
    async def things():
        return {"ok": True}

    app.add_middleware(staff_audit.StaffAuditMiddleware)
    app.add_middleware(StaffBearerMiddleware)
    app.state.pool = pool
    return app


@pytest.fixture
def audit_rows(monkeypatch):
    rows: list = []

    async def fake_write(conn, **kw):
        rows.append(kw)

    monkeypatch.setattr("services.acp_shared.audit_log.write_audit_log", fake_write)
    return rows


def test_cognito_write_without_the_secret_is_audited(real, pool, issuer, audit_rows):
    uid = pool.add(iss=ISS, sub=SUB)
    assert TestClient(_audit_app(pool)).post("/admin/things", headers=_cognito(issuer)).status_code == 200
    assert len(audit_rows) == 1
    assert audit_rows[0]["actor"] == f"admin:{uid}" and audit_rows[0]["details"]["auth"] == "cognito"


def test_preview_write_is_audited_as_preview(real, pool, audit_rows):
    assert TestClient(_audit_app(pool)).post("/admin/things", headers=_preview("admin")).status_code == 200
    assert len(audit_rows) == 1
    row = audit_rows[0]
    assert row["actor"] == f"admin:{USER}" and row["details"]["auth"] == "preview"
    assert row["details"]["role"] == "viewer"


def test_rate_limiter_tracks_a_cognito_call_without_the_secret(real, pool, issuer):
    uid = pool.add(iss=ISS, sub=SUB)
    assert real.get("/admin/platform/staff", headers=_cognito(issuer)).status_code == 200
    assert rate_limit.track_api_call.await_args.kwargs["admin_user_id"] == uid


# ── Constant-time compare for both secrets ───────────────────────────────────────────────────────

def test_both_secrets_use_the_constant_time_compare(monkeypatch):
    calls: list = []
    real_eq = staff_credential.secrets_equal

    def spy(a, b):
        calls.append((a, b))
        return real_eq(a, b)

    monkeypatch.setattr(staff_credential, "secrets_equal", spy)
    monkeypatch.setattr(staff_authz, "secrets_equal", spy)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    assert staff_authz._is_valid_secret(SECRET)
    assert staff_credential.is_preview_secret(PREVIEW, PREVIEW, [SECRET])
    assert not staff_credential.is_preview_secret("nope", PREVIEW, [SECRET])
    assert not staff_credential.is_preview_secret(SECRET, SECRET, [SECRET])
    assert (SECRET, SECRET) in calls and (PREVIEW, PREVIEW) in calls and ("nope", PREVIEW) in calls


def test_no_plain_comparison_of_an_admin_secret_in_the_api():
    """Every admin-secret compare goes through api/core/secret_compare.secrets_equal (hmac.compare_digest)."""
    offenders = []
    for path in (REPO / "api").rglob("*.py"):
        src = path.read_text()
        if path.name != "secret_compare.py" and "compare_digest" in src:
            offenders.append(f"{path}: compare_digest outside secret_compare.py")
        if re.search(r"(?i)(admin_secret|x_admin|preview)\w*\s*[!=]=\s*\w|[!=]=\s*\w*(ADMIN_SECRET|x_admin_secret)\b",
                     src):
            offenders.append(f"{path}: == / != on a secret")
    assert offenders == []
