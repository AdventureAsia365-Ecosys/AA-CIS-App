"""AA-1093 lane L2 — /tenant/v1/erasure-requests conforms to the contract tenant.erasure.v1 (OpenAPI 3.1).

``docs/contracts/tenant.erasure.v1.openapi.yaml`` is a byte-identical copy of the root contract (AA-Ecosys
``docs/contracts/``, the source of truth, checked there by ``tools/contracts/check_contracts.py``); this repo cannot see
the root repo in CI, so :data:`ROOT_CONTRACT_SHA256` pins the root file's sha256.

Checked: the served paths and scope are the contract's; the report body model is the contract's ErasureReport; a feed
item built by the real item_body validates as ErasureRequest (CRM step only, every step status); ReportAccepted and the
Problem bodies validate; the feed event change of tenant.entitlements.v1 1.7 (op, request_id) validates; the auth
outcomes (401 no token, 403 another scope) and a bad body (422) never reach the database. No response carries a name,
an actor, an approver or free text.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from api.core import m2m_auth
from api.routers import tenant_erasure as tr
from services.tenant_erasure import store
from tests.unit.aa976_m2m import install_m2m

ROOT = Path(__file__).resolve().parents[2] / "docs" / "contracts"
CONTRACT = ROOT / "tenant.erasure.v1.openapi.yaml"
DOC = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
ENTITLEMENTS = yaml.safe_load((ROOT / "tenant.entitlements.v1.openapi.yaml").read_text(encoding="utf-8"))
# sha256 of AA-Ecosys docs/contracts/tenant.erasure.v1.openapi.yaml (info.version 1.0, AA-1093, root PR #747).
# Update the root file first, copy it here byte for byte, then set this to `sha256sum` of the root file.
ROOT_CONTRACT_SHA256 = "1f0c814e4b2954ddbd386f5f9aed582cffb78b8512748f8926e2a000f4b37d8b"

RID = "10930000-0000-4000-8000-0000000000d1"
TID = "10930000-0000-4000-8000-0000000000d2"
T0 = datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)
FORBIDDEN_KEYS = {"name", "slug", "trading_name", "approved_by", "actor", "email", "snapshot_id", "reason", "detail"}


def _keys(node: Any) -> set[str]:
    if isinstance(node, dict):
        return set(node) | set().union(*(_keys(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_keys(v) for v in node)) if node else set()
    return set()


def _valid(name: str, body: Any, doc: dict = DOC) -> None:
    schema = Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": doc["components"]})
    errors = sorted(schema.iter_errors(body), key=str)
    assert not errors, [e.message for e in errors]
    assert not _keys(body) & FORBIDDEN_KEYS


def _row(**over) -> dict:
    row = {"request_id": uuid.UUID(RID), "tenant_id": uuid.UUID(TID), "phase": "apply", "round": 2, "opened_at": T0,
           "approval_expires_at": T0, "is_pilot": False, "is_synthetic": True, "round_apps": ["crm"],
           "done_apps": ["cis", "booking", "tripplanner"], "not_before_crm": T0, "updated_at": T0,
           "approved_plan_hash": "d" * 64, "crm_failed": False, "txid_text": "4242"}
    row.update(over)
    return row


# ── the contract ─────────────────────────────────────────────────────────────

def test_copy_is_the_pinned_root_contract():
    assert hashlib.sha256(CONTRACT.read_bytes()).hexdigest() == ROOT_CONTRACT_SHA256, (
        "docs/contracts/tenant.erasure.v1.openapi.yaml differs from the pinned root contract: change the AA-Ecosys "
        "root file first, copy it here unchanged, then update ROOT_CONTRACT_SHA256")


def test_the_router_serves_exactly_the_contract_paths_and_scope():
    served = {(route.path.removeprefix("/tenant/v1"), m) for route in tr.router.routes for m in route.methods}
    declared = {(p, m.upper()) for p, ops in DOC["paths"].items() for m in ops if m != "parameters"}
    assert served == declared
    assert DOC["servers"] == [{"url": "/tenant/v1"}]
    assert set(DOC["components"]["securitySchemes"]["m2m"]["flows"]["clientCredentials"]["scopes"]) == {
        m2m_auth.SCOPE_TENANT_ERASURE}


def test_the_report_body_model_is_the_contract_report():
    schema = DOC["components"]["schemas"]["ErasureReport"]
    assert set(tr.ErasureReportIn.model_fields) == set(schema["properties"]) == set(schema["required"])
    assert set(tr.TableEntry.model_fields) == set(DOC["components"]["schemas"]["TableEntry"]["properties"])
    assert set(tr.Census.model_fields) == set(DOC["components"]["schemas"]["Census"]["properties"])
    assert tr.MAX_LIMIT == DOC["paths"]["/erasure-requests"]["get"]["parameters"][1]["schema"]["maximum"]


@pytest.mark.parametrize("round_apps,done_apps,failed,status", [
    (["cis", "booking", "tripplanner"], [], False, "waiting"),
    (["crm"], ["cis", "booking", "tripplanner"], False, "active"),
    (["crm"], ["cis", "booking", "tripplanner"], True, "failed"),
    ([], ["cis", "booking", "tripplanner", "crm"], False, "done"),
])
def test_a_feed_item_is_the_contract_request(round_apps, done_apps, failed, status):
    body = tr.item_body(_row(round_apps=round_apps, done_apps=done_apps, crm_failed=failed))
    _valid("ErasureRequest", body)
    assert body["steps"] == [{"app": "crm", "status": status, "not_before": "2026-12-01T08:00:00Z",
                              "approved_plan_hash": "d" * 64}]
    _valid("ErasureRequestPage", {"items": [body], "next_cursor": body["cursor"]})


def test_the_cursor_round_trips():
    c = tr.encode_cursor(4242, uuid.UUID(RID))
    assert tr.decode_cursor(c) == (4242, uuid.UUID(RID)) and tr.decode_cursor(None)[0] == 0


def test_report_accepted_and_the_event_change_validate():
    _valid("ReportAccepted", {"report_id": RID, "created": True, "phase": "dry_run"})
    change = store.event_change({"request_id": RID, "mode": "live"}, "requested")
    _valid("ErasureRequestChange", change, ENTITLEMENTS)
    _valid("ErasureRequestChange", store.event_change({"request_id": RID}, "cancelled"), ENTITLEMENTS)


# ── auth and bodies through the real router ──────────────────────────────────

class FakePool:
    def __init__(self) -> None:
        self.acquired = 0

    @asynccontextmanager
    async def acquire(self, timeout: Optional[float] = None):
        self.acquired += 1
        raise store.NOT_MIGRATED[0]("relation does not exist")
        yield  # pragma: no cover


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(m2m_auth.ENV_M2M_ISSUER, raising=False)
    monkeypatch.delenv(m2m_auth.ENV_M2M_CRM_CLIENT_ID, raising=False)
    yield
    m2m_auth.set_m2m_verifier(None)


@pytest.fixture
def setup():
    issuer, _ = install_m2m()
    pool = FakePool()
    app = FastAPI()
    app.include_router(tr.router)
    app.add_exception_handler(m2m_auth.M2MProblem, m2m_auth.m2m_problem_handler)
    app.state.pool = pool
    return TestClient(app), issuer, pool


def _report(**over) -> dict:
    body = {"report_id": str(uuid.UUID(int=1)), "request_id": RID, "tenant_id": TID, "app": "crm", "round": 2,
            "mode": "dry_run", "status": "ok", "error_code": None, "plan_hash": "d" * 64,
            "tables": [{"table": "crm.agency_rows", "action": "shred", "rows": 3, "digest": None}], "blockers": [],
            "census": None, "job_id": None, "reported_at": "2027-03-01T08:00:00Z"}
    body.update(over)
    return body


def test_the_sample_report_is_a_contract_report():
    _valid("ErasureReport", _report())


def test_no_token_is_401(setup):
    client, _, pool = setup
    r = client.get("/tenant/v1/erasure-requests")
    assert r.status_code == 401 and r.headers["WWW-Authenticate"] == 'Bearer realm="tenant.erasure.v1"'
    _valid("Problem", r.json())
    assert pool.acquired == 0


def test_another_scope_is_403(setup):
    client, issuer, pool = setup
    r = client.post(f"/tenant/v1/erasure-requests/{RID}/reports", json=_report(),
                    headers={"Authorization": f"Bearer {issuer.token(scope=m2m_auth.SCOPE_TENANT_READ)}"})
    assert r.status_code == 403 and pool.acquired == 0
    _valid("Problem", r.json())


@pytest.mark.parametrize("over,status,error", [
    ({"app": "booking"}, 403, "wrong_app"),
    ({"plan_hash": "d" * 32}, 422, "invalid"),
    ({"agency_name": "Zed Trails"}, 422, "invalid"),          # extra field
    ({"mode": "verify", "census": None, "plan_hash": None}, 422, "invalid"),
    ({"request_id": TID}, 422, "invalid"),
])
def test_bad_reports_never_reach_the_database(setup, over, status, error):
    client, issuer, pool = setup
    auth = {"Authorization": f"Bearer {issuer.token(scope=m2m_auth.SCOPE_TENANT_ERASURE)}"}
    r = client.post(f"/tenant/v1/erasure-requests/{RID}/reports", json=_report(**over), headers=auth)
    assert (r.status_code, r.json()["error"]) == (status, error)
    _valid("Problem", r.json())
    assert pool.acquired == 0


def test_not_migrated_is_503_and_a_bad_cursor_400(setup):
    client, issuer, _ = setup
    auth = {"Authorization": f"Bearer {issuer.token(scope=m2m_auth.SCOPE_TENANT_ERASURE)}"}
    r = client.get("/tenant/v1/erasure-requests", headers=auth)
    assert r.status_code == 503 and r.json()["error"] == "unavailable"
    r = client.get("/tenant/v1/erasure-requests?after=nope", headers=auth)
    assert r.status_code == 400 and r.json()["error"] == "invalid"
    r = client.get("/tenant/v1/erasure-requests?limit=201", headers=auth)
    assert r.status_code == 400
    assert json.loads(json.dumps(r.json())) == r.json()
