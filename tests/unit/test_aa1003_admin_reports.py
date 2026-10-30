"""AA-1003 R1 — /admin/reports through the real app (staff_policy, BFF headers), store and queue faked.

- Reads need ops.read (admin only), the run needs ops.write (admin only); the viewer and the other roles get 403.
- GET /admin/reports returns every registry report with its due state; runs are paged server-side.
- POST run: 404 unknown report, 409 a report not generated here, 422 for an open / too old / too long period, 202
  with the queued run for EX-04 (default period = the day the daily chain covers).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from api.core import staff_authz
from api.routers import admin_reports
from services.reports import periods

_SECRET = "aa1003-test-admin-secret"
RUN_ID = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def api(monkeypatch):
    from api.main import app
    from api.routers import admin, admin_settings

    monkeypatch.setenv("ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin, "ADMIN_SECRET", _SECRET)
    monkeypatch.setattr(admin_settings, "ADMIN_SECRET", _SECRET)
    conn = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=ctx)
    monkeypatch.setattr(app.state, "pool", pool, raising=False)
    monkeypatch.setattr(app.state, "redis", None, raising=False)
    monkeypatch.setattr(admin_reports.store, "last_runs", AsyncMock(return_value={}))
    monkeypatch.setattr(admin_reports.store, "latest_kpi_day", AsyncMock(return_value=date(2026, 10, 28)))
    monkeypatch.setattr(admin_reports.store, "list_runs", AsyncMock(return_value=([_run()], 41)))
    monkeypatch.setattr(admin_reports.store, "get_run", AsyncMock(return_value=_run(output='{"kpis": []}')))
    enqueue = AsyncMock(side_effect=lambda pool, report, first, last, **kw: {
        "run_id": RUN_ID, "job_id": "j", "report_id": report.id, "period_start": first.isoformat(),
        "period_end": last.isoformat(), "status": "queued", "trigger": kw["trigger"]})
    monkeypatch.setattr(admin_reports.report_jobs, "enqueue_report_run", enqueue)
    client = TestClient(app)
    client.enqueue = enqueue
    return client


def _run(output=None):
    now = datetime(2026, 10, 29, 1, 0, tzinfo=timezone.utc)
    return {"id": RUN_ID, "report_id": "EX-04", "period_start": date(2026, 10, 28), "period_end": date(2026, 10, 28),
            "status": "succeeded", "trigger": "schedule", "triggered_by": "scheduler:aa-1003", "job_id": None,
            "started_at": now, "finished_at": now, "created_at": now, "summary": "s", "error": None, "s3_key": None,
            "approved_by": None, "approved_at": None, "updated_at": now, "output": output, "output_markdown": "# EX-04"}


def _staff(role="admin"):
    return {"X-Admin-Secret": _SECRET, "x-admin-role": role, "x-admin-user-id": "staff-1"}


def test_policy_is_admin_only():
    assert staff_authz.POLICY[("GET", "/admin/reports")] == staff_authz.OPS_READ
    assert staff_authz.POLICY[("GET", "/admin/reports/{report_id}/runs")] == staff_authz.OPS_READ
    assert staff_authz.POLICY[("GET", "/admin/reports/runs/{run_id}")] == staff_authz.OPS_READ
    assert staff_authz.POLICY[("POST", "/admin/reports/{report_id}/run")] == staff_authz.OPS_WRITE


@pytest.mark.parametrize("role", ["reviewer", "content", "viewer"])
def test_other_roles_are_refused(api, role):
    assert api.get("/admin/reports", headers=_staff(role)).status_code == 403
    assert api.post("/admin/reports/EX-04/run", json={}, headers=_staff(role)).status_code == 403


def test_list_returns_every_report_with_its_due_state(api):
    r = api.get("/admin/reports", headers=_staff())
    assert r.status_code == 200
    body = r.json()
    assert len(body["reports"]) == body["registry"]["reports"] >= 60
    ex04 = next(x for x in body["reports"] if x["id"] == "EX-04")
    assert ex04["runnable"] is True and ex04["due"]["state"] in ("due", "overdue")
    fi07 = next(x for x in body["reports"] if x["id"] == "FI-07")
    assert fi07["runnable"] is False and fi07["due"]["state"] == "not_automated"
    fi01 = next(x for x in body["reports"] if x["id"] == "FI-01")       # R3: monthly, generated here
    assert fi01["runnable"] is True and fi01["due"]["state"] in ("ok", "due", "overdue")
    assert body["latest_kpi_day"] == "2026-10-28"


def test_runs_are_paged_on_the_server(api):
    r = api.get("/admin/reports/EX-04/runs?page=2&page_size=10", headers=_staff())
    assert r.status_code == 200
    body = r.json()
    assert (body["total"], body["page"], body["page_size"]) == (41, 2, 10)
    assert admin_reports.store.list_runs.await_args.kwargs == {"limit": 10, "offset": 10}
    assert body["report"]["sections"] and body["report"]["kpi_details"][0]["definition"]
    assert api.get("/admin/reports/XX-99/runs", headers=_staff()).status_code == 404


def test_one_run_carries_its_output(api):
    r = api.get(f"/admin/reports/runs/{RUN_ID}", headers=_staff())
    assert r.status_code == 200 and r.json()["output"] == {"kpis": []} and r.json()["markdown"] == "# EX-04"
    assert api.get("/admin/reports/runs/not-a-uuid", headers=_staff()).status_code == 404


def test_run_queues_ex04_for_the_default_day(api):
    r = api.post("/admin/reports/EX-04/run", json={}, headers=_staff())
    assert r.status_code == 202, r.text
    day = periods.snapshot_day().isoformat()
    assert (r.json()["period_start"], r.json()["period_end"]) == (day, day)
    kw = api.enqueue.await_args.kwargs
    assert kw == {"trigger": "manual", "triggered_by": "admin:staff-1"}


def test_run_refusals(api):
    assert api.post("/admin/reports/ZZ-01/run", json={}, headers=_staff()).status_code == 404
    assert api.post("/admin/reports/FI-07/run", json={}, headers=_staff()).status_code == 409
    today = periods.today_ict().isoformat()
    assert api.post("/admin/reports/EX-04/run", json={"period_end": today}, headers=_staff()).status_code == 422
    assert api.post("/admin/reports/EX-04/run", json={"period_start": "2025-01-01"},
                    headers=_staff()).status_code == 422
    end = periods.latest_closed_day()
    start = date.fromordinal(end.toordinal() - 40)
    assert api.post("/admin/reports/EX-04/run", json={"period_start": start.isoformat(), "period_end": end.isoformat()},
                    headers=_staff()).status_code == 422
    assert api.post("/admin/reports/EX-04/run", json={"bogus": 1}, headers=_staff()).status_code == 422
    api.enqueue.assert_not_awaited()


def test_snapshot_route_queues_a_manual_snapshot(api, monkeypatch):
    assert staff_authz.POLICY[("POST", "/admin/reports/snapshots")] == staff_authz.OPS_WRITE
    enqueue = AsyncMock(return_value=("job-9", True))
    monkeypatch.setattr(admin_reports, "enqueue", enqueue)
    day = periods.latest_closed_day().isoformat()
    r = api.post("/admin/reports/snapshots", json={"day": day}, headers=_staff())
    assert r.status_code == 202 and r.json() == {"job_id": "job-9", "kind": "kpi_snapshot_daily", "day": day}
    assert enqueue.await_args.args[2] == {"trigger": "manual", "day": day}
    assert api.post("/admin/reports/snapshots", json={"day": periods.today_ict().isoformat()},
                    headers=_staff()).status_code == 422
    assert api.post("/admin/reports/snapshots", json={}, headers=_staff("viewer")).status_code == 403
