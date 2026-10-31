"""AA-751 / AA-832 — the api's in-process ops metrics sampler (api/core/ops_metrics_sampler.py) and the
capacity-aware backlog sample it publishes (shared/jobs/backlog.py).

- AA/RDS per-role connections (moved from the removed ops_rds_role_connections job, names unchanged so
  the AA-832 alarms keep working) and AA/Jobs/CIS OldestQueuedAgeSeconds + OverdueRunningJobs;
- one PutMetricData per namespace per tick, boto3 timeouts, one part failing never blocks the other;
- the loop never raises, is off in ENVIRONMENT=test and is cancelled before the main pool closes;
- the old schedule endpoint and job kind are gone; the deploy_gate_exempt flag still works.

asyncpg and boto3 are mocked — no DB, no AWS. The SQL runs on real Postgres in
tests/integration/test_aa751_ops_metrics_sampler.py.
"""
import asyncio
import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from api.core import ops_metrics_sampler as sampler
from shared.jobs import backlog, queue, registry, worker


@pytest.fixture(autouse=True)
def _no_jev_credit_check(monkeypatch):
    """AA-845: the loop also runs the Jev low-credit check; these tests are about the metrics tick, so it is
    stubbed (its own tests: tests/unit/test_aa845_jev_credit_alerts.py)."""
    monkeypatch.setattr(sampler, "jev_credit_tick", AsyncMock(return_value=None))

NOW = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)
_TEST_SECRET = "test-admin-secret"


def ago(seconds):
    return NOW - timedelta(seconds=seconds)


def K(background=False, expected_seconds=600):
    return SimpleNamespace(background=background, expected_seconds=expected_seconds)


KINDS = {"s1_rewrite": K(expected_seconds=300), "a3_atomize": K(), "place_geocode": K(background=True)}
CAPS = {"s1_rewrite": 6, "a3_atomize": 1, "place_geocode": 1}


def W(worker_id="w1", max_parallel=8, caps=None, alive=True, started=3600, seen=5, revision=None,
      fresh=None):
    return {"worker_id": worker_id, "max_parallel": max_parallel, "caps": caps if caps is not None else CAPS,
            "alive": alive, "fresh": alive if fresh is None else fresh, "started_at": ago(started),
            "last_seen_at": ago(seen), "task_revision": revision}


def R(kind, locked_by="w1", started=60, attempt_started=None, job_id=None):
    return {"id": job_id or f"{kind}-{started}", "kind": kind, "locked_by": locked_by, "started_at": ago(started),
            "attempt_started_at": None if attempt_started is None else ago(attempt_started).isoformat()}


def Q(kind, oldest, n=1):
    return {"kind": kind, "n": n, "oldest_run_after": ago(oldest)}


# ── claim_caps_for: one rule for the worker and the sampler ─────────────────────────────────────

def test_worker_claim_caps_is_claim_caps_for():
    worker.load_kinds()
    bg = worker.background_kinds()
    assert bg, "the registry has background kinds (place pipeline, retention)"
    for max_parallel, free, running_bg in itertools.product((1, 2, 4, 8), range(0, 9), (0, 1, 2)):
        w = worker.Worker(MagicMock(), max_parallel=max_parallel)
        assert w.claim_caps(free, running_bg) == worker.claim_caps_for(max_parallel, w.caps(), free, running_bg, bg)


def test_claim_caps_for_rules():
    bg = {"place_geocode"}
    assert "place_geocode" in worker.claim_caps_for(8, CAPS, 3, 0, bg)
    assert "place_geocode" not in worker.claim_caps_for(8, CAPS, 2, 0, bg)      # reserve (2)
    assert "place_geocode" not in worker.claim_caps_for(8, CAPS, 5, 1, bg)      # BACKGROUND_MAX_RUNNING
    assert "place_geocode" in worker.claim_caps_for(1, CAPS, 1, 0, bg)          # small worker: reserve 0
    caps = dict(CAPS)
    worker.claim_caps_for(8, caps, 1, 0, bg)
    assert caps == CAPS                                                          # input not mutated


# ── backlog_sample: OldestQueuedAgeSeconds ──────────────────────────────────────────────────────

def test_idle_queue_is_zero():
    out = backlog.backlog_sample([], [], [W()], KINDS, NOW, ago(10))
    assert out["oldest_queued_age_seconds"] == 0.0 and out["overdue_running_jobs"] == 0
    assert out["starved"] == {} and out["live_workers"] == 1


def test_no_live_worker_counts_since_the_last_worker_was_seen():
    workers = [W(alive=False, seen=600)]
    out = backlog.backlog_sample([Q("s1_rewrite", 1800, n=4)], [], workers, KINDS, NOW, ago(5))
    assert out["starved"] == {"s1_rewrite": {"reason": "no_live_worker", "ready": 4, "age_s": 600}}
    assert out["oldest_queued_age_seconds"] == 600.0
    # a row due after the worker left is starved since its run_after
    out = backlog.backlog_sample([Q("s1_rewrite", 120)], [], workers, KINDS, NOW, None)
    assert out["oldest_queued_age_seconds"] == 120.0
    # no worker row at all: since run_after
    out = backlog.backlog_sample([Q("s1_rewrite", 1800)], [], [], KINDS, NOW, None)
    assert out["oldest_queued_age_seconds"] == 1800.0


def test_unregistered_kind_is_starved_since_run_after():
    """The Window B probe (`x6_probe`, run_after now - 20 min) must fire on the next sample."""
    out = backlog.backlog_sample([Q("x6_probe", 1200)], [], [W()], KINDS, NOW, ago(1))
    assert out["starved"]["x6_probe"]["reason"] == "unregistered"
    assert out["oldest_queued_age_seconds"] == 1200.0


def test_wave_that_fills_every_slot_is_zero():
    running = [R("s1_rewrite", started=100 + i) for i in range(6)] + [R("a3_atomize"), R("place_geocode")]
    out = backlog.backlog_sample([Q("s1_rewrite", 3000, n=50), Q("a3_atomize", 2500, n=30)], running, [W()],
                                 KINDS, NOW, ago(400))
    assert out["oldest_queued_age_seconds"] == 0.0 and out["starved"] == {}


def test_kind_at_its_global_cap_is_not_starved():
    running = [R("s1_rewrite", started=100 + i) for i in range(6)]      # cap 6 reached, 2 slots free
    out = backlog.backlog_sample([Q("s1_rewrite", 3000, n=50)], running, [W()], KINDS, NOW, ago(900))
    assert out["starved"] == {}


def test_global_cap_counts_running_rows_of_every_worker():
    running = [R("a3_atomize", locked_by="w2")]
    workers = [W("w1"), W("w2")]
    out = backlog.backlog_sample([Q("a3_atomize", 2000)], running, workers, KINDS, NOW, ago(900))
    assert out["starved"] == {}


def test_background_kind_behind_the_reserve_is_not_starved():
    running = [R("s1_rewrite", started=100 + i) for i in range(6)]      # 2 free = the reserve
    out = backlog.backlog_sample([Q("place_geocode", 4000, n=200)], running, [W()], KINDS, NOW, ago(900))
    assert out["starved"] == {}
    # ... and behind another background job (BACKGROUND_MAX_RUNNING = 1)
    running = [R("place_geocode", started=50)]
    out = backlog.backlog_sample([Q("place_geocode", 4000, n=200)], running, [W()], KINDS, NOW, ago(900))
    assert out["starved"] == {}


def test_claimable_row_is_starved_since_capacity_last_freed():
    """A live worker has free slots and the kind is under its cap: the worker should have taken it.
    Starved since the last finish / re-queue, not since run_after."""
    out = backlog.backlog_sample([Q("a3_atomize", 3000)], [], [W(started=7200)], KINDS, NOW, ago(1000))
    assert out["starved"]["a3_atomize"] == {"reason": "claimable", "ready": 1, "age_s": 1000}
    assert out["oldest_queued_age_seconds"] == 1000.0


def test_claimable_right_after_a_finish_is_not_paged():
    """F13 transient: a wave job finished 3 s ago and the worker has not polled yet. The next
    s1_rewrite row has waited 40 min, but it has been claimable for 3 s only."""
    running = [R("s1_rewrite", started=100 + i) for i in range(5)]      # one of 6 just finished
    out = backlog.backlog_sample([Q("s1_rewrite", 2400, n=40)], running, [W()], KINDS, NOW, ago(3))
    assert out["starved"]["s1_rewrite"]["age_s"] == 3 and out["oldest_queued_age_seconds"] == 3.0


def test_claimable_bound_never_earlier_than_run_after_and_worker_start():
    out = backlog.backlog_sample([Q("a3_atomize", 60)], [], [W()], KINDS, NOW, ago(5000))
    assert out["oldest_queued_age_seconds"] == 60.0
    out = backlog.backlog_sample([Q("a3_atomize", 3000)], [], [W(started=120)], KINDS, NOW, ago(5000))
    assert out["oldest_queued_age_seconds"] == 120.0
    out = backlog.backlog_sample([Q("a3_atomize", 3000)], [], [W(started=120)], KINDS, NOW, None)
    assert out["oldest_queued_age_seconds"] == 120.0


def test_yielding_worker_does_not_count_as_a_claimer():
    """After a deploy the old task's worker stops claiming once a newer revision is live (AA-711)."""
    old = W("old", revision=10, started=7200)
    new = W("new", revision=11, started=30)
    full = [R("a3_atomize", locked_by="new")]
    out = backlog.backlog_sample([Q("s1_rewrite", 3000)], full, [old, W("new", max_parallel=1, revision=11)],
                                 KINDS, NOW, ago(2000))
    assert out["starved"] == {}            # only the yielding old worker had a free slot
    out = backlog.backlog_sample([Q("s1_rewrite", 3000)], [], [old, new], KINDS, NOW, ago(2000))
    assert out["starved"]["s1_rewrite"]["age_s"] == 30    # the new worker started 30 s ago


def test_worker_caps_decide_registration_not_the_api_registry():
    """A kind the live worker does not have is unregistered even if this process knows it."""
    w = W(caps={"s1_rewrite": 6})
    out = backlog.backlog_sample([Q("a3_atomize", 1500)], [], [w], KINDS, NOW, ago(1))
    assert out["starved"]["a3_atomize"]["reason"] == "unregistered"


def test_caps_as_json_text():
    w = W(caps='{"a3_atomize": 1}')
    out = backlog.backlog_sample([Q("a3_atomize", 3000)], [], [w], KINDS, NOW, ago(1000))
    assert out["starved"]["a3_atomize"]["reason"] == "claimable"


def test_oldest_is_the_max_over_starved_kinds():
    out = backlog.backlog_sample([Q("x6_probe", 1200), Q("a3_atomize", 3000)], [], [W(started=7200)],
                                 KINDS, NOW, ago(400))
    assert out["oldest_queued_age_seconds"] == 1200.0 and out["queued_due"] == 2


# ── backlog_sample: OverdueRunningJobs ──────────────────────────────────────────────────────────

def test_overdue_uses_two_x_expected_seconds():
    running = [R("s1_rewrite", started=601, job_id="a"),           # expected 300: 601 > 600
               R("s1_rewrite", started=599, job_id="b"),
               R("a3_atomize", started=1199, job_id="c"),          # expected 600: not yet
               R("unknown_kind", started=1201, job_id="d")]        # default 600
    out = backlog.backlog_sample([], running, [W(max_parallel=16)], KINDS, NOW, ago(1))
    assert out["overdue_running_jobs"] == 2
    assert {o["id"] for o in out["overdue"]} == {"a", "d"}
    assert next(o for o in out["overdue"] if o["id"] == "d")["expected_s"] == 600


def test_overdue_measures_the_current_attempt():
    """F18: a re-claimed job keeps its first started_at; attempt_started_at is the current attempt."""
    reclaimed = R("s1_rewrite", started=5000, attempt_started=30)
    assert backlog.backlog_sample([], [reclaimed], [W()], KINDS, NOW)["overdue_running_jobs"] == 0
    stuck = R("s1_rewrite", started=5000, attempt_started=900)
    assert backlog.backlog_sample([], [stuck], [W()], KINDS, NOW)["overdue_running_jobs"] == 1
    legacy = R("s1_rewrite", started=5000)                       # claimed before F18: started_at
    assert backlog.backlog_sample([], [legacy], [W()], KINDS, NOW)["overdue_running_jobs"] == 1
    garbage = dict(R("s1_rewrite", started=100), attempt_started_at="not a time")
    assert backlog.backlog_sample([], [garbage], [W()], KINDS, NOW)["overdue_running_jobs"] == 0


def test_overdue_list_is_capped_in_the_log_line():
    running = [R("s1_rewrite", started=5000 + i, job_id=str(i)) for i in range(15)]
    out = backlog.backlog_sample([], running, [W(max_parallel=32)], KINDS, NOW)
    assert out["overdue_running_jobs"] == 15 and len(out["overdue"]) == 10


# ── claim writes attempt_started_at (F18) ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_claim_merges_attempt_started_at():
    from shared.jobs import queue

    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.transaction.return_value.__aenter__ = AsyncMock()
    conn.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    await queue.claim(pool, "w1", {"k": 1}, 60)
    flat = " ".join(conn.fetchrow.call_args[0][0].split())
    assert "jsonb_build_object('attempt_started_at', now())" in flat
    assert "started_at = coalesce(started_at, now())" in flat


# ── AA/RDS helpers (moved, unchanged) ───────────────────────────────────────────────────────────

def _role_rows():
    return [
        {"rolname": "aa_app_user", "rolconnlimit": 15, "connections": 3},
        {"rolname": "booking", "rolconnlimit": 15, "connections": 12},
        {"rolname": "tripplanner", "rolconnlimit": 30, "connections": 0},
    ]


def test_role_sql_shape():
    flat = " ".join(sampler.ROLE_CONNECTIONS_SQL.split())
    assert "FROM pg_roles r LEFT JOIN pg_stat_activity a ON a.usename = r.rolname" in flat
    assert "WHERE r.rolcanlogin AND r.rolconnlimit > 0" in flat
    assert "count(a.pid)" in flat


def test_role_samples_pct():
    out = sampler.role_samples(_role_rows())
    assert out[1] == {"role": "booking", "limit": 15, "connections": 12, "pct": 80.0}
    assert out[0]["pct"] == 20.0 and out[2]["pct"] == 0.0
    assert sampler.role_samples([{"rolname": "x", "rolconnlimit": 3, "connections": 1}])[0]["pct"] == 33.33


def test_rds_metric_data_two_datums_per_role():
    data = sampler.metric_data(sampler.role_samples(_role_rows()), NOW)
    assert len(data) == 6
    booking = [d for d in data if d["Dimensions"] == [{"Name": "Role", "Value": "booking"}]]
    assert {(d["MetricName"], d["Unit"], d["Value"]) for d in booking} == {
        ("RoleConnections", "Count", 12.0), ("RoleConnectionLimitPct", "Percent", 80.0)}
    assert all(d["Timestamp"] == NOW for d in data)
    assert sampler.NAMESPACE_RDS == "AA/RDS"


def test_jobs_metric_data_no_dimensions():
    data = sampler.jobs_metric_data({"oldest_queued_age_seconds": 1200.0, "overdue_running_jobs": 2}, NOW)
    assert data == [
        {"MetricName": "OldestQueuedAgeSeconds", "Timestamp": NOW, "Value": 1200.0, "Unit": "Seconds"},
        {"MetricName": "OverdueRunningJobs", "Timestamp": NOW, "Value": 2.0, "Unit": "Count"},
    ]
    assert sampler.NAMESPACE_JOBS == "AA/Jobs/CIS"


def test_cloudwatch_client_timeouts_and_region(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with patch("boto3.client", return_value=MagicMock()) as make:
        sampler.cloudwatch_client()
    args, kw = make.call_args
    assert args == ("cloudwatch",) and kw["region_name"] == "us-west-1"
    cfg = kw["config"]
    assert cfg.connect_timeout == 3 and cfg.read_timeout == 5 and cfg.retries == {"max_attempts": 1}
    monkeypatch.setenv("AWS_REGION", "ap-southeast-1")
    with patch("boto3.client", return_value=MagicMock()) as make:
        sampler.cloudwatch_client()
    assert make.call_args.kwargs["region_name"] == "ap-southeast-1"


# ── sample_once ─────────────────────────────────────────────────────────────────────────────────

class StubCloudWatch:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def put_metric_data(self, **kw):
        self.calls.append(kw)
        if kw["Namespace"] in self.fail_on:
            raise RuntimeError(f"AccessDenied on {kw['Namespace']}")
        return {}


_SAMPLE = {"oldest_queued_age_seconds": 0.0, "overdue_running_jobs": 0, "starved": {}, "overdue": [],
           "queued_due": 3, "running": 2, "live_workers": 1}


class _CM:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


def _feed_conn(cursor=None):
    """AA-835: the connection the supply feed pull acquires; cursor None = the cursor row is locked (busy)."""
    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_CM())
    conn.execute = AsyncMock(return_value="INSERT 0 0")
    conn.fetchval = AsyncMock(return_value=cursor)
    conn.fetch = AsyncMock(return_value=[])
    return conn


def _pool(rows=None, error=None, execute="UPDATE 0"):
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=_role_rows() if rows is None else rows, side_effect=error)
    pool.execute = AsyncMock(return_value=execute)   # the retired-kind sweep
    pool.fetchval = AsyncMock(return_value=0)        # AA-987: the TripPlanner purge sweep (jobs created)
    pool.fetchrow = AsyncMock(return_value=None)     # AA-1003: the daily KPI snapshot enqueue (already queued)
    pool.feed_conn = _feed_conn()
    pool.acquire = MagicMock(side_effect=lambda *a, **kw: _CM(pool.feed_conn))   # AA-835: the supply feed pull
    return pool


async def _hang(*a, **kw):
    await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_sample_once_one_call_per_namespace():
    cw = StubCloudWatch()
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)) as bs:
        out = await sampler.sample_once(_pool(), cw, kinds=KINDS)
    bs.assert_awaited_once()
    assert [c["Namespace"] for c in cw.calls] == ["AA/RDS", "AA/Jobs/CIS"]
    assert len(cw.calls[0]["MetricData"]) == 6 and len(cw.calls[1]["MetricData"]) == 2
    assert out["rds_published"] == 6 and out["jobs_published"] == 2
    assert out["roles"]["booking"] == {"connections": 12, "limit": 15, "pct": 80.0}
    assert out["oldest_queued_age_seconds"] == 0.0 and out["overdue_running_jobs"] == 0
    assert "rds_error" not in out and "jobs_error" not in out


@pytest.mark.asyncio
async def test_rds_failure_does_not_block_jobs():
    cw = StubCloudWatch()
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        out = await sampler.sample_once(_pool(error=RuntimeError("pool closing")), cw, kinds=KINDS)
    assert "pool closing" in out["rds_error"]
    assert [c["Namespace"] for c in cw.calls] == ["AA/Jobs/CIS"]


@pytest.mark.asyncio
async def test_jobs_put_denied_is_reported_rds_still_published():
    """Until AA-CIS-Infra adds AA/Jobs/CIS to the task role, that put is AccessDenied: logged only."""
    cw = StubCloudWatch(fail_on={"AA/Jobs/CIS"})
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        out = await sampler.sample_once(_pool(), cw, kinds=KINDS)
    assert out["rds_published"] == 6 and "AccessDenied" in out["jobs_error"]
    assert "jobs_published" not in out


@pytest.mark.asyncio
async def test_backlog_failure_publishes_no_jobs_datapoint():
    cw = StubCloudWatch()
    with patch.object(backlog, "sample", AsyncMock(side_effect=RuntimeError("timeout"))):
        out = await sampler.sample_once(_pool(), cw, kinds=KINDS)
    assert "timeout" in out["jobs_error"] and [c["Namespace"] for c in cw.calls] == ["AA/RDS"]


@pytest.mark.asyncio
async def test_no_limited_roles_skips_the_rds_put():
    cw = StubCloudWatch()
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        out = await sampler.sample_once(_pool(rows=[]), cw, kinds=KINDS)
    assert out["roles"] == {} and out["rds_published"] == 0
    assert [c["Namespace"] for c in cw.calls] == ["AA/Jobs/CIS"]


@pytest.mark.asyncio
async def test_sample_once_loads_the_registry_when_not_given():
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)) as bs:
        await sampler.sample_once(_pool(), StubCloudWatch())
    kinds = bs.call_args[0][1]
    assert "a3_atomize" in kinds and "ops_rds_role_connections" not in kinds


# ── per-part timeout (a hung acquire/query never stalls the loop) ──────────────────────────────

@pytest.mark.asyncio
async def test_hung_rds_query_times_out_jobs_still_published():
    pool = _pool()
    pool.fetch = AsyncMock(side_effect=_hang)
    cw = StubCloudWatch()
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        out = await asyncio.wait_for(sampler.sample_once(pool, cw, kinds=KINDS, part_timeout_s=0.05), 2)
    assert out["rds_error"].startswith("TimeoutError")
    assert [c["Namespace"] for c in cw.calls] == ["AA/Jobs/CIS"] and out["jobs_published"] == 2


@pytest.mark.asyncio
async def test_hung_backlog_sample_times_out_rds_still_published():
    cw = StubCloudWatch()
    with patch.object(backlog, "sample", AsyncMock(side_effect=_hang)):
        out = await asyncio.wait_for(sampler.sample_once(_pool(), cw, kinds=KINDS, part_timeout_s=0.05), 2)
    assert out["jobs_error"].startswith("TimeoutError") and out["rds_published"] == 6
    assert [c["Namespace"] for c in cw.calls] == ["AA/RDS"]


@pytest.mark.asyncio
async def test_loop_keeps_ticking_with_a_pool_that_never_answers():
    pool = _pool()
    pool.fetch = AsyncMock(side_effect=_hang)
    pool.execute = AsyncMock(side_effect=_hang)
    app = SimpleNamespace(state=SimpleNamespace(pool=pool))
    with capture_logs() as logs, patch.object(backlog, "sample", AsyncMock(side_effect=_hang)):
        task = asyncio.create_task(sampler.run_ops_metrics_sampler(
            app, 0.01, 0, client_factory=StubCloudWatch, part_timeout_s=0.02))
        await _wait_for(logs, "ops_metrics_sampled", count=2)
        await sampler.stop_ops_metrics_sampler(task)
    lines = [e for e in logs if e["event"] == "ops_metrics_sampled"][:2]
    assert all(e["log_level"] == "warning" for e in lines)
    assert all(e["rds_error"].startswith("TimeoutError") and e["retired_error"].startswith("TimeoutError")
               and e["jobs_error"].startswith("TimeoutError") for e in lines)


def test_part_timeout_bounds_a_tick_below_the_interval():
    assert 3 * sampler.PART_TIMEOUT_S < sampler.OPS_METRICS_INTERVAL_S


# ── retired kinds (rolling-deploy leftovers) ───────────────────────────────────────────────────

def test_ops_rds_role_connections_is_retired():
    assert registry.RETIRED_KINDS == {"ops_rds_role_connections": "replaced by the ops metrics sampler"}


@pytest.mark.asyncio
async def test_retired_rows_are_cancelled_before_the_backlog_sample():
    pool = _pool(execute="UPDATE 2")
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        out = await sampler.sample_once(pool, StubCloudWatch(), kinds=KINDS)
    pool.execute.assert_awaited_once()
    sql, kinds_arg, reasons = pool.execute.call_args[0]
    assert "status IN ('queued', 'running')" in sql and "locked_by = NULL" in sql
    assert kinds_arg == ["ops_rds_role_connections"] and '"replaced by the ops metrics sampler"' in reasons
    assert out["retired_cancelled"] == 2 and "retired_error" not in out


@pytest.mark.asyncio
async def test_nothing_to_cancel_adds_no_field():
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        out = await sampler.sample_once(_pool(), StubCloudWatch(), kinds=KINDS)
    assert "retired_cancelled" not in out


@pytest.mark.asyncio
async def test_a_registered_kind_is_never_swept():
    pool = _pool()
    kinds = dict(KINDS, ops_rds_role_connections=K())
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        await sampler.sample_once(pool, StubCloudWatch(), kinds=kinds)
    pool.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_sweep_failure_does_not_block_jobs():
    pool = _pool()
    pool.execute = AsyncMock(side_effect=RuntimeError("permission denied"))
    cw = StubCloudWatch()
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        out = await sampler.sample_once(pool, cw, kinds=KINDS)
    assert "permission denied" in out["retired_error"] and out["jobs_published"] == 2


@pytest.mark.asyncio
async def test_tripplanner_purge_sweep_runs_each_tick_and_its_failure_blocks_nothing():
    """AA-987: one INSERT ... SELECT per tick; jobs created are reported, an error is its own field."""
    pool = _pool()
    pool.fetchval = AsyncMock(return_value=2)
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        out = await sampler.sample_once(pool, StubCloudWatch(), kinds=KINDS)
    pool.fetchval.assert_not_awaited()                    # kind not registered: no row a worker cannot run
    with_kind = dict(KINDS, tripplanner_purge=K())
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        out = await sampler.sample_once(pool, StubCloudWatch(), kinds=with_kind)
    assert "tripplanner_purge" in pool.fetchval.call_args[0][0] and out["tripplanner_purge_swept"] == 2
    pool.fetchval = AsyncMock(side_effect=RuntimeError("permission denied"))
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        out = await sampler.sample_once(pool, StubCloudWatch(), kinds=with_kind)
    assert "permission denied" in out["tripplanner_purge_error"] and out["jobs_published"] == 2


@pytest.mark.asyncio
async def test_supply_pull_runs_only_with_its_kind_and_its_failure_blocks_nothing(monkeypatch):
    """AA-835: one feed batch per tick while supply_content_draft is registered; an error is its own field."""
    monkeypatch.delenv("SUPPLY_FEED_PULL", raising=False)
    pool = _pool()
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        out = await sampler.sample_once(pool, StubCloudWatch(), kinds=KINDS)
    pool.acquire.assert_not_called()                      # kind not registered
    with_kind = dict(KINDS, supply_content_draft=K(background=True))
    pool.feed_conn = _feed_conn(cursor=5)
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        out = await sampler.sample_once(pool, StubCloudWatch(), kinds=with_kind)
    assert "booking.v_supply_outbox" in pool.feed_conn.fetch.call_args[0][0]
    assert "supply_pull_applied" not in out and "supply_pull_error" not in out   # no events: no field
    pool.feed_conn.fetch = AsyncMock(side_effect=RuntimeError("relation booking.v_supply_outbox does not exist"))
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        out = await sampler.sample_once(pool, StubCloudWatch(), kinds=with_kind)
    assert "v_supply_outbox" in out["supply_pull_error"] and out["jobs_published"] == 2
    assert "shared.supply_feed_cursor" in pool.feed_conn.execute.call_args[0][0]     # last_error recorded
    monkeypatch.setenv("SUPPLY_FEED_PULL", "off")
    pool = _pool()
    with patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        await sampler.sample_once(pool, StubCloudWatch(), kinds=with_kind)
    pool.acquire.assert_not_called()


# ── the loop ────────────────────────────────────────────────────────────────────────────────────

async def _wait_for(logs, event, count=1, timeout=2.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while sum(1 for entry in logs if entry["event"] == event) < count:
        assert loop.time() < deadline, f"no {count} x {event} within {timeout} s"
        await asyncio.sleep(0.005)


def test_defaults():
    assert sampler.OPS_METRICS_INTERVAL_S == 300.0


@pytest.mark.asyncio
async def test_loop_ticks_logs_and_cancels():
    app = SimpleNamespace(state=SimpleNamespace(pool=_pool()))
    cw = StubCloudWatch()
    with capture_logs() as logs, patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        task = asyncio.create_task(sampler.run_ops_metrics_sampler(app, 0.01, 0, client_factory=lambda: cw))
        await _wait_for(logs, "ops_metrics_sampled", count=2)
        await sampler.stop_ops_metrics_sampler(task)
    assert task.cancelled()
    line = next(e for e in logs if e["event"] == "ops_metrics_sampled")
    assert line["log_level"] == "info" and line["oldest_queued_age_seconds"] == 0.0
    assert line["roles"]["aa_app_user"]["connections"] == 3


@pytest.mark.asyncio
async def test_loop_survives_client_factory_failure_and_retries():
    app = SimpleNamespace(state=SimpleNamespace(pool=_pool()))
    cw = StubCloudWatch()
    factory = MagicMock(side_effect=[RuntimeError("no credentials"), cw])
    with capture_logs() as logs, patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        task = asyncio.create_task(sampler.run_ops_metrics_sampler(app, 0.01, 0, client_factory=factory))
        await _wait_for(logs, "ops_metrics_sampled", count=2)
        await sampler.stop_ops_metrics_sampler(task)
    first, second = [e for e in logs if e["event"] == "ops_metrics_sampled"][:2]
    assert first["log_level"] == "warning" and "no credentials" in first["error"]
    assert second["log_level"] == "info" and cw.calls


@pytest.mark.asyncio
async def test_part_errors_log_a_warning_line():
    app = SimpleNamespace(state=SimpleNamespace(pool=_pool()))
    cw = StubCloudWatch(fail_on={"AA/RDS", "AA/Jobs/CIS"})
    with capture_logs() as logs, patch.object(backlog, "sample", AsyncMock(return_value=_SAMPLE)):
        task = asyncio.create_task(sampler.run_ops_metrics_sampler(app, 0.01, 0, client_factory=lambda: cw))
        await _wait_for(logs, "ops_metrics_sampled")
        await sampler.stop_ops_metrics_sampler(task)
    line = next(e for e in logs if e["event"] == "ops_metrics_sampled")
    assert line["log_level"] == "warning" and "rds_error" in line and "jobs_error" in line


@pytest.mark.asyncio
async def test_no_pool_publishes_nothing():
    app = SimpleNamespace(state=SimpleNamespace())
    factory = MagicMock()
    with capture_logs() as logs:
        task = asyncio.create_task(sampler.run_ops_metrics_sampler(app, 0.01, 0, client_factory=factory))
        await asyncio.sleep(0.05)
        await sampler.stop_ops_metrics_sampler(task)
    assert not logs and not factory.called


@pytest.mark.asyncio
async def test_first_tick_waits_first_delay():
    app = SimpleNamespace(state=SimpleNamespace(pool=_pool()))
    factory = MagicMock()
    task = asyncio.create_task(sampler.run_ops_metrics_sampler(app, 0.01, 60, client_factory=factory))
    await asyncio.sleep(0.03)
    await sampler.stop_ops_metrics_sampler(task)
    assert not factory.called


@pytest.mark.asyncio
async def test_off_in_test_environment(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "test")
    assert sampler.start_ops_metrics_sampler(SimpleNamespace(state=SimpleNamespace())) is None
    monkeypatch.setenv("ENVIRONMENT", "dev")
    task = sampler.start_ops_metrics_sampler(SimpleNamespace(state=SimpleNamespace()))
    assert task is not None and task.get_name() == "aa751-ops-metrics-sampler"
    await sampler.stop_ops_metrics_sampler(task)
    await sampler.stop_ops_metrics_sampler(None)


@pytest.mark.asyncio
async def test_api_lifespan_starts_the_sampler_and_cancels_it_before_the_pool_closes(monkeypatch):
    from api import main

    monkeypatch.setenv("ENVIRONMENT", "dev")
    pool = _pool()
    order = []
    pool.close = AsyncMock(side_effect=lambda: order.append("pool.close"))
    redis = MagicMock()
    redis.aclose = AsyncMock()
    real_stop = sampler.stop_ops_metrics_sampler

    async def stop(task):
        order.append("sampler.stop")
        await real_stop(task)

    def sampler_tasks():
        return [t for t in asyncio.all_tasks() if t.get_name() == "aa751-ops-metrics-sampler"]

    with patch.object(main.asyncpg, "create_pool", AsyncMock(return_value=pool)), \
            patch.object(main.aioredis, "from_url", return_value=redis), \
            patch("api.core.aa544_tenant_pool.create_tenant_pool", AsyncMock(return_value=None)), \
            patch("shared.jobs.worker.in_api_enabled", return_value=False), \
            patch.object(sampler, "stop_ops_metrics_sampler", stop), \
            patch("api.core.identity_config.start_cis_staff_jwks_prefetch", return_value=None), \
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://x:y@localhost/z"}):
        app = SimpleNamespace(state=SimpleNamespace())
        async with main.lifespan(app):
            running = sampler_tasks()
            assert len(running) == 1 and not running[0].done()
        assert running[0].cancelled()
    assert order == ["sampler.stop", "pool.close"]
    assert not sampler_tasks()


@pytest.mark.asyncio
async def test_api_lifespan_test_environment_starts_no_sampler(monkeypatch):
    from api import main

    monkeypatch.setenv("ENVIRONMENT", "test")
    pool = _pool()
    pool.close = AsyncMock()
    redis = MagicMock()
    redis.aclose = AsyncMock()
    with patch.object(main.asyncpg, "create_pool", AsyncMock(return_value=pool)), \
            patch.object(main.aioredis, "from_url", return_value=redis), \
            patch("api.core.aa544_tenant_pool.create_tenant_pool", AsyncMock(return_value=None)), \
            patch("shared.jobs.worker.in_api_enabled", return_value=False), \
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://x:y@localhost/z"}):
        async with main.lifespan(SimpleNamespace(state=SimpleNamespace())):
            assert not [t for t in asyncio.all_tasks() if t.get_name() == "aa751-ops-metrics-sampler"]


# ── the AA-832 chain is gone; the deploy_gate_exempt mechanism stays ────────────────────────────

def test_schedule_endpoint_removed():
    from api.routers.admin_job_runner import router

    paths = {r.path for r in router.routes}
    assert "/admin/job-runner/rds-role-connections/schedule" not in paths
    assert "/admin/job-runner/retention/schedule" in paths


def test_job_kind_removed_and_no_kind_is_exempt():
    worker.load_kinds()
    assert "ops_rds_role_connections" not in registry.kinds()
    # AA-1018: the daily LLM probe may sit queued across a deploy (a re-run is harmless), so it is left out
    assert [k.name for k in registry.kinds().values() if k.deploy_gate_exempt] == ["llm_connection_probe"]


@pytest.fixture
def exempt_test_kind():
    name = "aa751_test_exempt"

    @registry.job_kind(name, deploy_gate_exempt=True)
    async def _handler(ctx):
        return None

    yield name
    registry._KINDS.pop(name, None)


@pytest.mark.asyncio
async def test_deploy_gate_still_excludes_an_exempt_kind(monkeypatch, exempt_test_kind):
    from api.routers import admin_job_runner

    monkeypatch.setattr("api.routers.admin.ADMIN_SECRET", _TEST_SECRET)
    pool = MagicMock()
    pool.fetch = AsyncMock(return_value=[])
    request = MagicMock()
    request.app.state.pool = pool
    out = await admin_job_runner.deploy_gate(request, x_admin_secret=_TEST_SECRET)
    expected = sorted([exempt_test_kind, "llm_connection_probe"])          # AA-1018: + the daily LLM probe
    assert out["safe_to_deploy"] is True and sorted(out["exempt_kinds"]) == expected
    # AA-990: + the scheduled-later horizon. AA-993 G5: the first fetch is active_count, the second deploy_blockers.
    sql, exempt, *rest = pool.fetch.call_args_list[0][0]
    assert "NOT (kind = ANY($1::text[]))" in sql and sorted(exempt) == expected
    assert rest == [queue.DEPLOY_GATE_HORIZON_S]
