"""AA-1093 lane L2 — the tenant offboarding erasure saga on real Postgres (every migration replayed; needs CIS 408 / 409).

No worker runs: the test calls the saga pass (tenant_erasure_saga_job.advance) and plays the CIS jobs it queues by
setting their status and result, as a worker would. Booking and TripPlanner report through stand-in report views
(contract ErasureReportRow, over a scratch table dropped at teardown); the CRM through the real route function with the
contract's ErasureReport body. Invented ids only.

  * T0: the offboard hook records the request (CRM T0 + 90 days, the others T0 + 30 days, is_pilot / is_synthetic
    copied) and emits account.erasure_requested {op requested}; the pilot is never scheduled; a restore cancels it
    ({op cancelled}) and is refused once an app applied;
  * stage 1 (CIS, Booking, TripPlanner): a missing report view is app_not_ready (a blocker, never a skip); the combined
    fingerprint covers the active apps and the round; the approval route refuses the admin secret alone, the switch
    off, a stale fingerprint and one older than 24 h; CIS applies last (backup, fresh DRY RUN, data_erasure apply);
  * stage 2 (the CRM alone, round 2): its own DRY RUN, approval, apply and census, then done with a receipt;
  * the CRM routes: idempotent report_id (201 / 200 / 409 report_conflict), wrong_app, round_superseded,
    phase_mismatch; the feed lists the CRM step (waiting in stage 1) and serves a change again after the cursor.
"""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402

HERE = os.path.dirname(__file__)
MIGRATIONS = os.path.join(HERE, "..", "..", "api", "migrations")
# The migration-only PR (CIS 408-411) merges before this code; until then the branch has no 408 to replay (a skip
# marker, not a module-level skip: pytest still collects the tests and exits 0).
pytestmark = pytest.mark.skipif(not any(f.startswith("408_") for f in os.listdir(MIGRATIONS)),
                                reason="CIS 408 is not in this tree yet (the migration-only PR merges first)")

import services.jobs  # noqa: E402,F401  (registers every kind, data_erasure and tenant_backup included)
from api.core.m2m_auth import M2MCaller, M2MProblem  # noqa: E402
from api.routers import admin_tenant_erasure as ar  # noqa: E402
from api.routers import tenant_erasure as tr  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from services.tenant_erasure import policy, saga, store  # noqa: E402
from services.jobs import tenant_erasure_saga_job as sj  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
SCRATCH = "zz_aa1093"
CIS_ROWS = [{"table": "shared.tenants", "action": "delete", "rows": 1},
            {"table": "acp_shared.subject", "action": "delete", "rows": 4},
            {"table": "acp_shared.audit_log", "action": "keep", "rows": 2}]
PEER_HASH = {"booking": "b" * 64, "tripplanner": "c" * 64, "crm": "d" * 64}
CRM = M2MCaller(client_id="crmclient", scopes=frozenset({"cis.tenant/erasure"}))


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute(f"CREATE SCHEMA IF NOT EXISTS {SCRATCH}")
    await p.execute(f"CREATE TABLE IF NOT EXISTS {SCRATCH}.reports (report_id uuid DEFAULT gen_random_uuid(), "
                    "request_id uuid, tenant_id uuid, app text, round smallint, mode text, status text, error_code text, "
                    "plan_hash text, tables jsonb DEFAULT '[]', blockers jsonb DEFAULT '[]', census jsonb, job_id uuid, "
                    "reported_at timestamptz DEFAULT clock_timestamp())")
    tenants: list[str] = []
    yield p, tenants
    for app in ("booking", "tripplanner"):
        await p.execute(f"DROP VIEW IF EXISTS {app}.v_tenant_erasure_report_v1")
    await p.execute(f"DROP SCHEMA {SCRATCH} CASCADE")
    for tid in tenants:
        rids = [r["request_id"] for r in await p.fetch(
            "SELECT request_id FROM shared.tenant_erasure_request WHERE tenant_id = $1::uuid", tid)]
        await p.execute("DELETE FROM shared.tenant_erasure_step WHERE request_id = ANY($1::uuid[])", rids)
        await p.execute("DELETE FROM shared.tenant_erasure_request WHERE tenant_id = $1::uuid", tid)
        await p.execute("DELETE FROM shared.job WHERE payload->>'request_id' = ANY($1::text[]) "
                        "OR payload->>'tenant' = $2 OR payload->>'tenant_id' = $2", [str(r) for r in rids], tid)
        await p.execute("UPDATE shared.tenants SET is_pilot = false, pilot_ends_at = NULL WHERE tenant_id = $1::uuid",
                        tid)
    await p.close()


async def _tenant(p, tenants, *, offboarded=True, canary=True) -> str:
    tid = str(uuid.uuid4())
    tenants.append(tid)
    await p.execute(
        "INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, posts_per_week, rate_limit_rpm, is_active, "
        "is_canary) VALUES ($1::uuid, $2, $3, 'starter'::plan_tier_enum, 3, 60, true, $4)",
        tid, "Lanternfall Voyages synthetic", f"zz1093-{tid[:12]}", canary)
    if offboarded:
        await p.execute("UPDATE shared.tenants SET account_status = 'offboarding', cancelled_at = now(), "
                        "is_active = false WHERE tenant_id = $1::uuid", tid)
    return tid


async def _offboard(p, tid, *, mode="live", is_pilot=False) -> dict:
    async with p.acquire() as conn, conn.transaction():
        t0 = await conn.fetchval("SELECT cancelled_at FROM shared.tenants WHERE tenant_id = $1::uuid", tid)
        if mode == "drill":
            return await store.create_request(conn, tid, opened_at=t0, mode="drill", actor="admin:pytest")
        return await store.on_offboard(conn, tid, offboarded_at=t0, actor="admin:pytest", is_pilot=is_pilot)


async def _views(p):
    for app in ("booking", "tripplanner"):
        await p.execute(f"CREATE SCHEMA IF NOT EXISTS {app}")
        await p.execute(f"CREATE OR REPLACE VIEW {app}.v_tenant_erasure_report_v1 AS SELECT report_id, request_id, "
                        f"tenant_id, app, round, mode, status, error_code, plan_hash, tables, blockers, census, "
                        f"job_id, reported_at FROM {SCRATCH}.reports WHERE app = '{app}'")


TABLES = '[{"table": "x_app.rows_of_tenant", "action": "delete", "rows": 2, "digest": null}]'


async def _report(p, app, rid, tid, mode, status="ok", *, plan_hash=None, census=None, round_=1):
    await p.execute(f"INSERT INTO {SCRATCH}.reports (request_id, tenant_id, app, round, mode, status, plan_hash, "
                    "tables, census) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb)",
                    rid, tid, app, round_, mode, status, plan_hash, TABLES,
                    json.dumps(census) if census is not None else None)


def _body(**kw):
    async def _json():
        return kw
    return SimpleNamespace(json=_json, app=SimpleNamespace(state=SimpleNamespace(pool=None)))


def _crm_report(rid, tid, mode, *, round_, status="ok", plan_hash=None, census=None, report_id=None, app="crm"):
    return {"report_id": report_id or str(uuid.uuid4()), "request_id": rid, "tenant_id": tid, "app": app,
            "round": round_, "mode": mode, "status": status, "error_code": None, "plan_hash": plan_hash,
            "tables": [{"table": "crm.agency_rows", "action": "shred", "rows": 3, "digest": None}],
            "blockers": [], "census": census, "job_id": None, "reported_at": "2027-02-01T00:00:00Z"}


async def _crm(p, rid, body):
    req = _body(**body)
    req.app.state.pool = p
    out = await tr.report_erasure_step(rid, req, caller=CRM)
    return out.status_code, json.loads(out.body)


async def _play(p, key_part, rid, *, result, status="succeeded", round_=1):
    """Act as the worker for the CIS job of this request whose idempotency key ends with ``key_part``."""
    job = await p.fetchrow("SELECT id FROM shared.job WHERE idempotency_key LIKE $1",
                           f"%{rid}:r{round_}:{key_part}")
    assert job is not None, key_part
    await p.execute("UPDATE shared.job SET status = $2, result = $3::jsonb, finished_at = now() WHERE id = $1",
                    job["id"], status, json.dumps(result))
    return str(job["id"])


def _staff(p, identity=None, role="admin"):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=p)), headers={"x-admin-role": role},
                           scope={"state": {"staff_identity": identity}} if identity else {"state": {}})


def _switch(monkeypatch, on: bool):
    """The registry switch cis.erasure.saga_apply_enabled (the reader serves the compiled default under tests)."""
    real = policy.setting
    monkeypatch.setattr(policy, "setting", lambda key: on if key == policy.APPLY_SETTING else real(key))


COGNITO = SimpleNamespace(auth="cognito", admin_id="0f0f0f0f-1093-4000-8000-000000000001")


async def _steps(p, rid, round_=1):
    return {(r["app"], r["mode"]): r for r in await p.fetch(
        "SELECT app, mode, status, error_code, plan_hash, approved_plan_hash, census, job_id "
        "FROM shared.tenant_erasure_step WHERE request_id = $1::uuid AND round = $2", rid, round_)}


async def _req(p, rid):
    return await p.fetchrow("SELECT * FROM shared.tenant_erasure_request WHERE request_id = $1::uuid", rid)


# ── T0 and restore ──────────────────────────────────────────────────────────────────────────────────────────────────

async def _last_event(p, tid):
    return json.loads(await p.fetchval("SELECT change FROM shared.tenant_account_event WHERE tenant_id = $1::uuid "
                                       "AND type = 'account.erasure_requested' ORDER BY seq DESC LIMIT 1", tid))


@pytest.mark.asyncio
async def test_offboard_schedules_crm_90_days_others_30_and_emits_the_event(pool):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    out = await _offboard(p, tid)
    assert out["status"] == "scheduled"
    req = await _req(p, out["request_id"])
    assert req["phase"] == "dry_run" and req["mode"] == "live" and list(req["round_apps"]) == []
    assert req["is_synthetic"] is True and req["is_pilot"] is False
    assert req["not_before_crm"] - req["opened_at"] == timedelta(days=90)
    for app in ("cis", "booking", "tripplanner"):
        assert req[f"not_before_{app}"] - req["opened_at"] == timedelta(days=30)
    assert await _last_event(p, tid) == {"op": "requested", "request_id": out["request_id"]}
    again = await _offboard(p, tid)
    assert again["status"] == "skipped"          # one open request per tenant


@pytest.mark.asyncio
async def test_the_pilot_is_never_scheduled(pool):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    out = await _offboard(p, tid, is_pilot=True)
    assert out["status"] == "skipped" and "pilot" in out["reason"]
    assert await p.fetchval("SELECT count(*) AS n FROM shared.tenant_erasure_request WHERE tenant_id = $1::uuid",
                            tid) == 0


@pytest.mark.asyncio
async def test_the_route_hook_reads_t0_and_the_pilot_flag_from_the_tenant_row(pool):
    """POST /admin/tenants/{id}/offboard passes only the actor: T0 is the cancelled_at its UPDATE just wrote and the
    pilot flag is the row's."""
    p, tenants = pool
    tid = await _tenant(p, tenants)
    async with p.acquire() as conn, conn.transaction():
        out = await store.on_offboard(conn, tid, actor="admin:pytest")
    assert out["status"] == "scheduled"
    req = await _req(p, out["request_id"])
    assert req["opened_at"] == await p.fetchval("SELECT cancelled_at FROM shared.tenants WHERE tenant_id = $1::uuid",
                                                tid)
    pilot = await _tenant(p, tenants)
    await p.execute("UPDATE shared.tenants SET is_pilot = false, pilot_ends_at = NULL WHERE is_pilot")
    await p.execute("UPDATE shared.tenants SET kind = 'agency', is_pilot = true, pilot_ends_at = now() + "
                    "interval '8 weeks' WHERE tenant_id = $1::uuid", pilot)
    async with p.acquire() as conn, conn.transaction():
        skipped = await store.on_offboard(conn, pilot, actor="admin:pytest")
    assert skipped["status"] == "skipped" and "pilot" in skipped["reason"]


@pytest.mark.asyncio
async def test_restore_cancels_the_request_and_is_refused_once_an_app_applied(pool):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    rid = (await _offboard(p, tid))["request_id"]
    async with p.acquire() as conn, conn.transaction():
        assert await store.on_restore(conn, tid) == rid
    assert (await _req(p, rid))["phase"] == "cancelled"
    assert await _last_event(p, tid) == {"op": "cancelled", "request_id": rid}
    second = (await _offboard(p, tid))["request_id"]
    await p.execute("INSERT INTO shared.tenant_erasure_step (request_id, round, app, mode, status) "
                    "VALUES ($1::uuid, 1, 'booking', 'apply', 'ok')", second)
    with pytest.raises(store.ErasureInProgress):
        async with p.acquire() as conn, conn.transaction():
            await store.on_restore(conn, tid)
    assert (await _req(p, second))["phase"] == "dry_run"


@pytest.mark.asyncio
async def test_a_live_request_waits_for_the_first_not_before_then_stage_one(pool):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    rid = (await _offboard(p, tid))["request_id"]
    soon = datetime.now(timezone.utc) + timedelta(days=29)
    out = await sj.advance(p, rid, now=soon)
    assert out["phase"] == "dry_run" and out["reason"] == "not_due"
    assert rid not in [r for r in await sj.ensure_jobs(p, now=soon)]
    out = await sj.advance(p, rid, now=datetime.now(timezone.utc) + timedelta(days=31))
    assert out["phase"] == "dry_run" and out["waiting"]
    assert list((await _req(p, rid))["round_apps"]) == ["cis", "booking", "tripplanner"]     # the CRM waits


@pytest.mark.asyncio
async def test_a_report_view_outside_the_contract_shape_is_a_named_blocker(pool):
    """A report view that answers rows without the contract's round / status words never leaves the step pending
    silently: it is app_not_ready with the code report_shape_unknown."""
    p, tenants = pool
    tid = await _tenant(p, tenants)
    rid = (await _offboard(p, tid, mode="drill"))["request_id"]
    await _views(p)
    await p.execute("CREATE SCHEMA IF NOT EXISTS tripplanner")
    await p.execute(f"CREATE TABLE {SCRATCH}.tp_log (request_id uuid, tenant_id uuid, mode text, status text, "
                    "plan_hash text, counts jsonb)")
    await p.execute("DROP VIEW tripplanner.v_tenant_erasure_report_v1")
    await p.execute(f"CREATE VIEW tripplanner.v_tenant_erasure_report_v1 AS SELECT * FROM {SCRATCH}.tp_log")
    await p.execute(f"INSERT INTO {SCRATCH}.tp_log VALUES ($1::uuid, $2::uuid, 'dry_run', 'planned', $3, "
                    "'{}'::jsonb)", rid, tid, PEER_HASH["tripplanner"])
    await _report(p, "booking", rid, tid, "dry_run", plan_hash=PEER_HASH["booking"])
    out = await sj.advance(p, rid, now=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert out["phase"] == "dry_run" and out["waiting"]
    st = await _steps(p, rid)
    assert st[("booking", "dry_run")]["status"] == "ok"
    assert st[("tripplanner", "dry_run")]["status"] == "app_not_ready"
    assert st[("tripplanner", "dry_run")]["error_code"] == "report_shape_unknown"


# ── the full drill path: stage 1 then stage 2 ───────────────────────────────────────────────────────────────────────

async def _stage1_to_approval(p, tid, rid, now):
    out = await sj.advance(p, rid, now=now)
    assert out["phase"] == "dry_run" and out["waiting"]
    st = await _steps(p, rid)
    assert st[("booking", "dry_run")]["status"] == "app_not_ready"
    assert st[("tripplanner", "dry_run")]["status"] == "app_not_ready"
    assert st[("cis", "dry_run")]["status"] == "pending" and ("crm", "dry_run") not in st
    assert {b["kind"] for b in json.loads((await _req(p, rid))["blockers"])} == {"app_not_ready"}
    await _play(p, "cis:dry_run", rid, result={"mode": "dry_run", "rows": CIS_ROWS, "blocked": False,
                                               "fingerprint": "1" * 64})
    await _views(p)
    await _report(p, "booking", rid, tid, "dry_run", plan_hash=PEER_HASH["booking"])
    await _report(p, "tripplanner", rid, tid, "dry_run", plan_hash=PEER_HASH["tripplanner"])
    out = await sj.advance(p, rid, now=now)
    assert out["phase"] == "awaiting_approval"
    req = await _req(p, rid)
    plans = {"cis": saga.cis_plan_hash(tid, 1, CIS_ROWS), "booking": PEER_HASH["booking"],
             "tripplanner": PEER_HASH["tripplanner"]}
    assert req["combined_fingerprint"] == saga.combined_fingerprint(rid, tid, 1, plans)
    return req["combined_fingerprint"]


@pytest.mark.asyncio
async def test_drill_stage_one_cis_last_then_stage_two_crm(pool, monkeypatch):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    rid = (await _offboard(p, tid, mode="drill"))["request_id"]
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    fp = await _stage1_to_approval(p, tid, rid, now)

    # the approval
    _switch(monkeypatch, False)
    for ident, why in ((None, 403), (COGNITO, 403)):                       # admin secret alone; switch off
        with pytest.raises(HTTPException) as e:
            await ar.approve(rid, ar.ApproveRequest(fingerprint=fp, snapshot_id="snap-drill-1"), _staff(p, ident))
        assert e.value.status_code == why
    _switch(monkeypatch, True)
    with pytest.raises(HTTPException) as e:
        await ar.approve(rid, ar.ApproveRequest(fingerprint="f" * 64, snapshot_id="snap-drill-1"),
                         _staff(p, COGNITO))
    assert e.value.status_code == 409 and "differs" in e.value.detail
    await p.execute("UPDATE shared.tenant_erasure_request SET fingerprint_at = now() - interval '25 hours' "
                    "WHERE request_id = $1::uuid", rid)
    with pytest.raises(HTTPException) as e:
        await ar.approve(rid, ar.ApproveRequest(fingerprint=fp, snapshot_id="snap-drill-1"), _staff(p, COGNITO))
    assert e.value.status_code == 409 and "older" in e.value.detail
    await p.execute("UPDATE shared.tenant_erasure_request SET fingerprint_at = now() WHERE request_id = $1::uuid", rid)
    ok = await ar.approve(rid, ar.ApproveRequest(fingerprint=fp, snapshot_id="snap-drill-1"), _staff(p, COGNITO))
    assert ok["phase"] == "apply" and ok["approved_by"].startswith("admin:")
    st = await _steps(p, rid)
    assert all(st[(a, "dry_run")]["approved_plan_hash"] == st[(a, "dry_run")]["plan_hash"]
               for a in ("cis", "booking", "tripplanner"))

    # apply: CIS waits for Booking and TripPlanner (CIS last in its stage)
    later = datetime.now(timezone.utc) + timedelta(seconds=2)
    await _report(p, "booking", rid, tid, "apply", plan_hash=PEER_HASH["booking"])
    out = await sj.advance(p, rid, now=later)
    assert out["phase"] == "apply" and out["waiting"] and out["waiting_for"] == ["tripplanner"]
    assert ("cis", "apply") not in await _steps(p, rid)
    await _report(p, "tripplanner", rid, tid, "apply", plan_hash=PEER_HASH["tripplanner"])
    out = await sj.advance(p, rid, now=later)
    assert out["waiting_for"] == ["cis:backup"]
    await _play(p, "cis:backup", rid, result={"rows": 5})
    out = await sj.advance(p, rid, now=later)
    assert out["waiting_for"] == ["cis:recheck"]
    rows = [dict(r, rows=r["rows"] + 1) if r["action"] == "keep" else r for r in CIS_ROWS]   # the saga's audit row
    recheck = await _play(p, "cis:recheck", rid, result={"mode": "dry_run", "fingerprint": "2" * 64, "blocked": False,
                                                         "rows": rows})
    out = await sj.advance(p, rid, now=later)
    assert out["waiting_for"] == ["cis:apply"]
    payload = json.loads(await p.fetchval("SELECT payload FROM shared.job WHERE idempotency_key LIKE $1",
                                          f"%{rid}:r1:cis:apply"))
    assert payload["mode"] == "apply" and payload["dry_run_job_id"] == recheck
    assert payload["approved_fingerprint"] == "2" * 64 and payload["snapshot_id"] == "snap-drill-1"
    assert payload["approved_by"] == ok["approved_by"]
    await _play(p, "cis:apply", rid, result={"mode": "apply", "verified": True, "verified_left": {},
                                             "applied": [{"table": "shared.tenants", "action": "delete", "rows": 1}]})
    out = await sj.advance(p, rid, now=later)
    assert out["phase"] == "verify" and out["waiting"]
    zero = {"pii_columns_checked": 3, "pii_rows_found": 0}
    await _report(p, "booking", rid, tid, "verify", census=zero)
    await _report(p, "tripplanner", rid, tid, "verify", census=zero)
    out = await sj.advance(p, rid, now=later)
    assert out["phase"] == "dry_run" and out["next_round"] == 2                # stage 1 done, the CRM next
    req = await _req(p, rid)
    assert list(req["done_apps"]) == ["cis", "booking", "tripplanner"] and list(req["round_apps"]) == []
    assert req["approved_fingerprint"] is None and req["round"] == 2

    # stage 2: the CRM alone (drill: its not_before is T0 too)
    out = await sj.advance(p, rid, now=later)
    assert out["phase"] == "dry_run" and out["waiting_for"] == ["crm"]
    assert list((await _req(p, rid))["round_apps"]) == ["crm"]
    crm_hash = "d" * 64
    dry = _crm_report(rid, tid, "dry_run", round_=2, plan_hash=crm_hash)
    assert (await _crm(p, rid, dry))[0] == 201
    assert await _crm(p, rid, dry) == (200, {"report_id": dry["report_id"], "created": False, "phase": "dry_run"})
    with pytest.raises(M2MProblem) as e:
        await _crm(p, rid, {**dry, "plan_hash": "e" * 64})
    assert e.value.status == 409 and e.value.error == "report_conflict"
    with pytest.raises(M2MProblem) as e:
        await _crm(p, rid, _crm_report(rid, tid, "dry_run", round_=1, plan_hash=crm_hash))
    assert e.value.error == "round_superseded"
    with pytest.raises(M2MProblem) as e:
        await _crm(p, rid, _crm_report(rid, tid, "dry_run", round_=2, plan_hash=crm_hash, app="booking"))
    assert e.value.status == 403 and e.value.error == "wrong_app"
    with pytest.raises(M2MProblem) as e:
        await _crm(p, rid, _crm_report(rid, tid, "apply", round_=2, plan_hash=crm_hash))
    assert e.value.error == "phase_mismatch"
    out = await sj.advance(p, rid, now=later)
    assert out["phase"] == "awaiting_approval"
    fp2 = (await _req(p, rid))["combined_fingerprint"]
    assert fp2 == saga.combined_fingerprint(rid, tid, 2, {"crm": crm_hash})
    await ar.approve(rid, ar.ApproveRequest(fingerprint=fp2, snapshot_id="snap-drill-2"), _staff(p, COGNITO))
    assert (await _steps(p, rid, 2))[("crm", "dry_run")]["approved_plan_hash"] == crm_hash
    assert (await _crm(p, rid, _crm_report(rid, tid, "apply", round_=2, plan_hash=crm_hash)))[0] == 201
    out = await sj.advance(p, rid, now=later)
    assert out["phase"] == "verify" and out["waiting_for"] == ["crm"]
    with pytest.raises(M2MProblem):
        await _crm(p, rid, _crm_report(rid, tid, "verify", round_=2))       # an ok census report carries the census
    assert (await _crm(p, rid, _crm_report(rid, tid, "verify", round_=2, census=zero)))[0] == 201
    out = await sj.advance(p, rid, now=later)
    assert out["phase"] == "done"
    req = await _req(p, rid)
    receipt = json.loads(req["receipt"])
    assert req["done_at"] is not None and list(req["done_apps"]) == list(saga.APPS)
    assert receipt["apps"]["crm"]["round"] == 2 and receipt["apps"]["cis"]["round"] == 1
    assert all(receipt["apps"][a]["pii_rows_found"] == 0 for a in saga.APPS) and receipt["outside_database"]


@pytest.mark.asyncio
async def test_a_changed_cis_plan_stops_the_apply(pool, monkeypatch):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    rid = (await _offboard(p, tid, mode="drill"))["request_id"]
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    fp = await _stage1_to_approval(p, tid, rid, now)
    _switch(monkeypatch, True)
    await ar.approve(rid, ar.ApproveRequest(fingerprint=fp, snapshot_id="snap-2"), _staff(p, COGNITO))
    for app in ("booking", "tripplanner"):
        await _report(p, app, rid, tid, "apply", plan_hash=PEER_HASH[app])
    later = datetime.now(timezone.utc) + timedelta(seconds=2)
    await sj.advance(p, rid, now=later)
    await _play(p, "cis:backup", rid, result={})
    await sj.advance(p, rid, now=later)
    await _play(p, "cis:recheck", rid, result={"mode": "dry_run", "blocked": False, "fingerprint": "3" * 64,
                                               "rows": [dict(CIS_ROWS[0], rows=2)] + CIS_ROWS[1:]})
    out = await sj.advance(p, rid, now=later)
    assert not out["waiting"] and out["blockers"][0]["kind"] == "plan_changed"
    cis = (await _steps(p, rid))[("cis", "apply")]
    assert cis["status"] == "refused" and cis["error_code"] == "plan_changed"
    assert await p.fetchval("SELECT count(*) AS n FROM shared.job WHERE idempotency_key LIKE $1",
                            f"%{rid}:r1:cis:apply") == 0
    out = await ar.new_round(rid, _staff(p), "pytest")
    req = await _req(p, rid)
    assert out["round"] == 2 and req["phase"] == "dry_run" and req["approved_fingerprint"] is None
    assert list(req["round_apps"]) == []


@pytest.mark.asyncio
async def test_drill_route_needs_a_synthetic_offboarded_tenant(pool):
    p, tenants = pool
    real = await _tenant(p, tenants, canary=False)
    with pytest.raises(HTTPException) as e:
        await ar.start_drill(ar.DrillRequest(tenant_id=real), _staff(p), "pytest")
    assert e.value.status_code == 409 and "synthetic" in e.value.detail
    live = await _tenant(p, tenants, offboarded=False)
    with pytest.raises(HTTPException) as e:
        await ar.start_drill(ar.DrillRequest(tenant_id=live), _staff(p), "pytest")
    assert "not offboarded" in e.value.detail
    tid = await _tenant(p, tenants)
    rid = (await _offboard(p, tid))["request_id"]                          # a live request, 30 / 90 days
    out = await ar.start_drill(ar.DrillRequest(tenant_id=tid), _staff(p), "pytest")
    req = await _req(p, rid)
    assert out["request"]["request_id"] == rid and req["mode"] == "drill"
    assert all(req[f"not_before_{a}"] == req["opened_at"] for a in saga.APPS)
    job_id, created = await sj.ensure_job(p, rid, "dry_run", 1)
    assert created and (await sj.ensure_job(p, rid, "dry_run", 1)) == (job_id, False)


@pytest.mark.asyncio
async def test_a_pilot_request_fails(pool):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    rid = (await _offboard(p, tid, mode="drill"))["request_id"]
    await p.execute("UPDATE shared.tenants SET is_pilot = false, pilot_ends_at = NULL WHERE is_pilot")
    await p.execute("UPDATE shared.tenants SET kind = 'agency', is_pilot = true, pilot_ends_at = now() + "
                    "interval '8 weeks' WHERE tenant_id = $1::uuid", tid)
    out = await sj.advance(p, rid, now=datetime.now(timezone.utc) + timedelta(seconds=1))
    assert out["phase"] == "failed" and (await _req(p, rid))["phase"] == "failed"


@pytest.mark.asyncio
async def test_crm_feed_serves_its_step_and_changes_after_the_cursor(pool):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    rid = (await _offboard(p, tid))["request_id"]
    req = _body()
    req.app.state.pool = p
    page = json.loads((await tr.list_erasure_requests(req, None, "200", caller=CRM)).body)
    mine = [i for i in page["items"] if i["request_id"] == rid]
    assert len(mine) == 1 and mine[0]["phase"] == "dry_run" and mine[0]["is_synthetic"] is True
    step = mine[0]["steps"]
    row = await _req(p, rid)
    assert len(step) == 1 and step[0]["app"] == "crm" and step[0]["status"] == "waiting"
    assert step[0]["approved_plan_hash"] is None
    assert step[0]["not_before"].startswith(row["not_before_crm"].astimezone(timezone.utc).date().isoformat())
    cursor = page["next_cursor"]
    after = json.loads((await tr.list_erasure_requests(req, cursor, None, caller=CRM)).body)
    assert rid not in {i["request_id"] for i in after["items"]}
    await p.execute("UPDATE shared.tenant_erasure_request SET round_apps = '{crm}' WHERE request_id = $1::uuid", rid)
    again = json.loads((await tr.list_erasure_requests(req, cursor, None, caller=CRM)).body)
    assert [i["steps"][0]["status"] for i in again["items"] if i["request_id"] == rid] == ["active"]
    with pytest.raises(M2MProblem):
        await tr.list_erasure_requests(req, "not-a-cursor", None, caller=CRM)
    with pytest.raises(M2MProblem):
        await tr.list_erasure_requests(req, None, "500", caller=CRM)        # the contract caps limit at 200
    with pytest.raises(M2MProblem) as e:
        await _crm(p, str(uuid.uuid4()), _crm_report(str(uuid.uuid4()), tid, "dry_run", round_=1))
    assert e.value.status == 422                                            # body request_id differs from the path
