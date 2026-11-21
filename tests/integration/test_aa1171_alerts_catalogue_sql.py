"""AA-1171 (S287 alerts catalogue P0) — the two reads of api/services/alerts_catalogue.py on a real Postgres
(conftest replays every migration, CIS 469 included): the last bell of each CIS in-app check (shared.notifications,
by check name, the last 7 days) and the confirmed recipients per topic (shared.alert_recipient.topic), which turn
into the row state and the recipient gaps. Invented addresses only; the rows are removed after the test."""
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from api.services import alerts_catalogue as cat  # noqa: E402
from shared import ops_checks  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
CLEAN = ("DELETE FROM shared.notifications WHERE event_type LIKE 'platform.ops_check.%'",
         "DELETE FROM shared.alert_recipient WHERE email LIKE 'zz-aa1171-%'")


@pytest_asyncio.fixture
async def pool():
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    for sql in CLEAN:
        await p.execute(sql)
    yield p
    for sql in CLEAN:
        await p.execute(sql)
    await p.close()


async def _bell(pool, check, ago):
    await pool.execute(
        "INSERT INTO shared.notifications (tenant_id, actor_type, event_type, entity_type, entity_id, payload, "
        "target_roles, created_at) VALUES ($1::uuid, 'system', $2, 'ops_check', $3, '{}'::jsonb, ARRAY['admin'], "
        "now() - $4::interval)", ops_checks.PLATFORM_TENANT_ID, ops_checks.EVENT_TYPE_PREFIX + check, check, ago)


async def test_last_bell_per_check_drives_the_row_state(pool):
    await _bell(pool, "jobs_starved", timedelta(hours=1))
    await _bell(pool, "jobs_starved", timedelta(hours=3))
    await _bell(pool, "rds_xid_age", timedelta(days=2))
    await _bell(pool, "route_fb.t9_blog", timedelta(minutes=10))
    await _bell(pool, "rds_role_connections", timedelta(days=9))     # older than the 7-day read
    last = await cat.last_cis_alerts(pool)
    assert set(last) == {"jobs_starved", "rds_xid_age", "route_fb.t9_blog"}
    now = datetime.now(timezone.utc)
    rows = {r["name"]: r for r in cat.cis_check_rows(now.isoformat(), last, now)}
    assert rows["jobs_starved"]["state"] == "ALARM" and rows["rds_xid_age"]["state"] == "OK"
    assert rows["route_fb.<stage>"]["state"] == "ALARM" and rows["rds_role_connections"]["state"] == "OK"


async def _has_topic(pool) -> bool:
    return bool(await pool.fetchval(
        "SELECT count(*) AS n FROM information_schema.columns WHERE table_schema = 'shared' "
        "AND table_name = 'alert_recipient' AND column_name = 'topic'"))


async def test_recipient_counts_per_topic(pool):
    if not await _has_topic(pool):   # the code PR's local run before the CIS 469 migration PR is on main
        pytest.skip("CIS 469 (alert_recipient.topic) is not in this tree yet")
    assert {g["key"] for g in await cat.recipient_gaps(pool)} >= {"page_no_recipient"}
    await pool.execute("INSERT INTO shared.alert_recipient (email, status, created_by, confirmed_at, topic) VALUES "
                       "('zz-aa1171-a@example.invalid', 'confirmed', 'admin:zz', now(), 'alerts'), "
                       "('zz-aa1171-p@example.invalid', 'confirmed', 'admin:zz', now(), 'page')")
    assert await cat.recipient_gaps(pool) == []


async def test_last_alerts_read_parses(pool):
    assert await pool.fetch(cat.LAST_ALERTS_SQL) == []
