"""AA-1093 (S279, root ADR 0006a A10b-5) — the E2E admin's erasure saga DRY RUN drill against real Postgres, logged in
AS cis_app (CIS 244 in the Dev order, as test_aa1093_tenant_backup), so the grants and RLS are the ones the API
container meets on Dev. Seeding and teardown use the superuser. Invented names only.

The route functions are called with a request stand-in carrying the A10b identity (the bearer middleware's
scope["state"]["staff_identity"]); the offboard / restore check is the routes' dependency
(api/core/e2e_tenant_writes.SYNTHETIC_PATH_TENANT), called the way FastAPI calls it.

- a synthetic tenant (is_canary, not the pilot): offboard (the saga's T0 request), drill, Run now (one job queued),
  stop (failed, stopped_by_tech_lead), restore (the request cancelled, the tenant active again); approve and new-round
  are refused 403 on the way, before the request is read;
- a real tenant (is_canary FALSE) and the pilot (is_canary TRUE, is_pilot TRUE): every one of the five writes is
  refused 403 e2e_read_only, and nothing is written (no request moved, no job, no audit row by the E2E actor).
"""
import os
import sys
import uuid
from types import SimpleNamespace

import asyncpg
import psycopg2
import pytest
import pytest_asyncio
from _audit_cleanup import delete_audit_rows  # AA-884: audit_log is append-only (CIS 248)
from fastapi import HTTPException

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import (  # noqa: E402
    CIS_APP_CI_PASSWORD, DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER, x1_244_ci_shape, x1_244_ci_unshape,
    x1_244_undo,
)
import services.jobs  # noqa: E402,F401  (registers every kind, tenant_erasure_saga included)
from api.core import e2e_tenant_writes  # noqa: E402
from api.routers import admin  # noqa: E402
from api.routers import admin_tenant_erasure as ar  # noqa: E402
from services.tenant_erasure import saga, store  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
APP_DSN = f"postgresql://cis_app:{CIS_APP_CI_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
MIGRATION_244 = os.path.join(os.path.dirname(__file__), "..", "..", "api", "migrations", "244_x1_cis_app_runtime.sql")
ADMIN_ID = "0f0f0f0f-1093-4a10-8b05-000000000001"
ACTOR = f"admin:{ADMIN_ID}"
A10B = SimpleNamespace(e2e_read_only=True, e2e_allowlisted_writes=True, auth="cognito", admin_id=ADMIN_ID)


def _body_244_without_guards() -> str:
    sql = open(MIGRATION_244, encoding="utf-8").read()
    return "BEGIN;\n" + sql[sql.index("-- ── Guard 0"):sql.index("-- ── 4. Register")] + "\nCOMMIT;\n"


@pytest.fixture(scope="module", autouse=True)
def dev_shape():
    """CIS 244 in the Dev order (as test_aa1093_tenant_backup), undone after."""
    su = psycopg2.connect(host=DB_HOST, port=DB_PORT, dbname=DB_NAME, user=DB_USER, password=DB_PASS)
    su.autocommit = True
    a = su.cursor()
    try:
        x1_244_ci_shape(a)
        a.execute(_body_244_without_guards())
        a.execute("INSERT INTO shared.schema_versions (version, applied_at, description) "
                  "VALUES ('244', now(), 'test_aa1093_a10b5_erasure_drill_db') ON CONFLICT (version) DO NOTHING")
        x1_244_ci_unshape(a)
        a.execute("ALTER ROLE cis_app LOGIN")
        yield
    finally:
        x1_244_undo(a)
        su.close()


@pytest_asyncio.fixture
async def pools(monkeypatch):
    monkeypatch.setattr(admin, "verify_admin_secret", lambda _s: None)
    su = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    app = await asyncpg.create_pool(APP_DSN, min_size=1, max_size=3)
    assert await app.fetchval("SELECT current_user AS who") == "cis_app"
    tenants: list[str] = []
    yield su, app, tenants
    await app.close()
    for tid in tenants:
        rids = [r["request_id"] for r in await su.fetch(
            "SELECT request_id FROM shared.tenant_erasure_request WHERE tenant_id = $1::uuid", tid)]
        await su.execute("DELETE FROM shared.job WHERE payload->>'request_id' = ANY($1::text[]) "
                         "OR payload->>'tenant_id' = $2", [str(r) for r in rids], tid)
        await su.execute("DELETE FROM shared.tenant_erasure_step WHERE request_id = ANY($1::uuid[])", rids)
        await su.execute("DELETE FROM shared.tenant_erasure_request WHERE tenant_id = $1::uuid", tid)
        await delete_audit_rows(su, "tenant_id = $1", tid)
        await su.execute("UPDATE shared.tenants SET is_pilot = false, pilot_ends_at = NULL WHERE tenant_id = $1::uuid",
                         tid)
    await su.close()


async def _tenant(su, tenants, *, canary: bool, pilot: bool = False, offboarded: bool = False) -> str:
    tid = str(uuid.uuid4())
    tenants.append(tid)
    await su.execute(
        "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, is_active, "
        "kind, is_canary) VALUES ($1::uuid, 'Lanternfall Voyages (synthetic)', $2, 'starter'::plan_tier_enum, 3, 60, "
        "true, 'agency', $3)", tid, f"zz1093e2e-{tid[:12]}", canary)
    if pilot:
        await su.execute("UPDATE shared.tenants SET is_pilot = false, pilot_ends_at = NULL WHERE is_pilot")
        await su.execute("UPDATE shared.tenants SET is_pilot = true, pilot_ends_at = now() + interval '8 weeks' "
                         "WHERE tenant_id = $1::uuid", tid)
    if offboarded:
        await su.execute("UPDATE shared.tenants SET account_status = 'offboarding', cancelled_at = now(), "
                         "is_active = false WHERE tenant_id = $1::uuid", tid)
    return tid


def _request(pool, tenant_id=None) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=pool)), headers={"x-admin-role": "admin"},
                           scope={"state": {"staff_identity": A10B}}, path_params={"tenant_id": tenant_id})


async def _path_check(pool, tid) -> None:
    """The offboard / restore dependency, as FastAPI calls it."""
    (dep,) = e2e_tenant_writes.SYNTHETIC_PATH_TENANT
    await dep.dependency(_request(pool, tid))


async def _refused(call) -> None:
    with pytest.raises(HTTPException) as e:
        await call
    assert (e.value.status_code, e.value.detail) == (403, "e2e_read_only")


async def _phase(su, rid) -> str:
    return await su.fetchval("SELECT phase AS phase FROM shared.tenant_erasure_request WHERE request_id = $1::uuid",
                             rid)


@pytest.mark.asyncio
async def test_the_e2e_admin_drills_a_synthetic_tenant_end_to_end_as_cis_app(pools):
    su, app, tenants = pools
    tid = await _tenant(su, tenants, canary=True)
    req = _request(app, tid)

    await _path_check(app, tid)
    off = await admin.offboard_tenant(tid, admin.OffboardRequest(reason="A10b-5 drill (synthetic)"), req, "s",
                                      x_admin_user_id=ADMIN_ID, x_admin_role="admin")
    assert off["status"] == "offboarded" and off["erasure_request"]
    started = (await ar.start_drill(ar.DrillRequest(tenant_id=tid), req, ADMIN_ID))["request"]
    rid = started["request_id"]
    assert started["mode"] == "drill" and started["phase"] == "dry_run"

    ran = await ar.advance_now(rid, req, ADMIN_ID)
    assert ran["created"] is True
    job = await su.fetchrow("SELECT status AS status, created_by AS created_by FROM shared.job "
                            "WHERE kind = 'tenant_erasure_saga' AND payload->>'request_id' = $1", rid)
    assert job["status"] == "queued" and job["created_by"] == ACTOR

    # never an E2E write, refused before the request is read
    await _refused(ar.approve(rid, ar.ApproveRequest(fingerprint="a" * 64, snapshot_id="snap-a10b5"), req))
    await _refused(ar.new_round(rid, req))
    assert await _phase(su, rid) == "dry_run"

    stopped = await ar.stop(rid, req, ADMIN_ID)
    assert stopped["phase"] == "failed"
    blockers = await su.fetchval("SELECT blockers AS b FROM shared.tenant_erasure_request WHERE request_id = $1::uuid",
                                 rid)
    assert saga.STOPPED_KIND in str(blockers)

    await _path_check(app, tid)
    back = await admin.restore_tenant(uuid.UUID(tid), req, "s", ADMIN_ID, "admin")
    assert back.status == "active" and back.erasure_request_cancelled == rid
    row = await su.fetchrow("SELECT cancelled_at AS cancelled_at, is_active AS is_active FROM shared.tenants "
                            "WHERE tenant_id = $1::uuid", tid)
    assert row["cancelled_at"] is None and row["is_active"] is True
    assert await _phase(su, rid) == "cancelled"
    actions = {r["action"] for r in await su.fetch(
        "SELECT action AS action FROM acp_shared.audit_log WHERE tenant_id = $1 AND actor = $2", tid, ACTOR)}
    assert {"agency.offboard"} <= actions and len(actions) >= 4


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["real", "pilot"])
async def test_a_real_tenant_or_the_pilot_is_refused_every_drill_write(pools, kind):
    su, app, tenants = pools
    tid = await _tenant(su, tenants, canary=kind == "pilot", pilot=kind == "pilot", offboarded=True)
    async with su.acquire() as conn, conn.transaction():
        opened = await conn.fetchval("SELECT cancelled_at AS t0 FROM shared.tenants WHERE tenant_id = $1::uuid", tid)
        rid = (await store.create_request(conn, tid, opened_at=opened, mode="drill", actor="admin:pytest"))[
            "request_id"]
    req = _request(app, tid)

    await _refused(_path_check(app, tid))                                     # offboard and restore
    await _refused(ar.start_drill(ar.DrillRequest(tenant_id=tid), req, ADMIN_ID))
    await _refused(ar.advance_now(rid, req, ADMIN_ID))
    await _refused(ar.stop(rid, req, ADMIN_ID))

    assert await _phase(su, rid) == "dry_run"
    assert await su.fetchval("SELECT count(*) AS n FROM shared.job WHERE payload->>'request_id' = $1", rid) == 0
    assert await su.fetchval("SELECT count(*) AS n FROM acp_shared.audit_log WHERE tenant_id = $1 AND actor = $2",
                             tid, ACTOR) == 0
