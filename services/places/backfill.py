"""AA-712 — admin backfill of the place pipeline: pick ≤ 50 live tours (one country or all), estimate
the paid calls with no call at all, then enqueue one place_extract per tour (design §3, §7).

The estimate is computed from the same pure functions the job runs:
  - day calls: tours whose days_hash changed AND whose rules miss a non-last overnight (needs_model);
  - new names: places + rule anchors whose (name, country, locality) key is neither a resolved
    geo.place_name nor a reusable shared.destinations row (model-found anchors are not counted);
  - locate calls = ceil(new names / 40); geocoder requests ≈ new names (external provider only);
  - embed calls ≈ one per tour with places (≤ 96 texts per call).
Prices (AA-847, S247): the design §8 token envelope per call (days ≈ 3.2k in / ≤ 1.2k out, locate ≈ 1k in /
≤ 0.8k out) priced at the CURRENT route of each stage — shared.llm_role_config model (+ its shadow × sample pct)
joined to shared.llm_model_catalog prices, read on the same connection. A stage or price the DB does not give falls
back to SAFE_DEFAULTS and pricing.COST_TABLE, and a model known to neither is priced at Sonnet rates (an estimate
must not come out low). Before S247 the figures were hard-coded Sonnet prices (days $0.028 + a Luna shadow, locate
$0.015), so a Haiku route was quoted ~3x too high. Embed ≈ $0.0005 per tour; geocoder at its configured price per
request.

Tours: one country (or all) up to MAX_TOURS, or (AA-847) an explicit list of ≤ MAX_TOUR_IDS tour ids — live tour id
or source id — e.g. the tours the coverage gate lists as missing. Ids that are not live are returned, not run.

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
MAX_TOUR_IDS = 100
WAVE_KINDS = ("s1_rewrite", "a3_atomize", "recompute")
# design §8 token envelope per call (input, output); the price per call comes from the stage's live route.
TOKENS = {"days": (3200, 1200), "locate": (1000, 800)}
STAGES = {"days": "a3_place_days", "locate": "a3_place_locate"}
EMBED_PER_TOUR_USD = 0.0005
_SONNET_RATES = (3.0, 15.0)          # $/MTok: the pricing.py rule for a model with no known price
GEOCODER_BACKFILL_CAP_USD = 50.0   # = spend_budget geocoder/job:place_geocode per run (migration 226)

ROUTE_PRICE_SQL = """
    SELECT rc.stage, rc.model_id, rc.shadow_model_id, rc.shadow_sample_pct,
           m.price_in_per_mtok::float8 AS in_mtok, m.price_out_per_mtok::float8 AS out_mtok,
           s.price_in_per_mtok::float8 AS shadow_in_mtok, s.price_out_per_mtok::float8 AS shadow_out_mtok
      FROM shared.llm_role_config rc
      LEFT JOIN shared.llm_model_catalog m ON m.model_key = rc.model_id
      LEFT JOIN shared.llm_model_catalog s ON s.model_key = rc.shadow_model_id
     WHERE rc.stage = ANY($1::text[]) AND rc.is_active
"""


def _fallback_rates(model: Optional[str]) -> tuple[float, float]:
    """$/MTok from pricing.COST_TABLE for a legacy key, else Sonnet rates (never quote low)."""
    from shared.llm_client import pricing
    base = pricing.COST_TABLE.get(pricing._SHORT_KEY_TO_MODEL_ID.get(model or "", model or ""))
    return (base["in"] * 1000, base["out"] * 1000) if base else _SONNET_RATES


def _per_call(tokens: tuple[int, int], in_mtok: Optional[float], out_mtok: Optional[float],
              model: Optional[str]) -> float:
    if in_mtok is None or out_mtok is None:
        in_mtok, out_mtok = _fallback_rates(model)
    return (tokens[0] * in_mtok + tokens[1] * out_mtok) / 1_000_000


def route_prices(rows: list[dict]) -> dict:
    """Pure: ROUTE_PRICE_SQL rows -> {"days", "days_shadow", "locate", "embed"} $ per call, plus the models used."""
    from shared.llm_client.role_config import SAFE_DEFAULTS
    by_stage = {r["stage"]: r for r in rows}
    out: dict = {"embed": EMBED_PER_TOUR_USD, "models": {}}
    for part, stage in STAGES.items():
        r = by_stage.get(stage)
        if r is None:
            d = SAFE_DEFAULTS[stage]
            r = {"model_id": d.model_id, "shadow_model_id": d.shadow_model_id, "shadow_sample_pct": d.shadow_sample_pct}
        out[part] = _per_call(TOKENS[part], r.get("in_mtok"), r.get("out_mtok"), r["model_id"])
        model_info = {"model": r["model_id"], "per_call_usd": round(out[part], 5)}
        if part == "days":
            pct = (r.get("shadow_sample_pct") or 0) if r.get("shadow_model_id") else 0
            out["days_shadow"] = (pct / 100) * (_per_call(TOKENS[part], r.get("shadow_in_mtok"),
                                                          r.get("shadow_out_mtok"), r.get("shadow_model_id"))
                                                if pct else 0.0)
            if pct:
                model_info["shadow"] = {"model": r["shadow_model_id"], "sample_pct": pct}
        out["models"][stage] = model_info
    return out


async def load_route_prices(conn) -> dict:
    """The live route + catalog prices of the two model stages (one read on the caller's connection); on a read
    failure the SAFE_DEFAULTS / COST_TABLE prices, so a dry run never fails on pricing."""
    try:
        rows = [dict(r) for r in await conn.fetch(ROUTE_PRICE_SQL, list(STAGES.values()))]
    except Exception:
        rows = []
    return route_prices(rows)


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


def is_stale(tour: dict, build: Optional[dict], components_hash: Optional[str] = None) -> bool:
    """Stale = no ok build of this version, another rule version, other day text or (AA-989, when the caller read
    the atoms) other components: an atom-only change (re-upload, action-only re-atomize) is stale too."""
    if build is None or build.get("status") != "ok" or str(build.get("tour_id")) != tour["tour_id"]:
        return True
    if build.get("rule_version") != days_mod.RULE_VERSION:
        return True
    if build.get("days_hash") != days_mod.days_hash(tour.get("aa_itineraries") or ""):
        return True
    return components_hash is not None and build.get("components_hash") != components_hash


async def _is_stale_read(conn, tour: dict, build: Optional[dict]) -> bool:
    """is_stale(), reading the tour's atoms only when the cheap checks say fresh."""
    if is_stale(tour, build):
        return True
    return is_stale(tour, build, comp_mod.components_hash(await store.tour_atoms(conn, tour["tour_id"])))


def normalize_tour_ids(ids: list[str]) -> list[str]:
    """Lower-cased, de-duplicated (first wins), each a uuid; ValueError otherwise."""
    import uuid as _uuid
    out: list[str] = []
    for raw in ids:
        v = str(_uuid.UUID(str(raw).strip()))
        if v not in out:
            out.append(v)
    return out


async def plan_ids(conn, tour_ids: list[str], *, only_stale: bool) -> tuple[list[dict], dict]:
    """AA-847: the live tours named by `tour_ids` (live tour id or source id), one per source, in request order.
    Returns (picked, skipped) with skipped = {"not_live": [...], "fresh": [...]} (fresh only when only_stale)."""
    ids = normalize_tour_ids(tour_ids)[:MAX_TOUR_IDS]
    tours = await store.live_tours_by_ids(conn, ids)
    by_tour = {t["tour_id"]: t for t in tours}
    by_source: dict = {}
    for t in sorted(tours, key=lambda x: x["tour_id"]):
        by_source.setdefault(t["source_id"], t)
    builds = {r["source_id"]: dict(r) for r in await conn.fetch(
        "SELECT source_id, tour_id::text AS tour_id, status, rule_version, days_hash, components_hash "
        "FROM geo.tour_build "
        "WHERE source_id = ANY($1::text[])", [t["source_id"] for t in tours])} if tours else {}
    picked, seen, not_live, fresh = [], set(), [], []
    for i in ids:
        t = by_tour.get(i) or by_source.get(i)
        if t is None:
            not_live.append(i)
            continue
        if t["source_id"] in seen:
            continue
        seen.add(t["source_id"])
        b = builds.get(t["source_id"])
        if only_stale and not await _is_stale_read(conn, t, b):
            fresh.append(i)
            continue
        picked.append({**t, "build": b})
    return picked, {"not_live": not_live, "fresh": fresh}


async def plan(conn, *, country: Optional[str], limit: int, only_stale: bool) -> list[dict]:
    limit = max(1, min(int(limit), MAX_TOURS))
    tours = sorted(await store.live_tours(conn, country), key=lambda t: t["source_id"])
    builds = {r["source_id"]: dict(r) for r in await conn.fetch(
        "SELECT source_id, tour_id::text AS tour_id, status, rule_version, days_hash, components_hash "
        "FROM geo.tour_build "
        "WHERE source_id = ANY($1::text[])", [t["source_id"] for t in tours])}
    picked = []
    for t in tours:
        b = builds.get(t["source_id"])
        if only_stale and not await _is_stale_read(conn, t, b):
            continue
        picked.append({**t, "build": b})
        if len(picked) >= limit:
            break
    return picked


async def estimate(conn, tours: list[dict], geocoder_cost_per_request: float, geocoder_external: bool) -> dict:
    from services.places.pipeline import canonical

    price = await load_route_prices(conn)

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
    llm_cost = (day_calls * (price["days"] + price["days_shadow"]) + locate_calls * price["locate"]
                + tours_with_places * price["embed"])
    geo_cost = geo_requests * geocoder_cost_per_request
    return {"tours": len(tours), "day_calls": day_calls, "new_names": new_names,
            "names_already_pending": len(keys & pending_keys), "locate_calls": locate_calls,
            "embed_calls": tours_with_places, "geocoder_requests": geo_requests,
            "llm_cost_usd": round(llm_cost, 4), "geocoder_cost_usd": round(geo_cost, 4),
            "expected_cost_usd": round(llm_cost + geo_cost, 4),
            "llm_cost_per_tour_usd": round(llm_cost / len(tours), 4) if tours else 0.0,
            "prices": price["models"]}
