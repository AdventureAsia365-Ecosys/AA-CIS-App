"""AA-1087 (S278, root ADR 0006a A10b-3) — the Dev-only E2E CIS admin (A10b) may release a held piece (Tenant QA >
Compliance, the staff review "approve" of a tenant's content), on a synthetic tenant only.

Covers api/core/e2e_staff.py (the route on the allow-list, its concrete path) and the real app end to end through the
bearer middleware, staff_policy and api/routers/admin_tenant_compliance.py: allowed on a synthetic tenant (the flag
resolved, the normal audit row with the A10b admin as the actor, the AA-884 staff.request row), refused 403
``e2e_read_only`` on a real tenant (is_canary FALSE or NULL, or the pilot, which the SQL answers FALSE for) before the
flag is resolved or audited; the A10d identity is refused outright; a normal admin is unaffected. No network, no AWS,
no DB.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import pytest
import structlog.testing
from fastapi.testclient import TestClient

from api.core import e2e_staff, staff_identity
from api.main import app as real_app
from api.routers import admin, admin_settings
from api.routers import admin_tenant_compliance as compliance
from tests.unit.aa854_cognito_staff import ISS, Directory
from tests.unit.test_aa816_e2e_cis_admin import (  # noqa: F401 — issuer is a fixture
    SECRET, _admin_e2e_row, _admin_e2e_token, _set_env, issuer,
)

SYNTHETIC = "4c3b2a19-0f8e-4d7c-9b6a-5e4d3c2b1a09"
REAL = "7e6d5c4b-3a29-4180-9f7e-6d5c4b3a2918"
NULL_FLAG = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
PIECE = "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e"
TEXT = "A made-up paragraph about a walk along the Lantern River."
SHA = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
CS_E2E = "e2e-cis-cs@e2e-mail.lumiguides.it.com"
NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


# ── api/core/e2e_staff.py ────────────────────────────────────────────────────────────────────────

def test_the_release_route_is_on_the_allow_list():
    assert e2e_staff.E2E_COMPLIANCE_RELEASE_WRITES == {("POST", "/admin/tenant-qa/compliance/{flag_id}/release")}
    assert e2e_staff.E2E_COMPLIANCE_RELEASE_WRITES <= e2e_staff.E2E_ALLOWED_WRITES


@pytest.mark.parametrize("path,allowed", [
    (f"/admin/tenant-qa/compliance/{uuid.uuid4()}/release", True),
    (f"/admin/tenant-qa/compliance/{uuid.uuid4()}/release/", False),
    (f"/admin/tenant-qa/compliance/{uuid.uuid4()}/dismiss", False),
    ("/admin/tenant-qa/compliance/not-a-uuid/release", False),
    (f"/admin/tenant-qa/compliance/{uuid.uuid4()}/release/x", False),
])
def test_the_concrete_path_is_matched_in_full(path, allowed):
    assert e2e_staff.is_allowed_e2e_write_path("POST", path) is allowed
    assert e2e_staff.is_allowed_e2e_write_path("PUT", path) is False


# ── the real app ─────────────────────────────────────────────────────────────────────────────────

class ComplianceDirectory(Directory):
    """The staff directory for the middleware, plus one open subject_gate flag per tenant and shared.tenants (synthetic,
    real, flag NULL). Writes are recorded, never executed."""

    FLAGS = {SYNTHETIC: True, REAL: False, NULL_FLAG: None}

    def __init__(self) -> None:
        super().__init__()
        self.flags = {str(uuid.uuid4()): tid for tid in self.FLAGS}
        self.writes: list[tuple[str, tuple]] = []
        self.request_audit: list[tuple] = []
        self.synthetic_checks = 0

    def flag_of(self, tenant: str) -> str:
        return next(f for f, t in self.flags.items() if t == tenant)

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        if sql in (staff_identity.SQL_BY_IDENTITY, staff_identity.SQL_FIRST_LINK, staff_identity.SQL_BY_EMAIL):
            return await super().fetchrow(sql, *args)
        if sql == compliance.RELEASE_FLAG_SQL:
            tid = self.flags.get(str(args[0]))
            if tid is None:
                return None
            return {"flag_id": str(args[0]), "tenant_id": tid, "piece_id": PIECE, "state": "open",
                    "kind": "subject_gate", "checked_sha256": SHA, "content_text": TEXT}
        if sql == compliance.RELEASE_SQL:
            self.writes.append(("release", args))
            return {"flag_id": str(args[0]), "resolved_at": NOW}
        raise AssertionError(sql)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        assert sql == e2e_staff.SYNTHETIC_TENANT_SQL, sql
        self.synthetic_checks += 1
        return self.FLAGS.get(str(args[0]))

    async def execute(self, sql: str, *args: Any) -> str:
        if "shared.tenant_api_usage" in sql:
            return "INSERT 0 1"
        if "acp_shared.audit_log" in sql and "staff.request" in args:
            self.request_audit.append(args)
            return "INSERT 0 1"
        self.writes.append(("execute", (sql, *args)))
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any) -> list:
        raise AssertionError(sql)


@pytest.fixture
def real(monkeypatch, issuer):  # noqa: F811
    _set_env(monkeypatch, "dev")
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    directory = ComplianceDirectory()
    admin_id = _admin_e2e_row(directory)
    monkeypatch.setattr(real_app.state, "pool", directory, raising=False)
    return TestClient(real_app), directory, admin_id


def _release(client, token: str, flag_id: str, admin_id: Optional[str] = None):
    headers = {"Authorization": f"Bearer {token}"}
    if admin_id:
        headers["X-Admin-User-Id"] = admin_id
    return client.post(f"/admin/tenant-qa/compliance/{flag_id}/release",
                       json={"reason": "Synthetic rehearsal: links checked", "content_sha256": SHA}, headers=headers)


def test_release_on_a_synthetic_tenant_is_allowed_and_audited(real, issuer):  # noqa: F811
    client, directory, admin_id = real
    res = _release(client, _admin_e2e_token(issuer), directory.flag_of(SYNTHETIC))
    assert res.status_code == 200, res.text
    assert res.json()["resolution"] == "released"
    assert directory.synthetic_checks == 1
    assert [w[0] for w in directory.writes].count("release") == 1
    audits = [w[1] for w in directory.writes if w[0] == "execute" and "acp_shared.audit_log" in w[1][0]]
    assert len(audits) == 1 and compliance.RELEASE_ACTION in audits[0] and f"admin:{admin_id}" in audits[0]
    (row,), = [directory.request_audit]
    assert f"admin:{admin_id}" in row and 200 in row


@pytest.mark.parametrize("tenant", [REAL, NULL_FLAG])
def test_release_on_a_real_tenant_is_refused_before_any_write(real, issuer, tenant):  # noqa: F811
    client, directory, _ = real
    with structlog.testing.capture_logs() as logs:
        res = _release(client, _admin_e2e_token(issuer), directory.flag_of(tenant))
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    assert directory.writes == []
    assert any(e.get("event") == "compliance_release_e2e_refused" and e.get("reason") == "tenant_not_synthetic"
               for e in logs)
    (row,), = [directory.request_audit]   # the refused call still leaves its AA-884 row, with the 403
    assert 403 in row


def test_release_of_an_unknown_flag_is_404(real, issuer):  # noqa: F811
    client, directory, _ = real
    res = _release(client, _admin_e2e_token(issuer), str(uuid.uuid4()))
    assert res.status_code == 404 and directory.writes == [] and directory.synthetic_checks == 0


def test_the_a10d_identity_is_refused_even_on_a_synthetic_tenant(real, issuer):  # noqa: F811
    client, directory, _ = real
    directory.add(role="customer_success", email=CS_E2E, iss=ISS, sub="cs-sub-0001", username="e2e-cis-cs")
    token = issuer.token(email=CS_E2E, sub="cs-sub-0001", **{"cognito:groups": ["cis-customer_success"]})
    res = _release(client, token, directory.flag_of(SYNTHETIC))
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    assert directory.writes == [] and directory.synthetic_checks == 0


def test_release_outside_dev_is_refused(real, issuer, monkeypatch):  # noqa: F811
    client, directory, _ = real
    _set_env(monkeypatch, "prod")
    staff_identity.clear_cache()
    res = _release(client, _admin_e2e_token(issuer), directory.flag_of(SYNTHETIC))
    assert res.status_code == 403 and directory.writes == []


@pytest.mark.asyncio
async def test_a_normal_admin_releases_a_real_tenant_flag_as_before(real):
    """Control: the route check is a no-op for every identity that is not a Dev-only E2E session."""
    _, directory, _ = real

    class _Req:
        scope: dict = {"state": {}}

    with structlog.testing.capture_logs() as logs:
        await compliance._refuse_e2e(_Req(), directory, REAL, uuid.uuid4())
    assert directory.synthetic_checks == 0 and logs == []


def test_release_payload_shape_is_unchanged():
    # The A10b session uses the same body as any admin: reason + the flag's content_sha256.
    body = compliance.ReleaseBody(reason="ok ok", content_sha256=SHA)
    assert json.loads(body.model_dump_json()) == {"reason": "ok ok", "content_sha256": SHA}
