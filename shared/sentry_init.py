"""Sentry error tracking for the CIS API and the job worker (AA-142, AA-870).

Lives in `shared/` so `python -m worker` can call it without importing `api` (whose package
`__init__` builds the whole FastAPI app). `api/core/sentry.py` re-exports it for the API.

Privacy (AA-870, S241 review P0-6): `send_default_pii=False`, no request bodies, no cookies, and
request headers reduced to an allowlist, so admin secrets, partner keys and session tokens in
custom headers (`x-admin-secret`, `x-api-key`, `x-tripplanner-key`, ...) never leave the task.
Breadcrumbs keep the AA-712 redaction of key-like URL query parameters.
AA-941 (S249 security review): no frame locals on any event (`include_local_variables=False` + before_send drops
`vars`), and JWT-shaped strings, `Bearer` values and e-mails are redacted in the remaining free text.

Log-line events (AA-871): structlog lines reach the logging integration through stdlib (shared/log_config.py), so an
``error`` / ``exception`` line is a Sentry event. ``before_send`` titles it with the event name (fields in ``extra``)
and caps it at THROTTLE_MAX events per (logger, event) per THROTTLE_WINDOW_S in each process.
"""
import json
import logging
import os
import re
import threading
import time
from collections import deque

import boto3
import sentry_sdk

logger = logging.getLogger(__name__)

SECRET_ID = "aa-cis/dev/sentry-dsn-api"

# Request headers that may be sent with an event. Everything else is dropped.
SAFE_HEADERS = frozenset({
    "accept", "accept-encoding", "accept-language", "content-length", "content-type", "host",
    "user-agent", "x-forwarded-proto", "x-run-id", "x-tenant-id",
})


def _get_dsn() -> str:
    try:
        client = boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "us-west-1"),
        )
        secret = client.get_secret_value(SecretId=SECRET_ID)
        return secret["SecretString"].strip()
    except Exception as e:
        logger.warning(f"sentry_dsn_fetch_failed: {e}")
        return ""


# AA-712: Geoapify takes its key as a URL query parameter; Sentry's default httpx / stdlib integrations
# record request URLs in breadcrumbs. Redact any key-like query parameter before a breadcrumb is kept.
_SECRET_PARAM = re.compile(r"((?:api_?key|apikey|access_token|token|key)=)[^&\s'\"]+", re.I)


def _redact(value):
    if isinstance(value, str):
        return _SECRET_PARAM.sub(r"\1***", value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact(v) for v in value)
    return value


def before_breadcrumb(crumb, hint):
    """Strip API keys (URLs, query strings), JWTs, Bearer values and e-mails from breadcrumb message / data."""
    if "message" in crumb:
        crumb["message"] = _redact_pii(crumb["message"])
    if "data" in crumb:
        crumb["data"] = _redact_pii(crumb["data"])
    return crumb


# AA-871 — structlog lines reach Sentry through stdlib (shared/log_config.py), so ``record.msg`` is structlog's
# event dict. Fields that duplicate the event's own timestamp / level / logger / release / environment are not copied
# into ``extra``.
_DUPLICATE_FIELDS = frozenset({
    "event", "timestamp", "level", "logger", "exc_info", "stack_info", "service", "environment", "git_sha",
})
_EXTRA_STR_MAX = 2000

# AA-871 — per-process cap on log-line events: at most THROTTLE_MAX events per (logger, event name) in
# THROTTLE_WINDOW_S. Some error lines fire once per request / tour / LLM call during an outage and would burn the
# Sentry quota; the first few carry the signal. Dropped events are only counted (``throttled_events``), never sent.
THROTTLE_MAX = 5
THROTTLE_WINDOW_S = 600.0
_THROTTLE_KEYS_MAX = 2000
_throttle: dict = {}
_throttle_lock = threading.Lock()
throttled_events = 0


def _throttled(key, now: float | None = None) -> bool:
    global throttled_events
    now = time.monotonic() if now is None else now
    with _throttle_lock:
        if len(_throttle) > _THROTTLE_KEYS_MAX:  # forget keys whose window has passed (bounded memory)
            for k in [k for k, q in _throttle.items() if not q or now - q[-1] >= THROTTLE_WINDOW_S]:
                del _throttle[k]
        q = _throttle.setdefault(key, deque())
        while q and now - q[0] >= THROTTLE_WINDOW_S:
            q.popleft()
        if len(q) >= THROTTLE_MAX:
            throttled_events += 1
            return True
        q.append(now)
        return False


def _json_safe(value):
    """``extra`` is added after the SDK serialized the event, so it must already be JSON-friendly."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_EXTRA_STR_MAX]
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return str(value)[:_EXTRA_STR_MAX]


def _shape_log_event(event, record) -> None:
    """A structlog line: the event name is the message (Sentry title / grouping), the other fields go to ``extra``.
    Without this the title is ``str(event_dict)`` — a new issue for every distinct value."""
    fields = record.msg
    name = str(fields.get("event") or record.name)
    event["logentry"] = {"message": name, "formatted": name}
    extra = event.get("extra") if isinstance(event.get("extra"), dict) else {}
    for k, v in fields.items():
        if k not in _DUPLICATE_FIELDS and not str(k).startswith("_"):
            extra[str(k)] = _json_safe(v)
    event["extra"] = extra


def _log_event_key(record) -> tuple:
    msg = record.msg
    name = msg.get("event") if isinstance(msg, dict) else msg
    return (record.name, str(name))


# Frame locals (AA-941 security review, S249): the SDK keeps every frame's local variables by default
# (include_local_variables=True). An unhandled 500 on a staff route then carried the full Cognito access token
# (StaffBearerMiddleware `scope` / `headers`, Starlette `scope` / `request`) and the request body (FastAPI `body`,
# `values`, route payloads such as an e-mail) to Sentry; AA-839 had only scrubbed erasure jobs. init_sentry now turns
# locals off for every event and before_send drops any `vars` that still arrive (belt and braces: a future init or
# integration that turns them back on still sends none). Stack frames (file / line / function) are kept.
#
# Remaining free text (exception messages, log-line ``extra``, breadcrumbs, request URL) is redacted by value:
# JWT-shaped strings, ``Bearer <x>`` values and e-mail addresses never leave the task.
_BEARER = re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=-]+", re.I)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
# Starts only where a local part can start, so a long run without "@" is scanned once (pattern from AA-Partner scrub).
_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
REDACTED = "[redacted]"


def _redact_pii(value):
    """Key-like query params (AA-712) plus JWTs, Bearer values and e-mails, recursively over str / dict / list."""
    if isinstance(value, str):
        value = _SECRET_PARAM.sub(r"\1***", value)
        value = _BEARER.sub(r"\1" + REDACTED, value)
        value = _JWT.sub(REDACTED, value)
        return _EMAIL.sub(REDACTED, value)
    if isinstance(value, dict):
        return {k: _redact_pii(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_pii(v) for v in value)
    return value


def _frames(event):
    stacks = [event.get("stacktrace")]
    for container in ("exception", "threads"):
        block = event.get(container)
        values = block.get("values") if isinstance(block, dict) else None
        for v in values or []:
            stacks.append(v.get("stacktrace") if isinstance(v, dict) else None)
    for stack in stacks:
        for f in (stack if isinstance(stack, dict) else {}).get("frames") or []:
            if isinstance(f, dict):
                yield f


def scrub_frame_locals(event) -> None:
    """Drops the locals (``vars``) of every frame of the event (AA-839 for erasure jobs, AA-941 for everything)."""
    for f in _frames(event):
        f.pop("vars", None)


def redact_event_text(event) -> None:
    """Redacts credentials and e-mails in the event's free-text fields (see _redact_pii)."""
    for block_name in ("exception", "threads"):
        block = event.get(block_name)
        for v in (block.get("values") if isinstance(block, dict) else None) or []:
            if isinstance(v, dict) and isinstance(v.get("value"), str):
                v["value"] = _redact_pii(v["value"])
    for field in ("message", "logentry", "extra"):
        if field in event:
            event[field] = _redact_pii(event[field])
    crumbs = event.get("breadcrumbs")
    values = crumbs.get("values") if isinstance(crumbs, dict) else crumbs
    for crumb in values if isinstance(values, list) else []:
        if isinstance(crumb, dict):
            for field in ("message", "data"):
                if field in crumb:
                    crumb[field] = _redact_pii(crumb[field])
    request = event.get("request")
    if isinstance(request, dict):
        for field in ("url", "query_string"):
            if field in request:
                request[field] = _redact_pii(request[field])


def before_send(event, hint):
    """Drop request bodies, cookies and non-allowlisted headers; redact key-like query strings.
    AA-871: shape and throttle events made from log lines (``hint["log_record"]``).
    AA-839 / AA-941: no frame locals on any event; JWTs, Bearer values and e-mails redacted in the text fields."""
    scrub_frame_locals(event)
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {k: v for k, v in headers.items() if str(k).lower() in SAFE_HEADERS}
    record = (hint or {}).get("log_record")
    if record is not None:
        try:
            if _throttled(_log_event_key(record)):
                return None
            if isinstance(record.msg, dict):
                _shape_log_event(event, record)
        except Exception:  # never lose the event over its shaping (an exception here would drop it)
            logger.debug("sentry_log_event_shape_failed", exc_info=True)
    try:
        redact_event_text(event)
    except Exception:  # fail closed: an event whose text could not be redacted is not sent
        logger.debug("sentry_event_redact_failed", exc_info=True)
        return None
    return event


def release() -> str:
    """The git SHA baked into the image at build time (Dockerfile ARG GIT_SHA), else 'unknown'."""
    return os.getenv("APP_GIT_SHA") or "unknown"


def init_sentry(service: str = "api") -> None:
    dsn = _get_dsn()
    if not dsn:
        logging.warning("Sentry DSN not found — error tracking disabled")
        return
    integrations = []
    if service == "api":
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import ignore_logger
        from sentry_sdk.integrations.starlette import StarletteIntegration
        integrations = [StarletteIntegration(), FastApiIntegration()]
        # AA-871 — uvicorn logs "Exception in ASGI application" for every unhandled 500; the ASGI integration already
        # captures that exception (with the request_id tag), so the log line would be a second event for the same
        # error.
        ignore_logger("uvicorn.error")
    sentry_sdk.init(
        dsn=dsn,
        integrations=integrations,
        environment=os.getenv("ENVIRONMENT", "dev"),
        release=release(),
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,  # AA-941: locals held the staff token and request bodies (see above)
        traces_sample_rate=0.1,
        profiles_sample_rate=0.1,
        before_send=before_send,
        before_breadcrumb=before_breadcrumb,
    )
    sentry_sdk.set_tag("service", service)
