"""AA-1140 (S289) — the status sampler and GET /admin/overview/status on a real Postgres (conftest replays every
migration, CIS 476 included): a tick writes one sample per service, health check, queue and database; a deployment and
a firing alarm become events, a re-run of the bucket writes nothing twice, the next tick closes the alarm and the
completed rollout; then build_status reads them back (runtimes with the uptime strip, the event timeline, the alarm
band, the verdict). AWS and the peer apps are fakes; invented service and alarm names only. Rows are removed by the
test's own subjects."""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import ops_status  # noqa: E402
from api.services.ops_status import sampler, sections  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ENV = "zz1140"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        yield p
    finally:
        await p.execute("DELETE FROM acp_shared.ops_event WHERE subject LIKE 'aa-%-zz1140-%' "
                        "OR subject LIKE 'zz1140%' OR dedupe_key LIKE '%zz1140%'")
        await p.execute("DELETE FROM acp_shared.ops_sample WHERE subject LIKE 'aa-%-zz1140-%' "
                        "OR job_id = '00000000-0000-0000-0000-000000001140'::uuid")
        await p.close()


def _now() -> datetime:
    t = datetime.now(timezone.utc)
    return t.replace(minute=t.minute - t.minute % 5, second=0, microsecond=0)


def _ecs(rollout: str, at: datetime) -> MagicMock:
    client = MagicMock()
    services = [{"serviceName": f"aa-cis-{ENV}-api", "desiredCount": 1, "runningCount": 1, "pendingCount": 0,
                 "taskDefinition": "arn:aws:ecs:zz:task-definition/aa-cis-zz1140-api:7",
                 "deployments": [{"id": "ecs-svc/zz1140-1", "status": "PRIMARY", "rolloutState": rollout,
                                  "taskDefinition": "td:7", "createdAt": at - timedelta(minutes=3),
                                  "updatedAt": at - timedelta(minutes=1)}]},
                {"serviceName": f"aa-cis-{ENV}-worker", "desiredCount": 1, "runningCount": 0, "pendingCount": 1,
                 "taskDefinition": "arn:aws:ecs:zz:task-definition/aa-cis-zz1140-worker:3", "deployments": []}]
    client.describe_services.return_value = {"services": services, "failures": []}
    client.describe_task_definition.return_value = {"taskDefinition": {"containerDefinitions": [
        {"name": "api", "image": "registry.invalid/aa-cis-zz1140-api:dev-zz1140"}]}}
    return client


def _cw(firing: bool, at: datetime) -> MagicMock:
    client = MagicMock()
    alarms = [{"AlarmName": f"aa-crm-{ENV}-api-p95", "AlarmArn": "arn:zz", "StateValue": "ALARM" if firing else "OK",
               "StateUpdatedTimestamp": at - timedelta(minutes=12), "Namespace": "AWS/ApplicationELB",
               "AlarmDescription": "zz1140 p95 over the limit"}]
    client.describe_alarms.return_value = {"MetricAlarms": alarms, "CompositeAlarms": []}
    return client


@pytest.fixture
def no_peers(monkeypatch):
    from api.services import platform_apps

    async def card(app_id, client=None, *, base=None, paths=None):
        return {"id": app_id, "status": "ok" if app_id != "crm" else "down",
                "probe": {"reason": "zz1140 probe"}, "git_sha": "zz1140", "ready": {"schema_version": "1"}}

    monkeypatch.setattr(platform_apps, "http_health_card", card)


@pytest.mark.asyncio
async def test_tick_writes_samples_and_events_and_closes_them(pool, no_peers):
    at = _now()
    job_id = "00000000-0000-0000-0000-000000001140"
    out = await sampler.tick(pool, at, job_id, env=ENV, ecs_client=_ecs("IN_PROGRESS", at), cw_client=_cw(True, at))
    assert not [k for k in out if k.endswith("_error")], out
    rows = await pool.fetch("SELECT kind, subject, state FROM acp_shared.ops_sample WHERE job_id = $1::uuid "
                            "ORDER BY kind, subject", job_id)
    got = {(r["kind"], r["subject"]): r["state"] for r in rows}
    assert got[("ecs_service", f"aa-cis-{ENV}-api")] == "warn"
    assert got[("ecs_service", f"aa-cis-{ENV}-worker")] == "down"
    assert got[("ecs_service", f"aa-crm-{ENV}-web")] == "unknown"
    assert got[("app_health", "crm")] == "down" and got[("app_health", "cis")] == "ok"
    assert ("queue", "shared.job") in got and ("database", "rds") in got and ("alarms", "all") in got
    ev = await pool.fetch("SELECT kind, severity, ended_at FROM acp_shared.ops_event WHERE dedupe_key LIKE "
                          "'%zz1140%' ORDER BY kind")
    kinds = {r["kind"]: r for r in ev}
    assert set(kinds) == {"alarm", "deploy", "service_down"}
    assert kinds["alarm"]["ended_at"] is None and kinds["deploy"]["ended_at"] is None
    assert kinds["service_down"]["severity"] == "sev1"

    # a re-run of the same bucket writes nothing twice
    before = await pool.fetchval("SELECT count(*) FROM acp_shared.ops_sample WHERE job_id = $1::uuid", job_id)
    await sampler.tick(pool, at, job_id, env=ENV, ecs_client=_ecs("IN_PROGRESS", at), cw_client=_cw(True, at))
    assert await pool.fetchval("SELECT count(*) FROM acp_shared.ops_sample WHERE job_id = $1::uuid",
                               job_id) == before
    assert await pool.fetchval("SELECT count(*) FROM acp_shared.ops_event WHERE dedupe_key LIKE '%zz1140%'") == 3

    # the next tick: rollout completed, alarm OK, the worker still down
    nxt = at + timedelta(minutes=5)
    await sampler.tick(pool, nxt, job_id, env=ENV, ecs_client=_ecs("COMPLETED", nxt), cw_client=_cw(False, nxt))
    ev = {r["kind"]: r for r in await pool.fetch(
        "SELECT kind, ended_at FROM acp_shared.ops_event WHERE dedupe_key LIKE '%zz1140%'")}
    assert ev["alarm"]["ended_at"] is not None and ev["deploy"]["ended_at"] is not None
    assert ev["service_down"]["ended_at"] is None


@pytest.mark.asyncio
async def test_build_status_reads_the_samples_back(pool, no_peers, monkeypatch):
    at = _now()
    job_id = "00000000-0000-0000-0000-000000001140"
    await sampler.tick(pool, at - timedelta(minutes=5), job_id, env=ENV,
                       ecs_client=_ecs("COMPLETED", at), cw_client=_cw(True, at))
    from api.services import delivery_metrics, platform_apps

    monkeypatch.setattr(platform_apps, "collect_apps", AsyncMock(return_value={"apps": [
        {"id": "cis", "overall": {"status": "ok"}, "api": {"git_sha": "zz1140"}, "workers": [],
         "schema": {"status": "ok"}}]}))
    monkeypatch.setattr(delivery_metrics, "load", AsyncMock(side_effect=FileNotFoundError("no summary")))
    redis = None   # signals need Redis: without it the section says cache_offline, the rest still answers
    out = await ops_status.build_status(pool, redis, range_key="1h", compare=True)
    s = out["sections"]
    assert out["range"]["key"] == "1h" and out["previous"] is not None
    assert s["signals"]["status"] == "unavailable" and s["signals"]["reason"] == "cache_offline"
    assert s["runtimes"]["status"] == "ok"
    rows = {r["subject"]: r for r in s["runtimes"]["data"]["rows"]}
    assert rows[f"aa-cis-{ENV}-worker"]["state"] == "down" and rows[f"aa-cis-{ENV}-worker"]["cells"]
    assert any(r["state"] == sections.NOT_MEASURED for r in s["runtimes"]["data"]["rows"])
    assert s["jobs"]["status"] == "ok"
    queues = {q["id"]: q for q in s["jobs"]["data"]["queues"]}
    assert queues["booking"]["status"] == sections.NOT_MEASURED and queues["crm"]["status"] == "unavailable"
    assert s["ai"]["status"] == "ok" and s["cost"]["status"] == "ok" and s["delivery"]["status"] == "ok"
    texts = [e["text"] for e in s["events"]["data"]]
    assert any("Alarm: aa-crm-zz1140-api-p95" == t for t in texts)
    assert any(t.startswith("Deploy aa-cis-zz1140-api") for t in texts)
    assert s["signals"]["alarm_bands"] and s["signals"]["deploys"]
    v = out["verdict"]
    assert v["status"] == "attention" and v["chips"]["alarms_firing"] >= 1
    assert {i["severity"] for i in v["attention"]} >= {"sev1"}
    assert {src["section"] for src in out["sources"]} >= {"signals", "runtimes", "jobs", "events"}
    assert out["thresholds"]["error_rate_pct"]["warn"] == 1.0
