"""S263 alert diet (AA-1025) — the in-app ops checks that replaced CloudWatch alarms (shared/ops_checks.py).

- log events: the structlog processor counts the events the removed metric filters matched; the names exist in code;
- state checks: the thresholds of the removed alarms (job queue, role connection limits, XID age);
- alerting: bell at most every 6 h, one SNS e-mail per check and UTC day, a failed publish removes its bell;
- the worker flushes its own events and never fails its loop for it.

asyncpg and boto3 are faked: no DB, no AWS.
"""
import asyncio
import io
import json
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from shared import log_config, ops_checks
from shared.jobs import worker as worker_mod

REPO = Path(__file__).resolve().parents[2]
NOW = datetime.now(timezone.utc)


# ── log events ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("event,check", sorted(ops_checks.EVENT_CHECKS.items()))
def test_every_named_event_matches_its_check(event, check):
    assert ops_checks.match_event({"event": event}) == check
    assert ops_checks.match_event({"event": f"{event} job=abc retry=1"}) == check   # stdlib formatted line
    assert check in ops_checks.CHECKS


def test_every_event_name_is_still_written_by_the_code():
    """Renaming an event in the code would silently drop its check (as it silently dropped the old alarm)."""
    for event in list(ops_checks.EVENT_CHECKS) + sorted(ops_checks.KNOWN_ADMIN_POOL_ROLLBACKS):
        out = subprocess.run(["git", "grep", "-l", "-e", f'"{event}"', "--", "*.py", ":!tests",
                              ":!shared/ops_checks.py"], cwd=REPO, capture_output=True, text=True)
        assert out.stdout.strip(), f"no code writes the log event {event!r}"


def test_admin_pool_route_unless_a_known_rollback():
    assert ops_checks.match_event({"event": "angle_gate_listed", "used_tenant_pool": False}) == \
        "tenant_route_admin_pool"
    assert ops_checks.match_event({"event": "publish_log_listed", "used_tenant_pool": False}) is None
    assert ops_checks.match_event({"event": "angle_gate_listed", "used_tenant_pool": True}) is None


def test_cis_app_refused_connection_in_any_text_field():
    line = {"event": "worker_cycle_failed",
            "exception": 'asyncpg.exceptions.TooManyConnectionsError: too many connections for role "cis_app"'}
    assert ops_checks.match_event(line) == "cis_app_too_many_connections"
    assert ops_checks.match_event({"event": "x", "error": 'too many connections for role "booking"'}) is None


def test_unrelated_lines_are_ignored():
    assert ops_checks.match_event({"event": "request_completed", "status": 200}) is None
    assert ops_checks.match_event({"event": 42}) is None
    assert ops_checks.match_event({}) is None


def test_capture_never_raises_and_returns_the_line(monkeypatch):
    monkeypatch.setattr(ops_checks, "match_event", MagicMock(side_effect=RuntimeError("boom")))
    line = {"event": "llm_call_log_write_failed"}
    assert ops_checks.capture(None, "warning", line) is line


def test_buffer_counts_drains_and_restores():
    buf = ops_checks.EventBuffer()
    buf.record("rds_xid_age", {"event": "a"}, now=NOW)
    buf.record("rds_xid_age", {"event": "b"}, now=NOW + timedelta(seconds=5))
    items = buf.drain()
    assert items["rds_xid_age"]["count"] == 2 and items["rds_xid_age"]["sample"] == {"event": "a"}
    assert len(buf) == 0
    buf.record("rds_xid_age", {}, now=NOW + timedelta(seconds=9))
    buf.restore(items)
    again = buf.drain()["rds_xid_age"]
    assert again["count"] == 3 and again["first_at"] == NOW.isoformat()


def test_the_json_handler_counts_events_through_structlog_and_stdlib(monkeypatch):
    buf = ops_checks.EventBuffer()
    monkeypatch.setattr(ops_checks, "BUFFER", buf)
    log_config.configure_logging("cis-api", stream=io.StringIO())
    try:
        structlog.get_logger().warning("decision_log_write_failed", reason="pool", stage="s1_rewrite")
        logging.getLogger("x").warning("aa817_tenant_flag_read_failed reason=%s", "timeout")
        structlog.get_logger().info("request_completed", status=200)
    finally:
        root = logging.getLogger()
        for h in list(root.handlers):
            if h.get_name() == log_config.HANDLER_NAME:
                root.removeHandler(h)
        structlog.reset_defaults()
    items = buf.drain()
    assert set(items) == {"decision_log_write_failed", "tenant_flag_read_failed"}
    assert items["decision_log_write_failed"]["sample"] == {"event": "decision_log_write_failed", "reason": "pool",
                                                            "stage": "s1_rewrite"}


# ── state checks ───────────────────────────────────────────────────────────────────────────────

def test_state_findings_thresholds():
    healthy = {"oldest_queued_age_seconds": 900.0, "overdue_running_jobs": 0, "xid_age": 1_000_000_000,
               "roles": {"booking": {"connections": 12, "limit": 15, "pct": 80.0}}}
    assert ops_checks.state_findings(healthy) == []
    bad = {"oldest_queued_age_seconds": 901.0, "starved": {"a3_atomize": 901}, "live_workers": 0,
           "overdue_running_jobs": 2, "overdue": [{"kind": "s1_rewrite"}],
           "xid_age": 1_000_000_001, "roles": {"crm_app": {"connections": 6, "limit": 6, "pct": 100.0}}}
    found = {f.check: f for f in ops_checks.state_findings(bad)}
    assert set(found) == {"jobs_starved", "jobs_overdue", "rds_role_connections", "rds_xid_age"}
    assert found["rds_role_connections"].summary == "crm_app 6/6"
    assert found["jobs_starved"].details["live_workers"] == 0


def test_a_failed_part_checks_nothing():
    assert ops_checks.state_findings({"rds_error": "x", "jobs_error": "y"}) == []


def test_render_subject_is_short_ascii(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    f = ops_checks.Finding("rds_role_connections", "crm_app 6/6", {"roles": {"crm_app": {"pct": 100.0}}})
    subject, text = ops_checks.render(f)
    assert subject == "[AA-CIS dev] Ops check: Database role near its connection limit"
    assert len(subject) <= 100 and subject.isascii()
    assert "crm_app 6/6" in text and "/admin/platform/apps" in text and '"pct": 100.0' in text


# ── alerting ───────────────────────────────────────────────────────────────────────────────────

class _CM:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _pool(last=None, mailed_today=False):
    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_CM())
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"last": last, "mailed_today": mailed_today})
    conn.fetchval = AsyncMock(return_value=77)
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=_CM(conn))
    pool.execute = AsyncMock()
    pool.conn = conn
    return pool


FINDING = ops_checks.Finding("jobs_starved", "oldest starved queued job waited 1200 s", {"live_workers": 0})


@pytest.fixture
def topic(monkeypatch):
    monkeypatch.setenv("ALERTS_TOPIC_ARN", "topic-placeholder")


@pytest.mark.asyncio
async def test_first_alert_writes_a_bell_and_mails(topic):
    pool, sns = _pool(), MagicMock()
    out = await ops_checks.raise_alert(pool, FINDING, sns)
    assert out == {"bell": True, "email": "sent"}
    sql, *args = pool.conn.fetchval.await_args.args
    assert "INSERT INTO shared.notifications" in sql
    assert args[1] == "platform.ops_check.jobs_starved" and args[2] == "jobs_starved" and args[4] == ["admin"]
    assert json.loads(args[3])["title"] == "Job queue starved"
    lock = pool.conn.execute.await_args_list[0].args
    assert "pg_advisory_xact_lock" in lock[0]
    kw = sns.publish.call_args.kwargs
    assert kw["TopicArn"] == "topic-placeholder" and "Job queue starved" in kw["Subject"]


@pytest.mark.asyncio
async def test_a_bell_in_the_last_six_hours_skips_everything(topic):
    pool, sns = _pool(last=NOW - timedelta(hours=5)), MagicMock()
    assert await ops_checks.raise_alert(pool, FINDING, sns) == {"bell": False}
    pool.conn.fetchval.assert_not_awaited() and sns.publish.assert_not_called()


@pytest.mark.asyncio
async def test_second_bell_of_the_day_does_not_mail(topic):
    pool, sns = _pool(last=NOW - timedelta(hours=7), mailed_today=True), MagicMock()
    assert await ops_checks.raise_alert(pool, FINDING, sns) == {"bell": True, "email": "already_today"}
    sns.publish.assert_not_called()


@pytest.mark.asyncio
async def test_failed_publish_removes_its_bell_so_the_next_tick_retries(topic):
    pool, sns = _pool(), MagicMock()
    sns.publish.side_effect = RuntimeError("AccessDenied")
    assert await ops_checks.raise_alert(pool, FINDING, sns) == {"bell": False, "email": "failed"}
    assert pool.execute.await_args.args == (ops_checks.DELETE_BELL_SQL, 77)


@pytest.mark.asyncio
async def test_no_topic_bell_only(monkeypatch):
    monkeypatch.delenv("ALERTS_TOPIC_ARN", raising=False)
    pool, factory = _pool(), MagicMock()
    out = await ops_checks.raise_alert(pool, FINDING, client_factory=factory)
    assert out == {"bell": True, "email": "not_configured"} and not factory.called


@pytest.mark.asyncio
async def test_alert_all_isolates_a_failing_finding(topic):
    pool = _pool()
    pool.acquire = MagicMock(side_effect=[RuntimeError("pool closed"), _CM(pool.conn)])
    other = ops_checks.Finding("rds_xid_age", "max(age(datfrozenxid)) = 1100000000")
    out = await ops_checks.alert_all(pool, [FINDING, other], MagicMock())
    assert "pool closed" in out["jobs_starved"]["error"] and out["rds_xid_age"] == {"bell": True, "email": "sent"}


@pytest.mark.asyncio
async def test_flush_events_alerts_and_keeps_what_failed(topic):
    buf = ops_checks.EventBuffer()
    buf.record("llm_call_log_write_failed", {"event": "llm_call_log_write_failed"})
    buf.record("tenant_pool_unavailable", {"event": "aa817_tenant_pool_unavailable"})
    pool = _pool()
    pool.acquire = MagicMock(side_effect=[RuntimeError("db down"), _CM(pool.conn)])
    out = await ops_checks.flush_events(pool, MagicMock(), buffer=buf)
    assert "error" in out["llm_call_log_write_failed"] and out["tenant_pool_unavailable"]["bell"] is True
    assert set(buf.drain()) == {"llm_call_log_write_failed"}
    assert await ops_checks.flush_events(pool, MagicMock(), buffer=buf) == {}   # nothing counted: no DB call


@pytest.mark.asyncio
async def test_flush_cancelled_mid_way_loses_nothing(topic):
    buf = ops_checks.EventBuffer()
    buf.record("rds_xid_age", {})

    async def hang(*a, **kw):
        await asyncio.sleep(3600)

    pool = _pool()
    pool.conn.fetchrow = AsyncMock(side_effect=hang)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(ops_checks.flush_events(pool, MagicMock(), buffer=buf), 0.05)
    assert buf.drain()["rds_xid_age"]["count"] == 1


# ── worker flush ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_worker_flush_never_raises(monkeypatch):
    w = worker_mod.Worker(MagicMock())
    monkeypatch.setattr(ops_checks, "flush_events", AsyncMock(side_effect=RuntimeError("db down")))
    await w._flush_ops_events()
    monkeypatch.setattr(ops_checks, "flush_events", AsyncMock(return_value={"x": {"bell": True}}))
    await w._flush_ops_events()
    assert w.ops_flush_every_seconds == 300.0
