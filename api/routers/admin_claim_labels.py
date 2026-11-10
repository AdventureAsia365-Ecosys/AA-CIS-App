"""AA-1048 W2 (S273) — Writing Lab › claim labels: the staff API over the claim-level ground truth
(shared.eval_claim_label, CIS 388; services/ground_truth/). The same panel / store code as `scripts/aa1048/cli.py`,
reachable without a DB login: scripts/ is not in the image and the pilot PII guard refuses a data bundle sent to an
ECS exec (services/ground_truth/importer.py says why this path).

  POST  /admin/writing-memory/claim-labels/import       {set_id, set_version, dry_run (default true), export,
                                                        judgements}: validate, consolidate the 3 lenses, draw the
                                                        20% audit and (dry_run false) write; idempotent (a stored
                                                        judgement with the same class and label is `unchanged`);
                                                        counts only in the answer, one audit_log row per applied run
  GET   /admin/writing-memory/claim-labels/split-plan   the T9 pieces and T2 versions a golden-set addendum can
                                                        freeze, with their tour and split, and the plan to reach the
                                                        held-out target per writer (ids, counts, dollars; no text)
  GET   /admin/writing-memory/claim-labels/{set_id}/{set_version}/report   counts per label / labeller / lens /
                                                        state / split / writer, agreement per class, the panel
                                                        error rate, contradictions (claim hashes) and the manifest
  POST  /admin/writing-memory/claim-labels/{set_id}/{set_version}/enqueue  {dry_run, split, writers, max_open}:
                                                        disputed and audit-drawn claims into the disagreement queue
                                                        (source golden_claim), in adjudication order (held-out, T9,
                                                        then by object); max_open caps the open items of the set (one
                                                        batch of tech-lead work), a later call tops it up
  POST  /admin/writing-memory/claim-labels/{set_id}/{set_version}/pull-resolutions  {dry_run}: resolved queue
                                                        items -> tech-lead rows
  GET   /admin/writing-memory/claim-labels/{set_id}/{set_version}/finals?audit_ok=   the release-evidence labels
                                                        WITH claim text (tenant text: Writing Lab text read, logged)

AA-1063 (S275) — the Writing Lab review view, agent proposals, the label set browser and corrections
(services/ground_truth/review.py, CIS 394):
  GET   /admin/writing-memory/claim-labels/review?set_id=&set_version=&writer=&state=&proposal=&limit=&offset=
                                                        golden_claim queue items in adjudication order with the
                                                        sentence, context, evidence, lenses, newest proposal and
                                                        decision; total and facets (Lab text read, logged)
  GET   /admin/writing-memory/claim-labels/review/{item_id}/context   the full source of one item (logged)
  POST  /admin/writing-memory/claim-labels/review/{item_id}/answer    {answer: correct | wrong | unsure, note?,
                                                        proposal_id?}: resolve + tech-lead row in one transaction
  POST  /admin/writing-memory/claim-labels/review/accept-proposals    {item_ids}: each open item answered with its
                                                        newest proposal (at most 500)
  POST  /admin/writing-memory/claim-labels/proposals/import           {run_id, dry_run (default true), set_id?,
                                                        set_version?, proposals}: an agent panel's proposals
  GET   /admin/writing-memory/claim-labels/sets         every label set version with its counts (no text)
  GET   /admin/writing-memory/claim-labels/{set_id}/{set_version}/claims?source=&label=&writer=&claim_class=&split=
                                                        &relabelled=&q=&limit=&offset=   every labelled claim with who
                                                        decided it, when and why, and its history (logged)
  POST  /admin/writing-memory/claim-labels/{set_id}/{set_version}/claims/{claim_hash}/relabel   {label,
                                                        claim_class?, reason, supersedes}: an append-only correction
The report and finals routes read the newest correction of a claim as its final label (review.effective_rows).
Writes need a signed-in admin (x-admin-user-id) and ai.admin; 503 `unavailable` before CIS 394.

Who may call (api/core/staff_authz.py POLICY): writes ai.admin; the counts reads lab.read (or tenant_content.read);
the finals read tenant_content.read or lab.tenant_text.read (logged first, api/core/lab_read_log.py).
Errors: 422 a bad payload (nothing written), 409 the version already holds other labels for these claims (a relabel is
a new set version), 503 `unavailable` before CIS 388.
The router is included without an include prefix (staff_policy reads the route template from scope["route"].path).
"""
from __future__ import annotations

from typing import Any, Literal, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core.lab_read_log import LAB_TEXT_LOGGED
from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from services.ground_truth import importer
from services.ground_truth import panel as pn
from services.ground_truth import review as rv
from services.ground_truth import split_plan as sp
from services.ground_truth import store

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/writing-memory/claim-labels", tags=["admin-claim-labels"],
                   dependencies=[Depends(verify_admin_secret), Depends(staff_policy)])

SET_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9@._-]{0,31}$"
SET_VERSION_PATTERN = r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$"


def _pool(request: Request):
    return request.app.state.pool


def _actor(raw: Optional[str]) -> str:
    from services.writing_memory.signals import admin_actor_id

    return f"admin:{admin_actor_id(raw)}"


def _set(set_id: str, set_version: str) -> tuple[str, str]:
    from services.ground_truth.claims import valid_set

    if not valid_set(set_id, set_version):
        raise HTTPException(status_code=404, detail="Unknown label set")
    return set_id, set_version


def _unavailable(e: Exception, mig: str = "CIS 388"):
    logger.warning("aa1048_claim_labels_unavailable", error=f"{type(e).__name__}: {str(e)[:200]}")
    return HTTPException(status_code=503, detail={"detail": f"The claim-label tables are not applied yet ({mig})",
                                                  "code": "unavailable"})


def _need_actor(raw: Optional[str]) -> str:
    """AA-1063: a review write needs a signed-in admin (the id the BFF forwards from the verified JWT)."""
    from services.writing_memory.signals import admin_actor_id

    actor = admin_actor_id(raw)
    if actor == "unknown":
        raise HTTPException(status_code=403, detail={"detail": "This action needs a signed-in admin (no admin id "
                                                               "on the request)", "code": "no_actor"})
    return f"admin:{actor}"


def _review_error(e: "rv.ReviewError"):
    return HTTPException(status_code=e.status, detail={"detail": e.detail, "code": e.code})


def _uuid_or_404(value: str, what: str) -> str:
    from uuid import UUID

    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=404, detail=f"Unknown {what}")


def _refused(e: Exception, status: int, code: str):
    return HTTPException(status_code=status, detail={"detail": str(e)[:2000], "code": code})


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_: Optional[str] = Field(None, alias="schema", max_length=64)
    set_id: str = Field(..., pattern=SET_ID_PATTERN)
    set_version: str = Field(..., pattern=SET_VERSION_PATTERN)
    dry_run: bool = True
    refs_dropped: int = Field(0, ge=0)      # built by `cli payload` (importer.clean_refs); informative only
    export: list[dict[str, Any]] = Field(..., min_length=1, max_length=importer.MAX_EXPORT_LINES)
    judgements: list[dict[str, Any]] = Field(..., min_length=1, max_length=importer.MAX_JUDGEMENTS)


class DryRunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = True


class EnqueueBody(DryRunBody):
    """S273 (decision "t9-heldout-first"): one batch of adjudication work. Without the three filters every disputed
    and audit claim of the set is queued, as before."""
    split: Optional[Literal["held_out", "train"]] = None
    writers: Optional[list[Literal["a1", "t2", "t9_blog", "t9_social"]]] = Field(None, min_length=1, max_length=4)
    max_open: Optional[int] = Field(None, ge=1, le=5000)


@router.post("/import")
async def import_labels(body: ImportBody, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    payload = body.model_dump(exclude={"schema_", "refs_dropped"})
    try:
        async with _pool(request).acquire() as conn:
            result = await importer.import_labels(conn, payload, actor=_actor(x_admin_user_id))
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    except store.StoreRefused as e:
        raise _refused(e, 409, "set_version_holds_other_labels")
    except pn.PanelError as e:
        raise _refused(e, 422, "bad_payload")
    logger.info("aa1048_claim_labels_import", set=result["set"], dry_run=result["dry_run"], rows=result["rows"],
                inserted=result["inserted"], unchanged=result["unchanged"])
    return result


@router.get("/split-plan")
async def split_plan(request: Request,
                     violation_rate: float = Query(sp.PANEL1_VIOLATION_RATE, gt=0, le=1),
                     observed_precision: float = Query(sp.DEFAULT_OBSERVED_PRECISION, ge=0.9, le=1),
                     per_tour_cap: int = Query(sp.DEFAULT_PER_TOUR_CAP, ge=1, le=100)):
    target = sp.target_held_out_claims(violation_rate=violation_rate, observed_precision=observed_precision)
    if target is None:
        raise HTTPException(status_code=422, detail="The bar is out of reach at this precision / rate")
    async with _pool(request).acquire() as conn:
        cands = await sp.live_candidates(conn)
    result = sp.plan(cands, target_claims=target, per_tour_cap=per_tour_cap)
    return {**result, "violation_rate": violation_rate, "observed_precision": observed_precision,
            "candidates": [c.as_dict() for c in cands]}


# ── AA-1063: review view, proposals, label set browser ────────────────────────────────────────────────────────────
# Literal paths before the /{set_id}/{set_version}/... routes.

WriterLit = Literal["a1", "t2", "t9_blog", "t9_social"]
LabelLit = Literal["violation", "ok", "unsure"]


class AnswerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: Literal["correct", "wrong", "unsure"]
    note: Optional[str] = Field(None, max_length=400)
    proposal_id: Optional[str] = Field(None, max_length=36)


class AcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_ids: list[str] = Field(..., min_length=1, max_length=rv.MAX_BULK)


class ProposalsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(..., min_length=1, max_length=64)
    dry_run: bool = True
    set_id: Optional[str] = Field(None, pattern=SET_ID_PATTERN)
    set_version: Optional[str] = Field(None, pattern=SET_VERSION_PATTERN)
    proposals: list[dict[str, Any]] = Field(..., min_length=1, max_length=rv.MAX_PROPOSALS)


class RelabelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: LabelLit
    claim_class: Optional[str] = Field(None, max_length=48)
    reason: str = Field(..., min_length=3, max_length=600)
    supersedes: Optional[str] = Field(None, max_length=36)


@router.get("/review", dependencies=LAB_TEXT_LOGGED)
async def review_items(request: Request, set_id: Optional[str] = Query(None, pattern=SET_ID_PATTERN),
                       set_version: Optional[str] = Query(None, pattern=SET_VERSION_PATTERN),
                       writer: Optional[WriterLit] = None,
                       state: Literal["open", "resolved", "all"] = "open",
                       proposal: Optional[Literal["ok", "violation", "unsure", "none"]] = None,
                       limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
    try:
        async with _pool(request).acquire() as conn:
            return await rv.list_items(conn, set_id=set_id, set_version=set_version, writer=writer, state=state,
                                       proposal=proposal, limit=limit, offset=offset)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e, "CIS 394")


@router.get("/review/{item_id}/context", dependencies=LAB_TEXT_LOGGED)
async def review_context(item_id: str, request: Request):
    iid = _uuid_or_404(item_id, "claim item")
    try:
        async with _pool(request).acquire() as conn:
            return await rv.item_context(conn, iid)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    except rv.ReviewError as e:
        raise _review_error(e)


@router.post("/review/accept-proposals")
async def review_accept(body: AcceptBody, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    actor = _need_actor(x_admin_user_id)
    ids = [_uuid_or_404(i, "claim item") for i in body.item_ids]
    try:
        async with _pool(request).acquire() as conn:
            result = await rv.accept_proposals(conn, ids, actor=actor)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e, "CIS 394")
    except rv.ReviewError as e:
        raise _review_error(e)
    logger.info("aa1063_claim_review_accept", items=len(ids), accepted=result["accepted"],
                refused=len(result["refused"]), no_proposal=result["no_proposal"])
    return result


@router.post("/review/{item_id}/answer")
async def review_answer(item_id: str, body: AnswerBody, request: Request,
                        x_admin_user_id: Optional[str] = Header(None)):
    actor = _need_actor(x_admin_user_id)
    iid = _uuid_or_404(item_id, "claim item")
    pid = _uuid_or_404(body.proposal_id, "proposal") if body.proposal_id else None
    try:
        async with _pool(request).acquire() as conn:
            result = await rv.answer_item(conn, iid, answer=body.answer, actor=actor, note=body.note, proposal_id=pid)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e, "CIS 394")
    except rv.ReviewError as e:
        raise _review_error(e)
    except store.StoreRefused as e:
        raise _refused(e, 409, "set_version_holds_other_labels")
    logger.info("aa1063_claim_review_answer", item_id=iid, answer=body.answer, resolution=result["resolution"],
                inserted=result["inserted"])
    return result


@router.post("/proposals/import")
async def proposals_import(body: ProposalsBody, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    actor = _need_actor(x_admin_user_id)
    try:
        async with _pool(request).acquire() as conn:
            result = await rv.import_proposals(conn, run_id=body.run_id, proposals=body.proposals,
                                               apply=not body.dry_run, actor=actor, set_id=body.set_id,
                                               set_version=body.set_version)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e, "CIS 394")
    except rv.ReviewError as e:
        raise _review_error(e)
    logger.info("aa1063_claim_proposals_import", run_id=body.run_id, dry_run=result["dry_run"], new=result["new"],
                inserted=result["inserted"], not_queued=result["not_queued"])
    return result


@router.get("/sets")
async def label_sets(request: Request):
    try:
        async with _pool(request).acquire() as conn:
            return {"sets": await rv.list_sets(conn)}
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)


@router.get("/{set_id}/{set_version}/report")
async def report(set_id: str, set_version: str, request: Request):
    set_id, set_version = _set(set_id, set_version)
    try:
        async with _pool(request).acquire() as conn:
            rows = await store.read_rows(conn, set_id, set_version)
            relabels = await rv.read_relabels_soft(conn, set_id, set_version)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    # AA-1063: counts over the stored label rows; everything that reads a final label reads the newest correction
    eff = rv.effective_rows(rows, relabels)
    finals = pn.final_labels(eff)
    labels_by_class: dict[str, dict[str, int]] = {}
    for f in finals.values():
        cls = labels_by_class.setdefault(f.claim_class or "-", {})
        cls[f.label] = cls.get(f.label, 0) + 1
    return {"set": f"{set_id}@{set_version}", "rows": len(rows), "relabels": len(relabels), **importer.counts(rows),
            "evidence_sources": {s: sum(1 for f in finals.values() if f.source == s) for s in rv.SOURCES},
            "agreement": pn.agreement_report(eff), "contradictions": pn.contradictions(finals),
            "labels_by_class": labels_by_class,
            "manifest": pn.manifest(eff, set_id=set_id, set_version=set_version)}


@router.post("/{set_id}/{set_version}/enqueue")
async def enqueue(set_id: str, set_version: str, body: EnqueueBody, request: Request):
    from datetime import date

    set_id, set_version = _set(set_id, set_version)
    try:
        async with _pool(request).acquire() as conn:
            rows = await store.read_rows(conn, set_id, set_version)
            async with conn.transaction():
                result = await store.enqueue(conn, rows, today=date.today(), apply=not body.dry_run,
                                             split=body.split, writers=body.writers, max_open=body.max_open)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    logger.info("aa1048_claim_labels_enqueue", set=f"{set_id}@{set_version}", candidates=result["candidates"],
                added=result["added"], dry_run=result["dry_run"], remaining=result.get("remaining"),
                max_open=body.max_open, split=body.split)
    return result


@router.post("/{set_id}/{set_version}/pull-resolutions")
async def pull_resolutions(set_id: str, set_version: str, body: DryRunBody, request: Request):
    from services.ground_truth import policy

    set_id, set_version = _set(set_id, set_version)
    try:
        async with _pool(request).acquire() as conn:
            result = await store.pull_resolutions(conn, set_id, set_version, apply=not body.dry_run,
                                                  policy_sha=policy.policy_sha())
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    except store.StoreRefused as e:
        raise _refused(e, 409, "set_version_holds_other_labels")
    logger.info("aa1048_claim_labels_pull", set=f"{set_id}@{set_version}", inserted=result.get("inserted"),
                resolved_items=result.get("resolved_items"))
    return result


@router.get("/{set_id}/{set_version}/finals", dependencies=LAB_TEXT_LOGGED)
async def finals(set_id: str, set_version: str, request: Request, audit_ok: bool = False):
    """The labels that count as release evidence (tech lead; agreed panel labels too with audit_ok), in the
    `cli finals` file shape: the orchestrator saves the answer to the gitignored scratch folder for `cli detectors`
    and `r1_calibrate`."""
    set_id, set_version = _set(set_id, set_version)
    try:
        async with _pool(request).acquire() as conn:
            rows = await store.read_rows(conn, set_id, set_version)
            relabels = await rv.read_relabels_soft(conn, set_id, set_version)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    kept = pn.release_evidence(pn.final_labels(rv.effective_rows(rows, relabels)), audit_ok=audit_ok)
    return {"set": f"{set_id}@{set_version}", "audit_ok": audit_ok, "finals": importer.finals_rows(kept)}


@router.get("/{set_id}/{set_version}/claims", dependencies=LAB_TEXT_LOGGED)
async def set_claims(set_id: str, set_version: str, request: Request,
                     source: Optional[Literal["tech_lead", "panel_agreed", "none"]] = None,
                     label: Optional[LabelLit] = None, writer: Optional[WriterLit] = None,
                     claim_class: Optional[str] = Query(None, max_length=48),
                     split: Optional[Literal["held_out", "train"]] = None, relabelled: Optional[bool] = None,
                     q: Optional[str] = Query(None, max_length=200),
                     limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100000)):
    """AA-1063 label set browser: every labelled claim of a set version (Lab text read, logged)."""
    set_id, set_version = _set(set_id, set_version)
    try:
        async with _pool(request).acquire() as conn:
            rows = await store.read_rows(conn, set_id, set_version)
            relabels = await rv.read_relabels_soft(conn, set_id, set_version)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    entries = rv.claim_entries(rows, relabels)
    kept = rv.filter_claims(entries, source=source, label=label, writer=writer, claim_class=claim_class, split=split,
                            relabelled=relabelled, q=q)
    return {"set": f"{set_id}@{set_version}", "total": len(kept), "limit": limit, "offset": offset,
            "classes": sorted({e["claim_class"] for e in entries if e["claim_class"]}),
            "summary": rv.set_summary(rows, relabels), "claims": kept[offset:offset + limit]}


@router.post("/{set_id}/{set_version}/claims/{claim_hash}/relabel")
async def relabel_claim(set_id: str, set_version: str, claim_hash: str, body: RelabelBody, request: Request,
                        x_admin_user_id: Optional[str] = Header(None)):
    """AA-1063: change a claim's final label (append only; `supersedes` = the current head correction or null)."""
    from services.ground_truth.claims import valid_hex64

    actor = _need_actor(x_admin_user_id)
    set_id, set_version = _set(set_id, set_version)
    if not valid_hex64(claim_hash):
        raise HTTPException(status_code=404, detail="Unknown claim")
    sup = _uuid_or_404(body.supersedes, "correction") if body.supersedes else None
    try:
        async with _pool(request).acquire() as conn:
            result = await rv.relabel(conn, set_id, set_version, claim_hash, label=body.label,
                                      claim_class=body.claim_class, reason=body.reason, supersedes=sup, actor=actor)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e, "CIS 394")
    except rv.ReviewError as e:
        raise _review_error(e)
    logger.info("aa1063_claim_relabel", set=f"{set_id}@{set_version}", relabel_id=result["relabel_id"],
                previous_source=result["previous_source"])
    return result


__all__ = ["router"]
