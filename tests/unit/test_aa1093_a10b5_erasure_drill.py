"""AA-1093 (S279, root ADR 0006a A10b-5) — the Dev-only E2E CIS admin (A10b) may run the live DRY RUN drill of the
tenant offboarding erasure saga on a synthetic tenant: offboard, restore, drill, advance (Run now), stop. Approve and
new-round stay refused.

Covers api/core/e2e_staff.py (the five routes on the allow-list, their concrete paths, synthetic_write_refusal),
api/core/e2e_tenant_writes.py and the real app end to end through the bearer middleware, staff_policy,
api/routers/admin.py (offboard, restore) and api/routers/admin_tenant_erasure.py: each route lets the A10b session
past its check on a synthetic tenant (the route then answers as for any admin), refuses it 403 ``e2e_read_only``
before any write, job or audit row on a real tenant (is_canary FALSE or NULL, or the pilot, which the SQL answers
FALSE for: tests/integration/test_aa1093_a10b5_erasure_drill_db.py checks that on Postgres as cis_app); approve and
new-round refuse the session in the middleware and again in the route; the A10d identity is refused outright; a
normal admin is unaffected. No network, no AWS, no DB.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest
import structlog.testing
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api.core import e2e_staff, e2e_tenant_writes, staff_authz, staff_identity
from api.main import app as real_app
from api.routers import admin, admin_settings
from api.routers import admin_tenant_erasure as ar
from tests.unit.aa854_cognito_staff import ISS, Directory
from tests.unit.test_aa816_e2e_cis_admin import (  # noqa: F401 — issuer is a fixture
    SECRET, _admin_e2e_row, _admin_e2e_token, _set_env, issuer,
)

SYNTHETIC = "4c3b2a19-0f8e-4d7c-9b6a-5e4d3c2b1093"
REAL = "7e6d5c4b-3a29-4180-9f7e-6d5c4b3a1093"
NULL_FLAG = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b1093"
PILOT = "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c1093"
# one erasure request per tenant
REQUEST_OF = {SYNTHETIC: "5f0e2c1a-0000-4000-8000-00000000a501", REAL: "5f0e2c1a-0000-4000-8000-00000000a502",
              NULL_FLAG: "5f0e2c1a-0000-4000-8000-00000000a503", PILOT: "5f0e2c1a-0000-4000-8000-00000000a504"}
CS_E2E = "e2e-cis-cs@e2e-mail.lumiguides.it.com"
NOW = datetime(2026, 11, 13, tzinfo=timezone.utc)
SAGA = "/admin/job-runner/erasure/requests"
_T = "5f0e2c1a-0000-4000-8000-0000000a10b5"   # fixed: parametrize ids must match in every xdist worker


# ── api/core/e2e_staff.py ────────────────────────────────────────────────────────────────────────

def test_the_drill_routes_are_on_the_allow_list():
    assert e2e_staff.E2E_ERASURE_DRILL_WRITES == {
        ("POST", "/admin/tenants/{tenant_id}/offboard"),
        ("POST", "/admin/tenants/{tenant_id}/restore"),
        ("POST", f"{SAGA}/drill"),
        ("POST", f"{SAGA}/{{request_id}}/advance"),
        ("POST", f"{SAGA}/{{request_id}}/stop"),
    }
    assert e2e_staff.E2E_ERASURE_DRILL_WRITES <= e2e_staff.E2E_ALLOWED_WRITES
    # only a person approves an erasure; a new round is a tech-lead decision
    for action in ("approve", "new-round"):
        assert ("POST", f"{SAGA}/{{request_id}}/{action}") not in e2e_staff.E2E_ALLOWED_WRITES
    assert ("POST", "/admin/job-runner/erasure/run") not in e2e_staff.E2E_ALLOWED_WRITES


@pytest.mark.parametrize("path,allowed", [
    (f"/admin/tenants/{_T}/offboard", True),
    (f"/admin/tenants/{_T}/restore", True),
    (f"{SAGA}/drill", True),
    (f"{SAGA}/{_T}/advance", True),
    (f"{SAGA}/{_T}/stop", True),
    (f"{SAGA}/{_T}/approve", False),
    (f"{SAGA}/{_T}/new-round", False),
    ("/admin/job-runner/erasure/run", False),
    (f"/admin/tenants/{_T}/offboard/", False),
    (f"/admin/tenants/{_T}/restore/x", False),
    ("/admin/tenants/not-a-uuid/offboard", False),
    (f"{SAGA}/drill/", False),
    (f"{SAGA}/not-a-uuid/advance", False),
    (f"/admin/tours/{_T}/restore", False),
])
def test_the_concrete_path_is_matched_in_full(path, allowed):
    assert e2e_staff.is_allowed_e2e_write_path("POST", path) is allowed
    assert e2e_staff.is_allowed_e2e_write_path("PATCH", path) is False


class _Conn:
    def __init__(self, answer: Any) -> None:
        self.answer, self.calls = answer, 0

    async def fetchval(self, sql: str, *args: Any) -> Any:
        assert sql == e2e_staff.SYNTHETIC_TENANT_SQL
        self.calls += 1
        return self.answer


A10B = SimpleNamespace(e2e_read_only=True, e2e_allowlisted_writes=True)
A10D = SimpleNamespace(e2e_read_only=True, e2e_allowlisted_writes=False)
STAFF = SimpleNamespace(e2e_read_only=False, e2e_allowlisted_writes=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity,answer,tenant,expected,reads", [
    (None, None, SYNTHETIC, None, 0),
    (STAFF, False, REAL, None, 0),
    (A10B, True, SYNTHETIC, None, 1),
    (A10B, False, REAL, "tenant_not_synthetic", 1),
    (A10B, None, NULL_FLAG, "tenant_not_synthetic", 1),
    (A10B, True, "not-a-uuid", "tenant_not_synthetic", 0),
    (A10B, True, None, "tenant_not_synthetic", 0),
    (A10D, True, SYNTHETIC, "not_allowlisted", 0),
], ids=["no_identity", "staff", "a10b_synthetic", "a10b_real", "a10b_null_flag", "a10b_bad_id", "a10b_no_id",
        "a10d"])
async def test_synthetic_write_refusal(identity, answer, tenant, expected, reads):
    conn = _Conn(answer)
    assert await e2e_staff.synthetic_write_refusal(identity, conn, tenant) == expected
    assert conn.calls == reads


def _stand_in(identity: Any, **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(scope={"state": {"staff_identity": identity}}, **extra)


def test_refuse_any_e2e():
    for identity in (A10B, A10D):
        with pytest.raises(HTTPException) as e:
            e2e_tenant_writes.refuse_any_e2e(_stand_in(identity), event="x_refused")
        assert (e.value.status_code, e.value.detail) == (403, "e2e_read_only")
    e2e_tenant_writes.refuse_any_e2e(_stand_in(STAFF), event="x_refused")


# ── the real app ─────────────────────────────────────────────────────────────────────────────────

class ErasureDirectory(Directory):
    """The staff directory for the middleware, plus shared.tenants (synthetic, real, NULL flag, pilot; every tenant
    active, so offboard reads it as already offboarded only when asked) and a stand-in saga store. Writes are recorded,
    never executed."""

    FLAGS = {SYNTHETIC: True, REAL: False, NULL_FLAG: None, PILOT: False}

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple] = []
        self.request_audit: list[tuple] = []
        self.synthetic_checks = 0
        self.offboarded: set[str] = set()

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        if sql in (staff_identity.SQL_BY_IDENTITY, staff_identity.SQL_FIRST_LINK, staff_identity.SQL_BY_EMAIL):
            return await super().fetchrow(sql, *args)
        tid = str(args[0])
        if tid not in self.FLAGS:
            return None
        cancelled = NOW if tid in self.offboarded else None
        if sql == ar.TENANT_SQL:                                                   # drill
            return {"tenant_id": tid, "is_pilot": tid == PILOT, "cancelled_at": cancelled}
        if "FROM shared.tenants WHERE tenant_id" in sql.replace("\n", " "):        # offboard, restore
            return {"tenant_id": tid, "name": "Lantern Bay Trails", "slug": "lantern-bay", "is_active": True,
                    "cancelled_at": cancelled, "cancellation_reason": None}
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
        self.writes.append(("execute", sql, args))
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any) -> list:
        raise AssertionError(sql)


@pytest.fixture
def real(monkeypatch, issuer):  # noqa: F811
    _set_env(monkeypatch, "dev")
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", SECRET)
    directory = ErasureDirectory()
    admin_id = _admin_e2e_row(directory)
    monkeypatch.setattr(real_app.state, "pool", directory, raising=False)
    by_id = {rid: tid for tid, rid in REQUEST_OF.items()}
    reads: list[str] = []

    async def _get_request(conn, rid, for_update=False):
        reads.append(rid)
        if rid not in by_id:
            return None
        return {"request_id": rid, "tenant_id": by_id[rid], "phase": "dry_run", "mode": "drill", "round": 1,
                "round_apps": [], "blockers": []}

    async def _ensure_job(pool, rid, phase, round_, created_by=None, run_now=False):
        directory.writes.append(("job", rid, phase, created_by, run_now))
        return "9d8c7b6a-5f4e-4d3c-8b2a-190f8e7d1093", True

    async def _move(conn, rid, from_phases, to_phase, **fields):
        directory.writes.append(("move", rid, to_phase, fields))
        return True

    async def _audit(conn, **kw):
        directory.writes.append(("audit", kw["action"], kw["actor"]))

    from services.jobs import tenant_erasure_saga_job as sj
    monkeypatch.setattr(ar.store, "get_request", _get_request)
    monkeypatch.setattr(ar.store, "move", _move)
    monkeypatch.setattr(sj, "ensure_job", _ensure_job)
    monkeypatch.setattr(ar, "write_audit_log", _audit)
    return TestClient(real_app), directory, admin_id, reads


def _post(client, token: str, path: str, body: Optional[dict] = None):
    return client.post(path, headers={"Authorization": f"Bearer {token}"}, json=body if body is not None else {})


def _routes(tenant: str) -> dict[str, tuple[str, Optional[dict]]]:
    rid = REQUEST_OF[tenant]
    return {
        "offboard": (f"/admin/tenants/{tenant}/offboard", {"reason": "A10b-5 drill of a synthetic tenant"}),
        "restore": (f"/admin/tenants/{tenant}/restore", None),
        "drill": (f"{SAGA}/drill", {"tenant_id": tenant}),
        "advance": (f"{SAGA}/{rid}/advance", None),
        "stop": (f"{SAGA}/{rid}/stop", None),
    }


ROUTES = ["offboard", "restore", "drill", "advance", "stop"]


def test_a_synthetic_tenant_passes_the_check_and_the_routes_answer_as_for_any_admin(real, issuer):  # noqa: F811
    """The spec's probes: drill / restore on an active synthetic tenant answer 409 from the route itself; offboard of
    an already-offboarded one 409 too; advance and stop on its request run (a job queued, the request stopped)."""
    client, directory, admin_id, _ = real
    token = _admin_e2e_token(issuer)
    routes = _routes(SYNTHETIC)
    res = {name: _post(client, token, *routes[name]) for name in ("drill", "restore")}
    assert res["drill"].status_code == 409 and "not offboarded" in res["drill"].json()["detail"]
    assert res["restore"].status_code == 409 and "not offboarded" in res["restore"].json()["detail"]
    directory.offboarded.add(SYNTHETIC)
    off = _post(client, token, *routes["offboard"])
    assert off.status_code == 409 and "already offboarded" in off.json()["detail"]
    adv = _post(client, token, *routes["advance"])
    assert adv.status_code == 200, adv.text
    assert adv.json()["created"] is True
    stop = _post(client, token, *routes["stop"])
    assert stop.status_code == 200 and stop.json()["stopped"] is True
    kinds = [w[0] for w in directory.writes]
    assert kinds == ["job", "move", "audit"]
    assert directory.writes[0][3] == f"admin:{admin_id}" and directory.writes[2][2] == f"admin:{admin_id}"
    assert directory.synthetic_checks >= 5
    assert all(403 not in row for row in directory.request_audit) and len(directory.request_audit) == 5


@pytest.mark.parametrize("tenant", [REAL, NULL_FLAG, PILOT], ids=["real", "null_flag", "pilot"])
@pytest.mark.parametrize("route", ROUTES)
def test_a_real_tenant_or_the_pilot_is_refused_before_any_write(real, issuer, tenant, route):  # noqa: F811
    client, directory, _, _ = real
    directory.offboarded.add(tenant)   # so a route that ran would write
    with structlog.testing.capture_logs() as logs:
        res = _post(client, _admin_e2e_token(issuer), *_routes(tenant)[route])
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    assert directory.writes == []
    assert any(e.get("reason") == "tenant_not_synthetic" and e.get("event") in (
        "tenant_offboarding_e2e_refused", ar.E2E_REFUSED) for e in logs)
    (row,), = [directory.request_audit]   # the refused call still leaves its AA-884 row, with the 403
    assert 403 in row


def test_advance_and_stop_of_an_unknown_request_are_404(real, issuer):  # noqa: F811
    client, directory, _, _ = real
    for action in ("advance", "stop"):
        res = _post(client, _admin_e2e_token(issuer), f"{SAGA}/{_T}/{action}")
        assert res.status_code == 404
    assert directory.writes == [] and directory.synthetic_checks == 0


@pytest.mark.parametrize("action,body", [("approve", {"fingerprint": "a" * 64, "snapshot_id": "snap-a10b5"}),
                                         ("new-round", None)], ids=["approve", "new_round"])
def test_approve_and_new_round_stay_refused_even_on_a_synthetic_tenant(real, issuer, action, body):  # noqa: F811
    client, directory, _, reads = real
    res = _post(client, _admin_e2e_token(issuer), f"{SAGA}/{REQUEST_OF[SYNTHETIC]}/{action}", body)
    assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"})
    assert reads == [] and directory.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["approve", "new_round"])
async def test_approve_and_new_round_refuse_the_session_inside_the_route_too(real, call):
    """Defence in depth: the route itself refuses any Dev-only E2E identity, before the request is read."""
    _, directory, _, reads = real
    req = _stand_in(A10B, app=SimpleNamespace(state=SimpleNamespace(pool=directory)), headers={"x-admin-role": "admin"})
    with pytest.raises(HTTPException) as e:
        if call == "approve":
            await ar.approve(REQUEST_OF[SYNTHETIC], ar.ApproveRequest(fingerprint="a" * 64, snapshot_id="s"), req)
        else:
            await ar.new_round(REQUEST_OF[SYNTHETIC], req)
    assert (e.value.status_code, e.value.detail) == (403, "e2e_read_only")
    assert reads == [] and directory.writes == []


def test_the_a10d_identity_is_refused_even_on_a_synthetic_tenant(real, issuer):  # noqa: F811
    client, directory, _, _ = real
    directory.add(role="customer_success", email=CS_E2E, iss=ISS, sub="cs-sub-0001", username="e2e-cis-cs")
    token = issuer.token(email=CS_E2E, sub="cs-sub-0001", **{"cognito:groups": ["cis-customer_success"]})
    for name, (path, body) in _routes(SYNTHETIC).items():
        res = _post(client, token, path, body)
        assert (res.status_code, res.json()) == (403, {"detail": "e2e_read_only"}), name
    assert directory.writes == [] and directory.synthetic_checks == 0


def test_the_drill_writes_outside_dev_are_refused(real, issuer, monkeypatch):  # noqa: F811
    client, directory, _, _ = real
    _set_env(monkeypatch, "prod")
    staff_identity.clear_cache()
    res = _post(client, _admin_e2e_token(issuer), *_routes(SYNTHETIC)["advance"])
    assert res.status_code == 403 and directory.writes == []


@pytest.mark.parametrize("route", sorted(e2e_staff.E2E_ERASURE_DRILL_WRITES))
def test_the_route_policy_lets_the_a10b_session_through(route):
    principal = staff_authz.principal_from_role("admin", A10B)
    assert staff_authz.denial_reason(principal, *route) is None
    assert staff_authz.denial_reason(staff_authz.principal_from_role("admin", A10D), *route) == "e2e_read_only"


@pytest.mark.asyncio
async def test_a_normal_admin_is_unaffected():
    """Control: the checks are no-ops (no DB read) for every identity that is not a Dev-only E2E session."""
    pool = SimpleNamespace()   # any use would raise
    with structlog.testing.capture_logs() as logs:
        await e2e_tenant_writes.refuse_unless_synthetic(_stand_in(STAFF), pool, REAL, event="x")
        await e2e_tenant_writes.refuse_unless_synthetic(_stand_in(None), pool, REAL, event="x")
        await e2e_tenant_writes._synthetic_path_tenant(_stand_in(STAFF, path_params={"tenant_id": REAL}))
        e2e_tenant_writes.refuse_any_e2e(_stand_in(STAFF), event="x")
    assert logs == []
