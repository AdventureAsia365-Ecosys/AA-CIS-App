import logging
import os
import re

import boto3
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

logger = logging.getLogger(__name__)


def _get_dsn() -> str:
    try:
        client = boto3.client(
            "secretsmanager",
            region_name=os.environ.get("AWS_REGION", "us-west-1"),
        )
        secret = client.get_secret_value(SecretId="aa-cis/dev/sentry-dsn-api")
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


def init_sentry() -> None:
    dsn = _get_dsn()
    if not dsn:
        logging.warning("Sentry DSN not found — error tracking disabled")
        return
    sentry_sdk.init(
        dsn=dsn,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        environment=os.getenv("ENVIRONMENT", "dev"),
        traces_sample_rate=0.1,
        profiles_sample_rate=0.1,
        release=os.getenv("VERCEL_GIT_COMMIT_SHA", "unknown"),
        before_breadcrumb=before_breadcrumb,
    )
