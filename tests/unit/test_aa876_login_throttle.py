"""AA-876 — login throttle on /auth/admin-login and /auth/tenant-login (api/core/login_throttle.py).

Per-IP failure limit (20 per 15 min) + all-attempt ceiling, per-(username, IP) exponential backoff from 5
failures and a high cross-IP username limit (admin only), all checked before the DB read and bcrypt;
rightmost-global X-Forwarded-For as the IP; total fail-open.
"""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import bcrypt
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

import api.main as main
from api.core import login_throttle as lt
from api.main import app, get_pool
from api.routers.auth import _hash_api_key

ADMIN_ID = "00000000-0000-0000-0000-000000000876"
PASSWORD = "correct horse battery staple"
GOOD_HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(4)).decode()
TENANT_KEY = "cis_" + "k" * 28


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeRedis:
    """The few commands the throttle uses (direct and pipelined), with TTLs on a controllable clock."""

    def __init__(self, clock):
        self.clock = clock
        self.store: dict[str, tuple[str, float | None]] = {}
        self.calls = 0

    def _live(self, key):
        item = self.store.get(key)
        if item and item[1] is not None and item[1] <= self.clock():
            del self.store[key]
            return None
        return item

    def _get(self, key):
        item = self._live(key)
        return item[0] if item else None

    def _incr(self, key):
        item = self._live(key)
        value = int(item[0]) + 1 if item else 1
        self.store[key] = (str(value), item[1] if item else None)
        return value

    def _expire(self, key, seconds):
        item = self._live(key)
        if item:
            self.store[key] = (item[0], self.clock() + seconds)
        return bool(item)

    def _ttl(self, key):
        item = self._live(key)
        if not item:
            return -2
        if item[1] is None:
            return -1
        return int(item[1] - self.clock())

    def _set(self, key, value, ex=None, nx=False):
        if nx and self._live(key):
            return None
        self.store[key] = (str(value), self.clock() + ex if ex else None)
        return True

    def _delete(self, *keys):
        return sum(1 for k in keys if self.store.pop(k, None) is not None)

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    async def delete(self, *keys):
        self.calls += 1
        return self._delete(*keys)

    async def ttl(self, key):
        return self._ttl(key)


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def __getattr__(self, name):
        fn = getattr(self.redis, "_" + name)

        def queue(*args, **kwargs):
            self.ops.append((fn, args, kwargs))
            return self
        return queue

    async def execute(self):
        self.redis.calls += 1
        return [fn(*a, **k) for fn, a, k in self.ops]


class BrokenRedis:
    """Every command raises; counts how many times it was reached."""

    def __init__(self):
        self.calls = 0

    def pipeline(self, transaction=True):
        return BrokenPipeline(self)

    async def delete(self, *keys):
        self.calls += 1
        raise ConnectionError("redis down")


class BrokenPipeline:
    def __init__(self, redis):
        self.redis = redis

    def __getattr__(self, name):
        return lambda *a, **k: self

    async def execute(self):
        self.redis.calls += 1
        raise ConnectionError("redis down")


class _Pool:
    def __init__(self):
        self.admin_rows: dict[str, dict] = {}
        self.tenant_rows: dict[str, dict] = {}
        self.reads = 0

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetchrow(self, sql, arg):
        self.reads += 1
        if "shared.admin_users" in sql:
            return self.admin_rows.get(arg)
        if "shared.tenants" in sql:
            return self.tenant_rows.get(arg)
        raise AssertionError(sql)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(lt.time, "time", c)
    return c


@pytest.fixture
def redis(clock):
    return FakeRedis(clock)


@pytest.fixture
def pool():
    fake = _Pool()
    fake.admin_rows["alice"] = {"id": ADMIN_ID, "username": "alice", "password_hash": GOOD_HASH,
                                "role": "admin", "is_active": True}
    fake.tenant_rows[_hash_api_key(TENANT_KEY)] = {"tenant_id": "t-876", "name": "T", "plan_tier": "starter"}
    app.dependency_overrides[get_pool] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_pool, None)


@pytest.fixture
def with_redis(redis, monkeypatch):
    monkeypatch.setattr(app.state, "redis", redis, raising=False)
    return redis


@pytest.fixture
def client(pool, monkeypatch):
    monkeypatch.setattr(main, "write_audit_log", AsyncMock())
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def verify_calls(monkeypatch):
    calls = []
    real = main._verify_password

    def spy(pw, h):
        calls.append(h)
        return real(pw, h) if h else False

    monkeypatch.setattr(main, "_verify_password", spy)
    return calls


def _admin(client, username="mallory", password="wrong", xff=None):
    headers = {"X-Forwarded-For": xff} if xff else {}
    return client.post("/auth/admin-login", json={"username": username, "password": password}, headers=headers)


def _tenant(client, key="cis_" + "z" * 28, xff=None):
    headers = {"X-Forwarded-For": xff} if xff else {}
    return client.post("/auth/tenant-login", json={"api_key": key}, headers=headers)


# ── per IP ───────────────────────────────────────────────────────────────────


def test_21st_admin_login_after_20_failures_from_one_ip_is_429_before_bcrypt(client, with_redis, verify_calls):
    for i in range(lt.LOGIN_IP_FAIL_LIMIT):
        assert _admin(client, username=f"user{i}", xff="34.120.0.7").status_code == 401
    assert len(verify_calls) == lt.LOGIN_IP_FAIL_LIMIT
    res = _admin(client, username="someone-else", xff="34.120.0.7")
    assert res.status_code == 429
    assert res.json() == {"detail": lt.THROTTLED_DETAIL}
    assert 1 <= int(res.headers["Retry-After"]) <= lt.LOGIN_FAIL_TTL_S
    assert len(verify_calls) == lt.LOGIN_IP_FAIL_LIMIT  # the 429 never reached bcrypt


def test_successes_do_not_fill_the_failure_bucket(client, with_redis):
    # Staff and CI smoke logins share one Vercel egress IP through the BFFs: 30 good logins stay 200.
    for _ in range(30):
        assert _admin(client, username="alice", password=PASSWORD, xff="34.120.0.40").status_code == 200


def test_all_attempt_ceiling_protects_bcrypt(client, with_redis, clock, monkeypatch):
    monkeypatch.setattr(lt, "LOGIN_IP_ATTEMPT_LIMIT", 3)
    for _ in range(3):
        assert _admin(client, username="alice", password=PASSWORD, xff="34.120.0.41").status_code == 200
    res = _admin(client, username="alice", password=PASSWORD, xff="34.120.0.41")
    assert res.status_code == 429
    assert 1 <= int(res.headers["Retry-After"]) <= lt.LOGIN_IP_WINDOW_S + 1
    clock.t += lt.LOGIN_IP_WINDOW_S
    assert _admin(client, username="alice", password=PASSWORD, xff="34.120.0.41").status_code == 200


def test_two_ips_have_separate_buckets_and_the_failures_expire(client, with_redis, clock):
    for i in range(lt.LOGIN_IP_FAIL_LIMIT):
        _admin(client, username=f"user{i}", xff="34.120.0.7")
    assert _admin(client, username="x", xff="34.120.0.7").status_code == 429
    assert _admin(client, username="x", xff="52.8.100.9").status_code == 401
    clock.t += lt.LOGIN_FAIL_TTL_S
    assert _admin(client, username="y", xff="34.120.0.7").status_code == 401


def test_spoofed_leftmost_xff_does_not_escape_the_bucket(client, with_redis):
    for i in range(lt.LOGIN_IP_FAIL_LIMIT):
        _admin(client, username=f"user{i}", xff=f"1.2.3.{i}, 34.120.0.50")
    assert _admin(client, username="x", xff="9.9.9.9, 34.120.0.50, 10.0.3.7").status_code == 429


def test_tenant_login_per_ip_429(client, with_redis, pool):
    for _ in range(lt.LOGIN_IP_FAIL_LIMIT):
        assert _tenant(client, xff="34.120.0.8").status_code == 401
    res = _tenant(client, key=TENANT_KEY, xff="34.120.0.8")
    assert res.status_code == 429 and "Retry-After" in res.headers
    # the admin scope has its own bucket for the same IP
    assert _admin(client, xff="34.120.0.8").status_code == 401
    # a good key from another IP still logs in
    assert _tenant(client, key=TENANT_KEY, xff="52.8.100.1").status_code == 200


def test_tenant_throttle_runs_before_the_db_read(client, with_redis, pool):
    for _ in range(lt.LOGIN_IP_FAIL_LIMIT):
        _tenant(client, xff="34.120.0.9")
    reads = pool.reads
    assert _tenant(client, xff="34.120.0.9").status_code == 429
    assert pool.reads == reads


def test_good_tenant_logins_do_not_fill_the_bucket(client, with_redis):
    for _ in range(30):
        assert _tenant(client, key=TENANT_KEY, xff="34.120.0.10").status_code == 200


# ── per (username, IP) and per username ──────────────────────────────────────


def test_five_failures_back_off_that_username_from_that_ip_only(client, with_redis, clock, verify_calls):
    for _ in range(lt.LOGIN_FAIL_THRESHOLD):
        assert _admin(client, username="alice", xff="34.120.0.11").status_code == 401
    res = _admin(client, username="alice", password=PASSWORD, xff="34.120.0.11")
    assert res.status_code == 429
    assert res.json() == {"detail": lt.THROTTLED_DETAIL}  # same body as the IP limit
    assert int(res.headers["Retry-After"]) == lt.LOGIN_WAIT_BASE_S
    # the real user from another IP is not locked out, other usernames are unaffected
    assert _admin(client, username="alice", password=PASSWORD, xff="52.8.100.200").status_code == 200
    assert _admin(client, username="bob", xff="34.120.0.11").status_code == 401
    # the wait expires on its own (no lockout); the next failure doubles it
    clock.t += lt.LOGIN_WAIT_BASE_S + 1
    assert _admin(client, username="alice", xff="34.120.0.11").status_code == 401
    res = _admin(client, username="alice", xff="34.120.0.11")
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) == 2 * lt.LOGIN_WAIT_BASE_S
    # and everything is gone after the failure TTL
    clock.t += lt.LOGIN_FAIL_TTL_S + 1
    assert _admin(client, username="alice", password=PASSWORD, xff="34.120.0.11").status_code == 200


def test_backoff_is_capped():
    assert lt.backoff_seconds(lt.LOGIN_FAIL_THRESHOLD) == lt.LOGIN_WAIT_BASE_S
    assert lt.backoff_seconds(lt.LOGIN_FAIL_THRESHOLD + 1) == 2 * lt.LOGIN_WAIT_BASE_S
    assert lt.backoff_seconds(lt.LOGIN_FAIL_THRESHOLD + 30) == lt.LOGIN_WAIT_MAX_S


def test_distributed_failures_set_the_global_username_wait(client, with_redis):
    for i in range(lt.LOGIN_GLOBAL_FAIL_THRESHOLD):
        # 4 failures per IP: never reaches the per-(user, IP) threshold
        assert _admin(client, username="alice", xff=f"34.121.{i // 4}.{i % 4 + 1}").status_code == 401
    res = _admin(client, username="alice", password=PASSWORD, xff="52.8.100.77")
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) == lt.LOGIN_GLOBAL_WAIT_S
    assert _admin(client, username="bob", xff="52.8.100.77").status_code == 401


def test_username_key_is_normalised_and_not_the_username():
    assert lt.username_key(" Alice ") == lt.username_key("alice")
    assert "alice" not in lt.username_key("alice")


def test_success_clears_the_user_ip_failure_count(client, with_redis):
    for _ in range(lt.LOGIN_FAIL_THRESHOLD - 1):
        _admin(client, username="alice", xff="34.120.0.20")
    assert _admin(client, username="alice", password=PASSWORD, xff="34.120.0.20").status_code == 200
    # four more failures after the success: still under the threshold
    for _ in range(lt.LOGIN_FAIL_THRESHOLD - 1):
        assert _admin(client, username="alice", xff="34.120.0.20").status_code == 401
    assert _admin(client, username="alice", password=PASSWORD, xff="34.120.0.20").status_code == 200


def test_no_username_is_stored_in_redis(client, with_redis):
    _admin(client, username="alice-secret-name", xff="34.120.0.21")
    assert not any("alice-secret-name" in k for k in with_redis.store)


# ── fail-open ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["missing", None, "broken", "asyncmock"])
def test_redis_problems_fail_open(client, monkeypatch, verify_calls, state):
    if state == "missing":
        if hasattr(app.state, "redis"):
            monkeypatch.delattr(app.state, "redis")
    else:
        value = {None: None, "broken": BrokenRedis(), "asyncmock": AsyncMock()}[state]
        monkeypatch.setattr(app.state, "redis", value, raising=False)
    for _ in range(lt.LOGIN_IP_FAIL_LIMIT + lt.LOGIN_FAIL_THRESHOLD + 2):
        assert _admin(client, username="alice", xff="34.120.0.30").status_code == 401
        assert _tenant(client, xff="34.120.0.30").status_code == 401
    assert _admin(client, username="alice", password=PASSWORD, xff="34.120.0.30").status_code == 200


def test_a_redis_error_skips_the_rest_of_the_request(client, monkeypatch):
    broken = BrokenRedis()
    monkeypatch.setattr(app.state, "redis", broken, raising=False)
    assert _admin(client, username="alice", xff="34.120.0.31").status_code == 401
    assert broken.calls == 1  # check() failed; record_failure() did not try again
    assert _admin(client, username="alice", password=PASSWORD, xff="34.120.0.31").status_code == 200
    assert broken.calls == 2


# ── client_ip ────────────────────────────────────────────────────────────────


def _req(xff_values=(), host="10.0.1.5"):
    raw = [(b"x-forwarded-for", v.encode()) for v in xff_values]
    return SimpleNamespace(headers=Headers(raw=raw), client=SimpleNamespace(host=host) if host else None)


@pytest.mark.parametrize("values,expected", [
    (["34.120.0.5"], "34.120.0.5"),
    # a caller-written (spoofed) leftmost entry is ignored: API GW appends the real source to the right
    (["1.2.3.4, 34.120.0.5"], "34.120.0.5"),
    # private hops after the edge (NLB / ALB) and garbage are skipped
    (["8.8.8.8, 34.120.0.5, 10.0.3.7"], "34.120.0.5"),
    (["34.120.0.5, not-an-ip, 172.16.0.1, 127.0.0.1"], "34.120.0.5"),
    # several header lines are read in order
    (["9.9.9.9", "34.120.0.5, 10.0.0.2"], "34.120.0.5"),
    # IPv6 is bucketed by /64
    (["2001:4860:1234:5678:aaaa:bbbb:cccc:dddd"], "2001:4860:1234:5678::/64"),
    (["::ffff:34.120.0.5"], "34.120.0.5"),
    # CGNAT / shared address space (a VPC-link hop) is not global either
    (["34.120.0.5, 100.64.12.3"], "34.120.0.5"),
    # nothing public: the socket peer
    (["10.0.0.1, 192.168.1.1"], "10.0.1.5"),
    ([], "10.0.1.5"),
])
def test_client_ip(values, expected):
    assert lt.client_ip(_req(values)) == expected


def test_client_ip_non_ip_peer_is_an_opaque_key():
    assert lt.client_ip(_req((), host="testclient")) == "testclient"
    assert lt.client_ip(_req((), host=None)) == "unknown"


def test_ipv6_same_64_shares_a_bucket():
    a = lt.client_ip(_req(["2001:4860:1:2:aaaa::1"]))
    b = lt.client_ip(_req(["2001:4860:1:2:ffff::9"]))
    c = lt.client_ip(_req(["2001:4860:1:3::1"]))
    assert a == b != c
