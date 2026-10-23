"""AA-987 — job kind `tripplanner_purge`: a tour's served state changed, tell TripPlanner to drop its caches.

Why. A retired tour left every CIS read at once (AA-986), but the public TripPlanner site kept it for up to 30 min:
the browse list is cached by the Vercel CDN (`max-age=1800`) and the Next.js pages by ISR (30 min). TripPlanner has
no scheduler that could pull the outbox every minute (Vercel Hobby crons run once a day), so CIS pushes a signed
notice and TripPlanner purges on demand (`POST /api/revalidate/catalog` in AA-TripPlanner-Web).

Outbox-driven, not fire-and-forget:
  - one job per lifecycle event, idempotency key ``tripplanner_purge:<event idempotency_key>``: shared.job is the
    delivery inbox (contracts README rule 3 / 4: anti-join on the event key, never the BIGSERIAL id alone);
  - enqueued right after the lifecycle action commits (services/tour_lifecycle/effects.run_after_commit, and the
    publish of services/export/handler.process_export), best-effort;
  - a sweep (api ops sampler, every OPS_METRICS_INTERVAL_S) enqueues every event of the last SWEEP_WINDOW_S that has
    no job yet, so an enqueue lost after a commit is delivered a few minutes later, not never;
  - the handler re-reads the event from gold_aa_internal.tour_lifecycle_event (the base table of the contract view
    catalog.v_tour_lifecycle_outbox; cis_app has no grant on the view) and sends it;
  - a purge is idempotent on the TripPlanner side (dropping a cache twice drops it once), so a replay or a retry is
    safe; the worker retries a 5xx / network error with backoff (MAX_ATTEMPTS), a 4xx fails at once (NonRetryable:
    a bad signature or payload is not fixed by waiting). When every attempt fails, TripPlanner's own time-based
    refresh (30 min) is the bound, as before AA-987.

Authenticated with an HMAC-SHA256 shared secret: header ``X-AA-Signature: t=<unix seconds>,v1=<hex>`` over
``f"{t}.{body}"``. TripPlanner refuses a signature older than 5 minutes. The secret lives in Secrets Manager under
``aa-cis/<ENVIRONMENT>/tripplanner-revalidate-hmac`` (the CIS task role may not read ``tripplanner/*``); the same
value is the TripPlanner Vercel env ``CATALOG_REVALIDATE_SECRET``. Not configured (secret missing, URL empty) = the
job succeeds with ``{"delivered": false, "skipped": "not_configured"}`` and one warning line, so an environment without
the secret never piles up failed jobs.

Payload: ``{"event_key": "<tour_lifecycle_event.idempotency_key>", "trigger": "after_commit" | "publish" | "sweep"}``.

Observable: the job row (Jobs page: status, attempts, result with HTTP status and latency), the AA/Jobs/CIS
CloudWatch metrics every kind gets, and one log line per attempt (``tripplanner_purge_delivered`` /
``tripplanner_purge_failed`` / ``tripplanner_purge_not_configured``). No secret, no payload body in logs.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import threading
import time
from typing import Any, Iterable, Optional

import structlog

from shared.jobs import queue
from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "tripplanner_purge"
KEY_PREFIX = f"{KIND}:"
# 30 s, 60 s, 120 s backoff between attempts (shared.jobs.queue.backoff_seconds): ~3.5 min of retries. Short on
# purpose: a queued retry counts in the CIS deploy gate (DM-11.4), and past a few minutes the 30-min TTL is near.
MAX_ATTEMPTS = 4
HTTP_TIMEOUT_S = 10.0

DEFAULT_URL = "https://aa-tripplanner.vercel.app/api/revalidate/catalog"
SIGNATURE_HEADER = "X-AA-Signature"
CONTRACT = "catalog.tour.lifecycle.v1"

# The sweep looks back this far; an event older than the TripPlanner TTL (30 min) needs no purge any more, the margin
# covers an api that was down for a while. SWEEP_LAG_S leaves the after-commit enqueue its chance first (the key is
# unique either way, so a race only costs one no-op INSERT).
SWEEP_WINDOW_S = 7200
SWEEP_LAG_S = 30

_SECRET_TTL_S = 600      # re-read the secret every 10 min (a rotation needs no deploy)
_SECRET_FAILURE_TTL_S = 60
_SM_CONNECT_TIMEOUT_S = 2
_SM_READ_TIMEOUT_S = 3

# 4xx answers a retry cannot fix (TripPlanner refused the signature, the payload or the route). 408 / 425 / 429 and
# every 5xx (503 = TripPlanner's secret not set yet) are retried.
_RETRYABLE_4XX = {408, 425, 429}

EVENT_SQL = """
    SELECT e.id AS event_id, e.source_system, e.source_id, e.tour_id::text AS tour_id, e.event, e.reason,
           e.replaced_by_tour_id::text AS replaced_by_tour_id, e.occurred_at, e.idempotency_key
      FROM gold_aa_internal.tour_lifecycle_event e
     WHERE e.idempotency_key = $1
"""

SWEEP_SQL = f"""
    WITH created AS (
    INSERT INTO shared.job (kind, payload, idempotency_key, max_attempts, created_by)
    SELECT '{KIND}', jsonb_build_object('event_key', e.idempotency_key, 'trigger', 'sweep'),
           '{KEY_PREFIX}' || e.idempotency_key, {MAX_ATTEMPTS}, 'system:{KIND}_sweep'
      FROM gold_aa_internal.tour_lifecycle_event e
     WHERE e.occurred_at > now() - make_interval(secs => $1)
       AND e.occurred_at < now() - make_interval(secs => $2)
       AND NOT EXISTS (SELECT 1 FROM shared.job j WHERE j.idempotency_key = '{KEY_PREFIX}' || e.idempotency_key)
    ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING
    RETURNING 1
    )
    SELECT count(*) FROM created
"""


# ── configuration ──────────────────────────────────────────────────────────────────────────────────────────────────

def target_url() -> str:
    """Where the notice goes. `TRIPPLANNER_REVALIDATE_URL` = "" switches delivery off."""
    value = os.environ.get("TRIPPLANNER_REVALIDATE_URL")
    return DEFAULT_URL if value is None else value.strip()


def secret_name() -> str:
    env = (os.environ.get("ENVIRONMENT") or "dev").strip().lower() or "dev"
    return os.environ.get("TRIPPLANNER_REVALIDATE_SECRET_NAME") or f"aa-cis/{env}/tripplanner-revalidate-hmac"


def _fetch_secret_string(name: str) -> str:
    import boto3
    from botocore.config import Config
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-1"
    client = boto3.client("secretsmanager", region_name=region, config=Config(
        connect_timeout=_SM_CONNECT_TIMEOUT_S, read_timeout=_SM_READ_TIMEOUT_S,
        retries={"max_attempts": 2, "mode": "standard"},
    ))
    return client.get_secret_value(SecretId=name)["SecretString"]


def parse_secret(raw: Optional[str]) -> Optional[str]:
    """The HMAC key from the secret string: the bare value, or JSON ``{"secret": "..."}``. None when empty."""
    value = (raw or "").strip()
    if value.startswith("{"):
        try:
            value = str(json.loads(value).get("secret") or "").strip()
        except (ValueError, AttributeError):
            return None
    return value or None


class _SecretCache:
    """TTL cache of the HMAC key. A failed read is cached for _SECRET_FAILURE_TTL_S (None = not configured)."""

    def __init__(self, fetch=_fetch_secret_string):
        self._fetch = fetch
        self._lock = threading.Lock()
        self._value: Optional[str] = None
        self._until = 0.0

    def get(self) -> Optional[str]:
        local = os.environ.get("TRIPPLANNER_REVALIDATE_SECRET")
        if local:
            return local.strip() or None
        with self._lock:
            now = time.monotonic()
            if now < self._until:
                return self._value
            try:
                self._value = parse_secret(self._fetch(secret_name()))
                self._until = now + (_SECRET_TTL_S if self._value else _SECRET_FAILURE_TTL_S)
            except Exception as e:  # ResourceNotFound, AccessDenied, network: not configured for now
                logger.warning("tripplanner_purge_secret_unavailable", secret=secret_name(), error=type(e).__name__)
                self._value = None
                self._until = now + _SECRET_FAILURE_TTL_S
            return self._value

    def reset(self) -> None:
        with self._lock:
            self._value, self._until = None, 0.0


_secret = _SecretCache()


async def hmac_secret() -> Optional[str]:
    return await asyncio.to_thread(_secret.get)


# ── the notice ─────────────────────────────────────────────────────────────────────────────────────────────────────

def event_body(row: Any) -> dict:
    """One outbox row as the notice payload (the catalog.v_tour_lifecycle_outbox columns, CIS 274)."""
    occurred = row["occurred_at"]
    return {
        "event_id": int(row["event_id"]),
        "idempotency_key": row["idempotency_key"],
        "source_system": row["source_system"],
        "source_id": row["source_id"],
        "tour_id": row["tour_id"],
        "event": row["event"],
        "reason": row["reason"],
        "replaced_by_tour_id": row["replaced_by_tour_id"],
        "occurred_at": occurred.isoformat() if hasattr(occurred, "isoformat") else str(occurred),
    }


def encode_body(events: list[dict]) -> bytes:
    return json.dumps({"contract": CONTRACT, "events": events}, separators=(",", ":"), sort_keys=True).encode()


def sign(secret: str, body: bytes, timestamp: int) -> str:
    """The X-AA-Signature value: ``t=<timestamp>,v1=<hex hmac_sha256(secret, f"{t}.{body}")>``."""
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


async def _post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
    import httpx
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=False) as client:
        resp = await client.post(url, content=body, headers=headers)
        return resp.status_code, resp.text[:500]


async def deliver(row: Any, *, secret: str, url: str, post=None, now: Optional[float] = None) -> dict:
    """POST one event. Returns the result dict on 2xx; raises NonRetryable on a final 4xx, RuntimeError otherwise.
    `post` defaults to the module's _post, looked up per call (tests patch it)."""
    post = post or _post
    body = encode_body([event_body(row)])
    ts = int(now if now is not None else time.time())
    headers = {"content-type": "application/json", SIGNATURE_HEADER: sign(secret, body, ts),
               "x-aa-event-key": row["idempotency_key"]}
    started = time.monotonic()
    try:
        status, text = await post(url, body, headers)
    except Exception as e:  # network, DNS, timeout: retried by the runner
        raise RuntimeError(f"tripplanner purge request failed: {type(e).__name__}") from e
    ms = int((time.monotonic() - started) * 1000)
    if 200 <= status < 300:
        try:
            answer = json.loads(text) if text else {}
        except ValueError:
            answer = {}
        return {"delivered": True, "status": status, "ms": ms,
                "purged": answer.get("purged") if isinstance(answer, dict) else None}
    message = f"tripplanner answered {status}: {text[:200]}"
    if 400 <= status < 500 and status not in _RETRYABLE_4XX:
        raise NonRetryable(message)
    raise RuntimeError(message)


@job_kind(KIND, concurrency=1, max_attempts=MAX_ATTEMPTS, expected_seconds=60)
async def run(ctx: JobContext) -> dict:
    key = (ctx.payload or {}).get("event_key")
    if not key or not isinstance(key, str):
        raise NonRetryable("payload needs event_key")
    row = await ctx.pool.fetchrow(EVENT_SQL, key)
    if row is None:
        raise NonRetryable(f"no tour lifecycle event with key {key[:120]}")
    base = {"event_id": int(row["event_id"]), "lifecycle_event": row["event"], "source_id": row["source_id"],
            "trigger": (ctx.payload or {}).get("trigger")}

    url = target_url()
    secret = await hmac_secret() if url else None
    if not url or not secret:
        logger.warning("tripplanner_purge_not_configured", job_id=ctx.job_id, url_set=bool(url),
                       secret=secret_name(), **base)
        return {**base, "delivered": False, "skipped": "not_configured"}

    try:
        result = await deliver(row, secret=secret, url=url)
    except Exception as e:
        logger.warning("tripplanner_purge_failed", job_id=ctx.job_id, attempt=ctx.job.attempt,
                       error=str(e)[:300], **base)
        raise
    logger.info("tripplanner_purge_delivered", job_id=ctx.job_id, attempt=ctx.job.attempt,
                status=result["status"], ms=result["ms"], **base)
    return {**base, **result}


# ── enqueue + sweep ────────────────────────────────────────────────────────────────────────────────────────────────

async def enqueue_for_events(db, events: Iterable[dict], *, trigger: str = "after_commit",
                             created_by: str = "system:tour_lifecycle") -> int:
    """One job per lifecycle event (``events`` = Outcome.events, each with its ``idempotency_key``). Best-effort:
    never raises (the state change is committed already; the sweep catches what is lost here). Returns the number of
    jobs created. ``db`` is a pool or a connection outside a transaction."""
    created = 0
    for e in events or []:
        key = e.get("idempotency_key") if isinstance(e, dict) else None
        if not key:
            continue
        try:
            _, new = await queue.enqueue(db, KIND, {"event_key": key, "trigger": trigger},
                                         idempotency_key=KEY_PREFIX + key, max_attempts=MAX_ATTEMPTS,
                                         created_by=created_by)
            created += int(new)
        except Exception as exc:
            logger.warning("tripplanner_purge_enqueue_failed", lifecycle_event=e.get("event"),
                           error=type(exc).__name__)
    return created


async def sweep(db, *, window_s: int = SWEEP_WINDOW_S, lag_s: int = SWEEP_LAG_S) -> int:
    """Enqueue a job for every recent lifecycle event that has none (lost after-commit enqueue, a path that does not
    enqueue). One INSERT ... SELECT, idempotent. Returns the number of jobs created."""
    return int(await db.fetchval(SWEEP_SQL, float(window_s), float(lag_s)) or 0)
