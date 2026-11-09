"""AA-1025 H1b — the ``llm_spend_alert`` ops check on a real shared.llm_app_profile (CIS 345) and shared.llm_call_log.

- today's (UTC) spend of the tripplanner rows (the ledger v2 app column or the pre-v2 quality_signal app), not
  yesterday's and not another app's, against the profile's ``default_daily_budget_usd``;
- no budget = no check; 80 % = near, 100 % = cap; the cap finding writes one admin bell with its own check name.
Invented stages and models; the rows are removed again (in a transaction that is rolled back where possible).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from conftest import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER  # noqa: E402
from shared import ops_checks  # noqa: E402

DSN = f"postgresql://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
STAGE = "tp_zz_check"


@pytest_asyncio.fixture
async def pool(monkeypatch):
    monkeypatch.delenv("ALERTS_TOPIC_ARN", raising=False)        # bell only (no SNS)
    p = await asyncpg.create_pool(DSN, min_size=1, max_size=2)

    async def clean():
        await p.execute("DELETE FROM shared.llm_call_log WHERE stage = $1", STAGE)
        await p.execute("DELETE FROM shared.notifications WHERE event_type LIKE 'platform.ops_check.llm_spend%'")
        await p.execute("UPDATE shared.llm_app_profile SET default_daily_budget_usd = NULL WHERE app = 'tripplanner'")
    await clean()
    yield p
    await clean()
    await p.close()


async def _log(pool, cost, *, app="tripplanner", at=NOW, legacy=False):
    signal = json.dumps({"app": app}) if legacy else "{}"
    await pool.execute(
        "INSERT INTO shared.llm_call_log (stage, role, model, quality_signal, cost_usd, app, created_at) "
        "VALUES ($1, 'writer', 'zz-model', $2::jsonb, $3, $4, $5)",
        STAGE, signal, cost, None if legacy else app, at)


async def _budget(pool, usd):
    await pool.execute("UPDATE shared.llm_app_profile SET default_daily_budget_usd = $1 WHERE app = 'tripplanner'",
                       usd)


async def test_no_budget_is_no_check(pool):
    await _log(pool, 50.0)
    assert await ops_checks.app_spend_findings(pool, now=NOW) == []


async def test_todays_tripplanner_rows_only(pool):
    await _budget(pool, 10.0)
    await _log(pool, 3.0)
    await _log(pool, 4.0, legacy=True)                              # a pre-v2 row: app in quality_signal only
    await _log(pool, 100.0, app="cis")
    await _log(pool, 100.0, at=NOW - timedelta(days=1, hours=1))    # yesterday
    found = await ops_checks.app_spend_findings(pool, now=NOW)
    assert found == []                                              # 7 of 10 USD: under 80 %
    await _log(pool, 1.5)
    [near] = await ops_checks.app_spend_findings(pool, now=NOW)
    assert near.check == "llm_spend_alert.tripplanner.near" and near.details["spent_usd"] == pytest.approx(8.5)


async def test_the_cap_writes_one_bell(pool):
    await _budget(pool, 1.0)
    await _log(pool, 1.25)
    [cap] = await ops_checks.app_spend_findings(pool, now=NOW)
    assert cap.check == "llm_spend_alert.tripplanner.cap"
    assert await ops_checks.alert_all(pool, [cap]) == {cap.check: {"bell": True, "email": "not_configured"}}
    rows = await pool.fetch("SELECT entity_id, payload FROM shared.notifications WHERE event_type = $1",
                            ops_checks.EVENT_TYPE_PREFIX + cap.check)
    assert len(rows) == 1 and rows[0]["entity_id"] == cap.check
    assert json.loads(rows[0]["payload"])["title"] == "TripPlanner AI spend reached its daily cap"
