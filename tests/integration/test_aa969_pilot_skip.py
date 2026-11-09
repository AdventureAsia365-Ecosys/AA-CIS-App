"""AA-969 part 2 (root ADR 0019 d16) against real Postgres: CIS clean-up skips the pilot tenant.

- ``retention_tenant_api_usage`` (and ``retention_llm_call_log``, same predicate) leaves the pilot tenant's raw rows in
  place while another tenant's expired rows are rolled up, archived and deleted; the rollup still counts the pilot's
  calls (the numbers stay answerable);
- the E2E identity's "synthetic tenant" check (``api.core.e2e_staff``) is false for the pilot tenant even when its
  canary flag is set.

Assertions are on the seeded rows only (the shared test DB can hold other tests' rows).
"""
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
from api.core import e2e_staff  # noqa: E402
from services.jobs import retention_job  # noqa: E402
from services.retention.archive import S3Archiver  # noqa: E402
from services.retention.policy import SPECS  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.jobs.registry import JobContext  # noqa: E402
from tests.integration.test_aa825_retention_jobs import FakeS3  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    yield p
    await p.execute("DELETE FROM shared.job WHERE kind LIKE 'retention_%'")
    await p.close()


async def _tenant(pool, *, pilot: bool, canary: bool = False) -> str:
    tid = str(uuid.uuid4())
    await pool.execute(
        """
        INSERT INTO shared.tenants (tenant_id, name, slug, plan_tier, rate_limit_rpm, is_active, posts_per_week,
                                    kind, is_canary, is_pilot, pilot_ends_at)
        VALUES ($1::uuid, 'Invented Pilot Agency', $2, 'starter', 60, true, 3, 'agency', $3, $4,
                CASE WHEN $4 THEN now() + interval '7 days' END)""",
        tid, f"aa969-{tid[:8]}", canary, pilot,
    )
    return tid


async def _drop(pool, *tids: str) -> None:
    for tid in tids:
        await pool.execute("DELETE FROM shared.tenant_api_usage_monthly WHERE tenant_id = $1::uuid", tid)
        await pool.execute("DELETE FROM shared.tenant_api_usage WHERE tenant_id = $1::uuid", tid)
        await pool.execute("DELETE FROM shared.tenants WHERE tenant_id = $1::uuid", tid)


@pytest.mark.asyncio
async def test_usage_retention_keeps_the_pilot_tenants_raw_rows(pool):
    await pool.execute("UPDATE shared.tenants SET is_pilot = false, pilot_ends_at = NULL WHERE is_pilot")
    pilot, other = await _tenant(pool, pilot=True), await _tenant(pool, pilot=False)
    try:
        old = datetime.now(timezone.utc) - timedelta(days=130)
        for tid in (pilot, other):
            await pool.execute(
                "INSERT INTO shared.tenant_api_usage (tenant_id, endpoint, method, status_code, response_ms, called_at,"
                " actor_type) VALUES ($1::uuid, '/v1/tours', 'GET', 200, 10, $2, 'tenant')",
                tid, old,
            )
        job_id, _ = await queue.enqueue(
            pool, "retention_tenant_api_usage",
            {"trigger": "manual", "dry_run": False, "max_rows": 100000, "pause_ms": 0},
        )
        row = await pool.fetchrow("SELECT * FROM shared.job WHERE id = $1::uuid", job_id)
        s3 = FakeS3()
        result = await retention_job.run_spec(
            JobContext(pool, queue._to_job(row)), SPECS["retention_tenant_api_usage"], archiver=S3Archiver(s3, job_id)
        )
        await pool.execute("UPDATE shared.job SET status = 'succeeded', finished_at = now(), result = $2::jsonb"
                           " WHERE id = $1::uuid", job_id, json.dumps(result, default=str))
        left = {
            str(r["tenant_id"]): r["n"]
            for r in await pool.fetch(
                "SELECT tenant_id, count(*) AS n FROM shared.tenant_api_usage WHERE tenant_id = ANY($1::uuid[])"
                " GROUP BY tenant_id",
                [uuid.UUID(pilot), uuid.UUID(other)],
            )
        }
        assert left == {pilot: 1}  # the pilot's row stays, the other tenant's expired row is gone
        assert other in {x["tenant_id"] for x in s3.lines()} and pilot not in {x["tenant_id"] for x in s3.lines()}
        rolled = await pool.fetchval(
            "SELECT coalesce(sum(calls), 0) AS n FROM shared.tenant_api_usage_monthly WHERE tenant_id = $1::uuid", pilot
        )
        assert rolled == 1  # the rollup still counts the pilot's calls
    finally:
        await _drop(pool, pilot, other)


@pytest.mark.asyncio
async def test_the_e2e_identity_never_treats_the_pilot_as_synthetic(pool):
    await pool.execute("UPDATE shared.tenants SET is_pilot = false, pilot_ends_at = NULL WHERE is_pilot")
    pilot = await _tenant(pool, pilot=True, canary=True)
    canary = await _tenant(pool, pilot=False, canary=True)
    try:
        async with pool.acquire() as conn:
            assert await e2e_staff.is_synthetic_tenant(conn, uuid.UUID(pilot)) is False
            assert await e2e_staff.is_synthetic_tenant(conn, uuid.UUID(canary)) is True
    finally:
        await _drop(pool, pilot, canary)
