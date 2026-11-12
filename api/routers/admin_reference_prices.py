"""AA-1091 item 5 — AA Catalogue › Reference prices: the structured reference price of the AA tours
(silver_aa_internal.tour_reference_price, CIS 401) for the customer-service team.

  GET    /admin/reference-prices?status=&country=&q=&live_only=&page=&page_size=
             the tours of the page filter with their source text, saved price, status and the parser's suggestion;
             counts per status and the countries of the whole filtered set
  PATCH  /admin/reference-prices/{source_key}   {amount, currency, basis, note?, expected_updated_at?}: save (confirmed)
  DELETE /admin/reference-prices/{source_key}   remove the saved price (the source text stays)
  POST   /admin/reference-prices/accept         {source_keys: [...]}: confirm saved prices / save the suggestions
  POST   /admin/reference-prices/parse          {dry_run: true, live_only: false}: parse every source text with no
                                                saved price (DRY RUN first; confirmed and staff entries never touched)
  GET    /admin/reference-prices/export?status=&country=&q=&live_only=
             the .xlsx of the filter as base64 JSON {filename, content_base64, rows} (the REST API Gateway in front of
             the API is not binary safe, AA-741; the page decodes it)
  POST   /admin/reference-prices/import         {file_base64, dry_run: true}: per-line plan (create / update /
                                                unchanged / skip / error); an apply needs a file with no error line
  GET    /admin/reference-prices/{source_key}/history   the audit rows of one key, newest first

Root ADR 0005 d7: a reference price, never a selling price; the free text stays in raw_tours.price_raw. Who may call
(api/core/staff_authz.py POLICY): reads reference_price.read (admin, content_lead, content, customer_success, finance,
viewer), writes reference_price.write (admin, content_lead, customer_success); the viewer is refused on every write.
Every change leaves one acp_shared.audit_log row per key (tenant_id NULL, actor admin:<staff id>, before / after) in the
write's transaction. Before CIS 401 is applied every route answers 503. AA-Booking reads the saved values through the
contract view catalog.v_tour_price_reference_v2 (CIS 402). Included without a prefix (staff_policy reads the route).
"""
from __future__ import annotations

import base64
import json
from typing import Any, Optional

import asyncpg
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from api.core.staff_authz import staff_policy
from services.acp_shared.audit_log import PlatformAuditAction, write_audit_log
from services.reference_price import store, workbook
from services.reference_price.store import ReferencePriceError

logger = structlog.get_logger()
router = APIRouter(tags=["admin-reference-prices"], dependencies=[Depends(staff_policy)])

RESOURCE = "tour_reference_price"
_NOT_MIGRATED = "Reference prices are not available yet: the database update (CIS 401) is not applied"
_ACTIONS = {
    "saved": PlatformAuditAction.REFERENCE_PRICE_SAVED,
    "confirmed": PlatformAuditAction.REFERENCE_PRICE_CONFIRMED,
    "cleared": PlatformAuditAction.REFERENCE_PRICE_CLEARED,
    "parsed": PlatformAuditAction.REFERENCE_PRICE_PARSED,
    "imported": PlatformAuditAction.REFERENCE_PRICE_IMPORTED,
}
_B64_MAX = (workbook.MAX_FILE_BYTES * 4) // 3 + 8


def _actor(x_admin_user_id: Optional[str]) -> str:
    return f"admin:{(x_admin_user_id or 'admin').strip()[:56] or 'admin'}"


def _pool(request: Request):
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        raise HTTPException(status_code=503, detail="DB not ready")
    return pool


def _refused(e: ReferencePriceError) -> HTTPException:
    return HTTPException(status_code=e.status, detail=e.detail)


async def _audit(conn, actor: str, changes: list[store.Change], extra: Optional[dict] = None) -> None:
    for c in changes:
        details: dict[str, Any] = {"before": c.before, "after": c.after}
        if extra:
            details.update(extra)
        await write_audit_log(conn, tenant_id=None, actor=actor, action=_ACTIONS[c.action], resource_type=RESOURCE,
                              resource_id=c.source_key, details=details)


class RefPriceSaveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Any
    currency: str = Field(max_length=8)
    basis: str = Field(max_length=32)
    note: Optional[str] = Field(default=None, max_length=store.MAX_NOTE * 2)
    expected_updated_at: Optional[str] = Field(default=None, max_length=64)


class RefPriceAcceptBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_keys: list[str] = Field(min_length=1, max_length=store.MAX_BULK)


class RefPriceParseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry_run: bool = True
    live_only: bool = False


class RefPriceImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    file_base64: str = Field(min_length=1, max_length=_B64_MAX)
    dry_run: bool = True


_StatusQ = Query("all", max_length=20)
_CountryQ = Query(None, max_length=80)
_SearchQ = Query(None, max_length=200)


@router.get("/admin/reference-prices")
async def list_reference_prices(request: Request, status: str = _StatusQ, country: Optional[str] = _CountryQ,
                                q: Optional[str] = _SearchQ, live_only: bool = True,
                                page: int = Query(1, ge=1, le=10000), page_size: int = Query(50, ge=1, le=200)):
    try:
        async with _pool(request).acquire() as conn:
            items = await store.list_items(conn)
        return store.page_of(items, status=status, country=country, q=q, live_only=live_only, page=page,
                             page_size=page_size)
    except asyncpg.exceptions.UndefinedTableError:
        raise HTTPException(status_code=503, detail=_NOT_MIGRATED) from None
    except ReferencePriceError as e:
        raise _refused(e) from None


@router.get("/admin/reference-prices/export")
async def export_reference_prices(request: Request, status: str = _StatusQ, country: Optional[str] = _CountryQ,
                                  q: Optional[str] = _SearchQ, live_only: bool = True):
    try:
        async with _pool(request).acquire() as conn:
            items = await store.list_items(conn)
        page = store.page_of(items, status=status, country=country, q=q, live_only=live_only, page=1,
                             page_size=len(items) or 1)
    except asyncpg.exceptions.UndefinedTableError:
        raise HTTPException(status_code=503, detail=_NOT_MIGRATED) from None
    except ReferencePriceError as e:
        raise _refused(e) from None
    content = workbook.export_bytes(page["items"])
    return {"filename": workbook.export_filename(), "rows": len(page["items"]),
            "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "content_base64": base64.b64encode(content).decode("ascii")}


@router.post("/admin/reference-prices/accept")
async def accept_reference_prices(body: RefPriceAcceptBody, request: Request,
                                  x_admin_user_id: Optional[str] = Header(None)):
    actor = _actor(x_admin_user_id)
    try:
        async with _pool(request).acquire() as conn, conn.transaction():
            changes, skipped = await store.confirm(conn, body.source_keys, actor=actor)
            await _audit(conn, actor, changes)
    except asyncpg.exceptions.UndefinedTableError:
        raise HTTPException(status_code=503, detail=_NOT_MIGRATED) from None
    except ReferencePriceError as e:
        raise _refused(e) from None
    logger.info("reference_price_accepted", confirmed=len(changes), skipped=len(skipped))
    return {"confirmed": [c.source_key for c in changes], "skipped": skipped}


@router.post("/admin/reference-prices/parse")
async def parse_reference_prices(body: RefPriceParseBody, request: Request,
                                 x_admin_user_id: Optional[str] = Header(None)):
    actor = _actor(x_admin_user_id)
    try:
        async with _pool(request).acquire() as conn, conn.transaction():
            summary, changes = await store.parse_missing(conn, actor=actor, dry_run=body.dry_run,
                                                         live_only=body.live_only)
            await _audit(conn, actor, changes)
    except asyncpg.exceptions.UndefinedTableError:
        raise HTTPException(status_code=503, detail=_NOT_MIGRATED) from None
    logger.info("reference_price_parse", dry_run=body.dry_run, changed=len(changes))
    return summary


@router.post("/admin/reference-prices/import")
async def import_reference_prices(body: RefPriceImportBody, request: Request,
                                  x_admin_user_id: Optional[str] = Header(None)):
    actor = _actor(x_admin_user_id)
    try:
        content = base64.b64decode(body.file_base64, validate=True)
    except ValueError:   # binascii.Error is a ValueError
        raise HTTPException(status_code=400, detail="The file is not valid base64") from None
    try:
        lines = workbook.read_rows(content)
    except workbook.WorkbookError as e:
        raise HTTPException(status_code=422, detail=str(e)) from None
    try:
        async with _pool(request).acquire() as conn, conn.transaction():
            plan, values = await store.plan_import(conn, lines)
            applied: list[store.Change] = []
            if not body.dry_run:
                if plan.errors:
                    raise HTTPException(status_code=422, detail={
                        "message": f"{plan.errors} line(s) have errors: fix them and import again (nothing was saved)",
                        "summary": plan.summary(), "rows": plan.rows})
                applied = await store.apply_import(conn, values, actor=actor)
                await _audit(conn, actor, applied, {"import_lines": len(lines)})
    except asyncpg.exceptions.UndefinedTableError:
        raise HTTPException(status_code=503, detail=_NOT_MIGRATED) from None
    except ReferencePriceError as e:
        raise _refused(e) from None
    logger.info("reference_price_import", dry_run=body.dry_run, lines=len(lines), changes=plan.changes,
                errors=plan.errors, applied=len(applied))
    return {"dry_run": body.dry_run, "applied": len(applied), "summary": plan.summary(), "rows": plan.rows}


@router.patch("/admin/reference-prices/{source_key}")
async def save_reference_price(source_key: str, body: RefPriceSaveBody, request: Request,
                               x_admin_user_id: Optional[str] = Header(None)):
    actor = _actor(x_admin_user_id)
    try:
        values = store.validate(body.amount, body.currency, body.basis, body.note)
        async with _pool(request).acquire() as conn, conn.transaction():
            change = await store.save(conn, source_key, values, actor=actor,
                                      expected_updated_at=body.expected_updated_at)
            await _audit(conn, actor, [change])
            row = await store.one_row(conn, change.source_key)
    except asyncpg.exceptions.UndefinedTableError:
        raise HTTPException(status_code=503, detail=_NOT_MIGRATED) from None
    except ReferencePriceError as e:
        raise _refused(e) from None
    logger.info("reference_price_saved", source_key=change.source_key)
    return store.item_of(row)


@router.delete("/admin/reference-prices/{source_key}")
async def clear_reference_price(source_key: str, request: Request, x_admin_user_id: Optional[str] = Header(None)):
    actor = _actor(x_admin_user_id)
    try:
        async with _pool(request).acquire() as conn, conn.transaction():
            change = await store.clear(conn, source_key)
            await _audit(conn, actor, [change])
    except asyncpg.exceptions.UndefinedTableError:
        raise HTTPException(status_code=503, detail=_NOT_MIGRATED) from None
    except ReferencePriceError as e:
        raise _refused(e) from None
    logger.info("reference_price_cleared", source_key=change.source_key)
    return {"source_key": change.source_key, "cleared": True}


_HISTORY_SQL = """
    SELECT action, actor, details, created_at
      FROM acp_shared.audit_log
     WHERE resource_type = 'tour_reference_price' AND resource_id = $1 AND tenant_id IS NULL
       AND action LIKE 'reference_price.%'
     ORDER BY created_at DESC
     LIMIT 100
"""


@router.get("/admin/reference-prices/{source_key}/history")
async def reference_price_history(source_key: str, request: Request):
    key = (source_key or "").strip()
    if not key or len(key) > store.MAX_KEY:
        raise HTTPException(status_code=404, detail="Tour not found")
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(_HISTORY_SQL, key)
    out = []
    for r in rows:
        details = r["details"]
        if isinstance(details, str):
            details = json.loads(details or "{}")
        out.append({"action": r["action"], "actor": r["actor"], "details": details or {},
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None})
    return {"source_key": key, "history": out}
