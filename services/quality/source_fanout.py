"""AA-1052 W6 (root ADR 0020 layer 3 "lineage and versions") — the source-change fan-out ($0, no model call).

When a source a writer read changes, the stored writer objects written from it are marked ``source_changed`` in
``shared.source_change_mark`` (CIS 392) with a typed reason. Nothing is regenerated, no model is called, no object's
own status (tenant-visible or not) changes: the mark is an admin signal that the object may no longer match its
source, read by ``GET /admin/quality/lineage/fanout/{tour_id}``.

Sources and their dependents (lineage from stored ids only):

==========  ==========================================================  ==========================================
source      the change (the hook that enqueues the ``source_fanout``    dependents re-checked
            job; every hook is best-effort and never fails its write)
==========  ==========================================================  ==========================================
raw_tour    an admin edit of the raw tour (PATCH /admin/tours/{id}/raw,  S1 (the live Master version and the newest
            /country)                                                    version), T2 (each tenant's newest version
                                                                         of the tour), T9 (pieces citing its atoms)
master      a Master publish / promote (``process_export``: the new      T2 versions of an older Master of the tour,
            published_tours row; A3 re-atomizes after it)                T9 pieces citing its atoms
atom        an admin atom edit or soft delete (PATCH /admin/atoms/{id})  T9 pieces citing that atom
place_card  an admin edit of a card's facts / status, a card merge       T9 pieces citing an atom linked to the card
==========  ==========================================================  ==========================================

An object is a dependent only when it was written BEFORE the change (``created_at <= changed_at``): one written after
it already read the new source. A T9 piece cites an atom through its angle request (``angle_gate_request.atom_id``)
or its route moments (``writer_input.route_moments[].atom_id``); only stored pieces count (approved / held).

The re-check, per dependent (``recheck``): an S1 / T2 object of a raw-tour change that names a FactSheet (``fs_id``,
CIS 386) is compared with the tour FactSheet rebuilt from the raw tour NOW (``factsheet.build_tour_factsheet``, pure
and $0): the same rendered text means the writer read exactly these facts and the object is left unmarked
(``unchanged``); another text, or no stored sheet, marks it. Every other dependent is marked: its source is not a
FactSheet line set (a Master, an atom, a place card) or has no stored sheet.

A mark is unique per open (object, source kind, source ref): a later fan-out of the same source bumps
``last_seen_at`` / ``hits``. ``reason`` is the typed cause (``REASONS``); ``details`` carries ids and field names
only, never text.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

S1, T2, T9 = "s1", "t2", "t9"
OBJECT_KINDS = (S1, T2, T9)

RAW_TOUR, MASTER, ATOM, PLACE_CARD = "raw_tour", "master", "atom", "place_card"
SOURCE_KINDS = (RAW_TOUR, MASTER, ATOM, PLACE_CARD)

# The typed reasons (CHECK source_change_mark_reason_check, CIS 392) and the source kind each belongs to.
REASONS: dict[str, str] = {
    "raw_tour_edited": RAW_TOUR,
    "master_published": MASTER,
    "atom_edited": ATOM,
    "atom_deleted": ATOM,
    "place_card_facts_edited": PLACE_CARD,
    "place_card_status_changed": PLACE_CARD,
    "place_cards_merged": PLACE_CARD,
    "manual_recheck": "",          # an admin re-check of one tour (raw_tour or master); see admin_quality_lineage
}
MANUAL_SOURCES = (RAW_TOUR, MASTER)

# Which writer objects depend on each source kind.
DEPENDENTS: dict[str, tuple[str, ...]] = {
    RAW_TOUR: (S1, T2, T9),
    MASTER: (T2, T9),
    ATOM: (T9,),
    PLACE_CARD: (T9,),
}
# Sources whose scope is one tour (the payload names tour_id); atom / place_card name source_ref.
TOUR_SCOPED = (RAW_TOUR, MASTER)

KIND = "source_fanout"          # the job kind (services/jobs/source_fanout_job.py)
REF_MAX = 128
T9_STATUSES = ("approved", "held")
T2_SKIP_STATUSES = ("rejected", "failed")

# Re-check outcomes per dependent.
MARK, UNCHANGED = "mark", "unchanged"


class FanoutRefused(ValueError):
    """A fan-out request that cannot run (unknown source / reason, missing scope)."""


@dataclass(frozen=True)
class Change:
    """One source change: what changed, where, when and why."""
    source_kind: str
    reason: str
    changed_at: datetime
    tour_id: Optional[str] = None
    source_ref: str = ""
    fields: tuple[str, ...] = ()       # raw field names (raw_tour) — names only

    def validate(self) -> "Change":
        if self.source_kind not in SOURCE_KINDS:
            raise FanoutRefused(f"unknown source kind {self.source_kind!r}")
        if self.reason not in REASONS:
            raise FanoutRefused(f"unknown reason {self.reason!r}")
        if REASONS[self.reason] and REASONS[self.reason] != self.source_kind:
            raise FanoutRefused(f"reason {self.reason!r} is not a {self.source_kind} change")
        if self.reason == "manual_recheck" and self.source_kind not in MANUAL_SOURCES:
            raise FanoutRefused("a manual re-check names a tour (raw_tour or master)")
        if self.source_kind in TOUR_SCOPED and not self.tour_id:
            raise FanoutRefused(f"a {self.source_kind} change names its tour")
        if self.source_kind in (ATOM, PLACE_CARD) and not self.source_ref:
            raise FanoutRefused(f"a {self.source_kind} change names its {self.source_kind} id")
        if len(self.source_ref or "") > REF_MAX:
            raise FanoutRefused("source_ref is at most 128 characters")
        return self

    def payload(self) -> dict:
        return {"source_kind": self.source_kind, "reason": self.reason, "changed_at": self.changed_at.isoformat(),
                **({"tour_id": str(self.tour_id)} if self.tour_id else {}),
                **({"source_ref": self.source_ref} if self.source_ref else {}),
                **({"fields": sorted(self.fields)} if self.fields else {})}

    @classmethod
    def from_payload(cls, payload: dict) -> "Change":
        at = payload.get("changed_at")
        if not isinstance(at, str):
            raise FanoutRefused("changed_at is required")
        try:
            changed_at = datetime.fromisoformat(at)
        except ValueError as e:
            raise FanoutRefused(f"changed_at {at!r} is not an ISO time") from e
        fields = payload.get("fields") or ()
        return cls(source_kind=str(payload.get("source_kind") or ""), reason=str(payload.get("reason") or ""),
                   changed_at=changed_at, tour_id=payload.get("tour_id") or None,
                   source_ref=str(payload.get("source_ref") or ""),
                   fields=tuple(str(f)[:64] for f in fields if isinstance(f, str))[:50]).validate()


@dataclass
class Dependent:
    object_kind: str
    object_id: str
    tour_id: str
    object_created_at: datetime
    details: dict = field(default_factory=dict)
    fs_render: Optional[str] = None    # the stored FactSheet text (S1 / T2 with fs_id); never written to a mark


# ── SQL (table and column names: root docs/architecture/db-schema-reference.md) ─────────────────────────────────────

# S1: the version the live Master was published from, and the newest version, of the tour; written before $2.
S1_SQL = """
    SELECT 's1' AS object_kind, gc.id::text AS object_id, gc.tour_id::text AS tour_id,
           gc.created_at AS object_created_at, gc.version_num, gc.fs_id::text AS fs_id,
           fs.render_text AS fs_render,
           EXISTS (SELECT 1 FROM gold_aa_internal.published_tours pt
                    WHERE pt.generated_content_id = gc.id AND pt.deleted_at IS NULL) AS live
      FROM silver_aa_internal.generated_content gc
      LEFT JOIN acp_shared.fact_sheet fs ON fs.fs_id = gc.fs_id
     WHERE gc.tour_id = $1::uuid AND gc.created_at <= $2::timestamptz
       AND (gc.id IN (SELECT pt.generated_content_id FROM gold_aa_internal.published_tours pt
                       WHERE pt.tour_id = $1::uuid AND pt.deleted_at IS NULL)
            OR gc.version_num = (SELECT max(g2.version_num) FROM silver_aa_internal.generated_content g2
                                  WHERE g2.tour_id = $1::uuid))
     ORDER BY gc.version_num"""

# T2: each tenant's newest version per Master of the tour ($3 = a published_tours id to leave out: the new Master of a
# publish; '' = none), not rejected / failed, written before $2.
T2_SQL = """
    SELECT * FROM (
        SELECT DISTINCT ON (ttv.tenant_id, ttv.published_tour_id)
               't2' AS object_kind, ttv.id::text AS object_id, pt.tour_id::text AS tour_id,
               ttv.created_at AS object_created_at, ttv.version_number, ttv.status,
               ttv.published_tour_id::text AS published_tour_id, ttv.fs_id::text AS fs_id,
               fs.render_text AS fs_render
          FROM gold_aa_internal.tenant_tour_versions ttv
          JOIN gold_aa_internal.published_tours pt ON pt.id = ttv.published_tour_id
          LEFT JOIN acp_shared.fact_sheet fs ON fs.fs_id = ttv.fs_id
         WHERE pt.tour_id = $1::uuid AND ($3::text = '' OR pt.id::text <> $3::text)
         ORDER BY ttv.tenant_id, ttv.published_tour_id, ttv.version_number DESC
    ) v
     WHERE v.object_created_at <= $2::timestamptz AND v.status <> ALL($4::text[])
     ORDER BY v.object_created_at, v.object_id"""

# T9: the stored pieces citing an atom of the set: the tour's atoms ($1, NULL = none) or the named atom ids ($2), by
# the angle request's atom or a route moment; written before $3. One row per piece, with the atoms it cites.
T9_SQL = """
    WITH atoms AS (
        SELECT ta.atom_id, ta.tour_id
          FROM acp_contract.tour_atoms ta
         WHERE ($1::uuid IS NOT NULL AND ta.tour_id = $1::uuid) OR ta.atom_id = ANY($2::text[])
    ), cited AS (
        SELECT cp.piece_id, a.atom_id, a.tour_id
          FROM acp_shared.content_piece cp
          JOIN acp_shared.angle_gate_request r ON r.request_id = cp.angle_gate_request_id
          JOIN atoms a ON a.atom_id = r.atom_id
         WHERE cp.status = ANY($4::text[]) AND cp.created_at <= $3::timestamptz
        UNION
        SELECT cp.piece_id, a.atom_id, a.tour_id
          FROM acp_shared.content_piece cp
          CROSS JOIN LATERAL jsonb_array_elements(
              CASE WHEN jsonb_typeof(cp.writer_input -> 'route_moments') = 'array'
                   THEN cp.writer_input -> 'route_moments' ELSE '[]'::jsonb END) m
          JOIN atoms a ON a.atom_id = m ->> 'atom_id'
         WHERE cp.status = ANY($4::text[]) AND cp.created_at <= $3::timestamptz
    )
    SELECT 't9' AS object_kind, cp.piece_id::text AS object_id,
           (array_agg(c.tour_id::text ORDER BY c.tour_id::text))[1] AS tour_id,
           cp.created_at AS object_created_at, cp.status,
           NULLIF(cp.writer_input ->> 'fs_id', '') AS fs_id,
           array_agg(DISTINCT c.atom_id ORDER BY c.atom_id) AS atom_ids
      FROM cited c
      JOIN acp_shared.content_piece cp ON cp.piece_id = c.piece_id
     GROUP BY cp.piece_id, cp.created_at, cp.status, cp.writer_input
     ORDER BY cp.created_at, cp.piece_id"""

# The atoms linked to a place card (its links, CIS atom_place_card).
CARD_ATOMS_SQL = "SELECT atom_id FROM acp_shared.atom_place_card WHERE card_id = $1::uuid ORDER BY atom_id"

# The raw tour row the tour FactSheet is built from (the columns factsheet.raw_source_tour reads).
RAW_ROW_SQL = """
    SELECT tour_id, src_name, src_subtitle, src_summary, src_description, src_highlights, src_itineraries, country,
           duration, price_raw, inclusions, exclusions, group_size, best_time_to_go
      FROM silver_aa_internal.raw_tours
     WHERE tour_id = $1::uuid"""

UPSERT_SQL = """
    INSERT INTO shared.source_change_mark AS m
        (object_kind, object_id, tour_id, source_kind, source_ref, reason, changed_at, object_created_at, job_id,
         details)
    VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7::timestamptz, $8::timestamptz, $9::uuid, $10::jsonb)
    ON CONFLICT (object_kind, object_id, source_kind, source_ref) WHERE cleared_at IS NULL
    DO UPDATE SET last_seen_at = now(), hits = m.hits + 1, reason = EXCLUDED.reason, job_id = EXCLUDED.job_id,
                  changed_at = GREATEST(m.changed_at, EXCLUDED.changed_at), details = EXCLUDED.details
    RETURNING (xmax = 0) AS inserted, mark_id::text AS mark_id"""

# The open and the recent cleared marks of one tour (the admin read; ids and reasons only).
MARKS_SQL = """
    SELECT mark_id::text AS mark_id, object_kind, object_id::text AS object_id, tour_id::text AS tour_id,
           source_kind, source_ref, reason, changed_at, object_created_at, detected_at, last_seen_at, hits,
           job_id::text AS job_id, details, cleared_at, cleared_by
      FROM shared.source_change_mark
     WHERE tour_id = $1::uuid AND (cleared_at IS NULL OR cleared_at >= now() - interval '30 days')
     ORDER BY cleared_at IS NOT NULL, detected_at DESC, object_kind, object_id
     LIMIT $2"""
MARKS_LIMIT = 500


# ── pure helpers ────────────────────────────────────────────────────────────────────────────────────────────────────

def current_tour_render(raw_row) -> Optional[str]:
    """The tour FactSheet text the writer would read from this raw row now (pure, $0), or None (no row / no name)."""
    if raw_row is None:
        return None
    from services.quality.factsheet import build_tour_factsheet, raw_source_tour
    tour, agency = raw_source_tour(raw_row)
    if not (tour.get("name") or "").strip():
        return None
    return build_tour_factsheet(tour, agency=agency).render()


def recheck(dep: Dependent, change: Change, current_render: Optional[str]) -> str:
    """MARK or UNCHANGED for one dependent (pure). Only an S1 / T2 object of a raw-tour change that names a stored
    FactSheet can be cleared: its stored text equal to the text rebuilt now means the edit did not touch what it
    read."""
    if change.source_kind != RAW_TOUR or dep.object_kind not in (S1, T2):
        return MARK
    if dep.fs_render is None or current_render is None:
        return MARK
    return UNCHANGED if dep.fs_render == current_render else MARK


def _row(r) -> dict:
    return dict(r) if not isinstance(r, dict) else r


def dependent_from_row(r) -> Dependent:
    """A Dependent from one S1 / T2 / T9 row (pure): ids and numbers into ``details``, never text."""
    r = _row(r)
    kind = r["object_kind"]
    details: dict[str, Any] = {}
    if r.get("fs_id"):
        details["fs_id"] = r["fs_id"]
    if kind == S1:
        details.update(version_num=r.get("version_num"), live=bool(r.get("live")))
    elif kind == T2:
        details.update(version_number=r.get("version_number"), published_tour_id=r.get("published_tour_id"))
    else:
        details["atom_ids"] = list(r.get("atom_ids") or [])[:50]
    return Dependent(object_kind=kind, object_id=str(r["object_id"]), tour_id=str(r["tour_id"]),
                     object_created_at=r["object_created_at"], details=details, fs_render=r.get("fs_render"))


def mark_args(dep: Dependent, change: Change, job_id: Optional[str]) -> tuple:
    """The UPSERT arguments of one mark (pure; the unit test pins the order)."""
    details = dict(dep.details)
    if change.fields:
        details["fields"] = sorted(change.fields)
    return (dep.object_kind, dep.object_id, dep.tour_id, change.source_kind, change.source_ref or "", change.reason,
            change.changed_at, dep.object_created_at, job_id, json.dumps(details, default=str, sort_keys=True))


def view_mark(r) -> dict:
    """One mark for the admin read: ids, kinds, reasons, times (iso)."""
    r = _row(r)
    out = {}
    for k, v in r.items():
        if k == "details" and isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                v = {}
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    return out


# ── the fan-out ─────────────────────────────────────────────────────────────────────────────────────────────────────

async def _atom_scope(conn, change: Change) -> tuple[Optional[str], list[str]]:
    """(tour_id or None, atom ids) of the T9 query."""
    if change.source_kind in TOUR_SCOPED:
        return str(change.tour_id), []
    if change.source_kind == ATOM:
        return None, [change.source_ref]
    rows = await conn.fetch(CARD_ATOMS_SQL, change.source_ref)
    return None, [r["atom_id"] for r in rows]


async def dependents(conn, change: Change) -> list[Dependent]:
    """Every writer object that read the changed source and was written before the change (read-only)."""
    kinds = DEPENDENTS[change.source_kind]
    out: list[Dependent] = []
    if S1 in kinds:
        out += [dependent_from_row(r) for r in await conn.fetch(S1_SQL, str(change.tour_id), change.changed_at)]
    if T2 in kinds:
        skip_pt = change.source_ref if change.source_kind == MASTER else ""
        out += [dependent_from_row(r) for r in await conn.fetch(
            T2_SQL, str(change.tour_id), change.changed_at, skip_pt or "", list(T2_SKIP_STATUSES))]
    if T9 in kinds:
        tour_id, atom_ids = await _atom_scope(conn, change)
        if tour_id or atom_ids:
            out += [dependent_from_row(r) for r in await conn.fetch(
                T9_SQL, tour_id, atom_ids, change.changed_at, list(T9_STATUSES))]
    return out


async def fan_out(conn, change: Change, *, job_id: Optional[str] = None, dry_run: bool = False) -> dict:
    """Find the dependents of one change, re-check each ($0) and mark the ones whose source moved. One transaction
    (a retried job starts over). ``dry_run`` reads and re-checks, writes nothing. Returns counts and ids, no text."""
    change.validate()
    current = None
    async with conn.transaction():
        deps = await dependents(conn, change)
        if change.source_kind == RAW_TOUR and any(d.object_kind in (S1, T2) for d in deps):
            current = current_tour_render(await conn.fetchrow(RAW_ROW_SQL, str(change.tour_id)))
        marked, bumped, unchanged = [], [], []
        for dep in deps:
            if recheck(dep, change, current) == UNCHANGED:
                unchanged.append({"object_kind": dep.object_kind, "object_id": dep.object_id})
                continue
            item = {"object_kind": dep.object_kind, "object_id": dep.object_id}
            if dry_run:
                marked.append(item)
                continue
            row = await conn.fetchrow(UPSERT_SQL, *mark_args(dep, change, job_id))
            (marked if row["inserted"] else bumped).append({**item, "mark_id": row["mark_id"]})
    by_kind: dict[str, int] = {}
    for item in marked + bumped:
        by_kind[item["object_kind"]] = by_kind.get(item["object_kind"], 0) + 1
    return {"source_kind": change.source_kind, "reason": change.reason, "tour_id": change.tour_id,
            "source_ref": change.source_ref or None, "changed_at": change.changed_at.isoformat(),
            "dry_run": dry_run, "dependents": len(deps), "marked": marked, "bumped": bumped,
            "unchanged": unchanged, "by_kind": dict(sorted(by_kind.items())), "llm_calls": 0}


async def read_tour(conn, tour_id: str, *, limit: int = MARKS_LIMIT) -> dict:
    """The fan-out of one tour for the admin read: its open (and the last 30 days of cleared) marks, counted per
    object kind and reason. Ids and reasons only."""
    rows = [view_mark(r) for r in await conn.fetch(MARKS_SQL, str(tour_id), int(limit))]
    open_rows = [r for r in rows if r.get("cleared_at") is None]
    count = lambda key: dict(sorted(_tally(r[key] for r in open_rows).items()))  # noqa: E731
    return {"tour_id": str(tour_id), "open": len(open_rows), "cleared_recent": len(rows) - len(open_rows),
            "by_object_kind": count("object_kind"), "by_reason": count("reason"), "marks": rows}


def _tally(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


# ── enqueue (the hooks) ─────────────────────────────────────────────────────────────────────────────────────────────

def idempotency_key(change: Change) -> str:
    """One queued job per source and minute: a burst of edits of one tour / atom / card folds into one fan-out (the
    job reads every dependent written before its own changed_at, the last edit of the minute wins)."""
    scope = change.source_ref or str(change.tour_id)
    return f"{KIND}:{change.source_kind}:{scope}:{change.changed_at.strftime('%Y-%m-%dT%H:%M')}"


async def enqueue(pool, change: Change, *, created_by: str = "system") -> Optional[str]:
    """Queue the fan-out of one change; the job id (None when a job of the same minute exists). Raises on a bad
    change; the caller's hook (``enqueue_safe``) never lets that fail its own write."""
    from shared.jobs import queue
    change.validate()
    job_id, created = await queue.enqueue(pool, KIND, change.payload(), idempotency_key=idempotency_key(change),
                                          max_attempts=3, created_by=created_by[:128])
    return job_id if created else None


async def enqueue_safe(pool, change: Change, *, created_by: str = "system") -> dict:
    """The hook form: never raises (a fan-out is never a reason to fail the write that changed the source)."""
    import structlog
    try:
        job_id = await enqueue(pool, change, created_by=created_by)
        return {"job_id": job_id}
    except Exception as e:  # noqa: BLE001
        structlog.get_logger().warning("source_fanout_enqueue_failed", source_kind=change.source_kind,
                                       reason=change.reason, tour_id=str(change.tour_id or ""),
                                       source_ref=change.source_ref, error=str(e)[:200])
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


__all__ = ["ATOM", "Change", "DEPENDENTS", "Dependent", "FanoutRefused", "KIND", "MASTER", "OBJECT_KINDS",
           "PLACE_CARD", "RAW_TOUR", "REASONS", "SOURCE_KINDS", "current_tour_render", "dependent_from_row",
           "dependents", "enqueue", "enqueue_safe", "fan_out", "idempotency_key", "mark_args", "read_tour",
           "recheck", "view_mark"]
