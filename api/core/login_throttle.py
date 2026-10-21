"""Login throttle for /auth/admin-login and /auth/tenant-login (AA-876).

Everything lives in the api's shared Redis (app.state.redis) and is checked BEFORE the DB read and the bcrypt
compare, in ONE pipelined round trip, so a throttled request costs one Redis call and nothing else.

Per source IP, per scope (``admin`` / ``tenant``):

- FAILURES: ``loginfail_ip:{scope}:{ip}``, LOGIN_IP_FAIL_LIMIT failed logins per LOGIN_FAIL_TTL_S (the TTL is set
  on the first failure and never refreshed). Successes do not count, so staff and CI smoke logins sharing one
  Vercel egress IP (every BFF login) never fill it.
- ALL ATTEMPTS: ``loginrl:{scope}:{ip}:{window}``, a high ceiling (LOGIN_IP_ATTEMPT_LIMIT per
  LOGIN_IP_WINDOW_S) that only protects the task's bcrypt CPU.

Per username, admin only (``h`` = hash of the normalised username, so no username is stored in Redis):

- Per (username, IP): from LOGIN_FAIL_THRESHOLD failures within LOGIN_FAIL_TTL_S, an exponential wait
  (30 s, 60 s, ... capped at LOGIN_WAIT_MAX_S) during which that username from that IP answers 429 without a
  password check. Keys ``loginfail:{h}:{ip}`` / ``loginwait:{h}:{ip}``; a success clears both. Keyed on the IP
  too, so nobody can keep a known username (several are named in public repos) locked from somewhere else.
- Per username across IPs: from LOGIN_GLOBAL_FAIL_THRESHOLD failures in LOGIN_FAIL_TTL_S (distributed
  guessing), ``loginwait_g:{h}`` for LOGIN_GLOBAL_WAIT_S. Deliberately high: it is the one remote lever on a
  real account, so it needs a botnet-sized effort and expires by itself.

Backoff only, never a hard lockout: every key expires on its own. Every 429 has the same body and a
Retry-After, so the answer does not say which limit fired (or whether the username exists). Every 429 logs
``login_throttled`` (scope, reason, IP; no username).

Fail-open, totally: no Redis on app.state, a None client, a Redis error or timeout, or an unexpected reply
type (a test double) all mean "no limit". A real error logs ``login_throttle_redis_error`` and marks the
request so the later record_* calls skip Redis (a dead Redis costs at most one bounded call per login, well
inside the admin BFF's 5 s timeout). The API Gateway login throttle still caps the rate then.

The client IP comes from X-Forwarded-For: the RIGHTMOST globally routable address. The edge is API Gateway
(REST) -> VPC link (NLB) -> ALB -> ECS; API Gateway appends the real source address and the later hops are
private or CGNAT (not global, skipped). Anything a caller writes into X-Forwarded-For lands to the LEFT of that
entry, so it cannot pick its own bucket. Revisit this if a proxy is added in front of API Gateway or the ALB
is ever reached directly. IPv6 addresses are bucketed by /64 (one subscriber's whole prefix).
"""
from __future__ import annotations

import hashlib
import ipaddress
import time
from typing import Any, Optional

import structlog
from fastapi import HTTPException

logger = structlog.get_logger(__name__)

LOGIN_IP_FAIL_LIMIT = 20
LOGIN_IP_WINDOW_S = 300
LOGIN_IP_ATTEMPT_LIMIT = 100
LOGIN_FAIL_TTL_S = 900
LOGIN_FAIL_THRESHOLD = 5
LOGIN_WAIT_BASE_S = 30
LOGIN_WAIT_MAX_S = 900
LOGIN_GLOBAL_FAIL_THRESHOLD = 50
LOGIN_GLOBAL_WAIT_S = 300

THROTTLED_DETAIL = "Too many sign-in attempts. Try again later."

_OFF_FLAG = "login_throttle_off"


def _as_int(value: Any) -> Optional[int]:
    """A Redis integer reply, or None for anything else (a MagicMock from a test double, None, garbage)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (str, bytes)):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _redis(request: Any) -> Any:
    """The Redis client for this request, or None when there is none or an earlier call already failed."""
    try:
        if getattr(request.state, _OFF_FLAG, False):
            return None
    except Exception:
        pass
    try:
        return getattr(request.app.state, "redis", None)
    except Exception:
        return None


def _switch_off(request: Any, step: str, exc: BaseException) -> None:
    logger.warning("login_throttle_redis_error", step=step, error=repr(exc))
    try:
        setattr(request.state, _OFF_FLAG, True)
    except Exception:
        pass


def _bucket(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{ip}/64", strict=False))
    return str(ip)


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def client_ip(request: Any) -> str:
    """The throttle key for the caller: rightmost global X-Forwarded-For entry (IPv6 as its /64), else the
    socket peer (used as an opaque string when it is not an IP, e.g. TestClient's "testclient")."""
    values: list[str] = []
    try:
        headers = request.headers
        raw = headers.getlist("x-forwarded-for") if hasattr(headers, "getlist") else [headers.get("x-forwarded-for")]
        for header in raw:
            if header:
                values.extend(part.strip() for part in str(header).split(","))
    except Exception:
        values = []
    for entry in reversed(values):
        try:
            ip = ipaddress.ip_address(entry)
        except ValueError:
            continue
        if _is_public(ip):
            return _bucket(ip)
    try:
        host = request.client.host if request.client else ""
    except Exception:
        host = ""
    if not host:
        return "unknown"
    try:
        return _bucket(ipaddress.ip_address(host))
    except ValueError:
        return str(host)


def username_key(username: str) -> str:
    return hashlib.sha256((username or "").strip().lower().encode()).hexdigest()[:32]


def backoff_seconds(failures: int) -> int:
    """The (username, IP) wait after ``failures`` failures: 30 s at the threshold, doubling, capped."""
    return int(min(LOGIN_WAIT_BASE_S * 2 ** max(0, failures - LOGIN_FAIL_THRESHOLD), LOGIN_WAIT_MAX_S))


def _throttled(scope: str, reason: str, ip: str, retry_after: int) -> HTTPException:
    logger.warning("login_throttled", scope=scope, reason=reason, ip=ip, retry_after=retry_after)
    return HTTPException(
        status_code=429, detail=THROTTLED_DETAIL, headers={"Retry-After": str(max(1, int(retry_after)))},
    )


async def check(request: Any, scope: str, username: Optional[str] = None) -> None:
    """Raise HTTPException(429) when any limit is hit for this caller (and, admin, this username).
    Returns None otherwise, including on any Redis problem. Counts this attempt in the all-attempts window."""
    redis = _redis(request)
    if redis is None:
        return
    ip = client_ip(request)
    now = time.time()
    window = int(now // LOGIN_IP_WINDOW_S)
    fail_ip_key = f"loginfail_ip:{scope}:{ip}"
    rate_key = f"loginrl:{scope}:{ip}:{window}"
    h = username_key(username) if username else None
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.get(fail_ip_key)
        pipe.ttl(fail_ip_key)
        pipe.incr(rate_key)
        pipe.expire(rate_key, LOGIN_IP_WINDOW_S * 2)
        if h:
            pipe.ttl(f"loginwait:{h}:{ip}")
            pipe.ttl(f"loginwait_g:{h}")
        replies = await pipe.execute()
    except Exception as exc:
        _switch_off(request, "check", exc)
        return
    if not isinstance(replies, (list, tuple)) or len(replies) < 4:
        return
    fails, fails_ttl, attempts = _as_int(replies[0]), _as_int(replies[1]), _as_int(replies[2])
    if fails is not None and fails >= LOGIN_IP_FAIL_LIMIT:
        raise _throttled(scope, "ip_fail", ip, fails_ttl if fails_ttl and fails_ttl > 0 else LOGIN_FAIL_TTL_S)
    if attempts is not None and attempts > LOGIN_IP_ATTEMPT_LIMIT:
        raise _throttled(scope, "ip_rate", ip, int((window + 1) * LOGIN_IP_WINDOW_S - now) + 1)
    if h and len(replies) >= 6:
        user_wait, global_wait = _as_int(replies[4]), _as_int(replies[5])
        if user_wait is not None and user_wait > 0:
            raise _throttled(scope, "user", ip, user_wait)
        if global_wait is not None and global_wait > 0:
            raise _throttled(scope, "user_global", ip, global_wait)


async def record_failure(request: Any, scope: str, username: Optional[str] = None) -> None:
    """Count one failed login from this IP (and, admin, for this username); start or lengthen the waits."""
    redis = _redis(request)
    if redis is None:
        return
    ip = client_ip(request)
    h = username_key(username) if username else None
    try:
        # SET ... NX EX then INCR: the TTL is set once, on the first failure, and INCR keeps it (one round trip,
        # works on every Redis version — EXPIRE NX needs 7.0).
        pipe = redis.pipeline(transaction=False)
        fail_ip_key = f"loginfail_ip:{scope}:{ip}"
        pipe.set(fail_ip_key, 0, ex=LOGIN_FAIL_TTL_S, nx=True)
        pipe.incr(fail_ip_key)
        if h:
            for key in (f"loginfail:{h}:{ip}", f"loginfail_g:{h}"):
                pipe.set(key, 0, ex=LOGIN_FAIL_TTL_S, nx=True)
                pipe.incr(key)
        replies = await pipe.execute()
        if not h or not isinstance(replies, (list, tuple)) or len(replies) < 6:
            return
        user_fails, global_fails = _as_int(replies[3]), _as_int(replies[5])
        waits = redis.pipeline(transaction=False)
        queued = False
        if user_fails is not None and user_fails >= LOGIN_FAIL_THRESHOLD:
            waits.set(f"loginwait:{h}:{ip}", 1, ex=backoff_seconds(user_fails))
            queued = True
        if global_fails is not None and global_fails >= LOGIN_GLOBAL_FAIL_THRESHOLD:
            waits.set(f"loginwait_g:{h}", 1, ex=LOGIN_GLOBAL_WAIT_S)
            queued = True
        if queued:
            await waits.execute()
    except Exception as exc:
        _switch_off(request, "record_failure", exc)


async def record_success(request: Any, scope: str, username: Optional[str] = None) -> None:
    """After a good login, clear this (username, IP) failure count and wait. The per-IP failure count and the
    cross-IP username count are left to expire: a success must not reset what an attacker has been doing."""
    redis = _redis(request)
    if redis is None or not username:
        return
    ip = client_ip(request)
    h = username_key(username)
    try:
        await redis.delete(f"loginfail:{h}:{ip}", f"loginwait:{h}:{ip}")
    except Exception as exc:
        _switch_off(request, "record_success", exc)
