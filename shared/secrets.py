"""
shared/secrets.py — Secrets Manager via boto3 SDK
Fetches secrets from AWS Secrets Manager with in-memory TTL cache.
Fallback to env vars for local dev / ECS (which uses task def secret injection).
"""
import json
import os
import time
from functools import lru_cache
from typing import Optional
from urllib.parse import parse_qsl, quote, urlsplit

import structlog

logger = structlog.get_logger()

_cache: dict = {}
_TTL = 3600  # 1 hour refresh


def _fetch_secret_sdk(secret_arn: str) -> str:
    """Fetch secret directly via boto3 SDK."""
    import boto3
    region = os.environ.get("AWS_DEFAULT_REGION", "us-west-1")
    sm = boto3.client("secretsmanager", region_name=region)
    value = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
    logger.info("secret.fetched", arn=secret_arn[-20:])
    return value


def _get_cached(key: str, fetch_fn) -> str:
    now = time.time()
    if key not in _cache or now - _cache.get(f"{key}_ts", 0) > _TTL:
        _cache[key] = fetch_fn()
        _cache[f"{key}_ts"] = now
    return _cache[key]


# AA-838 D9 (data standards C1.1a): every CIS connection names itself in pg_stat_activity / Performance
# Insights. The named pools pass server_settings={"application_name": ...} (cis-api, cis-tenant, cis-worker,
# cis-job), which wins over the DSN. Every other connection built from get_database_url() — the one-off
# asyncpg.connect() calls in routers, job handlers and the call-log writers — showed up with an empty name
# (the first capacity review saw them as "(empty)", ~3rd by load). They now carry this default.
DEFAULT_DB_APPLICATION_NAME = "cis-direct"


def with_application_name(dsn: str, name: Optional[str] = None) -> str:
    """Return `dsn` with an `application_name` query parameter (asyncpg and libpq both read it).

    Pure. Leaves the DSN untouched when it already names one, or when it is not a postgres URL (a libpq
    key=value string). Only appends to the query, so the user/password/host part is never re-encoded.
    `name` defaults to env CIS_DB_APPLICATION_NAME, then DEFAULT_DB_APPLICATION_NAME."""
    s = dsn.strip()
    if not s.startswith(("postgres://", "postgresql://")):
        return dsn
    base, _, fragment = s.partition("#")
    query = urlsplit(base).query
    if any(k == "application_name" for k, _ in parse_qsl(query, keep_blank_values=True)):
        return s
    name = name or os.environ.get("CIS_DB_APPLICATION_NAME") or DEFAULT_DB_APPLICATION_NAME
    sep = "&" if query else ("" if base.endswith("?") else "?")
    out = f"{base}{sep}application_name={quote(name, safe='')}"
    return f"{out}#{fragment}" if fragment else out


def get_database_url() -> str:
    """Get DATABASE_URL — Secrets Manager if ARN set, else env var (ECS/local) — with a default
    application_name (AA-838 D9, `with_application_name`)."""
    arn = os.environ.get("SECRET_DB_ARN")
    if not arn:
        return with_application_name(os.environ["DATABASE_URL"])
    return with_application_name(_get_cached("db_url", lambda: _fetch_secret_sdk(arn)))


def get_dataforseo_creds() -> tuple[str, str]:
    """Get DataForSEO login + password from Secrets Manager or env."""
    arn = os.environ.get("SECRET_DATAFORSEO_ARN")
    if not arn:
        return (
            os.environ.get("DATAFORSEO_LOGIN", ""),
            os.environ.get("DATAFORSEO_PASSWORD", ""),
        )

    def fetch():
        raw = _fetch_secret_sdk(arn)
        data = json.loads(raw)
        return json.dumps(data)

    raw = _get_cached("dataforseo", fetch)
    data = json.loads(raw)
    return data["login"], data["password"]


def get_typesafe_api_key() -> str:
    """AA-660 — Jev (TypeSafe) API key. Env `TYPESAFE_API_KEY` for local runs; otherwise the Secrets
    Manager secret named by `SECRET_TYPESAFE_ID` (default `aa-cis/dev/typesafe`). The secret may hold
    the bare key or a JSON object with `api_key`. Raises if neither is available — callers (decide.py)
    turn that into zone='error' and keep their existing rule."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    secret_id = os.environ.get("SECRET_TYPESAFE_ID", "aa-cis/dev/typesafe")

    def fetch():
        raw = _fetch_secret_sdk(secret_id).strip()
        if raw.startswith("{"):
            return json.loads(raw)["api_key"]
        return raw

    return _get_cached("typesafe", fetch)


def get_geoapify_key() -> str:
    """AA-712 — Geoapify API key (GEOCODER_PROVIDER=geoapify, the default since S236). Env
    `GEOAPIFY_API_KEY` for local runs; otherwise the Secrets Manager secret named by `SECRET_GEOAPIFY_ID`
    (default `aa-cis/dev/geoapify`, a plain string; a JSON object with `api_key` is also accepted). The
    worker task role needs secretsmanager:GetSecretValue on it (AA-CIS-Infra). Never log the value.
    Raises if neither is available — the geocode call fails and the names stay pending."""
    key = os.environ.get("GEOAPIFY_API_KEY")
    if key:
        return key.strip()
    secret_id = os.environ.get("SECRET_GEOAPIFY_ID", "aa-cis/dev/geoapify")

    def fetch():
        raw = _fetch_secret_sdk(secret_id).strip()
        if raw.startswith("{"):
            return json.loads(raw)["api_key"].strip()
        return raw

    return _get_cached("geoapify", fetch)
