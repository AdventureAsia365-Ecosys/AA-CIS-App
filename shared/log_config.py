"""AA-871 — one JSON log format for the CIS API and the job worker.

Every line the process writes to stdout (CloudWatch log group ``/ecs/aa-cis-dev``, shared by api and worker) is one
JSON object, whichever way it was logged:

- ``structlog.get_logger().info("event_name", key=value)`` (most CIS code);
- stdlib ``logging.getLogger(__name__).info("text %s", x)`` (a few modules, uvicorn, libraries): the root handler's
  :class:`structlog.stdlib.ProcessorFormatter` renders them; ``event`` is the formatted message.

Fields on every line: ``timestamp`` (UTC ISO 8601), ``level``, ``logger``, ``event``, ``service`` (``cis-api`` |
``cis-worker``), ``environment``, ``git_sha``, plus what is bound in :mod:`structlog.contextvars`: ``request_id``
(request-id middleware, ``api/middleware/request_id.py``), ``tenant_id`` (``/v1/*`` rate limiter, once the tenant
JWT is verified), ``job_id`` / ``job_kind`` (job worker). Event names and messages are unchanged, so the
substring metric filters on the log group still match inside the JSON lines.

Called once per process: at import of ``api.main`` and in ``python -m worker``. Not on import in unit tests
(``ENVIRONMENT=test``); tests call :func:`configure_logging` with their own ``stream``.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from typing import IO, Any

import structlog

from shared.sentry_init import _redact, release

HANDLER_NAME = "aa-json"

# Third-party loggers that are chatty at INFO (httpx logs every request URL, query string included; botocore logs
# credential lookups). Before AA-871 nothing printed stdlib INFO at all (no root handler), so keeping them at WARNING
# keeps their volume as it was.
QUIET_LOGGERS = (
    "httpx", "httpx2", "httpcore", "botocore", "boto3", "s3transfer", "urllib3", "openai", "anthropic", "langfuse",
    "google", "googleapiclient", "asyncio",
)

# DM-6.4 safety net on top of "ids only in log lines": e-mail addresses and Bearer tokens anywhere in a value,
# plus the AA-712 key-like query parameters (``?apiKey=…``) handled by shared.sentry_init._redact.
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
# Credentials in a DSN / URL (``postgresql://user:pass@host``): user and password are replaced, the host is kept.
# Runs before the e-mail pattern, which would otherwise eat ``pass@host.tld`` and leave the user name.
_DSN_USERINFO = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
# ``Authorization: <anything>`` as header text (any scheme).
_AUTH_HEADER = re.compile(r"(?i)\b(authorization[\"']?\s*[:=]\s*[\"']?)[^\r\n\"',;}]+")
# ``password=…`` / ``secret: …`` / ``"client_secret": "…"`` key=value text.
_SECRET_KV = re.compile(
    r"(?i)\b((?:password|passwd|pwd|secret|client_secret)[\"']?\s*[:=]\s*[\"']?)[^\s&\"',;}]+")
# ``Basic <base64>``: the token must look like base64 (a digit, ``+/=``, or a lower→upper case change), so plain
# words after "basic" ("basic information") are kept.
_BASIC = re.compile(r"\b(Basic|BASIC|basic)\s+(?=[A-Za-z0-9+/]*(?:[0-9+/=]|[a-z][A-Z]))[A-Za-z0-9+/]{8,}={0,2}")


def _scrub_str(value: str) -> str:
    value = _DSN_USERINFO.sub(r"\1[Filtered]@", value)
    value = _AUTH_HEADER.sub(r"\1[Filtered]", value)
    value = _SECRET_KV.sub(r"\1[Filtered]", value)
    value = _BEARER.sub(r"\1 [Filtered]", value)
    value = _BASIC.sub(r"\1 [Filtered]", value)
    return _EMAIL.sub("[email]", _redact(value))


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return _scrub_str(value)
    if isinstance(value, dict):
        return {k: _scrub_value(v) for k, v in value.items()}
    if isinstance(value, tuple) and hasattr(value, "_fields"):  # NamedTuple: positional constructor
        return type(value)(*(_scrub_value(v) for v in value))
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub_value(v) for v in value)
    return value


def scrub_event(_logger: Any, _method: str, event_dict: dict) -> dict:
    """structlog processor: drop uvicorn's ANSI copy of the message, redact secrets / e-mails in every value.
    Never raises: logging must not fail because of a value it cannot rebuild (the line is then kept unscrubbed)."""
    try:
        event_dict.pop("color_message", None)
        if event_dict.get("message") == event_dict.get("event"):
            event_dict.pop("message")  # LogRecord.message, set when another handler formatted the record first
        return {k: (v if k.startswith("_") else _scrub_value(v)) for k, v in event_dict.items()}
    except Exception:
        return event_dict


def rename_domain_request_id(_logger: Any, _method: str, event_dict: dict) -> dict:
    """structlog processor, before ``merge_contextvars``: an explicit ``request_id=`` on a log call is the T8/T9
    angle-gate / content-writing request (a domain id), not the HTTP request. It is logged as ``acp_request_id`` so
    the HTTP ``request_id`` from the request-id middleware stays on the line."""
    if "request_id" in event_dict and "acp_request_id" not in event_dict:
        event_dict["acp_request_id"] = event_dict.pop("request_id")
    return event_dict


def _wrap_for_formatter(logger: Any, name: str, event_dict: dict):
    """``ProcessorFormatter.wrap_for_formatter`` that also hands ``exc_info`` to stdlib:
    ``log.error(..., exc_info=True)`` then sets ``LogRecord.exc_info``, so Sentry's logging integration attaches the
    traceback (it reads only the record). The JSON line is unchanged: the formatter still renders ``exc_info`` from
    the event dict."""
    args, kwargs = structlog.stdlib.ProcessorFormatter.wrap_for_formatter(logger, name, event_dict)
    exc_info = event_dict.get("exc_info")
    if exc_info:
        kwargs["exc_info"] = exc_info
    return args, kwargs


def _static_fields(service: str):
    fields = {
        "service": service,
        "environment": os.getenv("ENVIRONMENT", "dev"),
        "git_sha": release(),
    }

    def add(_logger: Any, _method: str, event_dict: dict) -> dict:
        for k, v in fields.items():
            event_dict.setdefault(k, v)
        return event_dict

    return add


def _level(level: int | str | None) -> int:
    if level is None:
        level = os.getenv("LOG_LEVEL", "INFO")
    if isinstance(level, str):
        value = getattr(logging, level.strip().upper(), None)
        return value if isinstance(value, int) else logging.INFO
    return level


def configure_logging(service: str, stream: IO[str] | None = None, level: int | str | None = None) -> None:
    """Install the JSON handler on the root logger and point structlog at it. Idempotent."""
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _static_fields(service),
    ]
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*shared, structlog.stdlib.ExtraAdder()],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            scrub_event,
            structlog.processors.JSONRenderer(default=str),
        ],
    )
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for h in list(root.handlers):
        # Ours from an earlier call, or a plain basicConfig StreamHandler. pytest's capture handlers are subclasses
        # of StreamHandler and are left alone, so caplog keeps working.
        if h.get_name() == HANDLER_NAME or type(h) is logging.StreamHandler:
            root.removeHandler(h)
    root.addHandler(handler)
    root.setLevel(_level(level))
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # uvicorn installs its own text handlers before it imports the app; route them through the JSON handler.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # The request-id middleware writes one `request_completed` line per request instead of uvicorn's access log
    # (which also carried the client IP).
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            rename_domain_request_id,
            *shared,
            structlog.stdlib.PositionalArgumentsFormatter(),
            # Scrubbed before the record leaves structlog too, so Sentry's logging integration (ERROR lines become
            # Sentry events) gets the redacted values.
            scrub_event,
            _wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


_process_service: str | None = None


def configure_for_process(service: str) -> None:
    """The process entrypoints' call: configure, except under unit tests (``ENVIRONMENT=test``), where importing
    ``api.main`` must not swap the global logging set-up of the whole test session.

    The first call of a process wins: the worker entrypoint configures ``cis-worker`` and later imports modules that
    import ``api.main`` (which asks for ``cis-api`` at import), so live worker lines said ``service: cis-api``."""
    global _process_service
    if os.getenv("ENVIRONMENT") == "test" or _process_service is not None:
        return
    _process_service = service
    configure_logging(service)
