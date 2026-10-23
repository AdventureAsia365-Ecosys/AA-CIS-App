"""AA-986 L2: the one CIS write service for a tour's served state (ADR 0015 decision 2).

Every change to `raw_tours.source_status / deleted_at`, `published_tours.master_status / deleted_at` and every INSERT
into `raw_tours` / `published_tours` goes through this module (tests/unit/test_aa986_l2_lifecycle_guard.py fails the
build otherwise). The actions:

  retire_source    trash a source version at S0/A0 (PATCH /admin/tours/{id}/trash, DELETE /admin/raw-tours/{id})
  restore_source   bring a trashed version back (PATCH /admin/tours/{id}/restore, staging "Restore")
  supersede        a new version replaces the live one (staging replace / update), or is kept beside it (keep_both),
                   or starts a new tour (staging "Create a new tour")
  set_active       make an older version the live one (POST /admin/raw-tours/{id}/set-active)
  pause_master / activate_master / trash_master / restore_master   the Master Content lifecycle buttons
  publish          the Master insert of process_export

In ONE transaction each action: changes the status columns and records why (published_tours.master_status_reason,
CIS 272); switches off every active Master of the key whose source is not live (never two active Masters per key);
dismisses pending reviews of dead versions (all pending reviews of the key when its Master is trashed); writes the
lifecycle event when the derived state of the key (gold_aa_internal.v_tour_liveness) changed; cancels queued T2 / T8 /
T9 jobs of a retired or paused key; notifies every tenant with a derivative when the key becomes retired
(tour.discontinued) and every tenant still holding that warning when it is served again (tour.reinstated). After
commit the caller runs services.tour_lifecycle.effects.run_after_commit (recompute, place graph, trip pages, job
hooks).

Not here (ADR 0015 slices): tenant reading of the notifications and the 409s on tenant routes (L3), the catalog
tombstone views (L4), Booking's staff notification for confirmed bookings (L5: CIS cannot read booking.*).
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import asyncpg
import structlog

from services.notifications import EventType, NotificationService
from shared.tour_liveness import SourceState, source_not_live_message

from . import derivatives as _d

logger = structlog.get_logger()

AA_TENANT = _d.AA_TENANT
# The catalog key of a raw_tours row aliased rt: the expression of raw_tours.source_key (CIS 258), written out so the
# service does not depend on the stored column.
KEY_EXPR = "(COALESCE(rt.source_group_id, rt.tour_id))::text"

# Reason a Master is not active (CIS 272 CHECK).
STAFF_DEACTIVATED, STAFF_TRASHED, SOURCE_DELETED, SUPERSEDED = (
    "staff_deactivated", "staff_trashed", "source_deleted", "superseded")

# The `restored` event's reason per action (CIS 272 CHECK).
_RESTORED_REASON = {"restore_source": "restore", "publish": "publish", "activate_master": "activate",
                    "set_active": "set_active", "supersede": "publish"}


class LifecycleError(Exception):
    """A refused action. `detail` starts with the machine code (the admin UI shows the whole string)."""

    def __str__(self) -> str:
        return self.detail

    def __init__(self, status: int, code: str, message: str):
        super().__init__(status, code, message)
        self.status = status
        self.code = code
        self.message = message

    @property
    def detail(self) -> str:
        # A 404 keeps its plain sentence (the answers before L2); every refusal of a rule starts with its code.
        return self.message if self.status == 404 else f"{self.code}: {self.message}"


@dataclass
class Outcome:
    action: str
    tour_id: str
    source_id: Optional[str] = None
    tour_name: str = ""
    before: tuple = (None, None)
    after: tuple = (None, None)
    events: list = field(default_factory=list)
    masters_off: list = field(default_factory=list)       # [(tour_id, reason)]
    masters_on: list = field(default_factory=list)        # [tour_id]
    reviews_dismissed: int = 0
    cancelled_jobs: list = field(default_factory=list)    # [{"id", "kind"}]
    notified_tenants: list = field(default_factory=list)  # tour.discontinued
    reinstated_tenants: list = field(default_factory=list)
    derivatives: dict = field(default_factory=dict)       # {tenant_id: {kind: n}} of the tenants warned
    recompute: list = field(default_factory=list)         # [(tour_id, new_status)] for after commit
    extra: dict = field(default_factory=dict)

    def add_recompute(self, tour_id: str, status: str) -> None:
        if (tour_id, status) not in self.recompute:
            self.recompute.append((tour_id, status))

    def summary(self) -> dict:
        return {
            "action": self.action, "tour_id": self.tour_id, "source_id": self.source_id,
            "state_before": self.before[0], "reason_before": self.before[1],
            "state_after": self.after[0], "reason_after": self.after[1],
            "events": [e["event"] + ":" + e["reason"] for e in self.events],
            "masters_switched_off": [t for t, _ in self.masters_off],
            "masters_switched_on": list(self.masters_on),
            "reviews_dismissed": self.reviews_dismissed,
            "jobs_cancelled": len(self.cancelled_jobs),
            "tenants_notified": len(self.notified_tenants),
            "tenants_reinstated": len(self.reinstated_tenants),
        }


# ── reads ──────────────────────────────────────────────────────────────────────────────────────────────────────────

_VERSION_SQL = f"""
    SELECT rt.tour_id::text AS tour_id, rt.tenant_id::text AS tenant_id, rt.src_name,
           rt.source_group_id::text AS source_group_id, rt.source_status::text AS source_status, rt.deleted_at,
           {KEY_EXPR} AS source_id
      FROM silver_aa_internal.raw_tours rt
     WHERE rt.tour_id = $1::uuid
       FOR UPDATE OF rt
"""

_MASTER_SQL = """
    SELECT pt.tour_id::text AS tour_id, pt.aa_name, pt.master_status::text AS master_status,
           pt.master_status_reason, pt.deleted_at
      FROM gold_aa_internal.published_tours pt
     WHERE pt.tour_id = $1::uuid
       FOR UPDATE OF pt
"""


# The source row of a Master / a publish, locked FOR SHARE until the action's transaction ends. Every writer of the
# source row (retire_source, restore_source, set_active, supersede) takes a row lock on it (FOR UPDATE, or the UPDATE
# itself), so it waits for this transaction, and this one waits for it: activate_master and publish re-check liveness
# under the lock and never put an active Master over a source that a concurrent retire / supersede just killed.
# Lock order: the source row first, then the Master row (retire_source does the same), so the two never deadlock.
_LOCK_SOURCE_SQL = """
    SELECT source_status::text AS source_status, deleted_at
      FROM silver_aa_internal.raw_tours
     WHERE tour_id = $1::uuid
       FOR SHARE
"""


def _uuid_or_none(value) -> Optional[str]:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


async def _version(conn, tour_id) -> Optional[asyncpg.Record]:
    tid = _uuid_or_none(tour_id)
    return await conn.fetchrow(_VERSION_SQL, tid) if tid else None


async def _master(conn, tour_id) -> Optional[asyncpg.Record]:
    tid = _uuid_or_none(tour_id)
    return await conn.fetchrow(_MASTER_SQL, tid) if tid else None


async def _locked_source_state(conn, tour_id) -> SourceState:
    """The liveness of `tour_id`'s source row, read under FOR SHARE (see _LOCK_SOURCE_SQL). Call inside the action's
    transaction, before the Master row is locked."""
    tid = _uuid_or_none(tour_id)
    row = await conn.fetchrow(_LOCK_SOURCE_SQL, tid) if tid else None
    if row is None:
        return SourceState(tour_id=str(tour_id), exists=False)
    return SourceState(tour_id=tid, exists=True, source_status=row["source_status"], deleted_at=row["deleted_at"])


async def key_of(conn, tour_id) -> Optional[str]:
    tid = _uuid_or_none(tour_id)
    if not tid:
        return None
    return await conn.fetchval(f"SELECT {KEY_EXPR} FROM silver_aa_internal.raw_tours rt WHERE rt.tour_id = $1::uuid",
                               tid)


async def served_state(conn, source_id: Optional[str]) -> tuple:
    """(state, reason) of the key from gold_aa_internal.v_tour_liveness; (None, None) for a key never served."""
    if not source_id:
        return (None, None)
    row = await conn.fetchrow(
        "SELECT state, reason FROM gold_aa_internal.v_tour_liveness WHERE source_id = $1", source_id)
    return (row["state"], row["reason"]) if row else (None, None)


async def _key_tour_ids(conn, source_id: str) -> list[str]:
    rows = await conn.fetch(
        f"SELECT rt.tour_id::text AS tour_id FROM silver_aa_internal.raw_tours rt WHERE {KEY_EXPR} = $1", source_id)
    return [r["tour_id"] for r in rows]


async def key_was_published(conn, tour_id) -> bool:
    """Q8: some version of the tour's key has a Master Content row (the tour was published at least once)."""
    sid = await key_of(conn, tour_id)
    if not sid:
        return False
    return bool(await conn.fetchval(f"""
        SELECT EXISTS (SELECT 1 FROM silver_aa_internal.raw_tours rt
                         JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rt.tour_id
                        WHERE {KEY_EXPR} = $1)
    """, sid))


DELETED_KEY_SQL = """
    SELECT bool_or(k.source_status = 'active' AND k.deleted_at IS NULL) AS has_live,
           max(k.deleted_at) FILTER (WHERE k.source_status = 'trashed' OR k.deleted_at IS NOT NULL) AS deleted_at,
           (array_agg(k.tour_id::text ORDER BY k.deleted_at DESC NULLS LAST, k.source_version DESC)
              FILTER (WHERE k.source_status = 'trashed'))[1] AS trashed_tour_id
      FROM silver_aa_internal.raw_tours k
     WHERE (COALESCE(k.source_group_id, k.tour_id))::text = $1
"""


async def deleted_key_info(conn, source_id: Optional[str]) -> dict:
    """Q6: is the key deleted at S0/A0 (no live version, a version trashed)? {deleted, deleted_at, trashed_tour_id}."""
    if not source_id:
        return {"deleted": False, "deleted_at": None, "trashed_tour_id": None}
    row = await conn.fetchrow(DELETED_KEY_SQL, source_id)
    deleted = bool(row and not row["has_live"] and row["deleted_at"] is not None)
    return {"deleted": deleted, "deleted_at": row["deleted_at"] if deleted else None,
            "trashed_tour_id": row["trashed_tour_id"] if deleted else None}


# ── the cascade (one transaction) ──────────────────────────────────────────────────────────────────────────────────

async def _switch_off_dead_masters(conn, out: Outcome) -> None:
    """Never an active Master over a source that is not live (and so never two active Masters per key)."""
    rows = await conn.fetch(f"""
        UPDATE gold_aa_internal.published_tours pt
           SET master_status = 'inactive'::gold_aa_internal.master_status_enum,
               master_status_reason = CASE WHEN rt.source_status = 'trashed' OR rt.deleted_at IS NOT NULL
                                           THEN '{SOURCE_DELETED}' ELSE '{SUPERSEDED}' END
          FROM silver_aa_internal.raw_tours rt
         WHERE rt.tour_id = pt.tour_id
           AND {KEY_EXPR} = $1
           AND pt.master_status = 'active'
           AND NOT (rt.source_status = 'active' AND rt.deleted_at IS NULL)
        RETURNING pt.tour_id::text AS tour_id, pt.master_status_reason AS reason
    """, out.source_id)
    for r in rows:
        out.masters_off.append((r["tour_id"], r["reason"]))
        out.add_recompute(r["tour_id"], "inactive")


async def _dismiss_reviews(conn, out: Outcome, *, all_of_key: bool) -> None:
    status = await conn.execute(f"""
        UPDATE silver_aa_internal.review_queue rq
           SET review_status = 'dismissed'::review_status_enum, reviewed_at = NOW(),
               reviewer_notes = 'AA-986: ' || $2
          FROM silver_aa_internal.raw_tours rt
         WHERE rq.tour_id = rt.tour_id
           AND {KEY_EXPR} = $1
           AND rq.review_status = 'pending'
           AND ($3 OR NOT (rt.source_status = 'active' AND rt.deleted_at IS NULL))
    """, out.source_id, f"tour lifecycle {out.action}", all_of_key)
    try:
        out.reviews_dismissed += int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        pass


async def _write_event(conn, out: Outcome, event: str, reason: str, actor: str,
                       replaced_by: Optional[str] = None, tour_id: Optional[str] = None) -> None:
    details = {"masters_switched_off": [t for t, _ in out.masters_off], "masters_switched_on": out.masters_on}
    key = f"{out.action}:{tour_id or out.tour_id}:{event}:{uuid.uuid4().hex}"
    await conn.execute("""
        INSERT INTO gold_aa_internal.tour_lifecycle_event
            (source_id, tour_id, event, reason, replaced_by_tour_id, action, actor, details, idempotency_key)
        VALUES ($1, $2::uuid, $3, $4, $5::uuid, $6, $7, $8::jsonb, $9)
    """, out.source_id, tour_id or out.tour_id, event, reason, replaced_by, out.action, actor,
        json.dumps(details), key)
    out.events.append({"event": event, "reason": reason, "tour_id": tour_id or out.tour_id,
                       "replaced_by_tour_id": replaced_by})


async def _outstanding_warnings(conn, tour_ids: list[str]) -> list[tuple[str, str]]:
    """(tenant_id, entity_id) pairs whose latest tour.discontinued / tour.reinstated is a discontinued."""
    rows = await conn.fetch("""
        SELECT DISTINCT ON (n.tenant_id, n.entity_id) n.tenant_id::text AS tenant_id, n.entity_id, n.event_type
          FROM shared.notifications n
         WHERE n.entity_type = 'tour' AND n.entity_id = ANY($1::text[])
           AND n.event_type IN ('tour.discontinued', 'tour.reinstated')
         ORDER BY n.tenant_id, n.entity_id, n.created_at DESC, n.id DESC
    """, tour_ids)
    return [(r["tenant_id"], r["entity_id"]) for r in rows if r["event_type"] == EventType.TOUR_DISCONTINUED.value]


async def _cascade(conn, out: Outcome, actor: str, *, superseded: Optional[list] = None,
                   replaced_by: Optional[str] = None) -> None:
    """Steps 1–5 of ADR 0015 decision 2 after the action's own status write. `superseded`: versions this action
    moved to 'superseded' (replaced by `replaced_by`)."""
    await _switch_off_dead_masters(conn, out)
    out.after = await served_state(conn, out.source_id)
    b_state, a_state, a_reason = out.before[0], out.after[0], out.after[1]
    ever_served = b_state is not None or a_state is not None

    await _dismiss_reviews(conn, out, all_of_key=(a_state == "retired" and a_reason == "master_trashed"))

    # The record (decision 4): one row per change of the derived state.
    wrote_superseded = False
    if superseded and replaced_by and ever_served:
        for old in superseded:
            await _write_event(conn, out, "superseded", "replaced", actor, replaced_by=replaced_by, tour_id=old)
        wrote_superseded = True
    if a_state == "served":
        if b_state in ("retired", "paused") or wrote_superseded:
            await _write_event(conn, out, "restored", _RESTORED_REASON.get(out.action, "restore"), actor)
    elif a_state in ("retired", "paused") and out.after != out.before:
        if not (wrote_superseded and a_reason == "superseded_pending"):
            await _write_event(conn, out, a_state, a_reason, actor)

    tour_ids = await _key_tour_ids(conn, out.source_id) if out.source_id else [out.tour_id]

    # Tenant work that spends LLM money on the tour stops (queued only).
    if a_state == "retired" or a_reason == "master_inactive":
        rows = await conn.fetch(_d.CANCEL_QUEUED_JOBS_SQL, tour_ids, out.action)
        out.cancelled_jobs += [{"id": r["id"], "kind": r["kind"]} for r in rows]

    svc = NotificationService(conn)
    if a_state == "retired" and b_state != "retired":
        derivs = await _d.tenant_derivatives(conn, tour_ids)
        warned = {t for t, _ in await _outstanding_warnings(conn, tour_ids)}
        for tenant_id, kinds in derivs.items():
            if tenant_id in warned:
                continue
            await svc.emit(
                event_type=EventType.TOUR_DISCONTINUED, entity_type="tour", entity_id=out.tour_id,
                tenant_id=tenant_id, actor_type="admin" if actor != "pipeline" else "system",
                payload={"tour_name": out.tour_name, "changed_by": actor, "reason": a_reason,
                         "source_id": out.source_id, "derivatives": kinds,
                         "live_posts": int(kinds.get("live_posts", 0))})
            out.notified_tenants.append(tenant_id)
            out.derivatives[tenant_id] = kinds
    elif a_state == "served" and b_state != "served":
        for tenant_id, entity_id in await _outstanding_warnings(conn, tour_ids):
            await svc.emit(
                event_type=EventType.TOUR_REINSTATED, entity_type="tour", entity_id=entity_id,
                tenant_id=tenant_id, actor_type="admin" if actor != "pipeline" else "system",
                payload={"tour_name": out.tour_name, "changed_by": actor, "source_id": out.source_id})
            out.reinstated_tenants.append(tenant_id)

    logger.info("tour_lifecycle", **out.summary())


async def _staff_event(conn, event_type: EventType, tour_id: str, tenant_id: str, payload: dict) -> None:
    await NotificationService(conn).emit(event_type=event_type, entity_type="tour", entity_id=tour_id,
                                         tenant_id=tenant_id, payload=payload, actor_type="admin")


# ── source actions ─────────────────────────────────────────────────────────────────────────────────────────────────

async def retire_source(conn, tour_id, *, actor: str = "admin", deleted_by: Optional[str] = None) -> Outcome:
    """Trash a source version at S0/A0. The key is retired (source_deleted) when it was the live version."""
    async with conn.transaction():
        v = await _version(conn, tour_id)
        if not v or v["source_status"] == "trashed":
            raise LifecycleError(404, "not_found", "Tour not found or already trashed")
        out = Outcome("retire_source", v["tour_id"], v["source_id"], v["src_name"] or "")
        out.before = await served_state(conn, out.source_id)
        row = await conn.fetchrow("""
            UPDATE silver_aa_internal.raw_tours
               SET source_status = 'trashed'::silver_aa_internal.source_status_enum,
                   deleted_at = NOW(), deleted_by = $2
             WHERE tour_id = $1::uuid AND source_status <> 'trashed'
            RETURNING deleted_at
        """, out.tour_id, deleted_by or actor)
        out.extra["deleted_at"] = row["deleted_at"]
        await _cascade(conn, out, actor)
        await _staff_event(conn, EventType.SOURCE_TRASHED, out.tour_id, v["tenant_id"],
                           {"tour_name": out.tour_name, "changed_by": actor})
        out.add_recompute(out.tour_id, "trashed")
    return out


async def restore_source(conn, tour_id, *, actor: str = "admin") -> Outcome:
    """Bring a trashed version back; re-enable only the Masters the service switched off for that deletion."""
    async with conn.transaction():
        v = await _version(conn, tour_id)
        if not v or v["source_status"] != "trashed":
            raise LifecycleError(404, "not_found", "Tour not found or not in trashed state")
        out = Outcome("restore_source", v["tour_id"], v["source_id"], v["src_name"] or "")
        out.before = await served_state(conn, out.source_id)
        try:
            async with conn.transaction():
                await conn.execute("""
                    UPDATE silver_aa_internal.raw_tours
                       SET source_status = 'active'::silver_aa_internal.source_status_enum,
                           deleted_at = NULL, deleted_by = NULL
                     WHERE tour_id = $1::uuid AND source_status = 'trashed'
                """, out.tour_id)
        except asyncpg.UniqueViolationError:
            raise LifecycleError(409, "group_has_active_version",
                                 "Another active tour exists in the same source group. "
                                 "Set it to superseded first, then restore.")
        rows = await conn.fetch(f"""
            UPDATE gold_aa_internal.published_tours
               SET master_status = 'active'::gold_aa_internal.master_status_enum, master_status_reason = NULL
             WHERE tour_id = $1::uuid AND master_status = 'inactive' AND master_status_reason = '{SOURCE_DELETED}'
            RETURNING tour_id::text AS tour_id
        """, out.tour_id)
        out.masters_on = [r["tour_id"] for r in rows]
        await _cascade(conn, out, actor)
        await _staff_event(conn, EventType.SOURCE_RESTORED, out.tour_id, v["tenant_id"],
                           {"tour_name": out.tour_name, "changed_by": actor})
        out.add_recompute(out.tour_id, "active" if out.after[0] == "served" else "inactive")
    return out


_INSERT_VERSION_SQL = """
    INSERT INTO silver_aa_internal.raw_tours (
        tenant_id, batch_id, source_id,
        tour_id_external, sku, provider,
        src_name, src_subtitle, src_summary, src_description,
        src_highlights, src_itineraries,
        country, duration, group_size, period,
        price_raw, inclusions, exclusions, links,
        activities, feature, best_time_to_go,
        pipeline_status,
        source_group_id, source_version, source_status
    ) VALUES (
        $1::uuid, $2::uuid, $3::uuid,
        $4, $5, $6,
        $7, $8, $9, $10,
        $11::jsonb, $12,
        $13, $14, $15, $16,
        $17, $18, $19, $20::jsonb,
        $21::jsonb, $22, $23,
        'ingested',
        $24::uuid, $25, $26
    )
    RETURNING tour_id::text
"""


async def _insert_version(conn, *, tenant_id: str, batch_id: str, payload: dict, group_id: str, version: int,
                          status: str) -> str:
    return await conn.fetchval(
        _INSERT_VERSION_SQL,
        tenant_id, batch_id, payload.get("source_id"), payload.get("tour_id_external"), payload.get("sku"),
        payload.get("provider"), payload.get("src_name"), payload.get("src_subtitle"), payload.get("src_summary"),
        payload.get("src_description"), json.dumps(payload.get("src_highlights") or []),
        payload.get("src_itineraries"), payload.get("country"), payload.get("duration"), payload.get("group_size"),
        payload.get("period"), payload.get("price_raw"), payload.get("inclusions"), payload.get("exclusions"),
        json.dumps(payload.get("links") or []), json.dumps(payload.get("activities") or []), payload.get("feature"),
        payload.get("best_time_to_go"), group_id, version, status)


async def supersede(conn, *, decision: str, tenant_id: str, batch_id: str, payload: dict,
                    matched_tour_id: Optional[str], matched_group_id: Optional[str],
                    actor: str = "admin") -> Outcome:
    """Commit a staged upload (decision replace | update | keep_both | create_new).

    replace / update  the incoming row becomes the live version of the matched key; the old live version is
                      superseded and its Master switched off (reason superseded): the key is paused
                      (superseded_pending) until the new version's Master is published (Q11), and a supersede is
                      invisible to tenants (Q2: no notification).
    keep_both         the incoming row is added as a superseded version; nothing else changes.
    create_new        the incoming row starts a new key (version 1, live), the matched one is left as it is (Q6).
    A matched key that was deleted at S0/A0 refuses replace / update / keep_both (409 source_deleted, Q6): staff choose
    Restore (restore_source) or Create a new tour."""
    if decision not in ("replace", "update", "keep_both", "create_new"):
        raise LifecycleError(400, "bad_decision", f"unknown decision {decision!r}")
    async with conn.transaction():
        source_id = await key_of(conn, matched_tour_id) if matched_tour_id else None
        if decision != "create_new" and source_id:
            info = await deleted_key_info(conn, source_id)
            if info["deleted"]:
                when = info["deleted_at"].date().isoformat() if info["deleted_at"] else "an earlier date"
                raise LifecycleError(409, "source_deleted",
                                     f"this tour was deleted on {when}. Choose Restore (bring the deleted tour back) "
                                     "or Create a new tour.")
        if decision == "create_new" or not matched_group_id:
            group_id, version = str(uuid.uuid4()), 1
            source_id = None if decision == "create_new" else source_id
        else:
            group_id = matched_group_id
            await conn.execute("SELECT 1 FROM silver_aa_internal.raw_tours WHERE source_group_id = $1::uuid "
                               "FOR UPDATE", group_id)
            version = await conn.fetchval(
                "SELECT COALESCE(MAX(source_version), 0) + 1 FROM silver_aa_internal.raw_tours "
                "WHERE source_group_id = $1::uuid", group_id)
        before = await served_state(conn, source_id)
        old_live: list[str] = []
        if decision in ("replace", "update") and matched_group_id and decision != "create_new":
            rows = await conn.fetch("""
                UPDATE silver_aa_internal.raw_tours
                   SET source_status = 'superseded'::silver_aa_internal.source_status_enum
                 WHERE source_group_id = $1::uuid AND source_status = 'active'
                RETURNING tour_id::text AS tour_id
            """, group_id)
            old_live = [r["tour_id"] for r in rows]
        status = "superseded" if decision == "keep_both" else "active"
        new_tour_id = await _insert_version(conn, tenant_id=tenant_id, batch_id=batch_id, payload=payload,
                                            group_id=group_id, version=version, status=status)
        out = Outcome("supersede", new_tour_id, await key_of(conn, new_tour_id), payload.get("src_name") or "")
        out.before = before
        out.extra.update({"decision": decision, "superseded": old_live, "source_version": version})
        await _cascade(conn, out, actor, superseded=old_live or None, replaced_by=new_tour_id)
    return out


async def set_active(conn, tour_id, *, actor: str = "admin") -> Outcome:
    """Make `tour_id` the live version of its key; the current live version is superseded. A trashed version is
    refused (restore it instead). A Master the service switched off for a supersede comes back with it."""
    async with conn.transaction():
        v = await _version(conn, tour_id)
        if not v or not v["source_group_id"]:
            raise LifecycleError(404, "not_found", "Tour not found")
        if v["source_status"] == "trashed" or v["deleted_at"] is not None:
            raise LifecycleError(409, "source_trashed",
                                 "this version was deleted (trashed) in S0/A0. Restore it (Upload, Trashed Source "
                                 "Tours, Restore) instead of setting it active.")
        out = Outcome("set_active", v["tour_id"], v["source_id"], v["src_name"] or "")
        out.before = await served_state(conn, out.source_id)
        rows = await conn.fetch("""
            UPDATE silver_aa_internal.raw_tours
               SET source_status = 'superseded'::silver_aa_internal.source_status_enum
             WHERE source_group_id = $1::uuid AND source_status = 'active' AND tour_id <> $2::uuid
            RETURNING tour_id::text AS tour_id
        """, v["source_group_id"], out.tour_id)
        old_live = [r["tour_id"] for r in rows]
        await conn.execute("""
            UPDATE silver_aa_internal.raw_tours
               SET source_status = 'active'::silver_aa_internal.source_status_enum
             WHERE tour_id = $1::uuid
        """, out.tour_id)
        on = await conn.fetch(f"""
            UPDATE gold_aa_internal.published_tours
               SET master_status = 'active'::gold_aa_internal.master_status_enum, master_status_reason = NULL
             WHERE tour_id = $1::uuid AND master_status = 'inactive' AND master_status_reason = '{SUPERSEDED}'
            RETURNING tour_id::text AS tour_id
        """, out.tour_id)
        out.masters_on = [r["tour_id"] for r in on]
        await _cascade(conn, out, actor, superseded=old_live or None, replaced_by=out.tour_id)
        out.add_recompute(out.tour_id, "active")
    return out


# ── Master actions ─────────────────────────────────────────────────────────────────────────────────────────────────

async def _master_outcome(conn, action: str, tour_id) -> tuple[Outcome, asyncpg.Record]:
    m = await _master(conn, tour_id)
    if not m:
        raise LifecycleError(404, "not_found", "Tour not found")
    out = Outcome(action, m["tour_id"], await key_of(conn, m["tour_id"]), m["aa_name"] or "")
    out.before = await served_state(conn, out.source_id)
    return out, m


async def pause_master(conn, tour_id, *, actor: str = "admin") -> Outcome:
    """Set Inactive: the key is paused (master_inactive), no tenant notification (Q1)."""
    async with conn.transaction():
        out, m = await _master_outcome(conn, "pause_master", tour_id)
        if m["master_status"] == "trashed":
            raise LifecycleError(409, "master_trashed", "this Master is in the trash. Restore it first.")
        await conn.execute(f"""
            UPDATE gold_aa_internal.published_tours
               SET master_status = 'inactive'::gold_aa_internal.master_status_enum,
                   master_status_reason = '{STAFF_DEACTIVATED}'
             WHERE tour_id = $1::uuid
        """, out.tour_id)
        await _cascade(conn, out, actor)
        await _staff_event(conn, EventType.MASTER_DEACTIVATED, out.tour_id, AA_TENANT,
                           {"tour_name": out.tour_name, "new_status": "inactive", "changed_by": actor})
        out.add_recompute(out.tour_id, "inactive")
    return out


async def activate_master(conn, tour_id, *, actor: str = "admin") -> Outcome:
    """Set Active. Refused when the source version is not live (409 source_not_live): a deleted tour comes back only
    through restore_source."""
    async with conn.transaction():
        # Source first (FOR SHARE), then the Master: a concurrent retire / supersede of this source either commits
        # before (and the check below refuses) or waits for this commit (and its switch-off sees this active Master).
        state = await _locked_source_state(conn, tour_id)
        out, m = await _master_outcome(conn, "activate_master", tour_id)
        if m["master_status"] == "trashed":
            raise LifecycleError(409, "master_trashed", "this Master is in the trash. Restore it first.")
        if not state.live:
            msg = source_not_live_message(state, "activate this Master").split(": ", 1)[1]
            if state.reason in ("trashed", "deleted"):
                msg += " Restoring the source tour brings its Master back too."
            raise LifecycleError(409, "source_not_live", msg)
        await conn.execute("""
            UPDATE gold_aa_internal.published_tours
               SET master_status = 'active'::gold_aa_internal.master_status_enum, master_status_reason = NULL
             WHERE tour_id = $1::uuid
        """, out.tour_id)
        out.masters_on = [out.tour_id] if m["master_status"] != "active" else []
        await _cascade(conn, out, actor)
        await _staff_event(conn, EventType.MASTER_ACTIVATED, out.tour_id, AA_TENANT,
                           {"tour_name": out.tour_name, "new_status": "active", "changed_by": actor})
        out.add_recompute(out.tour_id, "active")
    return out


async def trash_master(conn, tour_id, *, actor: str = "admin") -> Outcome:
    """Trash the Master: the key is retired (master_trashed) and tenants with a derivative are warned (Q1)."""
    async with conn.transaction():
        out, m = await _master_outcome(conn, "trash_master", tour_id)
        if m["master_status"] == "trashed":
            raise LifecycleError(404, "not_found", "Tour not found or already trashed")
        row = await conn.fetchrow(f"""
            UPDATE gold_aa_internal.published_tours
               SET master_status = 'trashed'::gold_aa_internal.master_status_enum,
                   master_status_reason = '{STAFF_TRASHED}', deleted_at = NOW(), deleted_by = $2
             WHERE tour_id = $1::uuid
            RETURNING deleted_at
        """, out.tour_id, actor)
        out.extra["deleted_at"] = row["deleted_at"]
        await _cascade(conn, out, actor)
        await _staff_event(conn, EventType.MASTER_TRASHED, out.tour_id, AA_TENANT,
                           {"tour_name": out.tour_name, "changed_by": actor})
        out.add_recompute(out.tour_id, "trashed")
    return out


async def restore_master(conn, tour_id, *, actor: str = "admin") -> Outcome:
    """Take a Master out of the trash; it comes back inactive (paused) and staff activate it."""
    async with conn.transaction():
        out, m = await _master_outcome(conn, "restore_master", tour_id)
        if m["master_status"] != "trashed":
            raise LifecycleError(404, "not_found", "Tour not found or not in trashed state")
        await conn.execute(f"""
            UPDATE gold_aa_internal.published_tours
               SET master_status = 'inactive'::gold_aa_internal.master_status_enum,
                   master_status_reason = '{STAFF_DEACTIVATED}', deleted_at = NULL, deleted_by = NULL
             WHERE tour_id = $1::uuid
        """, out.tour_id)
        await _cascade(conn, out, actor)
        await _staff_event(conn, EventType.MASTER_RESTORED, out.tour_id, AA_TENANT,
                           {"tour_name": out.tour_name, "new_status": "inactive", "changed_by": actor})
        out.add_recompute(out.tour_id, "inactive")
    return out


# ── inserts ────────────────────────────────────────────────────────────────────────────────────────────────────────

async def publish(conn, data: dict, *, tenant_slug: str = "aa_internal", actor: str = "pipeline") -> Outcome:
    """The Master insert of process_export. Closes a paused / retired key with `restored` (reason publish) and
    switches off any other active Master of the key.

    The caller checked the source is live (L1) before its own reads; this re-checks it under FOR SHARE inside the
    transaction (409 source_not_live), so a retire / supersede that commits between that check and this insert is
    refused here, and one that starts after the lock waits for this commit and then switches the new Master off.

    Publish dates (AA-645 d06, CIS 273): a first publish gets published_at and updated_at from their defaults; a
    re-publish of the key's Master (a rewrite, a rerun wave, the admin promote of an older version) keeps the first
    published_at and sets updated_at = now() — the repository's ON CONFLICT branch, which only this function runs."""
    from shared.repository.published_catalog_repository import PublishedCatalogRepository
    async with conn.transaction():
        tour_id = str(data["tour_id"])
        state = await _locked_source_state(conn, tour_id)
        if not state.live:
            raise LifecycleError(409, "source_not_live",
                                 source_not_live_message(state, "publish to Master Content").split(": ", 1)[1])
        out = Outcome("publish", tour_id, await key_of(conn, tour_id), str(data.get("aa_name") or ""))
        out.before = await served_state(conn, out.source_id)
        out.extra["catalog_id"] = await PublishedCatalogRepository(conn, tenant_slug).insert(data)
        await _cascade(conn, out, actor)
    return out


async def insert_new_sources(tour_repo, records: list[dict]) -> tuple[list, list]:
    """Ingest: rows that start a new key (version 1, live). A key without a Master has no served state, so this is a
    plain insert with no event (ADR 0015 decision 2); re-uploads of an existing key go through staging (supersede)."""
    for r in records:
        if int(r.get("source_version") or 1) != 1 or (r.get("source_status") or "active") != "active":
            raise ValueError("insert_new_sources takes new keys only (source_version 1, active)")
    return await tour_repo.insert_batch(records)


# ── dry run (Q8 confirm dialog) ────────────────────────────────────────────────────────────────────────────────────

PREVIEW_ACTIONS = {"retire_source": retire_source, "trash_master": trash_master, "pause_master": pause_master}


async def preview(conn, tour_id, action: str) -> dict:
    """What `action` would do to `tour_id`, counted by running it in a transaction that is rolled back: the same
    code path as the real action, so the counts cannot drift from it. Bookings are not counted: CIS cannot read
    booking.* (ADR 0015 slice L5)."""
    fn = PREVIEW_ACTIONS.get(action)
    if fn is None:
        raise LifecycleError(400, "bad_action", f"action must be one of {sorted(PREVIEW_ACTIONS)}")
    sid = await key_of(conn, tour_id) if action == "retire_source" else None
    if action != "retire_source":
        m = await conn.fetchval("SELECT tour_id::text FROM gold_aa_internal.published_tours WHERE tour_id = $1::uuid",
                                _uuid_or_none(tour_id))
        sid = await key_of(conn, m) if m else None
    tour_ids = await _key_tour_ids(conn, sid) if sid else [str(tour_id)]
    derivs = await _d.tenant_derivatives(conn, tour_ids)
    queued = await conn.fetchval(_d.COUNT_QUEUED_JOBS_SQL, tour_ids)
    tr = conn.transaction()
    await tr.start()
    try:
        try:
            out = await fn(conn, tour_id, actor="dry-run")
            refused = None
        except LifecycleError as e:
            out, refused = None, e
    finally:
        await tr.rollback()
    result: dict[str, Any] = {
        "action": action, "tour_id": str(tour_id), "source_id": sid,
        "allowed": refused is None, "refused": refused.detail if refused else None,
        "derivatives": _d.summarize(derivs), "queued_jobs": int(queued or 0),
        "bookings": None,
        "bookings_note": "Bookings are not counted here: AA-Booking keeps confirmed bookings and holds and flags them "
                         "for staff (ADR 0015 slice L5).",
    }
    if out is not None:
        result.update(out.summary())
        result["notifies_tenants"] = len(out.notified_tenants)
    return result
