"""S263 alert diet (AA-1025) — shared/ops_checks.py raise_alert on a real shared.notifications.

- the first alert writes one admin bell (platform tenant, roles admin, entity ops_check) and publishes one e-mail;
- a second alert within 6 h writes nothing; a bell older than 6 h but from today writes a bell without an e-mail;
  yesterday's bell lets today's first one mail.

SNS is a recording stub.
"""
import json
import os
import sys

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import ops_checks  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
EVENT = "platform.ops_check.jobs_starved"
FINDING = ops_checks.Finding("jobs_starved", "oldest starved queued job waited 1200 s", {"live_workers": 0})


class StubSns:
    def __init__(self):
        self.calls = []

    def publish(self, **kw):
        self.calls.append(kw)
        return {"MessageId": "m-1"}


@pytest_asyncio.fixture
async def pool(monkeypatch):
    monkeypatch.setenv("ALERTS_TOPIC_ARN", "topic-placeholder")
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    await p.execute("DELETE FROM shared.notifications WHERE event_type LIKE 'platform.ops_check.%'")
    yield p
    await p.execute("DELETE FROM shared.notifications WHERE event_type LIKE 'platform.ops_check.%'")
    await p.close()


async def _bells(pool):
    return await pool.fetch("SELECT tenant_id::text AS tenant, entity_type AS et, entity_id AS eid, payload, "
                            "target_roles AS roles FROM shared.notifications WHERE event_type = $1", EVENT)


async def test_first_alert_bell_and_email_then_quiet(pool):
    sns = StubSns()
    assert await ops_checks.raise_alert(pool, FINDING, sns) == {"bell": True, "email": "sent"}
    rows = await _bells(pool)
    assert len(rows) == 1
    row = rows[0]
    assert row["tenant"] == ops_checks.PLATFORM_TENANT_ID and row["et"] == "ops_check" and row["eid"] == "jobs_starved"
    assert list(row["roles"]) == ["admin"]
    assert json.loads(row["payload"])["title"] == "Job queue starved"
    assert len(sns.calls) == 1 and sns.calls[0]["Subject"].endswith("Ops check: Job queue starved")

    assert await ops_checks.raise_alert(pool, FINDING, sns) == {"bell": False}
    assert len(await _bells(pool)) == 1 and len(sns.calls) == 1


async def test_a_later_bell_the_same_day_does_not_mail_again(pool):
    sns = StubSns()
    await ops_checks.raise_alert(pool, FINDING, sns)
    # age the first bell past the 6-hour throttle but keep it inside today (UTC) when possible
    await pool.execute("UPDATE shared.notifications SET created_at = greatest(now() - interval '7 hours', "
                       "date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') WHERE event_type = $1", EVENT)
    aged = await pool.fetchval("SELECT created_at <= now() - interval '6 hours' FROM shared.notifications "
                               "WHERE event_type = $1", EVENT)
    if not aged:
        pytest.skip("less than 6 hours into the UTC day: the same-day case cannot be built")
    assert await ops_checks.raise_alert(pool, FINDING, sns) == {"bell": True, "email": "already_today"}
    assert len(await _bells(pool)) == 2 and len(sns.calls) == 1


async def test_yesterdays_bell_lets_todays_first_one_mail(pool):
    sns = StubSns()
    await ops_checks.raise_alert(pool, FINDING, sns)
    await pool.execute("UPDATE shared.notifications SET created_at = "
                       "(date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') - interval '1 minute' "
                       "WHERE event_type = $1", EVENT)
    stale = await pool.fetchval("SELECT created_at <= now() - interval '6 hours' FROM shared.notifications "
                                "WHERE event_type = $1", EVENT)
    if not stale:
        pytest.skip("less than 6 hours into the UTC day: yesterday's bell is still inside the throttle")
    assert await ops_checks.raise_alert(pool, FINDING, sns) == {"bell": True, "email": "sent"}
    assert len(sns.calls) == 2
