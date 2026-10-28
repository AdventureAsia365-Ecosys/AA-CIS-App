"""AA-712 — what the place jobs do, with the I/O passed in (tests use fakes for every paid call).

extract_tour_places(): one live tour → geo.tour_day + geo.tour_place of its source (design §3
place_extract: days → components → link → embed → persist).
geocode_pending(): pending names → coordinates → shared.destinations → relink → outlier guard
(design §3 place_geocode: locate → confirm → guard → upsert → relink).
build_tour_graph(): live sources → one graph build, swapped in (design §3 tour_graph_build).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

import structlog

from services.places import components as comp_mod
from services.places import days as days_mod
from services.places import graph as graph_mod
from services.places import guards, locate, store
from services.places.names import country_norm, locality_key, name_norm
from shared import countries as _countries

logger = structlog.get_logger()

UNRESOLVABLE_LOCALITY = comp_mod.UNRESOLVABLE_LOCALITY   # "?": a generic name with no locality, never matches a key


class _NotLive(Exception):
    """Raised inside the persist transaction to roll it back when the version is no longer live."""


def canonical(country: Optional[str], tour_country: Optional[str] = None) -> str:
    """The canonical ISO country name (shared/countries) a place / component is keyed on. AA-990: one rule with new
    place cards (shared.countries.one_country, as place_cards.normalise_card_country): a known name or alias ->
    its canonical name ('tanzania' -> 'Tanzania', cross-border kept); a free-text compound ("Nepal and China", a
    border peak) -> the tour's country when it is literally one of the named countries, else the FIRST named one —
    never the tour's country when the text does not name it. So the name key reuses the destination other tours
    resolved instead of a dirty "nepal and china" key; the located destination still carries the place's real
    country (locate accepts any ISO country). Text that names no country ('Himalayas') is kept as written."""
    one = _countries.one_country(country, tour_country)
    return one.name if one else (country or "").strip()


def tour_day_count(duration_raw: Optional[str], itinerary: Optional[str], highest_day: int = 0) -> int:
    """The tour's day count as TripPlanner's S236 route rule reads it (browse/routes.py TOUR_DAYS_COUNT_SQL:
    GREATEST(v_tour.duration_days, its day rows)): the duration text's number, else the highest canonical day
    (validity.duration_days = the v_tour rule), never under `highest_day` (the day records' last day). AA-990 N1
    (S255): the leg rule's denominator must be TripPlanner's, or a "10 days" tour whose itinerary text stops at day 6
    would pin legs that still give it no route."""
    from services.places import validity
    return max(highest_day or 0, validity.duration_days(duration_raw, itinerary) or 0)


@dataclass
class ExtractDeps:
    generate_days: Optional[Callable[[str, str], Awaitable[str]]] = None
    embed: Optional[Callable[[list[str]], Awaitable[list[list[float]]]]] = None
    spent: dict = field(default_factory=dict)


def _day_from_row(r: dict) -> days_mod.DayRecord:
    return days_mod.DayRecord(r["day_index"], r.get("title") or "", r.get("start_place"), r.get("end_place"),
                              r.get("overnight_place"), list(r.get("places") or []), r.get("extracted_by") or "rule",
                              bool(r.get("is_transit_day")))


def _anchor_key(anchor: Optional[str], country: str) -> Optional[tuple]:
    if not anchor:
        return None
    loc = locality_key(anchor, None)
    if loc is None:
        return None
    n = name_norm(anchor)
    return (n, country_norm(country), loc) if n else None


async def extract_tour_places(pool, tour: dict, deps: ExtractDeps, *, force: bool = False,
                              job_id: Optional[str] = None) -> dict:
    """`tour` = store.live_tour() row. Returns the job result (status skipped_unchanged |
    rebased | skipped_not_live | ok | partial)."""
    tour_id, source_id = tour["tour_id"], tour["source_id"]
    country = canonical(tour.get("country"))
    text = tour.get("aa_itineraries") or ""
    async with pool.acquire() as conn:
        build = await store.get_build(conn, source_id)
        atoms = await store.tour_atoms(conn, tour_id)
        with_vectors = await store.has_embedding_column(conn)
    prev = (build or {}).get("counts") or {}
    dh, ch = days_mod.days_hash(text), comp_mod.components_hash(atoms)
    rule_ok = build is not None and build.get("rule_version") == days_mod.RULE_VERSION
    same_days = bool(rule_ok and not force and build.get("days_hash") == dh and not prev.get("days_model_failed"))
    same_comps = bool(same_days and build.get("components_hash") == ch and not prev.get("embed_failed"))
    base = {"tour_id": tour_id, "source_id": source_id, "country": country}

    if same_days and same_comps and build.get("status") == "ok":
        if str(build["tour_id"]) == tour_id:
            return {**base, "status": "skipped_unchanged"}
        async with pool.acquire() as conn:
            try:
                async with conn.transaction():
                    await store.lock_build(conn, source_id, tour_id)
                    if await store.live_tour_for_source(conn, source_id) != tour_id:
                        raise _NotLive()
                    await store.set_build_tour(conn, source_id, tour_id)
            except _NotLive:
                return {**base, "status": "skipped_not_live"}
        return {**base, "status": "rebased", "from_tour_id": str(build["tour_id"])}

    # ── days ──
    counts: dict = {"atoms": len(atoms)}
    if same_days:
        async with pool.acquire() as conn:
            records = [_day_from_row(r) for r in await store.existing_days(conn, source_id)]
        counts["days_reused"] = True
    else:
        res = await days_mod.extract_tour({"tour_id": tour_id, "aa_name": tour.get("aa_name"),
                                           "aa_itineraries": text}, deps.generate_days)
        records = res.records
        counts.update(days_model_called=res.model_called, days_model_failed=res.model_failed,
                      rule_missed_nights=res.rule_missed_nights)
        stay = days_mod.stay_atom_overnights(atoms)
        counts["stay_lever_fills"] = days_mod.stay_lever_fills(records, days_mod.rule_overnights(text), stay)
    anchors = {r.day_index: days_mod.anchor_place(r.overnight_place, r.end_place) for r in records}
    day_towns = {d: a for d, a in anchors.items() if a}

    # ── components ──
    # AA-990 N1 (S255): the tour-level leg rule measures coverage against the tour's day count
    tour_days = tour_day_count(tour.get("duration"), text, max((r.day_index for r in records), default=0))
    comps, c_counts = comp_mod.build_components(source_id, country, atoms, day_towns, tour_days=tour_days)
    for c in comps:
        c.country = canonical(c.country, country)
        c.country_norm = country_norm(c.country)
    counts.update(components=c_counts["components"], skipped=c_counts["skipped"],
                  generic_without_locality=c_counts["generic_without_locality"],
                  linked_to_feature=c_counts.get("linked_to_feature", 0),
                  transit_journeys=c_counts.get("transit_journeys", 0),
                  transit_route_legs=c_counts.get("transit_route_legs", 0),
                  route_leg_days=c_counts.get("route_leg_days", 0))

    # ── link ── card destination → resolved name → reusable destination → pending
    comp_keys = {c.component_id: (c.name_norm, c.country_norm, c.locality_norm) for c in comps
                 if c.locality_norm is not None and c.name_norm}
    anchor_keys = {d: _anchor_key(a, country) for d, a in anchors.items()}
    all_keys = set(comp_keys.values()) | {k for k in anchor_keys.values() if k}
    card_dest = {c.component_id: a["card_destination_id"] for c in comps for a in atoms
                 if c.place_card_id and str(a.get("card_id")) == c.place_card_id and a.get("card_destination_id")}
    async with pool.acquire() as conn:
        resolved = await store.resolved_names(conn, all_keys)
        known = await store.known_destinations(conn, all_keys - set(resolved))
        dest_by_key = {**known, **resolved}
        new_rows = []
        for c in comps:
            key = comp_keys.get(c.component_id)
            if key is None or key in resolved:
                continue
            dest = known.get(key) or card_dest.get(c.component_id)
            new_rows.append((key[0], key[1], key[2], c.link_name, c.country, c.locality, tour.get("aa_name"),
                             dest, c.place_card_id, "resolved" if dest else "pending"))
        for d, key in anchor_keys.items():
            if key and key not in resolved:
                dest = known.get(key)
                new_rows.append((key[0], key[1], key[2], anchors[d], country, None, tour.get("aa_name"), dest,
                                 None, "resolved" if dest else "pending"))
    name_rows = list({r[:3]: r for r in new_rows}.values())
    comp_dest = {cid: card_dest.get(cid) or dest_by_key.get(k) for cid, k in comp_keys.items()}
    night_dest = {d: dest_by_key.get(k) if k else None for d, k in anchor_keys.items()}
    # AA-847 in-tour dedupe: two destinations of this tour that the same-place rules merge (dedupe.decide auto_merge,
    # e.g. "Kuang Si Falls" and "Tat Kuang Si" 300 m apart) → every component and night of this tour uses the
    # cluster's keeper. Component ids are unchanged (they come from the itinerary spelling); the other tours and the
    # destinations are untouched (the catalog-wide merge is the place_dedupe job, logged in geo.place_merge).
    async with pool.acquire() as conn:
        remap = await _in_tour_keepers(conn, set(comp_dest.values()) | set(night_dest.values()), comps, comp_dest)
    if remap:
        comp_dest = {cid: remap.get(d, d) if d else d for cid, d in comp_dest.items()}
        night_dest = {day: remap.get(d, d) if d else d for day, d in night_dest.items()}
    counts["in_tour_duplicates_merged"] = len(remap)
    # AA-989: TripPlanner pins components by id — a rebuild keeps the previous row's id for the same place (respelt,
    # retyped or moved a day), so a saved trip does not lose its pins (components.carry_over_ids).
    async with pool.acquire() as conn:
        previous = await store.existing_places(conn, source_id)
    carry, carry_counts = comp_mod.carry_over_ids(comps, comp_dest, previous)
    comp_dest = comp_mod.apply_carry_over(comps, comp_dest, carry)
    counts.update(carry_counts)
    # AA-990: every previous id the carry-over could not keep → a redirect to the component that took its place,
    # written to geo.component_redirect (CIS 285) with the place rows below; TripPlanner follows it.
    redirects, redirect_counts, redirect_rules = comp_mod.redirect_dropped(comps, comp_dest, previous)
    counts.update(redirect_counts)
    counts["places_linked"] = sum(1 for v in comp_dest.values() if v)
    counts["nights_linked"] = sum(1 for v in night_dest.values() if v)
    counts["names_new_pending"] = sum(1 for r in new_rows if r[-1] == "pending")

    # ── embed ── reuse by text_hash; failure → no vectors, status partial (the next run retries)
    vectors: Optional[list[Optional[str]]] = None
    if with_vectors:
        from services.places.llm import vector_literal
        async with pool.acquire() as conn:
            reuse = await store.existing_vectors(conn, source_id)
        vectors = [reuse.get(c.text_hash) for c in comps]
        missing = [i for i, v in enumerate(vectors) if v is None]
        counts["embeddings_reused"] = len(comps) - len(missing)
        if missing and deps.embed is not None:
            try:
                fresh = await deps.embed([comps[i].text_extract for i in missing])
                for i, vec in zip(missing, fresh):
                    vectors[i] = vector_literal(vec)
                counts["embeddings_new"] = len(missing)
            except Exception as e:  # noqa: BLE001 — places still land; the next run embeds
                from shared.cost_guard import BudgetExceeded
                if isinstance(e, BudgetExceeded):
                    raise
                counts["embed_failed"] = True
                logger.warning("place_embed_failed", tour_id=tour_id, error=str(e)[:200])
        elif missing:
            counts["embed_failed"] = True

    # ── persist ── under the build row lock, after re-checking this version is still the live one
    day_rows = []
    for r in records:
        a = anchors.get(r.day_index)
        key = anchor_keys.get(r.day_index)
        day_rows.append((source_id, r.day_index, tour_id, r.title, r.start_place, r.end_place, r.overnight_place,
                         a, key[0] if key else None, key[1] if key else None, key[2] if key else "",
                         night_dest.get(r.day_index), r.places, r.extracted_by, r.is_transit_day))
    place_rows = [(c.component_id, source_id, tour_id, c.day_index, c.name, c.name_norm, c.country, c.country_norm,
                   c.locality_norm if c.locality_norm is not None else UNRESOLVABLE_LOCALITY,
                   comp_dest.get(c.component_id), c.place_card_id, c.activity, c.intensity_level, c.season_months,
                   c.duration_hint, c.text_extract, c.text_hash, c.atom_ids) for c in comps]
    partial = bool(counts.get("days_model_failed") or counts.get("embed_failed"))
    status = "partial" if partial else "ok"
    cost = round(sum(v for k, v in deps.spent.items() if k != "calls"), 6)
    async with pool.acquire() as conn:
        try:
            async with conn.transaction():
                await store.lock_build(conn, source_id, tour_id)
                if await store.live_tour_for_source(conn, source_id) != tour_id:
                    raise _NotLive()
                # AA-847: one writer of destination identity at a time (a merge never races this write; a pointer
                # read before a merge is repointed by the next place_dedupe run)
                await store.lock_destinations(conn)
                # names only once the version is known live: a rolled-back build queues no paid geocode
                await store.insert_names(conn, name_rows)
                await store.replace_source_rows(conn, source_id, tour_id, day_rows, place_rows, vectors)
                # AA-990: same transaction as the place rows — a pin never sees the id gone without its redirect
                counts["ids_redirects_cleared"] = await store.write_redirects(
                    conn, source_id, redirects, redirect_rules, [c.component_id for c in comps])
                await store.write_build(conn, source_id, tour_id=tour_id, country=country, days_hash=dh,
                                        components_hash=ch, rule_version=days_mod.RULE_VERSION, status=status,
                                        counts=counts, cost_usd=cost, job_id=job_id)
        except _NotLive:
            return {**base, "status": "skipped_not_live"}
        pending_due = await store.pending_names_due(conn)
    return {**base, "status": status, "days": len(records), "cost_usd": cost, "pending_names_due": pending_due,
            **counts}


async def _validity_counts(conn) -> dict:
    """AA-847: the content-validity counts of the live catalog, stored with each graph build (trend on
    /admin/places). Never fails the build; a failure is recorded in the counts, not swallowed silently."""
    from services.places import validity
    try:
        summary, _ = await validity.report(conn)
        return validity.stats_counts(summary)
    except Exception as e:  # noqa: BLE001
        logger.warning("place_validity_report_failed", error=str(e)[:200])
        return {"error": type(e).__name__}


async def _in_tour_keepers(conn, dest_ids: set, comps: list, comp_dest: dict) -> dict[str, str]:
    """{non-keeper destination id: keeper id} among one tour's linked destinations: the same-place decision of the
    place_dedupe job (services/places/dedupe.in_tour_remap), rules band only — the model band waits for place_dedupe."""
    from services.places import dedupe
    briefs = await store.destination_briefs(conn, {d for d in dest_ids if d})
    if len(briefs) < 2:
        return {}
    n_comp: dict[str, int] = {}
    for c in comps:
        d = comp_dest.get(c.component_id)
        if d:
            n_comp[str(d)] = n_comp.get(str(d), 0) + 1
    rows = [dedupe.Row(i, b["name"], b["country"], float(b["lat"]), float(b["lng"]), name_norm=b.get("name_norm"),
                       locality_norm=b.get("locality_norm") or "", located_by=b["located_by"],
                       geo_status=b.get("geo_status"), precision=b["geo_precision"], result_type=b["result_type"],
                       geocoder_name=b.get("geocoder_name"), place_id=b.get("geocoder_place_id"),
                       components=n_comp.get(i, 0),
                       created_at=b["created_at"].isoformat() if b.get("created_at") else "")
            for i, b in briefs.items() if b.get("lat") is not None and b.get("lng") is not None]
    remap = dedupe.in_tour_remap(rows)
    if remap:
        # no audit row (the tour's own rows are rebuilt each run); the pairs are logged so a wrong merge is traceable
        names = {r.id: r.name for r in rows}
        pairs = [f"{names[m]} -> {names[k]}" for m, k in sorted(remap.items())]
        logger.info("place_in_tour_duplicates_merged", pairs=pairs[:20], n=len(pairs),
                    rule_version=dedupe.RULE_VERSION)
    return remap


# ── geocode ────────────────────────────────────────────────────────────────────────────────

def _label(row: dict) -> str:
    return f"{row['display_name']} ({row['locality']})" if row.get("locality") else row["display_name"]


async def geocode_pending(pool, generate, geocoder, *, limit: int, job_id: Optional[str] = None,
                          progress=None) -> dict:
    """Resolve up to `limit` due pending names. The only writer of shared.destinations coordinates
    and geo.place_name status."""
    from shared.cost_guard import BudgetExceeded

    async with pool.acquire() as conn:
        due = await store.due_names(conn, limit)
    out = {"names": len(due), "resolved": 0, "inserted": 0, "updated": 0, "kept": 0, "failed": 0,
           "unknown": 0, "deferred": 0, "review": 0, "shapes_pending": 0, "by": {}, "geocoder": geocoder.name}
    if not due:
        return out
    keys = [(r["name_norm"], r["country_norm"], r["locality_norm"]) for r in due]
    # names another job / card already answers: no call
    async with pool.acquire() as conn:
        known = await store.known_destinations(conn, keys)
        card_rows = await conn.fetch(
            "SELECT card_id::text AS card_id, destination_id::text AS d FROM acp_shared.place_card "
            "WHERE card_id = ANY($1::uuid[]) AND destination_id IS NOT NULL",
            [r["place_card_id"] for r in due if r.get("place_card_id")])
        card_dest = {r["card_id"]: r["d"] for r in card_rows}
        for r, k in zip(due, keys):
            dest = known.get(k) or card_dest.get(r.get("place_card_id") or "")
            if dest:
                await store.mark_name(conn, k, destination_id=dest)
                out["resolved"] += 1
    todo = [locate.Item(k, r["display_name"], r["country"], r.get("tour_name") or "", r.get("locality"))
            for r, k in zip(due, keys) if not (known.get(k) or card_dest.get(r.get("place_card_id") or ""))]
    touched: list[str] = []
    try:
        for start in range(0, len(todo), locate.BATCH):
            chunk = todo[start:start + locate.BATCH]
            credits_left = getattr(geocoder, "credits_left", None)
            left = credits_left() if credits_left is not None else None
            if left is not None and left <= 0:   # no model call for names we cannot check
                out["deferred"] += len(todo) - start
                out["stopped_credit_limit"] = True
                break
            if left is not None and left < len(chunk):
                # ≥ 1 credit per name: locate only what today's credits can check, defer the rest unpaid
                out["deferred"] += len(chunk) - left
                out["stopped_credit_limit"] = True
                chunk = chunk[:left]
            outcomes = await locate.locate_many(chunk, generate, geocoder)
            async with pool.acquire() as conn:
                for it in chunk:
                    o = outcomes.get(it.key) or locate.Outcome(None, error="no outcome")
                    if o.deferred:            # credit guard / geocoder unavailable: pending, no try counted
                        out["deferred"] += 1
                        out["stopped_geocoder_unavailable" if o.reason == locate.DEFER_UNAVAILABLE
                            else "stopped_credit_limit"] = True
                        continue
                    if o.point is None:
                        st = await store.mark_name(conn, it.key, error=o.error or o.reason)
                        out["unknown" if st == "unknown" else "failed"] += 1
                        continue
                    async with conn.transaction():
                        await store.lock_destinations(conn)     # AA-847: serialised with place_dedupe merges
                        dest_id, action = await store.upsert_destination(conn, it.key, it.name, o.point,
                                                                         locality=it.locality)
                        await store.mark_name(conn, it.key, destination_id=dest_id)
                        # AA-847: a line gets its pending geo.place_shape row (job place_shape draws it); the
                        # point is never moved for shape reasons — this job stays the only coordinate writer
                        if o.point.precision == "line" and await store.ensure_pending_shape(conn, dest_id):
                            out["shapes_pending"] += 1
                    out[action] += 1
                    out["resolved"] += 1
                    out["by"][o.point.located_by] = out["by"].get(o.point.located_by, 0) + 1
                    out["review"] += 1 if o.point.review_reason else 0
                    touched.append(dest_id)
            if progress is not None:
                await progress(step="locate", done=min(start + locate.BATCH, len(todo)), total=len(todo))
            if out.get("stopped_credit_limit") or out.get("stopped_geocoder_unavailable"):
                out["deferred"] += len(todo) - start - len(todo[start:start + locate.BATCH])
                break
    except BudgetExceeded:
        out["stopped_budget"] = True
        raise
    finally:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await store.lock_destinations(conn)
                out["relinked"] = await store.relink(conn)
            out["outliers"], out["suspects"] = await apply_outlier_rule(conn, touched or None)
        out["touched"] = len(touched)
    return out


async def apply_outlier_rule(conn, destination_ids: Optional[list[str]]) -> tuple[int, list[str]]:
    """Outlier observations for the sources that use these destinations (all when None), then the
    suspect rule. Returns (observations, newly suspect destination ids)."""
    rows = await store.night_stops(conn, destination_ids)
    by_source: dict[str, list[guards.DayStop]] = {}
    for r in rows:
        by_source.setdefault(r["source_id"], []).append(
            guards.DayStop(r["day_index"], r["destination_id"], r["lat"], r["lng"], r["is_transit_day"]))
    obs = []
    for source_id, stops in by_source.items():
        for dest, day, dist in guards.outlier_observations(stops):
            obs.append((dest, source_id, day, dist))
    await store.record_outliers(conn, obs, evaluated_sources=by_source)
    await store.prune_outliers(conn, {t["source_id"] for t in await store.live_tours(conn)})
    return len(obs), await store.apply_suspects(conn)


# ── graph ──────────────────────────────────────────────────────────────────────────────────

async def build_tour_graph(pool, *, junction_km: float = graph_mod.DEFAULT_JUNCTION_KM,
                           job_id: Optional[str] = None) -> dict:
    async with pool.acquire() as conn:
        live = await store.live_tours(conn)
        if not live:  # RLS or a reset: keep the previous graph rather than replace it with nothing
            return {"status": "refused_no_live_sources"}
        source_ids = sorted({t["source_id"] for t in live})
        days, places, dests = await store.graph_inputs(conn, source_ids)
        # pure CPU (pairwise junctions over every node): off the worker's event loop (S201)
        g = await asyncio.to_thread(graph_mod.build_graph, graph_mod.stop_rows(days, places, dests), dests,
                                    places, junction_km)
        # AA-847: the stretch of each line a tour-day follows (geo.tour_segment, versioned with this build)
        line_ids = [d for d in dests if graph_mod.is_line(dests, d)]
        shapes = await store.line_shapes(conn, line_ids)
        g.segments = await asyncio.to_thread(graph_mod.line_segments, g.stops, places, days, dests, shapes)
        g.stats["segments"] = {b: sum(1 for s in g.segments if s["basis"] == b)
                               for b in ("day_anchors", "anchor_window", "feature")}
        g.stats["validity"] = await _validity_counts(conn)
        build_id = await store.swap_graph(conn, g, {"junction_km": junction_km, "live_sources": len(source_ids)},
                                          job_id)
    return {"status": "ok", "build_id": build_id, "live_sources": len(source_ids), **g.stats}
