"""AA-1094 (S278, root ADR 0006a A10b-4) — the Dev-only E2E CIS admin (A10b) may attach or cancel the TripPlanner
add-on of a synthetic tenant through the tenant_provision routes (Tenant 360 > Subscription "Add an add-on" /
"Cancel"), so the agent can serve a synthetic agency on TripPlanner.

Covers api/core/e2e_staff.py (the two routes on the allow-list, their concrete paths; the create route stays off it)
and the real app end to end through the bearer middleware, staff_policy and api/routers/admin_tenant_provision.py:
allowed for a tripplanner add-on of a synthetic tenant (the job enqueued with the A10b admin as requested_by), refused
403 ``e2e_read_only`` before any job for a crm add-on, a mixed request or a real tenant (is_canary FALSE or NULL, or
the pilot, which the SQL answers FALSE for); the A10d identity is refused outright; a normal admin is unaffected. No
network, no AWS, no DB.
"""
from __future__ import annotations

from typing import Any, Optional

import pytest
import structlog.testing
from fastapi.testclient import TestClient

from api.core import e2e_staff, staff_identity
from api.main import app as real_app
from api.routers import admin, admin_settings
from api.routers import admin_tenant_provision as prov
from tests.unit.aa854_cognito_staff import ISS, Directory
from tests.unit.test_aa816_e2e_cis_admin import (  # noqa: F401 — issuer is a fixture
    SECRET, _admin_e2e_row, _admin_e2e_token, _set_env, issuer,
)

SYNTHETIC = "4c3b2a19-0f8e-4d7c-9b6a-5e4d3c2b1a09"
REAL = "7e6d5c4b-3a29-4180-9f7e-6d5c4b3a2918"
NULL_FLAG = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
JOB = "9d8c7b6a-5f4e-4d3c-8b2a-190f8e7d6c5b"
CS_E2E = "e2e-cis-cs@e2e-mail.lumiguides.it.com"
TP = {"product": "tripplanner", "plan_key": "tripplanner.hosted"}
CRM = {"product": "crm", "plan_key": "crm.core", "seats": 3}


# ── api/core/e2e_staff.py ────────────────────────────────────────────────────────────────────────

def test_the_add_on_routes_are_on_the_allow_list():
    assert e2e_staff.E2E_TRIPPLANNER_ADDON_WRITES == {
        ("POST", "/admin/tenants/{tenant_id}/provision"),
        ("POST", "/admin/tenants/{tenant_id}/provision/cancel"),
    }
    assert e2e_staff.E2E_TRIPPLANNER_ADDON_WRITES <= e2e_staff.E2E_ALLOWED_WRITES
    assert e2e_staff.E2E_ADDON_PRODUCTS == {"tripplanner"}
    # a new tenant is never an E2E write
    assert ("POST", "/admin/tenants/provision") not in e2e_staff.E2E_ALLOWED_WRITES


_T = "5f0e2c1a-0000-4000-8000-0000000094aa"  # fixed: parametrize ids must match in every xdist worker


@pytest.mark.parametrize("path,allowed", [
    (f"/admin/tenants/{_T}/provision", True),
    (f"/admin/tenants/{_T}/provision/cancel", True),
    ("/admin/tenants/provision", False),
    (f"/admin/tenants/{_T}/provision/", False),
    (f"/admin/tenants/{_T}/provision/cancel/", False),
    (f"/admin/tenants/{_T}/provision/x", False),
    ("/admin/tenants/not-a-uuid/provision", False),
    (f"/admin/tenants/{_T}/entitlement-overrides/tp.hosted", False),
])
def test_the_concrete_path_is_matched_in_full(path, allowed):
    assert e2e_staff.is_allowed_e2e_write_path("POST", path) is allowed
    assert e2e_staff.is_allowed_e2e_write_path("PUT", path) is False


# ── the real app ─────────────────────────────────────────────────────────────────────────────────

class ProvisionDirectory(Directory):
    """The staff directory for the middleware, plus shared.tenants' synthetic flag (synthetic, real, NULL). The job
    enqueue and read are stubbed by the fixture and recorded."""

    FLAGS = {SYNTHETIC: True, REAL: False, NULL_FLAG: None}

    def __init__(self) -> None:
        super().__init__()
        self.request_audit: list[tuple] = []
        self.synthetic_checks = 0

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
        raise AssertionError(sql)


@pytest.fixture
def real(monkeypatch, issuer):  # noqa: F811
    _set_env(monkeypatch, "dev")
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    directory = ProvisionDirectory()
    admin_id = _admin_e2e_row(directory)
    monkeypatch.setattr(real_app.state, "pool", directory, raising=False)
    enqueued: list[Any] = []

    async def _enqueue_request(pool, req):
        enqueued.append(req)
        return JOB, True

    async def _job(request, job_id):
        req = enqueued[-1]
        return prov.ProvisionJobOut(job_id=job_id, created=False, status="queued", request_key=req.request_key,
                                    tenant_id=req.tenant_id)

    monkeypatch.setattr(prov.tenant_provision_job, "enqueue_request", _enqueue_request)
    monkeypatch.setattr(prov, "_job", _job)
    return TestClient(real_app), directory, admin_id, enqueued


def _attach(client, token: str, tenant: str, add_ons: list[dict]):
    return client.post(f"/admin/tenants/{tenant}/provision", headers={"Authorization": f"Bearer {token}"},
                       json={"request_key": "e2e-aa1094-attach-0001", "add_ons": add_ons})


def _cancel(client, token: str, tenant: str, products: list[str]):
    return client.post(f"/admin/tenants/{tenant}/provision/cancel", headers={"Authorization": f"Bearer {token}"},
                       json={"request_key": "e2e-aa1094-cancel-0001", "products": products})


def test_tripplanner_attach_on_a_synthetic_tenant_is_enqueued(real, issuer):  # noqa: F811
    client, directory, admin_id, enqueued = real
    res = _attach(client, _admin_e2e_token(issuer), SYNTHETIC, [TP])
    assert res.status_code == 202, res.text
    assert res.json()["job_id"] == JOB and res.json()["created"] is True
    assert directory.synthetic_checks == 1
    (req,) = enqueued
    assert (req.mode, req.tenant_id, req.requested_by) == ("attach", SYNTHETIC, admin_id)
    assert [a.product for a in req.add_ons] == ["tripplanner"]
    (row,), = [directory.request_audit]   # the AA-884 staff.request row names the A10b admin
    assert f"admin:{admin_id}" in row and 202 in row


def test_tripplanner_cancel_on_a_synthetic_tenant_is_enqueued(real, issuer):  # noqa: F811
    client, directory, _, enqueued = real
    res = _cancel(client, _admin_e2e_token(issuer), SYNTHETIC, ["tripplanner"])
    assert res.status_code == 202, res.text
    (req,) = enqueued
    assert (req.mode, req.cancel) == ("detach", ("tripplanner",))


@pytest.mark.parametrize("tenant", [REAL, NULL_FLAG])
def test_attach_on_a_real_tenant_is_refused_before_any_job(real, issuer, tenant):  # noqa: F811
    client, directory, _, enqueued = real
    with structlog.testing.capture_logs() as logs:
        res = _attach(client, _admin_e2e_token(issuer), tenant, [TP])
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    assert enqueued == []
    assert any(e.get("event") == "tenant_provision_e2e_refused" and e.get("reason") == "tenant_not_synthetic"
               for e in logs)
    (row,), = [directory.request_audit]   # the refused call still leaves its AA-884 row, with the 403
    assert 403 in row


def test_cancel_on_a_real_tenant_is_refused(real, issuer):  # noqa: F811
    client, _, _, enqueued = real
    res = _cancel(client, _admin_e2e_token(issuer), REAL, ["tripplanner"])
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"}) and enqueued == []


@pytest.mark.parametrize("add_ons", [[CRM], [TP, CRM]], ids=["crm", "tripplanner_and_crm"])
def test_a_crm_add_on_is_refused_even_on_a_synthetic_tenant(real, issuer, add_ons):  # noqa: F811
    client, directory, _, enqueued = real
    with structlog.testing.capture_logs() as logs:
        res = _attach(client, _admin_e2e_token(issuer), SYNTHETIC, add_ons)
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    assert enqueued == [] and directory.synthetic_checks == 0
    assert any(e.get("reason") == "product_not_allowed" for e in logs)


def test_a_crm_cancel_is_refused_even_on_a_synthetic_tenant(real, issuer):  # noqa: F811
    client, directory, _, enqueued = real
    res = _cancel(client, _admin_e2e_token(issuer), SYNTHETIC, ["crm", "tripplanner"])
    assert res.status_code == 403 and enqueued == [] and directory.synthetic_checks == 0


def test_creating_a_tenant_stays_refused(real, issuer):  # noqa: F811
    client, _, _, enqueued = real
    res = client.post("/admin/tenants/provision", headers={"Authorization": f"Bearer {_admin_e2e_token(issuer)}"},
                      json={"request_key": "e2e-aa1094-create-01", "name": "Lantern Bay Trails", "slug": "lantern-bay",
                            "plan_tier": "starter", "posts_per_week": 1, "kind": "demo", "add_ons": [TP]})
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"}) and enqueued == []


def test_the_a10d_identity_is_refused_even_on_a_synthetic_tenant(real, issuer):  # noqa: F811
    client, directory, _, enqueued = real
    directory.add(role="customer_success", email=CS_E2E, iss=ISS, sub="cs-sub-0001", username="e2e-cis-cs")
    token = issuer.token(email=CS_E2E, sub="cs-sub-0001", **{"cognito:groups": ["cis-customer_success"]})
    res = _attach(client, token, SYNTHETIC, [TP])
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    assert enqueued == [] and directory.synthetic_checks == 0


def test_attach_outside_dev_is_refused(real, issuer, monkeypatch):  # noqa: F811
    client, _, _, enqueued = real
    _set_env(monkeypatch, "prod")
    staff_identity.clear_cache()
    res = _attach(client, _admin_e2e_token(issuer), SYNTHETIC, [TP])
    assert res.status_code == 403 and enqueued == []


@pytest.mark.asyncio
async def test_a_normal_admin_is_unaffected():
    """Control: the route check is a no-op for every identity that is not a Dev-only E2E session (no DB read)."""

    class _Req:
        scope: dict = {"state": {}}

        class app:  # noqa: N801 — a request stand-in
            class state:  # noqa: N801
                pool: Optional[Any] = None   # any use would raise

    with structlog.testing.capture_logs() as logs:
        await prov._refuse_e2e(_Req(), SYNTHETIC, ["crm"])
    assert logs == []
