"""AA-839 — the data_erasure job against real Postgres (every migration replayed by conftest).

Synthetic data only: each test makes its own tenants (random ids) and its own traveller hash. Covers:
- the inventory is complete for the replayed schema (no tenant_id table without an entry) and every SQL the
  planner generates runs (counts on a tenant with no rows);
- a tenant DRY RUN counts the seeded rows, writes nothing and leaves another tenant alone;
- the apply is refused with the flag off, before the retention window, on a stale fingerprint or a blocker, and
  then runs in one transaction: deletes children-first (the quarter_plan <-> version cycle included), detaches
  the cost log, merges the daily rollup into the platform row, keeps the audit chain, writes the erasure audit
  row, and a DRY RUN afterwards finds 0;
- a traveller DRY RUN finds the address in a notification and a job payload by hash, the apply deletes /
  redacts them and strips the hash from both job rows.
"""
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from services.erasure import inventory as inv  # noqa: E402
from services.erasure import plan as pl  # noqa: E402
from services.jobs import erasure_job as ej  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.jobs.registry import JobContext, NonRetryable  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
ON = {ej.APPLY_FLAG: "true"}
# Registry tables the replayed test database may lack (migrations 001 / 020 and the acp_deliver page table are
# skipped or fail there). Missing is fine (nothing to erase); unregistered is not.
CI_ABSENT = {"gold_aa_internal.tenant_tour_versions", "acp_shared.acp_output_rules", "acp_deliver.tenant_tour_pages"}


class FakeS3:
    def list_objects_v2(self, **kw):
        return {"KeyCount": 2 if kw["Prefix"].startswith("brand-identity/") else 0, "IsTruncated": False}


class FakeSM:
    def describe_secret(self, SecretId):
        if SecretId.startswith("acp/cms/"):
            return {"Name": SecretId}
        err = Exception("not found")
        err.response = {"Error": {"Code": "ResourceNotFoundException"}}
        raise err


AWS = {"erasure_aws": {"s3": FakeS3(), "secretsmanager": FakeSM(), "bucket": "aa-cis-bronze-test"}}


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    yield p
    await p.execute("DELETE FROM shared.job WHERE kind = 'data_erasure'")
    await p.close()


async def _run(pool, payload, env=None, now=None, resources=None):
    """Runs the handler as the worker would (a real shared.job row), then marks the job like the worker."""
    job_id, _ = await queue.enqueue(pool, ej.KIND, payload)
    row = await pool.fetchrow("SELECT * FROM shared.job WHERE id = $1::uuid", job_id)
    ctx = JobContext(pool, queue._to_job(row), resources=resources if resources is not None else AWS)
    try:
        result = await ej.run_erasure(ctx, env=env or {}, now=now)
    except Exception:
        await pool.execute("UPDATE shared.job SET status = 'failed', finished_at = now() WHERE id = $1::uuid",
                           job_id)
        raise
    await pool.execute("UPDATE shared.job SET status = 'succeeded', finished_at = now(), result = $2::jsonb "
                       "WHERE id = $1::uuid", job_id, json.dumps(result, default=str))
    return job_id, result


async def _tenant(pool, offboarded_days=None) -> str:
    tid = str(uuid.uuid4())
    await pool.execute("""
        INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active, posts_per_week,
                                    cancelled_at)
        VALUES ($1::uuid, 'AA-839 synthetic', $2, 'starter', 60, $3, 3,
                CASE WHEN $4::int IS NULL THEN NULL ELSE now() - make_interval(days => $4::int) END)""",
                       tid, f"aa839-{tid[:8]}", offboarded_days is None, offboarded_days)
    return tid


async def _seed(pool, tid: str, stage: str) -> dict:
    """One row (or a few) per kind of table: content/config (delete), FK cycle (quarter plan), cost log (detach),
    daily rollup (merge), API usage, finished job, audit row (keep)."""
    await pool.execute("INSERT INTO acp_shared.tenant_config (tenant_id, markets, channels) "
                       "VALUES ($1::uuid, ARRAY['VN'], ARRAY['blog'])", tid)
    plan_id, version_id = str(uuid.uuid4()), str(uuid.uuid4())
    await pool.execute("INSERT INTO acp_shared.quarter_plan (plan_id, tenant_id, year, quarter) "
                       "VALUES ($1::uuid, $2::uuid, 2026, 4)", plan_id, tid)
    await pool.execute("""INSERT INTO acp_shared.quarter_plan_version
                          (version_id, plan_id, version_no, payload, source, approval_status)
                          VALUES ($1::uuid, $2::uuid, 1, '{}'::jsonb, 'standard', 'pending')""", version_id, plan_id)
    await pool.execute("UPDATE acp_shared.quarter_plan SET current_version_id = $2::uuid WHERE plan_id = $1::uuid",
                       plan_id, version_id)
    await pool.execute("""INSERT INTO shared.notifications (tenant_id, actor_type, event_type, payload, target_roles)
                          VALUES ($1::uuid, 'system', 'aa839.test', '{"n": 1}'::jsonb, ARRAY['admin'])""", tid)
    await pool.execute("INSERT INTO shared.unmapped_market_requests (tenant_id, country_code) "
                       "VALUES ($1::uuid, 'ZZ')", tid)
    await pool.execute("""INSERT INTO shared.tenant_api_usage (id, tenant_id, endpoint, method, status_code, called_at,
                                                               actor_type)
                          VALUES (gen_random_uuid(), $1::uuid, '/v1/x', 'GET', 200, now(), 'tenant')""", tid)
    piece = str(uuid.uuid4())
    await pool.execute("""
        INSERT INTO shared.llm_call_log (tenant_id, stage, role, model, tokens_in, tokens_out, cost_usd, quality_signal,
                                         content_piece_id, created_at)
        VALUES ($1::uuid, $2, 'writer', 'm1', 10, 1, 0.5, '{}'::jsonb, $3::uuid, now()),
               ($1::uuid, $2, 'writer', 'm1', 10, 1, 0.25, '{}'::jsonb, NULL, now())""", tid, stage, piece)
    for tenant, calls in ((tid, 3), (None, 5)):     # the platform (NULL) row of the same key exists already
        await pool.execute("""
            INSERT INTO shared.llm_call_daily (day, app, tenant_id, stage, role, model, provider, account, is_shadow,
                                               calls, fallback_calls, tokens_in, tokens_out, cost_usd, updated_at)
            VALUES (DATE '2026-01-01', 'cis', $1::uuid, $2, 'writer', 'm1', 'bedrock', 'acc3', false,
                    $3, 0, 100, 10, 1.5, now())""", tenant, stage, calls)
    job_id, _ = await queue.enqueue(pool, "t9_write", {"tenant_id": tid, "piece_id": piece})
    await pool.execute("UPDATE shared.job SET status = 'succeeded', finished_at = now() WHERE id = $1::uuid", job_id)
    await pool.execute("""INSERT INTO acp_shared.audit_log (id, tenant_id, actor, action, resource_type, resource_id,
                                                           details, created_at)
                          VALUES (gen_random_uuid(), $1::text, 'admin:t', 'agency.offboard', 'tenant', $1::text,
                                  '{}'::jsonb,
                                  now())""", tid)
    return {"plan_id": plan_id, "version_id": version_id, "job_id": job_id}


def _rows(result) -> dict:
    return {(r["table"], r["action"]): r["rows"] for r in result["rows"]}


# ── inventory completeness + every generated SQL runs ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_inventory_covers_every_tenant_table_and_the_plan_sql_runs(pool):
    async with pool.acquire() as conn:
        catalog = await pl.load_catalog(conn)
        plan = pl.plan_tenant(inv.Registry(), catalog)
        assert plan.unregistered == [], (
            f"CIS tables with tenant_id and no entry in services/erasure/inventory.py: {plan.unregistered}")
        assert set(plan.missing) <= CI_ABSENT, plan.missing
        assert not [c for c in plan.checks if c.kind in ("unsupported_cycle", "too_deep")]
        assert plan.order[-1] == "shared.tenants"
        assert "acp_shared.quarter_plan_version" in plan.nodes          # reached through plan_id
        out = await pl.count_tenant(conn, plan, str(uuid.uuid4()))    # every count / check SQL executes
    assert all(r["rows"] == 0 for r in out["rows"])
    assert not [b for b in out["blockers"] if b["rows"] is None], out["blockers"]


# ── tenant ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_dry_run_counts_and_writes_nothing(pool):
    tid, other = await _tenant(pool, offboarded_days=40), await _tenant(pool)
    stage = f"adhoc_aa839_{uuid.uuid4().hex[:8]}"
    await _seed(pool, tid, stage)
    await _seed(pool, other, stage + "_o")
    _, res = await _run(pool, {"subject": "tenant", "tenant": tid})
    rows = _rows(res)
    assert res["mode"] == "dry_run" and not res["blocked"], res["blockers"]
    assert rows[("shared.tenants", "delete")] == 1
    assert rows[("acp_shared.tenant_config", "delete")] == 1
    assert rows[("acp_shared.quarter_plan", "delete")] == 1
    assert rows[("acp_shared.quarter_plan_version", "cascade")] == 1
    assert rows[("acp_shared.quarter_plan", "null_first:current_version_id")] == 1
    assert rows[("shared.llm_call_log", "detach")] == 2
    assert rows[("shared.llm_call_daily", "merge")] == 1
    assert rows[("shared.job", "delete")] == 1
    assert rows[("acp_shared.audit_log", "keep")] == 1
    assert res["tenant_state"] == "offboarded" and res["unregistered"] == []
    assert res["s3"][1] == {"prefix": f"bronze:brand-identity/{tid}/", "objects": 2}
    assert {s["name"]: s["exists"] for s in res["secrets"]} == {f"acp/cms/{tid}": True, f"acp/social/{tid}": False}
    assert any(i["owner"] == "AA-Partner (crm)" for i in res["checklist"])
    # nothing written
    assert await pool.fetchval("SELECT count(*) FROM shared.tenants WHERE tenant_id = $1::uuid", tid) == 1
    assert await pool.fetchval("SELECT count(*) FROM shared.llm_call_log WHERE tenant_id = $1::uuid", tid) == 2


@pytest.mark.asyncio
async def test_tenant_apply_refusals(pool):
    tid = await _tenant(pool, offboarded_days=3)
    await _seed(pool, tid, f"adhoc_aa839_{uuid.uuid4().hex[:8]}")
    dry_id, dry = await _run(pool, {"subject": "tenant", "tenant": tid})
    apply = {"subject": "tenant", "tenant": tid, "mode": "apply", "dry_run_job_id": dry_id,
             "approved_fingerprint": dry["fingerprint"], "approved_by": "Nghiep", "snapshot_id": "dsar-test-pre"}
    with pytest.raises(NonRetryable, match=ej.APPLY_FLAG):
        await _run(pool, apply)                                   # flag off (the default everywhere)
    with pytest.raises(NonRetryable, match="less than 30 days"):
        await _run(pool, apply, env=ON)                           # offboarded 3 days ago
    env = dict(ON, ERASURE_TENANT_RETENTION_DAYS="0")
    await pool.execute("INSERT INTO shared.unmapped_market_requests (tenant_id, country_code) "
                       "VALUES ($1::uuid, 'ZY')", tid)            # counts change after the approval
    with pytest.raises(NonRetryable, match="counts changed"):
        await _run(pool, apply, env=env)
    assert await pool.fetchval("SELECT count(*) FROM shared.tenants WHERE tenant_id = $1::uuid", tid) == 1


@pytest.mark.asyncio
async def test_tenant_apply_erases_children_first_and_leaves_other_tenants(pool):
    tid, other = await _tenant(pool, offboarded_days=40), await _tenant(pool)
    stage = f"adhoc_aa839_{uuid.uuid4().hex[:8]}"
    seeded = await _seed(pool, tid, stage)
    await _seed(pool, other, stage + "_o")
    dry_id, dry = await _run(pool, {"subject": "tenant", "tenant": tid})
    _, res = await _run(pool, {"subject": "tenant", "tenant": tid, "mode": "apply", "dry_run_job_id": dry_id,
                               "approved_fingerprint": dry["fingerprint"], "approved_by": "Nghiep",
                               "snapshot_id": "dsar-test-pre", "actor": "admin:test"}, env=ON)
    assert res["mode"] == "apply" and res["verified"] is True, res.get("verified_left")
    assert await pool.fetchval("SELECT count(*) FROM shared.tenants WHERE tenant_id = $1::uuid", tid) == 0
    assert await pool.fetchval("SELECT count(*) FROM acp_shared.quarter_plan_version WHERE version_id = $1::uuid",
                               seeded["version_id"]) == 0
    assert await pool.fetchval("SELECT count(*) FROM shared.job WHERE id = $1::uuid", seeded["job_id"]) == 0
    # cost history kept without the tenant link
    log = await pool.fetch("SELECT tenant_id, content_piece_id FROM shared.llm_call_log WHERE stage = $1", stage)
    assert len(log) == 2 and all(r["tenant_id"] is None and r["content_piece_id"] is None for r in log)
    daily = await pool.fetch("SELECT tenant_id, calls FROM shared.llm_call_daily WHERE stage = $1", stage)
    assert [(r["tenant_id"], r["calls"]) for r in daily] == [(None, 8)]
    # audit chain kept + one erasure row, counts only
    actions = [r["action"] for r in await pool.fetch(
        "SELECT action FROM acp_shared.audit_log WHERE tenant_id = $1::text ORDER BY created_at", tid)]
    assert actions == ["agency.offboard", "tenant.erased"]
    details = await pool.fetchval("SELECT details FROM acp_shared.audit_log WHERE tenant_id = $1::text "
                                  "AND action = 'tenant.erased'", tid)
    details = json.loads(details) if isinstance(details, str) else details
    assert details["dry_run_job_id"] == dry_id and details["snapshot_id"] == "dsar-test-pre"
    # the other tenant is untouched
    assert await pool.fetchval("SELECT count(*) FROM acp_shared.quarter_plan WHERE tenant_id = $1::uuid", other) == 1
    assert await pool.fetchval("SELECT count(*) FROM shared.llm_call_log WHERE tenant_id = $1::uuid", other) == 2
    # independent verification: a new DRY RUN finds nothing but the kept audit rows
    _, again = await _run(pool, {"subject": "tenant", "tenant": tid})
    left = {k: v for k, v in _rows(again).items() if v}
    assert left == {("acp_shared.audit_log", "keep"): 2}, left
    assert again["tenant_state"] is None


@pytest.mark.asyncio
async def test_a_row_of_another_tenant_pointing_at_erased_rows_blocks(pool):
    tid, other = await _tenant(pool, offboarded_days=40), await _tenant(pool)
    seeded = await _seed(pool, tid, f"adhoc_aa839_{uuid.uuid4().hex[:8]}")
    await pool.execute("INSERT INTO acp_shared.quarter_plan (plan_id, tenant_id, year, quarter, current_version_id) "
                       "VALUES (gen_random_uuid(), $1::uuid, 2026, 3, $2::uuid)", other, seeded["version_id"])
    q_id, _ = await queue.enqueue(pool, "t9_write", {"tenant_id": tid})          # still queued
    dry_id, dry = await _run(pool, {"subject": "tenant", "tenant": tid})
    kinds = {(b["kind"], b["table"]) for b in dry["blockers"]}
    assert ("cross_tenant", "acp_shared.quarter_plan") in kinds and ("live_jobs", "shared.job") in kinds
    with pytest.raises(NonRetryable, match="blockers"):
        await _run(pool, {"subject": "tenant", "tenant": tid, "mode": "apply", "dry_run_job_id": dry_id,
                          "approved_fingerprint": dry["fingerprint"], "approved_by": "N", "snapshot_id": "s"}, env=ON)
    await pool.execute("DELETE FROM shared.job WHERE id = $1::uuid", q_id)


# ── traveller ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_traveller_dry_run_then_apply(pool):
    address = f"Traveller.{uuid.uuid4().hex[:8]}@Example.com"
    h = hashlib.sha256(address.strip().lower().encode()).hexdigest()
    tid = await _tenant(pool)
    notif = await pool.fetchval(
        """INSERT INTO shared.notifications (tenant_id, actor_type, event_type, payload, target_roles)
           VALUES ($1::uuid, 'system', 'aa839.test', jsonb_build_object('from', 'Name <' || $2 || '>'),
                   ARRAY['admin']) RETURNING id""", tid, address)
    bad_job, _ = await queue.enqueue(pool, "t9_write", {"tenant_id": tid, "note": f"reply to {address.lower()}"})
    await pool.execute("UPDATE shared.job SET status = 'failed', finished_at = now() WHERE id = $1::uuid", bad_job)
    other = await pool.fetchval(
        """INSERT INTO shared.notifications (tenant_id, actor_type, event_type, payload, target_roles)
           VALUES ($1::uuid, 'system', 'aa839.test', '{"from": "someone.else@example.com"}'::jsonb, ARRAY['admin'])
           RETURNING id""", tid)

    dry_id, dry = await _run(pool, {"subject": "traveller", "email_sha256": h})
    rows = {r["table"]: r for r in dry["rows"]}
    assert rows["shared.notifications:payload"]["ids"] == [str(notif)]
    assert rows["shared.job:payload"]["ids"] == [bad_job] and rows["shared.job:payload"]["action"] == "redact"
    assert rows["shared.admin_users:email"]["rows"] == 0 and not dry["blocked"]
    assert address.lower() not in json.dumps(dry) and address not in json.dumps(dry)
    assert any(i["owner"] == "AA-Booking" for i in dry["checklist"])

    _, res = await _run(pool, {"subject": "traveller", "email_sha256": h, "mode": "apply", "dry_run_job_id": dry_id,
                               "approved_fingerprint": dry["fingerprint"], "approved_by": "Nghiep",
                               "snapshot_id": "dsar-test-pre"}, env=ON)
    assert res["verified"] is True
    assert await pool.fetchval("SELECT count(*) FROM shared.notifications WHERE id = $1", notif) == 0
    assert await pool.fetchval("SELECT count(*) FROM shared.notifications WHERE id = $1", other) == 1
    payload = await pool.fetchval("SELECT payload FROM shared.job WHERE id = $1::uuid", bad_job)
    assert (json.loads(payload) if isinstance(payload, str) else payload) == {"redacted": "AA-839"}
    stripped = await pool.fetchval("SELECT payload ->> 'email_sha256' FROM shared.job WHERE id = $1::uuid", dry_id)
    assert stripped == ej.STRIPPED
    audit = await pool.fetchval("SELECT details::text FROM acp_shared.audit_log WHERE action = 'dsar.cis_erased' "
                                "AND details ->> 'dry_run_job_id' = $1", dry_id)
    assert audit and h not in audit
    # a new DRY RUN with the same hash finds nothing
    _, again = await _run(pool, {"subject": "traveller", "email_sha256": h})
    assert all(r["rows"] == 0 for r in again["rows"])


@pytest.mark.asyncio
async def test_now_is_checked_against_the_dry_run_age(pool):
    tid = await _tenant(pool, offboarded_days=40)
    dry_id, dry = await _run(pool, {"subject": "tenant", "tenant": tid})
    later = datetime.now(timezone.utc) + timedelta(hours=30)
    with pytest.raises(NonRetryable, match="older than"):
        await _run(pool, {"subject": "tenant", "tenant": tid, "mode": "apply", "dry_run_job_id": dry_id,
                          "approved_fingerprint": dry["fingerprint"], "approved_by": "N", "snapshot_id": "s"},
                   env=ON, now=later)
