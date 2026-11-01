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
# AA-997 v2: query strategy. "v1" = one normalised query, route-middle bias (the S255 behaviour, default);
# "v2" = geomatch.query_plan variants (≤ V2_MAX_QUERIES requests a name), bias to the model's point when it is on the
# route (or there is no route), compact name matching. Chosen per job payload (place_relocate `strategy`).
STRATEGY_V1, STRATEGY_V2 = "v1", "v2"
STRATEGIES = (STRATEGY_V1, STRATEGY_V2)
V2_MAX_QUERIES = 3


def default_accept_rule(value: Optional[str] = None) -> str:
    """The acceptance rule place_geocode uses for NEW names (AA-997 v2 review fix: strict is behind a switch): env
    GEOCODER_ACCEPT_RULE (⚠️ underscore name — task definition), "v1" (default) | "aa997.2" (geomatch.ACCEPT_RULE: v1 +
    the strict_reason checks) | "aa997.3" (the same checks, route evidence tuned on the S258 P4 sample) | "aa997.4"
    (aa997.3 + the day-evidence guard, geomatch.day_disagree). Anything else →
    v1 (logged). place_relocate takes it from its payload `accept_rule`."""
    raw = (value if value is not None else os.environ.get("GEOCODER_ACCEPT_RULE", geomatch.ACCEPT_RULE_V1)).strip()
    if raw.lower() not in geomatch.ACCEPT_RULES:
        logger.warning("geocoder_accept_rule_unknown_using_v1", value=raw)
        return geomatch.ACCEPT_RULE_V1
    return raw.lower()


def default_strategy(value: Optional[str] = None) -> str:
    """The query strategy place_geocode uses for NEW names: env GEOCODER_STRATEGY (⚠️ underscore name — task
    definition), "v1" (default) | "v2". Anything else → v1 (logged). place_relocate takes it from its payload."""
    raw = (value if value is not None else os.environ.get("GEOCODER_STRATEGY", STRATEGY_V1)).strip().lower()
    if raw not in STRATEGIES:
        logger.warning("geocoder_strategy_unknown_using_v1", value=raw)
        return STRATEGY_V1
    return raw


class GeocoderError(Exception):
    """A provider call failed (after retries). The message never contains the request URL or key."""


class GeocoderNotSupported(GeocoderError):
    """GEOCODER_PROVIDER names a provider removed by the S236 decision."""


class CreditLimitReached(GeocoderError):
    """The daily credit guard stopped the call: the name stays pending (no try is consumed)."""


class GeocoderUnavailable(GeocoderError):
    """Setup / auth failure (no key, secret unreadable, HTTP 401 / 403): no name can be checked until an
    operator fixes it, so the run stops and every name stays pending without a try counted — the same
    handling as CreditLimitReached. Never a per-name error (3 tries would mark good names `unknown`)."""


@dataclass(frozen=True)
class Confirmed:
    lat: float
    lng: float
    country: str            # canonical ISO country name (shared/countries), the PLACE's country
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

    async def confirm(self, name: str, lat: Optional[float], lng: Optional[float], country: str, *,
                      native_name: Optional[str] = None, routes: tuple = (), settlement: bool = False,
                      strategy: str = "v1", context: Optional[str] = None, max_queries: Optional[int] = None,
                      strict: bool = False) -> Result:
        """Confirmed (a candidate passed the acceptance rule), Unconfirmed (checked, nothing passed) or
        None (this geocoder does not check). Raises on a failed call — the name is retried later.
        AA-997: lat / lng (the model's point) is a hint and may be None; `routes` (guards.route_km) is the reference
        when given; `settlement` asks for a town (the town fallback). AA-997 v2: `strategy` "v2" = hint bias,
        query variants with `context` (geomatch.query_plan, ≤ `max_queries` requests) and compact name matching;
        `strict` = a strict acceptance rule (strict_reason checks) instead of v1: True = geomatch.ACCEPT_RULE, or a
        rule name of geomatch.STRICT_RULES."""
        ...

    async def aclose(self) -> None: ...


class _Throttle:
    """At most `rate` calls per second from this process."""

    def __init__(self, rate: float):
        self.interval = 1.0 / clamp_rate(rate)
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


def clamp_rate(rate: float) -> float:
    """Requests per second in (0, DEFAULT_RATE_PER_S]: a 0 / negative / non-numeric value must never turn
    the throttle off (the Free plan allows 5 req/s)."""
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return DEFAULT_RATE_PER_S
    if not r > 0:   # also NaN
        return DEFAULT_RATE_PER_S
    return min(r, DEFAULT_RATE_PER_S)


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


# AA-1017 (registry v2, migration 322): the daily credit limit is the Geoapify connection row's daily_credit_limit
# (shared.llm_provider_connection 'geoapify', seeded 2500); the env GEOAPIFY_DAILY_CREDIT_LIMIT and
# DEFAULT_DAILY_CREDITS answer when there is no row (row -> env -> constant). Jobs refresh it from the DB when they
# start (refresh_daily_credit_limit); a sync reader (daily_credit_limit) uses the last value read in this process.
GEOAPIFY_CONNECTION_KEY = "geoapify"
_registry_daily_limit: Optional[int] = None
# AA-642 (S263 L2, B1): the row's ``enabled`` (the admin switch), read with the daily limit when a job starts. False =
# every Geoapify request is refused before it is sent (GeocoderUnavailable: the run stops, names stay pending, no try
# counted). None (no row read yet, no row, unreadable) = on. A switch flipped during a run applies from the next run.
_registry_enabled: Optional[bool] = None


def connection_switched_off() -> bool:
    """True when the Geoapify connection row last read in this process says enabled = false."""
    return _registry_enabled is False


def check_switch() -> None:
    """Raise GeocoderUnavailable when the Geoapify connection is switched off (nothing is sent)."""
    if connection_switched_off():
        from shared.llm_client import connections as conn_registry

        raise GeocoderUnavailable(f"geoapify: {conn_registry.switched_off_reason(GEOAPIFY_CONNECTION_KEY)}")


def daily_credit_limit() -> int:
    """The Geoapify credits a UTC day: the connection row's value last read in this process, else the env var, else
    DEFAULT_DAILY_CREDITS."""
    if _registry_daily_limit is not None:
        return _registry_daily_limit
    return int(_env_float("GEOAPIFY_DAILY_CREDIT_LIMIT", DEFAULT_DAILY_CREDITS))


async def refresh_daily_credit_limit(db) -> int:
    """Read the Geoapify connection row (``db``: asyncpg connection or pool) and return ``daily_credit_limit()``. A
    missing row / column or a failed read keeps the last value (none = env / constant). AA-642 (S263 L2): the row's
    switch too (``connection_switched_off``). Never raises."""
    global _registry_daily_limit, _registry_enabled
    from shared.llm_client import connections as conn_registry

    row = await conn_registry.provider_connection(db, GEOAPIFY_CONNECTION_KEY)
    if row is not None:
        value = row.daily_credit_limit
        _registry_daily_limit = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        _registry_enabled = row.enabled if isinstance(row.enabled, bool) else None
    return daily_credit_limit()


async def note_access(state: str) -> None:
    """AA-642 (S263 L2, B1): the Geoapify row's access_state (written when it changes in this process). Never
    raises."""
    from shared.llm_client import connections as conn_registry

    await conn_registry.note_provider_access(GEOAPIFY_CONNECTION_KEY, state)


def _note_access(state: str) -> None:
    """``note_access`` from sync code (a missing key is found in the sync ``_get_key``): scheduled on the running loop
    when there is one, else skipped (the next async call writes the state). Never raises."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        task = loop.create_task(note_access(state))
        _pending_notes.add(task)
        task.add_done_callback(_pending_notes.discard)
    except Exception:  # noqa: BLE001 — a state write never breaks the geocoder
        pass


_pending_notes: set = set()


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

    async def confirm(self, name: str, lat: Optional[float], lng: Optional[float], country: str, *,
                      native_name: Optional[str] = None, routes: tuple = (), settlement: bool = False,
                      strategy: str = "v1", context: Optional[str] = None, max_queries: Optional[int] = None,
                      strict: bool = False) -> Result:
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
        self.failed_requests = 0            # AA-804: of those, the ones that did not answer 200 (still a credit)
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
        check_switch()                      # AA-642 (S263 L2): switched off in the admin = nothing is sent
        if not self._key:
            from shared.secrets import get_geoapify_key
            try:
                key = get_geoapify_key()
            except Exception as e:  # noqa: BLE001 — the message may name the secret; keep the type only
                _note_access("unconfigured")
                raise GeocoderUnavailable(f"geoapify: API key unavailable ({type(e).__name__})") from None
            if not (key or "").strip():
                _note_access("unconfigured")
                raise GeocoderUnavailable("geoapify: API key is empty")
            self._key = key.strip()
        return self._key

    def ensure_ready(self) -> None:
        """Raise GeocoderUnavailable now (before any paid model call) when the key cannot be read."""
        self._get_key()

    def credits_left(self) -> int:
        return self.daily_credit_limit - self.credits_used_today - self.requests

    @staticmethod
    def params(text: str, iso2: str, *, codes: Optional[list] = None,
               bias: Optional[tuple[float, float]] = None) -> dict:
        """Request parameters WITHOUT the key (safe to log / assert). AA-997: `codes` widens the country filter (China
        covers hk / mo, guards.search_codes); `bias` = (lat, lng) ranks results near the tour's route first."""
        p = {"text": text, "format": "json", "limit": str(geomatch.TOP_N), "lang": "en"}
        codes = [c for c in (codes or ([iso2] if iso2 else [])) if c]
        if codes:
            p["filter"] = f"countrycode:{','.join(codes)}"
        if bias is not None:
            p["bias"] = f"proximity:{bias[1]:.5f},{bias[0]:.5f}"
        return p

    async def search(self, text: str, iso2: str, *, codes: Optional[list] = None,
                     bias: Optional[tuple[float, float]] = None) -> dict:
        """One search (with retries on 429 / 5xx / transport errors). Every attempt costs a credit."""
        params = {**self.params(text, iso2, codes=codes, bias=bias), "apiKey": self._get_key()}
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
                self.failed_requests += 1
            else:
                status = getattr(resp, "status_code", 0)
                if status == 200:
                    try:
                        data = resp.json()
                    except Exception as e:  # noqa: BLE001
                        self.failed_requests += 1
                        raise GeocoderError(f"geoapify: invalid JSON ({type(e).__name__})") from None
                    await note_access("ok")
                    return data
                self.failed_requests += 1
                if status in (401, 403):
                    await note_access("access_denied")
                    raise GeocoderUnavailable(f"geoapify: HTTP {status} (key rejected)")
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
    async def confirm(self, name: str, lat: Optional[float], lng: Optional[float], country: str, *,
                      native_name: Optional[str] = None, routes: tuple = (), settlement: bool = False,
                      strategy: str = "v1", context: Optional[str] = None, max_queries: Optional[int] = None,
                      strict=False) -> Result:
        iso = guards.iso2(country)
        rule = geomatch.strict_rule(strict) or geomatch.ACCEPT_RULE_V1
        codes = guards.search_codes(country)
        v2 = strategy == STRATEGY_V2
        if v2:      # (search text, bare name judged): context variants tried only after the plain name missed
            plan = geomatch.query_plan(name, context=None if settlement else context,
                                       max_queries=max_queries or V2_MAX_QUERIES)
        else:
            plan = [(q, q) for q in geomatch.normalise(name)]
        if not plan:
            return Unconfirmed({"provider": self.name, "outcome": "skipped_generic"})
        if not guards.has_route(routes) and (lat is None or lng is None):
            # AA-997: nothing to check an answer against (no model point, no located stop of the tour): no call
            return Unconfirmed({"provider": self.name, "outcome": "skipped_no_reference"})
        if native_name and iso in NATIVE_RETRY_ISO2 and geomatch.has_native_script(native_name):
            plan = plan + [(native_name.strip(), native_name.strip())]
        bias = guards.search_bias(routes, lat, lng, name) if v2 else guards.route_bias(routes)
        tried = []
        for text, q in plan:
            payload = await self.search(text, iso, codes=codes, bias=bias)
            cands = geomatch.candidates(payload)
            native = geomatch.has_native_script(q)
            v = (_accept_native(q, cands, lat, lng, iso, ref_name=name, routes=routes, iso_codes=codes,
                                strict=strict) if native
                 else geomatch.accept(q, cands, lat, lng, iso, routes=routes, iso_codes=tuple(codes),
                                      settlement=True if settlement else None, compact=v2, strict=strict))
            tried.append({"query": text, "results": len(cands), "rejected": v.reasons,
                          "top": _brief(cands[0]) if cands else None})
            if v.accepted is not None:
                c = v.accepted
                ctry = guards.ISO2_COUNTRY.get(c.iso2 or iso)
                if not ctry:
                    continue
                meta = {"provider": self.name, "outcome": "accepted", "query": text, "judged": q, "rank": v.rank,
                        "strategy": strategy,
                        "rule": rule,
                        "reference": "route" if guards.has_route(routes) else "hint",
                        "result_type": c.result_type, "confidence": c.confidence, "match_type": c.match_type,
                        "name": c.name, "sim": v.sim, "km": v.km, "source": c.source,
                        "attribution": ATTRIBUTION, "tried": tried[:-1]}
                if lat is not None and lng is not None:     # the model's point judged against (a later re-judge)
                    meta["hint"] = [round(lat, 5), round(lng, 5)]
                return Confirmed(c.lat, c.lng, ctry, c.place_id, self.name, meta)
        return Unconfirmed({"provider": self.name, "outcome": "no_match", "tried": tried,
                            "rule": rule})

    async def aclose(self) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()


def _brief(c: geomatch.Candidate) -> dict:
    return {"name": c.name, "result_type": c.result_type, "confidence": c.confidence}


def _accept_native(query: str, cands: list, lat: Optional[float], lng: Optional[float], iso2: str, *,
                   ref_name: str = "", routes: tuple = (), iso_codes=None, strict=False) -> geomatch.Verdict:
    """A native-script query (中文 / 한글) has no Latin tokens to compare. Same rule as geomatch.accept
    otherwise: the radius and the settlement-only `city` rule come from the English name (`ref_name`) and
    the native query's own feature suffix (山莊 / 寺 / 湖 / 사 / 호 …); the candidate's name must contain the
    query or be contained in it, and then be ≥ 2 characters and ≥ half the query's length — "太平" (a city)
    for "太平山莊" (a mountain lodge) is the Taipingshan homonym trap of bake-off row 58."""
    rule = geomatch.strict_rule(strict)
    q = query.replace(" ", "")
    settlement = (geomatch.is_settlement(ref_name) if ref_name else True) and not geomatch.native_feature(q)
    codes = set(iso_codes or ()) | ({iso2} if iso2 else set())
    reasons = []
    fallback = None          # geomatch.accept's `day_trip` second choice
    for i, c in enumerate(cands[:geomatch.TOP_N]):
        nm = (c.name or "").replace(" ", "")
        if codes and c.iso2 and c.iso2 not in codes:
            reasons.append(f"{i}:country")
        elif not nm or not (q in nm or nm in q) or (nm in q and (len(nm) < 2 or 2 * len(nm) < len(q))):
            reasons.append(f"{i}:name")
        elif c.result_type in geomatch.REJECT_TYPES or (c.result_type == "city" and not settlement):
            reasons.append(f"{i}:type")
        else:
            # the radius comes from the English name (`ref_name`)
            dist, why = geomatch.reference_km(c.lat, c.lng, lat, lng, ref_name or query, routes)
            if rule and why in (None, geomatch.DAY_TRIP):     # AA-997 v2 strict checks (no Latin words: no kind)
                why = geomatch.strict_reason(ref_name or query, c, 1.0, lat, lng, routes, settlement=settlement,
                                             day_trip=why == geomatch.DAY_TRIP, check_kind=False,
                                             params=geomatch.strict_params(rule)) or why
            if why == geomatch.DAY_TRIP and fallback is None:
                fallback = geomatch.Verdict(c, i, 1.0, round(dist, 2))
            if why:
                reasons.append(f"{i}:{why}")
            else:
                return geomatch.Verdict(c, i, 1.0, round(dist, 2), reasons)
    return geomatch.with_fallback(fallback, reasons)


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
    GEOAPIFY_DAILY_CREDIT_LIMIT (default 2500; AA-1017: the Geoapify connection row first, daily_credit_limit()),
    GEOAPIFY_RATE_PER_S (default 5, clamped to (0, 5])."""
    name = provider_name(value)
    if name == "model_only":
        return ModelOnlyGeocoder()
    if name == "geoapify":
        kw.setdefault("cost_per_request", _env_float("GEOCODER_COST_PER_REQUEST_USD", 0.0))
        kw.setdefault("daily_credit_limit", daily_credit_limit())
        kw.setdefault("rate_per_s", clamp_rate(_env_float("GEOAPIFY_RATE_PER_S", DEFAULT_RATE_PER_S)))
    return PROVIDERS[name](**kw)
