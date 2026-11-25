"""AA-1142 follow-up (S293) — the external checks run by the CIS worker (job kind ops_external_check) and the two
sources on the status page: the probe (DNS, TLS, health GET; a hung target never holds the run), the job's bucket,
schedule and late skip, the per-source strips, the feed stale only when every source in use is stale, one
Needs-attention item per target naming the fresh sources that see it failing. No network, no DB; invented hosts."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services.ops_status import external, external_probe
from api.services.ops_status.ranges import TimeRange
from services.jobs import ops_external_check_job as job
from shared import ops_external_settings as xs

NOW = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)
RNG = TimeRange("6h", NOW - timedelta(hours=6), NOW, 300)
LIMITS = {"warn": 21.0, "crit": 7.0}
HEALTH = {"id": "zz-api", "app": "cis", "host": "api.zz-example.test", "mode": "health", "path": "/health",
          "expect": [200]}
TLS = {"id": "zz-auth", "app": "shared", "host": "auth.zz-example.test", "mode": "tls"}
END = datetime(2027, 1, 20, tzinfo=timezone.utc)


def _dns_ok(host, timeout):
    return True, None


def _tls_ok(host, timeout):
    return True, END, None


def _get(status):
    return lambda host, path, timeout: (status, 120, None)


# ── probe ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_healthy_target_has_every_step():
    r = external_probe.check_one(HEALTH, 5.0, dns=_dns_ok, tls=_tls_ok, get=_get(200))
    assert (r["dns_ok"], r["tls_ok"], r["http_status"], r["http_ok"], r["cert_not_after"]) == (True, True, 200, True,
                                                                                             END)
    assert external.state_of(r, NOW, LIMITS) == "ok"


def test_a_failed_step_leaves_the_later_ones_unchecked():
    r = external_probe.check_one(HEALTH, 5.0, dns=lambda h, t: (False, "gaierror: zz"), tls=_tls_ok, get=_get(200))
    assert r["tls_ok"] is None and r["http_status"] is None and r["error"] == "gaierror: zz"
    assert external.state_of(r, NOW, LIMITS) == "down"
    r = external_probe.check_one(HEALTH, 5.0, dns=_dns_ok, tls=lambda h, t: (False, None, "SSLError: zz"),
                                 get=_get(200))
    assert r["http_status"] is None and external.state_of(r, NOW, LIMITS) == "down"
    r = external_probe.check_one(HEALTH, 5.0, dns=_dns_ok, tls=_tls_ok, get=_get(502))
    assert r["http_ok"] is False and external.state_of(r, NOW, LIMITS) == "degraded"


def test_a_tls_target_makes_no_request():
    get = MagicMock()
    r = external_probe.check_one(TLS, 5.0, dns=_dns_ok, tls=_tls_ok, get=get)
    assert get.call_count == 0 and r["http_status"] is None and external.state_of(r, NOW, LIMITS) == "ok"


def test_the_certificate_end_date_is_read():
    assert external_probe.cert_end({"notAfter": "Jan 20 00:00:00 2027 GMT"}) == END
    assert external_probe.cert_end({}) is None and external_probe.cert_end(None) is None


@pytest.mark.asyncio
async def test_a_hung_target_never_holds_the_run():
    def slow_dns(host, timeout):
        import time

        time.sleep(0.5)
        return True, None

    fast = {**HEALTH, "id": "zz-fast"}
    out = await external_probe.run([{**HEALTH, "id": "zz-slow"}], 0.05, dns=slow_dns, tls=_tls_ok, get=_get(200))
    assert out[0]["dns_ok"] is None and out[0]["error"].startswith("TimeoutError")
    assert external.state_of(out[0], NOW, LIMITS) == "down"      # a customer would meet the same hang
    out = await external_probe.run([fast, TLS], 1.0, dns=_dns_ok, tls=_tls_ok, get=_get(200))
    assert [r["target"] for r in out] == ["zz-fast", "zz-auth"]


# ── job ─────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("minute, minutes, start", [
    (0, 15, 0), (14, 15, 0), (15, 15, 15), (59, 15, 45), (59, 7, 56), (3, 5, 0), (44, 60, 0),
])
def test_buckets_align_to_the_hour(minute, minutes, start):
    t = NOW.replace(minute=minute, second=31)
    assert job.bucket_start(t, minutes) == NOW.replace(minute=start)
    assert job.bucket_key(job.bucket_start(t, minutes)) == f"ops_external_check:2026-11-25T10:{start:02d}"


@pytest.mark.asyncio
async def test_tick_queues_one_run_per_bucket_only_with_the_worker_source(monkeypatch):
    monkeypatch.setattr(xs, "interval_min", lambda: 15)
    enq = AsyncMock(return_value=("job-1", True))
    monkeypatch.setattr(job, "enqueue", enq)
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)
    kinds = {job.KIND: object()}
    monkeypatch.setattr(xs, "sources", lambda: ("worker", "github"))
    out: dict = {}
    await job.tick_part(pool, kinds, NOW.replace(minute=17), out)
    assert out[job.KIND]["key"] == "ops_external_check:2026-11-25T10:15"
    args, kw = enq.call_args
    assert args[1] == job.KIND and args[2] == {"trigger": "schedule", "bucket": "2026-11-25T10:15:00+00:00"}
    assert kw["max_attempts"] == 1 and kw["idempotency_key"] == out[job.KIND]["key"]
    pool.fetchval = AsyncMock(return_value=True)      # the bucket's run exists
    out = {}
    await job.tick_part(pool, kinds, NOW.replace(minute=17), out)
    assert out == {} and enq.call_count == 1
    monkeypatch.setattr(xs, "sources", lambda: ("github",))
    await job.tick_part(pool, kinds, NOW, out)
    await job.tick_part(pool, {}, NOW, out)
    assert out == {} and enq.call_count == 1


def test_the_kind_is_a_monitor_exempt_from_the_deploy_gate():
    from shared.jobs.registry import kinds
    from shared.jobs.worker import load_kinds

    load_kinds()
    k = kinds()[job.KIND]
    assert k.monitor and k.background and k.deploy_gate_exempt and not k.deploy_blocking and k.max_attempts == 1


class _Ctx:
    def __init__(self, payload):
        self.payload, self.pool, self.job_id, self.result = payload, MagicMock(), "job-zz", None

    def set_result(self, r):
        self.result = r


@pytest.mark.asyncio
async def test_a_late_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(xs, "interval_min", lambda: 15)
    store = AsyncMock()
    monkeypatch.setattr(external, "store_run", store)
    ctx = _Ctx({"trigger": "schedule", "bucket": (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()})
    out = await job.run(ctx)
    assert out["status"] == "skipped_late" and store.call_count == 0


@pytest.mark.asyncio
async def test_a_run_checks_the_valid_targets_and_stores_them_as_worker(monkeypatch):
    monkeypatch.setattr(xs, "targets", lambda: ([HEALTH, TLS], [{"id": "zz-bad", "problem": "bad host"}]))
    monkeypatch.setattr(xs, "timeout_s", lambda: 4.0)
    seen: dict = {}

    async def fake_run(targets, timeout):
        seen["targets"], seen["timeout"] = targets, timeout
        return [external_probe.check_one(t, timeout, dns=_dns_ok, tls=_tls_ok, get=_get(503)) for t in targets]

    store = AsyncMock(return_value=2)
    monkeypatch.setattr(external_probe, "run", fake_run)
    monkeypatch.setattr(external, "store_run", store)
    ctx = _Ctx({"trigger": "manual"})
    out = await job.run(ctx)
    assert seen == {"targets": [HEALTH, TLS], "timeout": 4.0}
    assert store.call_args[0][1] == "worker" and len(store.call_args[0][3]) == 2
    assert store.call_args[0][2].tzinfo is not None   # the run's time, kept at full precision (not whole seconds)
    assert out["status"] == "checked" and out["states"] == {"degraded": 1, "ok": 1}
    assert out["skipped"] == [{"id": "zz-bad", "problem": "bad host"}] and ctx.result == out


# ── two sources on the page ─────────────────────────────────────────────────────────────────────────────────────

def _row(target, source, state, at, days=40.0):
    return {"checked_at": at, "source": source, "target": target, "app": "cis", "host": f"{target}.zz-example.test",
            "state": state, "dns_ok": state != "down", "tls_ok": True,
            "http_status": None if state == "down" else 200, "latency_ms": 90, "error": None,
            "cert_not_after": at + timedelta(days=days)}


WINDOW = timedelta(minutes=45)   # 3 x 15 minutes: a row checked within it is not stale

def test_each_source_is_stale_on_its_own_interval():
    latest = [_row("zz-api", "worker", "ok", NOW - timedelta(minutes=20)),
              _row("zz-api", "github", "ok", NOW - timedelta(hours=9))]
    srcs = external.source_summary(latest, {"worker": 15, "github": 15}, NOW, 3)
    assert [(s["source"], s["stale"]) for s in srcs] == [("worker", False), ("github", True)]
    srcs = external.source_summary(latest, {"worker": 5, "github": 15}, NOW, 3)
    assert [s["stale"] for s in srcs] == [True, True]
    assert external.source_summary([], {"worker": 15}, NOW, 3) == [
        {"source": "worker", "newest": None, "interval_minutes": 15, "stale": True}]
    assert external.stale_after(3, 5) == timedelta(minutes=15)


def _pool(in_range, latest):
    pool = MagicMock()
    pool.fetch = AsyncMock(side_effect=lambda sql, *a: in_range if "DISTINCT ON" not in sql else latest)
    return pool


@pytest.mark.asyncio
async def test_the_feed_is_stale_only_when_every_source_is(monkeypatch):
    from shared import ops_status_thresholds as th

    monkeypatch.setattr(xs, "sources", lambda: ("worker", "github"))
    monkeypatch.setattr(xs, "interval_min", lambda: 15)
    monkeypatch.setattr(th, "stale_factor", lambda: 3.0)
    late_github = [_row("zz-api", "github", "down", NOW - timedelta(hours=9))]
    fresh_worker = [_row("zz-api", "worker", "ok", NOW - timedelta(minutes=10))]
    sec = await external.collect(_pool(late_github + fresh_worker, fresh_worker + late_github), RNG, NOW)
    d = sec["data"]
    assert sec["status"] == "ok" and d["stale"] is False and d["schedule_minutes"] == 15
    assert [(r["source"], r["stale"]) for r in d["rows"]] == [("worker", False), ("github", True)]
    assert external.items(d) == []        # only the stale GitHub source saw it down
    sec = await external.collect(_pool(late_github, late_github), RNG, NOW)
    assert sec["data"]["stale"] is True   # the worker never reported, GitHub is late
    assert [i[0] for i in external.items(sec["data"])] == ["external-feed"]
    monkeypatch.setattr(xs, "sources", lambda: ("worker",))
    pool = _pool([], fresh_worker)
    await external.collect(pool, RNG, NOW)
    assert all(c.args[-1] == ["worker"] for c in pool.fetch.call_args_list)   # a source left out is not read
    monkeypatch.setattr(xs, "sources", lambda: ())
    assert (await external.collect(pool, RNG, NOW))["reason"] == "external_checks_off"


def test_one_item_per_target_naming_the_fresh_sources_that_see_it_fail():
    at = NOW - timedelta(minutes=5)
    rows = external.target_rows([], [_row("zz-web", "worker", "down", at), _row("zz-web", "github", "down", at),
                                     _row("zz-api", "worker", "ok", at), _row("zz-api", "github", "down", at),
                                     _row("zz-auth", "worker", "ok", at, days=3.0),
                                     _row("zz-auth", "github", "ok", at, days=3.0)],
                                RNG, NOW, LIMITS, 99.5, {"worker": WINDOW, "github": WINDOW})
    got = {i[0]: i for i in external.items({"rows": rows, "stale": False})}
    assert set(got) == {"external-zz-web", "external-zz-api", "external-cert-zz-auth"}
    assert got["external-zz-web"][3]["sources"] == ["worker", "github"]
    assert got["external-zz-api"][3]["sources"] == ["github"]
    v = {i["id"]: i for i in external.verdict_items({"rows": rows, "stale": False})}
    assert v["external-zz-web"]["msg"] == {"key": "external_fails", "app": "cis", "host": "zz-web.zz-example.test",
                                           "what": "DNS", "code": "dns", "status": None,
                                           "sources": ["worker", "github"]}



@pytest.mark.asyncio
async def test_a_target_no_longer_checked_goes_grey_while_its_source_reports_the_others(monkeypatch):
    """Review S293: staleness is judged per row. A host retired and removed from the worker's list keeps its last
    'down' result in the table for the history days; while the worker keeps checking the other targets, that old row
    is stale (grey, "stopped reporting") and raises no SEV2 item, nor does an old critical certificate on it."""
    from shared import ops_status_thresholds as th

    monkeypatch.setattr(xs, "sources", lambda: ("worker",))
    monkeypatch.setattr(xs, "interval_min", lambda: 15)
    monkeypatch.setattr(th, "stale_factor", lambda: 3.0)
    retired = _row("zz-old", "worker", "down", NOW - timedelta(days=2))
    old_cert = _row("zz-gone", "worker", "ok", NOW - timedelta(hours=1), days=1.0)
    live = _row("zz-api", "worker", "ok", NOW - timedelta(minutes=10))
    sec = await external.collect(_pool([live], [retired, old_cert, live]), RNG, NOW)
    d = sec["data"]
    assert d["stale"] is False and [s["stale"] for s in d["sources"]] == [False]
    by = {r["target"]: r for r in d["rows"]}
    assert by["zz-old"]["stale"] is True and by["zz-gone"]["stale"] is True and by["zz-api"]["stale"] is False
    assert external.items(d) == []
    late = _row("zz-api", "worker", "down", NOW - timedelta(minutes=44))     # inside 3 x 15 minutes: still live
    d = (await external.collect(_pool([], [retired, late]), RNG, NOW))["data"]
    assert [i[0] for i in external.items(d)] == ["external-zz-api"]

@pytest.mark.parametrize("r, code", [
    ({"dns_ok": False}, ("dns", None)), ({"dns_ok": True, "tls_ok": False}, ("tls", None)),
    ({"dns_ok": True, "tls_ok": True, "http_status": 503}, ("http", 503)),
    ({"dns_ok": True, "tls_ok": True, "http_status": None}, ("request", None)),
])
def test_what_failed(r, code):
    assert external.what_failed(r) == code


@pytest.mark.asyncio
async def test_the_github_copy_stops_when_github_is_not_a_source(monkeypatch):
    monkeypatch.setattr(xs, "sources", lambda: ("worker",))
    loader = MagicMock()
    out: dict = {}
    await external.ingest(MagicMock(), out, loader=loader)
    assert out == {"external": "github_off"} and loader.call_count == 0


def test_the_ops_metrics_sampler_queues_the_worker_checks():
    import inspect

    from api.core import ops_metrics_sampler

    src = inspect.getsource(ops_metrics_sampler.sample_once)
    assert "ops_external_check_job as _x" in src
    assert '_x.tick_part(pool, kinds, ts, out), out, "ops_external_check_error"' in src


def test_every_external_and_burn_item_has_a_message_the_page_can_word():
    """S293: the external check and error budget items carry a structured message (title and detail) the page words
    in English or Vietnamese; every key is in lib/opsStatusText.ts MSG_KEYS (test_aa1140_status_text reads it)."""
    import re
    from pathlib import Path

    from api.services.ops_status import slo

    rules = {"target_pct": 99.5, "window_days": 30, "fast": {"window_min": 60, "factor": 14.4},
             "slow": {"window_hours": 6, "factor": 6.0}, "min_requests": 50}

    src = (Path(__file__).resolve().parents[2] / "frontend" / "lib" / "opsStatusText.ts").read_text(encoding="utf-8")
    known = set(re.findall(r'"([a-z0-9_]+)"', re.search(r"export const MSG_KEYS = \[(.*?)\] as const;", src,
                                                         re.S).group(1)))
    at = NOW - timedelta(minutes=5)
    rows = external.target_rows([], [_row("zz-web", "worker", "down", at), _row("zz-auth", "worker", "ok", at,
                                                                                 days=3.0)],
                                RNG, NOW, LIMITS, 99.5, {"worker": WINDOW})
    items = (external.verdict_items({"rows": rows, "stale": False})
             + external.verdict_items({"rows": rows, "stale": True, "newest": at.isoformat()}))
    burn = {"app": "cis", "label": "CIS", "basis": "requests", "target_pct": 99.5, "budget_left_pct": 12.5,
            "fast": {"burn": 20.0, "events": 1000, "bad": 100, "factor": 14.4, "state": "alert"},
            "slow": {"burn": 1.0, "events": 5000, "bad": 25, "factor": 6.0, "state": "ok"}, "measured": True}
    items += slo.verdict_items({"rules": rules, "rows": [burn]})
    keys = {i["msg"]["key"] for i in items} | {i["detail_msg"]["key"] for i in items if i["detail_msg"]}
    assert keys == {"external_fails", "external_cert", "external_stale", "external_stale_detail", "slo_burn",
                    "slo_burn_detail"}
    assert keys <= known, keys - known
    fast = next(i for i in items if i["msg"]["key"] == "slo_burn")
    assert fast["msg"]["minutes"] == 60
    assert fast["detail_msg"] == {"key": "slo_burn_detail", "bad": 100, "events": 1000, "basis": "requests",
                                  "target": 99.5, "budget_left": 12.5}
