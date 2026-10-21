"""AA-712 — where a place is: the model proposes coordinates, the configured geocoder confirms.
Port of AA-TripPlanner-Web backend/extraction/locate.py (AA-675 follow-up, @ 6523816).

Mapbox forward geocoding by name alone is unreliable for Asian place names, even limited to the
tour's country (29/09/2026, Dev): "Chiang Mai" -> a village in Roi Et, "Delhi" -> a village in
Madhya Pradesh. The model knows these places. So:

  1. one a3_place_locate call per batch (BATCH names) returns [lat, lng, country] per name, or null
     when it does not know the place (it must not guess);
  2. the geocoder (services/places/geocoder.py, Geoapify since S236) searches the name in that
     country; a candidate passing the acceptance rule (services/places/geomatch.py: name match, type,
     ≤ 25 km / 50 km of the model's point) is taken (located_by 'geoapify', precision 'point'),
     otherwise the model's point is kept ('llm', precision 'area'; a hard place kind — temple, pass,
     lake, village… — is put on the /admin/places review list). AA-847: a linear feature (river, road, trail) is
     precision 'line' either way — drawn from geo.place_shape (job place_shape), never a pin;
  3. hard guards (guards.point_ok): AA country, inside its bounding box; a confirming result in
     another country than the model's is ignored.

A failed model batch or a geocoder error leaves the names pending (retried later), never a guess.
The geocoder's daily credit guard, and a setup / auth failure (no key, secret unreadable, HTTP 401/403),
defer the rest of the run: no try consumed, no further paid model call.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import structlog

from services.places import geomatch, guards
from services.places.geocoder import (CONFIRMED_BY, Confirmed, CreditLimitReached, GeocoderUnavailable,
                                      Unconfirmed)

logger = structlog.get_logger()

BATCH = 40
MAX_TOKENS = 4000
CONFIDENCE = {"geoapify": 0.9, "mapbox": 0.9, "manual": 1.0, "llm": 0.6}
NATIVE_NAME_COUNTRIES = ("Taiwan", "South Korea")
REVIEW_UNCONFIRMED_HARD = "unconfirmed_hard_type"
# AA-847: the review reason the first validity branch wrote on HIDDEN linear features (geo_status 'suspect'). Owner
# decision S246: lines are drawn, never hidden, so nothing writes it any more; store._write_point un-hides a row that
# still carries it when the name is located again as a line.
REVIEW_LINEAR_FEATURE = "linear_feature"
DEFER_CREDIT_LIMIT = "credit_limit"
DEFER_UNAVAILABLE = "geocoder_unavailable"

SYSTEM_PROMPT = (
    "You are a geographer for Adventure Asia. For each place named on a tour itinerary you give "
    "its coordinates and country. You answer only for places you actually know; for anything "
    "you are unsure of you return null. You never invent a location."
)


@dataclass(frozen=True)
class Item:
    key: tuple            # (name_norm, country_norm, locality_norm) — geo.place_name key
    name: str             # display name to locate
    country: str          # the country it is expected in (card / tour country)
    tour_name: str = ""
    locality: Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.name} ({self.locality})" if self.locality else self.name


@dataclass(frozen=True)
class Located:
    lat: float
    lng: float
    country: str
    located_by: str       # geoapify | llm (mapbox: legacy TripPlanner rows only)
    place_id: Optional[str] = None
    meta: Optional[dict] = field(default=None, compare=False, hash=False)   # geocoder audit (geocoder_meta)
    review_reason: Optional[str] = field(default=None, compare=False, hash=False)
    kind: Optional[str] = field(default=None, compare=False, hash=False)   # geomatch.feature_kind (AA-847)

    @property
    def confidence(self) -> float:
        return CONFIDENCE.get(self.located_by, 0.5)

    @property
    def precision(self) -> str:
        """'point' for a geocoder-confirmed (or admin) point; 'area' for the model's own point — shown as
        an area, never as a pinpoint. AA-847: an area feature (geomatch.feature_kind) is 'area' even when a geocoder
        confirmed it; a LINEAR feature is 'line' (owner decision S246: drawn as a line from geo.place_shape, never a
        pin, a circle or hidden) — Geoapify's point for "Ho Chi Minh Trail" is one spot on a 1,000 km trail, kept only
        as the anchor the shape is searched around."""
        if self.kind == geomatch.LINEAR:
            return "line"
        if self.kind == geomatch.AREA:
            return "area"
        return "point" if self.located_by in CONFIRMED_BY or self.located_by == "manual" else "area"

    @property
    def provider(self) -> Optional[str]:
        return (self.meta or {}).get("provider")


@dataclass(frozen=True)
class Outcome:
    point: Optional[Located]      # None: the model does not know it / guard rejected
    error: Optional[str] = None   # set when the attempt failed (model or geocoder error) → retry later
    reason: Optional[str] = None  # why point is None without an error (unknown / guard)
    deferred: bool = False        # credit guard / geocoder unavailable: stays pending, no try counted (reason)


@dataclass(frozen=True)
class Proposal:
    lat: float
    lng: float
    country: str
    native_name: Optional[str] = None   # 中文 / 한글 name (Taiwan, South Korea) — the geocoder's 2nd query


def build_prompt(items: list[Item]) -> str:
    lines = "\n".join(f"- {i.label} | tour country: {i.country} | tour: {i.tour_name}" for i in items)
    return f"""Places (name | the tour's country | the tour's name, for context):
{lines}

Return ONLY a JSON object mapping each place name, copied exactly, to [latitude, longitude, country]
or null. Country is where the place really is (a Thailand tour may sleep in Siem Reap, Cambodia).
Use the town, village, park or landmark itself; for a hotel, lodge or camp, the town it is in.
For a place in Taiwan or South Korea, add its name in the local script (Chinese / Hangul) as a 4th
element when you know it.
Example: {{"Paro": [27.43, 89.41, "Bhutan"], "Taroko Gorge": [24.16, 121.62, "Taiwan", "太魯閣"],
"Unknown Lodge": null}}"""


def parse_answer(raw: str, labels: list[str]) -> dict[str, Optional[Proposal]]:
    """Model JSON -> {label: Proposal(lat, lng, canonical country, native name?) | None}; anything
    malformed -> None; a native name is kept only in native script for Taiwan / South Korea.
    Raises ValueError when the answer has no JSON object at all (the batch is retried)."""
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        data = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"no JSON object in the locate answer: {str(e)[:80]}") from e
    lower = {str(k).strip().lower(): v for k, v in data.items()} if isinstance(data, dict) else {}
    out: dict[str, Optional[Proposal]] = {}
    for n in labels:
        v = lower.get(n.lower())
        try:
            lat, lng, country = float(v[0]), float(v[1]), guards.canonical_country(v[2])
        except (TypeError, ValueError, IndexError, KeyError):
            out[n] = None
            continue
        native = v[3] if len(v) > 3 and isinstance(v[3], str) else None
        if not (country in NATIVE_NAME_COUNTRIES and geomatch.has_native_script(native)):
            native = None
        out[n] = Proposal(lat, lng, country, native.strip() if native else None) if country else None
    return out


def batches(items: list[Item], size: int = BATCH) -> list[list[Item]]:
    """Batches of ≤ size items with distinct labels (the answer is keyed by label)."""
    out: list[list[Item]] = []
    for it in items:
        for b in out:
            if len(b) < size and all(x.label.lower() != it.label.lower() for x in b):
                b.append(it)
                break
        else:
            out.append([it])
    return out


GenerateFn = Callable[[str, str], Awaitable[str]]


async def locate_many(items: list[Item], generate: GenerateFn, geocoder) -> dict[tuple, Outcome]:
    """Outcome per item key. `generate` raises to stop (e.g. BudgetExceeded propagates)."""
    from shared.cost_guard import BudgetExceeded

    out: dict[tuple, Outcome] = {}
    stopped: Optional[str] = None     # DEFER_* once the geocoder cannot check any more names this run
    external = geocoder is not None and getattr(geocoder, "calls_external", False)
    ensure_ready = getattr(geocoder, "ensure_ready", None) if external else None
    if ensure_ready is not None:
        try:
            ensure_ready()            # no paid model call when the key cannot even be read
        except GeocoderUnavailable as e:
            logger.error("place_geocoder_unavailable", provider=geocoder.name, error=str(e)[:200])
            stopped = DEFER_UNAVAILABLE
    for batch in batches(items):
        if stopped:   # no more paid model calls for names the geocoder cannot check
            for i in batch:
                out[i.key] = Outcome(None, reason=stopped, deferred=True)
            continue
        labels = [i.label for i in batch]
        try:
            answer = parse_answer(await generate(SYSTEM_PROMPT, build_prompt(batch)), labels)
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001 — a failed batch stays pending
            logger.warning("place_locate_batch_failed", names=len(batch), error=str(e)[:200])
            for i in batch:
                out[i.key] = Outcome(None, error=f"locate: {str(e)[:200]}")
            continue
        for i in batch:
            point = answer.get(i.label)
            if point is None:
                out[i.key] = Outcome(None, reason="unknown_to_model")
                continue
            lat, lng, country = point.lat, point.lng, point.country
            bad = guards.point_ok(lat, lng, country)
            if bad:
                out[i.key] = Outcome(None, reason=bad)
                continue
            res = None
            if external and not stopped:
                try:
                    # the plain name: a generic name's locality is in the key, not in the query
                    res = await geocoder.confirm(i.name, lat, lng, country, native_name=point.native_name)
                except CreditLimitReached as e:
                    logger.warning("place_geocoder_credit_limit", provider=geocoder.name, error=str(e)[:200])
                    stopped = DEFER_CREDIT_LIMIT
                except GeocoderUnavailable as e:
                    logger.error("place_geocoder_unavailable", provider=geocoder.name, error=str(e)[:200])
                    stopped = DEFER_UNAVAILABLE
                except Exception as e:  # noqa: BLE001 — retried later, never a guess
                    logger.warning("place_geocoder_failed", provider=geocoder.name, error=str(e)[:200])
                    out[i.key] = Outcome(None, error=f"{geocoder.name}: {str(e)[:200]}")
                    continue
            if stopped:
                out[i.key] = Outcome(None, reason=stopped, deferred=True)
                continue
            out[i.key] = Outcome(_located(i, lat, lng, country, res))
    return out


def _located(item: Item, lat: float, lng: float, country: str, res) -> Located:
    """The confirmed point when the geocoder accepted a candidate in the model's country, else the model's
    point ('llm', area precision; hard place kinds go to the review list)."""
    if isinstance(res, Confirmed) and res.country == country \
            and guards.point_ok(res.lat, res.lng, res.country) is None:
        kind = geomatch.feature_kind(item.name, (res.meta or {}).get("result_type"))
        return Located(res.lat, res.lng, res.country, res.located_by, res.place_id, meta=dict(res.meta), kind=kind)
    meta = None
    if isinstance(res, Confirmed):     # accepted by the rule but in another country / outside its bbox
        meta = {**res.meta, "outcome": "rejected_country_guard"}
    elif isinstance(res, Unconfirmed):
        meta = dict(res.meta)
    kind = geomatch.feature_kind(item.name)
    # a line's geometry is searched by name (place_shape: Overpass) around this point: no pin review for it
    review = REVIEW_UNCONFIRMED_HARD if kind != geomatch.LINEAR and geomatch.is_hard(item.name) else None
    return Located(lat, lng, country, "llm", meta=meta, review_reason=review, kind=kind)
