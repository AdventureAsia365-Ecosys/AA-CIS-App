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

The required list holds on every publish path, not only the draft publish: process_export (services/export/handler.py,
the one exit every publish goes through: S1 auto publish, Review Queue approve, version promote, recompose, the draft
publish) calls :func:`export_guard` on the version it publishes and refuses an incomplete hand-made tour with
:class:`RequiredMissingError` (the version leaves 'approved' for 'hitl', like the other refusals; S1 files a review).

Two people: the people who saved a hand-made tour's facts are its writers too (the facts are staff-written content,
not supplier input), so the draft publish counts them with the wording's editors (:func:`facts_editors`, the history
rows since the tour's last publish). While cis.content.publish_two_person is on, a facts save of a published hand-made
tour is refused (409 ``facts_two_person``): it would go live with no second person; and an automatic exit (the S1
auto publish, recompose) of a hand-made tour whose facts a person wrote since its last publish, or that was never
published, is refused (:class:`FactsReviewRequiredError`, S1 files it in the Review Queue). A facts draft that a second
person publishes is a follow-up.

Facts saves: a tour that was never published is a draft nobody outside sees, so the form saves as the person types
(autosave). Once it has a Master, a save is a change of a live tour: it needs a reason, keeps the required list complete
(409 ``required_missing``) and runs the source fan-out (the drawer's PATCH /admin/tours/{id}/raw refuses a hand-made
tour's facts: 409 ``manual_tour_use_workspace``). Every save that changes something writes one history row
(raw_tour_revision, staff_edit) and one audit row with the before / after. Two people saving the same form: the save
carries the ``saved_at`` it was loaded with (422 without it once the tour has one); another save in between is 409
``facts_changed``.
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
FACTS_TWO_PERSON = "facts_two_person"


class WorkspaceError(Exception):
    def __init__(self, status: int, code: str, message: str, extra: Optional[dict] = None):
        super().__init__(status, code, message, extra)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


class HandMadeRefusal(Exception):
    """process_export refused to publish a hand-made tour (:func:`export_guard`); the version left 'approved' for
    'hitl' before the refusal. The S1 auto publish files it in the Review Queue (admin_pipeline/_engine.py)."""

    code = "hand_made_refused"


class RequiredMissingError(HandMadeRefusal):
    """process_export refused to publish a hand-made tour whose required list is not complete; the version left
    'approved' for 'hitl' before the refusal (services/export/handler.py)."""

    code = "required_missing"

    def __init__(self, tour_id: str, version_id: str, gaps: list[dict]):
        super().__init__(tour_id, version_id, gaps)
        self.tour_id, self.version_id, self.gaps = tour_id, version_id, gaps

    def __str__(self) -> str:
        return _missing_message(self.gaps)

    def detail(self) -> dict:
        return {"code": self.code, "message": str(self), "items": _missing_items(self.gaps)}


class FactsReviewRequiredError(HandMadeRefusal):
    """While cis.content.publish_two_person is on, an automatic exit (S1 auto publish, recompose, lambda) refused a
    hand-made tour whose facts a person wrote since its last publish, or that was never published: no second person has
    seen those facts. The S1 auto publish files the version in the Review Queue, whose approve counts the facts
    writers in the two-person gate (api/routers/admin_pipeline/_publish.py review_publish_gate)."""

    code = "facts_review_required"

    def __init__(self, tour_id: str, version_id: str, writers: list[str]):
        super().__init__(tour_id, version_id, writers)
        self.tour_id, self.version_id, self.writers = tour_id, version_id, writers

    def __str__(self) -> str:
        return ("Two-person publishing is on: a person wrote this hand-made tour's facts, so a second person approves "
                "it in the Review Queue before it goes live")


def _missing_message(gaps: list[dict]) -> str:
    return "This hand-made tour is not complete: " + "; ".join(i["message"] for i in gaps)


def _missing_items(gaps: list[dict]) -> list[dict]:
    return [{"key": i["key"], "message": i["message"], "params": i["params"]} for i in gaps]


class _OneConn:
    """A pool-like view of one connection (the readers here take a pool): acquire() yields the connection itself, so a
    read in ``async with pool.acquire() as c, c.transaction()`` is a savepoint of the caller's transaction."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _cm():
            yield self._conn
        return _cm()


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
    from services.tour_lifecycle import open_batch, run_after_commit, supersede

    lim, opts = await policy.limits(), await policy.form_options()
    stored = F.normalize(form, tour_type_label=_tour_type_label(form.tour_type, {}, opts))
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
            # raw_tours.batch_id is a foreign key to shared.pipeline_runs: the tour's own batch row comes first
            batch_id = await open_batch(conn, tenant_id=MASTER_TENANT, batch_name="Content workspace",
                                        step_name="content_workspace")
            outcome = await supersede(conn, decision="create_new", tenant_id=MASTER_TENANT, batch_id=batch_id,
                                      payload=payload, matched_tour_id=None, matched_group_id=None, actor=actor)
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


def _tour_type_label(code: Optional[str], old: dict, opts: policy.FormOptions) -> Optional[str]:
    """The label of a chosen tour type: an option in force, or the type the tour already holds (an administrator may
    have removed it since; the tour keeps the label it was saved with). Anything else is 422 ``tour_type_unknown``."""
    if not code:
        return None
    option = opts.tour_type(code)
    if option is not None:
        return option["label"]
    if old.get("tour_type") == code:
        return old.get("tour_type_label") or None
    raise WorkspaceError(422, "tour_type_unknown", "Choose the tour type from the list",
                         {"allowed": [t["code"] for t in opts.tour_types]})


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
    from api.services import content_publish_gate as gate

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
        two_person = await gate.switch_on(conn)
    state = _state(row)
    if state == "live" and has_draft:
        state = "live_changes"
    published = state in ("live", "live_changes", "paused")
    facts_two_person = row["entry_origin"] == "manual" and published and two_person
    return {
        "tour_id": row["tour_id"], "entry_origin": row["entry_origin"], "state": state,
        "editable": row["entry_origin"] == "manual" and state != "retired" and not facts_two_person,
        "facts_two_person": facts_two_person,
        "reason_required": row["entry_origin"] == "manual" and published,
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

def _normal_reason(reason: Optional[str]) -> Optional[str]:
    """A facts save's reason, stripped: blank is None (no reason), anything else has 3 to 500 characters (422)."""
    why = (reason or "").strip() or None
    if why is not None and not 3 <= len(why) <= 500:
        raise WorkspaceError(422, "reason_length", "A reason has 3 to 500 characters")
    return why


def _check_savable(row, old: dict, *, expected_saved_at: Optional[str], reason: Optional[str],
                   two_person: bool = False) -> bool:
    """Refuse a save the tour cannot take; True when the tour is published (the save is a live change). While the
    two-person switch is on, a published tour's facts are not saved here: the save would go live with no second
    person (409 ``facts_two_person``)."""
    if row is None:
        raise WorkspaceError(404, "not_found", "Tour not found")
    if row["entry_origin"] != "manual":
        raise WorkspaceError(409, "not_hand_made", "Only a hand-made tour is edited here; change the facts of a sheet "
                             "tour in its drawer (Original tab)")
    state = _state(row)
    if state == "retired":
        raise WorkspaceError(409, "tour_retired", "This tour was deleted: restore it before you edit it")
    if old.get("saved_at") and not expected_saved_at:
        raise WorkspaceError(422, "expected_saved_at_required", "Say which save you changed (expected_saved_at): "
                             "reload the tour and save again")
    if expected_saved_at and old.get("saved_at") and expected_saved_at != old.get("saved_at"):
        raise WorkspaceError(409, "facts_changed", "Someone else saved this tour since you opened it: reload it to see "
                             "their changes", {"saved_at": old.get("saved_at")})
    live = state != "draft"
    if live and two_person:
        raise WorkspaceError(409, FACTS_TWO_PERSON, "Two-person publishing is on: a change to the facts of a published "
                             "hand-made tour would go live without a second person, so it cannot be saved here yet")
    if live and not reason:
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


async def _refuse_incomplete_live(pool, conn, tour_id: str, stored: dict, country: Optional[str], supplier_id,
                                  lim: policy.Limits) -> None:
    """A save of a published hand-made tour goes live at once (the source fan-out), so it must keep the tour's required
    list complete: the same list as the publish (the new facts, country and supplier; the open wording draft, else the
    live version), 409 ``required_missing`` with the missing items. A never-published tour keeps saving incomplete
    drafts (nobody outside sees it; the publish checks the list)."""
    supplier, _ok = await _supplier_of(pool, supplier_id)
    items = F.checklist(stored, limits=lim, country_ok=bool(country), supplier=supplier,
                        wording=await wording_of(conn, tour_id))
    gaps = F.missing(items)
    if gaps:
        raise WorkspaceError(409, "required_missing", _missing_message(gaps), {"items": _missing_items(gaps)})


async def save_facts(pool, tour_id: str, form: F.FactsIn, *, expected_saved_at: Optional[str], reason: Optional[str],
                     actor: str) -> dict:
    from api.routers.admin_pipeline.tour_detail import _source_fanout_raw, record_raw_revision
    from api.services import content_publish_gate as gate

    lim, opts = await policy.limits(), await policy.form_options()
    _check_size(F.normalize(form), lim)
    country, _iso2 = _country(form.country)
    why = _normal_reason(reason)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(_TOUR_SQL + " FOR UPDATE OF rt", tour_id)
            old = F.facts_from_row(row) if row is not None else {}
            live = _check_savable(row, old, expected_saved_at=expected_saved_at, reason=why,
                                  two_person=await gate.switch_on(conn))
            stored = F.normalize(form, tour_type_label=_tour_type_label(form.tour_type, old, opts))
            new_cols, sup, changed, hist_b, hist_a = await _plan_save(pool, row, form, stored, old, country)
            if not changed:
                return {"tour_id": tour_id, "saved": False, "saved_at": old.get("saved_at")}
            if live:                     # AA-1148 review: a live tour stays complete
                await _refuse_incomplete_live(pool, conn, tour_id, stored, country, sup, lim)
            stored["saved_at"] = _now_iso()
            cols = list(new_cols)
            sets = ", ".join(f"{c} = ${i + 2}" for i, c in enumerate(cols))
            n = len(cols)
            changed_at = await conn.fetchval(
                f"UPDATE silver_aa_internal.raw_tours SET {sets}, manual_facts = ${n + 2}::jsonb, "
                f"supplier_id = ${n + 3}::uuid WHERE tour_id = $1::uuid RETURNING now() AS changed_at",
                tour_id, *[new_cols[c] for c in cols], json.dumps(stored, ensure_ascii=False), sup)
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

# The entry origin read through to_jsonb: NULL before CIS 478 instead of an error, so the probe needs no savepoint and
# runs on any connection (process_export's own, a publish transaction).
_ORIGIN_SQL = """
    SELECT to_jsonb(rt) ->> 'entry_origin' AS origin FROM silver_aa_internal.raw_tours rt WHERE rt.tour_id = $1::uuid
"""


async def is_hand_made(conn, tour_id: str) -> bool:
    """True for a hand-made tour row (entry_origin 'manual'); False for a sheet tour, an unknown id or before CIS 478.
    The workspace's own probe of one row; the Excel fact-draft merge, merge-preview and publish refuse per key instead
    (AA-1181, services/tour_lifecycle/hand_made.py ``key_is_hand_made``)."""
    return await conn.fetchval(_ORIGIN_SQL, tour_id) == "manual"


async def _manual_row(conn, tour_id: str):
    """The tour row when it is a hand-made tour; None for a sheet tour, an unknown id or before CIS 478."""
    if not await is_hand_made(conn, tour_id):
        return None
    return await conn.fetchrow(_TOUR_SQL, tour_id)


async def required_gaps(conn, tour_id: str, *, wording: Optional[dict] = None, pool=None) -> Optional[list[dict]]:
    """The missing required items of a hand-made tour ([] = complete); None for any other tour. ``wording`` is the
    version about to be published (aa_name, aa_summary, aa_itineraries); without it, the open human draft or the live
    version. ``pool`` reads the supplier view; without it, ``conn`` (a savepoint, fail soft)."""
    row = await _manual_row(conn, tour_id)
    if row is None:
        return None
    lim = await policy.limits()
    supplier, _ok = await _supplier_of(pool or _OneConn(conn), row["supplier_id"])
    w = wording if wording is not None else await wording_of(conn, row["tour_id"])
    items = F.checklist(F.facts_from_row(row), limits=lim, country_ok=bool(row["country"]), supplier=supplier,
                        wording=w)
    return F.missing(items)


_EVER_PUBLISHED_SQL = """
    SELECT EXISTS (SELECT 1 FROM gold_aa_internal.published_tours pt WHERE pt.tour_id = $1::uuid) AS published
"""


async def _automatic_exit_refusal(conn, version_id: str, tour_id: str,
                                  contract_exit: Optional[str]) -> Optional[FactsReviewRequiredError]:
    """The two-person rule on an automatic exit of a complete hand-made tour: while cis.content.publish_two_person is
    on, a tour whose facts a person wrote since its last publish (:func:`facts_editors`), or that was never published
    (its creator typed every fact), never goes live without a second person. None when the exit is a person's (the
    draft publish, the approve and the promote run the publish gate with the facts writers) or the switch is off."""
    from api.services import content_publish_gate as gate
    from services.content_generation.human_lock import AUTOMATIC_EXITS

    if contract_exit not in AUTOMATIC_EXITS or not await gate.switch_on(conn):
        return None
    writers = await facts_editors(conn, tour_id)
    if writers or not await conn.fetchval(_EVER_PUBLISHED_SQL, tour_id):
        return FactsReviewRequiredError(tour_id, version_id, writers)
    return None


async def export_guard(conn, silver: str, version_id: str, tour_id: str, row: Any,
                       contract_exit: Optional[str] = None) -> None:
    """process_export (services/export/handler.py), on every exit (S1 auto publish, Review Queue approve, promote,
    recompose, the draft publish): a hand-made tour publishes only with its required list complete, checked on the
    version being published (:class:`RequiredMissingError`); on an automatic exit, while two-person publishing is on,
    only when no person wrote its facts since its last publish (:class:`FactsReviewRequiredError`). A refused version
    leaves 'approved' for 'hitl' (as the other publish refusals do) before any publish write. A sheet tour, and a
    database before CIS 478, pass."""
    get = row.get if hasattr(row, "get") else (lambda k: row[k])
    gaps = await required_gaps(conn, tour_id, wording={k: get(k) for k in ("aa_name", "aa_summary", "aa_itineraries")})
    if gaps is None:                     # not a hand-made tour
        return
    refusal = (RequiredMissingError(tour_id, version_id, gaps) if gaps
               else await _automatic_exit_refusal(conn, version_id, tour_id, contract_exit))
    if refusal is None:
        return
    logger.warning("export_refused_hand_made", version_id=version_id, tour_id=tour_id, exit=contract_exit,
                   code=refusal.code, missing=[g["key"] for g in gaps])
    await conn.execute(f"""
        UPDATE {silver}.generated_content
        SET status = 'hitl'
        WHERE id = $1::uuid AND status = 'approved'
    """, version_id)
    raise refusal


async def publish_check(pool, conn, tour_id: str) -> None:
    """In the draft publish transaction: a hand-made tour publishes only with its required list complete (409
    ``required_missing`` with the missing items); a sheet tour is not checked here. process_export checks the same
    list again on every publish path (:class:`RequiredMissingError`)."""
    gaps = await required_gaps(conn, tour_id, pool=pool)
    if gaps:
        raise WorkspaceError(409, "required_missing", _missing_message(gaps), {"items": _missing_items(gaps)})


_FACTS_EDITORS_SQL = """
    SELECT DISTINCT r.actor
      FROM silver_aa_internal.raw_tour_revision r
      LEFT JOIN gold_aa_internal.published_tours pt ON pt.tour_id = r.tour_id
     WHERE r.tour_id = $1::uuid AND r.source = 'staff_edit'
       AND (pt.tour_id IS NULL OR r.created_at > pt.updated_at)
"""
_CREATOR_SQL = """
    SELECT a.actor FROM acp_shared.audit_log a
     WHERE a.action = 'raw_tour.manual_created' AND a.resource_id = $1
       AND NOT EXISTS (SELECT 1 FROM gold_aa_internal.published_tours pt WHERE pt.tour_id = $2::uuid)
"""


async def facts_editors(conn, tour_id: str) -> list[str]:
    """The people who wrote a hand-made tour's facts since its last publish (the history rows of its facts saves and
    supplier links, staff_edit; and its creator while it was never published); [] for any other tour. The draft publish
    counts them as writers of what goes live (the two-person rule)."""
    import asyncpg

    if await _manual_row(conn, tour_id) is None:
        return []
    try:
        async with conn.transaction():   # a savepoint: a missing history table never fails the publish
            rows = await conn.fetch(_FACTS_EDITORS_SQL, tour_id)
            creator = await conn.fetch(_CREATOR_SQL, str(tour_id), tour_id)
    except (asyncpg.exceptions.UndefinedTableError, asyncpg.exceptions.UndefinedColumnError):
        return []
    return sorted({r["actor"] for r in [*rows, *creator] if r["actor"]})


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


__all__ = ["FACTS_TWO_PERSON", "FactsReviewRequiredError", "HandMadeRefusal", "RequiredMissingError", "WorkspaceError",
           "apply_suggestions", "checklist_for",
           "create_tour", "export_guard", "facts_editors", "link_tours", "list_manual", "publish_check", "read_tour",
           "required_gaps", "save_facts", "unlinked_report", "wording_of"]
