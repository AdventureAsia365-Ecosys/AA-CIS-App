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
  POST  /admin/writing-memory/claim-labels/{set_id}/{set_version}/enqueue  {dry_run}: disputed and audit-drawn
                                                        claims into the disagreement queue (source golden_claim)
  POST  /admin/writing-memory/claim-labels/{set_id}/{set_version}/pull-resolutions  {dry_run}: resolved queue
                                                        items -> tech-lead rows
  GET   /admin/writing-memory/claim-labels/{set_id}/{set_version}/finals?audit_ok=   the release-evidence labels
                                                        WITH claim text (tenant text: Writing Lab text read, logged)

Who may call (api/core/staff_authz.py POLICY): writes ai.admin; the counts reads lab.read (or tenant_content.read);
the finals read tenant_content.read or lab.tenant_text.read (logged first, api/core/lab_read_log.py).
Errors: 422 a bad payload (nothing written), 409 the version already holds other labels for these claims (a relabel is
a new set version), 503 `unavailable` before CIS 388.
The router is included without an include prefix (staff_policy reads the route template from scope["route"].path).
"""
from __future__ import annotations

from typing import Any, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core.lab_read_log import LAB_TEXT_LOGGED
from api.core.staff_authz import staff_policy
from api.routers.admin import verify_admin_secret
from services.ground_truth import importer
from services.ground_truth import panel as pn
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


def _unavailable(e: Exception):
    logger.warning("aa1048_claim_labels_unavailable", error=f"{type(e).__name__}: {str(e)[:200]}")
    return HTTPException(status_code=503, detail={"detail": "The claim-label table is not applied yet (CIS 388)",
                                                  "code": "unavailable"})


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


@router.get("/{set_id}/{set_version}/report")
async def report(set_id: str, set_version: str, request: Request):
    set_id, set_version = _set(set_id, set_version)
    try:
        async with _pool(request).acquire() as conn:
            rows = await store.read_rows(conn, set_id, set_version)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    finals = pn.final_labels(rows)
    labels_by_class: dict[str, dict[str, int]] = {}
    for f in finals.values():
        cls = labels_by_class.setdefault(f.claim_class or "-", {})
        cls[f.label] = cls.get(f.label, 0) + 1
    return {"set": f"{set_id}@{set_version}", "rows": len(rows), **importer.counts(rows),
            "agreement": pn.agreement_report(rows), "contradictions": pn.contradictions(finals),
            "labels_by_class": labels_by_class,
            "manifest": pn.manifest(rows, set_id=set_id, set_version=set_version)}


@router.post("/{set_id}/{set_version}/enqueue")
async def enqueue(set_id: str, set_version: str, body: DryRunBody, request: Request):
    from datetime import date

    set_id, set_version = _set(set_id, set_version)
    try:
        async with _pool(request).acquire() as conn:
            rows = await store.read_rows(conn, set_id, set_version)
            async with conn.transaction():
                result = await store.enqueue(conn, rows, today=date.today(), apply=not body.dry_run)
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    logger.info("aa1048_claim_labels_enqueue", set=f"{set_id}@{set_version}", **result)
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
    except asyncpg.UndefinedTableError as e:
        raise _unavailable(e)
    kept = pn.release_evidence(pn.final_labels(rows), audit_ok=audit_ok)
    return {"set": f"{set_id}@{set_version}", "audit_ok": audit_ok, "finals": importer.finals_rows(kept)}


__all__ = ["router"]
