"""Sentry error tracking for the CIS API and the job worker (AA-142, AA-870).

Lives in `shared/` so `python -m worker` can call it without importing `api` (whose package
`__init__` builds the whole FastAPI app). `api/core/sentry.py` re-exports it for the API.

Privacy (AA-870, S241 review P0-6): `send_default_pii=False`, no request bodies, no cookies, and
request headers reduced to an allowlist, so admin secrets, partner keys and session tokens in
custom headers (`x-admin-secret`, `x-api-key`, `x-tripplanner-key`, ...) never leave the task.
Breadcrumbs keep the AA-712 redaction of key-like URL query parameters.

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
    """Strip API keys from breadcrumb message / data (URLs, query strings)."""
    if "message" in crumb:
        crumb["message"] = _redact(crumb["message"])
    if "data" in crumb:
        crumb["data"] = _redact(crumb["data"])
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


def before_send(event, hint):
    """Drop request bodies, cookies and non-allowlisted headers; redact key-like query strings.
    AA-871: shape and throttle events made from log lines (``hint["log_record"]``)."""
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = {k: v for k, v in headers.items() if str(k).lower() in SAFE_HEADERS}
        for field in ("url", "query_string"):
            if field in request:
                request[field] = _redact(request[field])
    record = (hint or {}).get("log_record")
    if record is not None:
        try:
            if _throttled(_log_event_key(record)):
                return None
            if isinstance(record.msg, dict):
                _shape_log_event(event, record)
        except Exception:  # never lose the event over its shaping (an exception here would drop it)
            logger.debug("sentry_log_event_shape_failed", exc_info=True)
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
        traces_sample_rate=0.1,
        profiles_sample_rate=0.1,
        before_send=before_send,
        before_breadcrumb=before_breadcrumb,
    )
    sentry_sdk.set_tag("service", service)
