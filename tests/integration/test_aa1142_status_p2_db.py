"""AA-1142 (S292) — the P2 reads and writes of the Ecosystem status page on a real Postgres (conftest replays every
migration, CIS 484 and 485 included): an incident is declared with its update and audit rows, refused a second time for
the same alert, updated (status, SEV, owner, note), resolved and reopened, and read back with its timeline (its own
trail, the ops events of its app, the acks of its item); the sampler's copy of an external check run is stored once
and drawn as a strip; the per-agency drill-down reads the hourly usage rollup and failed jobs per agency; the CRM's
availability is counted from its health samples. Invented names only; rows are removed by the test's own keys."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services.ops_status import external, incidents, slo, tenants  # noqa: E402
from api.services.ops_status.ranges import TimeRange  # noqa: E402
from shared import ops_status_thresholds as th  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ITEM = "errors-zz1142"
ACTOR_A = "admin:00000000-0000-4000-8000-0000000a1142"
ACTOR_B = "admin:00000000-0000-4000-8000-0000000b1142"
AGENCY = "00000000-0000-4000-8000-0000000c1142"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    try:
        yield p
    finally:
        await p.execute("DELETE FROM acp_shared.ops_incident_update WHERE incident_id IN (SELECT incident_id FROM "
                        "acp_shared.ops_incident WHERE title LIKE 'zz1142%')")
        await p.execute("DELETE FROM acp_shared.ops_incident WHERE title LIKE 'zz1142%'")
        await p.execute("DELETE FROM acp_shared.ops_event WHERE dedupe_key LIKE 'zz1142%'")
        await p.execute("DELETE FROM acp_shared.ops_external_check WHERE target LIKE 'zz1142%'")
        await p.execute("DELETE FROM acp_shared.ops_sample WHERE subject = 'zz1142-crm'")
        await p.execute("DELETE FROM shared.ai_usage_hourly WHERE tenant_id = $1::uuid", AGENCY)
        await p.execute("DELETE FROM shared.job WHERE kind = 'zz1142_kind'")
        await p.close()


@pytest.mark.asyncio
async def test_incident_declare_update_resolve_reopen_with_timeline(pool):
    now = datetime.now(timezone.utc)
    await pool.execute(
        "INSERT INTO acp_shared.ops_event (occurred_at, kind, severity, app, subject, title, details, dedupe_key) "
        "VALUES ($1, 'deploy', 'info', 'cis', 'zz-api', 'zz1142 deploy', '{}'::jsonb, 'zz1142-deploy')",
        now - timedelta(minutes=20))
    inc = await incidents.declare(pool, title="zz1142 invented outage", severity="sev2", app="cis", item_id=ITEM,
                                  summary=None, actor=ACTOR_A, now=now)
    assert inc["status"] == "investigating" and inc["owner"] == ACTOR_A
    with pytest.raises(incidents.IncidentError) as e:
        await incidents.declare(pool, title="zz1142 second", severity="sev3", app="cis", item_id=ITEM, summary=None,
                                actor=ACTOR_B)
    assert e.value.status == 409 and e.value.incident_id == inc["incident_id"]
    later = now + timedelta(minutes=5)
    up = await incidents.update(pool, inc["incident_id"], status="identified", severity="sev1", take_over=True,
                                note="zz1142 the invented cause", actor=ACTOR_B, now=later)
    assert (up["status"], up["severity"], up["owner"]) == ("identified", "sev1", ACTOR_B)
    with pytest.raises(incidents.IncidentError) as e:
        await incidents.update(pool, inc["incident_id"], status=None, severity=None, take_over=False, note=None,
                               actor=ACTOR_B)
    assert e.value.code == "nothing_to_change"
    done = await incidents.update(pool, inc["incident_id"], status="resolved", severity=None, take_over=False,
                                  note=None, actor=ACTOR_B, now=later + timedelta(minutes=5))
    assert done["resolved_at"] is not None
    d = await incidents.detail(pool, inc["incident_id"], now=later + timedelta(minutes=10))
    kinds = [(r["kind"], r["type"]) for r in d["timeline"]]
    assert ("event", "deploy") in kinds and ("update", "declared") in kinds and ("update", "note") in kinds
    assert [t for k, t in kinds if k == "update"] == ["declared", "status", "severity", "owner", "note", "status"]
    assert d["lesson_draft"].startswith("<principle> — zz1142 invented outage (SEV1, cis)")
    audit = await pool.fetch("SELECT action FROM acp_shared.audit_log WHERE resource_type = 'ops_incident' AND "
                             "resource_id = $1", str(inc["incident_id"]))
    assert {a["action"] for a in audit} == {"ops_incident_declared", "ops_incident_updated"}
    # a resolved incident frees the alert; reopening it while a new one is open is refused
    again = await incidents.declare(pool, title="zz1142 relapse", severity="sev2", app="cis", item_id=ITEM,
                                    summary="zz1142 again", actor=ACTOR_A)
    with pytest.raises(incidents.IncidentError) as e:
        await incidents.update(pool, inc["incident_id"], status="monitoring", severity=None, take_over=False,
                               note=None, actor=ACTOR_A)
    assert e.value.status == 409
    listed = await incidents.list_incidents(pool, open_only=True)
    assert again["incident_id"] in {i["incident_id"] for i in listed["incidents"]}
    assert inc["incident_id"] not in {i["incident_id"] for i in listed["incidents"]}


@pytest.mark.asyncio
async def test_external_ingest_once_and_strip(pool):
    at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10)
    doc = {"format": 1, "checked_at": at.isoformat(), "results": [
        {"target": "zz1142-api", "app": "cis", "host": "api.zz1142.example", "mode": "health", "dns_ok": True,
         "tls_ok": True, "cert_not_after": (at + timedelta(days=60)).isoformat(), "http_status": 200, "http_ok": True,
         "latency_ms": 180, "error": None},
        {"target": "zz1142-web", "app": "booking", "host": "web.zz1142.example", "mode": "health", "dns_ok": False,
         "tls_ok": None, "cert_not_after": None, "http_status": None, "http_ok": None, "latency_ms": None,
         "error": "zz1142 name not found"}]}
    raw = json.dumps(doc).encode()
    for _ in range(2):   # the sampler sees the same run on two ticks: stored once
        out: dict = {}
        await external.ingest(pool, out, loader=lambda: raw)
    n = await pool.fetchval("SELECT count(*) AS n FROM acp_shared.ops_external_check WHERE target LIKE 'zz1142%'")
    assert n == 2
    now = datetime.now(timezone.utc)
    sec = await external.collect(pool, TimeRange("1h", now - timedelta(hours=1), now, 60), now)
    assert sec["status"] == "ok" and sec["data"]["stale"] is False
    rows = {r["target"]: r for r in sec["data"]["rows"]}
    assert rows["zz1142-api"]["state"] == "ok" and rows["zz1142-web"]["state"] == "down"
    assert "down" in rows["zz1142-web"]["cells"]
    ids = {i["id"] for i in external.verdict_items(sec["data"])}
    assert "external-zz1142-web" in ids


@pytest.mark.asyncio
async def test_tenant_drill_down_reads_the_rollup_and_failed_jobs(pool):
    now = datetime.now(timezone.utc)
    hour = now.replace(minute=0, second=0, microsecond=0)
    for status, req, cost in (("ok", 9, 0.30), ("error", 1, 0.0)):
        await pool.execute(
            "INSERT INTO shared.ai_usage_hourly (hour, source, app, provider, tenant_id, status, error_class, traffic, "
            "requests, ledger_rows, fallback_requests, tokens_in, tokens_out, cache_read_tokens, cache_write_tokens, "
            "reasoning_tokens, cost_usd, latency_ms_sum, latency_ms_count, latency_hist, refreshed_at, stage) "
            "VALUES ($1, 'llm', 'cis', 'anthropic', $2::uuid, $3, $4, 'live', $5, $5, 0, 0, 0, 0, 0, 0, $6, 0, 0, "
            "array_fill(0, ARRAY[20]), now(), 'zz1142_stage')",
            hour, AGENCY, status, None if status == "ok" else "zz1142_error", req, cost)
    await pool.execute(
        "INSERT INTO shared.job (kind, payload, status, finished_at) VALUES ('zz1142_kind', $1::jsonb, 'failed', $2)",
        json.dumps({"tenant_id": AGENCY}), now - timedelta(minutes=5))
    rng = TimeRange("1h", now - timedelta(hours=1), now, 60)
    out = await tenants.collect(pool, rng, None, now)
    spend = {r["tenant_id"]: r for r in out["ai_spend"]}
    assert spend[AGENCY]["cost_usd"] == 0.3 and spend[AGENCY]["requests"] == 10
    assert {r["tenant_id"]: r["failed"] for r in out["ai_errors"]}[AGENCY] == 1
    assert {r["tenant_id"]: r["failed"] for r in out["failed_jobs"]}[AGENCY] == 1
    one = (await tenants.collect(pool, rng, AGENCY, now))["agency"]
    assert one["ai_failed"] == 1 and one["jobs_failed"] == 1 and one["failed_jobs"][0]["kind"] == "zz1142_kind"
    assert one["ai_errors"][0]["error_class"] == "zz1142_error" and one["known"] is False


@pytest.mark.asyncio
async def test_crm_availability_from_health_samples(pool):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    for i in range(24):
        await pool.execute(
            "INSERT INTO acp_shared.ops_sample (sampled_at, kind, subject, app, state, value) "
            "VALUES ($1, 'app_health', 'zz1142-crm', 'crm', $2, '{}'::jsonb)",
            now - timedelta(minutes=5 * i), "down" if i < 2 else "ok")
    row = await slo._health_row(pool, "zz1142-crm", "Zz CRM", slo.windows(now, th.slo()), th.slo())
    assert row["events"] == 24 and row["bad"] == 2 and row["fast"]["bad"] == 2
    assert row["fast"]["state"] == "few"   # 12 samples in the hour, under the 50 minimum
