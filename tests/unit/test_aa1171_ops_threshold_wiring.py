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


def test_each_check_family_bells_on_its_own_window_setting(setting):
    """Every bell window is a setting (route, daily, nightly, default); only route_back keeps a technical window."""
    families = {"route_fb.st": ops_thresholds.BELL_EVERY_ROUTE_HOURS, "route_last.st": ops_thresholds.BELL_EVERY_ROUTE_HOURS,
                "route_cost.st": ops_thresholds.BELL_EVERY_DAILY_HOURS,
                "route_cut.st": ops_thresholds.BELL_EVERY_DAILY_HOURS,
                "llm_stage_coverage.st": ops_thresholds.BELL_EVERY_DAILY_HOURS,
                "route_digest": ops_thresholds.BELL_EVERY_NIGHTLY_HOURS,
                "qf_cut.st": ops_thresholds.BELL_EVERY_NIGHTLY_HOURS, "qf_digest": ops_thresholds.BELL_EVERY_NIGHTLY_HOURS,
                "research_stale_share": ops_thresholds.BELL_EVERY_NIGHTLY_HOURS,
                "research_auto_failed": ops_thresholds.BELL_EVERY_NIGHTLY_HOURS,
                "jobs_starved": ops_thresholds.BELL_EVERY_HOURS}
    for name, key in families.items():
        assert ops_checks.check_for(name).bell_key == key, name
    setting[ops_thresholds.BELL_EVERY_ROUTE_HOURS] = 3
    setting[ops_thresholds.BELL_EVERY_DAILY_HOURS] = 48
    setting[ops_thresholds.BELL_EVERY_NIGHTLY_HOURS] = 16
    assert ops_checks.check_for("route_fb.st").bell_every == timedelta(hours=3)
    assert ops_checks.check_for("llm_stage_coverage.st").bell_every == timedelta(hours=48)
    assert ops_checks.check_for("qf_drift.st").bell_every == timedelta(hours=16)
    back = ops_checks.check_for("route_back.st")
    assert back.fixed_bell == timedelta(minutes=1) and back.bell_every == timedelta(minutes=1)


def test_route_cut_share_follows_the_setting(setting):
    from shared.llm_client import route_watch

    stats = {"st": {"replies": 100, "cut": 7}}
    assert [f.check for f in route_watch.cut_findings(stats, {})] == ["route_cut.st"]        # 7 % > 5 %
    setting[ops_thresholds.REPLY_CUT_ALERT_PCT] = 8
    assert route_watch.cut_findings(stats, {}) == []
    setting[ops_thresholds.REPLY_CUT_ALERT_PCT] = 6
    assert route_watch.cut_findings(stats, {})[0].details["threshold_pct"] == 6.0


def test_quality_funnel_rules_follow_the_settings(setting):
    from services.quality import funnel

    rec, end = datetime(2026, 11, 14, tzinfo=timezone.utc), datetime(2026, 11, 21, tzinfo=timezone.utc)

    def w(stage, model, win, answered, cut=0):
        return {"stage": stage, "model": model, "win": win, "answered": answered, "fallback_answered": 0,
                "replies": answered, "cut": cut}

    rows = [w("t9_write", "m-old", "prior", 200), w("t9_write", "m-old", "recent", 170, cut=12),
            w("t9_write", "m-new", "recent", 30)]
    checks = lambda: sorted(f.check for f in funnel.watch_findings(  # noqa: E731
        funnel.summarize_watch(rows, {}, recent=rec, end=end)))
    assert checks() == ["qf_cut.t9_write", "qf_drift.t9_write"]               # 6 % cut, 15 % drift
    setting[ops_thresholds.REPLY_CUT_ALERT_PCT] = 7
    setting[ops_thresholds.QF_DRIFT_ALERT_PCT] = 20
    assert checks() == []
    setting[ops_thresholds.QF_DRIFT_ALERT_PCT] = 10
    setting[ops_thresholds.QF_MIN_REQUESTS] = 250                               # 200 rows a week: under the floor
    assert checks() == []
    setting[ops_thresholds.QF_MIN_REQUESTS] = 20
    rows.append(w("t9_write", "m-new", "prior", 4))                             # 2 % of the prior week
    assert checks() == []                                                        # m-new is not new under 1 %
    setting[ops_thresholds.QF_DRIFT_NEW_MODEL_PCT] = 5
    assert checks() == ["qf_drift.t9_write"]
    th = funnel.thresholds()
    assert (th["min_requests"], th["cut_alert_pct"], th["drift_alert_pct"], th["drift_new_model_pct"]) == \
        (20, 7.0, 10.0, 5.0)
