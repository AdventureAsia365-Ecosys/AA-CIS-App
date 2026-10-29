"""AA-671 S257 — job kind `photo_destination_match`: give the tour-matched photos that have no destination the
destination their label names (shared.place_photo.destination_id), so the photo views by place
(shared.v_destination_photos, catalog.v_destination_photo) and the pool cover fallback find them.

Payload: {"dry_run": bool (DEFAULT TRUE — pass false to write), "country": str | None, "limit": int (1..5000,
          default 5000), "fill_covers": bool (default false; apply only — see below), "version": 1 | 2 (default 1)}.

Version 2 (S258, $0): the matcher also breaks a tie between places on one spot (tie_break same_spot) and matches a
label that IS the name of a place near the tour (rule nearby_exact: "Paro" -> the town Paro, at most 20 km from a place
of the tour, where version 1 only reported the monastery "Paro Taktsang" as weak); see match.resolve_destination.
The photo sync stays on version 1 until a version 2 run has been reviewed.

Rule (services.photos.destinations + services.photos.match.resolve_destination): a photo is compared only with the
places of its own tour (geo.tour_place of the tour's source key + the current graph build's stops, non-duplicate
destinations); only an unambiguous answer of a strong rule is written (a WEAK label-inside-a-name answer, "Paro" in
"Paro Taktsang", is counted and sampled, never written). Rows: status 'matched', tour_id set, destination_id NULL
(automatic and manual rows alike: a manual tour assignment left the destination empty; a destination set by anyone is
never changed, a rejected photo never touched). Every write is marked destination_match_source = 'auto' (CIS 303), so
it stays distinguishable from a person's choice: the sync re-matches it when its tour changes or on request, and a
folder-wide tour correction empties it.

``limit`` bounds one run (rows in (country, id) order). A row that does not match stays open, so with fewer rows
than are open a second run reads the same first rows again: use the default (it covers the whole table today, ~3,200
photos) or narrow by ``country``.

Safe to repeat: a written row leaves the set, so a second run plans only what is still open, and the UPDATE lands
only while the row still has no destination, is matched and has the same tour as when it was planned. One UPDATE
statement (atomic). Not run while a photo_sync job is running (``stopped: photo_sync_running``, success).

Covers: shared.destinations.cover_image_url is read by the TripPlanner, so filling it is opt-in, like in the photo
sync (``fill_covers``: after an apply, sync.fill_destination_covers — only empty covers or covers it set earlier).
Every run (dry run included) previews it in ``covers_preview``: covers that would change now, and the planned
destinations whose cover is empty / job-set and that have no served photo yet (an upper bound: a planned photo
without a stored rendition is not served).

Coverage: every run reports ``covers_coverage`` — the pool tours per cover level (services/photos/covers.py: tour,
place, nearby, country, none), the tours without a cover per country, and on a dry run ``to_place_after_apply`` (tours
below the place level that have a place this run would give a photo). An apply reports it again after the writes
(``covers_coverage_after``). A failed coverage read is reported, never fails the run.

Audit: the result counts every outcome (matched / ambiguous / no_match / no_candidate / no_label), the rule that
matched, per country, with samples; an apply logs one `photo_destination_matched` line per photo it wrote and one
`photo_destination_ambiguous` line per tied photo. No model call, no Drive or S3 I/O ($0).
Enqueue: POST /admin/photos/destination-match (one queued or running job at a time).
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Optional

import structlog

from shared.jobs.registry import JobContext, NonRetryable, job_kind

logger = structlog.get_logger()

KIND = "photo_destination_match"
MAX_LIMIT = 5000
DEFAULT_LIMIT = MAX_LIMIT
MAX_SAMPLES = 30
OUTCOMES = ("matched", "ambiguous", "weak", "no_match", "no_candidate", "no_label")
VERSIONS = (1, 2)

_OPEN = """
      FROM shared.place_photo
     WHERE status = 'matched' AND tour_id IS NOT NULL AND destination_id IS NULL
       AND ($1::text IS NULL OR country = $1)
"""
OPEN_COUNT_SQL = "SELECT count(*)" + _OPEN
OPEN_ROWS_SQL = ("SELECT id::text AS id, country, tour_id::text AS tour_id, place_label, match_source" + _OPEN
                 + " ORDER BY country, id LIMIT $2")
# What the photo views by place serve (v_destination_photos: matched, a destination, a stored rendition).
SERVED_SQL = """
    SELECT count(*) AS photos, count(DISTINCT destination_id) AS destinations
      FROM shared.place_photo
     WHERE status = 'matched' AND destination_id IS NOT NULL AND s3_key_large IS NOT NULL
"""
APPLY_SQL = """
    UPDATE shared.place_photo p SET destination_id = x.destination_id, destination_match_source = 'auto',
           updated_at = now()
      FROM unnest($1::uuid[], $2::uuid[], $3::uuid[]) AS x(id, destination_id, tour_id)
     WHERE p.id = x.id AND p.destination_id IS NULL AND p.status = 'matched' AND p.tour_id = x.tour_id
    RETURNING p.id::text AS id
"""
SYNC_RUNNING_SQL = "SELECT 1 FROM shared.job WHERE kind = 'photo_sync' AND status = 'running' LIMIT 1"
# $1 public base, $2 the job-owned cover prefix (base + /content/photos/), $3 the planned destination ids.
# pending_now: the covers sync.fill_destination_covers would change right now (same WHERE);
# new_empty / new_job_owned: planned destinations with an empty / job-set cover and no served photo yet.
COVERS_PREVIEW_SQL = """
    SELECT (SELECT count(*) FROM shared.destinations d
              JOIN shared.v_destination_photos p ON p.destination_id = d.id AND p.position = 1
             WHERE (d.cover_image_url IS NULL OR d.cover_image_url LIKE $2 || '%')
               AND d.cover_image_url IS DISTINCT FROM $1 || p.path || '?size=large') AS pending_now,
           count(*) FILTER (WHERE d.cover_image_url IS NULL) AS new_empty,
           count(*) FILTER (WHERE d.cover_image_url LIKE $2 || '%') AS new_job_owned
      FROM shared.destinations d
     WHERE d.id = ANY($3::uuid[])
       AND NOT EXISTS (SELECT 1 FROM shared.v_destination_photos p WHERE p.destination_id = d.id)
"""


def _limit(payload: dict) -> int:
    raw = payload.get("limit", DEFAULT_LIMIT)
    if raw is None:
        return DEFAULT_LIMIT
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= MAX_LIMIT:
        raise NonRetryable(f"limit must be an integer from 1 to {MAX_LIMIT}")
    return raw


def _flag(payload: dict, name: str, default: bool) -> bool:
    raw = payload.get(name, default)
    if raw is None:
        return default
    if not isinstance(raw, bool):
        raise NonRetryable(f"{name} must be true or false")
    return raw


def _version(payload: dict) -> int:
    raw = payload.get("version", 1)
    if raw is None:
        return 1
    if isinstance(raw, bool) or raw not in VERSIONS:
        raise NonRetryable("version must be 1 or 2")
    return raw


def _dry_run(payload: dict) -> bool:
    raw = payload.get("dry_run", True)
    if not isinstance(raw, bool):
        raise NonRetryable("dry_run must be true or false")
    return raw


def plan(rows: list[dict], candidates: dict[str, list[tuple[str, str]]], place_info: Optional[dict] = None,
         nearby: Optional[dict] = None) -> dict[str, Any]:
    """Decide every open row. Pure. Returns {"writes": [(photo id, destination id, tour id, rule)], "ambiguous":
    [every tied row], "outcomes", "rules", "tie_breaks", "by_country", "samples"}. Version 2 passes `place_info` (tour
    id -> {destination id -> match.Place}) and `nearby` (label key -> [(destination id, name, Place)])."""
    from services.photos.match import label_keys, resolve_destination

    outcomes: Counter = Counter({o: 0 for o in OUTCOMES})
    rules: Counter = Counter()
    tie_breaks: Counter = Counter()
    by_country: dict[str, Counter] = {}
    samples: dict[str, list] = {"matched": [], "ambiguous": [], "weak": [], "no_match": []}
    writes: list[tuple[str, str, str, str]] = []
    ambiguous: list[dict] = []
    for r in rows:
        places = candidates.get(r["tour_id"], [])
        if place_info is None:
            found = resolve_destination(r["place_label"], places)
            near = []
        else:
            near = [n for k in sorted(label_keys(r["place_label"])) for n in (nearby or {}).get(k, [])]
            found = resolve_destination(r["place_label"], places, place_info.get(r["tour_id"], {}), near)
        outcomes[found.outcome] += 1
        by_country.setdefault(r["country"], Counter({o: 0 for o in OUTCOMES}))[found.outcome] += 1
        names = {}
        for did, name in list(places) + [(d, n) for d, n, _ in near]:
            names.setdefault(did, name)
        if found.outcome == "matched":
            rules[found.rule] += 1
            if found.tie_break:
                tie_breaks[found.tie_break] += 1
            writes.append((r["id"], found.destination_id, r["tour_id"], found.rule))
            item = {"photo_id": r["id"], "label": r["place_label"], "destination_id": found.destination_id,
                    "destination": names.get(found.destination_id), "rule": found.rule,
                    "match_source": r["match_source"]}
            if found.tie_break:
                item["tie_break"] = found.tie_break
                item["tied"] = [{"destination_id": d, "name": names.get(d)} for d in found.candidates]
            if found.km is not None:
                item["km"] = found.km
        elif found.outcome == "ambiguous":
            item = {"photo_id": r["id"], "label": r["place_label"], "rule": found.rule,
                    "candidates": [{"destination_id": d, "name": names.get(d)} for d in found.candidates]}
            ambiguous.append(item)
        elif found.outcome == "weak":    # never written: the label is only found inside this place's name
            item = {"photo_id": r["id"], "label": r["place_label"], "rule": found.rule,
                    "suggestion": {"destination_id": found.suggestion, "name": names.get(found.suggestion)},
                    "match_source": r["match_source"]}
        elif found.outcome == "no_match":
            item = {"photo_id": r["id"], "label": r["place_label"], "tour_places": sorted(set(names.values()))[:8]}
        else:
            item = None
        if item is not None and len(samples[found.outcome]) < MAX_SAMPLES:
            samples[found.outcome].append(item)
    return {"writes": writes, "ambiguous": ambiguous, "outcomes": dict(outcomes), "rules": dict(rules),
            "tie_breaks": dict(tie_breaks), "by_country": {c: dict(v) for c, v in sorted(by_country.items())},
            "samples": samples}


def coverage_sql() -> str:
    """The coverage statement over the pool tours (the base conditions of GET /v1/tours/pool, admin pool)."""
    from api.routers.v1_tours import _POOL_BASE_CONDITIONS
    from services.photos.covers import coverage_sql as build
    return build(_POOL_BASE_CONDITIONS)


async def _coverage(pool, job_id, planned_destinations) -> dict:
    """covers.summarise() over every pool tour; {"error": ...} when the read fails (a report, never a reason to stop).
    Reads raw_tours with the AA master pinned, like the candidate read (raw_tours is FORCE-RLS)."""
    from services.photos.covers import summarise
    from services.places.store import _catalog_fetch
    try:
        async with pool.acquire() as conn:
            rows = await _catalog_fetch(conn, coverage_sql())
    except Exception as exc:    # noqa: BLE001 — reported in the result and the log
        logger.warning("photo_destination_coverage_failed", job_id=job_id, error=type(exc).__name__)
        return {"error": type(exc).__name__}
    return summarise(rows, planned_destinations)


@job_kind(KIND, concurrency=1, max_attempts=2, expected_seconds=300, background=True)
async def run(ctx: JobContext) -> dict:
    from services.photos.destinations import nearby_by_key, tour_candidates, tour_places
    from services.photos.sync import fill_destination_covers, public_base

    payload = ctx.payload or {}
    dry_run = _dry_run(payload)
    fill_covers = _flag(payload, "fill_covers", False)
    limit = _limit(payload)
    version = _version(payload)
    country: Optional[str] = payload.get("country") or None
    if country is not None and not isinstance(country, str):
        raise NonRetryable("country must be a string")
    pool = ctx.pool
    res: dict[str, Any] = {"dry_run": dry_run, "country": country, "limit": limit, "fill_covers": fill_covers,
                           "version": version, "stopped": None}
    if not dry_run and await pool.fetchval(SYNC_RUNNING_SQL):
        res["stopped"] = "photo_sync_running"
        ctx.set_result(res)
        return res

    served = await pool.fetchrow(SERVED_SQL)
    res["served_before"] = {"photos": served["photos"], "destinations": served["destinations"]}
    res["open_before"] = int(await pool.fetchval(OPEN_COUNT_SQL, country))
    rows = [dict(r) for r in await pool.fetch(OPEN_ROWS_SQL, country, limit)]
    await ctx.progress(phase="matching", rows=len(rows), open_before=res["open_before"])
    places = nearby = None
    async with pool.acquire() as conn:
        candidates = await tour_candidates(conn, {r["tour_id"] for r in rows})
        if version == 2:
            places = await tour_places(conn, {r["tour_id"] for r in rows})
            nearby = await nearby_by_key(conn, [r["place_label"] for r in rows if r["place_label"]])
    decided = plan(rows, candidates, places, nearby)
    writes = decided.pop("writes")
    ambiguous = decided.pop("ambiguous")
    res.update(decided)
    res["planned"] = len(rows)
    res["tours"] = len({r["tour_id"] for r in rows})
    res["tours_without_places"] = len({r["tour_id"] for r in rows} - set(candidates))
    res["written"] = 0
    res["written_manual_rows"] = 0
    res["covers_set"] = 0
    base = public_base()
    preview = await pool.fetchrow(COVERS_PREVIEW_SQL, base, f"{base}/content/photos/",
                                  sorted({w[1] for w in writes}))
    res["covers_preview"] = {"pending_now": int(preview["pending_now"]), "new_empty": int(preview["new_empty"]),
                             "new_job_owned": int(preview["new_job_owned"])}
    res["covers_coverage"] = await _coverage(pool, ctx.job_id, sorted({w[1] for w in writes}))

    if not dry_run and writes:
        async with pool.acquire() as conn:
            done = {r["id"] for r in await conn.fetch(APPLY_SQL, [w[0] for w in writes], [w[1] for w in writes],
                                                      [w[2] for w in writes])}
            res["written"] = len(done)
        manual = {r["id"] for r in rows if r["match_source"] == "manual"}
        res["written_manual_rows"] = len(done & manual)
        for photo_id, dest_id, tour_id, rule in writes:
            if photo_id not in done:      # set or re-assigned by someone else since the plan: not ours
                continue
            logger.info("photo_destination_matched", job_id=ctx.job_id, photo_id=photo_id, destination_id=dest_id,
                        tour_id=tour_id, rule=rule)
    if not dry_run and fill_covers:
        async with pool.acquire() as conn:
            res["covers_set"] = await fill_destination_covers(conn)
    if not dry_run:
        for s in ambiguous:
            logger.info("photo_destination_ambiguous", job_id=ctx.job_id, photo_id=s["photo_id"], label=s["label"],
                        candidates=[c["destination_id"] for c in s["candidates"]])
    res["changed_meanwhile"] = len(writes) - res["written"] if not dry_run else 0
    served = await pool.fetchrow(SERVED_SQL)
    res["served_after"] = {"photos": served["photos"], "destinations": served["destinations"]}
    res["open_after"] = int(await pool.fetchval(OPEN_COUNT_SQL, country))
    if not dry_run:
        res["covers_coverage_after"] = await _coverage(pool, ctx.job_id, ())
    ctx.set_result(res)
    await ctx.progress(phase="done", planned=res["planned"], matched=res["outcomes"]["matched"],
                       written=res["written"])
    logger.info("photo_destination_match_done", job_id=ctx.job_id, dry_run=dry_run, country=country, version=version,
                planned=res["planned"], written=res["written"], covers_set=res["covers_set"], **res["outcomes"])
    return res
