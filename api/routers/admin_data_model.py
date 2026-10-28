"""AA-894 scope 2 — GET /admin/data-model: the live ERD for the admin page /admin/data-model (Platform › Developer).

  GET /admin/data-model[?refresh=1]   the latest erd.json + a diff summary vs previous.json      ops.read (admin)

Read only. The model comes from the nightly DB Schema Dump (AA-CIS-Infra) through S3, cached in this process for a
few minutes (api/services/data_model.py); nothing here touches the database. 404 when no model has been published
yet, 503 when it can not be read. Admin only: POLICY ("GET", "/admin/data-model") = ops.read, and the BFF refuses
every other role before the call (frontend/lib/rbac.ts API_READ_RULES "data-model").

Included in main.py WITHOUT an include prefix: staff_policy reads the route template from scope["route"].path.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from api.core.staff_authz import staff_policy
from api.services import data_model

# staff_policy checks the admin secret (or a verified staff Bearer) first, then the POLICY capability.
router = APIRouter(prefix="/admin", tags=["admin-data-model"], dependencies=[Depends(staff_policy)])


@router.get("/data-model", summary="Live data model (ERD) from the nightly schema dump, with a diff")
async def get_data_model(refresh: bool = Query(False, description="re-read storage (at most every 30 s)")):
    try:
        view = await data_model.load_data_model(refresh=refresh)
    except data_model.DataModelMissing as e:
        raise HTTPException(status_code=404, detail=str(e))
    except data_model.DataModelUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    # The browser keeps nothing: the page holds the model in react-query, the server caches it.
    return JSONResponse(view, headers={"Cache-Control": "private, no-store"})
