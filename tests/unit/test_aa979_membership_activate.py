"""AA-979 P2b — POST /tenant/membership/v1/invitations/{invitation_id}/activate (contract tenant.membership.v1 1.1) and
services/tenant_membership/session.py activate(): scope, identity rules, idempotency, conflicts, audit row.

A local M2M issuer (tests/unit/aa976_m2m.py) and a scripted fake connection; the SQL itself runs against PostgreSQL in
tests/integration/test_aa979_membership_session.py."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from tests.unit.test_aa978_tenant_membership_contract import DOC

from api.core import m2m_auth, partner_identity
from api.core.m2m_auth import M2MProblem, m2m_problem_handler
from api.routers.tenant_membership import router
from services.tenant_membership import session as studio
from tests.unit.aa976_m2m import install_m2m

ISS = partner_identity.DEV_PARTNER_ISSUER
TENANT = uuid.uuid4()
INVITATION = uuid.uuid4()
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
ACTIVATE_SCOPE = f"{m2m_auth.SCOPE_TENANT_READ} {m2m_auth.SCOPE_TENANT_ACTIVATE}"


def _row(status="invited", iss: Optional[str] = None, sub: Optional[str] = None) -> dict:
    return {"membership_id": uuid.uuid4(), "tenant_id": TENANT, "cognito_iss": iss, "cognito_sub": sub,
            "content_studio_role": "editor", "status": status, "invitation_id": INVITATION, "created_at": NOW,
            "created_by": "admin:x", "updated_at": NOW, "removed_at": None, "removed_by": None}


class _Tx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, row: Optional[dict], unique_violation: bool = False) -> None:
        self.row = row
        self.unique_violation = unique_violation
        self.executed: list[tuple] = []

    def transaction(self):
        return _Tx()

    async def fetchrow(self, sql: str, *args: Any):
        if sql == studio.INVITATION_LOCK_SQL:
            return self.row
        if sql == studio.ACTIVATE_SQL:
            if self.unique_violation:
                raise asyncpg.UniqueViolationError("duplicate")
            return {**self.row, "status": "active", "cognito_iss": args[1], "cognito_sub": args[2]}
        raise AssertionError(sql)

    async def execute(self, sql: str, *args: Any):
        self.executed.append((sql, args))


class FakePool:
    def __init__(self, conn: FakeConn) -> None:
        self.conn = conn

    def acquire(self, timeout: float | None = None):
        conn = self.conn

        class _CM:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _CM()


def _client(conn: FakeConn) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(M2MProblem, m2m_problem_handler)
    app.state.pool = FakePool(conn)
    return TestClient(app)


@pytest.fixture
def m2m():
    issuer, _ = install_m2m()
    yield issuer
    m2m_auth.set_m2m_verifier(None)


def _post(client: TestClient, token: Optional[str], body: Any, invitation: str = str(INVITATION)):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(f"/tenant/membership/v1/invitations/{invitation}/activate", headers=headers, json=body)


BODY = {"identity": {"iss": ISS, "sub": "partner-sub-1"}}


def test_activate_binds_the_identity_and_writes_the_audit_row(m2m):
    conn = FakeConn(_row())
    r = _post(_client(conn), m2m.token(scope=ACTIVATE_SCOPE), BODY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    assert body["membership"]["status"] == "active"
    assert body["membership"]["identity"] == {"iss": ISS, "sub": "partner-sub-1"}
    audit = [a for sql, a in conn.executed if "acp_shared.audit_log" in sql]
    assert len(audit) == 1
    assert audit[0][2] == "tenant.member_activated"
    details = json.loads(audit[0][-1])
    assert "partner-sub-1" not in json.dumps(details)  # fingerprint only, never the identity


def test_same_identity_again_is_a_no_op(m2m):
    conn = FakeConn(_row(status="active", iss=ISS, sub="partner-sub-1"))
    r = _post(_client(conn), m2m.token(scope=ACTIVATE_SCOPE), BODY)
    assert r.status_code == 200 and r.json()["changed"] is False
    assert conn.executed == []


@pytest.mark.parametrize("row,reason", [
    (_row(status="active", iss=ISS, sub="someone-else"), "identity_mismatch"),
    (_row(status="removed"), "membership_removed"),
])
def test_conflicts_are_409_with_a_reason(m2m, row, reason):
    r = _post(_client(FakeConn(row)), m2m.token(scope=ACTIVATE_SCOPE), BODY)
    assert r.status_code == 409
    assert r.json() == {"error": "conflict", "message": r.json()["message"], "reason": reason}


def test_second_live_membership_in_the_same_agency_is_409(m2m):
    r = _post(_client(FakeConn(_row(), unique_violation=True)), m2m.token(scope=ACTIVATE_SCOPE), BODY)
    assert r.status_code == 409 and r.json()["reason"] == "already_member"


def test_unknown_invitation_is_404(m2m):
    assert _post(_client(FakeConn(None)), m2m.token(scope=ACTIVATE_SCOPE), BODY).status_code == 404
    assert _post(_client(FakeConn(_row())), m2m.token(scope=ACTIVATE_SCOPE), BODY,
                 invitation="not-a-uuid").status_code == 404


def test_read_scope_alone_is_403(m2m):
    r = _post(_client(FakeConn(_row())), m2m.token(scope=m2m_auth.SCOPE_TENANT_READ), BODY)
    assert r.status_code == 403


def test_no_token_is_401():
    r = _post(_client(FakeConn(_row())), None, BODY)
    assert r.status_code == 401


@pytest.mark.parametrize("body", [
    {"identity": {"iss": "https://cognito-idp.us-west-1.amazonaws.com/us-west-1_bCcFvbSOS", "sub": "s"}},
    {"identity": {"iss": ISS}},
    {"identity": {"iss": ISS, "sub": "s"}, "role": "owner"},
    {"identity": {"iss": "http://insecure", "sub": "s"}},
])
def test_bodies_outside_the_contract_are_400(m2m, body):
    r = _post(_client(FakeConn(_row())), m2m.token(scope=ACTIVATE_SCOPE), body)
    assert r.status_code == 400


def _contract_errors(name: str, body: Any) -> list[str]:
    validator = Draft202012Validator({"$ref": f"#/components/schemas/{name}", "components": DOC["components"]})
    return [e.message for e in validator.iter_errors(body)]


def test_answers_conform_to_the_contract_1_1(m2m):
    ok = _post(_client(FakeConn(_row())), m2m.token(scope=ACTIVATE_SCOPE), BODY)
    assert _contract_errors("ActivationResult", ok.json()) == []
    conflict = _post(_client(FakeConn(_row(status="removed"))), m2m.token(scope=ACTIVATE_SCOPE), BODY)
    assert _contract_errors("Problem", conflict.json()) == []
    assert _contract_errors("ActivationRequest", BODY) == []
    assert DOC["info"]["version"] in ("1.1", "1.2")  # 1.2 (S252) only adds is_synthetic to getTenantMembers
    op = DOC["paths"]["/invitations/{invitation_id}/activate"]["post"]
    assert op["security"] == [{"m2m": [m2m_auth.SCOPE_TENANT_ACTIVATE]}]
