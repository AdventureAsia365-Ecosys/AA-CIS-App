"""S290 follow-up of AA-1140: the session checks the CIS api refuses, kept out of the status page's client error rate.

The admin web (its middleware and BFF) asks ``POST /auth/verify-admin`` whether a staff session is still valid on
every page load; an expired or signed-out session is answered 401. Those 401s are expected (about 1,100 a day on Dev,
S289 live check) and API Gateway counts them in ``4XXError``, so the CIS tile read them as client errors. Only these
401s are taken out (a 404 of a retired tour, a 403 or any other 4xx stays in the rate).

- :func:`record` (the verify-admin route, on each 401): ``INCR`` of the 5-minute bucket's counter in the api's
  shared Redis, kept :func:`ttl_s` (the page's longest range, setting ``cis.ops.status_history_days``, plus one day).
  Best effort: a Redis error is logged and the 401 goes out unchanged.
- :func:`count` (the status page, inside the cached signals read): the sum of the buckets of the range CloudWatch
  was read for, so the two counts cover the same span. None when Redis does not answer: the tile then shows the
  rate as CloudWatch gives it and no session check count, never a guess.
- :func:`apply` (pure): the CIS totals with ``session_checks`` and ``client_errors`` less those (never below 0).

Counts only: no token, user or address is stored. Free (Redis only, no AWS call).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from shared import ops_status_thresholds as th

logger = structlog.get_logger()

KEY_PREFIX = "admin:overview:status:session_401"
BUCKET_S = 300  # technical-constant: counter granularity, the status page's live read grid (SIGNALS_TTL_S)
MGET_CHUNK = 500  # technical-constant: keys per MGET, so one read of a 30-day range stays a few short commands
APP = "cis"  # only the CIS api answers the staff session checks


def bucket_of(t: datetime) -> int:
    e = int(t.timestamp())
    return e - e % BUCKET_S


def key_of(bucket: int) -> str:
    return f"{KEY_PREFIX}:{bucket}"


def ttl_s() -> int:
    """How long a bucket is kept: the longest range the page shows plus a day (the setting, read when written)."""
    return (int(th.history_days()) + 1) * 86400


def buckets(start: datetime, end: datetime) -> list[int]:
    """The buckets whose start lies in [start, end). Pure."""
    first = bucket_of(start)
    if first < int(start.timestamp()):
        first += BUCKET_S
    return list(range(first, int(end.timestamp()), BUCKET_S))


async def record(redis: Any, now: Optional[datetime] = None) -> None:
    """One refused session check. Never raises."""
    if redis is None:
        return
    key = key_of(bucket_of(now or datetime.now(timezone.utc)))
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.incr(key)
        pipe.expire(key, ttl_s())
        await pipe.execute()
    except Exception as e:  # noqa: BLE001 — a count must never change the 401
        logger.warning("ops_status_session_check_count_failed", error=repr(e)[:200])


async def count(redis: Any, start: datetime, end: datetime) -> Optional[int]:
    """The refused session checks in [start, end); None when Redis is missing or does not answer."""
    if redis is None:
        return None
    keys = [key_of(b) for b in buckets(start, end)]
    total = 0
    try:
        for i in range(0, len(keys), MGET_CHUNK):
            for v in await redis.mget(keys[i:i + MGET_CHUNK]):
                if v is not None:
                    total += int(v)
    except Exception as e:  # noqa: BLE001 — the tile falls back to the CloudWatch count
        logger.warning("ops_status_session_check_read_failed", error=repr(e)[:200])
        return None
    return total


def apply(sig: dict, session_checks: Optional[int]) -> dict:
    """The signals read with the CIS totals net of the refused session checks. Pure; unchanged when the count is
    missing or CIS has no client error count."""
    if session_checks is None or sig.get("status") != "ok":
        return sig
    cis = (sig.get("apps") or {}).get(APP)
    totals = (cis or {}).get("totals")
    if not isinstance(totals, dict) or totals.get("client_errors") is None:
        return sig
    net = {**totals, "client_errors": max(0, int(totals["client_errors"]) - session_checks),
           "session_checks": session_checks}
    return {**sig, "apps": {**sig["apps"], APP: {**cis, "totals": net}}}
