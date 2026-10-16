"""AA-832 — the SQL of `ops_rds_role_connections` and its schedule against a real Postgres.

ROLE_CONNECTIONS_SQL: a LOGIN role with a CONNECTION LIMIT and one open session is counted 1, a role
without a limit or without LOGIN is not listed. schedule(): enqueues once, then `already_pending`
until the job ends; the deploy gate does not count the kind. CloudWatch is a recording stub.
"""
import os
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared.jobs import queue  # noqa: E402
from shared.jobs.registry import JobContext  # noqa: E402
from services.jobs import ops_rds_role_connections_job as mod  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=4)
    await p.execute("DELETE FROM shared.job WHERE kind = $1", mod.KIND)
    yield p
    await p.execute("DELETE FROM shared.job WHERE kind = $1", mod.KIND)
    await p.close()


@pytest_asyncio.fixture
async def roles(pool):
    """Three cluster-wide roles with a unique suffix: limited LOGIN, unlimited LOGIN, limited NOLOGIN.
    CI's cistest is SUPERUSER (postgres service); a local DB without CREATEROLE skips."""
    if not await pool.fetchval("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user"):
        pytest.skip("test DB user cannot CREATE ROLE (CI runs it as SUPERUSER)")
    sfx = uuid.uuid4().hex[:8]
    names = {"limited": f"aa832_lim_{sfx}", "unlimited": f"aa832_unl_{sfx}", "nologin": f"aa832_nol_{sfx}"}
    await pool.execute(f"CREATE ROLE {names['limited']} LOGIN PASSWORD 'aa832' CONNECTION LIMIT 4")
    await pool.execute(f"CREATE ROLE {names['unlimited']} LOGIN PASSWORD 'aa832'")
    await pool.execute(f"CREATE ROLE {names['nologin']} NOLOGIN CONNECTION LIMIT 4")
    yield names
    for n in names.values():
        await pool.execute(f"DROP ROLE IF EXISTS {n}")


class StubCloudWatch:
    def __init__(self):
        self.calls = []

    def put_metric_data(self, **kw):
        self.calls.append(kw)


@pytest.mark.asyncio
async def test_sql_counts_sessions_of_limited_login_roles(pool, roles):
    rows = {r["rolname"]: r for r in await pool.fetch(mod.ROLE_CONNECTIONS_SQL)}
    assert roles["unlimited"] not in rows and roles["nologin"] not in rows
    assert rows[roles["limited"]]["rolconnlimit"] == 4 and rows[roles["limited"]]["connections"] == 0

    conn = await asyncpg.connect(host=DB_HOST, port=DB_PORT, database=DB_NAME,
                                 user=roles["limited"], password="aa832")
    try:
        rows = {r["rolname"]: r for r in await pool.fetch(mod.ROLE_CONNECTIONS_SQL)}
        assert rows[roles["limited"]]["connections"] == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_handler_publishes_the_limited_role(pool, roles):
    job_id, _ = await queue.enqueue(pool, mod.KIND, {"trigger": "schedule"})
    row = await pool.fetchrow("SELECT * FROM shared.job WHERE id = $1::uuid", job_id)
    cw = StubCloudWatch()
    out = await mod.run(JobContext(pool, queue._to_job(row), {"cloudwatch": cw}))
    assert out["roles"][roles["limited"]] == {"connections": 0, "limit": 4, "pct": 0.0}
    dims = {d["Dimensions"][0]["Value"] for d in cw.calls[0]["MetricData"]}
    assert roles["limited"] in dims and roles["unlimited"] not in dims


@pytest.mark.asyncio
async def test_schedule_once_while_pending_and_gate_exempt(pool):
    first = await mod.schedule(pool)
    assert first["enqueued"] is True
    second = await mod.schedule(pool)
    assert second == {"enqueued": False, "reason": "already_pending", "job_id": first["job_id"]}
    assert await pool.fetchval("SELECT count(*) FROM shared.job WHERE kind = $1", mod.KIND) == 1

    exempt = await queue.active_count(pool, exempt_kinds=[mod.KIND])
    counted = await queue.active_count(pool)
    assert counted["active"] - exempt["active"] == 1

    await pool.execute("UPDATE shared.job SET status = 'succeeded', finished_at = now() WHERE id = $1::uuid",
                       first["job_id"])
    third = await mod.schedule(pool)
    assert third["enqueued"] is True and third["job_id"] != first["job_id"]
