"""AA-1148 (S290) — the database side of the content workspace: hand-made tours (create, read, save the facts), the
required-list gate of their publish, and the supplier links of every source tour (the unlinked report, the bulk link,
the suggested matches). The routes are api/routers/admin_content_workspace.py.

A hand-made tour enters like an Excel row: the tour lifecycle service starts a new catalog key
(``supersede(decision='create_new')``, the only writer of a raw_tours INSERT), then this module marks it
``entry_origin = 'manual'``, stores the structured facts and the supplier, and writes the first wording draft (a
``generated_content`` row, status draft, origin human, version 1) from the facts. From there it is a normal tour: the
wording is edited through the draft routes of AA-1146 (PUT /admin/tours/{id}/draft), checked by the re-validation and
published through the one gate (POST /admin/tours/{id}/draft/publish), which also runs :func:`publish_check` here for a
hand-made tour (409 ``required_missing``). Rewrite (S1) can write it too, like any source tour.

Facts saves: a tour that was never published is a draft nobody outside sees, so the form saves as the person types
(autosave). Once it has a Master, a save is a change of a live tour: it needs a reason and runs the source fan-out, like
every raw edit (the drawer's PATCH /admin/tours/{id}/raw). Every save that changes something writes one history row
(raw_tour_revision, staff_edit) and one audit row with the before / after. Two people saving the same form: the save
carries the ``saved_at`` it was loaded with; another save in between is 409 ``facts_changed``.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from services.acp_shared.audit_log import write_audit_log
from services.content_workspace import facts as F
from services.content_workspace import policy
from services.content_workspace import suppliers as S

logger = structlog.get_logger()

MASTER_TENANT = "00000000-0000-0000-0000-000000000001"
RESOURCE = "raw_tour"


class WorkspaceError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: Optional[dict] = None):
        super().__init__(status, code, message, extra)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


# The columns a facts save writes (the rendered text columns, the country and the supplier fields).
_TEXT_COLS = ("src_name", "src_summary", "src_description", "src_itineraries", "duration", "group_size", "inclusions",
              "exclusions", "best_time_to_go")
_TOUR_SQL = """
    SELECT rt.tour_id::text AS tour_id, rt.src_name, rt.src_summary, rt.src_description, rt.src_itineraries,
           rt.duration, rt.group_size, rt.inclusions, rt.exclusions, rt.best_time_to_go, rt.country,
           rt.country_iso2::text AS country_iso2, rt.provider, rt.supplier_id::text AS supplier_id, rt.entry_origin,
           rt.manual_facts::text AS manual_facts, rt.source_status::text AS source_status, rt.deleted_at,
           rt.ingest_at, (COALESCE(rt.source_group_id, rt.tour_id))::text AS catalog_key,
           pt.master_status::text AS master_status, pt.deleted_at AS master_deleted_at, pt.published_at,
           pt.human_lock_at
      FROM silver_aa_internal.raw_tours rt
      LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rt.tour_id
     WHERE rt.tour_id = $1::uuid
"""
_WORDING_SQL = """
    SELECT gc.id::text AS id, gc.version_num, gc.status::text AS status, gc.aa_name, gc.aa_summary, gc.aa_itineraries
      FROM silver_aa_internal.generated_content gc
     WHERE gc.tour_id = $1::uuid AND (gc.status = 'draft' AND gc.origin = 'human'
           OR gc.id = (SELECT p.generated_content_id FROM gold_aa_internal.published_tours p
                        WHERE p.tour_id = $1::uuid))
     ORDER BY (gc.status = 'draft') DESC, gc.version_num DESC LIMIT 1
"""
_FIRST_DRAFT_SQL = """
    INSERT INTO silver_aa_internal.generated_content (
        tour_id, tenant_id, version_num, aa_name, aa_summary, aa_itineraries, aa_highlights, status, retry_count,
        metadata, fallback_used, human_edited, edited_at, edit_diff, revalidate_passed, satellite_used, origin,
        parent_version_num, edit_reason, author_id)
    VALUES ($1::uuid, $2::uuid, 1, $3, $4, $5, $6::jsonb, 'draft', 0, $7::jsonb, false, true, now(), $8::jsonb, NULL,
            false, 'human', NULL, $9, $10)
    RETURNING id::text AS id
"""
_FROM_PRICE_SQL = """
    SELECT amount::text AS amount, currency, basis, pax, season, valid_to
      FROM booking.v_from_prices WHERE source_system = 'cis' AND source_id = $1 LIMIT 1
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(v) -> Optional[str]:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _country(value: Optional[str]):
    """(canonical name, iso2) of a country typed or picked (ISO code or name); (None, None) for blank; 422 otherwise."""
    from shared import countries

    if not value or not str(value).strip():
        return None, None
    c = countries.by_iso2(value) if len(value.strip()) == 2 else None
    c = c or countries.resolve(value)
    if c is None:
        raise WorkspaceError(422, "country_unknown", f"{value!r} is not a country of the ISO list")
    return c.name, c.iso2


def _state(row) -> str:
    """draft (never published), live, live_changes is decided by the caller (open draft), retired (source not live),
    paused (Master switched off)."""
    if row["source_status"] != "active" or row["deleted_at"] is not None:
        return "retired"
    if row["master_status"] is None:
        return "draft"
    if row["master_status"] != "active" or row["master_deleted_at"] is not None:
        return "paused"
    return "live"


async def _supplier_of(pool, supplier_id: Optional[str]) -> tuple[Optional[dict], bool]:
    """(the supplier as the Booking view lists it, whether the view could be read). An id the view does not list is
    shown as unknown, never dropped."""
    if not supplier_id:
        return None, True
    found = await S.by_ids(pool, [supplier_id])
    if found is None:
        return {"id": supplier_id, "company_name": None, "status": "unknown", "code": None, "categories": [],
                "country_code": None}, False
    return found.get(supplier_id) or {"id": supplier_id, "company_name": None, "status": "missing", "code": None,
                                      "categories": [], "country_code": None}, True


async def _checked_supplier(pool, supplier_id: Optional[str]) -> Optional[dict]:
    """The supplier a save links: it must be listed by the Booking view and not inactive."""
    if not supplier_id:
        return None
    try:
        uuid.UUID(supplier_id)
    except ValueError:
        raise WorkspaceError(422, "supplier_unknown", "Choose the supplier from the list")
    found = await S.by_ids(pool, [supplier_id])
    if found is None:
        raise WorkspaceError(503, "suppliers_unavailable",
                             "The supplier list of Booking cannot be read right now: try again later")
    sup = found.get(supplier_id)
    if sup is None:
        raise WorkspaceError(422, "supplier_unknown", "This supplier is not in Booking's list")
    if sup["status"] == "inactive":
        raise WorkspaceError(422, "supplier_inactive", f"{sup['company_name']} is inactive in Booking")
    return sup


async def wording_of(conn, tour_id: str) -> Optional[dict]:
    """The open human draft of the wording, else the live version (name, summary, itinerary)."""
    row = await conn.fetchrow(_WORDING_SQL, tour_id)
    return dict(row) if row else None


async def checklist_for(pool, conn, row, facts: dict, *, limits: policy.Limits) -> tuple[list[dict], Optional[dict]]:
    supplier, _ok = await _supplier_of(pool, row["supplier_id"])
    wording = await wording_of(conn, row["tour_id"])
    items = F.checklist(facts, limits=limits, country_ok=bool(row["country"]), supplier=supplier, wording=wording)
    return items, supplier


# ── create ────────────────────────────────────────────────────────────────────────────────────────────────────────

async def create_tour(pool, form: F.FactsIn, *, actor: str) -> dict:
    """A new hand-made tour from the form: the source tour (a new catalog key), its facts and the first wording draft.
    Returns {tour_id, wording_version_id}."""
    from services.tour_lifecycle import run_after_commit, supersede

    lim = await policy.limits()
    stored = F.normalize(form)
    _check_size(stored, lim)
    if not stored["name"]:
        raise WorkspaceError(422, "name_required", "Give the tour a name")
    country, _iso2 = _country(form.country)
    sup = await _checked_supplier(pool, form.supplier_id)
    stored["saved_at"] = _now_iso()
    text = F.render(stored)
    payload = {**text, "country": country, "provider": sup["company_name"] if sup else None}
    async with pool.acquire() as conn:
        async with conn.transaction():
            outcome = await supersede(conn, decision="create_new", tenant_id=MASTER_TENANT,
                                      batch_id=str(uuid.uuid4()), payload=payload, matched_tour_id=None,
                                      matched_group_id=None, actor=actor)
            tour_id = outcome.tour_id
            await conn.execute(
                "UPDATE silver_aa_internal.raw_tours SET entry_origin = 'manual', manual_facts = $2::jsonb, "
                "supplier_id = $3::uuid WHERE tour_id = $1::uuid",
                tour_id, json.dumps(stored, ensure_ascii=False), sup["id"] if sup else None)
            first = F.first_wording(stored)
            reason = "Hand-made tour created in the content workspace"
            edit_diff = {"editors": [actor], "fields": sorted(k for k, v in first.items() if v), "reason": reason,
                         "draft": True}
            version_id = await conn.fetchval(
                _FIRST_DRAFT_SQL, tour_id, MASTER_TENANT, first["aa_name"], first["aa_summary"],
                first["aa_itineraries"], json.dumps(first["aa_highlights"]),
                json.dumps({"entry_origin": "manual", "source": "content_workspace"}), json.dumps(edit_diff),
                reason, actor)
            await write_audit_log(conn, tenant_id=None, actor=actor, action="raw_tour.manual_created",
                                  resource_type=RESOURCE, resource_id=tour_id, details={
                                      "tour_id": tour_id, "supplier_id": sup["id"] if sup else None,
                                      "country": country, "days": len(stored["days"]), "version_id": version_id})
    await run_after_commit(pool, outcome)
    # AA-1047 W1: the first wording through the REPORT-mode final contract (recorded, never blocks), like a draft save
    from services.quality import final_contract_store as fc_store
    async with pool.acquire() as conn:
        await fc_store.record_s1_exit(conn, version_id, "human_edit")
    logger.info("aa1148_manual_tour_created", tour_id=tour_id, supplier_id=sup["id"] if sup else None)
    return {"tour_id": tour_id, "wording_version_id": version_id}


def _check_size(stored: dict, lim: policy.Limits) -> None:
    if len(stored["days"]) > lim.max_days:
        raise WorkspaceError(422, "too_many_days", f"A hand-made tour has at most {lim.max_days} days",
                             {"max_days": lim.max_days})


# ── read ──────────────────────────────────────────────────────────────────────────────────────────────────────────

async def _from_price(pool, key: str) -> Optional[dict]:
    """The public "from" price Booking serves for the tour (price.from.v1), read-only; None when none or unreadable."""
    try:
        async with pool.acquire() as conn, conn.transaction():
            r = await conn.fetchrow(_FROM_PRICE_SQL, key)
    except Exception as exc:  # noqa: BLE001 — degrade to "no price"
        logger.warning("aa1148_from_price_unreadable", error=type(exc).__name__)
        return None
    if r is None or r["amount"] is None:
        return None
    return {"amount": r["amount"], "currency": str(r["currency"]).strip(), "basis": r["basis"], "pax": r["pax"],
            "season": r["season"], "valid_to": _iso(r["valid_to"])}


async def read_tour(pool, tour_id: str) -> dict:
    lim = await policy.limits()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(_TOUR_SQL, tour_id)
        if row is None:
            raise WorkspaceError(404, "not_found", "Tour not found")
        stored = F.facts_from_row(row)
        items, supplier = await checklist_for(pool, conn, row, stored, limits=lim)
        has_draft = bool(await conn.fetchval(
            "SELECT 1 AS ok FROM silver_aa_internal.generated_content WHERE tour_id = $1::uuid AND status = 'draft' "
            "AND origin = 'human'", tour_id))
    state = _state(row)
    if state == "live" and has_draft:
        state = "live_changes"
    return {
        "tour_id": row["tour_id"], "entry_origin": row["entry_origin"], "state": state,
        "editable": row["entry_origin"] == "manual" and state != "retired",
        "reason_required": row["entry_origin"] == "manual" and state in ("live", "live_changes", "paused"),
        "facts": {**stored, "country": row["country_iso2"], "supplier_id": row["supplier_id"]},
        "country_name": row["country"], "provider": row["provider"], "supplier": supplier,
        "checklist": items, "completeness": F.completeness(items), "has_draft": has_draft,
        "published_at": _iso(row["published_at"]), "locked": row["human_lock_at"] is not None,
        "from_price": await _from_price(pool, row["catalog_key"]), "limits": lim.as_dict(),
    }


async def list_manual(pool, *, q: str = "", limit: int = 50, offset: int = 0) -> dict:
    """The hand-made tours, newest first, with their state and supplier name."""
    where = ["rt.entry_origin = 'manual'"]
    args: list[Any] = []
    if q.strip():
        args.append(f"%{q.strip()}%")
        where.append(f"rt.src_name ILIKE ${len(args)}")
    sql_where = " AND ".join(where)
    async with pool.acquire() as conn:
        total = await conn.fetchval(f"SELECT count(*) AS n FROM silver_aa_internal.raw_tours rt WHERE {sql_where}",
                                    *args)
        rows = await conn.fetch(
            f"""SELECT rt.tour_id::text AS tour_id, rt.src_name, rt.country, rt.supplier_id::text AS supplier_id,
                       rt.provider, rt.ingest_at, rt.source_status::text AS source_status, rt.deleted_at,
                       pt.master_status::text AS master_status, pt.deleted_at AS master_deleted_at,
                       EXISTS (SELECT 1 FROM silver_aa_internal.generated_content g WHERE g.tour_id = rt.tour_id
                                  AND g.status = 'draft' AND g.origin = 'human') AS has_draft,
                       jsonb_array_length(COALESCE(rt.manual_facts -> 'days', '[]'::jsonb)) AS days
                  FROM silver_aa_internal.raw_tours rt
                  LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rt.tour_id
                 WHERE {sql_where}
                 ORDER BY rt.ingest_at DESC, rt.tour_id
                 LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}""", *args, limit, offset)
    names = await S.by_ids(pool, [r["supplier_id"] for r in rows]) or {}
    items = []
    for r in rows:
        state = _state(r)
        if state == "live" and r["has_draft"]:
            state = "live_changes"
        sup = names.get(r["supplier_id"]) if r["supplier_id"] else None
        items.append({"tour_id": r["tour_id"], "name": r["src_name"], "country": r["country"],
                      "supplier_id": r["supplier_id"],
                      "supplier_name": sup["company_name"] if sup else (r["provider"] if r["supplier_id"] else None),
                      "state": state, "days": r["days"], "created_at": _iso(r["ingest_at"])})
    return {"items": items, "total": total, "limit": limit, "offset": offset}


# ── save facts ────────────────────────────────────────────────────────────────────────────────────────────────────

def _check_savable(row, old: dict, *, expected_saved_at: Optional[str], reason: Optional[str]) -> bool:
    """Refuse a save the tour cannot take; True when the tour is published (the save is a live change)."""
    if row is None:
        raise WorkspaceError(404, "not_found", "Tour not found")
    if row["entry_origin"] != "manual":
        raise WorkspaceError(409, "not_hand_made", "Only a hand-made tour is edited here; change the facts of a sheet "
                             "tour in its drawer (Original tab)")
    state = _state(row)
    if state == "retired":
        raise WorkspaceError(409, "tour_retired", "This tour was deleted: restore it before you edit it")
    if expected_saved_at and old.get("saved_at") and expected_saved_at != old.get("saved_at"):
        raise WorkspaceError(409, "facts_changed", "Someone else saved this tour since you opened it: reload it to see "
                             "their changes", {"saved_at": old.get("saved_at")})
    live = state != "draft"
    if live and not (reason and 3 <= len(reason.strip()) <= 500):
        raise WorkspaceError(422, "reason_required", "This tour is published: say why you change its facts "
                             "(3 to 500 characters)")
    return live


async def _plan_save(pool, row, form: F.FactsIn, stored: dict, old: dict, country: Optional[str]) -> tuple:
    """(the column values to write, the supplier id, the changed fields, before, after) of a facts save."""
    sup, sup_row = row["supplier_id"], None
    if (form.supplier_id or None) != (row["supplier_id"] or None):
        sup_row = await _checked_supplier(pool, form.supplier_id)
        sup = sup_row["id"] if sup_row else None
    new_cols = {**F.render(stored), "country": country,
                "provider": sup_row["company_name"] if sup_row else row["provider"]}
    before = {c: row[c] for c in (*_TEXT_COLS, "country", "provider")}
    changed = [c for c in new_cols if (new_cols[c] or None) != (before[c] or None)]
    if (sup or None) != (row["supplier_id"] or None):
        changed.append("supplier_id")
    old_cmp = {k: v for k, v in old.items() if k != "saved_at"}
    new_cmp = {k: v for k, v in stored.items() if k != "saved_at"}
    if old_cmp != new_cmp:
        changed.append("manual_facts")
    after_vals = {**new_cols, "supplier_id": sup, "manual_facts": new_cmp}
    before_vals = {**before, "supplier_id": row["supplier_id"], "manual_facts": old_cmp}
    return new_cols, sup, changed, {c: before_vals[c] for c in changed}, {c: after_vals[c] for c in changed}


async def save_facts(pool, tour_id: str, form: F.FactsIn, *, expected_saved_at: Optional[str], reason: Optional[str],
                     actor: str) -> dict:
    from api.routers.admin_pipeline.tour_detail import _source_fanout_raw, record_raw_revision

    lim = await policy.limits()
    stored = F.normalize(form)
    _check_size(stored, lim)
    country, _iso2 = _country(form.country)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(_TOUR_SQL + " FOR UPDATE OF rt", tour_id)
            old = F.facts_from_row(row) if row is not None else {}
            live = _check_savable(row, old, expected_saved_at=expected_saved_at, reason=reason)
            new_cols, sup, changed, hist_b, hist_a = await _plan_save(pool, row, form, stored, old, country)
            if not changed:
                return {"tour_id": tour_id, "saved": False, "saved_at": old.get("saved_at")}
            stored["saved_at"] = _now_iso()
            cols = list(new_cols)
            sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
            n = len(cols)
            changed_at = await conn.fetchval(
                f"UPDATE silver_aa_internal.raw_tours SET {sets}, manual_facts = ${n + 2}::jsonb, "
                f"supplier_id = ${n + 3}::uuid WHERE tour_id = $1::uuid RETURNING now() AS changed_at",
                tour_id, *[new_cols[c] for c in cols], json.dumps(stored, ensure_ascii=False), sup)
            why = reason.strip() if reason else None
            revision = await record_raw_revision(conn, tour_id, source="staff_edit", before=hist_b, after=hist_a,
                                                 actor=actor, reason=why)
            await write_audit_log(conn, tenant_id=None, actor=actor, action="raw_tour.manual_facts_saved",
                                  resource_type=RESOURCE, resource_id=tour_id, details={
                                      "fields": changed, "before": hist_b, "after": hist_a, "live": live,
                                      "reason": why, "revision_num": revision})
    if live:
        await _source_fanout_raw(pool, tour_id, changed_at, [c for c in changed if c != "manual_facts"])
    return {"tour_id": tour_id, "saved": True, "saved_at": stored["saved_at"], "fields": changed,
            "revision_num": revision}


# ── the publish gate of a hand-made tour ─────────────────────────────────────────────────────────────────────────

async def publish_check(pool, conn, tour_id: str) -> None:
    """In the draft publish transaction: a hand-made tour publishes only with its required list complete (409
    ``required_missing`` with the missing items); a sheet tour is not checked here."""
    import asyncpg

    try:
        async with conn.transaction():   # a savepoint: before CIS 478 the read fails and the publish goes on
            row = await conn.fetchrow(_TOUR_SQL, tour_id)
    except asyncpg.exceptions.UndefinedColumnError:
        return
    if row is None or row["entry_origin"] != "manual":
        return
    lim = await policy.limits()
    items, _sup = await checklist_for(pool, conn, row, F.facts_from_row(row), limits=lim)
    gaps = F.missing(items)
    if gaps:
        raise WorkspaceError(409, "required_missing", "This hand-made tour is not complete: " + "; ".join(
            i["message"] for i in gaps), {"items": [{"key": i["key"], "message": i["message"], "params": i["params"]}
                                                 for i in gaps]})


# ── supplier links ───────────────────────────────────────────────────────────────────────────────────────────────

_UNLINKED_SQL = """
    SELECT rt.tour_id::text AS tour_id, rt.src_name, rt.provider, rt.country, rt.country_iso2::text AS country_iso2,
           rt.entry_origin, (pt.tour_id IS NOT NULL AND pt.master_status = 'active' AND pt.deleted_at IS NULL) AS live
      FROM silver_aa_internal.raw_tours rt
      LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = rt.tour_id
     WHERE rt.supplier_id IS NULL AND rt.source_status = 'active' AND rt.deleted_at IS NULL
"""


async def _unlinked_rows(conn, *, country: Optional[str], q: str) -> list:
    sql, args = _UNLINKED_SQL, []
    if country:
        args.append(country.upper())
        sql += f" AND rt.country_iso2 = ${len(args)}"
    if q.strip():
        args.append(f"%{q.strip()}%")
        sql += f" AND (rt.src_name ILIKE ${len(args)} OR rt.provider ILIKE ${len(args)})"
    return await conn.fetch(sql + " ORDER BY lower(COALESCE(rt.provider, '')), rt.src_name, rt.tour_id", *args)


async def unlinked_report(pool, *, country: Optional[str] = None, q: str = "", only_suggested: bool = False,
                          limit: int = 50, offset: int = 0) -> dict:
    """The live source tours not linked to an AA supplier yet, with the suggested supplier when the sheet's provider
    text names exactly one. Counts: unlinked, with a suggestion, linked."""
    async with pool.acquire() as conn:
        rows = await _unlinked_rows(conn, country=country, q=q)
        linked = await conn.fetchval(
            "SELECT count(*) AS n FROM silver_aa_internal.raw_tours WHERE supplier_id IS NOT NULL "
            "AND source_status = 'active' AND deleted_at IS NULL")
    all_sup = await S.list_all(pool)
    out = []
    for r in rows:
        sug = S.suggestion(r["provider"], r["country_iso2"], all_sup) if all_sup else None
        if only_suggested and sug is None:
            continue
        out.append({"tour_id": r["tour_id"], "name": r["src_name"], "provider": r["provider"],
                    "country": r["country"], "country_iso2": r["country_iso2"], "live": r["live"],
                    "entry_origin": r["entry_origin"],
                    "suggestion": ({"id": sug["id"], "company_name": sug["company_name"], "code": sug["code"]}
                                   if sug else None)})
    with_sug = sum(1 for x in out if x["suggestion"]) if not only_suggested else len(out)
    return {"items": out[offset:offset + limit], "total": len(out), "unlinked": len(rows), "with_suggestion": with_sug,
            "linked": linked, "suppliers_available": all_sup is not None, "limit": limit, "offset": offset}


async def _link(conn, tour_ids: list[str], supplier: dict, *, actor: str, reason: str, action: str) -> list[str]:
    rows = await conn.fetch(
        "UPDATE silver_aa_internal.raw_tours rt SET supplier_id = $2::uuid FROM silver_aa_internal.raw_tours old "
        "WHERE rt.tour_id = old.tour_id AND rt.tour_id = ANY($1::uuid[]) "
        "AND rt.supplier_id IS DISTINCT FROM $2::uuid "
        "RETURNING rt.tour_id::text AS tour_id, old.supplier_id::text AS before", tour_ids, supplier["id"])
    from api.routers.admin_pipeline.tour_detail import record_raw_revision
    for r in rows:
        revision = await record_raw_revision(conn, r["tour_id"], source="staff_edit",
                                             before={"supplier_id": r["before"]},
                                             after={"supplier_id": supplier["id"]}, actor=actor, reason=reason)
        await write_audit_log(conn, tenant_id=None, actor=actor, action=action, resource_type=RESOURCE,
                              resource_id=r["tour_id"], details={
                                  "before": {"supplier_id": r["before"]}, "after": {"supplier_id": supplier["id"]},
                                  "supplier_name": supplier["company_name"], "reason": reason,
                                  "revision_num": revision})
    return [r["tour_id"] for r in rows]


async def link_tours(pool, tour_ids: list[str], supplier_id: str, *, reason: str, actor: str) -> dict:
    """Link the given source tours to one supplier (a person's choice from the unlinked report or the tour list)."""
    lim = await policy.limits()
    ids = list(dict.fromkeys(tour_ids))
    if not ids:
        raise WorkspaceError(422, "no_tours", "Choose at least one tour")
    if len(ids) > lim.bulk_max:
        raise WorkspaceError(422, "too_many_tours", f"Link at most {lim.bulk_max} tours at a time",
                             {"bulk_max": lim.bulk_max})
    for i in ids:
        try:
            uuid.UUID(i)
        except ValueError:
            raise WorkspaceError(422, "bad_tour_id", "A tour id is not valid")
    sup = await _checked_supplier(pool, supplier_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            changed = await _link(conn, ids, sup, actor=actor, reason=reason, action="raw_tour.supplier_linked")
    return {"supplier_id": sup["id"], "linked": changed, "unchanged": len(ids) - len(changed)}


async def apply_suggestions(pool, *, dry_run: bool, reason: str, actor: str) -> dict:
    """Link every unlinked tour whose sheet provider names exactly one supplier (at most bulk_max per call; the rest
    stay for the next call). DRY RUN lists what would change and writes nothing."""
    lim = await policy.limits()
    all_sup = await S.list_all(pool)
    if all_sup is None:
        raise WorkspaceError(503, "suppliers_unavailable",
                             "The supplier list of Booking cannot be read right now: try again later")
    by_id = {s["id"]: s for s in all_sup}
    async with pool.acquire() as conn:
        rows = await _unlinked_rows(conn, country=None, q="")
        plan: dict[str, list[str]] = {}
        for r in rows:
            sug = S.suggestion(r["provider"], r["country_iso2"], all_sup)
            if sug:
                plan.setdefault(sug["id"], []).append(r["tour_id"])
        total = sum(len(v) for v in plan.values())
        batch: list[tuple[str, str]] = [(sid, tid) for sid, tids in plan.items() for tid in tids][: lim.bulk_max]
        preview = [{"supplier_id": sid, "supplier_name": by_id[sid]["company_name"], "tours": len(tids)}
                   for sid, tids in plan.items()]
        if dry_run:
            return {"dry_run": True, "matches": total, "would_link": len(batch), "bulk_max": lim.bulk_max,
                    "by_supplier": preview}
        linked: list[str] = []
        async with conn.transaction():
            for sid in dict.fromkeys(s for s, _ in batch):
                tids = [t for s, t in batch if s == sid]
                linked += await _link(conn, tids, by_id[sid], actor=actor, reason=reason,
                                      action="raw_tour.supplier_linked_by_match")
    return {"dry_run": False, "matches": total, "linked": len(linked), "remaining": total - len(linked),
            "bulk_max": lim.bulk_max, "by_supplier": preview}


__all__ = ["WorkspaceError", "apply_suggestions", "checklist_for", "create_tour", "link_tours", "list_manual",
           "publish_check", "read_tour", "save_facts", "unlinked_report", "wording_of"]
