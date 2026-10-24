"""catalog.http.v1 bodies from catalog view rows (AA-976, root contract docs/contracts/catalog.http.v1.openapi.yaml).

Pure functions: rows in (dicts with the column names of the catalog views, contract catalog.tour.v1.3), contract
objects out. No SQL, no I/O, so the router and the tests share one mapping.

Field sources (every name from the live schema reference, root docs/architecture/db-schema-reference.md):

- ``Tour`` / ``TourSummary``: one ``catalog.v_tour`` row (``source_system``, ``source_id``, ``title``, ``summary``,
  ``country``, ``duration_days``, ``activities``, ``inclusions``, ``exclusions``, ``updated_at``).
- ``days``: ``catalog.v_tour_day`` rows (``day_number``, ``title``, ``body``); each day's ``places`` are the
  ``catalog.v_tour_place`` rows of that ``day_number`` that resolved to a destination (``destination_id``,
  ``destination_name``, ``country``, ``lat``, ``lng``, ``precision``), in ``position`` order.
- ``facets``: activities from ``v_tour.activities`` (source JSON) and ``catalog.v_tour_component.activity``;
  intensity = the highest ``v_tour_component.intensity_level`` (graph enum leisurely / moderate / active /
  strenuous mapped to the contract's easy / moderate / challenging / strenuous).
- ``photos``: ``catalog.v_tour_photo`` rows, ``path`` relative to the CIS public API base (contract catalog.tour.v1
  ``cover_photo_path``), ``place_label`` as the alt text.
- ``Destination``: one ``catalog.v_destination`` row.

``content_hash`` is ``sha256:<hex>`` over the canonical JSON of the served body itself (everything but the hash), so
it changes exactly when what the CRM copies changes, including place, time-zone and photo changes that
``v_tour.content_hash`` (md5 over the v_tour columns only) does not see. The ETag of a tour is that hash.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

from services.catalog_http.geo import FALLBACK_ZONE, country_code, time_zone

SOURCE_SYSTEM = "cis"

# Contract maxLength values (components.schemas).
MAX_TITLE = 300
MAX_TEXT = 500
MAX_NAME = 200
MAX_ACTIVITY = 64
MAX_ALT = 300

_PRECISIONS = frozenset({"point", "area", "line"})
# catalog.graph.v1 intensity_level -> catalog.http.v1 Facets.intensity, in increasing order.
_INTENSITY = {"leisurely": "easy", "moderate": "moderate", "active": "challenging", "strenuous": "strenuous"}
_INTENSITY_RANK = {name: rank for rank, name in enumerate(_INTENSITY)}

_BULLET = re.compile(r"^\s*(?:[-*•·–—>]+|\(?\d{1,2}[.)])\s*")
_SPLIT = re.compile(r"\r?\n|;|\s•\s|\s\|\s")
_SPACES = re.compile(r"\s+")

# (code, suggests_collect_on_arrival, pattern). First match wins, so the specific rules come first. R25 (CRM spec):
# park / entrance fees, permits and porter tips are paid on arrival in Asia; the single supplement is on the R25 list
# too. Flights, insurance and personal spending are not lines the CRM collects at all (false).
_EXCLUSION_RULES: tuple[tuple[str, bool, re.Pattern[str]], ...] = (
    ("visa_on_arrival", True, re.compile(r"\bvisa\b.*\bon arrival\b|\bon arrival\b.*\bvisa\b", re.I)),
    ("visa", False, re.compile(r"\bvisas?\b", re.I)),
    ("tips", True, re.compile(r"\btip(?:s|ping)?\b|\bgratuit(?:y|ies)\b", re.I)),
    ("permit", True, re.compile(r"\bpermits?\b|\bTIMS\b", re.I)),
    ("camera_fee", True, re.compile(r"\b(?:camera|video|photo(?:graphy)?)\s+(?:fees?|charges?|tickets?)\b", re.I)),
    ("entrance_local", True, re.compile(
        r"\b(?:entrance|entry|admission|park|sightseeing|monument|temple|museum)\s+(?:fees?|tickets?|charges?)\b",
        re.I)),
    ("single_supplement", True, re.compile(r"\bsingle\s+(?:room\s+)?supplement\b|\bsingle\s+room\b", re.I)),
    ("international_flights", False, re.compile(r"\binternational\b.*\b(?:flights?|air\s*fares?|airfares?)\b",
                                                re.I)),
    ("flights", False, re.compile(r"\b(?:flights?|air\s*fares?|airfares?)\b", re.I)),
    ("travel_insurance", False, re.compile(r"\binsurance\b", re.I)),
    ("drinks", False, re.compile(r"\b(?:drinks?|beverages?|alcohol(?:ic)?)\b", re.I)),
    ("meals", False, re.compile(r"\b(?:meals?|lunch(?:es)?|dinners?|breakfasts?)\b", re.I)),
    ("personal_expenses", False, re.compile(r"\bpersonal\b|\blaundry\b|\bphone\b|\bexpenses?\b", re.I)),
)


def _clip(text: Optional[str], limit: int) -> Optional[str]:
    if text is None:
        return None
    text = str(text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _coord(value: Any, bound: float) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or not -bound <= number <= bound:   # NaN or out of range
        return None
    return number


def split_lines(text: Optional[str]) -> list[str]:
    """A free-text list (inclusions / exclusions as ingested) -> its items: one per line, ';', ' • ' or ' | ',
    bullets and '1.' markers removed, blanks and repeats dropped, each at most 500 characters."""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in _SPLIT.split(str(text)):
        item = _SPACES.sub(" ", _BULLET.sub("", part)).strip().rstrip(",.").strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(_clip(item, MAX_TEXT))
    return out


def classify_exclusion(text: str) -> tuple[Optional[str], bool]:
    """(code, suggests_collect_on_arrival) for one exclusion line; (None, False) when no rule matches."""
    for code, on_arrival, pattern in _EXCLUSION_RULES:
        if pattern.search(text):
            return code, on_arrival
    return None, False


def exclusions(text: Optional[str]) -> list[dict]:
    items = []
    for line in split_lines(text):
        code, on_arrival = classify_exclusion(line)
        items.append({"code": code, "text": line, "suggests_collect_on_arrival": on_arrival})
    return items


def _activity_names(raw: Any) -> list[str]:
    """Activity names from the source JSON as ingested: a list of strings, a list of {name|type|activity}, a
    {name: truthy} object, a comma-separated string, or a JSON string of any of those."""
    if raw is None:
        return []
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped[:1] in ("[", "{", '"'):
            try:
                return _activity_names(json.loads(stripped))
            except ValueError:
                pass
        return [p for p in re.split(r"[,;/\n]", stripped)]
    if isinstance(raw, dict):
        return [str(k) for k, v in raw.items() if v]
    if isinstance(raw, list):
        names: list[str] = []
        for item in raw:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                for key in ("name", "type", "activity", "label"):
                    if isinstance(item.get(key), str):
                        names.append(item[key])
                        break
        return names
    return []


def facets(tour_activities: Any, components: Iterable[Mapping[str, Any]] = ()) -> dict:
    names: set[str] = set()
    rank = -1
    for name in _activity_names(tour_activities):
        norm = _SPACES.sub(" ", str(name)).strip().lower()
        if norm:
            names.add(norm[:MAX_ACTIVITY])
    for comp in components:
        activity = comp.get("activity")
        if isinstance(activity, str) and activity.strip():
            names.add(_SPACES.sub(" ", activity).strip().lower()[:MAX_ACTIVITY])
        level = comp.get("intensity_level")
        if level in _INTENSITY_RANK:
            rank = max(rank, _INTENSITY_RANK[level])
    intensity = list(_INTENSITY.values())[rank] if rank >= 0 else None
    return {"activities": sorted(names), "intensity": intensity}


def content_hash(body: Mapping[str, Any]) -> str:
    """sha256 over the canonical JSON of a served body, without its own content_hash."""
    payload = {k: v for k, v in body.items() if k != "content_hash"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def photo_url(path: str, base: str) -> Optional[str]:
    if not path:
        return None
    if path.startswith("https://"):
        url = path
    elif path.startswith("/"):
        url = base.rstrip("/") + path
    else:
        return None
    if "size=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "size=large"


def _place(row: Mapping[str, Any], tour_code: Optional[str]) -> dict:
    lat, lng = _coord(row.get("lat"), 90), _coord(row.get("lng"), 180)
    code = country_code(row.get("country")) or tour_code
    zone = time_zone(code, lng, lat) or time_zone(tour_code, None) or FALLBACK_ZONE
    precision = row.get("precision")
    name = row.get("destination_name") or row.get("place_display_name") or row.get("place_name") or ""
    return {
        "destination_id": str(row["destination_id"]),
        "name": _clip(name, MAX_NAME) or "",
        "time_zone": zone,
        "lat": lat,
        "lng": lng,
        "precision": precision if precision in _PRECISIONS else None,
    }


def _days(day_rows: Iterable[Mapping[str, Any]], place_rows: Iterable[Mapping[str, Any]],
          tour_code: Optional[str]) -> list[dict]:
    by_day: dict[int, list[Mapping[str, Any]]] = {}
    for row in place_rows:
        if row.get("destination_id") is None or row.get("day_number") is None:
            continue
        by_day.setdefault(int(row["day_number"]), []).append(row)
    tour_zone = time_zone(tour_code, None) or FALLBACK_ZONE
    days = []
    for row in sorted(day_rows, key=lambda r: int(r.get("day_number") or 0)):
        day_no = int(row.get("day_number") or 0)
        if day_no < 1:      # contract TourDay.day_no minimum 1 (a 'Day 0' block is pre-tour text)
            continue
        places: list[dict] = []
        seen: set[str] = set()
        for prow in sorted(by_day.get(day_no, ()), key=lambda r: int(r.get("position") or 0)):
            key = str(prow["destination_id"])
            if key in seen:
                continue
            seen.add(key)
            places.append(_place(prow, tour_code))
        body = (row.get("body") or "").strip()
        days.append({
            "day_no": day_no,
            "title": _clip(row.get("title") or f"Day {day_no}", MAX_TITLE),
            "description": body or None,
            "time_zone": places[-1]["time_zone"] if places else tour_zone,
            "places": places,
        })
    return days


def tour_body(tour: Mapping[str, Any], *, days: Iterable[Mapping[str, Any]] = (),
              places: Iterable[Mapping[str, Any]] = (), photos: Iterable[Mapping[str, Any]] = (),
              components: Iterable[Mapping[str, Any]] = (), photo_base: str) -> Optional[dict]:
    """The contract ``Tour`` for one live catalog tour, or None when its country has no ISO code (the contract
    requires one; never expected: raw_tours.country is constrained to the AA list)."""
    code = country_code(tour.get("country"))
    if not code:
        return None
    day_list = _days(days, places, code)
    duration = tour.get("duration_days")
    if not isinstance(duration, int) or duration < 1:
        duration = max((d["day_no"] for d in day_list), default=1)
    photo_list: list[dict] = []
    seen_photos: set[str] = set()
    for row in sorted(photos, key=lambda r: int(r.get("position") or 0)):
        key = str(row.get("photo_id") or row.get("path"))
        url = photo_url(row.get("path") or "", photo_base)
        if not url or key in seen_photos:
            continue
        seen_photos.add(key)
        photo_list.append({"url": url, "alt": _clip(row.get("place_label"), MAX_ALT) or None})
    body = {
        "source_system": SOURCE_SYSTEM,
        "source_id": str(tour["source_id"]),
        "title": _clip(tour.get("title") or "", MAX_TITLE) or "",
        "summary": (tour.get("summary") or "").strip() or None,
        "country_code": code,
        "duration_days": duration,
        "days": day_list,
        "inclusions": split_lines(tour.get("inclusions")),
        "exclusions": exclusions(tour.get("exclusions")),
        "facets": facets(tour.get("activities"), components),
        "photos": photo_list,
    }
    body["content_hash"] = content_hash(body)
    return body


def tour_summary(body: Mapping[str, Any], updated_at: Any) -> dict:
    """The contract ``TourSummary`` of a served ``Tour`` body (same content_hash, so the list says when to re-read)."""
    return {
        "source_system": body["source_system"],
        "source_id": body["source_id"],
        "title": body["title"],
        "country_code": body["country_code"],
        "duration_days": body["duration_days"],
        "content_hash": body["content_hash"],
        "updated_at": _iso(updated_at) or _iso(datetime.now(timezone.utc)),
    }


def destination_body(row: Mapping[str, Any]) -> dict:
    """The contract ``Destination`` for one ``catalog.v_destination`` row."""
    code = country_code(row.get("country"))
    lat, lng = _coord(row.get("lat"), 90), _coord(row.get("lng"), 180)
    return {
        "destination_id": str(row["destination_id"]),
        "name": _clip(row.get("name") or "", MAX_NAME) or "",
        "country_code": code,
        "time_zone": time_zone(code, lng, lat) or FALLBACK_ZONE,
        "lat": lat,
        "lng": lng,
    }
