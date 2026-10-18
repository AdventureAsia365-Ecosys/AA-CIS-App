"""Multi-issuer RS256 access-token verifier (root ADR 0006 §8, AA-816 / AA-854).

One module, driven only by configuration. Each accepted issuer is an :class:`IssuerConfig`; a
:class:`MultiIssuerVerifier` holds the exact set of issuers (and, per issuer, the exact client ids) that
one route class accepts, e.g. CIS staff routes = WORKFORCE x {cis-admin, breakglass}.

Rules (ADR 0006 §8, numbered as there):

1. RS256 only. ``none``, ``HS*`` and every other ``alg`` are rejected before any key is looked up. The
   ``jku`` / ``x5u`` / ``jwk`` / ``x5c`` headers are never read: keys come only from the issuer's own JWKS.
2. The issuer entry is chosen by the *unverified* ``iss``; only that issuer's key set is consulted, so a
   ``kid`` is never looked up across issuers. Unknown ``iss`` -> :class:`InvalidToken`.
3. Keys are cached by ``kid`` per issuer. An unknown ``kid`` triggers a refetch at most once per
   :data:`UNKNOWN_KID_REFETCH_S`; a failed fetch keeps the last good key set, and a stale set that holds the
   ``kid`` is served without waiting while a refresh is in flight or not yet due. :meth:`prefetch` loads every
   issuer's keys at start-up (the JWKS is reached through the single NAT instance). JWKS redirects are refused.
4. Signature, ``exp`` (leeway <= :data:`MAX_LEEWAY_S`), ``iss``, ``token_use`` and the audience claim are
   checked. For Cognito access tokens the audience claim is ``client_id`` and there is no ``aud``.
5. Access tokens only (``token_use == "access"``); ID tokens are rejected.

Errors: :class:`InvalidToken` means "this token is not acceptable" (HTTP 401). :class:`AuthUnavailable`
means "no key set could ever be loaded for this issuer" (HTTP 503), never "invalid token".

This file is meant to be copied byte-identical into each app repo (ADR 0006 §8 rule 9), so it uses only
the standard library plus PyJWT[crypto]; app-specific values live in each app's own config module
(CIS: ``api/core/identity_config.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import jwt

log = logging.getLogger(__name__)

ALGORITHM = "RS256"
DEFAULT_LEEWAY_S = 30
MAX_LEEWAY_S = 60
JWKS_TTL_S = 3600.0
UNKNOWN_KID_REFETCH_S = 60.0
COLD_RETRY_S = 5.0
FETCH_TIMEOUT_S = 5.0
MAX_JWKS_BYTES = 256 * 1024
MAX_TOKEN_CHARS = 8192
MAX_SUB_CHARS = 128
MAX_TEXT_CHARS = 320

JwksFetcher = Callable[[str], Awaitable[dict[str, Any]]]


class InvalidToken(Exception):
    """The token is missing, malformed, expired, from an unknown issuer/client or badly signed (HTTP 401)."""


class AuthUnavailable(Exception):
    """No key set has ever been loaded for the token's issuer (HTTP 503)."""


@dataclass(frozen=True)
class Principal:
    """Who the verified token belongs to. Authorisation (role, tenant, ownership) is the caller's job.

    ``iss`` is the configured issuer (ADR 0006 §8 rule 7 calls it ``issuer``). ``email_verified`` is None when
    the token does not carry it (WORKFORCE access tokens today); only EXTERNAL tokens are expected to.
    """

    iss: str
    sub: str
    client_id: str
    groups: tuple[str, ...]
    email: str | None
    email_verified: bool | None
    username: str | None


@dataclass(frozen=True)
class IssuerConfig:
    """One accepted issuer and the client ids (audiences) a route class accepts from it.

    Cognito profile (default): ``audience_claim="client_id"``, ``token_use="access"``,
    ``groups_claim="cognito:groups"``. Bridge profile (CIS-minted Booking token, PR #721):
    ``audience_claim="aud"``, ``token_use=None``, ``groups_claim="cis_role"``.
    """

    issuer: str
    client_ids: frozenset[str]
    jwks_url: str = ""
    audience_claim: str = "client_id"
    token_use: str | None = "access"
    groups_claim: str = "cognito:groups"
    leeway_s: int = DEFAULT_LEEWAY_S
    name: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not self.issuer.startswith("https://") or self.issuer.endswith("/"):
            raise ValueError(f"issuer must be an https URL without a trailing slash: {self.issuer!r}")
        if not self.jwks_url:
            object.__setattr__(self, "jwks_url", f"{self.issuer}/.well-known/jwks.json")
        if not self.jwks_url.startswith("https://"):
            raise ValueError(f"jwks_url must be https: {self.jwks_url!r}")
        client_ids = frozenset(c for c in self.client_ids if isinstance(c, str) and c)
        if not client_ids:
            raise ValueError(f"issuer {self.issuer!r} needs at least one allowed client id")
        object.__setattr__(self, "client_ids", client_ids)
        if self.audience_claim not in ("client_id", "aud"):
            raise ValueError(f"audience_claim must be 'client_id' or 'aud': {self.audience_claim!r}")
        if not 0 <= self.leeway_s <= MAX_LEEWAY_S:
            raise ValueError(f"leeway_s must be between 0 and {MAX_LEEWAY_S}: {self.leeway_s}")


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """A JWKS URL never redirects; following one could leave https or the issuer's own host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201 - urllib signature
        raise urllib.error.HTTPError(req.full_url, code, "JWKS redirect refused", headers, fp)


def http_fetcher(timeout: float = FETCH_TIMEOUT_S) -> JwksFetcher:
    """GET a JWKS URL with the standard library in a worker thread (no extra HTTP client dependency)."""
    opener = urllib.request.build_opener(_RefuseRedirects)

    def _get(url: str) -> dict[str, Any]:
        if not url.startswith("https://"):
            raise ValueError("JWKS URL must be https")
        request = urllib.request.Request(url, headers={"Accept": "application/json"})  # noqa: S310 - https checked
        with opener.open(request, timeout=timeout) as response:  # noqa: S310
            if response.geturl() != url:
                raise ValueError("JWKS response came from another URL")
            body = response.read(MAX_JWKS_BYTES + 1)
        if len(body) > MAX_JWKS_BYTES:
            raise ValueError("JWKS response too large")
        return json.loads(body)

    async def fetch(url: str) -> dict[str, Any]:
        return await asyncio.to_thread(_get, url)

    return fetch


def _rsa_keys(document: Any) -> dict[str, Any]:
    """kid -> public key for every RSA RS256 signing key in a JWKS document; other keys are skipped."""
    keys: dict[str, Any] = {}
    entries = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        raise ValueError("JWKS document has no 'keys' list")
    for jwk in entries:
        if not isinstance(jwk, dict) or jwk.get("kty") != "RSA" or not isinstance(jwk.get("kid"), str):
            continue
        if jwk.get("use", "sig") != "sig" or jwk.get("alg", ALGORITHM) != ALGORITHM:
            continue
        try:
            keys[jwk["kid"]] = jwt.PyJWK(jwk, algorithm=ALGORITHM).key
        except jwt.PyJWTError as exc:
            log.warning("JWKS: skipping key %s (%s)", jwk["kid"], type(exc).__name__)
    return keys


class JwksCache:
    """The key set of ONE issuer. Never shared across issuers (rule 2)."""

    def __init__(
        self,
        url: str,
        fetcher: JwksFetcher,
        *,
        ttl: float = JWKS_TTL_S,
        unknown_kid_refetch: float = UNKNOWN_KID_REFETCH_S,
        cold_retry: float = COLD_RETRY_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._fetcher = fetcher
        self._ttl = ttl
        self._unknown_kid_refetch = unknown_kid_refetch
        self._cold_retry = cold_retry
        self._clock = clock
        self._keys: dict[str, Any] | None = None
        self._fetched_at = 0.0
        self._attempted_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def key_count(self) -> int:
        return len(self._keys or {})

    def _may_fetch(self, now: float) -> bool:
        if self._attempted_at is None:
            return True
        interval = self._cold_retry if self._keys is None else self._unknown_kid_refetch
        return now - self._attempted_at >= interval

    async def refresh(self) -> bool:
        """Fetch the key set now. On any failure keep the last good set and return False."""
        self._attempted_at = self._clock()
        try:
            keys = _rsa_keys(await self._fetcher(self._url))
        except Exception as exc:  # noqa: BLE001 - any fetch/parse failure keeps the last good key set
            log.warning("JWKS fetch failed for %s (%s); keeping %s cached key(s)",
                        self._url, type(exc).__name__, self.key_count)
            return False
        if not keys:
            log.warning("JWKS for %s has no usable RS256 key; keeping %s cached key(s)", self._url, self.key_count)
            return False
        self._keys = keys
        self._fetched_at = self._clock()
        return True

    def _fresh_hit(self, kid: str, now: float) -> Any:
        if self._keys is not None and now - self._fetched_at < self._ttl:
            return self._keys.get(kid)
        return None

    async def key(self, kid: str) -> Any:
        now = self._clock()
        hit = self._fresh_hit(kid, now)
        if hit is not None:
            return hit
        # A stale set still holding the kid: serve it without waiting while a refresh is in flight or not due
        # yet, so a slow or failing JWKS costs at most one request per window, not every request.
        stale_hit = self._keys.get(kid) if self._keys is not None else None
        if stale_hit is not None and (self._lock.locked() or not self._may_fetch(now)):
            return stale_hit
        async with self._lock:
            now = self._clock()
            hit = self._fresh_hit(kid, now)
            if hit is not None:
                return hit
            stale = self._keys is None or now - self._fetched_at >= self._ttl
            if (stale or kid not in self._keys) and self._may_fetch(now):
                await self.refresh()
            if self._keys is None:
                raise AuthUnavailable("JWKS unavailable")
            if kid not in self._keys:
                raise InvalidToken("unknown signing key")
            return self._keys[kid]


class MultiIssuerVerifier:
    """Verifies access tokens for one route class: the exact (issuer, client_id) pairs it was built with."""

    def __init__(
        self,
        issuers: Iterable[IssuerConfig],
        fetcher: JwksFetcher | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        fetch = fetcher or http_fetcher()
        self._issuers: dict[str, IssuerConfig] = {}
        self._caches: dict[str, JwksCache] = {}
        for cfg in issuers:
            if cfg.issuer in self._issuers:
                raise ValueError(f"issuer configured twice: {cfg.issuer!r}")
            self._issuers[cfg.issuer] = cfg
            self._caches[cfg.issuer] = JwksCache(cfg.jwks_url, fetch, clock=clock)
        if not self._issuers:
            raise ValueError("at least one issuer is required")

    @property
    def issuers(self) -> tuple[IssuerConfig, ...]:
        return tuple(self._issuers.values())

    async def prefetch(self) -> dict[str, int]:
        """Load every issuer's key set (start-up hook). Never raises; returns issuer -> cached key count."""
        result: dict[str, int] = {}
        for iss, cache in self._caches.items():
            await cache.refresh()
            result[iss] = cache.key_count
        return result

    async def verify(self, token: str) -> Principal:
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
            raise InvalidToken("malformed token")
        try:
            header = jwt.get_unverified_header(token)
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as exc:
            raise InvalidToken("malformed token") from exc
        # Rule 1: pin the algorithm before touching any key. jku/x5u/jwk/x5c are never read.
        if header.get("alg") != ALGORITHM:
            raise InvalidToken("unexpected algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InvalidToken("missing kid")
        # Rule 2: the unverified iss only selects which issuer's own key set to use.
        iss = unverified.get("iss") if isinstance(unverified, dict) else None
        cfg = self._issuers.get(iss) if isinstance(iss, str) else None
        if cfg is None:
            raise InvalidToken("unknown issuer")
        key = await self._caches[cfg.issuer].key(kid)

        required = ["exp", "iss", "sub", cfg.audience_claim]
        if cfg.token_use is not None:
            required.append("token_use")
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[ALGORITHM],
                issuer=cfg.issuer,
                audience=sorted(cfg.client_ids) if cfg.audience_claim == "aud" else None,
                leeway=cfg.leeway_s,
                options={
                    "require": required,
                    "verify_aud": cfg.audience_claim == "aud",
                    "enforce_minimum_key_length": True,
                },
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(type(exc).__name__) from exc

        if cfg.token_use is not None and claims.get("token_use") != cfg.token_use:
            raise InvalidToken("wrong token_use")
        client_id = _audience(claims.get(cfg.audience_claim), cfg)
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub.strip() or len(sub) > MAX_SUB_CHARS:
            raise InvalidToken("invalid sub")
        return Principal(
            iss=cfg.issuer,
            sub=sub,
            client_id=client_id,
            groups=_groups(claims.get(cfg.groups_claim)),
            email=_text(claims.get("email")),
            email_verified=_flag(claims.get("email_verified")),
            username=_text(claims.get("username")),
        )


def _audience(value: Any, cfg: IssuerConfig) -> str:
    """The allowed client id / audience the token was issued for, or InvalidToken."""
    if cfg.audience_claim == "client_id":
        if isinstance(value, str) and value in cfg.client_ids:
            return value
        raise InvalidToken("client not allowed")
    candidates = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    for candidate in candidates:
        if isinstance(candidate, str) and candidate in cfg.client_ids:
            return candidate
    raise InvalidToken("audience not allowed")


def _groups(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    return tuple(g for g in value if isinstance(g, str) and g)


def _text(value: Any) -> str | None:
    """The stripped string, or None. Over-long values are dropped, never cut: callers may match on them."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > MAX_TEXT_CHARS:
        return None
    return value


def _flag(value: Any) -> bool | None:
    """A boolean claim. Cognito pre token triggers emit "true"/"false" strings (EXTERNAL ``email_verified``),
    so exactly those two strings are accepted besides real booleans; anything else is None (unknown)."""
    if isinstance(value, bool):
        return value
    if value == "true":
        return True
    if value == "false":
        return False
    return None
