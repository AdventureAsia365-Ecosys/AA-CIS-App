"""AA-1057 GAP 2 (and the GAP 1 re-seed): keep tenant Subjects and angle-gate requests on live ids.

A rewrite or a supersede replaces ids under the tenant's work:

* Route detection (``route_detection.run_route_detection``) supersedes a Route and writes the next version under a
  new ``route_id`` (``…:v<n>``); a picked / used Route Subject (``acp_shared.subject.route_id``) kept the old id, so
  the Slate showed it on a superseded Route and a regenerate refused it (S273 audit: 7 used Subjects).
* An atomize run soft-deletes the atoms it does not reproduce (``a3_atomize``); a request on such an atom answers 409
  ``atom_replaced`` at T8 / T9 (S273 audit: 3 requests, 2 approved, 1 ``pending_choice`` stuck).
* A Segment Subject's request is planned on ONE representative member (its seed atom and that atom's tour as
  ``trip_id``). When that tour retires while the Segment still has a served member (GAP 1), the Subject is alive
  (shared.tour_liveness ``requests_about_tours_sql``) but its seed atom is not.

This module plans and applies the re-pointing. It runs as the ``retarget`` recompute stage after ``route`` (tour and
platform passes: every rewrite, supersede, retire and restore reaches a recompute), and the one-off
``scripts/aa1057_retarget_once.py`` runs the same plan with a DRY RUN first. Rules, in order (no guessing: a tie
leaves the row as it is and the plan says why):

Route Subjects (``state`` picked / used only: a proposed one is replaced by the next proposal) on a superseded Route:
  1. ``identity`` — the current Route of the same tour family (``raw_tours.source_group_id``) with the same day span
     (the Route identity ``(tour_id, first_day, last_day)``, across tour versions);
  2. ``overlap`` — else the current Route of the family sharing the most Segment ids (Jaccard > 0, one best);
  a target the tenant already holds a Subject on (same channel; unique index ``idx_subject_unique_route``) is
  ``taken``: the old Subject stays where it is.
Angle-gate requests whose atom is not a live atom of the platform or the tenant:
  1. ``repoint`` — the unique live atom of the same place in the same tour family (``atom_repoint.plan_repoints``,
     the AA-990 rule; atom id only);
  2. ``reseed`` — else, for a request on a Segment Subject whose Segment still has a seed member for the tenant, the
     member a pick would choose today (``slate._resolve_representative_atom``: atom, ``trip_id`` and, for Blog, the
     walk), unless the old atom's tour is paused (a pause or a supersede waiting for its Master comes back on the
     same tour; moving the request away would be permanent).
Every write is guarded on the value the plan read, so a concurrent pick or write is never overwritten.

Mode: env ``RECOMPUTE_RETARGET_MODE`` = ``report`` (default: plan only, counts in the job result) | ``apply`` |
``off``. The default stays ``report`` until the audited one-off has moved the rows the S273 audit listed (DRY RUN,
then an apply pinned to the reviewed ids); a follow-up change makes ``apply`` the default. An ``apply`` run puts the
restore list (``restore_rows``: ids and the old value of every row it changed, at most ``RESTORE_LOG_CAP`` per kind) in
its result and log. Ids only in results and logs, never tenant text.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from uuid import UUID

import asyncpg
import structlog

from services.acp_shared import atom_repoint as ar
from shared.tour_liveness import tour_served_sql

logger = structlog.get_logger()

MODE_ENV = "RECOMPUTE_RETARGET_MODE"
MODES = ("apply", "report", "off")
DEFAULT_MODE = "report"
RESTORE_LOG_CAP = 200

SUBJECT_OUTCOMES = ("retarget", "taken", "ambiguous", "no_match")
REQUEST_OUTCOMES = ("repoint", "reseed", "ambiguous", "no_match", "no_atom", "paused")
RETARGET_STATES = ("picked", "used")


def mode() -> str:
    m = (os.environ.get(MODE_ENV) or DEFAULT_MODE).strip().lower()
    return m if m in MODES else DEFAULT_MODE


# ── reads ────────────────────────────────────────────────────────────────────────────────────────────────────────────

# Picked / used Route Subjects on a superseded Route, with the Route's family (every version of its tour).
STALE_ROUTE_SUBJECTS_SQL = """
    SELECT s.subject_id::text AS subject_id, s.tenant_id::text AS tenant_id, s.channel, s.state,
           s.route_id, r.tour_id::text AS tour_id, r.first_day, r.last_day, r.ordered_segment_ids,
           COALESCE(rt.source_group_id, rt.tour_id, r.tour_id)::text AS source_id
      FROM acp_shared.subject s
      JOIN acp_contract.route r ON r.route_id = s.route_id
      LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = r.tour_id
     WHERE s.route_id IS NOT NULL AND s.state = ANY($1::text[]) AND r.superseded_at IS NOT NULL
     ORDER BY s.subject_id
"""

# Current Routes of the given families, on a served tour.
CURRENT_ROUTES_SQL = f"""
    SELECT r.route_id, r.tour_id::text AS tour_id, r.first_day, r.last_day, r.ordered_segment_ids,
           COALESCE(rt.source_group_id, rt.tour_id, r.tour_id)::text AS source_id
      FROM acp_contract.route r
      LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = r.tour_id
     WHERE r.superseded_at IS NULL
       AND COALESCE(rt.source_group_id, rt.tour_id, r.tour_id)::text = ANY($1::text[])
       AND {tour_served_sql('r.tour_id')}
"""

# The (tenant, channel) pairs that already hold a Subject on the given Routes.
TAKEN_ROUTES_SQL = """
    SELECT s.tenant_id::text AS tenant_id, s.channel, s.route_id
      FROM acp_shared.subject s
     WHERE s.route_id = ANY($1::text[])
"""

# Requests whose atom is not a live platform / own atom (the atom_replaced rule), with the request's Segment Subject
# and the served state of the old atom's tour family (NULL: never served or unknown).
STALE_REQUESTS_SQL = """
    SELECT r.request_id::text AS request_id, r.tenant_id::text AS tenant_id, r.atom_id, r.status, r.channel,
           r.trip_id::text AS trip_id, r.route_segment_ids, (r.route_segment_ids IS NOT NULL) AS is_route,
           s.segment_id, tl.state AS key_state
      FROM acp_shared.angle_gate_request r
      LEFT JOIN acp_shared.subject s ON s.subject_id = r.subject_id AND s.tenant_id = r.tenant_id
      LEFT JOIN acp_contract.tour_atoms ta ON ta.atom_id = r.atom_id
      LEFT JOIN silver_aa_internal.raw_tours rt ON rt.tour_id = COALESCE(ta.tour_id, r.trip_id)
      LEFT JOIN gold_aa_internal.v_tour_liveness tl
        ON tl.source_id = (COALESCE(rt.source_group_id, rt.tour_id))::text
     WHERE NOT EXISTS (SELECT 1 FROM acp_contract.v_active_tour_atoms a
                        WHERE a.atom_id = r.atom_id AND a.owner_scope IN ('platform', r.tenant_id::text)
                          AND NOT a.deleted AND NOT a.is_empty_marker)
     ORDER BY r.request_id
"""

# ── writes (guarded on what the plan read) ───────────────────────────────────────────────────────────────────────────

RETARGET_SUBJECT_SQL = """
    UPDATE acp_shared.subject s SET route_id = $3
     WHERE s.subject_id = $1::uuid AND s.route_id = $2
       AND NOT EXISTS (SELECT 1 FROM acp_shared.subject o
                        WHERE o.tenant_id = s.tenant_id AND o.channel = s.channel AND o.route_id = $3)
"""

REPOINT_REQUEST_SQL = ar.REPOINT_SQL

RESEED_REQUEST_SQL = """
    UPDATE acp_shared.angle_gate_request
       SET atom_id = $3, trip_id = $4::uuid, route_segment_ids = $5::jsonb, updated_at = now()
     WHERE request_id = $1::uuid AND atom_id = $2
"""


# ── pure planning ────────────────────────────────────────────────────────────────────────────────────────────────────

def _segments(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return [str(s) for s in raw] if isinstance(raw, list) else []


def _walk(raw: Any) -> Any:
    """A stored ``route_segment_ids`` value as JSON data (asyncpg returns jsonb as text): None stays None."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def plan_route_subjects(subjects: list[dict], current: list[dict], taken: Iterable[tuple]) -> list[dict]:
    """One decision per stale Route Subject: {subject_id, tenant_id, channel, state, old_route_id, new_route_id,
    outcome, rule}. ``subjects``: STALE_ROUTE_SUBJECTS_SQL rows; ``current``: CURRENT_ROUTES_SQL rows; ``taken``:
    (tenant_id, channel, route_id) already held. Deterministic (sorted by subject id)."""
    by_source: dict[str, list[dict]] = {}
    for c in current:
        by_source.setdefault(str(c["source_id"]), []).append(c)
    held = {(str(t), str(ch), str(r)) for t, ch, r in taken}
    out = []
    for s in sorted(subjects, key=lambda x: str(x["subject_id"])):
        d = {"subject_id": str(s["subject_id"]), "tenant_id": str(s["tenant_id"]), "channel": s.get("channel"),
             "state": s.get("state"), "old_route_id": s["route_id"], "new_route_id": None, "outcome": "no_match",
             "rule": None}
        family = by_source.get(str(s["source_id"]), [])
        target, rule = None, None
        same_span = [c for c in family if c["first_day"] == s["first_day"] and c["last_day"] == s["last_day"]]
        if len(same_span) == 1:
            target, rule = same_span[0], "identity"
        elif len(same_span) > 1:
            d["outcome"] = "ambiguous"          # two tour versions with a current Route of that span: never guess
        else:
            old_segments = _segments(s.get("ordered_segment_ids"))
            scored = sorted(((_jaccard(old_segments, _segments(c["ordered_segment_ids"])), c) for c in family),
                            key=lambda x: (-x[0], str(x[1]["route_id"])))
            if scored and scored[0][0] > 0:
                if len(scored) > 1 and scored[1][0] == scored[0][0]:
                    d["outcome"] = "ambiguous"
                else:
                    target, rule = scored[0][1], "overlap"
        if target is not None:
            key = (d["tenant_id"], str(d["channel"]), str(target["route_id"]))
            d.update(new_route_id=target["route_id"], rule=rule)
            if key in held:
                d["outcome"] = "taken"
            else:
                d["outcome"] = "retarget"
                held.add(key)               # a second stale Subject of the same tenant + channel cannot take it too
        out.append(d)
    return out


def plan_requests(requests: list[dict], old_atoms: dict[str, dict], candidates: list[dict]) -> list[dict]:
    """The AA-990 re-point decision per stale request, plus whether a Segment re-seed may follow:
    {request_id, tenant_id, status, old_atom_id, new_atom_id, outcome, rule, segment_id, channel, may_reseed,
    old_trip_id, old_route_segment_ids}. ``may_reseed``: not re-pointed, on a Segment Subject, and the old atom's tour
    family is not paused. ``old_trip_id`` / ``old_route_segment_ids``: what a re-seed overwrites (restore list)."""
    meta = {str(r["request_id"]): r for r in requests}
    out = []
    for d in ar.plan_repoints(requests, old_atoms, candidates):
        if d["outcome"] == "already_live":
            continue
        r = meta[d["request_id"]]
        d["segment_id"] = r.get("segment_id")
        d["channel"] = r.get("channel")
        d["old_trip_id"] = str(r["trip_id"]) if r.get("trip_id") else None
        d["old_route_segment_ids"] = _walk(r.get("route_segment_ids"))
        d["may_reseed"] = (d["outcome"] != "repoint" and bool(r.get("segment_id"))
                           and r.get("key_state") != "paused")
        if d["outcome"] != "repoint" and r.get("segment_id") and r.get("key_state") == "paused":
            d["outcome"] = "paused"
        out.append(d)
    return out


def _counts(decisions: list[dict], outcomes: tuple) -> dict:
    out = {k: 0 for k in outcomes}
    for d in decisions:
        out[d["outcome"]] = out.get(d["outcome"], 0) + 1
    return out


def fingerprint(ids: Iterable[str]) -> str:
    """A short pin of a set of ids (sha256 of the sorted ids): the DRY RUN and the apply must see the same set."""
    joined = ",".join(sorted(str(i) for i in ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


@dataclass
class Plan:
    subjects: list[dict] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)

    @property
    def subject_changes(self) -> list[dict]:
        return [d for d in self.subjects if d["outcome"] == "retarget"]

    @property
    def request_changes(self) -> list[dict]:
        return [d for d in self.requests if d["outcome"] in ("repoint", "reseed")]

    def summary(self) -> dict:
        return {
            "subjects": {"stale": len(self.subjects), **_counts(self.subjects, SUBJECT_OUTCOMES),
                         "pin": fingerprint(d["subject_id"] for d in self.subject_changes)},
            "requests": {"stale": len(self.requests), **_counts(self.requests, REQUEST_OUTCOMES),
                         "pin": fingerprint(d["request_id"] for d in self.request_changes)},
        }


# ── DB plan / apply ──────────────────────────────────────────────────────────────────────────────────────────────────

async def _plan_subjects(conn) -> list[dict]:
    subjects = [dict(r) for r in await conn.fetch(STALE_ROUTE_SUBJECTS_SQL, list(RETARGET_STATES))]
    if not subjects:
        return []
    sources = sorted({str(s["source_id"]) for s in subjects})
    current = [dict(r) for r in await conn.fetch(CURRENT_ROUTES_SQL, sources)]
    taken = [(r["tenant_id"], r["channel"], r["route_id"]) for r in await conn.fetch(
        TAKEN_ROUTES_SQL, sorted({c["route_id"] for c in current}))] if current else []
    return plan_route_subjects(subjects, current, taken)


async def _reseed(conn, d: dict) -> None:
    """Fill a ``may_reseed`` decision with the member a pick would choose today (outcome ``reseed``), if any."""
    from services.acp_contract.segment_matching import resolve_segment_aliases
    from services.acp_shared.slate import _resolve_representative_atom

    live = (await resolve_segment_aliases(conn, [d["segment_id"]])).get(d["segment_id"], d["segment_id"])
    atom, trip, walk = await _resolve_representative_atom(conn, UUID(d["tenant_id"]), live, None, d["channel"])
    if atom and atom != d["old_atom_id"]:
        d.update(new_atom_id=atom, new_trip_id=str(trip) if trip else None, new_route_segment_ids=walk,
                 outcome="reseed",
                 rule="segment_seed")


async def _plan_requests(conn) -> list[dict]:
    stale = [dict(r) for r in await conn.fetch(STALE_REQUESTS_SQL)]
    if not stale:
        return []
    with_atom = [s for s in stale if s.get("atom_id")]
    atom_ids = sorted({s["atom_id"] for s in with_atom})
    old = {r["atom_id"]: dict(r) for r in await conn.fetch(ar.OLD_ATOMS_SQL, atom_ids)}
    sources = sorted({str(o["source_id"]) for o in old.values()})
    cands = [dict(r) for r in await conn.fetch(ar.CANDIDATES_SQL, sources)] if sources else []
    decisions = plan_requests(with_atom, old, cands)
    for d in decisions:
        if d.get("may_reseed"):
            await _reseed(conn, d)
    return decisions


async def build_plan(conn) -> Plan:
    """Read-only: what a retarget would change now."""
    return Plan(subjects=await _plan_subjects(conn), requests=await _plan_requests(conn))


async def apply_plan(conn, plan: Plan) -> dict:
    """Apply the plan's changes on ``conn`` (the caller owns the transaction). Returns rows changed per kind; a row
    whose value moved since the plan (or whose target another Subject took meanwhile) is skipped."""
    subjects = requests = 0
    for d in plan.subject_changes:
        try:
            async with conn.transaction():          # a savepoint: a unique race on one row never aborts the rest
                status = await conn.execute(RETARGET_SUBJECT_SQL, d["subject_id"], d["old_route_id"],
                                            d["new_route_id"])
        except asyncpg.UniqueViolationError:
            continue
        subjects += _rowcount(status)
    for d in plan.request_changes:
        if d["outcome"] == "repoint":
            status = await conn.execute(REPOINT_REQUEST_SQL, d["request_id"], d["old_atom_id"], d["new_atom_id"])
        else:
            walk = d.get("new_route_segment_ids")
            status = await conn.execute(RESEED_REQUEST_SQL, d["request_id"], d["old_atom_id"], d["new_atom_id"],
                                        d.get("new_trip_id"), json.dumps(walk) if walk is not None else None)
        requests += _rowcount(status)
    return {"subjects": subjects, "requests": requests}


def _rowcount(status) -> int:
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


def _request_restore(d: dict) -> dict:
    row = {"request_id": d["request_id"], "atom_id": d["old_atom_id"], "new_atom_id": d["new_atom_id"],
           "outcome": d["outcome"]}
    if d["outcome"] == "reseed":    # RESEED_REQUEST_SQL also overwrites trip_id and route_segment_ids
        row.update(trip_id=d.get("old_trip_id"), route_segment_ids=d.get("old_route_segment_ids"),
                   new_trip_id=d.get("new_trip_id"), new_route_segment_ids=d.get("new_route_segment_ids"))
    return row


def restore_rows(plan: Plan) -> dict:
    """The rows an apply changes and the old value of every column it writes (ids only), to put a row back by hand:
    a Subject's ``route_id``; a request's ``atom_id``, and for a re-seed also its ``trip_id`` and
    ``route_segment_ids`` (the Blog walk, JSON or None)."""
    return {
        "subjects": [{"subject_id": d["subject_id"], "route_id": d["old_route_id"], "new_route_id": d["new_route_id"]}
                     for d in plan.subject_changes],
        "requests": [_request_restore(d) for d in plan.request_changes],
    }


def _capped_restore(plan: Plan) -> dict:
    rows = restore_rows(plan)
    out: dict[str, Any] = {k: v[:RESTORE_LOG_CAP] for k, v in rows.items()}
    out["truncated"] = any(len(v) > RESTORE_LOG_CAP for v in rows.values())
    return out


async def run_retarget(pool, run_mode: Optional[str] = None) -> dict:
    """The ``retarget`` recompute stage: plan, and in ``apply`` mode write the changes in one transaction. A run that
    writes returns (and logs) the restore list of the rows it planned to change (``restore``)."""
    m = run_mode or mode()
    if m == "off":
        return {"mode": m}
    async with pool.acquire() as conn:
        plan = await build_plan(conn)
        changed = {"subjects": 0, "requests": 0}
        writes = m == "apply" and bool(plan.subject_changes or plan.request_changes)
        if writes:
            async with conn.transaction():
                changed = await apply_plan(conn, plan)
    out = {"mode": m, **plan.summary(), "changed": changed}
    if writes:
        out["restore"] = _capped_restore(plan)
    if plan.subjects or plan.requests:
        logger.info("recompute_retarget", **out)
    return out


__all__ = ["DEFAULT_MODE", "MODE_ENV", "MODES", "Plan", "RESTORE_LOG_CAP", "apply_plan", "build_plan", "fingerprint",
           "mode", "plan_requests", "plan_route_subjects", "restore_rows", "run_retarget"]
