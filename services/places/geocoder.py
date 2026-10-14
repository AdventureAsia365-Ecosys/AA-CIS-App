"""AA-712 — geocoder adapter (design §4, task: provider chosen by config, default no external call).

Method (TripPlanner locate.py, S202 lesson "geocoding by name alone is wrong for Asian names"): the
model proposes [lat, lng, country] per name (services/places/locate.py); a geocoder only CONFIRMS
that point — it searches the name around it and a result within CONFIRM_KM in an AA country is
taken. When nothing confirms, the model point is kept (located_by 'llm', lower confidence).

Providers (`GEOCODER_PROVIDER`, default `model_only`):
  model_only        no external call: every point is the model's ('llm'). $0.
  mapbox_permanent  Mapbox Geocoding v6 forward with `permanent=true` — required to store results
                    (Mapbox terms: Temporary results may not be cached; design §4, Q6). ~$5 / 1,000.
  amazon_location   Amazon Location Places v2 Geocode with IntendedUse='Storage' (stored results need
                    the Storage intended use). Price: set GEOCODER_COST_PER_REQUEST_USD from the AWS
                    pricing page before enabling; the default below is only a budget placeholder.
An unknown value falls back to model_only (logged) — a typo must never start paid calls.

Only the confirm call is provider-specific; guards (services/places/guards.py) run on every result.
Tests use fakes / mocked HTTP and boto3 only (no live call).
"""
from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any, Optional, Protocol

import structlog

from services.places import guards

logger = structlog.get_logger()

MAPBOX_V6_URL = "https://api.mapbox.com/search/geocode/v6/forward"
# v6 forward has no POI type (POIs moved to the Search Box API); towns, villages, districts and
# neighbourhoods are what itinerary anchors are.
MAPBOX_TYPES = "place,locality,district,neighborhood"
DEFAULT_COST = {"model_only": 0.0, "mapbox_permanent": 0.005, "amazon_location": 0.005}
DEFAULT_RATE_PER_S = 5.0


@dataclass(frozen=True)
class Confirmed:
    lat: float
    lng: float
    country: str            # canonical AA country name
    place_id: Optional[str]
    located_by: str         # 'mapbox' | 'amazon_location' (shared.destinations.located_by)


class Geocoder(Protocol):
    name: str
    calls_external: bool
    cost_per_request: float
    requests: int

    async def confirm(self, name: str, lat: float, lng: float, country: str) -> Optional[Confirmed]:
        """A result within CONFIRM_KM of (lat, lng) in an AA country, or None."""
        ...

    async def aclose(self) -> None: ...


class _Throttle:
    """At most `rate` calls per second from this process (Mapbox default limit ~600/min)."""

    def __init__(self, rate: float):
        self.interval = 1.0 / rate if rate > 0 else 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval


def _first_within(candidates: list[tuple[float, float, str, Optional[str]]], lat: float, lng: float,
                  located_by: str) -> Optional[Confirmed]:
    """candidates: (lat, lng, iso2, place_id) in the provider's order."""
    for c_lat, c_lng, iso, pid in candidates:
        country = guards.ISO2_COUNTRY.get((iso or "").lower())
        if country and guards.km(lat, lng, c_lat, c_lng) <= guards.CONFIRM_KM:
            return Confirmed(c_lat, c_lng, country, pid, located_by)
    return None


class ModelOnlyGeocoder:
    name = "model_only"
    calls_external = False
    cost_per_request = 0.0

    def __init__(self):
        self.requests = 0

    async def confirm(self, name: str, lat: float, lng: float, country: str) -> Optional[Confirmed]:
        return None

    async def aclose(self) -> None:
        return None


class MapboxPermanentGeocoder:
    name = "mapbox_permanent"
    calls_external = True

    def __init__(self, *, http: Any = None, token: Optional[str] = None, cost_per_request: Optional[float] = None,
                 rate_per_s: float = DEFAULT_RATE_PER_S):
        self._http = http
        self._own_http = http is None
        self._token = token
        self.cost_per_request = DEFAULT_COST[self.name] if cost_per_request is None else cost_per_request
        self.requests = 0
        self._throttle = _Throttle(rate_per_s)

    def _client(self):
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    def _get_token(self) -> str:
        if not self._token:
            from shared.secrets import get_mapbox_token
            self._token = get_mapbox_token()
        return self._token

    def params(self, name: str, lat: float, lng: float) -> dict:
        return {"q": name, "permanent": "true", "proximity": f"{lng},{lat}", "country": ",".join(guards.AA_ISO2),
                "types": MAPBOX_TYPES, "limit": "5", "language": "en", "autocomplete": "false",
                "access_token": self._get_token()}

    @staticmethod
    def candidates(payload: dict) -> list[tuple[float, float, str, Optional[str]]]:
        out = []
        for f in payload.get("features") or []:
            coords = (f.get("geometry") or {}).get("coordinates") or []
            props = f.get("properties") or {}
            iso = (((props.get("context") or {}).get("country") or {}).get("country_code") or "")
            if len(coords) == 2:
                out.append((float(coords[1]), float(coords[0]), iso.lower(), props.get("mapbox_id")))
        return out

    async def confirm(self, name: str, lat: float, lng: float, country: str) -> Optional[Confirmed]:
        await self._throttle.wait()
        self.requests += 1
        resp = await self._client().get(MAPBOX_V6_URL, params=self.params(name, lat, lng))
        resp.raise_for_status()
        return _first_within(self.candidates(resp.json()), lat, lng, "mapbox")

    async def aclose(self) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()


class AmazonLocationGeocoder:
    name = "amazon_location"
    calls_external = True

    def __init__(self, *, client: Any = None, region: Optional[str] = None,
                 cost_per_request: Optional[float] = None, rate_per_s: float = DEFAULT_RATE_PER_S):
        self._client = client
        self._region = region or os.environ.get("GEOCODER_AWS_REGION", "us-west-2")
        self.cost_per_request = DEFAULT_COST[self.name] if cost_per_request is None else cost_per_request
        self.requests = 0
        self._throttle = _Throttle(rate_per_s)

    def _boto(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("geo-places", region_name=self._region)
        return self._client

    @staticmethod
    def request(name: str, lat: float, lng: float) -> dict:
        return {"QueryText": name, "BiasPosition": [lng, lat], "MaxResults": 5, "Language": "en",
                "IntendedUse": "Storage",
                "Filter": {"IncludeCountries": [guards.ISO2_ISO3[i] for i in guards.AA_ISO2]}}

    @staticmethod
    def candidates(payload: dict) -> list[tuple[float, float, str, Optional[str]]]:
        out = []
        for item in payload.get("ResultItems") or []:
            pos = item.get("Position") or []
            ctry = ((item.get("Address") or {}).get("Country") or {})
            iso = (ctry.get("Code2") or guards.ISO3_ISO2.get((ctry.get("Code3") or "").upper(), "")).lower()
            if len(pos) == 2:
                out.append((float(pos[1]), float(pos[0]), iso, item.get("PlaceId")))
        return out

    async def confirm(self, name: str, lat: float, lng: float, country: str) -> Optional[Confirmed]:
        await self._throttle.wait()
        self.requests += 1
        payload = await asyncio.to_thread(lambda: self._boto().geocode(**self.request(name, lat, lng)))
        return _first_within(self.candidates(payload), lat, lng, "amazon_location")

    async def aclose(self) -> None:
        return None


PROVIDERS = {"model_only": ModelOnlyGeocoder, "mapbox_permanent": MapboxPermanentGeocoder,
             "amazon_location": AmazonLocationGeocoder}


def provider_name(value: Optional[str] = None) -> str:
    raw = (value if value is not None else os.environ.get("GEOCODER_PROVIDER", "model_only")).strip().lower()
    if raw not in PROVIDERS:
        logger.warning("geocoder_provider_unknown_using_model_only", value=raw)
        return "model_only"
    return raw


def get_geocoder(value: Optional[str] = None, **kw) -> Geocoder:
    """The configured geocoder. `GEOCODER_COST_PER_REQUEST_USD` overrides the provider's price."""
    name = provider_name(value)
    if name == "model_only":
        return ModelOnlyGeocoder()
    cost = os.environ.get("GEOCODER_COST_PER_REQUEST_USD")
    if cost and "cost_per_request" not in kw:
        try:
            kw["cost_per_request"] = float(cost)
        except ValueError:
            logger.warning("geocoder_cost_env_invalid", value=cost)
    return PROVIDERS[name](**kw)
