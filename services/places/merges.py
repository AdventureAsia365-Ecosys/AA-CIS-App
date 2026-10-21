"""AA-847 Problem 2 — SQL of the automatic duplicate-place merge (migration 257). Every function takes an asyncpg
connection; transactions are the caller's unless stated.

  load_rows        shared.destinations (located, not suspect) + shape, overnight stop, card, live components,
                   merged members → dedupe.Row objects of one country (or all)
  apply_merge      duplicate_of = keeper, repoint the CIS rows, flatten the merged row's own members, log
                   geo.place_merge (one transaction, under lock_destinations)
  undo_merge       clear duplicate_of, restore the pointers that still hold the keeper, write a `different` pin
  repoint_dangling every CIS pointer that still names a merged row → its keeper (idempotent; a stale place_extract
                   write lands after a merge)
  verdicts / save_verdict / save_pin    geo.place_pair_verdict (cache by input hash; pins hold for any hash)
  allow_list / set_country               geo.place_dedupe_country
  list_merges / labelled_keeps           the /admin/places Merges tab; undone merges as labelled keep pairs

Not repointed (left on the merged id, hidden by the duplicate_of filters): geo.place_shape and geo.place_outlier
(primary keys would collide), geo.tour_segment and the graph tables (rebuilt by the graph build the merge enqueues).
A merged row's shape is never drawn, so a job merge never takes a line into a keeper that is not one (owner decision
S246 (1)): dedupe.keeper_key makes the line the keeper, dedupe.hides_line cuts the rest (a manual point keeper) from
the plan, and apply_merge re-checks it under the lock (line_hidden).
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable, Optional

from services.places import dedupe as dd
from services.places.store import lock_destinations

# (table, key columns, destination column) of the CIS rows a merge repoints (= validity_fix.REPOINT)
REPOINT = (
    ("geo.tour_place", ("component_id",), "destination_id"),
    ("geo.tour_day", ("source_id", "day_index"), "overnight_destination_id"),
    ("geo.place_name", ("name_norm", "country_norm", "locality_norm"), "destination_id"),
    ("acp_shared.place_card", ("card_id",), "destination_id"),
    ("shared.place_photo", ("id",), "destination_id"),
)
PIN = "*"


class MergeError(Exception):
    def __init__(self, status: int, detail):
        super().__init__(status, detail)
        self.status, self.detail = status, detail


def country_key(country: Optional[str]) -> str:
    return (country or "").strip().lower()


def diff_hash(changes: Iterable) -> str:
    return hashlib.sha256(json.dumps(sorted(list(c) for c in changes)).encode()).hexdigest()[:24]


# ── load ────────────────────────────────────────────────────────────────────────────────────────────────────────────

_ROWS_SQL = """
    SELECT d.id::text AS id, d.name, d.country, d.lat, d.lng, d.name_norm, d.locality_norm, d.located_by,
           d.geo_status, d.geo_precision, d.geocoder_meta ->> 'result_type' AS result_type,
           d.geocoder_meta ->> 'name' AS geocoder_name, d.geocoder_place_id, d.created_at,
           d.duplicate_of::text AS duplicate_of,
           ps.status AS shape_status, ps.osm_type, ps.osm_id, ps.geom_lo, ps.min_lat, ps.min_lng, ps.max_lat,
           ps.max_lng, ps.length_km, ps.kind AS shape_kind, ps.meta -> 'names' AS osm_names,
           ps.meta ->> 'line_class' AS line_class,
           CASE WHEN ps.status IN ('ok', 'manual') THEN ps.geom_mid END AS geom_mid
      FROM shared.destinations d
      LEFT JOIN geo.place_shape ps ON ps.destination_id = d.id
     WHERE d.geo_status <> 'suspect' AND d.located_by IS NOT NULL
       AND ($1::text IS NULL OR lower(btrim(d.country)) = $1)
"""
# an overnight stop of a built, not retired source
_STOPS_SQL = """
    SELECT DISTINCT td.overnight_destination_id::text AS id
      FROM geo.tour_day td JOIN geo.tour_build tb ON tb.source_id = td.source_id AND tb.status <> 'retired'
     WHERE td.overnight_destination_id IS NOT NULL
"""
_COMPONENTS_SQL = """
    SELECT tp.destination_id::text AS id, count(*) AS n
      FROM geo.tour_place tp JOIN geo.tour_build tb ON tb.source_id = tp.source_id AND tb.status <> 'retired'
     WHERE tp.destination_id IS NOT NULL GROUP BY 1
"""
# cards: the card naming the row directly, else the majority card of the names resolved to it (never a rejected one)
_CARDS_DIRECT_SQL = """
    SELECT destination_id::text AS id, card_id::text AS card, display_name
      FROM acp_shared.place_card WHERE destination_id IS NOT NULL AND status <> 'rejected'
"""
_CARDS_LINKED_SQL = """
    SELECT pn.destination_id::text AS id, pn.place_card_id::text AS card, pc.display_name, count(*) AS n
      FROM geo.place_name pn
      JOIN acp_shared.place_card pc ON pc.card_id = pn.place_card_id AND pc.status <> 'rejected'
     WHERE pn.destination_id IS NOT NULL AND pn.place_card_id IS NOT NULL
     GROUP BY 1, 2, 3
"""


def _shape(r) -> Optional[dd.Shape]:
    if r["shape_status"] is None:
        return None
    lines = dd.geojson_lines(r["geom_lo"]) if r["geom_lo"] is not None else ()
    bbox = (float(r["min_lat"]), float(r["min_lng"]), float(r["max_lat"]), float(r["max_lng"]))
    names = r["osm_names"]
    names = json.loads(names) if isinstance(names, str) else (names or [])
    mid = r["geom_mid"]
    mid = json.loads(mid) if isinstance(mid, str) else mid
    return dd.Shape(r["shape_status"], r["osm_type"], int(r["osm_id"]) if r["osm_id"] is not None else None, lines,
                    bbox, float(r["length_km"]) if r["length_km"] is not None else None, mid,
                    tuple(str(n) for n in names if n) if isinstance(names, list) else (), r["line_class"],
                    r["shape_kind"] or "line")


async def load_rows(conn, country: Optional[str] = None) -> tuple[list[dd.Row], dict[str, list[dd.Row]]]:
    """(usable rows, {keeper id: [rows merged into it]}) of one country (lower(btrim) form) or all."""
    rows = await conn.fetch(_ROWS_SQL, country_key(country) or None)
    stops = {r["id"] for r in await conn.fetch(_STOPS_SQL)}
    comps = {r["id"]: int(r["n"]) for r in await conn.fetch(_COMPONENTS_SQL)}
    direct: dict[str, list] = {}
    names: dict[str, str] = {}
    for r in await conn.fetch(_CARDS_DIRECT_SQL):
        direct.setdefault(r["id"], []).append(r["card"])
        names[r["card"]] = r["display_name"]
    linked: dict[str, dict] = {}
    for r in await conn.fetch(_CARDS_LINKED_SQL):
        linked.setdefault(r["id"], {})[r["card"]] = int(r["n"])
        names[r["card"]] = r["display_name"]
    built = []
    for r in rows:
        card = dd.derive_card(direct.get(r["id"], []), linked.get(r["id"], {}))
        built.append((r["duplicate_of"], dd.Row(
            r["id"], r["name"], r["country"], float(r["lat"]), float(r["lng"]), name_norm=r["name_norm"],
            locality_norm=r["locality_norm"] or "", located_by=r["located_by"], geo_status=r["geo_status"],
            precision=r["geo_precision"], result_type=r["result_type"], geocoder_name=r["geocoder_name"],
            place_id=r["geocoder_place_id"], stop=r["id"] in stops, card=card, card_name=names.get(card),
            components=comps.get(r["id"], 0), created_at=r["created_at"].isoformat() if r["created_at"] else "",
            shape=_shape(r))))
    merged: dict[str, list[dd.Row]] = {}
    for dup_of, row in built:
        if dup_of:
            merged.setdefault(dup_of, []).append(row)
    usable = [dd.Row(**{**row.__dict__, "merged_members": len(merged.get(row.id, []))})
              for dup_of, row in built if not dup_of]
    return usable, merged


async def has_embedding(conn) -> bool:
    return bool(await conn.fetchval(
        "SELECT 1 FROM pg_attribute WHERE attrelid = 'geo.tour_place'::regclass AND attname = 'embedding' "
        "AND NOT attisdropped"))


async def embedding_cosines(conn, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], float]:
    """Max cosine between the two destinations' component embeddings (geo.tour_place.embedding, cohere-embed-v4):
    free, no model call. {} when pgvector is absent."""
    if not pairs or not await has_embedding(conn):
        return {}
    rows = await conn.fetch(
        """SELECT x.a::text AS a, x.b::text AS b, max(1 - (ta.embedding <=> tb.embedding)) AS cos
             FROM unnest($1::uuid[], $2::uuid[]) AS x(a, b)
             JOIN geo.tour_place ta ON ta.destination_id = x.a AND ta.embedding IS NOT NULL
             JOIN geo.tour_place tb ON tb.destination_id = x.b AND tb.embedding IS NOT NULL
            GROUP BY 1, 2""", [p[0] for p in pairs], [p[1] for p in pairs])
    return {(r["a"], r["b"]): float(r["cos"]) for r in rows if r["cos"] is not None}


async def excerpts(conn, ids: Iterable[str], per_place: int = 2) -> dict[str, list[str]]:
    """Up to `per_place` component text extracts per destination (≤ 300 characters each), quoted to the model."""
    ids = sorted({str(i) for i in ids})
    if not ids:
        return {}
    rows = await conn.fetch(
        """SELECT destination_id::text AS id, text_extract
             FROM (SELECT destination_id, text_extract,
                          row_number() OVER (PARTITION BY destination_id ORDER BY created_at, component_id) AS n
                     FROM geo.tour_place WHERE destination_id = ANY($1::uuid[]) AND text_extract <> '') t
            WHERE n <= $2""", ids, per_place)
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["id"], []).append((r["text_extract"] or "")[:300])
    return out


# ── verdicts ────────────────────────────────────────────────────────────────────────────────────────────────────────

async def verdicts(conn, ids: Iterable[str]) -> tuple[dict, dict]:
    """({(lo, hi, input_hash): verdict} of model answers, {(lo, hi): verdict} of pins) touching these rows."""
    ids = sorted({str(i) for i in ids})
    if not ids:
        return {}, {}
    rows = await conn.fetch(
        """SELECT lo_id::text AS lo, hi_id::text AS hi, input_hash, verdict
             FROM geo.place_pair_verdict WHERE lo_id = ANY($1::uuid[]) OR hi_id = ANY($1::uuid[])""", ids)
    cache, pins = {}, {}
    for r in rows:
        if r["input_hash"] == PIN:
            pins[(r["lo"], r["hi"])] = r["verdict"]
        else:
            cache[(r["lo"], r["hi"], r["input_hash"])] = r["verdict"]
    return cache, pins


async def save_verdict(conn, lo: str, hi: str, input_hash: str, result: dict, *, models: list, cost_usd: float,
                       job_id: Optional[str], created_by: str) -> None:
    await conn.execute(
        """INSERT INTO geo.place_pair_verdict (lo_id, hi_id, input_hash, verdict, source, answers, models,
                                               rule_version, prompt_sha, cost_usd, job_id, created_by)
           VALUES ($1::uuid, $2::uuid, $3, $4, 'model', $5::jsonb, $6::jsonb, $7, $8, $9, $10::uuid, $11)
           ON CONFLICT (lo_id, hi_id, input_hash) DO NOTHING""",
        lo, hi, input_hash, result["verdict"], json.dumps(result.get("answers") or []), json.dumps(models),
        dd.RULE_VERSION, dd.prompt_sha(), round(float(cost_usd or 0.0), 6), job_id, created_by)


async def save_pin(conn, a: str, b: str, verdict: str, *, source: str, created_by: str,
                   job_id: Optional[str] = None) -> None:
    lo, hi = dd.pair_key(a, b)
    await conn.execute(
        """INSERT INTO geo.place_pair_verdict (lo_id, hi_id, input_hash, verdict, source, rule_version, job_id,
                                               created_by)
           VALUES ($1::uuid, $2::uuid, '*', $3, $4, $5, $6::uuid, $7)
           ON CONFLICT (lo_id, hi_id, input_hash) DO UPDATE
              SET verdict = EXCLUDED.verdict, source = EXCLUDED.source, created_at = now(),
                  created_by = EXCLUDED.created_by""",
        lo, hi, verdict, source, dd.RULE_VERSION, job_id, created_by)


# ── allow-list ──────────────────────────────────────────────────────────────────────────────────────────────────────

async def allow_list(conn) -> dict[str, dict]:
    rows = await conn.fetch("SELECT country, mode, approved_diff_hash, approved_diff, rule_version, updated_by, "
                            "updated_at FROM geo.place_dedupe_country ORDER BY country")
    out = {}
    for r in rows:
        d = dict(r)
        d["approved_diff"] = d["approved_diff"] if isinstance(d["approved_diff"], list) else \
            json.loads(d["approved_diff"] or "[]")
        out[r["country"]] = d
    return out


async def set_country(conn, country: str, mode: Optional[str], *, actor: str, diff: Optional[list] = None) -> dict:
    """mode None: off (removed: no model band, no writes); 'dry_run': model band, report only; 'apply': writes the
    approved diff (and up to the breaker outside it)."""
    key = country_key(country)
    if not key:
        raise MergeError(422, "country required")
    if mode is None:
        await conn.execute("DELETE FROM geo.place_dedupe_country WHERE country = $1", key)
        return {"country": key, "mode": "off"}
    if mode not in ("dry_run", "apply"):
        raise MergeError(422, "mode must be off, dry_run or apply")
    if mode == "apply" and diff is None:
        raise MergeError(422, "apply needs the approved dry-run diff")
    h = diff_hash(diff) if diff is not None else None
    await conn.execute(
        """INSERT INTO geo.place_dedupe_country (country, mode, approved_diff_hash, approved_diff, rule_version,
                                                 updated_by, updated_at)
           VALUES ($1, $2, $3, $4::jsonb, $5, $6, now())
           ON CONFLICT (country) DO UPDATE
              SET mode = EXCLUDED.mode, approved_diff_hash = EXCLUDED.approved_diff_hash,
                  approved_diff = EXCLUDED.approved_diff, rule_version = EXCLUDED.rule_version,
                  updated_by = EXCLUDED.updated_by, updated_at = now()""",
        key, mode, h, json.dumps(diff or []), dd.RULE_VERSION, actor)
    return {"country": key, "mode": mode, "approved_diff_hash": h}


async def drop_to_dry_run(conn, country: str, *, reason: str) -> None:
    """The rule version changed since the approval: back to dry_run (the approved diff no longer applies)."""
    await conn.execute(
        """UPDATE geo.place_dedupe_country SET mode = 'dry_run', approved_diff_hash = NULL, approved_diff = '[]',
                  rule_version = $2, updated_by = $3, updated_at = now() WHERE country = $1""",
        country_key(country), dd.RULE_VERSION, f"place_dedupe:{reason}")


# ── apply / undo ────────────────────────────────────────────────────────────────────────────────────────────────────

def _where(keys: tuple, start: int) -> str:
    return " AND ".join(f"{k}::text = ${start + i}" for i, k in enumerate(keys))


def line_hidden(keeper, merged) -> bool:
    """dedupe.hides_line on two shared.destinations rows (geo_precision, the geo.place_shape status): merging would
    leave a line, or a drawn line, on the merged id where the catalog never draws it."""
    def drawn(r) -> bool:
        return r["geo_precision"] == "line" and r["shape_status"] in dd.SHAPE_OK
    return ((merged["geo_precision"] == "line" and keeper["geo_precision"] != "line")
            or (drawn(merged) and not drawn(keeper)))


async def apply_merge(conn, m: dict, *, created_by: str, job_id: Optional[str] = None) -> Optional[str]:
    """One merge (a plan_country entry, or the admin fix's) in the caller's transaction. Re-checks under the lock that
    both rows are still usable, the merged row is not admin-fixed and the merge would not hide a line (a job merge
    never stops a line from being drawn); returns the geo.place_merge id, or None when the rows changed (nothing
    written)."""
    await lock_destinations(conn)
    merged, keeper = m["merged_id"], m["keeper_id"]
    st = await conn.fetch(
        """SELECT d.id::text AS id, d.duplicate_of, d.geo_status, d.located_by, d.geo_precision,
                  ps.status AS shape_status
             FROM shared.destinations d LEFT JOIN geo.place_shape ps ON ps.destination_id = d.id
            WHERE d.id = ANY($1::uuid[]) FOR UPDATE OF d""", [merged, keeper])
    by_id = {r["id"]: r for r in st}
    if len(by_id) != 2 or any(r["duplicate_of"] is not None or r["geo_status"] == "suspect" for r in st):
        return None
    if m.get("decided_by") != "admin" and (by_id[merged]["geo_status"] == "manual"
                                           or by_id[merged]["located_by"] == "manual"):
        return None
    if m.get("decided_by") != "admin" and line_hidden(by_id[keeper], by_id[merged]):
        return None         # owner decision S246 (1): the plan cuts this (dedupe.hides_line); re-checked on fresh rows
    repointed: dict[str, list] = {}
    for table, keys, col in REPOINT:
        rows = await conn.fetch(
            f"UPDATE {table} SET {col} = $1::uuid WHERE {col} = $2::uuid "
            f"RETURNING {', '.join(f'{k}::text AS {k}' for k in keys)}", keeper, merged)
        if rows:
            repointed[table] = [[r[k] for k in keys] for r in rows]
    flattened = [r["id"] for r in await conn.fetch(
        "UPDATE shared.destinations SET duplicate_of = $1::uuid WHERE duplicate_of = $2::uuid RETURNING id::text AS id",
        keeper, merged)]
    await conn.execute("UPDATE shared.destinations SET duplicate_of = $1::uuid WHERE id = $2::uuid", keeper, merged)
    return await conn.fetchval(
        """INSERT INTO geo.place_merge (merged_id, keeper_id, country, decided_by, rule_version, score, features,
                                        verdict, repointed, prior, job_id, created_by)
           VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb, $10::jsonb, $11::uuid, $12)
           RETURNING id::text""",
        merged, keeper, m.get("country") or "", m.get("decided_by") or "rule",
        m.get("rule_version") or dd.RULE_VERSION, m.get("score"), json.dumps(m.get("features") or {}, default=str),
        json.dumps(m["verdict"]) if m.get("verdict") else None, json.dumps(repointed),
        json.dumps({"flattened": flattened} if flattened else {}), job_id, created_by)


async def assert_roots(conn) -> int:
    """duplicate_of always names a root: the number of rows whose keeper is itself merged (0 expected)."""
    return int(await conn.fetchval(
        """SELECT count(*) FROM shared.destinations d JOIN shared.destinations k ON k.id = d.duplicate_of
            WHERE k.duplicate_of IS NOT NULL"""))


async def undo_merge(conn, merge_id: str, *, actor: str, reason: str, pin_source: str = "undo",
                     dry_run: bool = False) -> dict:
    """Undo one merge in the caller's transaction (409 on a later dependent merge, a name-key collision or a merged
    row that is no longer this merge's). Restores the pointers listed in `repointed` that still hold the keeper and
    reports the rows that changed since (a rebuilt tour); writes a `different` pin so the job never re-makes it."""
    await lock_destinations(conn)
    m = await conn.fetchrow(
        """SELECT id::text AS id, merged_id::text AS merged_id, keeper_id::text AS keeper_id, country, decided_by,
                  repointed, prior, undone_at FROM geo.place_merge WHERE id = $1::uuid FOR UPDATE""", merge_id)
    if m is None:
        raise MergeError(404, "no merge with this id")
    if m["undone_at"] is not None:
        raise MergeError(409, "this merge was already undone")
    if m["decided_by"] == "admin" and pin_source != "admin":
        # the admin fix also moved geo.place_shape rows (validity_fix.shape_merge): its own undo restores them
        raise MergeError(409, "an admin merge is undone with the validity fix undo (its audit row)")
    merged, keeper = m["merged_id"], m["keeper_id"]
    later = await conn.fetch(
        """SELECT id::text AS id, merged_id::text AS merged_id, keeper_id::text AS keeper_id FROM geo.place_merge
            WHERE undone_at IS NULL AND id <> $1::uuid AND merged_id = $2::uuid""", merge_id, keeper)
    if later:
        raise MergeError(409, {"error": "a later merge moved this keeper; undo that one first",
                               "depends_on": [dict(r) for r in later]})
    cur = await conn.fetchrow(
        """SELECT d.duplicate_of::text AS duplicate_of, d.name_norm, lower(btrim(d.country)) AS cn, d.locality_norm,
                  EXISTS (SELECT 1 FROM shared.destinations x
                           WHERE x.id <> d.id AND x.duplicate_of IS NULL AND d.name_norm IS NOT NULL
                             AND x.name_norm = d.name_norm AND lower(btrim(x.country)) = lower(btrim(d.country))
                             AND x.locality_norm = d.locality_norm) AS taken
             FROM shared.destinations d WHERE d.id = $1::uuid FOR UPDATE""", merged)
    if cur is None or cur["duplicate_of"] != keeper:
        raise MergeError(409, {"error": "the merged row no longer points at this keeper",
                               "duplicate_of": cur["duplicate_of"] if cur else None})
    if cur["taken"]:
        raise MergeError(409, {"error": "another live destination took this name key since the merge",
                               "name_norm": cur["name_norm"], "country": cur["cn"]})
    repointed = m["repointed"] if isinstance(m["repointed"], dict) else json.loads(m["repointed"] or "{}")
    prior = m["prior"] if isinstance(m["prior"], dict) else json.loads(m["prior"] or "{}")
    out = {"merge_id": merge_id, "merged_id": merged, "keeper_id": keeper, "dry_run": dry_run, "restored": {},
           "left": {}}
    if dry_run:
        out["would_restore"] = {t: len(v) for t, v in repointed.items()}
        return out
    for table, keys, col in REPOINT:
        listed = repointed.get(table) or []
        n = 0
        for key in listed:
            status = await conn.execute(
                f"UPDATE {table} SET {col} = $1::uuid WHERE {col} = $2::uuid AND {_where(keys, 3)}",
                merged, keeper, *key)
            n += int(status.split()[-1])
        if listed:
            out["restored"][table] = n
            if n < len(listed):
                out["left"][table] = len(listed) - n           # rebuilt / changed since: the relink settles them
    flat = prior.get("flattened") or []
    if flat:
        await conn.execute("UPDATE shared.destinations SET duplicate_of = $1::uuid "
                           "WHERE id = ANY($2::uuid[]) AND duplicate_of = $3::uuid", merged, flat, keeper)
    await conn.execute("UPDATE shared.destinations SET duplicate_of = NULL WHERE id = $1::uuid", merged)
    await conn.execute("UPDATE geo.place_merge SET undone_at = now(), undone_by = $2, undo_reason = $3 "
                       "WHERE id = $1::uuid", merge_id, actor, reason)
    await save_pin(conn, merged, keeper, "different", source=pin_source, created_by=actor)
    return out


DANGLING_SQL = tuple(
    f"""UPDATE {table} t SET {col} = d.duplicate_of
          FROM shared.destinations d
         WHERE t.{col} = d.id AND d.duplicate_of IS NOT NULL"""
    for table, _keys, col in REPOINT)


async def repoint_dangling(conn) -> dict:
    """Every CIS pointer that still names a merged row → its keeper (idempotent). Covers a place_extract persist that
    read a name's destination before a merge and wrote it after."""
    out = {}
    for (table, _k, _c), sql in zip(REPOINT, DANGLING_SQL):
        status = await conn.execute(sql)
        n = int(status.split()[-1])
        if n:
            out[table] = n
    return out


# ── admin views ─────────────────────────────────────────────────────────────────────────────────────────────────────

async def list_merges(conn, *, country: Optional[str] = None, active: Optional[bool] = None, page: int = 1,
                      page_size: int = 50) -> dict:
    page, page_size = max(1, page), max(1, min(page_size, 200))
    where = ["($1::text IS NULL OR lower(btrim(m.country)) = $1)",
             "($2::boolean IS NULL OR (m.undone_at IS NULL) = $2)"]
    total = await conn.fetchval(f"SELECT count(*) FROM geo.place_merge m WHERE {' AND '.join(where)}",
                                country_key(country) or None, active)
    rows = await conn.fetch(
        f"""SELECT m.id::text AS id, m.merged_id::text AS merged_id, dm.name AS merged_name,
                   m.keeper_id::text AS keeper_id, dk.name AS keeper_name, m.country, m.decided_by, m.rule_version,
                   m.score, m.features ->> 'reason' AS reason, m.features -> 'km' AS km, m.verdict, m.repointed,
                   m.prior, m.job_id::text AS job_id, m.created_at, m.created_by, m.undone_at, m.undone_by,
                   m.undo_reason
              FROM geo.place_merge m
              JOIN shared.destinations dm ON dm.id = m.merged_id
              JOIN shared.destinations dk ON dk.id = m.keeper_id
             WHERE {' AND '.join(where)}
             ORDER BY m.created_at DESC, m.id LIMIT $3 OFFSET $4""",
        country_key(country) or None, active, page_size, (page - 1) * page_size)
    items = []
    for r in rows:
        d = dict(r)
        for k in ("repointed", "prior", "verdict"):
            if isinstance(d.get(k), str):
                d[k] = json.loads(d[k])
        d["repointed_counts"] = {t: len(v) for t, v in (d.pop("repointed") or {}).items()}
        d["score"] = float(d["score"]) if d["score"] is not None else None
        d["km"] = json.loads(d["km"]) if isinstance(d["km"], str) else d["km"]
        for k in ("created_at", "undone_at"):
            d[k] = d[k].isoformat() if d[k] else None
        items.append(d)
    return {"total": total, "page": page, "page_size": page_size, "items": items}


async def labelled_keeps(conn) -> list[list]:
    """Every merge undone by a person (not the job's re-check) as a labelled keep pair for
    tests/unit/fixtures/aa847_dedupe_pairs.json: [merged name, keeper name, country, km, reason, undo reason]."""
    rows = await conn.fetch(
        """SELECT dm.name AS a, dk.name AS b, m.country, m.features -> 'km' AS km, m.features ->> 'reason' AS reason,
                  m.undo_reason, m.undone_at
             FROM geo.place_merge m
             JOIN shared.destinations dm ON dm.id = m.merged_id
             JOIN shared.destinations dk ON dk.id = m.keeper_id
            WHERE m.undone_at IS NOT NULL AND m.undo_reason NOT LIKE 'recheck:%'
            ORDER BY m.undone_at""")
    return [[r["a"], r["b"], r["country"], json.loads(r["km"]) if isinstance(r["km"], str) else r["km"],
             r["reason"], r["undo_reason"]] for r in rows]
