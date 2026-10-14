"""AA-712 — admin backfill of the place pipeline: pick ≤ 50 live tours (one country or all), estimate
the paid calls with no call at all, then enqueue one place_extract per tour (design §3, §7).

The estimate is computed from the same pure functions the job runs:
  - day calls: tours whose days_hash changed AND whose rules miss a non-last overnight (needs_model);
  - new names: places + rule anchors whose (name, country, locality) key is neither a resolved
    geo.place_name nor a reusable shared.destinations row (model-found anchors are not counted);
  - locate calls = ceil(new names / 40); geocoder requests ≈ new names (external provider only);
  - embed calls ≈ one per tour with places (≤ 96 texts per call).
Prices: design §8 per-call figures (Sonnet days ≤ $0.028, Luna shadow 20% × $0.0018, locate ≤ $0.015,
embed ≈ $0.0005), geocoder at its configured price per request.

Refused (by the router) while a wave runs: any s1_rewrite / a3_atomize / recompute job queued or
running, or a previous backfill batch still has queued place_extract jobs.
"""
from __future__ import annotations

import math
from typing import Optional

from services.places import components as comp_mod
from services.places import days as days_mod
from services.places import store
from services.places.names import country_norm, locality_key, name_norm

MAX_TOURS = 50
WAVE_KINDS = ("s1_rewrite", "a3_atomize", "recompute")
PRICE = {"days": 0.028, "days_shadow": 0.2 * 0.0018, "locate": 0.015, "embed": 0.0005}
GEOCODER_BACKFILL_CAP_USD = 50.0   # = spend_budget geocoder/job:place_geocode per run (migration 226)


async def wave_busy(conn) -> dict:
    """{kind: n} of wave kinds queued/running, plus queued backfill place_extract jobs."""
    rows = await conn.fetch(
        "SELECT kind, count(*)::int AS n FROM shared.job WHERE kind = ANY($1::text[]) "
        "AND status IN ('queued', 'running') GROUP BY kind", list(WAVE_KINDS))
    out = {r["kind"]: r["n"] for r in rows}
    n = await conn.fetchval(
        "SELECT count(*)::int FROM shared.job WHERE kind = 'place_extract' AND status = 'queued' "
        "AND created_by LIKE 'backfill:%'")
    if n:
        out["place_extract_backfill_queued"] = n
    return out


def is_stale(tour: dict, build: Optional[dict]) -> bool:
    if build is None or build.get("status") != "ok" or str(build.get("tour_id")) != tour["tour_id"]:
        return True
    if build.get("rule_version") != days_mod.RULE_VERSION:
        return True
    return build.get("days_hash") != days_mod.days_hash(tour.get("aa_itineraries") or "")


async def plan(conn, *, country: Optional[str], limit: int, only_stale: bool) -> list[dict]:
    limit = max(1, min(int(limit), MAX_TOURS))
    tours = sorted(await store.live_tours(conn, country), key=lambda t: t["source_id"])
    builds = {r["source_id"]: dict(r) for r in await conn.fetch(
        "SELECT source_id, tour_id::text AS tour_id, status, rule_version, days_hash FROM geo.tour_build "
        "WHERE source_id = ANY($1::text[])", [t["source_id"] for t in tours])}
    picked = []
    for t in tours:
        b = builds.get(t["source_id"])
        if only_stale and not is_stale(t, b):
            continue
        picked.append({**t, "build": b})
        if len(picked) >= limit:
            break
    return picked


async def estimate(conn, tours: list[dict], geocoder_cost_per_request: float, geocoder_external: bool) -> dict:
    from services.places.pipeline import canonical

    day_calls, keys, tours_with_places = 0, set(), 0
    for t in tours:
        text = t.get("aa_itineraries") or ""
        b = t.get("build")
        if (b is None or b.get("days_hash") != days_mod.days_hash(text)
                or b.get("rule_version") != days_mod.RULE_VERSION):
            blocks = days_mod.parse_days(text)
            if blocks and days_mod.needs_model(blocks, [days_mod.rule_day(x) for x in blocks]):
                day_calls += 1
        c = canonical(t.get("country"))
        anchors = {}
        for bl in days_mod.parse_days(text):
            s, e, o = days_mod.rule_day(bl)
            a = days_mod.anchor_place(o, e)
            for d in range(bl.day_from, bl.day_to + 1):
                if a:
                    anchors[d] = a
        for a in anchors.values():
            loc = locality_key(a, None)
            if loc is not None and name_norm(a):
                keys.add((name_norm(a), country_norm(c), loc))
        atoms = await store.tour_atoms(conn, t["tour_id"])
        comps, _ = comp_mod.build_components(t["source_id"], c, atoms, anchors)
        if comps:
            tours_with_places += 1
        for x in comps:
            if x.locality_norm is not None and x.name_norm:
                keys.add((x.name_norm, country_norm(canonical(x.country)), x.locality_norm))
    resolved = await store.resolved_names(conn, keys)
    known = await store.known_destinations(conn, keys - set(resolved))
    pending = await conn.fetch(
        "SELECT name_norm, country_norm, locality_norm FROM geo.place_name WHERE status = 'pending'")
    pending_keys = {(r["name_norm"], r["country_norm"], r["locality_norm"]) for r in pending}
    new_names = len(keys - set(resolved) - set(known) - pending_keys)
    locate_calls = math.ceil(new_names / 40) if new_names else 0
    geo_requests = new_names if geocoder_external else 0
    llm_cost = (day_calls * (PRICE["days"] + PRICE["days_shadow"]) + locate_calls * PRICE["locate"]
                + tours_with_places * PRICE["embed"])
    geo_cost = geo_requests * geocoder_cost_per_request
    return {"tours": len(tours), "day_calls": day_calls, "new_names": new_names,
            "names_already_pending": len(keys & pending_keys), "locate_calls": locate_calls,
            "embed_calls": tours_with_places, "geocoder_requests": geo_requests,
            "llm_cost_usd": round(llm_cost, 4), "geocoder_cost_usd": round(geo_cost, 4),
            "expected_cost_usd": round(llm_cost + geo_cost, 4),
            "llm_cost_per_tour_usd": round(llm_cost / len(tours), 4) if tours else 0.0}
