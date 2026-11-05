"""AA-1028 R1 slices 2-3 — the tenant_provision saga without a database: request validation, the payload round trip,
the job kind (registration, refusals are permanent, progress and result), and the admin routes (POLICY, admin role
only, 202 with the job, 422 / 404). The database steps are covered by tests/integration/test_aa1028_tenant_provision.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.core.staff_authz import POLICY, TENANT_READ, TENANT_WRITE
from services.tenant_account import provision as pv
from shared.jobs import registry
from shared.jobs.queue import Job

KEY = "zz-request-0001"
TID = "7d4c6f0e-1b2a-4c3d-9e8f-0a1b2c3d4e5f"


def _create(**kw):
    body = {"request_key": KEY, "mode": "create", "name": "  Larkspur Journeys  ", "slug": "larkspur-journeys",
            "plan_tier": "growth", "posts_per_week": 4}
    body.update(kw)
    return body


# ── parse ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_parse_create_round_trips():
    req = pv.parse(_create(kind="demo", requested_by="u-1", trial_days=None,
                           add_ons=[{"product": "crm", "plan_key": "crm.core", "seats": 5}]))
    assert req.name == "Larkspur Journeys" and req.mode == "create" and req.kind == "demo"
    assert req.idempotency_key == f"tenant_provision:{KEY}"
    assert req.add_ons == (pv.AddOn("crm", "crm.core", 5),)
    payload = req.to_payload()
    assert payload["requested_by"] == "u-1" and "tenant_id" not in payload
    assert pv.parse(json.loads(json.dumps(payload))) == req


def test_parse_attach_round_trips():
    req = pv.parse({"request_key": KEY, "mode": "attach", "tenant_id": TID.upper(),
                    "add_ons": [{"product": "tripplanner", "plan_key": "tripplanner.pro"}]})
    assert req.tenant_id == TID and req.add_ons[0].seats is None
    payload = req.to_payload()
    assert set(payload) == {"request_key", "mode", "tenant_id", "add_ons"}
    assert pv.parse(payload) == req and req.only_new is False


def test_parse_only_new_round_trips_and_needs_a_real_true():
    """AA-1036 review: an approved add-on request's attach carries only_new (never change a live add-on) through
    the stored job payload; anything but a JSON true is the ordinary attach."""
    base = {"request_key": KEY, "mode": "attach", "tenant_id": TID,
            "add_ons": [{"product": "crm", "plan_key": "crm.core", "seats": 2}]}
    req = pv.parse({**base, "only_new": True})
    assert req.only_new is True and req.to_payload()["only_new"] is True
    assert pv.parse(json.loads(json.dumps(req.to_payload()))) == req
    for value in ("true", 1, None, False):
        assert pv.parse({**base, "only_new": value}).only_new is False, value
    # create and detach never carry it
    assert pv.parse(_create(only_new=True)).only_new is False
    assert pv.parse({**base, "mode": "detach", "products": ["crm"], "only_new": True}).only_new is False


@pytest.mark.parametrize("body,code", [
    ("nope", "invalid_request"),
    (_create(request_key="short"), "invalid_request_key"),
    (_create(request_key="bad key with spaces"), "invalid_request_key"),
    (_create(mode="delete"), "invalid_request"),
    (_create(name=" "), "invalid_name"),
    (_create(slug="Not-A-Slug"), "invalid_slug"),
    (_create(plan_tier="enterprise"), "invalid_plan_tier"),
    (_create(plan_tier="internal"), "invalid_plan_tier"),
    (_create(posts_per_week=0), "invalid_posts_per_week"),
    (_create(posts_per_week=True), "invalid_posts_per_week"),
    (_create(kind="internal"), "invalid_kind"),
    # slice 3: a trial of 1-60 days (the tenant_account_tick ends it)
    (_create(trial_days=0), "invalid_trial_days"),
    (_create(trial_days=61), "invalid_trial_days"),
    (_create(trial_days="14"), "invalid_trial_days"),
    (_create(trial_days=True), "invalid_trial_days"),
    # slice 3: the pilot marker exists on Dev only (ENVIRONMENT is "test" here)
    (_create(kind="agency", pilot_weeks=4), "pilot_dev_only"),
    ({"request_key": KEY, "mode": "detach", "tenant_id": TID}, "invalid_products"),
    ({"request_key": KEY, "mode": "detach", "tenant_id": TID, "products": []}, "invalid_products"),
    ({"request_key": KEY, "mode": "detach", "tenant_id": TID, "products": ["content"]}, "invalid_products"),
    ({"request_key": KEY, "mode": "detach", "tenant_id": TID, "products": ["crm", "crm"]}, "invalid_products"),
    ({"request_key": KEY, "mode": "detach", "tenant_id": "nope", "products": ["crm"]}, "invalid_tenant"),
    (_create(add_ons={"product": "crm"}), "invalid_add_ons"),
    (_create(add_ons=["crm"]), "invalid_add_ons"),
    (_create(add_ons=[{"product": "booking", "plan_key": "booking.x"}]), "invalid_add_ons"),
    (_create(add_ons=[{"product": "crm", "plan_key": "tripplanner.hosted"}]), "invalid_add_ons"),
    (_create(add_ons=[{"product": "crm", "plan_key": "crm.core", "seats": 0}]), "invalid_add_ons"),
    (_create(add_ons=[{"product": "crm", "plan_key": "crm.core", "seats": True}]), "invalid_add_ons"),
    (_create(add_ons=[{"product": "crm", "plan_key": "crm.core"}, {"product": "crm", "plan_key": "crm.pro"}]),
     "invalid_add_ons"),
    (_create(add_ons=[{"product": "crm", "plan_key": "crm.core"}] * 3), "invalid_add_ons"),
    ({"request_key": KEY, "mode": "attach", "tenant_id": "x", "add_ons": []}, "invalid_tenant"),
    ({"request_key": KEY, "mode": "attach", "tenant_id": TID, "add_ons": []}, "invalid_add_ons"),
])
def test_parse_refuses(body, code):
    with pytest.raises(pv.ProvisionError) as info:
        pv.parse(body)
    assert info.value.code == code and str(info.value).startswith(code)


def test_parse_trial_and_detach_round_trip():
    req = pv.parse(_create(trial_days=14))
    assert req.trial_days == 14 and req.kind == "agency" and req.to_payload()["trial_days"] == 14
    assert pv.parse(req.to_payload()) == req
    det = pv.parse({"request_key": KEY, "mode": "detach", "tenant_id": TID, "products": ["tripplanner", "crm"],
                    "add_ons": [{"product": "crm", "plan_key": "crm.core"}]})
    assert det.cancel == ("crm", "tripplanner") and det.add_ons == ()
    assert det.to_payload()["products"] == ["crm", "tripplanner"] and pv.parse(det.to_payload()) == det


def test_dev_guard(monkeypatch):
    """Root ADR 0019 d16: on Dev a new tenant is demo by default; an agency only as the one pilot (1-8 weeks)."""
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert pv.parse(_create()).kind == "demo"
    with pytest.raises(pv.ProvisionError) as e:
        pv.parse(_create(kind="agency"))
    assert e.value.code == "dev_real_tenant_refused"
    pilot = pv.parse(_create(kind="agency", pilot_weeks=8))
    assert pilot.pilot_weeks == 8 and pv.parse(pilot.to_payload()) == pilot
    for bad, code in ((9, "invalid_pilot_weeks"), (0, "invalid_pilot_weeks"), (True, "invalid_pilot_weeks")):
        with pytest.raises(pv.ProvisionError) as e:
            pv.parse(_create(kind="agency", pilot_weeks=bad))
        assert e.value.code == code
    with pytest.raises(pv.ProvisionError) as e:
        pv.parse(_create(kind="demo", pilot_weeks=4))
    assert e.value.code == "pilot_needs_agency"
    monkeypatch.setenv("ENVIRONMENT", "dev ")          # compared exactly
    assert pv.parse(_create()).kind == "agency"


def test_actor_fits_the_feed_column():
    assert pv.actor_for(str(uuid.uuid4())).startswith("job:tenant_provision:")
    assert len(pv.actor_for("x" * 100)) == 64


def test_result_shape():
    req = pv.parse(_create())
    out = pv._result(req, TID, steps=["tenant_created"], subscriptions=[], events=3, warnings=[], resumed=False)
    assert "api_key" not in out and out["kind"] == pv.KIND and out["events_written"] == 3


# ── the job ───────────────────────────────────────────────────────────────────────────────────────────────────────

def _job(payload):
    return Job(id="j-1", kind=pv.KIND, payload=payload, status="running", attempt=1, max_attempts=3, progress={},
               cost_usd=0.0)


def test_kind_registered():
    from services import jobs  # noqa: F401  (registers every kind)

    k = registry.get_kind(pv.KIND)
    assert k is not None and k.concurrency == 1 and k.max_attempts == 3 and not k.background


@pytest.mark.asyncio
async def test_job_runs_the_saga(monkeypatch):
    from services.jobs import tenant_provision_job as job
    from shared.jobs import queue

    monkeypatch.setattr(queue, "set_progress", AsyncMock())
    result = {"tenant_id": TID, "steps": ["tenant_created", "content_active"], "events_written": 12,
              "resumed": False}
    saga = AsyncMock(return_value=result)
    monkeypatch.setattr(pv, "provision", saga)
    ctx = registry.JobContext(MagicMock(), _job(pv.parse(_create()).to_payload()))
    assert await job.run(ctx) == result and ctx.result == result
    assert saga.await_args.kwargs["job_id"] == "j-1"
    assert queue.set_progress.await_args.args[2]["phase"] == "done"


@pytest.mark.asyncio
async def test_job_refusals_are_permanent(monkeypatch):
    from services.jobs import tenant_provision_job as job
    from shared.jobs import queue

    monkeypatch.setattr(queue, "set_progress", AsyncMock())
    with pytest.raises(registry.NonRetryable, match="invalid_request_key"):
        await job.run(registry.JobContext(MagicMock(), _job({"request_key": "x"})))
    monkeypatch.setattr(pv, "provision", AsyncMock(side_effect=pv.ProvisionError("slug_taken", "exists")))
    with pytest.raises(registry.NonRetryable, match="slug_taken"):
        await job.run(registry.JobContext(MagicMock(), _job(pv.parse(_create()).to_payload())))


@pytest.mark.asyncio
async def test_enqueue_uses_the_request_key(monkeypatch):
    from services.jobs import tenant_provision_job as job

    enq = AsyncMock(return_value=("j-7", True))
    monkeypatch.setattr(job, "enqueue", enq)
    req = pv.parse(_create(requested_by="u-9"))
    assert await job.enqueue_request(MagicMock(), req) == ("j-7", True)
    assert enq.await_args.kwargs == {"idempotency_key": f"tenant_provision:{KEY}", "created_by": "admin:u-9"}


# ── the routes ────────────────────────────────────────────────────────────────────────────────────────────────────
_SECRET = "aa1028-provision-test-secret"


@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from api.main import app
    from api.routers import admin, admin_settings

    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    pool = MagicMock()
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    return TestClient(app, raise_server_exceptions=True), pool


def _h(role="admin"):
    h = {"X-Admin-Secret": _SECRET, "x-admin-user-id": "u-admin"}
    if role is not None:
        h["x-admin-role"] = role
    return h


def _row(payload, status="queued", result=None, error=None):
    return {"id": "8a1f2b3c-4d5e-4f60-8a1b-2c3d4e5f6a7b", "status": status, "payload": json.dumps(payload),
            "progress": "{}", "result": None if result is None else json.dumps(result), "error": error}


def test_policy_entries():
    assert POLICY[("POST", "/admin/tenants/provision")] == TENANT_WRITE
    assert POLICY[("POST", "/admin/tenants/{tenant_id}/provision")] == TENANT_WRITE
    assert POLICY[("GET", "/admin/tenants/provision/{job_id}")] == TENANT_READ
    assert POLICY[("POST", "/admin/tenants/{tenant_id}/provision/cancel")] == TENANT_WRITE


@pytest.mark.parametrize("role", ["content", None])
def test_writes_refuse_non_admin(client, role):
    c, _ = client
    body = {"request_key": KEY, "name": "Larkspur", "slug": "larkspur", "plan_tier": "starter", "posts_per_week": 3}
    assert c.post("/admin/tenants/provision", json=body, headers=_h(role)).status_code == 403
    assert c.post(f"/admin/tenants/{TID}/provision", headers=_h(role),
                  json={"request_key": KEY, "add_ons": [{"product": "crm", "plan_key": "crm.core"}]}
                  ).status_code == 403
    assert c.post(f"/admin/tenants/{TID}/provision/cancel", headers=_h(role),
                  json={"request_key": KEY, "products": ["crm"]}).status_code == 403


def test_create_enqueues_one_job(client, monkeypatch):
    from services.jobs import tenant_provision_job as job

    c, pool = client
    enq = AsyncMock(return_value=("8a1f2b3c-4d5e-4f60-8a1b-2c3d4e5f6a7b", True))
    monkeypatch.setattr(job, "enqueue_request", enq)
    body = {"request_key": KEY, "name": "Larkspur Journeys", "slug": "larkspur-journeys", "plan_tier": "starter",
            "posts_per_week": 3, "add_ons": [{"product": "crm", "plan_key": "crm.core"}]}
    pool.fetchrow = AsyncMock(return_value=_row({**body, "mode": "create"}))
    resp = c.post("/admin/tenants/provision", json=body, headers=_h())
    assert resp.status_code == 202, resp.text
    out = resp.json()
    assert out["created"] is True and out["status"] == "queued" and out["request_key"] == KEY
    req = enq.await_args.args[1]
    assert req.mode == "create" and req.requested_by == "u-admin"
    assert req.add_ons == (pv.AddOn("crm", "crm.core", None),)


def test_attach_enqueues_and_status_reads_the_job(client, monkeypatch):
    from services.jobs import tenant_provision_job as job

    c, pool = client
    monkeypatch.setattr(job, "enqueue_request", AsyncMock(return_value=("8a1f2b3c-4d5e-4f60-8a1b-2c3d4e5f6a7b",
                                                                        False)))
    payload = {"request_key": KEY, "mode": "attach", "tenant_id": TID,
               "add_ons": [{"product": "crm", "plan_key": "crm.pro", "seats": 3}]}
    result = {"tenant_id": TID, "steps": ["crm_subscribed"]}
    pool.fetchrow = AsyncMock(return_value=_row(payload, status="succeeded", result=result))
    resp = c.post(f"/admin/tenants/{TID}/provision", headers=_h(),
                  json={"request_key": KEY, "add_ons": payload["add_ons"]})
    assert resp.status_code == 202 and resp.json()["created"] is False
    # AA-1036 review: only_new is set by an add-on request approval only; the admin body cannot carry it
    resp = c.post(f"/admin/tenants/{TID}/provision", headers=_h(),
                  json={"request_key": KEY, "add_ons": payload["add_ons"], "only_new": True})
    assert resp.status_code == 202 and job.enqueue_request.await_args.args[1].only_new is False
    got = c.get("/admin/tenants/provision/8a1f2b3c-4d5e-4f60-8a1b-2c3d4e5f6a7b", headers=_h()).json()
    assert got["status"] == "succeeded" and got["tenant_id"] == TID and got["steps"] == ["crm_subscribed"]
    assert pool.fetchrow.await_args.args[2] == pv.KIND


def test_cancel_enqueues_a_detach_job(client, monkeypatch):
    from services.jobs import tenant_provision_job as job

    c, pool = client
    enq = AsyncMock(return_value=("8a1f2b3c-4d5e-4f60-8a1b-2c3d4e5f6a7b", True))
    monkeypatch.setattr(job, "enqueue_request", enq)
    payload = {"request_key": KEY, "mode": "detach", "tenant_id": TID, "products": ["crm"]}
    pool.fetchrow = AsyncMock(return_value=_row(payload))
    resp = c.post(f"/admin/tenants/{TID}/provision/cancel", headers=_h(),
                  json={"request_key": KEY, "products": ["crm"]})
    assert resp.status_code == 202, resp.text
    req = enq.await_args.args[1]
    assert req.mode == "detach" and req.cancel == ("crm",) and req.tenant_id == TID


def test_validation_and_missing_job(client):
    c, pool = client
    bad = {"request_key": KEY, "name": "X", "slug": "x", "plan_tier": "enterprise", "posts_per_week": 3}
    assert c.post("/admin/tenants/provision", json=bad, headers=_h()).status_code == 422
    assert c.post(f"/admin/tenants/{TID}/provision", json={"request_key": KEY, "add_ons": []},
                  headers=_h()).status_code == 422
    trial = {"request_key": KEY, "name": "Larkspur Journeys", "slug": "larkspur-journeys", "plan_tier": "starter",
             "posts_per_week": 3, "trial_days": 61}
    assert c.post("/admin/tenants/provision", json=trial, headers=_h()).status_code == 422
    assert c.post(f"/admin/tenants/{TID}/provision/cancel", json={"request_key": KEY, "products": ["content"]},
                  headers=_h()).status_code == 422
    pool.fetchrow = AsyncMock(return_value=None)
    assert c.get(f"/admin/tenants/provision/{TID}", headers=_h()).status_code == 404
