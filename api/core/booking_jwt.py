"""AA-680 — CIS as the staff identity provider for AA-Booking (root ADR 0004, O1).

CIS mints a short-lived RS256 JWT (`aud=booking`) for a signed-in staff user and publishes the
public half as a JWKS. AA-Booking verifies the token offline against that JWKS; it never sees the
private key and never shares CIS's HS256 `JWT_SECRET`.

Key material lives in Secrets Manager (name from `BOOKING_JWT_SECRET_NAME`, default
`aa-cis/dev/booking-jwt-signing-key`) as JSON:

    {"kid": "bk-2026-10",
     "private_key_pem": "-----BEGIN PRIVATE KEY-----...",
     "previous": [{"kid": "bk-2026-07", "public_key_pem": "-----BEGIN PUBLIC KEY-----..."}]}

`previous` keeps old public keys in the JWKS through a rotation so tokens signed just before the
switch still verify. The secret is loaded lazily with a TTL cache; a missing or malformed secret
raises `SigningKeyUnavailable` (the endpoints turn it into 503) and never stops the app starting.
"""
from __future__ import annotations

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
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-1"
    client = boto3.client("secretsmanager", region_name=region)
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
    for entry in data.get("previous") or []:
        try:
            prev_kid = str(entry["kid"]).strip()
            prev_pub = serialization.load_pem_public_key(entry["public_key_pem"].encode())
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise SigningKeyUnavailable(
                f"booking signing secret has a malformed previous key: {type(exc).__name__}"
            ) from exc
        if not isinstance(prev_pub, RSAPublicKey):
            raise SigningKeyUnavailable("previous booking signing key must be an RSA public key")
        if prev_kid and prev_kid != kid:
            public_keys.append((prev_kid, prev_pub))
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
    return _cache.get()


def reset_key_cache() -> None:
    """Tests and a forced rotation reload."""
    _cache.reset()


def mint_booking_token(*, admin_id: str, username: str, role: str,
                       now: datetime | None = None) -> tuple[str, int]:
    """Sign a Booking access token for one staff user. Returns (token, expires_in seconds)."""
    keys = get_signing_keys()
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
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(claims, keys.private_key, algorithm=BOOKING_JWT_ALG,
                       headers={"kid": keys.kid, "typ": "JWT"})
    return token, BOOKING_JWT_TTL_S


def jwks() -> dict:
    """The public JWKS document: current key first, then any previous keys still in rotation."""
    keys = get_signing_keys()
    out = []
    for kid, pub in keys.public_keys:
        jwk = RSAAlgorithm.to_jwk(pub, as_dict=True)
        jwk.update({"kid": kid, "use": "sig", "alg": BOOKING_JWT_ALG})
        out.append(jwk)
    return {"keys": out}
