"""AA-997 — re-locate destinations whose point came from the model (S254 geocode audit, fix plan step 1 "then
re-locate the 3,770 located_by = llm rows"), the body of job kind `place_relocate`.

Selection: shared.destinations with `located_by` in the payload's list (default ['llm']; 'mapbox' is allowed too — the
legacy TripPlanner rows the audit found 8/9 right), not merged, not admin-fixed, not yet stamped by this RULE_VERSION
(`geocoder_meta.relocate.v`), optionally one country. Each row is checked against the route of the tours that use it
(store.route_anchors: the located overnight stops of days d-1..d+1, this row excluded).
AA-997 v2 (RULE_VERSION aa997.2, S257 interim audit): a row an earlier version already stamped is taken again only
when its outcome is in the payload's `recheck` list (RECHECK_ALLOWED): `llm_point` / `off_route` (the model's point is
still there: the row is re-located with the new rule), `confirmed` (an aa997.1 confirmation, located_by 'geoapify'
now: re-judged first from its stored geocoder_meta at no cost, re-searched only when the stricter rule refuses it).
Untouched rows come first, then rechecks.

dry_run (no paid call, no write): counts of what apply would do with no call (non-place names, off-route points,
Hong Kong / Macao points stored as China), the re-judge of `confirmed` rows, and the Geoapify request estimate (an
upper bound: every query the strategy may send — v1: geomatch.normalise, 1 or 2; v2: geomatch.query_plan, ≤ 3 —
Free plan: $0; a paid plan: GEOCODER_COST_PER_REQUEST_USD per request) and the days it takes at RELOCATE_DAILY_CREDITS.

apply (≤ MAX_ROWS_PER_RUN rows a run, under the Geoapify lock, each row in its own transaction under the destinations
lock), per row:
  - a non-place name (services/places/name_filter.py) → review_reason `non_place:<cause>`, no call;
  - the geocoder confirms the name against the route (or, with no route, within the bake-off radius of the stored point)
    → the confirmed point (located_by 'geoapify', precision by kind, review_reason cleared; China → Hong Kong / Macao
    when the point is there and the key is free) — `store.upsert`'s overwrite rule (guards.may_overwrite) applies; the
    stamp keeps the previous point (`prev`) so a wrong move can be undone;
  - else the stored point stays and is flagged: review_reason `off_route` (far off every route of its tours) or
    `llm_point` (no geocoder confirmed it); with `hide_unconfirmed` the row is also `suspect` (hidden from the catalog
    and from new links, like the outlier rule) — off by default, the owner's call;
  - a re-checked Geoapify row (an aa997.1 `confirmed` row, or with recheck `geoapify` a place_geocode row never
    touched): kept as is (`kept`, no call) when its stored answer passes the strict rule; else re-searched (reference:
    the route, or the current point when there is none) — a passing answer is written (`reconfirmed`), none →
    review_reason `weak_confirm` (point kept).
Overnight towns are taken first (they geocode well and, once confirmed, give the later runs' sites their route). A
row an admin fixed, or place_dedupe merged, after the run read its selection is `skipped` (re-read under the lock).
Every processed row is stamped `geocoder_meta.relocate = {v, outcome, at, …}` so a rerun skips it. Nothing is deleted:
component links and destination ids stay as they are (TripPlanner pins them).

hide / unhide (no paid call; AA-997 v2, the readers' "hide unconfirmed" policy, owner's call): `hide` lists — and with
`confirm` sets `suspect` on — rows still carrying one of HIDE_REASONS (the catalog, TripPlanner and new links already
skip `suspect` rows), stamped `geocoder_meta.hidden`; `unhide` reverts exactly the rows that stamp marks.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Optional

import structlog

from services.places import geomatch, guards, locate, name_filter, store

logger = structlog.get_logger()

RULE_VERSION = "aa997.2"
LOCATED_BY_ALLOWED = ("llm", "mapbox")
# outcomes of an earlier rule version that a v2 run may take again (payload `recheck`)
# (`geoapify` = rows place_geocode located with Geoapify, never touched by relocate: re-judged from their stored answer)
RECHECK_ALLOWED = ("llm_point", "off_route", "confirmed", "geoapify")
MAX_ROWS_PER_RUN = 400
# Geoapify Free plan: 3,000 credits/day; place_geocode stops at 2,500 (GEOAPIFY_DAILY_CREDIT_LIMIT). The relocate job
# stops RESERVE_CREDITS earlier so new names of the day are still geocoded.
RESERVE_CREDITS = 1000
SAMPLE = 25
REVIEW_WEAK_CONFIRM = "weak_confirm"      # an aa997.1 confirmation the strict rule refuses and no new search confirms
HIDE_REASONS = (locate.REVIEW_LLM_POINT, locate.REVIEW_OFF_ROUTE, locate.REVIEW_UNCONFIRMED_HARD, REVIEW_WEAK_CONFIRM)
# opt-in only (never in the default set): pins of names the pre-filter judged not a place (`non_place:<cause>`) — 25/28
# verified not-a-place names, but also 3/439 verified-correct names (S254 fixture), so it is asked for explicitly
HIDE_NON_PLACE = name_filter.NON_PLACE_PREFIX
HIDE_STAMP = "aa997.2-policy"

OUTCOMES = ("confirmed", "non_place", "off_route", "llm_point", "country_fixed", "skipped", "kept", "reconfirmed",
            "weak_confirm")

_ROWS_SQL = """
    SELECT d.id::text AS id, d.name, d.country, d.lat, d.lng, d.located_by, d.geo_status, d.geo_precision,
           d.review_reason, d.name_norm, d.locality_norm, d.geocoder_meta
      FROM shared.destinations d
     WHERE d.duplicate_of IS NULL AND d.geo_status <> 'manual'
       AND ($2::text IS NULL OR lower(btrim(d.country)) = lower(btrim($2)))
       AND (d.geocoder_meta -> 'relocate' ->> 'v') IS DISTINCT FROM $3
       AND ((d.located_by = ANY($1::text[])
             AND ((d.geocoder_meta -> 'relocate') IS NULL
                  OR (d.geocoder_meta -> 'relocate' ->> 'outcome') = ANY($5::text[])))
            OR ('confirmed' = ANY($5::text[]) AND d.located_by = 'geoapify'
                AND (d.geocoder_meta -> 'relocate' ->> 'outcome') = 'confirmed')
            OR ('geoapify' = ANY($5::text[]) AND d.located_by = 'geoapify'
                AND (d.geocoder_meta -> 'relocate') IS NULL))
     -- untouched rows first; then overnight towns: once confirmed they are route anchors (store.route_anchors) for the
     -- sites of later runs
     ORDER BY (d.geocoder_meta -> 'relocate') IS NOT NULL,
              EXISTS (SELECT 1 FROM geo.tour_day td WHERE td.overnight_destination_id = d.id) DESC,
              lower(btrim(d.country)), d.id
     LIMIT $4
"""
_COUNT_SQL = """
    SELECT count(*) FROM shared.destinations d
     WHERE d.duplicate_of IS NULL AND d.geo_status <> 'manual'
       AND ($2::text IS NULL OR lower(btrim(d.country)) = lower(btrim($2)))
       AND (d.geocoder_meta -> 'relocate' ->> 'v') IS DISTINCT FROM $3
       AND ((d.located_by = ANY($1::text[])
             AND ((d.geocoder_meta -> 'relocate') IS NULL
                  OR (d.geocoder_meta -> 'relocate' ->> 'outcome') = ANY($4::text[])))
            OR ('confirmed' = ANY($4::text[]) AND d.located_by = 'geoapify'
                AND (d.geocoder_meta -> 'relocate' ->> 'outcome') = 'confirmed')
            OR ('geoapify' = ANY($4::text[]) AND d.located_by = 'geoapify'
                AND (d.geocoder_meta -> 'relocate') IS NULL))
"""
_KEYS_SQL = """
    SELECT destination_id::text AS d, name_norm, country_norm, locality_norm FROM geo.place_name
     WHERE destination_id = ANY($1::uuid[])
"""
# strategy v2: the overnight towns of days d-1..d of the tours using a name (the context of a homonym query)
_TOWNS_SQL = """
    WITH k AS (SELECT * FROM unnest($1::text[], $2::text[], $3::text[]) AS k(n, c, l)),
         uses AS (
           SELECT k.n, k.c, k.l, tp.source_id, tp.day_index
             FROM geo.tour_place tp JOIN k ON (tp.name_norm, tp.country_norm, tp.locality_norm) = (k.n, k.c, k.l)
           UNION
           SELECT k.n, k.c, k.l, td.source_id, td.day_index
             FROM geo.tour_day td JOIN k ON (td.anchor_norm, td.anchor_country_norm, td.anchor_locality_norm)
                                            = (k.n, k.c, k.l))
    SELECT u.n, u.c, u.l, d.name, d.lat, d.lng, count(*) AS uses
      FROM uses u
      JOIN geo.tour_day td ON td.source_id = u.source_id AND td.day_index BETWEEN u.day_index - 1 AND u.day_index
      JOIN shared.destinations d ON d.id = td.overnight_destination_id
     WHERE d.located_by IN ('geoapify', 'manual', 'mapbox') AND d.geo_status <> 'suspect' AND d.duplicate_of IS NULL
       AND d.geo_precision IS DISTINCT FROM 'line' AND NOT (d.id = ANY($4::uuid[]))
     GROUP BY 1, 2, 3, 4, 5, 6
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
# re-read under the destinations lock: an admin may fix the row, or place_dedupe merge it, while the run (≤ 400 rows,
# minutes of geocoder calls) works from the selection it read at the start
_CURRENT_SQL = """
    SELECT located_by, geo_status, duplicate_of IS NOT NULL AS merged FROM shared.destinations
     WHERE id = $1::uuid FOR UPDATE
"""
# hide / unhide policy (no call)
_HIDE_CANDIDATES_SQL = """
    SELECT d.id::text AS id, d.name, d.country, d.review_reason, d.located_by
      FROM shared.destinations d
     WHERE d.duplicate_of IS NULL AND d.geo_status = 'ok'
       AND (d.review_reason = ANY($1::text[]) OR ($3::bool AND d.review_reason LIKE ($4::text || ':%')))
       AND ($2::text IS NULL OR lower(btrim(d.country)) = lower(btrim($2)))
     ORDER BY lower(btrim(d.country)), d.id
"""
_HIDE_ROW_SQL = """
    UPDATE shared.destinations
       SET geo_status = 'suspect',
           geocoder_meta = coalesce(geocoder_meta, '{}'::jsonb) || jsonb_build_object('hidden', $2::jsonb)
     WHERE id = $1::uuid AND geo_status = 'ok' AND duplicate_of IS NULL
"""
_UNHIDE_ROWS_SQL = """
    SELECT d.id::text AS id FROM shared.destinations d
     WHERE d.geo_status = 'suspect' AND (d.geocoder_meta -> 'hidden' ->> 'by') = $1
       AND ($2::text IS NULL OR lower(btrim(d.country)) = lower(btrim($2)))
"""
_UNHIDE_ROW_SQL = """
    UPDATE shared.destinations SET geo_status = 'ok', geocoder_meta = geocoder_meta - 'hidden'
     WHERE id = $1::uuid AND geo_status = 'suspect'
"""
_UNHIDE_NAMES_SQL = """
    UPDATE geo.place_name SET status = 'resolved', updated_at = now()
     WHERE destination_id = $1::uuid AND status = 'suspect'
"""


async def _still_due(conn, row: dict) -> Optional[dict]:
    """The row's current located_by / geo_status when it may still be processed (not admin-fixed, not merged, still
    one of the selected sources), else None."""
    cur = await conn.fetchrow(_CURRENT_SQL, row["id"])
    if cur is None or cur["merged"] or cur["geo_status"] == "manual" or cur["located_by"] != row["located_by"]:
        return None
    return dict(cur)


def located_by_list(value) -> list[str]:
    vals = [v for v in (value or ["llm"]) if v in LOCATED_BY_ALLOWED]
    return sorted(set(vals)) or ["llm"]


def recheck_list(value) -> list[str]:
    return sorted({v for v in (value or []) if v in RECHECK_ALLOWED})


async def candidates(conn, *, located_by: list[str], country: Optional[str], limit: int,
                     recheck: Optional[list[str]] = None) -> list[dict]:
    return [dict(r) for r in await conn.fetch(_ROWS_SQL, located_by, country, RULE_VERSION, limit,
                                              recheck_list(recheck))]


async def count_candidates(conn, *, located_by: list[str], country: Optional[str],
                           recheck: Optional[list[str]] = None) -> int:
    return int(await conn.fetchval(_COUNT_SQL, located_by, country, RULE_VERSION, recheck_list(recheck)) or 0)


async def _keys_by_destination(conn, ids: list[str]) -> dict[str, list[tuple]]:
    keys_by: dict[str, list[tuple]] = {}
    for k in await conn.fetch(_KEYS_SQL, ids):
        keys_by.setdefault(k["d"], []).append((k["name_norm"], k["country_norm"], k["locality_norm"]))
    return keys_by


async def routes_by_destination(conn, rows: list[dict]) -> dict[str, tuple]:
    """{destination id: routes} — the routes of every name key resolved to the row, the row itself excluded."""
    ids = [r["id"] for r in rows]
    if not ids:
        return {}
    keys_by = await _keys_by_destination(conn, ids)
    all_keys = [k for ks in keys_by.values() for k in ks]
    routes = await store.route_anchors(conn, all_keys, exclude=ids)
    return {d: tuple(r for k in ks for r in routes.get(k, ())) for d, ks in keys_by.items()}


async def towns_by_destination(conn, rows: list[dict]) -> dict[str, list[tuple]]:
    """{destination id: [(town name, lat, lng, uses), …]} — the overnight towns of days d-1..d of the tours using the
    row's names (strategy v2: the context of the second query)."""
    ids = [r["id"] for r in rows]
    if not ids:
        return {}
    keys_by = await _keys_by_destination(conn, ids)
    all_keys = list(dict.fromkeys(k for ks in keys_by.values() for k in ks))
    if not all_keys:
        return {}
    a, b, c = (list(x) for x in zip(*all_keys))
    by_key: dict[tuple, list[tuple]] = {}
    for r in await conn.fetch(_TOWNS_SQL, a, b, c, ids):
        by_key.setdefault((r["n"], r["c"], r["l"]), []).append(
            (r["name"], float(r["lat"]), float(r["lng"]), int(r["uses"])))
    return {d: [t for k in ks for t in by_key.get(k, [])] for d, ks in keys_by.items()}


def context_town(name: str, towns: list[tuple], lat: Optional[float], lng: Optional[float]) -> Optional[str]:
    """The town to add to a homonym query: the most used overnight town of the name's days, ties broken by the
    distance to the stored point; None when there is none or it is the place itself."""
    best = None
    for t_name, t_lat, t_lng, uses in towns or ():
        if not t_name or geomatch.name_sim(t_name, name) >= 0.999 or geomatch.name_sim(name, t_name) >= 0.999:
            continue
        dist = guards.km(lat, lng, t_lat, t_lng) if lat is not None and lng is not None else 0.0
        key = (-uses, dist)
        if best is None or key < best[0]:
            best = (key, t_name)
    return best[1] if best else None


def _requests_for(query: str, strategy: str, context: Optional[str] = None) -> int:
    from services.places.geocoder import STRATEGY_V2, V2_MAX_QUERIES
    if strategy == STRATEGY_V2:
        return len(geomatch.query_plan(query, context=context, max_queries=V2_MAX_QUERIES))
    return len(geomatch.normalise(query))


def classify_row(row: dict, routes: tuple, *, strategy: str = "v1", context: Optional[str] = None) -> dict:
    """What apply would do with no call: non-place, current point off the route, Hong Kong / Macao stored as China;
    and the Geoapify requests the row may cost (an upper bound; 0 for a non-place name)."""
    verdict = name_filter.classify(row["name"], row.get("locality_norm") or None)
    lat, lng = float(row["lat"]), float(row["lng"])
    on = guards.on_route(lat, lng, routes, row["name"])
    refined = guards.refine_country(lat, lng, row["country"])
    requests = _requests_for(verdict.query, strategy, context) if verdict.is_place else 0
    return {"non_place": verdict.cause, "query": verdict.query, "on_route": on,
            "route_km": None if on is None else round(guards.route_km(lat, lng, routes), 1),
            "hk_mo": refined if refined != row["country"] else None, "requests": requests}


def _meta(row: dict) -> dict:
    m = row.get("geocoder_meta")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except ValueError:
            m = None
    return m if isinstance(m, dict) else {}


def _stamp_of(row: dict) -> dict:
    s = _meta(row).get("relocate")
    return s if isinstance(s, dict) else {}


def is_confirmed_recheck(row: dict) -> bool:
    """A Geoapify-located row taken by `recheck` (an aa997.1 `confirmed` row, or a place_geocode row never touched)."""
    return row.get("located_by") == "geoapify" and _stamp_of(row).get("outcome") in ("confirmed", None)


def rejudge(row: dict, routes: tuple) -> dict:
    """The stored aa997.1 confirmation of a row against the strict rule (AA-997 v2), with no call: {'ok': bool,
    'reason': why refused | None}. The candidate is rebuilt from geocoder_meta (name, result_type, match_type) at the
    row's point; the model's old point is gone, so the rule runs with the route as the only reference (no hint: a
    kind conflict is refused, hint_disagree cannot be judged). No stored candidate name → not judgeable, refused."""
    meta = _meta(row)
    query = meta.get("judged") or meta.get("query") or row["name"]    # v2 stores the bare name judged as `judged`
    cname = meta.get("name")
    if not cname:
        return {"ok": False, "reason": "no_meta"}
    iso = guards.iso2(row["country"])
    c = geomatch.Candidate(float(row["lat"]), float(row["lng"]), iso, cname, meta.get("result_type") or "",
                           meta.get("confidence"), meta.get("match_type"), None, meta.get("source"))
    settlement = geomatch.is_settlement(query)
    sim = geomatch.name_sim(query, cname)
    if not (sim >= 0.999 or (sim >= 0.5 and len(geomatch.tokens(query)) >= 2)):
        return {"ok": False, "reason": "name"}
    if c.result_type in geomatch.REJECT_TYPES or (c.result_type == "city" and not settlement):
        return {"ok": False, "reason": "type"}
    # an aa997.1 day-trip accept (off the route radius, within DAY_TRIP_KM): judged with the day-trip radius as v2 does
    day_trip = guards.has_route(routes) and guards.on_route(c.lat, c.lng, routes, query) is False
    if day_trip and (guards.route_km(c.lat, c.lng, routes) or 0.0) > guards.DAY_TRIP_KM:
        return {"ok": False, "reason": "off_route"}
    why = geomatch.strict_reason(query, c, sim, None, None, routes, settlement=settlement, day_trip=day_trip)
    return {"ok": why is None, "reason": why}


def _brief(row: dict, c: dict) -> dict:
    return {"id": row["id"], "name": row["name"], "country": row["country"], "lat": row["lat"], "lng": row["lng"],
            **{k: c[k] for k in ("non_place", "route_km", "hk_mo") if c.get(k) is not None}}


async def dry_run(conn, *, located_by: list[str], country: Optional[str], cost_per_request: float,
                  daily_credits: int, recheck: Optional[list[str]] = None, strategy: str = "v1",
                  max_requests: Optional[int] = None) -> dict:
    """The report apply works from — no paid call, no write."""
    rows = await candidates(conn, located_by=located_by, country=country, limit=1_000_000, recheck=recheck)
    routes = await routes_by_destination(conn, rows)
    towns = await towns_by_destination(conn, rows) if strategy == "v2" else {}
    by_country: dict[str, int] = {}
    non_place: dict[str, int] = {}
    counts = {"rows": len(rows), "with_route": 0, "on_route": 0, "off_route": 0, "no_route": 0, "hk_mo": 0,
              "non_place": 0, "requests": 0, "with_context": 0}
    by_prev: dict[str, int] = {}
    judged = {"rows": 0, "kept": 0, "refused": 0, "refused_by": {}}
    samples: dict[str, list] = {"off_route": [], "non_place": [], "hk_mo": [], "rejudge_refused": []}
    for row in rows:
        prev = _stamp_of(row).get("outcome") or "untouched"
        by_prev[prev] = by_prev.get(prev, 0) + 1
        r_routes = routes.get(row["id"], ())
        if is_confirmed_recheck(row):
            j = rejudge(row, r_routes)
            judged["rows"] += 1
            if j["ok"]:
                judged["kept"] += 1
                continue
            judged["refused"] += 1
            judged["refused_by"][j["reason"]] = judged["refused_by"].get(j["reason"], 0) + 1
            if len(samples["rejudge_refused"]) < SAMPLE:
                samples["rejudge_refused"].append({"id": row["id"], "name": row["name"], "country": row["country"],
                                                   "reason": j["reason"],
                                                   "candidate": _meta(row).get("name"),
                                                   "result_type": _meta(row).get("result_type")})
        ctx = context_town(row["name"], towns.get(row["id"], []), row["lat"], row["lng"]) if towns else None
        c = classify_row(row, r_routes, strategy=strategy, context=ctx)
        counts["with_context"] += 1 if ctx else 0
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
    capped = min(credits, max_requests) if max_requests else credits
    return {"mode": "dry_run", "rule_version": RULE_VERSION, "located_by": located_by, "country": country,
            "recheck": recheck_list(recheck), "strategy": strategy, "max_requests": max_requests,
            **counts, "by_previous_outcome": by_prev, "rejudge_confirmed": judged, "non_place_by": non_place,
            "by_country": dict(sorted(by_country.items(), key=lambda kv: -kv[1])),
            "estimate": {"geoapify_requests": credits, "geoapify_credits": credits, "requests_upper_bound": True,
                         "capped_requests": capped,
                         "cost_usd": round(capped * cost_per_request, 4), "cost_per_request_usd": cost_per_request,
                         "daily_credits": daily_credits,
                         "days": math.ceil(capped / daily_credits) if daily_credits > 0 and capped else 0,
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


def _keep_linear(row: dict, reason: str) -> str:
    """A row already flagged linear_feature keeps that reason: store._write_point un-hides the row on it once a
    geocoder line lands, so overwriting it with off_route / non_place would leave the row hidden for good."""
    return locate.REVIEW_LINEAR_FEATURE if row.get("review_reason") == locate.REVIEW_LINEAR_FEATURE else reason


def _prev(row: dict) -> list:
    return [row["lat"], row["lng"], row.get("located_by")]


async def _apply_confirmed_recheck(conn, row: dict, routes: tuple, geocoder, *, hide_unconfirmed: bool,
                                   strategy: str, context: Optional[str]) -> dict:
    """An aa997.1 `confirmed` row (located_by geoapify): kept when its stored answer passes the strict rule (no call),
    else searched again with the route as reference — a passing answer is written (`reconfirmed`), none flags
    `weak_confirm` (the point stays; hidden too with hide_unconfirmed)."""
    j = rejudge(row, routes)
    if j["ok"]:
        async with conn.transaction():
            await store.lock_destinations(conn)
            if await _still_due(conn, row) is None:
                return {"outcome": "skipped"}
            await conn.execute(_STAMP_SQL, row["id"], _stamp("kept", prev_v=_stamp_of(row).get("v") or "geocode"))
        return {"outcome": "kept"}
    query = name_filter.classify(row["name"], row.get("locality_norm") or None).query or row["name"]
    item = locate.Item((row["name_norm"], (row["country"] or "").lower(), row.get("locality_norm") or ""),
                       query, row["country"], routes=routes)
    kw = {"strategy": strategy, "context": context} if strategy != "v1" else {}
    lat, lng = float(row["lat"]), float(row["lng"])
    # the reference: the tour route; with none, the current point (it passed the bake-off radius of the model's point
    # once — the right area, the wrong feature is what the strict rule refused)
    h_lat, h_lng = (None, None) if guards.has_route(routes) else (lat, lng)
    res = await geocoder.confirm(query, h_lat, h_lng, row["country"], routes=routes, **kw)
    hit = locate._confirmed(item, res, row["country"])
    async with conn.transaction():
        await store.lock_destinations(conn)
        if await _still_due(conn, row) is None:
            return {"outcome": "skipped"}
        if hit is not None:
            await store._write_point(conn, row["id"], hit, False)
            km = round(guards.km(lat, lng, hit.lat, hit.lng), 1)
            await conn.execute(_STAMP_SQL, row["id"], _stamp("reconfirmed", moved_km=km, prev=_prev(row),
                                                             refused=j["reason"]))
            return {"outcome": "reconfirmed", "moved_km": km}
        await conn.execute(_FLAG_SQL, row["id"], _keep_linear(row, REVIEW_WEAK_CONFIRM), hide_unconfirmed)
        if hide_unconfirmed:
            await conn.execute(_HIDE_NAMES_SQL, row["id"])
        meta = (res.meta if res is not None else {}) or {}
        await conn.execute(_STAMP_SQL, row["id"], _stamp("weak_confirm", refused=j["reason"],
                                                         geocoder=meta.get("outcome")))
    return {"outcome": "weak_confirm", "refused": j["reason"]}


async def apply_row(conn, row: dict, routes: tuple, geocoder, *, hide_unconfirmed: bool, strategy: str = "v1",
                    context: Optional[str] = None) -> dict:
    """One row (module docstring). Returns {outcome, ...}. Raises the geocoder's CreditLimitReached /
    GeocoderUnavailable (the run stops; the row is not stamped and is taken again next run)."""
    if is_confirmed_recheck(row):
        return await _apply_confirmed_recheck(conn, row, routes, geocoder, hide_unconfirmed=hide_unconfirmed,
                                              strategy=strategy, context=context)
    c = classify_row(row, routes)
    if c["non_place"]:
        reason = _keep_linear(row, f"{name_filter.NON_PLACE_PREFIX}:{c['non_place']}")
        async with conn.transaction():
            await store.lock_destinations(conn)
            if await _still_due(conn, row) is None:
                return {"outcome": "skipped"}
            await conn.execute(_FLAG_SQL, row["id"], reason, hide_unconfirmed)
            if hide_unconfirmed:
                await conn.execute(_HIDE_NAMES_SQL, row["id"])
            await conn.execute(_STAMP_SQL, row["id"], _stamp("non_place", cause=c["non_place"]))
        return {"outcome": "non_place", "cause": c["non_place"]}
    item = locate.Item((row["name_norm"], (row["country"] or "").lower(), row.get("locality_norm") or ""),
                       c["query"], row["country"], routes=routes)
    lat, lng = float(row["lat"]), float(row["lng"])
    kw = {"strategy": strategy, "context": context} if strategy != "v1" else {}
    res = await geocoder.confirm(c["query"], lat, lng, row["country"], routes=routes, **kw)
    hit = locate._confirmed(item, res, row["country"])
    async with conn.transaction():
        await store.lock_destinations(conn)
        cur = await _still_due(conn, row)
        if cur is None:            # fixed by an admin / merged / re-located meanwhile: nothing to do, no stamp
            return {"outcome": "skipped"}
        if hit is not None:
            if guards.may_overwrite(cur["located_by"], cur["geo_status"], hit.located_by):
                await store._write_point(conn, row["id"], hit, False)
            fixed = hit.country != row["country"] and await _fix_country(conn, row, hit.country)
            km = round(guards.km(lat, lng, hit.lat, hit.lng), 1)
            await conn.execute(_STAMP_SQL, row["id"], _stamp("confirmed", moved_km=km, prev=_prev(row)))
            return {"outcome": "confirmed", "moved_km": km, "country_fixed": bool(fixed)}
        outcome = locate.REVIEW_OFF_ROUTE if c["on_route"] is False else locate.REVIEW_LLM_POINT
        # an off-route flag wins; otherwise a reason the row already carries (unconfirmed_hard_type …) is kept
        reason = _keep_linear(row, outcome if outcome == locate.REVIEW_OFF_ROUTE
                              else (row.get("review_reason") or outcome))
        await conn.execute(_FLAG_SQL, row["id"], reason, hide_unconfirmed)
        if hide_unconfirmed:
            await conn.execute(_HIDE_NAMES_SQL, row["id"])
        fixed = bool(c["hk_mo"]) and await _fix_country(conn, row, c["hk_mo"])
        meta = (res.meta if res is not None else {}) or {}
        await conn.execute(_STAMP_SQL, row["id"], _stamp(outcome, route_km=c["route_km"],
                                                         geocoder=meta.get("outcome")))
    return {"outcome": outcome, "country_fixed": bool(fixed)}


async def apply(pool, geocoder, *, located_by: list[str], country: Optional[str], limit: int,
                hide_unconfirmed: bool = False, progress=None, recheck: Optional[list[str]] = None,
                strategy: str = "v1", max_requests: Optional[int] = None) -> dict:
    """Up to `limit` rows. Result counts per outcome; `stopped_credit_limit` / `stopped_geocoder_unavailable` when the
    geocoder stopped the run, `stopped_request_cap` when this run's requests reached `max_requests` (checked before
    each row, so a run may end up to V2_MAX_QUERIES - 1 requests over); `remaining` = rows still to do."""
    from services.places.geocoder import CreditLimitReached, GeocoderUnavailable
    from shared.cost_guard import BudgetExceeded

    async with pool.acquire() as conn:
        rows = await candidates(conn, located_by=located_by, country=country, limit=limit, recheck=recheck)
        routes = await routes_by_destination(conn, rows)
        towns = await towns_by_destination(conn, rows) if strategy == "v2" else {}
    out: dict = {"mode": "apply", "rule_version": RULE_VERSION, "located_by": located_by, "country": country,
                 "recheck": recheck_list(recheck), "strategy": strategy, "hide_unconfirmed": hide_unconfirmed,
                 "rows": len(rows), "done": 0, "errors": 0, **{o: 0 for o in OUTCOMES}, "moved_km_max": 0.0,
                 "samples": []}
    start_requests = getattr(geocoder, "requests", 0) or 0
    for n, row in enumerate(rows):
        if max_requests is not None and (getattr(geocoder, "requests", 0) or 0) - start_requests >= max_requests:
            out["stopped_request_cap"] = True
            break
        ctx = context_town(row["name"], towns.get(row["id"], []), row["lat"], row["lng"]) if towns else None
        try:
            async with pool.acquire() as conn:
                r = await apply_row(conn, row, routes.get(row["id"], ()), geocoder,
                                    hide_unconfirmed=hide_unconfirmed, strategy=strategy, context=ctx)
        except CreditLimitReached as e:
            out["stopped_credit_limit"] = True
            logger.warning("place_relocate_credit_limit", error=str(e)[:200])
            break
        except GeocoderUnavailable as e:
            out["stopped_geocoder_unavailable"] = True
            logger.error("place_relocate_geocoder_unavailable", error=str(e)[:200])
            break
        except BudgetExceeded:     # the run budget stops the run (the job alerts), not one row
            raise
        except Exception as e:  # noqa: BLE001 — one bad row must not stop the run; it is taken again next run
            out["errors"] += 1
            logger.warning("place_relocate_row_failed", destination_id=row["id"], error=str(e)[:200])
            continue
        out["done"] += 1
        out[r["outcome"]] += 1
        out["country_fixed"] += 1 if r.get("country_fixed") else 0
        out["moved_km_max"] = max(out["moved_km_max"], r.get("moved_km") or 0.0)
        notable = r["outcome"] not in ("confirmed", "kept") or (r.get("moved_km") or 0) > geomatch.ACCEPT_KM
        if notable and len(out["samples"]) < SAMPLE:
            out["samples"].append({"id": row["id"], "name": row["name"], "country": row["country"], **r})
        if progress is not None and n % 20 == 19:
            await progress(step="relocate", done=n + 1, total=len(rows))
    async with pool.acquire() as conn:
        out["remaining"] = await count_candidates(conn, located_by=located_by, country=country, recheck=recheck)
    return out


# ── hide / unhide: the readers' "hide unconfirmed" policy (no call) ─────────────────────────────────────────────────

def hide_reasons(value) -> list[str]:
    """The reasons a hide run takes: the asked ones among HIDE_REASONS (+ the opt-in HIDE_NON_PLACE); default all of
    HIDE_REASONS. A non-place-only request is honoured (it is an explicit choice)."""
    allowed = HIDE_REASONS + (HIDE_NON_PLACE,)
    vals = [v for v in (value or HIDE_REASONS) if v in allowed]
    return sorted(set(vals)) or list(HIDE_REASONS)


async def hide(pool, *, reasons: list[str], country: Optional[str], confirm: bool) -> dict:
    """Rows still flagged with one of `reasons` (geo_status 'ok'): counted by reason / country; with `confirm` each is
    set `suspect` (and its names), stamped `geocoder_meta.hidden = {by, reason, at}` for unhide. No call."""
    async with pool.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(_HIDE_CANDIDATES_SQL, [x for x in reasons if x != HIDE_NON_PLACE],
                                                  country, HIDE_NON_PLACE in reasons, HIDE_NON_PLACE)]
    by_reason: dict[str, int] = {}
    by_country: dict[str, int] = {}
    for r in rows:
        by_reason[r["review_reason"]] = by_reason.get(r["review_reason"], 0) + 1
        by_country[r["country"]] = by_country.get(r["country"], 0) + 1
    out = {"mode": "hide", "rule_version": RULE_VERSION, "reasons": reasons, "country": country, "confirm": confirm,
           "rows": len(rows), "by_reason": by_reason,
           "by_country": dict(sorted(by_country.items(), key=lambda kv: -kv[1])), "hidden": 0,
           "samples": [{"id": r["id"], "name": r["name"], "country": r["country"], "reason": r["review_reason"]}
                       for r in rows[:SAMPLE]]}
    if not confirm:
        return out
    for r in rows:
        stamp = json.dumps({"by": HIDE_STAMP, "reason": r["review_reason"],
                            "at": datetime.now(timezone.utc).isoformat()})
        async with pool.acquire() as conn:
            async with conn.transaction():
                await store.lock_destinations(conn)
                status = await conn.execute(_HIDE_ROW_SQL, r["id"], stamp)
                if status.endswith(" 1"):
                    await conn.execute(_HIDE_NAMES_SQL, r["id"])
                    out["hidden"] += 1
    return out


async def unhide(pool, *, country: Optional[str]) -> dict:
    """Revert every row `hide` marked (and still suspect): geo_status 'ok', its suspect names 'resolved'. No call."""
    async with pool.acquire() as conn:
        ids = [r["id"] for r in await conn.fetch(_UNHIDE_ROWS_SQL, HIDE_STAMP, country)]
    n = 0
    for dest in ids:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await store.lock_destinations(conn)
                status = await conn.execute(_UNHIDE_ROW_SQL, dest)
                if status.endswith(" 1"):
                    await conn.execute(_UNHIDE_NAMES_SQL, dest)
                    n += 1
    return {"mode": "unhide", "rule_version": RULE_VERSION, "country": country, "rows": len(ids), "unhidden": n}
