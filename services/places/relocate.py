"""AA-997 — re-locate destinations whose point came from the model (S254 geocode audit, fix plan step 1 "then
re-locate the 3,770 located_by = llm rows"), the body of job kind `place_relocate`.

Selection: shared.destinations with `located_by` in the payload's list (default ['llm']; 'mapbox' is allowed too — the
legacy TripPlanner rows the audit found 8/9 right), not merged, not admin-fixed, not yet stamped by this RULE_VERSION
(`geocoder_meta.relocate.v`), optionally one country. Each row is checked against the route of the tours that use it
(store.route_anchors: the located overnight stops of days d-1..d+1, this row excluded).

dry_run (no paid call, no write): counts of what apply would do with no call (non-place names, off-route points,
Hong Kong / Macao points stored as China) and the Geoapify credit estimate (one request per query
geomatch.normalise sends — 1 for most names, 2 for an "X to Y" name — Free plan: $0; a paid plan:
GEOCODER_COST_PER_REQUEST_USD per request) and the days it takes at RELOCATE_DAILY_CREDITS a day.

apply (≤ MAX_ROWS_PER_RUN rows a run, under the Geoapify lock, each row in its own transaction under the destinations
lock), per row:
  - a non-place name (services/places/name_filter.py) → review_reason `non_place:<cause>`, no call;
  - the geocoder confirms the name against the route (or, with no route, within the bake-off radius of the stored point)
    → the confirmed point (located_by 'geoapify', precision by kind, review_reason cleared; China → Hong Kong / Macao
    when the point is there and the key is free) — `store.upsert`'s overwrite rule (guards.may_overwrite) applies;
  - else the stored point stays and is flagged: review_reason `off_route` (far off every route of its tours) or
    `llm_point` (no geocoder confirmed it); with `hide_unconfirmed` the row is also `suspect` (hidden from the catalog
    and from new links, like the outlier rule) — off by default, the owner's call.
Every processed row is stamped `geocoder_meta.relocate = {v, outcome, at}` so a rerun skips it. Nothing is deleted:
component links and destination ids stay as they are (TripPlanner pins them).
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Optional

import structlog

from services.places import geomatch, guards, locate, name_filter, store

logger = structlog.get_logger()

RULE_VERSION = "aa997.1"
LOCATED_BY_ALLOWED = ("llm", "mapbox")
MAX_ROWS_PER_RUN = 400
# Geoapify Free plan: 3,000 credits/day; place_geocode stops at 2,500 (GEOAPIFY_DAILY_CREDIT_LIMIT). The relocate job
# stops RESERVE_CREDITS earlier so new names of the day are still geocoded.
RESERVE_CREDITS = 1000
SAMPLE = 25

OUTCOMES = ("confirmed", "non_place", "off_route", "llm_point", "country_fixed")

_ROWS_SQL = """
    SELECT d.id::text AS id, d.name, d.country, d.lat, d.lng, d.located_by, d.geo_status, d.geo_precision,
           d.review_reason, d.name_norm, d.locality_norm
      FROM shared.destinations d
     WHERE d.located_by = ANY($1::text[]) AND d.duplicate_of IS NULL AND d.geo_status <> 'manual'
       AND ($2::text IS NULL OR lower(btrim(d.country)) = lower(btrim($2)))
       AND (d.geocoder_meta -> 'relocate' ->> 'v') IS DISTINCT FROM $3
     ORDER BY lower(btrim(d.country)), d.id
     LIMIT $4
"""
_COUNT_SQL = """
    SELECT count(*) FROM shared.destinations d
     WHERE d.located_by = ANY($1::text[]) AND d.duplicate_of IS NULL AND d.geo_status <> 'manual'
       AND ($2::text IS NULL OR lower(btrim(d.country)) = lower(btrim($2)))
       AND (d.geocoder_meta -> 'relocate' ->> 'v') IS DISTINCT FROM $3
"""
_KEYS_SQL = """
    SELECT destination_id::text AS d, name_norm, country_norm, locality_norm FROM geo.place_name
     WHERE destination_id = ANY($1::uuid[])
"""
_STAMP_SQL = """
    UPDATE shared.destinations
       SET geocoder_meta = coalesce(geocoder_meta, '{}'::jsonb) || jsonb_build_object('relocate', $2::jsonb)
     WHERE id = $1::uuid
"""
_FLAG_SQL = """
    UPDATE shared.destinations SET review_reason = $2,
           geo_status = CASE WHEN $3 AND geo_status = 'ok' THEN 'suspect' ELSE geo_status END
     WHERE id = $1::uuid AND geo_status <> 'manual'
"""
_HIDE_NAMES_SQL = """
    UPDATE geo.place_name SET status = 'suspect', updated_at = now()
     WHERE destination_id = $1::uuid AND status = 'resolved'
"""
_COUNTRY_SQL = "UPDATE shared.destinations SET country = $2 WHERE id = $1::uuid"


def located_by_list(value) -> list[str]:
    vals = [v for v in (value or ["llm"]) if v in LOCATED_BY_ALLOWED]
    return sorted(set(vals)) or ["llm"]


async def candidates(conn, *, located_by: list[str], country: Optional[str], limit: int) -> list[dict]:
    return [dict(r) for r in await conn.fetch(_ROWS_SQL, located_by, country, RULE_VERSION, limit)]


async def count_candidates(conn, *, located_by: list[str], country: Optional[str]) -> int:
    return int(await conn.fetchval(_COUNT_SQL, located_by, country, RULE_VERSION) or 0)


async def routes_by_destination(conn, rows: list[dict]) -> dict[str, tuple]:
    """{destination id: routes} — the routes of every name key resolved to the row, the row itself excluded."""
    ids = [r["id"] for r in rows]
    if not ids:
        return {}
    keys_by: dict[str, list[tuple]] = {}
    for k in await conn.fetch(_KEYS_SQL, ids):
        keys_by.setdefault(k["d"], []).append((k["name_norm"], k["country_norm"], k["locality_norm"]))
    all_keys = [k for ks in keys_by.values() for k in ks]
    routes = await store.route_anchors(conn, all_keys, exclude=ids)
    return {d: tuple(r for k in ks for r in routes.get(k, ())) for d, ks in keys_by.items()}


def classify_row(row: dict, routes: tuple) -> dict:
    """What apply would do with no call: non-place, current point off the route, Hong Kong / Macao stored as China;
    and the Geoapify requests the row costs (0 for a non-place name)."""
    verdict = name_filter.classify(row["name"], row.get("locality_norm") or None)
    lat, lng = float(row["lat"]), float(row["lng"])
    on = guards.on_route(lat, lng, routes, row["name"])
    refined = guards.refine_country(lat, lng, row["country"])
    queries = geomatch.normalise(verdict.query) if verdict.is_place else []
    return {"non_place": verdict.cause, "query": verdict.query, "on_route": on,
            "route_km": None if on is None else round(guards.route_km(lat, lng, routes), 1),
            "hk_mo": refined if refined != row["country"] else None, "requests": len(queries)}


def _brief(row: dict, c: dict) -> dict:
    return {"id": row["id"], "name": row["name"], "country": row["country"], "lat": row["lat"], "lng": row["lng"],
            **{k: c[k] for k in ("non_place", "route_km", "hk_mo") if c.get(k) is not None}}


async def dry_run(conn, *, located_by: list[str], country: Optional[str], cost_per_request: float,
                  daily_credits: int) -> dict:
    """The report apply works from — no paid call, no write."""
    rows = await candidates(conn, located_by=located_by, country=country, limit=1_000_000)
    routes = await routes_by_destination(conn, rows)
    by_country: dict[str, int] = {}
    non_place: dict[str, int] = {}
    counts = {"rows": len(rows), "with_route": 0, "on_route": 0, "off_route": 0, "no_route": 0, "hk_mo": 0,
              "non_place": 0, "requests": 0}
    samples: dict[str, list] = {"off_route": [], "non_place": [], "hk_mo": []}
    for row in rows:
        c = classify_row(row, routes.get(row["id"], ()))
        by_country[row["country"]] = by_country.get(row["country"], 0) + 1
        counts["requests"] += c["requests"]
        if c["non_place"]:
            counts["non_place"] += 1
            non_place[c["non_place"]] = non_place.get(c["non_place"], 0) + 1
            if len(samples["non_place"]) < SAMPLE:
                samples["non_place"].append(_brief(row, c))
        if c["on_route"] is None:
            counts["no_route"] += 1
        else:
            counts["with_route"] += 1
            counts["on_route" if c["on_route"] else "off_route"] += 1
            if not c["on_route"] and len(samples["off_route"]) < SAMPLE:
                samples["off_route"].append(_brief(row, c))
        if c["hk_mo"]:
            counts["hk_mo"] += 1
            if len(samples["hk_mo"]) < SAMPLE:
                samples["hk_mo"].append(_brief(row, c))
    credits = counts["requests"]
    return {"mode": "dry_run", "rule_version": RULE_VERSION, "located_by": located_by, "country": country,
            **counts, "non_place_by": non_place,
            "by_country": dict(sorted(by_country.items(), key=lambda kv: -kv[1])),
            "estimate": {"geoapify_requests": credits, "geoapify_credits": credits,
                         "cost_usd": round(credits * cost_per_request, 4), "cost_per_request_usd": cost_per_request,
                         "daily_credits": daily_credits,
                         "days": math.ceil(credits / daily_credits) if daily_credits > 0 and credits else 0,
                         "model_calls": 0, "model_cost_usd": 0.0},
            "samples": samples}


def _stamp(outcome: str, **extra) -> str:
    return json.dumps({"v": RULE_VERSION, "outcome": outcome, "at": datetime.now(timezone.utc).isoformat(), **extra},
                      default=str)


async def _fix_country(conn, row: dict, country: str) -> bool:
    """China → Hong Kong / Macao for a point inside them; skipped (False) when that key already has a row."""
    import asyncpg
    try:
        async with conn.transaction():
            await conn.execute(_COUNTRY_SQL, row["id"], country)
        return True
    except asyncpg.UniqueViolationError:
        return False


async def apply_row(conn, row: dict, routes: tuple, geocoder, *, hide_unconfirmed: bool) -> dict:
    """One row (module docstring). Returns {outcome, ...}. Raises the geocoder's CreditLimitReached /
    GeocoderUnavailable (the run stops; the row is not stamped and is taken again next run)."""
    c = classify_row(row, routes)
    if c["non_place"]:
        reason = f"{name_filter.NON_PLACE_PREFIX}:{c['non_place']}"
        async with conn.transaction():
            await store.lock_destinations(conn)
            await conn.execute(_FLAG_SQL, row["id"], reason, hide_unconfirmed)
            if hide_unconfirmed:
                await conn.execute(_HIDE_NAMES_SQL, row["id"])
            await conn.execute(_STAMP_SQL, row["id"], _stamp("non_place", cause=c["non_place"]))
        return {"outcome": "non_place", "cause": c["non_place"]}
    item = locate.Item((row["name_norm"], (row["country"] or "").lower(), row.get("locality_norm") or ""),
                       c["query"], row["country"], routes=routes)
    lat, lng = float(row["lat"]), float(row["lng"])
    res = await geocoder.confirm(c["query"], lat, lng, row["country"], routes=routes)
    hit = locate._confirmed(item, res, row["country"])
    async with conn.transaction():
        await store.lock_destinations(conn)
        if hit is not None:
            if guards.may_overwrite(row["located_by"], row["geo_status"], hit.located_by):
                await store._write_point(conn, row["id"], hit, False)
            fixed = hit.country != row["country"] and await _fix_country(conn, row, hit.country)
            km = round(guards.km(lat, lng, hit.lat, hit.lng), 1)
            await conn.execute(_STAMP_SQL, row["id"], _stamp("confirmed", moved_km=km))
            return {"outcome": "confirmed", "moved_km": km, "country_fixed": bool(fixed)}
        outcome = locate.REVIEW_OFF_ROUTE if c["on_route"] is False else locate.REVIEW_LLM_POINT
        # an off-route flag wins; otherwise a reason the row already carries (unconfirmed_hard_type …) is kept
        reason = outcome if outcome == locate.REVIEW_OFF_ROUTE else (row.get("review_reason") or outcome)
        await conn.execute(_FLAG_SQL, row["id"], reason, hide_unconfirmed)
        if hide_unconfirmed:
            await conn.execute(_HIDE_NAMES_SQL, row["id"])
        fixed = bool(c["hk_mo"]) and await _fix_country(conn, row, c["hk_mo"])
        meta = (res.meta if res is not None else {}) or {}
        await conn.execute(_STAMP_SQL, row["id"], _stamp(outcome, route_km=c["route_km"],
                                                         geocoder=meta.get("outcome")))
    return {"outcome": outcome, "country_fixed": bool(fixed)}


async def apply(pool, geocoder, *, located_by: list[str], country: Optional[str], limit: int,
                hide_unconfirmed: bool = False, progress=None) -> dict:
    """Up to `limit` rows. Result counts per outcome; `stopped_credit_limit` / `stopped_geocoder_unavailable` when the
    geocoder stopped the run; `remaining` = rows still to do."""
    from services.places.geocoder import CreditLimitReached, GeocoderUnavailable

    async with pool.acquire() as conn:
        rows = await candidates(conn, located_by=located_by, country=country, limit=limit)
        routes = await routes_by_destination(conn, rows)
    out: dict = {"mode": "apply", "rule_version": RULE_VERSION, "located_by": located_by, "country": country,
                 "hide_unconfirmed": hide_unconfirmed, "rows": len(rows), "done": 0, "errors": 0,
                 **{o: 0 for o in OUTCOMES}, "moved_km_max": 0.0, "samples": []}
    for n, row in enumerate(rows):
        try:
            async with pool.acquire() as conn:
                r = await apply_row(conn, row, routes.get(row["id"], ()), geocoder,
                                    hide_unconfirmed=hide_unconfirmed)
        except CreditLimitReached as e:
            out["stopped_credit_limit"] = True
            logger.warning("place_relocate_credit_limit", error=str(e)[:200])
            break
        except GeocoderUnavailable as e:
            out["stopped_geocoder_unavailable"] = True
            logger.error("place_relocate_geocoder_unavailable", error=str(e)[:200])
            break
        except Exception as e:  # noqa: BLE001 — one bad row must not stop the run; it is taken again next run
            out["errors"] += 1
            logger.warning("place_relocate_row_failed", destination_id=row["id"], error=str(e)[:200])
            continue
        out["done"] += 1
        out[r["outcome"]] += 1
        out["country_fixed"] += 1 if r.get("country_fixed") else 0
        out["moved_km_max"] = max(out["moved_km_max"], r.get("moved_km") or 0.0)
        notable = r["outcome"] != "confirmed" or (r.get("moved_km") or 0) > geomatch.ACCEPT_KM
        if notable and len(out["samples"]) < SAMPLE:
            out["samples"].append({"id": row["id"], "name": row["name"], "country": row["country"], **r})
        if progress is not None and n % 20 == 19:
            await progress(step="relocate", done=n + 1, total=len(rows))
    async with pool.acquire() as conn:
        out["remaining"] = await count_candidates(conn, located_by=located_by, country=country)
    return out
