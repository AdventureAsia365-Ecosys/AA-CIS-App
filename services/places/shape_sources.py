"""AA-847 — where the geometry of a line place comes from (job kind place_shape; plan §2). Offline job only: no
request path calls these.

  1. Geoapify Place Details by the destination's geocoder_place_id: `features=details` (1 credit, UNVERIFIED — the
     5-name trial reads the real use from the Geoapify dashboard first), then `details.full_geometry` (+1) only when
     the details geometry is a point or cut short. A single OSM way of a river is not the river: the job then also
     asks Overpass and prefers the relation / merged connected same-name ways (shape.choose).
  2. Overpass (public instance, free): ≤ 1 request / 2 s, backoff on 429 / 504; `[timeout:60][maxsize:64 MB]`;
     waterway river / stream / canal ways + waterway relations, highway / railway ways, route relations (hiking,
     foot, road, bicycle, railway); the name regex is tried on name, name:en, int_name and every name:*; the search
     area is the country area ∩ 100 km around the anchors, geometry cut to that box (`out geom(bbox)`).
  3. The anchored approximate route and 4. the admin's manual GeoJSON need no call (shape.anchored_line, admin).

Both clients take an injectable `http` (tests use recorded payloads — no network in tests) and never put the API key
in an exception or a log line (geocoder.redact). Credits are counted per call (`credits`) for geo.geocode_call.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable, Optional, Sequence

import structlog

from services.places import geocoder as geocoder_mod
from services.places import guards
from services.places.shape import LineCandidate, merge_ways, name_tokens, parts_from_geojson

logger = structlog.get_logger()

DETAILS_URL = "https://api.geoapify.com/v2/place-details"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_MIN_INTERVAL_S = 2.0
OVERPASS_SEARCH_KM = 100.0
OVERPASS_TIMEOUT_S = 60
OVERPASS_MAXSIZE = 64 * 1024 * 1024
# Geoapify credits per Place Details feature — ASSUMED (1 per feature asked), not verified: the trial measures it.
DETAILS_CREDITS = 1
FULL_GEOMETRY_CREDITS = 1
OSM_TYPES = {"n": "node", "w": "way", "r": "relation", "node": "node", "way": "way", "relation": "relation",
             "N": "node", "W": "way", "R": "relation"}


class ShapeSourceError(Exception):
    """A call failed after retries (the message never holds the URL or the key)."""


# ── Geoapify Place Details ────────────────────────────────────────────────────────────────────────────────────────

def details_names(props: dict) -> list:
    raw = (props.get("datasource") or {}).get("raw") or {}
    names = [props.get("name"), raw.get("name"), raw.get("name:en"), raw.get("int_name")]
    names += [v for k, v in raw.items() if isinstance(k, str) and k.startswith("name:") and isinstance(v, str)]
    names += [v for k, v in (props.get("name_international") or {}).items() if isinstance(v, str)]
    return list(dict.fromkeys(n for n in names if n))


def details_tags(props: dict) -> dict:
    raw = (props.get("datasource") or {}).get("raw") or {}
    return {k: raw[k] for k in ("waterway", "highway", "railway", "route", "type") if k in raw}


def parse_details(payload: dict) -> Optional[LineCandidate]:
    """Geoapify Place Details FeatureCollection → the line feature (None when it carries no line geometry)."""
    for f in (payload or {}).get("features") or []:
        parts = parts_from_geojson(f.get("geometry"))
        if not parts:
            continue
        props = f.get("properties") or {}
        raw = (props.get("datasource") or {}).get("raw") or {}
        osm_type = OSM_TYPES.get(str(raw.get("osm_type") or ""))
        osm_id = raw.get("osm_id")
        try:
            osm_id = abs(int(osm_id)) if osm_id is not None else None
        except (TypeError, ValueError):
            osm_id = None
        if osm_type is None or osm_id is None:
            osm_type = osm_id = None
        return LineCandidate(parts, details_names(props), osm_type, osm_id, details_tags(props), "geoapify_details")
    return None


def details_is_short(payload: dict) -> bool:
    """True when the details answer has no line (a point) or says the geometry was cut (`details` only)."""
    for f in (payload or {}).get("features") or []:
        g = f.get("geometry") or {}
        if g.get("type") in ("LineString", "MultiLineString"):
            props = f.get("properties") or {}
            return bool(props.get("geometry_simplified") or props.get("truncated"))
    return True


class GeoapifyDetails:
    """Place Details client. Shares the Geoapify key, the throttle rules and the daily credit guard of the geocoder
    (credits_used_today is loaded by the job from geo.geocode_call, which sums credits of every endpoint)."""

    name = "geoapify"
    endpoint = "place_details"

    def __init__(self, *, http: Any = None, api_key: Optional[str] = None, daily_credit_limit: int = 2500,
                 credits_used_today: int = 0, rate_per_s: float = geocoder_mod.DEFAULT_RATE_PER_S,
                 max_retries: int = 3, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self._http = http
        self._own_http = http is None
        self._key = api_key
        self.requests = 0
        self.failed_requests = 0       # AA-804: requests that did not answer 200 / 404 (their credits are still spent)
        self.credits = 0
        self.daily_credit_limit = int(daily_credit_limit)
        self.credits_used_today = int(credits_used_today)
        self.max_retries = max_retries
        self._sleep = sleep
        self._throttle = geocoder_mod._Throttle(rate_per_s)
        geocoder_mod._install_log_redaction()

    def credits_left(self) -> int:
        return self.daily_credit_limit - self.credits_used_today - self.credits

    def _client(self):
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=20.0)
        return self._http

    def _get_key(self) -> str:
        geocoder_mod.check_switch()         # AA-642 (S263 L2): switched off in the admin = nothing is sent
        if not self._key:
            from shared.secrets import get_geoapify_key
            try:
                key = get_geoapify_key()
            except Exception as e:  # noqa: BLE001
                raise geocoder_mod.GeocoderUnavailable(f"geoapify: API key unavailable ({type(e).__name__})") from None
            if not (key or "").strip():
                raise geocoder_mod.GeocoderUnavailable("geoapify: API key is empty")
            self._key = key.strip()
        return self._key

    @staticmethod
    def params(place_id: str, full_geometry: bool) -> dict:
        """Request parameters WITHOUT the key."""
        return {"id": place_id, "features": "details,details.full_geometry" if full_geometry else "details",
                "lang": "en"}

    async def fetch(self, place_id: str, *, full_geometry: bool = False) -> dict:
        cost = DETAILS_CREDITS + (FULL_GEOMETRY_CREDITS if full_geometry else 0)
        params = {**self.params(place_id, full_geometry), "apiKey": self._get_key()}
        last = "unknown"
        for attempt in range(self.max_retries + 1):
            if self.credits_left() < cost:
                raise geocoder_mod.CreditLimitReached(
                    f"geoapify daily credit guard: {self.credits_used_today + self.credits} used, "
                    f"limit {self.daily_credit_limit}")
            await self._throttle.wait()
            self.requests += 1
            self.credits += cost
            retry_after = None
            try:
                resp = await self._client().get(DETAILS_URL, params=params)
            except Exception as e:  # noqa: BLE001
                last = type(e).__name__
                self.failed_requests += 1
            else:
                status = getattr(resp, "status_code", 0)
                if status == 200:
                    try:
                        return resp.json()
                    except Exception as e:  # noqa: BLE001
                        self.failed_requests += 1
                        raise ShapeSourceError(f"geoapify details: invalid JSON ({type(e).__name__})") from None
                if status == 404:
                    return {}
                self.failed_requests += 1
                if status in (401, 403):
                    raise geocoder_mod.GeocoderUnavailable(f"geoapify: HTTP {status} (key rejected)")
                if status != 429 and status < 500:
                    raise ShapeSourceError(f"geoapify details: HTTP {status}")
                last = f"HTTP {status}"
                retry_after = (getattr(resp, "headers", None) or {}).get("Retry-After")
            if attempt == self.max_retries:
                break
            delay = min(geocoder_mod.RETRY_MAX_S, geocoder_mod.RETRY_BASE_S * (2 ** attempt))
            try:
                if retry_after is not None:
                    delay = min(geocoder_mod.RETRY_MAX_S, max(delay, float(retry_after)))
            except (TypeError, ValueError):
                pass
            await self._sleep(delay)
        raise ShapeSourceError(f"geoapify details: {last} after {self.max_retries + 1} attempts")

    async def line(self, place_id: str) -> Optional[LineCandidate]:
        """details first; full_geometry only when the details answer is a point or cut short."""
        payload = await self.fetch(place_id)
        cand = parse_details(payload)
        if cand is None or details_is_short(payload):
            full = parse_details(await self.fetch(place_id, full_geometry=True))
            cand = full or cand
        return cand

    async def aclose(self) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()


# ── Overpass ──────────────────────────────────────────────────────────────────────────────────────────────────────

def name_regex(name: str) -> Optional[str]:
    """Case-insensitive Overpass regex for the distinctive tokens of a name, in order, loosely joined
    ("Nam Ou River" → "ou"; "Ho Chi Minh Trail" → "ho.{0,3}chi.{0,3}minh"). None when there is nothing distinctive."""
    toks = name_tokens(name)
    if not toks or (len(toks) == 1 and len(toks[0]) < 2):
        return None
    if len(toks) == 1 and len(toks[0]) <= 3:
        return rf"(^|[^a-z]){re.escape(toks[0])}([^a-z]|$)"
    return ".{0,3}".join(re.escape(t) for t in toks)


def search_bbox(anchors: Sequence[tuple], iso2: str, km: float = OVERPASS_SEARCH_KM) -> tuple:
    """(south, west, north, east): the anchors' box grown by `km`, cut to the country's box."""
    lats = [a[0] for a in anchors]
    lngs = [a[1] for a in anchors]
    dlat = km / 110.574
    import math
    dlng = km / max(1.0, 111.320 * math.cos(math.radians(sum(lats) / len(lats))))
    s, w, n, e = min(lats) - dlat, min(lngs) - dlng, max(lats) + dlat, max(lngs) + dlng
    box = guards.BBOX.get(iso2)
    if box:
        s, n, w, e = max(s, box[0]), min(n, box[1]), max(w, box[2]), min(e, box[3])
    return (round(s, 4), round(w, 4), round(n, 4), round(e, 4))


def overpass_query(name: str, iso2: str, anchors: Sequence[tuple], line_class: str) -> Optional[str]:
    rx = name_regex(name)
    if rx is None or not anchors:
        return None
    s, w, n, e = search_bbox(anchors, iso2)
    if s >= n or w >= e:
        return None
    nm = f'[~"^(name|name:en|int_name|name:[a-z_-]+)$"~"{rx}",i]'
    box = f"({s},{w},{n},{e})"
    if line_class == "water":
        sel = [f'way["waterway"~"^(river|stream|canal)$"]{nm}(area.c){box};',
               f'relation["type"="waterway"]{nm}(area.c){box};',
               f'relation["waterway"~"^(river|stream|canal)$"]{nm}(area.c){box};']
    elif line_class == "road":
        sel = [f'way["highway"]{nm}(area.c){box};', f'way["railway"]{nm}(area.c){box};',
               f'relation["route"~"^(road|railway|train)$"]{nm}(area.c){box};']
    else:
        sel = [f'relation["route"~"^(hiking|foot|bicycle|mtb|road)$"]{nm}(area.c){box};',
               f'way["highway"~"^(path|footway|track|bridleway|primary|secondary|tertiary|trunk|unclassified)$"]'
               f'{nm}(area.c){box};']
    return (f"[out:json][timeout:{OVERPASS_TIMEOUT_S}][maxsize:{OVERPASS_MAXSIZE}];\n"
            f'area["ISO3166-1"="{iso2.upper()}"]["admin_level"="2"]->.c;\n'
            f"(\n  " + "\n  ".join(sel) + "\n);\n"
            f"out body geom({s},{w},{n},{e});\n"
            # the relations each found way belongs to (members as refs, no geometry): a way of a relation-backed
            # river loses to the relation (shape.choose)
            "way._;\nrel(bw)->.r;\n.r out body;")


def _geometry_parts(geometry: list) -> list:
    pts = []
    for g in geometry or []:
        if isinstance(g, dict) and "lat" in g and "lon" in g:
            pts.append((float(g["lon"]), float(g["lat"])))
    return [pts] if len(pts) >= 2 else []


def _names(tags: dict) -> list:
    names = [tags.get("name"), tags.get("name:en"), tags.get("int_name")]
    names += [v for k, v in tags.items() if k.startswith("name:")]
    return list(dict.fromkeys(n for n in names if n))


def parse_overpass(payload: dict) -> list:
    """Overpass `out body geom` JSON → candidates: each relation (its member ways' geometry chained), and the ways
    merged per name (shape.merge_ways). A way that belongs to a relation of the answer records it (member_of)."""
    elements = (payload or {}).get("elements") or []
    ways, rels, rel_members = [], [], {}
    for el in elements:
        t = el.get("type")
        tags = el.get("tags") or {}
        if t == "relation" and el.get("members"):
            parts = [p for m in el["members"] if m.get("type") == "way" for p in _geometry_parts(m.get("geometry"))]
            if parts:
                from services.places.shape import _chain
                rels.append(LineCandidate(_chain(parts), _names(tags), "relation", int(el["id"]), tags, "overpass"))
            rel_members[int(el["id"])] = {int(m["ref"]) for m in el["members"] if m.get("type") == "way"
                                          and m.get("ref") is not None}
        elif t == "way" and el.get("geometry"):
            parts = _geometry_parts(el["geometry"])
            if parts:
                ways.append(LineCandidate(parts, _names(tags), "way", int(el["id"]), tags, "overpass"))
    for w in ways:
        w.member_of = sorted(r for r, ms in rel_members.items() if w.osm_id in ms)
    return rels + merge_ways(ways)


class Overpass:
    name = "overpass"
    endpoint = "overpass"

    def __init__(self, *, http: Any = None, url: str = OVERPASS_URL, min_interval_s: float = OVERPASS_MIN_INTERVAL_S,
                 max_retries: int = 3, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self._http = http
        self._own_http = http is None
        self.url = url
        self.min_interval_s = max(OVERPASS_MIN_INTERVAL_S, float(min_interval_s))
        self.max_retries = max_retries
        self.requests = 0
        self._sleep = sleep
        self._clock = clock
        self._last = None

    def _client(self):
        if self._http is None:
            import httpx
            self._http = httpx.AsyncClient(timeout=OVERPASS_TIMEOUT_S + 15.0,
                                           headers={"User-Agent": "aa-cis-place-shape/1.0 (AA-847)"})
        return self._http

    async def _wait(self):
        if self._last is not None:
            gap = self._clock() - self._last
            if gap < self.min_interval_s:
                await self._sleep(self.min_interval_s - gap)
        self._last = self._clock()

    async def query(self, q: str) -> dict:
        last = "unknown"
        for attempt in range(self.max_retries + 1):
            await self._wait()
            self.requests += 1
            try:
                resp = await self._client().post(self.url, data={"data": q})
            except Exception as e:  # noqa: BLE001
                last = type(e).__name__
            else:
                status = getattr(resp, "status_code", 0)
                if status == 200:
                    try:
                        return resp.json()
                    except Exception as e:  # noqa: BLE001
                        raise ShapeSourceError(f"overpass: invalid JSON ({type(e).__name__})") from None
                if status not in (429, 502, 503, 504):
                    raise ShapeSourceError(f"overpass: HTTP {status}")
                last = f"HTTP {status}"
            if attempt == self.max_retries:
                break
            await self._sleep(min(60.0, 5.0 * (2 ** attempt)))
        raise ShapeSourceError(f"overpass: {last} after {self.max_retries + 1} attempts")

    async def lines(self, name: str, iso2: str, anchors: Sequence[tuple], line_class: str) -> list:
        q = overpass_query(name, iso2, anchors, line_class)
        if q is None:
            return []
        return parse_overpass(await self.query(q))

    async def aclose(self) -> None:
        if self._own_http and self._http is not None:
            await self._http.aclose()
