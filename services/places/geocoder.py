"""AA-712 — geocoder adapter (design §4; provider decision S236: Geoapify for every country).

Method (TripPlanner locate.py, S202 lesson "geocoding by name alone is wrong for Asian names"): the
model proposes [lat, lng, country] per name (services/places/locate.py); a geocoder only CONFIRMS
that point. When nothing confirms, the model point is kept (located_by 'llm', 'area' precision).

Providers (`GEOCODER_PROVIDER`, default `geoapify`):
  geoapify          Geoapify /v1/geocode/search (OpenStreetMap data), the confirming geocoder for ALL
                    countries (S236 bake-off: 38/64 named-or-related vs 14/64 for Amazon Location
                    Geocode). Free plan: 3,000 credits/day, 1 credit per request, ≤ 5 req/s, results may
                    be stored. Attribution is REQUIRED wherever the coordinates are shown:
                    "Powered by Geoapify" (follow-link) + "© OpenStreetMap contributors" (ATTRIBUTION).
                    Acceptance rule: services/places/geomatch.py. Key: Secrets Manager
                    `aa-cis/dev/geoapify` (plain string) via shared.secrets.get_geoapify_key().
  model_only        no external call: every point is the model's ('llm'). Tests / emergency switch.
  mapbox_permanent  NOT SUPPORTED (S236): Mapbox is the basemap only — Product Terms §2.7.3 forbid the
                    public-map use of stored Permanent results we need.
  amazon_location   NOT SUPPORTED (S236): no coverage for BT/CN/MN/KR, Geocode found 1/29 hard places,
                    Japan results may not be stored.
The two unsupported names are kept so a stale task-definition value fails loudly
(GeocoderNotSupported) instead of silently running without a geocoder. Any other unknown value falls
back to model_only (logged) — a typo must never start paid calls.

The API key is a URL query parameter (Geoapify has no header auth), so: no exception text or log line
from this module carries the request URL; HTTP errors are re-raised as GeocoderError with the status
only; the `httpx` logger (which logs request URLs at INFO) gets a redacting filter.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol, Union

import structlog

from services.places import geomatch, guards

logger = structlog.get_logger()

GEOAPIFY_URL = "https://api.geoapify.com/v1/geocode/search"
ATTRIBUTION = "Powered by Geoapify | © OpenStreetMap contributors"
ATTRIBUTION_LINKS = {"geoapify": "https://www.geoapify.com/", "osm": "https://www.openstreetmap.org/copyright"}
DEFAULT_PROVIDER = "geoapify"
DEFAULT_RATE_PER_S = 5.0            # Geoapify Free plan limit
DEFAULT_DAILY_CREDITS = 2500        # stop below the 3,000/day free quota (headroom for manual checks)
DEFAULT_MAX_RETRIES = 3
RETRY_BASE_S = 1.0
RETRY_MAX_S = 30.0
# Taiwan / South Korea: the bake-off found 1/6 and 1/4 by English name; a native-script name is tried next.
NATIVE_RETRY_ISO2 = frozenset({"tw", "kr"})
CONFIRMED_BY = guards.CONFIRMED_BY


class GeocoderError(Exception):
    """A provider call failed (after retries). The message never contains the request URL or key."""


class GeocoderNotSupported(GeocoderError):
    """GEOCODER_PROVIDER names a provider removed by the S236 decision."""


class CreditLimitReached(GeocoderError):
    """The daily credit guard stopped the call: the name stays pending (no try is consumed)."""


@dataclass(frozen=True)
class Confirmed:
    lat: float
    lng: float
    country: str            # canonical AA country name
    place_id: Optional[str]
    located_by: str         # 'geoapify' (shared.destinations.located_by)
    meta: dict = field(default_factory=dict, compare=False, hash=False)   # audit: provider, type, confidence…


@dataclass(frozen=True)
class Unconfirmed:
    """The geocoder was consulted (or deliberately skipped) and nothing passed the acceptance rule."""
    meta: dict = field(default_factory=dict, compare=False, hash=False)


Result = Union[Confirmed, Unconfirmed, None]


class Geocoder(Protocol):
    name: str
    calls_external: bool
    cost_per_request: float
    requests: int

    async def confirm(self, name: str, lat: float, lng: float, country: str, *,
                      native_name: Optional[str] = None) -> Result:
        """Confirmed (a candidate passed the acceptance rule), Unconfirmed (checked, nothing passed) or
        None (this geocoder does not check). Raises on a failed call — the name is retried later."""
        ...

    async def aclose(self) -> None: ...


class _Throttle:
    """At most `rate` calls per second from this process."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if not self.interval:
            return
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


_KEY_PARAM = re.compile(r"(apiKey=)[^&\s'\"]+", re.I)


def redact(text: str, secret: Optional[str] = None) -> str:
    text = _KEY_PARAM.sub(r"\1***", str(text))
    return text.replace(secret, "***") if secret else text


class _RedactApiKey(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact(a) if isinstance(a, str) or hasattr(a, "query") else a
                                for a in record.args)
        record.msg = redact(record.msg) if isinstance(record.msg, str) else record.msg
        return True


def _install_log_redaction() -> None:
    for name in ("httpx", "httpcore"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, _RedactApiKey) for f in lg.filters):
            lg.addFilter(_RedactApiKey())


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("geocoder_env_invalid", name=name, value=raw)
        return default


class ModelOnlyGeocoder:
    name = "model_only"
    calls_external = False
    cost_per_request = 0.0

    def __init__(self, **_ignored):
        self.requests = 0

    async def confirm(self, name: str, lat: float, lng: float, country: str, *,
                      native_name: Optional[str] = None) -> Result:
        return None

    async def aclose(self) -> None:
        return None


class GeoapifyGeocoder:
    name = "geoapify"
    calls_external = True

    def __init__(self, *, http: Any = None, api_key: Optional[str] = None, cost_per_request: float = 0.0,
                 rate_per_s: float = DEFAULT_RATE_PER_S, daily_credit_limit: int = DEFAULT_DAILY_CREDITS,
                 credits_used_today: int = 0, max_retries: int = DEFAULT_MAX_RETRIES,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self._http = http
        self._own_http = http is None
        self._key = api_key
        self.cost_per_request = cost_per_request
        self.requests = 0                   # HTTP requests sent by this instance (= credits spent)
        self.daily_credit_limit = int(daily_credit_limit)
        self.credits_used_today = int(credits_used_today)   # before this instance (geo.geocode_call, UTC day)
        self.max_retries = max_retries
        self._sleep = sleep
        self._throttle = _Throttle(rate_per_s)
        _install_log_redaction()

    # ── plumbing ──
    def _client(self):
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    def _get_key(self) -> str:
        if not self._key:
            from shared.secrets import get_geoapify_key
            self._key = get_geoapify_key()
        return self._key

    def credits_left(self) -> int:
        return self.daily_credit_limit - self.credits_used_today - self.requests

    @staticmethod
    def params(text: str, iso2: str) -> dict:
        """Request parameters WITHOUT the key (safe to log / assert)."""
        p = {"text": text, "format": "json", "limit": str(geomatch.TOP_N), "lang": "en"}
        if iso2:
            p["filter"] = f"countrycode:{iso2}"
        return p

    async def search(self, text: str, iso2: str) -> dict:
        """One search (with retries on 429 / 5xx / transport errors). Every attempt costs a credit."""
        params = {**self.params(text, iso2), "apiKey": self._get_key()}
        last = "unknown"
        for attempt in range(self.max_retries + 1):
            if self.credits_left() <= 0:
                raise CreditLimitReached(
                    f"geoapify daily credit guard: {self.credits_used_today + self.requests} used, "
                    f"limit {self.daily_credit_limit}")
            await self._throttle.wait()
            self.requests += 1
            retry_after = None
            try:
                resp = await self._client().get(GEOAPIFY_URL, params=params)
            except Exception as e:  # noqa: BLE001 — transport error: the message may hold the URL
                last = f"{type(e).__name__}"
            else:
                status = getattr(resp, "status_code", 0)
                if status == 200:
                    try:
                        return resp.json()
                    except Exception as e:  # noqa: BLE001
                        raise GeocoderError(f"geoapify: invalid JSON ({type(e).__name__})") from None
                if status != 429 and status < 500:
                    raise GeocoderError(f"geoapify: HTTP {status}")
                last = f"HTTP {status}"
                retry_after = (getattr(resp, "headers", None) or {}).get("Retry-After")
            if attempt == self.max_retries:
                break
            delay = min(RETRY_MAX_S, RETRY_BASE_S * (2 ** attempt))
            try:
                if retry_after is not None:
                    delay = min(RETRY_MAX_S, max(delay, float(retry_after)))
            except (TypeError, ValueError):
                pass
            logger.warning("geoapify_retry", attempt=attempt + 1, error=last, delay_s=delay)
            await self._sleep(delay)
        raise GeocoderError(f"geoapify: {last} after {self.max_retries + 1} attempts")

    # ── the confirm call ──
    async def confirm(self, name: str, lat: float, lng: float, country: str, *,
                      native_name: Optional[str] = None) -> Result:
        iso = guards.iso2(country)
        queries = geomatch.normalise(name)
        if not queries:
            return Unconfirmed({"provider": self.name, "outcome": "skipped_generic"})
        if native_name and iso in NATIVE_RETRY_ISO2 and geomatch.has_native_script(native_name):
            queries = queries + [native_name.strip()]
        tried = []
        for q in queries:
            payload = await self.search(q, iso)
            cands = geomatch.candidates(payload)
            native = geomatch.has_native_script(q)
            v = (_accept_native(q, cands, lat, lng, iso) if native
                 else geomatch.accept(q, cands, lat, lng, iso))
            tried.append({"query": q, "results": len(cands), "rejected": v.reasons,
                          "top": _brief(cands[0]) if cands else None})
            if v.accepted is not None:
                c = v.accepted
                ctry = guards.ISO2_COUNTRY.get(c.iso2 or iso)
                if not ctry:
                    continue
                meta = {"provider": self.name, "outcome": "accepted", "query": q, "rank": v.rank,
                        "result_type": c.result_type, "confidence": c.confidence, "match_type": c.match_type,
                        "name": c.name, "sim": v.sim, "km": v.km, "source": c.source,
                        "attribution": ATTRIBUTION, "tried": tried[:-1]}
                return Confirmed(c.lat, c.lng, ctry, c.place_id, self.name, meta)
        return Unconfirmed({"provider": self.name, "outcome": "no_match", "tried": tried})

    async def aclose(self) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()


def _brief(c: geomatch.Candidate) -> dict:
    return {"name": c.name, "result_type": c.result_type, "confidence": c.confidence}


def _accept_native(query: str, cands: list, lat: float, lng: float, iso2: str) -> geomatch.Verdict:
    """A native-script query (中文 / 한글) has no Latin tokens to compare: accept the first top-3 candidate
    whose name contains the query (or vice versa) and that passes the type and distance rules."""
    q = query.replace(" ", "")
    reasons = []
    for i, c in enumerate(cands[:geomatch.TOP_N]):
        nm = (c.name or "").replace(" ", "")
        dist = guards.km(lat, lng, c.lat, c.lng)
        if iso2 and c.iso2 and c.iso2 != iso2:
            reasons.append(f"{i}:country")
        elif not nm or not (q in nm or nm in q):
            reasons.append(f"{i}:name")
        elif c.result_type in geomatch.REJECT_TYPES:
            reasons.append(f"{i}:type")
        elif dist > geomatch.LONG_FEATURE_KM:
            reasons.append(f"{i}:distance")
        else:
            return geomatch.Verdict(c, i, 1.0, round(dist, 2), reasons)
    return geomatch.Verdict(None, reasons=reasons)


class _Unsupported:
    calls_external = True
    cost_per_request = 0.0

    def __init__(self, **_ignored):
        raise GeocoderNotSupported(
            f"GEOCODER_PROVIDER={self.name} is not supported since S236 (Geoapify is the confirming geocoder; "
            "Mapbox is the basemap only, Amazon Location is not used). Use 'geoapify' or 'model_only'.")


class MapboxPermanentGeocoder(_Unsupported):
    name = "mapbox_permanent"


class AmazonLocationGeocoder(_Unsupported):
    name = "amazon_location"


PROVIDERS = {"geoapify": GeoapifyGeocoder, "model_only": ModelOnlyGeocoder,
             "mapbox_permanent": MapboxPermanentGeocoder, "amazon_location": AmazonLocationGeocoder}


def provider_name(value: Optional[str] = None) -> str:
    raw = (value if value is not None else os.environ.get("GEOCODER_PROVIDER", DEFAULT_PROVIDER)).strip().lower()
    if raw not in PROVIDERS:
        logger.warning("geocoder_provider_unknown_using_model_only", value=raw)
        return "model_only"
    return raw


def get_geocoder(value: Optional[str] = None, **kw) -> Geocoder:
    """The configured geocoder. Env (⚠️ underscore names — set by hand in the task definition):
    GEOCODER_PROVIDER (default geoapify), GEOCODER_COST_PER_REQUEST_USD (default 0: Free plan),
    GEOAPIFY_DAILY_CREDIT_LIMIT (default 2500), GEOAPIFY_RATE_PER_S (default 5)."""
    name = provider_name(value)
    if name == "model_only":
        return ModelOnlyGeocoder()
    if name == "geoapify":
        kw.setdefault("cost_per_request", _env_float("GEOCODER_COST_PER_REQUEST_USD", 0.0))
        kw.setdefault("daily_credit_limit", int(_env_float("GEOAPIFY_DAILY_CREDIT_LIMIT", DEFAULT_DAILY_CREDITS)))
        kw.setdefault("rate_per_s", min(DEFAULT_RATE_PER_S, _env_float("GEOAPIFY_RATE_PER_S", DEFAULT_RATE_PER_S)))
    return PROVIDERS[name](**kw)
