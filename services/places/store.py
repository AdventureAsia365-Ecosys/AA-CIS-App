"""AA-712 — SQL of the place pipeline (schema geo, migration 226). Every function takes an asyncpg
connection (or pool where noted); transactions are the caller's unless stated.

Catalog reads use the catalog.v_tour liveness rule (migration 225: AA master tenant, master_status
active, not soft-deleted, raw source active and not deleted) on the base tables, inside a
transaction that pins app.tenant_id to the AA master tenant: published_tours / raw_tours are
FORCE-RLS and the runner may be neither superuser nor BYPASSRLS (lesson S236).
"""
from __future__ import annotations

import json
from typing import Iterable, Optional

MASTER_TENANT_ID = "00000000-0000-0000-0000-000000000001"

_LIVE_SQL = """
    SELECT pt.tour_id::text AS tour_id,
           COALESCE(rt.source_group_id, rt.tour_id)::text AS source_id,
           pt.aa_name, pt.aa_itineraries, rt.country,
           pt.generated_content_id::text AS version_id
      FROM gold_aa_internal.published_tours pt
      JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = pt.tour_id
     WHERE pt.tenant_id = '00000000-0000-0000-0000-000000000001'::uuid
       AND pt.master_status = 'active' AND pt.deleted_at IS NULL
       AND rt.source_status = 'active' AND rt.deleted_at IS NULL
"""


async def _catalog_fetch(conn, sql: str, *args) -> list:
    """Runs a catalog read with app.tenant_id = AA master for this transaction only."""
    async with conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", MASTER_TENANT_ID)
        return await conn.fetch(sql, *args)


async def live_tour(conn, tour_id: str) -> Optional[dict]:
    rows = await _catalog_fetch(conn, _LIVE_SQL + " AND pt.tour_id = $1::uuid", tour_id)
    return dict(rows[0]) if rows else None


async def live_tour_for_source(conn, source_id: str) -> Optional[str]:
    """The live tour_id of a source (None when the source is not live). Runs in the caller's
    transaction when there is one (persist re-check under the build row lock)."""
    sql = "SELECT tour_id FROM (" + _LIVE_SQL + ") l WHERE l.source_id = $1 LIMIT 1"
    if not conn.is_in_transaction():
        rows = await _catalog_fetch(conn, sql, source_id)
        return rows[0]["tour_id"] if rows else None
    await conn.execute("SELECT set_config('app.tenant_id', $1, true)", MASTER_TENANT_ID)
    return await conn.fetchval(sql, source_id)


async def live_tours(conn, country: Optional[str] = None) -> list[dict]:
    sql = _LIVE_SQL + (" AND lower(btrim(rt.country)) = lower(btrim($1))" if country else "")
    rows = await (_catalog_fetch(conn, sql, country) if country else _catalog_fetch(conn, sql))
    return [dict(r) for r in rows]


ATOMS_SQL = """
    SELECT a.atom_id, a.itinerary_day, btrim(a.place) AS place, a.activity_type, a.text,
           pc.card_id::text AS card_id, pc.display_name AS card_display_name, pc.country AS card_country,
           pc.locality AS card_locality, pc.destination_id::text AS card_destination_id
      FROM acp_contract.v_active_tour_atoms a
      LEFT JOIN acp_shared.atom_place_card apc ON apc.atom_id = a.atom_id
      LEFT JOIN acp_shared.place_card pc ON pc.card_id = apc.card_id AND pc.status <> 'rejected'
     WHERE a.tour_id = $1::uuid AND a.place IS NOT NULL AND btrim(a.place) <> ''
     ORDER BY a.itinerary_day NULLS LAST, a.atom_id
"""


async def tour_atoms(conn, tour_id: str) -> list[dict]:
    """Active atoms of a tour, read through acp_contract.v_active_tour_atoms (the A3 read rule; the
    view joins FORCE-RLS published_tours, so the read pins app.tenant_id like the catalog reads)."""
    return [dict(r) for r in await _catalog_fetch(conn, ATOMS_SQL, tour_id)]


# ── build row ────────────────────────────────────────────────────────────────────────────────

async def get_build(conn, source_id: str) -> Optional[dict]:
    row = await conn.fetchrow("SELECT * FROM geo.tour_build WHERE source_id = $1", source_id)
    if row is None:
        return None
    d = dict(row)
    d["counts"] = json.loads(d["counts"]) if isinstance(d.get("counts"), str) else (d.get("counts") or {})
    return d


async def lock_build(conn, source_id: str, tour_id: str) -> dict:
    """Inside a transaction: the source's build row, created if missing, locked FOR UPDATE."""
    await conn.execute(
        "INSERT INTO geo.tour_build (source_id, tour_id) VALUES ($1, $2::uuid) ON CONFLICT (source_id) DO NOTHING",
        source_id, tour_id)
    row = await conn.fetchrow("SELECT * FROM geo.tour_build WHERE source_id = $1 FOR UPDATE", source_id)
    return dict(row)


async def has_embedding_column(conn) -> bool:
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'geo' "
        "AND table_name = 'tour_place' AND column_name = 'embedding')"))


async def existing_days(conn, source_id: str) -> list[dict]:
    return [dict(r) for r in await conn.fetch(
        "SELECT * FROM geo.tour_day WHERE source_id = $1 ORDER BY day_index", source_id)]


async def existing_vectors(conn, source_id: str) -> dict[str, str]:
    """text_hash -> pgvector text of the source's current places (embedding reuse)."""
    rows = await conn.fetch(
        "SELECT DISTINCT ON (text_hash) text_hash, embedding::text AS v FROM geo.tour_place "
        "WHERE source_id = $1 AND embedding IS NOT NULL", source_id)
    return {r["text_hash"]: r["v"] for r in rows}


# ── name links ───────────────────────────────────────────────────────────────────────────────

def _keys_arrays(keys: Iterable[tuple]) -> tuple[list, list, list]:
    ks = list(dict.fromkeys(keys))
    return [k[0] for k in ks], [k[1] for k in ks], [k[2] for k in ks]


async def resolved_names(conn, keys: Iterable[tuple]) -> dict[tuple, str]:
    """geo.place_name cache: key -> destination_id for resolved names."""
    a, b, c = _keys_arrays(keys)
    if not a:
        return {}
    rows = await conn.fetch(
        """SELECT p.name_norm, p.country_norm, p.locality_norm, p.destination_id::text AS d
             FROM geo.place_name p JOIN unnest($1::text[], $2::text[], $3::text[]) AS k(n, c, l)
               ON (p.name_norm, p.country_norm, p.locality_norm) = (k.n, k.c, k.l)
            WHERE p.status = 'resolved' AND p.destination_id IS NOT NULL""", a, b, c)
    return {(r["name_norm"], r["country_norm"], r["locality_norm"]): r["d"] for r in rows}


async def known_destinations(conn, keys: Iterable[tuple]) -> dict[tuple, str]:
    """shared.destinations rows reusable by key: located (located_by set), not suspect, not a
    duplicate (design §4 legacy rule — unlocated / Mapbox-by-name rows go back to pending).
    AA-847: a key held only by a merged duplicate (duplicate_of set, admin fix on /admin/places) answers with its
    usable keeper, so the next build of a tour that spells the place the old way does not geocode it again and
    recreate the duplicate. A non-duplicate row of the key wins over a duplicate one."""
    a, b, c = _keys_arrays(keys)
    if not a:
        return {}
    rows = await conn.fetch(
        """SELECT d.name_norm, lower(btrim(d.country)) AS cn, d.locality_norm,
                  COALESCE(k.id, d.id)::text AS d, d.duplicate_of IS NULL AS direct
             FROM shared.destinations d JOIN unnest($1::text[], $2::text[], $3::text[]) AS k0(n, c, l)
               ON d.name_norm = k0.n AND lower(btrim(d.country)) = k0.c AND d.locality_norm = k0.l
             LEFT JOIN shared.destinations k ON k.id = d.duplicate_of
            WHERE d.located_by IS NOT NULL
              AND ((d.duplicate_of IS NULL AND d.geo_status <> 'suspect')
                   OR (d.duplicate_of IS NOT NULL AND k.duplicate_of IS NULL AND k.geo_status <> 'suspect'))
            ORDER BY (d.duplicate_of IS NULL), d.id""", a, b, c)
    return {(r["name_norm"], r["cn"], r["locality_norm"]): r["d"] for r in rows}   # direct rows last: they win


async def duplicate_keeper(conn, key: tuple) -> Optional[str]:
    """AA-847: the usable keeper of a merged duplicate holding `key` when no live row holds it, else None."""
    return await conn.fetchval(
        """SELECT k.id::text FROM shared.destinations d JOIN shared.destinations k ON k.id = d.duplicate_of
            WHERE d.name_norm = $1 AND lower(btrim(d.country)) = $2 AND d.locality_norm = $3
              AND k.duplicate_of IS NULL AND k.geo_status <> 'suspect'
              AND NOT EXISTS (SELECT 1 FROM shared.destinations x
                               WHERE x.name_norm = $1 AND lower(btrim(x.country)) = $2 AND x.locality_norm = $3
                                 AND x.duplicate_of IS NULL)
            ORDER BY d.id LIMIT 1""", key[0], key[1], key[2])


_NAME_INSERT_SQL = """
    INSERT INTO geo.place_name (name_norm, country_norm, locality_norm, display_name, country, locality,
                                tour_name, destination_id, place_card_id, status)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::uuid, $9::uuid, $10)
    ON CONFLICT (name_norm, country_norm, locality_norm) DO NOTHING
"""


async def insert_names(conn, rows: list[tuple]) -> None:
    """New names only (ON CONFLICT DO NOTHING): pending, or resolved when a known destination /
    card destination already answers them. Never updates an existing row (place_geocode owns it)."""
    if rows:
        await conn.executemany(_NAME_INSERT_SQL, rows)


# ── persist one source ───────────────────────────────────────────────────────────────────────

_DAY_INSERT = """
    INSERT INTO geo.tour_day (source_id, day_index, tour_id, title, start_place, end_place, overnight_place,
                              anchor_place, anchor_norm, anchor_country_norm, anchor_locality_norm,
                              overnight_destination_id, places, extracted_by, is_transit_day)
    VALUES ($1, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12::uuid, $13, $14, $15)
"""
_PLACE_COLS = ("component_id, source_id, tour_id, day_index, name, name_norm, country, country_norm, "
               "locality_norm, destination_id, place_card_id, activity, intensity_level, season_months, "
               "duration_hint, text_extract, text_hash, atom_ids")
_PLACE_VALUES = ("$1::uuid, $2, $3::uuid, $4, $5, $6, $7, $8, $9, $10::uuid, $11::uuid, $12, $13, $14, $15, "
                 "$16, $17, $18")


async def replace_source_rows(conn, source_id: str, tour_id: str, days: list[tuple], places: list[tuple],
                              vectors: Optional[list[Optional[str]]]) -> None:
    """Inside the caller's transaction: the source's day and place rows replaced (DELETE+INSERT per
    source — readers never see a half-written tour; design §2.2)."""
    await conn.execute("DELETE FROM geo.tour_day WHERE source_id = $1", source_id)
    # the source's nights may have changed: its outlier observations are recomputed by the next geocode
    await conn.execute("DELETE FROM geo.place_outlier WHERE source_id = $1", source_id)
    if days:
        await conn.executemany(_DAY_INSERT, days)
    await conn.execute("DELETE FROM geo.tour_place WHERE source_id = $1", source_id)
    if places:
        if vectors is not None:
            await conn.executemany(
                f"INSERT INTO geo.tour_place ({_PLACE_COLS}, embedding) VALUES ({_PLACE_VALUES}, $19::vector)",
                [p + (v,) for p, v in zip(places, vectors)])
        else:
            await conn.executemany(f"INSERT INTO geo.tour_place ({_PLACE_COLS}) VALUES ({_PLACE_VALUES})", places)


async def write_build(conn, source_id: str, *, tour_id: str, country: Optional[str], days_hash: Optional[str],
                      components_hash: Optional[str], rule_version: int, status: str, counts: dict,
                      cost_usd: float, job_id: Optional[str]) -> None:
    await conn.execute(
        """UPDATE geo.tour_build SET tour_id = $2::uuid, country = $3, days_hash = $4, components_hash = $5,
                  rule_version = $6, status = $7, counts = $8::jsonb, cost_usd = $9, built_at = now(),
                  job_id = $10::uuid, updated_at = now()
            WHERE source_id = $1""",
        source_id, tour_id, country, days_hash, components_hash, rule_version, status,
        json.dumps(counts, default=str), round(float(cost_usd or 0.0), 6), job_id)


async def set_build_tour(conn, source_id: str, tour_id: str) -> None:
    """Unchanged inputs from another version (re-upload, same text and atoms): only the built-from
    version moves; days / places keep their rows (component ids are stable)."""
    await conn.execute("UPDATE geo.tour_build SET tour_id = $2::uuid, updated_at = now() WHERE source_id = $1",
                       source_id, tour_id)
    await conn.execute("UPDATE geo.tour_day SET tour_id = $2::uuid WHERE source_id = $1", source_id, tour_id)
    await conn.execute("UPDATE geo.tour_place SET tour_id = $2::uuid WHERE source_id = $1", source_id, tour_id)


async def pending_names_due(conn) -> int:
    return int(await conn.fetchval(
        "SELECT count(*) FROM geo.place_name WHERE status = 'pending' AND next_retry_at <= now()"))


# ── geocode ──────────────────────────────────────────────────────────────────────────────────

async def due_names(conn, limit: int) -> list[dict]:
    return [dict(r) for r in await conn.fetch(
        """SELECT name_norm, country_norm, locality_norm, display_name, country, locality, tour_name,
                  place_card_id::text AS place_card_id, tries
             FROM geo.place_name WHERE status = 'pending' AND next_retry_at <= now()
            ORDER BY created_at, name_norm LIMIT $1""", limit)]


async def destination_by_key(conn, key: tuple) -> Optional[dict]:
    row = await conn.fetchrow(
        """SELECT id::text AS id, located_by, geo_status FROM shared.destinations
            WHERE name_norm = $1 AND lower(btrim(country)) = $2 AND locality_norm = $3 AND duplicate_of IS NULL""",
        key[0], key[1], key[2])
    return dict(row) if row else None


# TripPlanner's extraction writers still upsert ON CONFLICT (lower(name)) until slice 6 turns them
# off, so migration 226 keeps the global lower(name) unique index (review finding 1); a later
# migration drops it after the cutover. Until then a destination name must also be globally unique.
LOWER_NAME_INDEX = "destinations_lower_name_uidx"


def name_candidates(name: str, locality: Optional[str], country: Optional[str]) -> list[str]:
    """Names to try for a new destination row, in order: the plain place name, then qualified by
    locality and/or country — used only when the plain name is already another place's (the global
    lower(name) key that TripPlanner still needs)."""
    name = (name or "").strip()
    loc, ctry = (locality or "").strip(), (country or "").strip()
    out = [name]
    if loc:
        out.append(f"{name} ({loc})")
    if ctry:
        out.append(f"{name} ({ctry})")
    if loc and ctry:
        out.append(f"{name} ({loc}, {ctry})")
    return list(dict.fromkeys(out))


_DEST_INSERT_SQL = """
    INSERT INTO shared.destinations (name, country, lat, lng, located_by, located_at, geo_status, confidence,
                                     geocoder_place_id, geo_checked_at, name_norm, locality_norm,
                                     geo_precision, geocoder_provider, geocoded_at, geocoder_meta, review_reason)
    VALUES ($1, $2, $3, $4, $5, now(), 'ok', $6, $7, now(), $8, $9,
            $10, $11, CASE WHEN $11::text IS NULL THEN NULL ELSE now() END, $12::jsonb, $13)
    ON CONFLICT (name_norm, lower(btrim(country)), locality_norm)
        WHERE duplicate_of IS NULL AND name_norm IS NOT NULL DO NOTHING
    RETURNING id::text
"""


def _geo_args(located) -> tuple:
    """(geo_precision, geocoder_provider, geocoder_meta json, review_reason) of a Located point. AA-847 (S246): a
    linear feature is precision 'line' and geo_status 'ok' — drawn from geo.place_shape, never hidden."""
    meta = getattr(located, "meta", None)
    return (located.precision, (meta or {}).get("provider"),
            json.dumps(meta, ensure_ascii=False, default=str) if meta else None,
            getattr(located, "review_reason", None))


async def _write_point(conn, dest_id: str, located, force: bool) -> None:
    await conn.execute(
        """UPDATE shared.destinations SET lat = $2, lng = $3, located_by = $4, located_at = now(),
                  confidence = $5, geocoder_place_id = $6, geo_checked_at = now(),
                  geo_status = CASE WHEN geo_status = 'suspect' AND $7 THEN 'ok'
                                    -- AA-847: a row the first validity branch hid as a linear feature
                                    WHEN geo_status = 'suspect' AND review_reason = 'linear_feature'
                                         AND $8 = 'line' THEN 'ok'
                                    ELSE geo_status END,
                  geo_precision = $8, geocoder_provider = $9,
                  geocoded_at = CASE WHEN $9::text IS NULL THEN geocoded_at ELSE now() END,
                  geocoder_meta = COALESCE($10::jsonb, geocoder_meta), review_reason = $11
            WHERE id = $1::uuid""",
        dest_id, located.lat, located.lng, located.located_by, located.confidence, located.place_id, force,
        *_geo_args(located))


async def _adopt_unkeyed(conn, name: str, key: tuple, located, force: bool) -> Optional[tuple[str, str]]:
    """The plain name clashes on lower(name) with a row TripPlanner wrote after 226 (name_norm NULL):
    when it is the same place (same country, or none recorded) CIS takes it over — sets its key and,
    if the overwrite rule allows, the confirmed point — instead of creating a second row."""
    from services.places.guards import may_overwrite

    old = await conn.fetchrow(
        """SELECT id::text AS id, located_by, geo_status, country, name_norm, duplicate_of
             FROM shared.destinations WHERE lower(name) = lower($1) FOR UPDATE""", name)
    if old is None or old["name_norm"] is not None or old["duplicate_of"] is not None:
        return None
    old_country = (old["country"] or "").strip().lower()
    if old_country and old_country != located.country.strip().lower():
        return None
    await conn.execute(
        """UPDATE shared.destinations SET name_norm = $2, locality_norm = $3,
                  country = COALESCE(NULLIF(btrim(country), ''), $4)
            WHERE id = $1::uuid""", old["id"], key[0], key[2], located.country)
    if may_overwrite(old["located_by"], old["geo_status"], located.located_by, force=force):
        await _write_point(conn, old["id"], located, force)
        return old["id"], "updated"
    return old["id"], "kept"


async def upsert_destination(conn, key: tuple, name: str, located, *, locality: Optional[str] = None,
                             force: bool = False) -> tuple[str, str]:
    """Returns (destination_id, action) with action inserted | updated | kept. The only writer of
    shared.destinations coordinates (place_geocode). Never overwrites an admin-fixed row.

    `name` is the plain place name (the `name` column agrees with name_norm; the locality of a
    generic name is in locality_norm, not in the name). While migration 226 keeps the global
    lower(name) index (TripPlanner cutover), a name taken by another place falls back to a name
    qualified by locality / country (name_candidates). Each attempt runs in a savepoint, so a clash
    never aborts the caller's transaction."""
    import asyncpg

    from services.places.guards import may_overwrite

    keeper = await duplicate_keeper(conn, (key[0], located.country.strip().lower(), key[2]))
    if keeper is not None:   # AA-847: the key belongs to a merged duplicate: answer with its keeper
        return keeper, "kept"
    row = None
    for i, cand in enumerate(name_candidates(name, locality, located.country)):
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    _DEST_INSERT_SQL, cand, located.country, located.lat, located.lng, located.located_by,
                    located.confidence, located.place_id, key[0], key[2], *_geo_args(located))
        except asyncpg.UniqueViolationError as e:
            if getattr(e, "constraint_name", None) != LOWER_NAME_INDEX:
                raise
            if i == 0:
                async with conn.transaction():
                    adopted = await _adopt_unkeyed(conn, cand, key, located, force)
                if adopted is not None:
                    return adopted
            continue
        if row is not None:
            return row["id"], "inserted"
        break  # the new key already has a row: update it below
    else:
        raise RuntimeError(f"no free destination name for {key}: {name!r} is taken in every form")
    # the row is keyed by where the place IS (a Thailand tour's Siem Reap is a Cambodia row)
    old = await destination_by_key(conn, (key[0], located.country.strip().lower(), key[2]))
    if old is None:  # should not happen: the conflict row is that key
        raise RuntimeError(f"destination conflict without a row for {key}")
    if may_overwrite(old["located_by"], old["geo_status"], located.located_by, force=force):
        await _write_point(conn, old["id"], located, force)
        return old["id"], "updated"
    return old["id"], "kept"


_PENDING_SHAPE_SQL = """
    INSERT INTO geo.place_shape (destination_id, kind, status, min_lat, min_lng, max_lat, max_lng)
    SELECT d.id, 'line', 'pending', d.lat, d.lng, d.lat, d.lng
      FROM shared.destinations d
     WHERE d.id = $1::uuid AND d.geo_precision = 'line' AND d.duplicate_of IS NULL
    ON CONFLICT (destination_id) DO NOTHING
    RETURNING destination_id
"""


async def ensure_pending_shape(conn, destination_id: str) -> bool:
    """AA-847: a line destination without a geo.place_shape row gets a pending one (bbox = its point until the shape
    lands). The job kind place_shape is the only other writer. True when a row was created."""
    return await conn.fetchval(_PENDING_SHAPE_SQL, destination_id) is not None


async def mark_name(conn, key: tuple, *, destination_id: Optional[str] = None, error: Optional[str] = None,
                    max_tries: int = 3, retry_days: int = 1) -> str:
    """Resolved (destination_id given), or one more failed try: pending again in `retry_days`,
    'unknown' after `max_tries` (negative cache, like place_card_miss). Returns the new status."""
    if destination_id is not None:
        await conn.execute(
            """UPDATE geo.place_name SET status = 'resolved', destination_id = $4::uuid, tries = tries + 1,
                      last_error = NULL, updated_at = now()
                WHERE (name_norm, country_norm, locality_norm) = ($1, $2, $3)""",
            key[0], key[1], key[2], destination_id)
        return "resolved"
    return await conn.fetchval(
        """UPDATE geo.place_name SET tries = tries + 1, last_error = $4, updated_at = now(),
                  status = CASE WHEN tries + 1 >= $5 THEN 'unknown' ELSE 'pending' END,
                  next_retry_at = now() + make_interval(days => $6)
            WHERE (name_norm, country_norm, locality_norm) = ($1, $2, $3)
        RETURNING status""",
        key[0], key[1], key[2], (error or "")[:500], max_tries, retry_days)


RELINK_SQL = (
    """UPDATE geo.tour_place tp SET destination_id = pn.destination_id
         FROM geo.place_name pn
        WHERE tp.destination_id IS NULL AND pn.status = 'resolved' AND pn.destination_id IS NOT NULL
          AND (tp.name_norm, tp.country_norm, tp.locality_norm) = (pn.name_norm, pn.country_norm, pn.locality_norm)""",
    """UPDATE geo.tour_day td SET overnight_destination_id = pn.destination_id
         FROM geo.place_name pn
        WHERE td.overnight_destination_id IS NULL AND td.anchor_norm IS NOT NULL
          AND pn.status = 'resolved' AND pn.destination_id IS NOT NULL
          AND (td.anchor_norm, td.anchor_country_norm, td.anchor_locality_norm)
              = (pn.name_norm, pn.country_norm, pn.locality_norm)""",
    """UPDATE acp_shared.place_card pc SET destination_id = pn.destination_id
         FROM geo.place_name pn
        WHERE pc.destination_id IS NULL AND pn.place_card_id = pc.card_id
          AND pn.status = 'resolved' AND pn.destination_id IS NOT NULL""",
)


async def relink(conn) -> dict:
    """Plain SQL inside the geocode job (design §3): newly resolved names fill the unlinked places,
    nights and cards of every source — no per-tour re-link jobs."""
    out = {}
    for name, sql in zip(("places", "nights", "cards"), RELINK_SQL):
        status = await conn.execute(sql)
        out[name] = int(status.split()[-1])
    return out


async def night_stops(conn, destination_ids: Optional[list[str]] = None) -> list[dict]:
    """Nights with a located destination, per source (for the outlier rule). AA-847: a line destination ("a night on
    the Mekong") is not a stop: its point is one spot on a long feature, so it would be flagged as a false outlier."""
    rows = await conn.fetch(
        """SELECT td.source_id, td.day_index, td.overnight_destination_id::text AS destination_id,
                  d.lat, d.lng, td.is_transit_day
             FROM geo.tour_day td JOIN shared.destinations d ON d.id = td.overnight_destination_id
                                                         AND d.geo_precision IS DISTINCT FROM 'line'
            WHERE $1::uuid[] IS NULL OR td.source_id IN (
                  SELECT source_id FROM geo.tour_day WHERE overnight_destination_id = ANY($1::uuid[]))
            ORDER BY td.source_id, td.day_index""",
        destination_ids)
    return [dict(r) for r in rows]


async def record_outliers(conn, observations: list[tuple], evaluated_sources: Iterable[str] = ()) -> None:
    """(destination_id, source_id, day_index, km). The observations of every evaluated source are
    replaced (a re-confirmed point or a rebuilt tour drops the stale ones — review finding 10)."""
    sources = sorted(set(evaluated_sources))
    if sources:
        await conn.execute("DELETE FROM geo.place_outlier WHERE source_id = ANY($1::text[])", sources)
    if observations:
        await conn.executemany(
            """INSERT INTO geo.place_outlier (destination_id, source_id, day_index, km)
               VALUES ($1::uuid, $2, $3, $4)
               ON CONFLICT (destination_id, source_id, day_index) DO UPDATE SET km = EXCLUDED.km,
                   observed_at = now()""", observations)


async def prune_outliers(conn, live_source_ids: Iterable[str]) -> int:
    """Observations of sources that are no longer live (trashed / archived) stop counting toward the
    suspect rule. No-op when the live set is empty (an RLS or reset read must not wipe the table)."""
    live = sorted(set(live_source_ids))
    if not live:
        return 0
    status = await conn.execute("DELETE FROM geo.place_outlier WHERE NOT (source_id = ANY($1::text[]))", live)
    return int(status.split()[-1])


async def apply_suspects(conn) -> list[str]:
    """Destinations that now meet the suspect rule (guards.is_suspect, in SQL) → geo_status suspect,
    their names → suspect. Never an admin-fixed row. Returns the ids flagged now."""
    rows = await conn.fetch(
        """WITH o AS (SELECT destination_id, count(DISTINCT source_id) AS n FROM geo.place_outlier
                       GROUP BY destination_id)
           UPDATE shared.destinations d SET geo_status = 'suspect'
             FROM o WHERE d.id = o.destination_id AND d.geo_status = 'ok'
              AND (o.n >= 2 OR (o.n >= 1 AND d.located_by = 'llm'))
           RETURNING d.id::text""")
    ids = [r["id"] for r in rows]
    if ids:
        await conn.execute(
            "UPDATE geo.place_name SET status = 'suspect', updated_at = now() "
            "WHERE destination_id = ANY($1::uuid[]) AND status = 'resolved'", ids)
    return ids


async def geocoder_requests_today(conn, provider: str) -> int:
    """CREDITS this provider was charged today (UTC day — Geoapify's daily credit window) by finished runs of every
    endpoint (geocode search and, AA-847, Place Details): the start value of the daily credit guard. Rows written by
    code older than migration 255 carry no `credits` and count one credit per request."""
    return int(await conn.fetchval(
        """SELECT coalesce(sum(coalesce(credits, requests)), 0) FROM geo.geocode_call
            WHERE provider = $1 AND called_at >= date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'""",
        provider) or 0)


async def log_geocode_calls(conn, provider: str, requests: int, cost_usd: float, job_id: Optional[str], *,
                            credits: Optional[int] = None, endpoint: str = "search") -> None:
    """One row per run and endpoint. `credits` defaults to `requests` (a geocode search is 1 credit)."""
    if requests:
        await conn.execute(
            "INSERT INTO geo.geocode_call (provider, requests, cost_usd, job_id, credits, endpoint) "
            "VALUES ($1, $2, $3, $4::uuid, $5, $6)",
            provider, requests, round(cost_usd, 6), job_id, requests if credits is None else credits, endpoint)


async def destination_briefs(conn, ids: Iterable[str]) -> dict[str, dict]:
    """AA-847: name, country, point and source of usable destinations (the in-tour same-place rule)."""
    ids = sorted({str(i) for i in ids if i})
    if not ids:
        return {}
    rows = await conn.fetch(
        """SELECT id::text AS id, name, country, lat, lng, located_by, geo_precision, review_reason,
                  geocoder_meta ->> 'result_type' AS result_type
             FROM shared.destinations
            WHERE id = ANY($1::uuid[]) AND geo_status <> 'suspect' AND duplicate_of IS NULL""", ids)
    return {r["id"]: dict(r) for r in rows}


# ── graph ────────────────────────────────────────────────────────────────────────────────────

async def graph_inputs(conn, source_ids: list[str]) -> tuple[list[dict], list[dict], dict]:
    """Days and places of the given (live) sources + their usable destinations (not suspect, not
    a duplicate)."""
    days = [dict(r) for r in await conn.fetch(
        """SELECT source_id, day_index, overnight_destination_id
             FROM geo.tour_day WHERE source_id = ANY($1::text[])""", source_ids)]
    places = [dict(r) for r in await conn.fetch(
        """SELECT source_id, day_index, destination_id, activity, intensity_level, season_months
             FROM geo.tour_place WHERE source_id = ANY($1::text[]) AND destination_id IS NOT NULL""", source_ids)]
    ids = {d["overnight_destination_id"] for d in days} | {p["destination_id"] for p in places}
    ids.discard(None)
    dests = {}
    if ids:
        dests = {r["id"]: dict(r) for r in await conn.fetch(
            """SELECT id, name, country, lat, lng, geo_precision AS precision FROM shared.destinations
                WHERE id = ANY($1::uuid[]) AND geo_status <> 'suspect' AND duplicate_of IS NULL""", list(ids))}
    return days, places, dests


KEEP_PREVIOUS_BUILDS = 2


async def swap_graph(conn, g, params: dict, job_id: Optional[str]) -> str:
    """One transaction: new build rows, then the current pointer moves to it; builds older than the
    current + KEEP_PREVIOUS_BUILDS are deleted (cascade). Returns the new build_id."""
    async with conn.transaction():
        build_id = await conn.fetchval(
            "INSERT INTO geo.graph_build (params, stats, job_id) VALUES ($1::jsonb, $2::jsonb, $3::uuid) "
            "RETURNING build_id::text", json.dumps(params), json.dumps(g.stats, default=str), job_id)
        dests_country = {n["destination_id"]: n["country"] for n in g.nodes}
        if g.stops:
            await conn.executemany(
                "INSERT INTO geo.tour_stop (build_id, source_id, day_index, destination_id, country, source) "
                "VALUES ($1::uuid, $2, $3, $4, $5, $6)",
                [(build_id, s.source_id, s.day_index, s.destination_id, dests_country.get(s.destination_id), s.source)
                 for s in g.stops])
        if g.nodes:
            await conn.executemany(
                "INSERT INTO geo.graph_node (build_id, destination_id, name, country, lat, lng, source_ids, "
                "stop_count, activities, intensity_min, intensity_max, season_months) "
                "VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
                [(build_id, n["destination_id"], n["name"], n["country"], n["lat"], n["lng"], n["source_ids"],
                  n["stop_count"], n["activities"], n["intensity_min"], n["intensity_max"], n["season_months"])
                 for n in g.nodes])
        if g.edges:
            await conn.executemany(
                "INSERT INTO geo.graph_edge (build_id, from_destination_id, to_destination_id, tour_count, "
                "source_ids, km) VALUES ($1::uuid, $2, $3, $4, $5, $6)",
                [(build_id, e["from_destination_id"], e["to_destination_id"], e["tour_count"], e["source_ids"],
                  e["km"]) for e in g.edges])
        if g.legs:
            await conn.executemany(
                "INSERT INTO geo.tour_leg (build_id, source_id, day_from, day_to, days, start_destination_id, "
                "end_destination_id, destination_ids, countries, activities) "
                "VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                [(build_id, leg["source_id"], leg["day_from"], leg["day_to"], leg["days"],
                  leg["start_destination_id"], leg["end_destination_id"], leg["destination_ids"],
                  leg["countries"], leg["activities"]) for leg in g.legs])
        if g.junctions:
            await conn.executemany(
                "INSERT INTO geo.tour_junction (build_id, destination_a, destination_b, km, cross_border) "
                "VALUES ($1::uuid, $2, $3, $4, $5)",
                [(build_id, j["destination_a"], j["destination_b"], j["km"], j["cross_border"])
                 for j in g.junctions])
        await conn.execute("UPDATE geo.graph_build SET is_current = false WHERE is_current")
        await conn.execute("UPDATE geo.graph_build SET is_current = true WHERE build_id = $1::uuid", build_id)
        await conn.execute(
            """DELETE FROM geo.graph_build WHERE build_id IN (
                   SELECT build_id FROM geo.graph_build ORDER BY built_at DESC, build_id OFFSET $1)""",
            KEEP_PREVIOUS_BUILDS + 1)
    return build_id
