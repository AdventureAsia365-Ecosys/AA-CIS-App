"""AA-1171 (S287 decision D3) — the CIS in-app checks read their thresholds from Platform › Settings (CIS 469): a
changed value changes what fires. The setting values are patched on the reader (no DB); invented numbers only."""
from datetime import datetime, timedelta, timezone

import pytest

from shared import ops_checks, ops_thresholds, settings
from shared.jobs import backlog
from services.acp_contract import research_coverage as rc
from services.recompute import lifecycle


@pytest.fixture
def setting(monkeypatch):
    values: dict = {}
    real = settings.get

    def get(key, tenant_id=None):
        return values[key] if key in values else real(key, tenant_id)

    monkeypatch.setattr(settings, "get", get)
    return values


def test_jobs_starved_follows_the_setting(setting):
    tick = {"oldest_queued_age_seconds": 1000}
    assert [f.check for f in ops_checks.state_findings(tick)] == ["jobs_starved"]   # default 900 s
    setting[ops_thresholds.JOBS_STARVED_AFTER_S] = 1200
    assert ops_checks.state_findings(tick) == []
    setting[ops_thresholds.JOBS_STARVED_AFTER_S] = 600
    found = ops_checks.state_findings({"oldest_queued_age_seconds": 700})
    assert found[0].check == "jobs_starved" and found[0].details["threshold_s"] == 600
    assert "alert after 600 s" in found[0].summary


def test_role_connections_and_xid_follow_the_settings(setting):
    tick = {"roles": {"cis_app": {"pct": 75.0, "connections": 15, "limit": 20}}, "xid_age": 900_000_000}
    assert ops_checks.state_findings(tick) == []
    setting[ops_thresholds.ROLE_CONNECTIONS_PCT] = 70
    setting[ops_thresholds.XID_AGE_LIMIT] = 800_000_000
    assert sorted(f.check for f in ops_checks.state_findings(tick)) == ["rds_role_connections", "rds_xid_age"]


def test_app_spend_near_share_follows_the_setting(setting):
    rows = [{"app": "tripplanner", "cap_usd": 10.0, "spent_usd": 7.0}]
    assert ops_checks.spend_findings(rows, "2026-11-21") == []          # 70 % < 80 %
    setting[ops_thresholds.APP_SPEND_NEAR_PCT] = 60
    assert [f.check for f in ops_checks.spend_findings(rows, "2026-11-21")] == ["llm_spend_alert.tripplanner.near"]


def test_overdue_factor_recompute_window_and_stale_share_follow_the_settings(setting):
    assert backlog.overdue_factor() == 2.0 and lifecycle.lag_args()[2] == 1800.0 and rc.stale_share_limit() == 0.2
    setting[ops_thresholds.JOBS_OVERDUE_FACTOR] = 3.5
    setting[ops_thresholds.RECOMPUTE_LAG_LIMIT_S] = 3600
    setting[ops_thresholds.RESEARCH_STALE_SHARE_PCT] = 35
    assert backlog.overdue_factor() == 3.5 and lifecycle.lag_args()[2] == 3600.0 and rc.stale_share_limit() == 0.35
    out = rc.summarize(["p1", "p2", "p3"], {}, [])
    assert out["limit"] == 0.35


@pytest.mark.asyncio
async def test_bell_window_follows_the_setting(setting):
    """raise_alert writes no second bell inside the window, and does once the window is shorter."""
    now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(hours=3)

    class Conn:
        def __init__(self):
            self.bells = 0

        async def execute(self, *a):
            return "SELECT 1"

        async def fetchrow(self, *a):
            return {"last": last, "mailed_today": True}

        async def fetchval(self, *a):
            self.bells += 1
            return 1

        def transaction(self):
            conn = self

            class T:
                async def __aenter__(self):
                    return conn

                async def __aexit__(self, *e):
                    return False
            return T()

    class Pool:
        def __init__(self):
            self.conn = Conn()

        def acquire(self):
            pool = self

            class A:
                async def __aenter__(self):
                    return pool.conn

                async def __aexit__(self, *e):
                    return False
            return A()

    finding = ops_checks.Finding("jobs_starved", "x")
    pool = Pool()
    assert await ops_checks.raise_alert(pool, finding, now=now) == {"bell": False}      # default 6 h window
    setting[ops_thresholds.BELL_EVERY_HOURS] = 2
    out = await ops_checks.raise_alert(pool, finding, now=now)
    assert out["bell"] is True and pool.conn.bells == 1
