"""AA-680 — CIS as the staff identity provider for AA-Booking (root ADR 0004, O1).

CIS mints a short-lived RS256 JWT (`aud=booking`) for a signed-in staff user and publishes the
public half as a JWKS. AA-Booking verifies the token offline against that JWKS; it never sees the
private key and never shares CIS's HS256 `JWT_SECRET`.

Key material lives in Secrets Manager (name from `BOOKING_JWT_SECRET_NAME`, default
`aa-cis/dev/booking-jwt-signing-key`) as JSON:

    {"kid": "bk-2026-10",
     "private_key_pem": "-----BEGIN PRIVATE KEY-----...",
     "previous": [{"kid": "bk-2026-07", "public_key_pem": "-----BEGIN PUBLIC KEY-----..."}],
     "next": [{"kid": "bk-2027-01", "public_key_pem": "-----BEGIN PUBLIC KEY-----..."}]}

`previous` keeps old public keys in the JWKS through a rotation so tokens signed just before the
switch still verify; the optional `next` pre-publishes the key that will sign after the switch, so
every verifier's JWKS already holds it before the first token signed with it appears. Both are
public-only and never sign. The secret is loaded lazily with a TTL cache; a missing or malformed
secret raises `SigningKeyUnavailable` (the endpoints turn it into 503) and never stops the app
starting.

The Secrets Manager read is blocking (boto3). Async callers must go through
`get_signing_keys_async()` (runs it in a worker thread) and pass the keys to `mint_booking_token` /
`jwks`, so a slow or unreachable Secrets Manager never stalls the event loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt  # PyJWT
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from jwt.algorithms import RSAAlgorithm

logger = structlog.get_logger()

BOOKING_JWT_ISSUER = "https://api-cis.lumiguides.it.com"
BOOKING_JWT_AUDIENCE = "booking"
BOOKING_JWT_ALG = "RS256"
BOOKING_JWT_TTL_S = 900  # 15 minutes
DEFAULT_SECRET_NAME = "aa-cis/dev/booking-jwt-signing-key"

_KEY_TTL_S = 600        # re-read the secret every 10 min (picks up a rotation without a deploy)
_FAILURE_TTL_S = 30     # after a failed load, wait this long before asking Secrets Manager again
# Bound a Secrets Manager stall: boto3 defaults (60 s connect + 60 s read, several retries) could
# hold a worker thread — and every caller waiting on the cache lock — for minutes.
_SM_CONNECT_TIMEOUT_S = 2
_SM_READ_TIMEOUT_S = 3
_SM_MAX_ATTEMPTS = 2


class SigningKeyUnavailable(Exception):
    """The Booking signing key could not be loaded (secret missing, unreadable or malformed)."""


@dataclass(frozen=True)
class SigningKeys:
    kid: str
    private_key: RSAPrivateKey
    # Every public key to publish, current first: [(kid, public_key), ...]
    public_keys: tuple[tuple[str, RSAPublicKey], ...]


def secret_name() -> str:
    return os.environ.get("BOOKING_JWT_SECRET_NAME") or DEFAULT_SECRET_NAME


def _fetch_secret_string(name: str) -> str:
    import boto3
    from botocore.config import Config
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-1"
    client = boto3.client("secretsmanager", region_name=region, config=Config(
        connect_timeout=_SM_CONNECT_TIMEOUT_S, read_timeout=_SM_READ_TIMEOUT_S,
        retries={"max_attempts": _SM_MAX_ATTEMPTS, "mode": "standard"},
    ))
    return client.get_secret_value(SecretId=name)["SecretString"]


def parse_signing_secret(raw: str) -> SigningKeys:
    """Parse the secret JSON into keys. Raises SigningKeyUnavailable on any shape problem."""
    try:
        data = json.loads(raw)
        kid = str(data["kid"]).strip()
        private_key = serialization.load_pem_private_key(data["private_key_pem"].encode(), password=None)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SigningKeyUnavailable(f"booking signing secret is malformed: {type(exc).__name__}") from exc
    if not kid:
        raise SigningKeyUnavailable("booking signing secret has an empty kid")
    if not isinstance(private_key, RSAPrivateKey):
        raise SigningKeyUnavailable("booking signing key must be an RSA private key")

    public_keys: list[tuple[str, RSAPublicKey]] = [(kid, private_key.public_key())]
    seen = {kid}
    # A rotation mistake (blank or duplicate kid) must fail loudly: silently dropping a published key
    # would break every in-flight token signed with it. The cache keeps serving the last good keys.
    for field in ("previous", "next"):
        for entry in data.get(field) or []:
            try:
                extra_kid = str(entry["kid"]).strip()
                extra_pub = serialization.load_pem_public_key(entry["public_key_pem"].encode())
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise SigningKeyUnavailable(
                    f"booking signing secret has a malformed {field} key: {type(exc).__name__}"
                ) from exc
            if not isinstance(extra_pub, RSAPublicKey):
                raise SigningKeyUnavailable(f"{field} booking signing key must be an RSA public key")
            if not extra_kid:
                raise SigningKeyUnavailable(f"booking signing secret has a {field} key with an empty kid")
            if extra_kid in seen:
                raise SigningKeyUnavailable(
                    f"booking signing secret repeats kid {extra_kid!r} (in {field})"
                )
            seen.add(extra_kid)
            public_keys.append((extra_kid, extra_pub))
    return SigningKeys(kid=kid, private_key=private_key, public_keys=tuple(public_keys))


class _KeyCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: SigningKeys | None = None
        self._loaded_at = 0.0
        self._failed_at = 0.0
        self._error = ""

    def reset(self) -> None:
        with self._lock:
            self._keys, self._loaded_at, self._failed_at, self._error = None, 0.0, 0.0, ""

    def get(self) -> SigningKeys:
        now = time.monotonic()
        with self._lock:
            if self._keys is not None and now - self._loaded_at < _KEY_TTL_S:
                return self._keys
            if self._failed_at and now - self._failed_at < _FAILURE_TTL_S:
                # Keep serving the last good key through a transient Secrets Manager blip.
                if self._keys is not None:
                    return self._keys
                raise SigningKeyUnavailable(self._error)
            name = secret_name()
            try:
                keys = parse_signing_secret(_fetch_secret_string(name))
            except SigningKeyUnavailable as exc:
                self._failed_at, self._error = now, str(exc)
            except Exception as exc:  # boto3 ClientError, NoCredentialsError, network ...
                self._failed_at = now
                self._error = f"booking signing secret {name!r} could not be read: {type(exc).__name__}"
            else:
                self._keys, self._loaded_at, self._failed_at, self._error = keys, now, 0.0, ""
                return keys
            logger.error("booking_jwt_key_unavailable", secret=name, error=self._error)
            if self._keys is not None:
                return self._keys
            raise SigningKeyUnavailable(self._error)


_cache = _KeyCache()


def get_signing_keys() -> SigningKeys:
    """Blocking (may call Secrets Manager). From async code use `get_signing_keys_async`."""
    return _cache.get()


async def get_signing_keys_async() -> SigningKeys:
    """Load the keys off the event loop (the cache refresh is a blocking boto3 call)."""
    return await asyncio.to_thread(_cache.get)


def reset_key_cache() -> None:
    """Tests and a forced rotation reload."""
    _cache.reset()


def mint_booking_token(*, admin_id: str, username: str, role: str,
                       now: datetime | None = None, jti: str | None = None,
                       keys: SigningKeys | None = None) -> tuple[str, int]:
    """Sign a Booking access token for one staff user. Returns (token, expires_in seconds).

    Pass `keys` from `get_signing_keys_async()` when calling from async code; without it the keys
    are loaded synchronously (fine in tests and scripts)."""
    keys = keys or get_signing_keys()
    now = now or datetime.now(timezone.utc)
    claims = {
        "iss": BOOKING_JWT_ISSUER,
        "aud": BOOKING_JWT_AUDIENCE,
        "sub": admin_id,
        "name": username,
        "preferred_username": username,
        "cis_role": role,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(seconds=BOOKING_JWT_TTL_S),
        "jti": jti or str(uuid.uuid4()),
    }
    token = jwt.encode(claims, keys.private_key, algorithm=BOOKING_JWT_ALG,
                       headers={"kid": keys.kid, "typ": "JWT"})
    return token, BOOKING_JWT_TTL_S


def jwks(keys: SigningKeys | None = None) -> dict:
    """The public JWKS document: current key first, then previous, then next (pre-published)."""
    keys = keys or get_signing_keys()
    out = []
    for kid, pub in keys.public_keys:
        jwk = RSAAlgorithm.to_jwk(pub, as_dict=True)
        jwk.update({"kid": kid, "use": "sig", "alg": BOOKING_JWT_ALG})
        out.append(jwk)
    return {"keys": out}
