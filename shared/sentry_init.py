"""Sentry error tracking for the CIS API and the job worker (AA-142, AA-870).

Lives in `shared/` so `python -m worker` can call it without importing `api` (whose package
`__init__` builds the whole FastAPI app). `api/core/sentry.py` re-exports it for the API.

Privacy (AA-870, S241 review P0-6): `send_default_pii=False`, no request bodies, no cookies, and
request headers reduced to an allowlist, so admin secrets, partner keys and session tokens in
custom headers (`x-admin-secret`, `x-api-key`, `x-tripplanner-key`, ...) never leave the task.
Breadcrumbs keep the AA-712 redaction of key-like URL query parameters.
"""
import logging
import os
import re

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


def before_send(event, hint):
    """Drop request bodies, cookies and non-allowlisted headers; redact key-like query strings."""
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
        from sentry_sdk.integrations.starlette import StarletteIntegration
        integrations = [StarletteIntegration(), FastApiIntegration()]
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
