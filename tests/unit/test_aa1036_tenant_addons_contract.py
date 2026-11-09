"""AA-1036 R3 — /tenant/v1 add-ons (tenant.addons.v1): the CRM reads an agency's add-ons and relays its CRM owner's
add-on request (an agency whose owner has no Content studio membership).

``docs/contracts/tenant.addons.v1.openapi.yaml`` is a byte-identical copy of the root contract (AA-Ecosys
``docs/contracts/``, the source of truth); :data:`ROOT_CONTRACT_SHA256` pins the root file's sha256. Every scenario
calls the real router with a local M2M issuer; the bodies are built by the real service functions
(``addon_requests.add_on_states`` / ``tenant_row``) and validated against the contract's schemas, so a field the
service adds without the contract fails here. The database paths are in
``tests/integration/test_aa1036_addon_requests_pg.py``.
"""
from __future__ import annotations

import hashlib
import uuid
from contextlib import asynccontextmanager
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
from api.routers import tenant_addons
from api.routers.tenant_addons import router
from services.tenant_account import addon_requests as ar
from services.tenant_account.plans import PlanError
from tests.unit.aa976_m2m import OTHER_CLIENT, install_m2m

CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "contracts" / "tenant.addons.v1.openapi.yaml"
DOC = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
# sha256 of AA-Ecosys docs/contracts/tenant.addons.v1.openapi.yaml (info.version 1.0, AA-1036 R3).
# Update the root file first, copy it here byte for byte, then set this to `sha256sum` of the root file.
ROOT_CONTRACT_SHA256 = "2705746e4b47934ff24e3deba45fc9fce36f62ad960b0d4209f3a3cc0a7efc26"

TENANT = "1036ad00-0000-4000-8000-000000000001"
MEMBER = "1036ad00-0000-4000-8000-0000000000aa"
T0 = datetime(2026, 11, 5, 9, 0, 0, 123456, tzinfo=timezone.utc)
FORBIDDEN_KEYS = {"requested_by", "decided_by", "plan_key", "provision_job_id", "job_status", "job_error", "actor",
                  "email", "display_name", "role"}


def _row(product: str = "tripplanner", status: str = "pending", **extra: Any) -> dict:
    decided = None if status == "pending" else T0
    return {"request_id": str(uuid.uuid4()), "product": product, "status": status, "note": extra.get("note"),
            "requested_by": f"crm-member:{MEMBER}", "requested_at": T0, "decided_at": decided,
            "decided_by": None if status == "pending" else "admin:x", "decision_note": extra.get("decision_note"),
            "plan_key": "crm.core" if status == "approved" else None, "provision_job_id": None,
            "job_status": extra.get("job_status")}


class FakePool:
    def __init__(self, fail: Optional[BaseException] = None) -> None:
        self.fail, self.timeouts = fail, []

    @asynccontextmanager
    async def acquire(self, timeout: Optional[float] = None):
        self.timeouts.append(timeout)
        if self.fail:
            raise self.fail
        yield object()


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
def pool():
    return FakePool()


@pytest.fixture
def client(pool):
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(m2m_auth.M2MProblem, m2m_auth.m2m_problem_handler)
    app.state.pool = pool
    return TestClient(app)


@pytest.fixture
def service(monkeypatch):
    """The two service calls the router makes, recorded; bodies from the real pure functions."""
    seen: dict[str, Any] = {"view": [], "create": []}
    state: dict[str, Any] = {"live": {"crm": "active"}, "requests": [_row("tripplanner", "declined",
                                                                         decision_note="Finish the content setup.")],
                             "raise": None, "created": True}

    async def tenant_view(conn, tenant_id):
        seen["view"].append(tenant_id)
        if state["raise"]:
            raise state["raise"]
        return {"in_service": True, "add_ons": ar.add_on_states(state["live"], state["requests"])}

    async def create_request(conn, tenant_id, product, note, *, actor):
        seen["create"].append((tenant_id, product, note, actor))
        if state["raise"]:
            raise state["raise"]
        return ar.tenant_row(_row(product, "pending", note=note)), state["created"]

    monkeypatch.setattr(tenant_addons.ar, "tenant_view", tenant_view)
    monkeypatch.setattr(tenant_addons.ar, "create_request", create_request)
    return seen, state


def _auth(issuer, scope=m2m_auth.SCOPE_TENANT_ADDONS, **claims) -> dict:
    return {"Authorization": f"Bearer {issuer.token(scope=scope, **claims)}"}


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | set().union(*(_keys(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_keys(v) for v in node)) if node else set()
    return set()


def _valid(name: str, body: Any) -> None:
    schema = Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": DOC["components"]})
    errors = sorted(schema.iter_errors(body), key=str)
    assert not errors, [e.message for e in errors]
    assert not _keys(body) & FORBIDDEN_KEYS


def _post(client, issuer, body: Any, tenant: str = TENANT, **headers):
    return client.post(f"/tenant/v1/tenants/{tenant}/add-on-requests", json=body, headers={**_auth(issuer), **headers})


# ── the contract ─────────────────────────────────────────────────────────────

def test_copy_is_the_pinned_root_contract():
    assert hashlib.sha256(CONTRACT.read_bytes()).hexdigest() == ROOT_CONTRACT_SHA256, (
        "docs/contracts/tenant.addons.v1.openapi.yaml differs from the pinned root contract: change the AA-Ecosys "
        "root file first, copy it here unchanged, then update ROOT_CONTRACT_SHA256")


def test_the_router_serves_exactly_the_contract_paths_and_scope():
    served = {(route.path.removeprefix("/tenant/v1"), m) for route in router.routes for m in route.methods}
    declared = {(p, m.upper()) for p, ops in DOC["paths"].items() for m in ops if m != "parameters"}
    assert served == declared
    assert DOC["servers"] == [{"url": "/tenant/v1"}]
    assert set(DOC["components"]["securitySchemes"]["m2m"]["flows"]["clientCredentials"]["scopes"]) == {
        m2m_auth.SCOPE_TENANT_ADDONS}


def test_the_request_body_model_is_the_contract_schema():
    schema = DOC["components"]["schemas"]["AddOnRequestIn"]
    assert set(tenant_addons.TenantAddOnRequestIn.model_fields) == set(schema["properties"])
    assert schema["properties"]["note"]["$ref"].endswith("/Text")
    assert DOC["components"]["schemas"]["Text"]["maxLength"] == ar.NOTE_MAX


def test_the_tenant_row_is_the_contract_request_shape():
    """A key added to addon_requests.tenant_row must be added to the contract (additionalProperties: false)."""
    assert set(ar.tenant_row(_row())) == set(DOC["components"]["schemas"]["AddOnRequest"]["required"])


# ── auth ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [("GET", f"/tenants/{TENANT}/add-ons"),
                                         ("POST", f"/tenants/{TENANT}/add-on-requests")])
def test_no_token_is_401(client, issuer, service, method, path):
    r = client.request(method, f"/tenant/v1{path}", json={"product": "crm", "crm_member_id": MEMBER})
    assert r.status_code == 401 and r.headers["WWW-Authenticate"] == 'Bearer realm="tenant.addons.v1"'
    _valid("Problem", r.json())
    assert service[0] == {"view": [], "create": []}


@pytest.mark.parametrize("scope", [m2m_auth.SCOPE_TENANT_READ, m2m_auth.SCOPE_TENANT_ACTIVATE,
                                   m2m_auth.SCOPE_CATALOG_READ])
def test_another_scope_is_403(client, issuer, service, scope):
    r = client.post(f"/tenant/v1/tenants/{TENANT}/add-on-requests", json={"product": "crm", "crm_member_id": MEMBER},
                    headers=_auth(issuer, scope=scope))
    assert r.status_code == 403 and r.json() == {"error": "forbidden",
                                                 "message": "The token lacks the scope cis.tenant/addons."}
    assert service[0]["create"] == []


def test_another_client_is_403(client, issuer, service):
    r = client.get(f"/tenant/v1/tenants/{TENANT}/add-ons",
                   headers=_auth(issuer, client_id=OTHER_CLIENT, sub=OTHER_CLIENT))
    assert r.status_code == 403 and r.json()["message"] == "This client may not read or request add-ons."


# ── list ─────────────────────────────────────────────────────────────────────

def test_the_list_conforms(client, issuer, pool, service):
    seen, state = service
    state["requests"].insert(0, _row("tripplanner", "pending", note="For our river season."))
    r = client.get(f"/tenant/v1/tenants/{TENANT}/add-ons", headers=_auth(issuer))
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    body = r.json()
    _valid("AddOnList", body)
    assert [(a["product"], a["state"]) for a in body["add_ons"]] == [("crm", "active"), ("tripplanner", "requested")]
    assert body["add_ons"][1]["last_decision"]["decision_note"] == "Finish the content setup."
    assert seen["view"] == [TENANT] and pool.timeouts == [tenant_addons.ACQUIRE_TIMEOUT_S]


@pytest.mark.parametrize("tenant", ["not-a-uuid", "1036AD00-0000-4000-8000-00000000000Z"])
def test_a_bad_tenant_id_is_404(client, issuer, service, tenant):
    r = client.get(f"/tenant/v1/tenants/{tenant}/add-ons", headers=_auth(issuer))
    assert r.status_code == 404 and r.json()["reason"] == "tenant_not_found"
    _valid("Problem", r.json())
    assert service[0]["view"] == []


def test_an_unknown_tenant_is_404(client, issuer, service):
    service[1]["raise"] = PlanError("tenant_not_found", "no tenant with this id", 404)
    r = client.get(f"/tenant/v1/tenants/{TENANT}/add-ons", headers=_auth(issuer))
    assert r.status_code == 404 and r.json() == {"error": "not_found", "message": "no tenant with this id",
                                                 "reason": "tenant_not_found"}


@pytest.mark.parametrize("error", [asyncpg.UndefinedTableError("x"), OSError("down"), TimeoutError()])
def test_the_database_down_or_374_missing_is_503(issuer, service, error):
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(m2m_auth.M2MProblem, m2m_auth.m2m_problem_handler)
    app.state.pool = FakePool(fail=error)
    r = TestClient(app).get(f"/tenant/v1/tenants/{TENANT}/add-ons", headers=_auth(issuer))
    assert r.status_code == 503 and r.json()["error"] == "unavailable" and r.headers["Retry-After"] == "5"


def test_no_pool_is_503(issuer, service):
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(m2m_auth.M2MProblem, m2m_auth.m2m_problem_handler)
    r = TestClient(app).post(f"/tenant/v1/tenants/{TENANT}/add-on-requests",
                             json={"product": "crm", "crm_member_id": MEMBER}, headers=_auth(issuer))
    assert r.status_code == 503


# ── request ──────────────────────────────────────────────────────────────────

def test_a_request_names_the_crm_member_and_conforms(client, issuer, service):
    seen, _ = service
    body = {"product": "tripplanner", "note": "For our river season.", "crm_member_id": MEMBER}
    _valid("AddOnRequestIn", body)
    r = _post(client, issuer, body)
    assert r.status_code == 201 and r.headers["Cache-Control"] == "no-store"
    _valid("AddOnRequestCreated", r.json())
    assert r.json()["created"] is True and r.json()["request"]["status"] == "pending"
    assert seen["create"] == [(TENANT, "tripplanner", "For our river season.", f"crm-member:{MEMBER}")]
    assert len(seen["create"][0][3]) <= 64  # shared.tenant_addon_request.requested_by CHECK


def test_an_open_request_answers_200_created_false(client, issuer, service):
    service[1]["created"] = False
    r = _post(client, issuer, {"product": "crm", "crm_member_id": MEMBER})
    assert r.status_code == 200 and r.json()["created"] is False
    _valid("AddOnRequestCreated", r.json())


@pytest.mark.parametrize("body", [
    {"product": "booking", "crm_member_id": MEMBER},
    {"product": "crm"},
    {"product": "crm", "crm_member_id": "member-1"},
    {"product": "crm", "crm_member_id": MEMBER, "note": "x" * 501},
    {"product": "crm", "crm_member_id": MEMBER, "requested_by": "someone"},
    {"product": "crm", "crm_member_id": MEMBER, "email": "owner@example.test"},
    "not json",
])
def test_a_body_outside_the_contract_is_400(client, issuer, service, body):
    if isinstance(body, str):
        r = client.post(f"/tenant/v1/tenants/{TENANT}/add-on-requests", content=body,
                        headers={**_auth(issuer), "Content-Type": "application/json"})
    else:
        r = _post(client, issuer, body)
    assert r.status_code == 400 and r.json()["error"] == "invalid"
    _valid("Problem", r.json())
    assert service[0]["create"] == []


@pytest.mark.parametrize("error,status,problem", [
    (PlanError("already_subscribed", "the CRM add-on is already on", 409), 409, "conflict"),
    (PlanError("tenant_not_in_service", "this account cannot request add-ons now", 409), 409, "conflict"),
    (PlanError("too_many_requests", "too many add-on requests today; try again tomorrow", 429), 429,
     "too_many_requests"),
    (PlanError("note_too_long", "at most 500 characters"), 400, "invalid"),
    (PlanError("tenant_not_found", "no tenant with this id", 404), 404, "not_found"),
    (PlanError("odd", "a status the contract does not list", 422), 400, "invalid"),
])
def test_refusals_carry_the_cis_reason(client, issuer, service, error, status, problem):
    service[1]["raise"] = error
    r = _post(client, issuer, {"product": "crm", "crm_member_id": MEMBER})
    assert r.status_code == status
    assert r.json() == {"error": problem, "message": error.message, "reason": error.code}
    _valid("Problem", r.json())


def test_the_app_serves_the_routes():
    """Included in api.main (/tenant is neither /v1 nor a staff path: no tenant role middleware, no staff policy)."""
    from api.main import app

    paths = app.openapi()["paths"]
    assert "get" in paths["/tenant/v1/tenants/{tenant_id}/add-ons"]
    assert "post" in paths["/tenant/v1/tenants/{tenant_id}/add-on-requests"]
