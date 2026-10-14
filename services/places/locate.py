"""AA-712 — where a place is: the model proposes coordinates, the configured geocoder confirms.
Port of AA-TripPlanner-Web backend/extraction/locate.py (AA-675 follow-up, @ 6523816).

Mapbox forward geocoding by name alone is unreliable for Asian place names, even limited to the
tour's country (29/09/2026, Dev): "Chiang Mai" -> a village in Roi Et, "Delhi" -> a village in
Madhya Pradesh. The model knows these places. So:

  1. one a3_place_locate call per batch (BATCH names) returns [lat, lng, country] per name, or null
     when it does not know the place (it must not guess);
  2. the geocoder (services/places/geocoder.py) searches the name around that point; a result within
     CONFIRM_KM in the same country is taken (located_by mapbox / amazon_location), otherwise the
     model's point is kept ('llm');
  3. hard guards (guards.point_ok): AA country, inside its bounding box; a confirming result in
     another country than the model's is ignored.

A failed model batch or a geocoder error leaves the names pending (retried later), never a guess.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

import structlog

from services.places import guards

logger = structlog.get_logger()

BATCH = 40
MAX_TOKENS = 4000
CONFIDENCE = {"mapbox": 0.9, "amazon_location": 0.9, "llm": 0.6}

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
    located_by: str       # mapbox | amazon_location | llm
    place_id: Optional[str] = None

    @property
    def confidence(self) -> float:
        return CONFIDENCE.get(self.located_by, 0.5)


@dataclass(frozen=True)
class Outcome:
    point: Optional[Located]      # None: the model does not know it / guard rejected
    error: Optional[str] = None   # set when the attempt failed (model or geocoder error) → retry later
    reason: Optional[str] = None  # why point is None without an error (unknown / guard)


def build_prompt(items: list[Item]) -> str:
    lines = "\n".join(f"- {i.label} | tour country: {i.country} | tour: {i.tour_name}" for i in items)
    return f"""Places (name | the tour's country | the tour's name, for context):
{lines}

Return ONLY a JSON object mapping each place name, copied exactly, to [latitude, longitude, country]
or null. Country is where the place really is (a Thailand tour may sleep in Siem Reap, Cambodia).
Use the town, village, park or landmark itself; for a hotel, lodge or camp, the town it is in.
Example: {{"Paro": [27.43, 89.41, "Bhutan"], "Unknown Lodge": null}}"""


def parse_answer(raw: str, labels: list[str]) -> dict[str, Optional[tuple[float, float, str]]]:
    """Model JSON -> {label: (lat, lng, canonical country) | None}; anything malformed -> None.
    Raises ValueError when the answer has no JSON object at all (the batch is retried)."""
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        data = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError) as e:
        raise ValueError(f"no JSON object in the locate answer: {str(e)[:80]}") from e
    lower = {str(k).strip().lower(): v for k, v in data.items()} if isinstance(data, dict) else {}
    out: dict[str, Optional[tuple[float, float, str]]] = {}
    for n in labels:
        v = lower.get(n.lower())
        try:
            lat, lng, country = float(v[0]), float(v[1]), guards.canonical_country(v[2])
        except (TypeError, ValueError, IndexError):
            out[n] = None
            continue
        out[n] = (lat, lng, country) if country else None
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
    for batch in batches(items):
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
            lat, lng, country = point
            bad = guards.point_ok(lat, lng, country)
            if bad:
                out[i.key] = Outcome(None, reason=bad)
                continue
            confirmed = None
            if geocoder is not None and geocoder.calls_external:
                try:
                    confirmed = await geocoder.confirm(i.label, lat, lng, country)
                except Exception as e:  # noqa: BLE001 — retried later, never a guess
                    logger.warning("place_geocoder_failed", provider=geocoder.name, error=str(e)[:200])
                    out[i.key] = Outcome(None, error=f"{geocoder.name}: {str(e)[:200]}")
                    continue
            if confirmed is not None and confirmed.country == country \
                    and guards.point_ok(confirmed.lat, confirmed.lng, confirmed.country) is None:
                out[i.key] = Outcome(Located(confirmed.lat, confirmed.lng, confirmed.country,
                                             confirmed.located_by, confirmed.place_id))
            else:
                out[i.key] = Outcome(Located(lat, lng, country, "llm"))
    return out
