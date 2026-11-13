"""AA-1093 lane L2 — the tenant offboarding erasure saga on real Postgres (every migration replayed; needs CIS 408 / 409).

No worker runs: the test calls the saga pass (tenant_erasure_saga_job.advance) and plays the CIS jobs it queues by
setting their status and result, as a worker would. Booking and TripPlanner report through stand-in report views
(over a scratch table, dropped at teardown); the CRM through the real route function. Invented ids only.

  * T0: the offboard hook records the request (CRM T0 + 90 days, the others T0 + 30 days) and emits
    account.erasure_requested; the pilot is never scheduled; a restore cancels it (event status cancelled) and is
    refused once the apply started;
  * dry_run: a missing report view is app_not_ready (a blocker, never a skip); once every app reported, the combined
    fingerprint is sha256(request, tenant, sorted app:hash) and the request waits for the approval;
  * the approval route: the admin secret alone, the switch off, a stale fingerprint and a fingerprint older than 24 h
    are refused; a Cognito admin moves it to apply and pins every app's approved plan hash;
  * apply: CIS waits until Booking, TripPlanner and the CRM all report done (CIS last), then a tenant backup, a fresh
    DRY RUN matched against the approved CIS hash, the data_erasure apply; verify: every census 0 -> done + receipt;
  * the CRM feed lists the request with the CRM's own not_before and serves a change again after the cursor.
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
if not any(f.startswith("408_") for f in os.listdir(MIGRATIONS)):
    pytest.skip("CIS 408 is not in this tree yet (the migration-only PR merges first)", allow_module_level=True)

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
    await p.execute(f"CREATE TABLE IF NOT EXISTS {SCRATCH}.reports (app text, request_id uuid, round smallint, "
                    "mode text, status text, plan_hash text, counts jsonb, pii_left bigint, detail text, "
                    "reported_at timestamptz DEFAULT now())")
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
            return await store.create_request(conn, tid, offboarded_at=t0, mode="drill", actor="admin:pytest")
        return await store.on_offboard(conn, tid, offboarded_at=t0, actor="admin:pytest", is_pilot=is_pilot)


async def _views(p):
    for app in ("booking", "tripplanner"):
        await p.execute(f"CREATE SCHEMA IF NOT EXISTS {app}")
        await p.execute(f"CREATE OR REPLACE VIEW {app}.v_tenant_erasure_report_v1 AS SELECT request_id, round, mode, "
                        f"status, plan_hash, counts, pii_left, detail, reported_at FROM {SCRATCH}.reports "
                        f"WHERE app = '{app}'")


async def _report(p, app, rid, mode, status="done", *, plan_hash=None, pii_left=None, round_=1):
    await p.execute(f"INSERT INTO {SCRATCH}.reports (app, request_id, round, mode, status, plan_hash, counts, "
                    "pii_left) VALUES ($1, $2::uuid, $3, $4, $5, $6, '{\"delete\": 2}'::jsonb, $7)",
                    app, rid, round_, mode, status, plan_hash, pii_left)


def _body(**kw):
    async def _json():
        return kw
    return SimpleNamespace(json=_json, app=SimpleNamespace(state=SimpleNamespace(pool=None)))


async def _crm(p, rid, **kw):
    req = _body(**kw)
    req.app.state.pool = p
    return await tr.report_erasure_step(rid, req, caller=CRM)


async def _play(p, key_part, rid, *, result, status="succeeded"):
    """Act as the worker for the CIS job of this request whose idempotency key ends with ``key_part``."""
    job = await p.fetchrow("SELECT id FROM shared.job WHERE idempotency_key LIKE $1", f"%{rid}:r1:{key_part}")
    assert job is not None, key_part
    await p.execute("UPDATE shared.job SET status = $2, result = $3::jsonb, finished_at = now() WHERE id = $1",
                    job["id"], status, json.dumps(result))
    return str(job["id"])


def _staff(p, identity=None, role="admin"):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=p)), headers={"x-admin-role": role},
                           scope={"state": {"staff_identity": identity}} if identity else {"state": {}})


COGNITO = SimpleNamespace(auth="cognito", admin_id="0f0f0f0f-1093-4000-8000-000000000001")


async def _steps(p, rid):
    return {(r["app"], r["mode"]): r for r in await p.fetch(
        "SELECT app, mode, status, plan_hash, approved_plan_hash, pii_left, job_id FROM shared.tenant_erasure_step "
        "WHERE request_id = $1::uuid", rid)}


async def _req(p, rid):
    return await p.fetchrow("SELECT * FROM shared.tenant_erasure_request WHERE request_id = $1::uuid", rid)


# ── T0 and restore ──────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_offboard_schedules_crm_90_days_others_30_and_emits_the_event(pool):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    out = await _offboard(p, tid)
    assert out["status"] == "scheduled"
    req = await _req(p, out["request_id"])
    assert req["phase"] == "scheduled" and req["mode"] == "live"
    assert req["not_before_crm"] - req["offboarded_at"] == timedelta(days=90)
    for app in ("cis", "booking", "tripplanner"):
        assert req[f"not_before_{app}"] - req["offboarded_at"] == timedelta(days=30)
    ev = await p.fetchval("SELECT change FROM shared.tenant_account_event WHERE tenant_id = $1::uuid "
                          "AND type = 'account.erasure_requested' ORDER BY seq DESC LIMIT 1", tid)
    ev = json.loads(ev)
    assert ev["request_id"] == out["request_id"] and ev["status"] == "scheduled" and set(ev["not_before"]) == set(
        saga.APPS)
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
async def test_restore_cancels_the_request_and_is_refused_once_the_apply_started(pool):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    rid = (await _offboard(p, tid))["request_id"]
    async with p.acquire() as conn, conn.transaction():
        assert await store.on_restore(conn, tid) == rid
    assert (await _req(p, rid))["phase"] == "cancelled"
    ev = json.loads(await p.fetchval("SELECT change FROM shared.tenant_account_event WHERE tenant_id = $1::uuid AND "
                                     "type = 'account.erasure_requested' ORDER BY seq DESC LIMIT 1", tid))
    assert ev["status"] == "cancelled"
    second = (await _offboard(p, tid))["request_id"]
    await p.execute("UPDATE shared.tenant_erasure_request SET phase = 'apply' WHERE request_id = $1::uuid", second)
    with pytest.raises(store.ErasureInProgress):
        async with p.acquire() as conn, conn.transaction():
            await store.on_restore(conn, tid)
    assert (await _req(p, second))["phase"] == "apply"


@pytest.mark.asyncio
async def test_a_live_request_does_not_start_before_its_latest_not_before(pool):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    rid = (await _offboard(p, tid))["request_id"]
    out = await sj.advance(p, rid, now=datetime.now(timezone.utc) + timedelta(days=31))
    assert out["phase"] == "scheduled" and out["reason"] == "not due"       # the CRM's 90 days still run
    assert await sj.ensure_jobs(p, now=datetime.now(timezone.utc) + timedelta(days=31)) == []
    out = await sj.advance(p, rid, now=datetime.now(timezone.utc) + timedelta(days=91))
    assert out["phase"] == "dry_run"


# ── the full drill path ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_drill_dry_run_approval_apply_cis_last_verify(pool, monkeypatch):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    rid = (await _offboard(p, tid, mode="drill"))["request_id"]
    now = datetime.now(timezone.utc) + timedelta(seconds=1)

    # dry_run, no report view yet: Booking / TripPlanner app_not_ready, the CRM pending, CIS's DRY RUN queued
    out = await sj.advance(p, rid, now=now)
    assert out["phase"] == "dry_run" and out["waiting"]
    st = await _steps(p, rid)
    assert st[("booking", "dry_run")]["status"] == "app_not_ready"
    assert st[("tripplanner", "dry_run")]["status"] == "app_not_ready"
    assert st[("crm", "dry_run")]["status"] == "pending" and st[("cis", "dry_run")]["status"] == "pending"
    kinds = {b["kind"] for b in json.loads((await _req(p, rid))["blockers"])}
    assert kinds == {"app_not_ready"}
    await _play(p, "cis:dry_run", rid, result={"mode": "dry_run", "rows": CIS_ROWS, "blocked": False,
                                               "fingerprint": "1" * 64, "totals": {"delete": 5}})
    # the apps report
    await _views(p)
    await _report(p, "booking", rid, "dry_run", plan_hash=PEER_HASH["booking"])
    await _report(p, "tripplanner", rid, "dry_run", plan_hash=PEER_HASH["tripplanner"])
    with pytest.raises(M2MProblem) as e:                                  # a done dry run carries its hash
        await _crm(p, rid, mode="dry_run", status="done")
    assert e.value.status == 400
    await _crm(p, rid, mode="dry_run", status="done", plan_hash=PEER_HASH["crm"])
    out = await sj.advance(p, rid, now=now)
    assert out["phase"] == "awaiting_approval"
    req = await _req(p, rid)
    cis_hash = saga.cis_plan_hash(tid, CIS_ROWS)
    assert req["combined_fingerprint"] == saga.combined_fingerprint(rid, tid, {**PEER_HASH, "cis": cis_hash})

    # the approval
    fp = req["combined_fingerprint"]
    monkeypatch.delenv(policy.APPLY_SWITCH_ENV, raising=False)
    for ident, why in ((None, 403), (COGNITO, 403)):                       # admin secret alone; switch off
        with pytest.raises(HTTPException) as e:
            await ar.approve(rid, ar.ApproveRequest(fingerprint=fp, snapshot_id="snap-drill-1"), _staff(p, ident))
        assert e.value.status_code == why
    monkeypatch.setenv(policy.APPLY_SWITCH_ENV, "on")
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
    assert all(st[(a, "dry_run")]["approved_plan_hash"] == st[(a, "dry_run")]["plan_hash"] for a in saga.APPS)
    # the views serve the approved hash to Booking / TripPlanner only through the catalog view (CIS 410, its own test)

    # apply: CIS waits for every peer (CIS last)
    later = datetime.now(timezone.utc) + timedelta(seconds=2)
    await _report(p, "booking", rid, "apply")
    out = await sj.advance(p, rid, now=later)
    assert out["phase"] == "apply" and out["waiting"] and set(out["waiting_for"]) == {"tripplanner", "crm"}
    assert ("cis", "apply") not in await _steps(p, rid)
    assert await p.fetchval("SELECT count(*) AS n FROM shared.job WHERE idempotency_key LIKE $1",
                            f"%{rid}:r1:cis:%") == 1                         # only the DRY RUN so far
    await _report(p, "tripplanner", rid, "apply")
    await _crm(p, rid, mode="apply", status="done", counts={"delete": 9})
    out = await sj.advance(p, rid, now=later)
    assert out["waiting_for"] == ["cis:backup"]
    await _play(p, "cis:backup", rid, result={"rows": 5})
    out = await sj.advance(p, rid, now=later)
    assert out["waiting_for"] == ["cis:recheck"]
    recheck = await _play(p, "cis:recheck", rid, result={"mode": "dry_run", "fingerprint": "2" * 64, "blocked": False,
                                                         "rows": [dict(r, rows=r["rows"] + (1 if r["action"] == "keep"
                                                                                            else 0))
                                                                  for r in CIS_ROWS]})
    out = await sj.advance(p, rid, now=later)
    assert out["waiting_for"] == ["cis:apply"]
    apply_job = await p.fetchrow("SELECT payload FROM shared.job WHERE idempotency_key LIKE $1", f"%{rid}:r1:cis:apply")
    payload = json.loads(apply_job["payload"])
    assert payload["mode"] == "apply" and payload["dry_run_job_id"] == recheck
    assert payload["approved_fingerprint"] == "2" * 64 and payload["snapshot_id"] == "snap-drill-1"
    assert payload["approved_by"] == ok["approved_by"]
    await _play(p, "cis:apply", rid, result={"mode": "apply", "totals": {"delete": 5}, "verified": True,
                                             "verified_left": {}})
    out = await sj.advance(p, rid, now=later)
    assert out["phase"] == "verify" and out["waiting"]
    # verify: every census 0 -> done
    await _report(p, "booking", rid, "verify", pii_left=0)
    await _report(p, "tripplanner", rid, "verify", pii_left=0)
    with pytest.raises(M2MProblem):
        await _crm(p, rid, mode="verify", status="done")                  # a done census carries pii_left
    await _crm(p, rid, mode="verify", status="done", pii_left=0)
    out = await sj.advance(p, rid, now=later)
    assert out["phase"] == "done"
    req = await _req(p, rid)
    receipt = json.loads(req["receipt"])
    assert req["done_at"] is not None and receipt["apps"]["cis"]["pii_left"] == 0
    assert receipt["combined_fingerprint"] == fp and receipt["outside_database"]


@pytest.mark.asyncio
async def test_a_changed_cis_plan_stops_the_apply(pool, monkeypatch):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    rid = (await _offboard(p, tid, mode="drill"))["request_id"]
    now = datetime.now(timezone.utc) + timedelta(seconds=1)
    await sj.advance(p, rid, now=now)
    await _play(p, "cis:dry_run", rid, result={"mode": "dry_run", "rows": CIS_ROWS, "blocked": False})
    await _views(p)
    for app in ("booking", "tripplanner"):
        await _report(p, app, rid, "dry_run", plan_hash=PEER_HASH[app])
    await _crm(p, rid, mode="dry_run", status="done", plan_hash=PEER_HASH["crm"])
    await sj.advance(p, rid, now=now)
    fp = (await _req(p, rid))["combined_fingerprint"]
    monkeypatch.setenv(policy.APPLY_SWITCH_ENV, "on")
    await ar.approve(rid, ar.ApproveRequest(fingerprint=fp, snapshot_id="snap-2"), _staff(p, COGNITO))
    for app in ("booking", "tripplanner"):
        await _report(p, app, rid, "apply")
    await _crm(p, rid, mode="apply", status="done")
    later = datetime.now(timezone.utc) + timedelta(seconds=2)
    await sj.advance(p, rid, now=later)
    await _play(p, "cis:backup", rid, result={})
    await sj.advance(p, rid, now=later)
    await _play(p, "cis:recheck", rid, result={"mode": "dry_run", "blocked": False, "fingerprint": "3" * 64,
                                               "rows": [dict(CIS_ROWS[0], rows=2)] + CIS_ROWS[1:]})
    out = await sj.advance(p, rid, now=later)
    assert not out["waiting"] and out["blockers"][0]["kind"] == "plan_changed"
    assert (await _steps(p, rid))[("cis", "apply")]["status"] == "plan_changed"
    assert await p.fetchval("SELECT count(*) AS n FROM shared.job WHERE idempotency_key LIKE $1",
                            f"%{rid}:r1:cis:apply") == 0
    # a new round clears the approval and starts the DRY RUN again
    out = await ar.new_round(rid, _staff(p), "pytest")
    req = await _req(p, rid)
    assert out["round"] == 2 and req["phase"] == "dry_run" and req["approved_fingerprint"] is None


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
    assert all(req[f"not_before_{a}"] == req["offboarded_at"] for a in saga.APPS)
    job_id, created = await sj.ensure_job(p, rid, "scheduled", 1)
    assert created and (await sj.ensure_job(p, rid, "scheduled", 1)) == (job_id, False)


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
async def test_crm_feed_serves_its_not_before_and_changes_after_the_cursor(pool):
    p, tenants = pool
    tid = await _tenant(p, tenants)
    rid = (await _offboard(p, tid))["request_id"]
    req = _body()
    req.app.state.pool = p
    page = json.loads((await tr.list_erasure_requests(req, None, "500", caller=CRM)).body)
    mine = [i for i in page["items"] if i["request_id"] == rid]
    assert len(mine) == 1 and mine[0]["phase"] == "scheduled" and mine[0]["approved_plan_hash"] is None
    row = await _req(p, rid)
    assert mine[0]["not_before"].startswith(row["not_before_crm"].astimezone(timezone.utc).date().isoformat())
    cursor = page["next_cursor"]
    after = json.loads((await tr.list_erasure_requests(req, cursor, None, caller=CRM)).body)
    assert rid not in {i["request_id"] for i in after["items"]}
    await p.execute("UPDATE shared.tenant_erasure_request SET phase = 'dry_run' WHERE request_id = $1::uuid", rid)
    again = json.loads((await tr.list_erasure_requests(req, cursor, None, caller=CRM)).body)
    assert [i["phase"] for i in again["items"] if i["request_id"] == rid] == ["dry_run"]
    with pytest.raises(M2MProblem):
        await tr.list_erasure_requests(req, "not-a-cursor", None, caller=CRM)
    with pytest.raises(M2MProblem) as e:                                    # dry_run report outside dry_run
        await _crm(p, str(uuid.uuid4()), mode="dry_run", status="done", plan_hash="a" * 64)
    assert e.value.status == 404
