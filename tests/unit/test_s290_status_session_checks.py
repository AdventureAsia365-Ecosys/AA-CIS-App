"""S290 follow-up of AA-1140: the refused staff session checks (401 of /auth/verify-admin) are counted apart and kept
out of the CIS client error rate of the Ecosystem status page; every other 4xx stays in it. No network, no AWS, no DB.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import api.main as main_mod
from api.main import app, get_pool
from api.services import ops_status
from api.services.ops_overview import cache
from api.services.ops_status import cw_series, ranges, sections, session_checks
from api.services.ops_status.ranges import TimeRange
from shared import ops_status_thresholds as th

NOW = datetime(2026, 11, 24, 2, 7, 30, tzinfo=timezone.utc)


class FakeRedis:
    """INCR / EXPIRE through a pipeline and MGET, like the api's shared client; ``fail`` makes every call raise."""

    def __init__(self, fail: bool = False) -> None:
        self.data: dict[str, int] = {}
        self.ttl: dict[str, int] = {}
        self.fail = fail
        self.mget_calls = 0

    def pipeline(self, transaction: bool = True) -> "FakeRedis._Pipe":
        return FakeRedis._Pipe(self)

    class _Pipe:
        def __init__(self, r: "FakeRedis") -> None:
            self.r, self.ops = r, []

        def incr(self, key: str) -> None:
            self.ops.append(("incr", key, None))

        def expire(self, key: str, ttl: int) -> None:
            self.ops.append(("expire", key, ttl))

        async def execute(self) -> list:
            if self.r.fail:
                raise ConnectionError("redis down")
            for op, key, ttl in self.ops:
                if op == "incr":
                    self.r.data[key] = self.r.data.get(key, 0) + 1
                else:
                    self.r.ttl[key] = ttl
            return []

    async def mget(self, keys: list[str]) -> list:
        if self.fail:
            raise ConnectionError("redis down")
        self.mget_calls += 1
        return [None if k not in self.data else str(self.data[k]) for k in keys]


def _sig(client_errors: int | None = 40, requests: int = 1000) -> dict:
    return {"status": "ok", "as_of": NOW.isoformat(), "apps": {
        "cis": {"measured": True, "totals": {"requests": requests, "errors": 1, "client_errors": client_errors,
                                             "p95_ms": 120.0}},
        "booking": {"measured": True, "totals": {"requests": 50, "errors": 0, "client_errors": 9, "p95_ms": 90.0}},
    }}


# ── buckets, record, count ──────────────────────────────────────────────────────────────────────────────────────

def test_buckets_lie_on_the_five_minute_grid_inside_the_range():
    start, end = NOW - timedelta(minutes=20), NOW
    got = session_checks.buckets(start, end)
    assert all(b % session_checks.BUCKET_S == 0 for b in got)
    assert got[0] >= int(start.timestamp()) and got[-1] < int(end.timestamp())
    assert len(got) == 4
    assert session_checks.buckets(end, end) == []


def test_ttl_follows_the_history_setting():
    assert session_checks.ttl_s() == (th.history_days() + 1) * 86400


@pytest.mark.asyncio
async def test_record_then_count_over_the_range():
    r = FakeRedis()
    for minutes in (1, 2, 9, 30):
        await session_checks.record(r, NOW - timedelta(minutes=minutes))
    key = session_checks.key_of(session_checks.bucket_of(NOW - timedelta(minutes=1)))
    assert r.ttl[key] == session_checks.ttl_s()
    start = datetime.fromtimestamp(session_checks.bucket_of(NOW - timedelta(minutes=10)), tz=timezone.utc)
    assert await session_checks.count(r, start, NOW + timedelta(minutes=5)) == 3  # the one 30 minutes back is out
    assert await session_checks.count(r, NOW - timedelta(days=1), NOW + timedelta(minutes=5)) == 4


@pytest.mark.asyncio
async def test_a_long_range_is_read_in_chunks():
    r = FakeRedis()
    await session_checks.record(r, NOW - timedelta(days=29))
    assert await session_checks.count(r, NOW - timedelta(days=30), NOW) == 1
    assert r.mget_calls == -(-(30 * 288) // session_checks.MGET_CHUNK)


@pytest.mark.asyncio
async def test_redis_missing_or_failing_never_raises():
    await session_checks.record(None)
    await session_checks.record(FakeRedis(fail=True))
    assert await session_checks.count(None, NOW - timedelta(hours=1), NOW) is None
    assert await session_checks.count(FakeRedis(fail=True), NOW - timedelta(hours=1), NOW) is None


# ── the tile ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_apply_takes_only_the_session_checks_out_of_the_cis_client_errors():
    out = session_checks.apply(_sig(40), 30)
    cis = out["apps"]["cis"]["totals"]
    assert (cis["client_errors"], cis["session_checks"]) == (10, 30)
    assert out["apps"]["booking"]["totals"]["client_errors"] == 9 and "session_checks" not in out["apps"]["booking"][
        "totals"]
    assert session_checks.apply(_sig(5), 30)["apps"]["cis"]["totals"]["client_errors"] == 0  # never below zero
    sig = _sig(40)
    assert session_checks.apply(sig, None) is sig  # no count: CloudWatch's number as is
    assert session_checks.apply(_sig(None), 3)["apps"]["cis"]["totals"]["client_errors"] is None
    assert session_checks.apply({"status": "unavailable"}, 3) == {"status": "unavailable"}


def test_the_rate_and_its_level_use_the_net_count():
    limit = th.client_error_warn_pct()
    requests = 1000
    raw = int(requests * (limit + 5) / 100)  # above the limit with the session checks
    sig = session_checks.apply(_sig(raw, requests), raw - 1)
    tile = next(t for t in sections.app_tiles(sig, {}, {}, {}) if t["app"] == "cis")
    assert tile["traffic"]["client_error_rate_pct"] == round(100.0 / requests, 2)
    assert tile["traffic"]["client_errors_level"] == "ok" and tile["traffic"]["session_checks"] == raw - 1
    before = next(t for t in sections.app_tiles(_sig(raw, requests), {}, {}, {}) if t["app"] == "cis")
    assert before["traffic"]["client_errors_level"] == "warn"


@pytest.mark.asyncio
async def test_signals_read_counts_the_same_span_as_cloudwatch(monkeypatch):
    r = FakeRedis()
    await session_checks.record(r, NOW - timedelta(minutes=3))
    await session_checks.record(r, NOW - timedelta(minutes=3))
    spans: list = []

    def collect(src: TimeRange) -> dict:
        spans.append((src.start, src.end))
        return _sig(10)

    async def cached(redis, name, ttl, compute, **kw):
        return await compute()

    monkeypatch.setattr(cw_series, "collect", collect)
    monkeypatch.setattr(cache, "cached_section", cached)
    rng = ranges.resolve("1h", None, None, now=NOW, history_days=14)
    sig = await ops_status._signals(r, rng)
    assert sig["apps"]["cis"]["totals"]["session_checks"] == 2 and sig["apps"]["cis"]["totals"]["client_errors"] == 8
    prev = await ops_status._signals(r, rng.previous(), previous=True)
    assert "session_checks" not in prev["apps"]["cis"]["totals"]  # the previous period only draws series
    assert spans[0] == (ops_status.fetch_range(rng).start, ops_status.fetch_range(rng).end)


# ── the route counts its 401s, and only those ──────────────────────────────────────────────────────────────────

@pytest.fixture
def counted(monkeypatch):
    calls: list = []

    async def record(redis, now=None):
        calls.append(redis)

    monkeypatch.setattr(session_checks, "record", record)
    monkeypatch.setattr(app.state, "redis", "the-api-redis", raising=False)
    monkeypatch.delenv("LEGACY_ADMIN_LOGIN_ENABLED", raising=False)
    main_mod._staff_state_cache.clear()
    app.dependency_overrides[get_pool] = lambda: object()
    yield calls
    app.dependency_overrides.pop(get_pool, None)


def test_a_refused_session_check_is_counted(counted):
    client = TestClient(app)
    res = client.post("/auth/verify-admin", json={"token": "not-a-jwt"})
    assert res.status_code == 401
    res = client.post("/auth/verify-admin", content=b"[]", headers={"content-type": "application/json"})
    assert res.status_code == 401
    assert counted == ["the-api-redis", "the-api-redis"]


def test_a_non_401_answer_is_not_counted(counted, monkeypatch):
    async def forbidden(request, db):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="No CIS staff group")

    monkeypatch.setattr(main_mod, "_verify_admin", forbidden)
    res = TestClient(app).post("/auth/verify-admin", json={"token": "x"})
    assert res.status_code == 403 and counted == []
