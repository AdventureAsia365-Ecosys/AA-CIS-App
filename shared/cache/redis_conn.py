"""Redis connection settings from env (AA-874): one place for the URL and the TLS / AUTH kwargs.

Env (all optional):
  REDIS_HOST        host name; default "localhost" (no infra host name in this public repo, DM-6.10)
  REDIS_PORT        default 6379
  REDIS_TLS         "true" / "1" / "yes" / "on" -> scheme `rediss`, server certificate verified against the
                    system CA store, host name checked
  REDIS_AUTH_TOKEN  the ElastiCache AUTH token, injected by ECS from Secrets Manager. Passed as the
                    `password=` kwarg, never put into the URL, so a logged URL cannot carry it.

With neither REDIS_TLS nor REDIS_AUTH_TOKEN the result is exactly the old `redis://<host>:6379` with no extra
kwargs, so the same image runs against the old plain node and the new TLS + AUTH replication group; only the
task definition decides. ElastiCache accepts AUTH only over TLS, so a token without REDIS_TLS logs one ERROR
(`aa874_redis_auth_without_tls`) and the token is dropped: sending AUTH to a node without AUTH fails every
command, whereas a plain connection to the old node still works.

Lazy and I/O-free: callers pass the result straight to `redis.asyncio.from_url(url, **kwargs)`.
"""
from __future__ import annotations

import os

import structlog

logger = structlog.get_logger()

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 6379
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

_auth_without_tls_logged = False


def _env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in _TRUE_VALUES


def redis_conn_args() -> tuple[str, dict]:
    """(url, kwargs) for redis.asyncio.from_url, built from REDIS_HOST / REDIS_PORT / REDIS_TLS /
    REDIS_AUTH_TOKEN (see the module docstring). The URL never contains the token."""
    global _auth_without_tls_logged
    host = (os.environ.get("REDIS_HOST") or "").strip() or DEFAULT_HOST
    port = int((os.environ.get("REDIS_PORT") or "").strip() or DEFAULT_PORT)
    tls = _env_flag("REDIS_TLS")
    token = os.environ.get("REDIS_AUTH_TOKEN") or None

    kwargs: dict = {}
    if tls:
        # Explicit, so the behaviour does not depend on the redis-py version (ssl_check_hostname
        # defaults to False on 5.x and True on 6+). No ssl_ca_certs: the system CA store verifies the
        # ElastiCache certificate.
        kwargs["ssl_cert_reqs"] = "required"
        kwargs["ssl_check_hostname"] = True
        if token:
            kwargs["password"] = token
    elif token and not _auth_without_tls_logged:
        _auth_without_tls_logged = True
        logger.error("aa874_redis_auth_without_tls", host=host,
                     action="token ignored; set REDIS_TLS=true for an AUTH-enabled node")

    scheme = "rediss" if tls else "redis"
    return f"{scheme}://{host}:{port}", kwargs
