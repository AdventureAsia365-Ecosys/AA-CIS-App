"""AA-832 — RDS connections per LOGIN role: the `ops_rds_role_connections` job (one query, one
CloudWatch PutMetricData), its 5-minute enqueue endpoint (admin secret, idempotent while a job is
pending) and its exemption from the pre-deploy guard.

asyncpg and boto3 are mocked — no DB, no AWS. The SQL itself runs on real Postgres in
tests/integration/test_aa832_rds_role_connections.py.
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from shared.jobs import registry
from shared.jobs.queue import Job

_TEST_SECRET = "test-admin-secret"
_JOB_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _admin_secret(monkeypatch):
    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _TEST_SECRET)


def _rows():
    # what ROLE_CONNECTIONS_SQL returns on Dev (16/10/2026 limits)
    return [
        {"rolname": "aa_app_user", "rolconnlimit": 15, "connections": 3},
        {"rolname": "booking", "rolconnlimit": 15, "connections": 12},
        {"rolname": "tripplanner", "rolconnlimit": 30, "connections": 0},
    ]


class StubCloudWatch:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def put_metric_data(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return {}


def _ctx(rows, cloudwatch=None):
    from services.jobs import ops_rds_role_connections_job as mod

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=rows)
    job = Job(id="job-1", kind=mod.KIND, payload={"trigger": "schedule"}, status="running", attempt=1,
              max_attempts=2, progress={}, cost_usd=0.0)
    return registry.JobContext(pool, job, {"cloudwatch": cloudwatch} if cloudwatch else {}), pool


# ── registration ────────────────────────────────────────────────────────────────────────────────

def test_kind_registered():
    from services.jobs import ops_rds_role_connections_job as mod

    k = registry.kinds()["ops_rds_role_connections"]
    assert mod.KIND == "ops_rds_role_connections"
    assert k.concurrency == 1 and k.max_attempts == 2 and k.expected_seconds == 60
    # not background: it must not wait behind a place batch / retention run (claim_caps)
    assert k.background is False
    assert k.deploy_gate_exempt is True


def test_only_this_kind_is_deploy_gate_exempt():
    from shared.jobs.worker import load_kinds

    load_kinds()
    assert [k.name for k in registry.kinds().values() if k.deploy_gate_exempt] == ["ops_rds_role_connections"]


def test_sql_shape():
    from services.jobs.ops_rds_role_connections_job import ROLE_CONNECTIONS_SQL as sql

    flat = " ".join(sql.split())
    assert "FROM pg_roles r LEFT JOIN pg_stat_activity a ON a.usename = r.rolname" in flat
    assert "WHERE r.rolcanlogin AND r.rolconnlimit > 0" in flat
    assert "count(a.pid)" in flat


# ── pure helpers ────────────────────────────────────────────────────────────────────────────────

def test_role_samples_pct():
    from services.jobs.ops_rds_role_connections_job import role_samples

    out = role_samples(_rows())
    assert out[1] == {"role": "booking", "limit": 15, "connections": 12, "pct": 80.0}
    assert out[0]["pct"] == 20.0 and out[2]["pct"] == 0.0
    assert role_samples([{"rolname": "x", "rolconnlimit": 3, "connections": 1}])[0]["pct"] == 33.33


def test_metric_data_two_datums_per_role():
    from services.jobs.ops_rds_role_connections_job import metric_data, role_samples

    ts = datetime(2026, 10, 16, 10, 5, tzinfo=timezone.utc)
    data = metric_data(role_samples(_rows()), ts)
    assert len(data) == 6
    booking = [d for d in data if d["Dimensions"] == [{"Name": "Role", "Value": "booking"}]]
    assert {(d["MetricName"], d["Unit"], d["Value"]) for d in booking} == {
        ("RoleConnections", "Count", 12.0), ("RoleConnectionLimitPct", "Percent", 80.0)}
    assert all(d["Timestamp"] == ts for d in data)


# ── handler ─────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_handler_publishes_one_call():
    from services.jobs.ops_rds_role_connections_job import ROLE_CONNECTIONS_SQL, run

    cw = StubCloudWatch()
    ctx, pool = _ctx(_rows(), cw)
    out = await run(ctx)
    pool.fetch.assert_awaited_once_with(ROLE_CONNECTIONS_SQL)
    assert len(cw.calls) == 1
    assert cw.calls[0]["Namespace"] == "AA/RDS" and len(cw.calls[0]["MetricData"]) == 6
    assert out["published"] == 6 and out["namespace"] == "AA/RDS"
    assert out["roles"]["booking"] == {"connections": 12, "limit": 15, "pct": 80.0}


@pytest.mark.asyncio
async def test_handler_logs_per_role_counts():
    from services.jobs import ops_rds_role_connections_job as mod

    ctx, _ = _ctx(_rows(), StubCloudWatch())
    with patch.object(mod, "logger") as log:
        await mod.run(ctx)
    event, kw = log.info.call_args[0][0], log.info.call_args[1]
    assert event == "ops_rds_role_connections_sampled"
    assert kw["roles"]["tripplanner"] == {"connections": 0, "limit": 30, "pct": 0.0}


@pytest.mark.asyncio
async def test_handler_put_failure_is_raised():
    from services.jobs import ops_rds_role_connections_job as mod

    ctx, _ = _ctx(_rows(), StubCloudWatch(RuntimeError("AccessDenied")))
    with patch.object(mod, "logger") as log, pytest.raises(RuntimeError, match="AccessDenied"):
        await mod.run(ctx)
    assert log.warning.call_args[0][0] == "ops_rds_role_connections_put_failed"


@pytest.mark.asyncio
async def test_handler_no_limited_roles_publishes_nothing():
    from services.jobs.ops_rds_role_connections_job import run

    cw = StubCloudWatch()
    ctx, _ = _ctx([], cw)
    out = await run(ctx)
    assert out["published"] == 0 and out["roles"] == {} and cw.calls == []


@pytest.mark.asyncio
async def test_handler_builds_boto3_client_in_app_region(monkeypatch):
    from services.jobs.ops_rds_role_connections_job import run

    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    client = MagicMock()
    ctx, _ = _ctx(_rows())
    with patch("boto3.client", return_value=client) as make:
        await run(ctx)
    make.assert_called_once_with("cloudwatch", region_name="us-west-1")
    client.put_metric_data.assert_called_once()
    assert client.put_metric_data.call_args.kwargs["Namespace"] == "AA/RDS"

    monkeypatch.setenv("AWS_REGION", "ap-southeast-1")
    ctx, _ = _ctx(_rows())
    with patch("boto3.client", return_value=MagicMock()) as make:
        await run(ctx)
    make.assert_called_once_with("cloudwatch", region_name="ap-southeast-1")


# ── POST /admin/job-runner/rds-role-connections/schedule ────────────────────────────────────────

def _request(pool):
    request = MagicMock()
    request.app.state.pool = pool
    return request


def _pool(pending=None):
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=pending)
    pool.fetchrow = AsyncMock(return_value={"id": _JOB_ID})
    return pool


@pytest.mark.asyncio
async def test_schedule_enqueues_when_idle():
    from api.routers import admin_job_runner

    pool = _pool(pending=None)
    out = await admin_job_runner.rds_role_connections_schedule(_request(pool), x_admin_secret=_TEST_SECRET)
    assert out == {"enqueued": True, "job_id": _JOB_ID}
    args = pool.fetchrow.call_args[0]
    assert "INSERT INTO shared.job" in args[0]
    assert args[1] == "ops_rds_role_connections"
    assert args[3] is None          # no idempotency key: dedupe is by pending state
    assert args[4] == 2             # the kind's max_attempts
    assert args[6] == "scheduler:aa-832"


@pytest.mark.asyncio
async def test_schedule_is_idempotent_while_pending():
    from api.routers import admin_job_runner

    pool = _pool(pending="22222222-2222-2222-2222-222222222222")
    out = await admin_job_runner.rds_role_connections_schedule(_request(pool), x_admin_secret=_TEST_SECRET)
    assert out == {"enqueued": False, "reason": "already_pending",
                   "job_id": "22222222-2222-2222-2222-222222222222"}
    pool.fetchrow.assert_not_awaited()
    sql, kind = pool.fetchval.call_args[0]
    assert kind == "ops_rds_role_connections" and "status IN ('queued', 'running')" in sql


@pytest.mark.asyncio
async def test_schedule_rejects_wrong_or_missing_secret():
    from api.routers import admin_job_runner

    pool = _pool()
    for secret in ("wrong", None):
        with pytest.raises(HTTPException) as exc:
            await admin_job_runner.rds_role_connections_schedule(_request(pool), x_admin_secret=secret)
        assert exc.value.status_code == 403
    pool.fetchval.assert_not_awaited()
    pool.fetchrow.assert_not_awaited()


def test_schedule_route_registered_as_post():
    from api.routers.admin_job_runner import router

    routes = {(r.path, m) for r in router.routes for m in getattr(r, "methods", ())}
    assert ("/admin/job-runner/rds-role-connections/schedule", "POST") in routes


def test_schedule_http_auth_via_app():
    """Through FastAPI: the header name is x-admin-secret, a bad one is 403 before any DB use."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.routers.admin_job_runner import router

    app = FastAPI()
    app.include_router(router)
    pool = _pool(pending=None)
    app.state.pool = pool
    client = TestClient(app)
    path = "/admin/job-runner/rds-role-connections/schedule"
    assert client.post(path, headers={"x-admin-secret": "wrong"}).status_code == 403
    assert client.post(path).status_code == 403
    pool.fetchval.assert_not_awaited()
    ok = client.post(path, headers={"x-admin-secret": _TEST_SECRET})
    assert ok.status_code == 200 and ok.json() == {"enqueued": True, "job_id": _JOB_ID}


# ── deploy gate exemption ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deploy_gate_excludes_exempt_kind():
    from api.routers import admin_job_runner

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])   # only the exempt kind's row is active
    out = await admin_job_runner.deploy_gate(_request(pool), x_admin_secret=_TEST_SECRET)
    assert out["safe_to_deploy"] is True
    assert out["exempt_kinds"] == ["ops_rds_role_connections"]
    sql, exempt = pool.fetch.call_args[0]
    assert "NOT (kind = ANY($1::text[]))" in sql
    assert exempt == ["ops_rds_role_connections"]


@pytest.mark.asyncio
async def test_deploy_gate_still_blocks_other_kinds():
    from api.routers import admin_job_runner

    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[{"status": "queued", "n": 1}])
    out = await admin_job_runner.deploy_gate(_request(pool), x_admin_secret=_TEST_SECRET)
    assert out["active"] == 1 and out["safe_to_deploy"] is False
