"""AA-1106 — Platform › Product map: the product catalogue for the admin page /admin/platform/catalogue.

  GET /admin/platform/catalogue[?refresh=1]     the catalogue bundle: apps, features (pages, API route groups,
                                                enabling conditions, UAT scenarios), integrations (producer,
                                                consumers, contract, auth, conditions, failure, kill switch),
                                                journeys, the settings rows the conditions name       ops.read (admin)

Read only. The bundle comes from the root repo "Docs sync" workflow through S3 (api/services/product_catalogue.py),
cached in this process; nothing here touches the database. Admin only: POLICY = ops.read, and the BFF refuses every
other role first (frontend/lib/rbac.ts API_READ_RULES "platform").

Included in main.py WITHOUT an include prefix: staff_policy reads the route template from scope["route"].path.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from api.core.staff_authz import staff_policy
from api.services import docs_reader, product_catalogue

router = APIRouter(prefix="/admin/platform/catalogue", tags=["admin-product-catalogue"],
                   dependencies=[Depends(staff_policy)])

_NO_STORE = {"Cache-Control": "private, no-store"}


@router.get("", summary="The product catalogue: features, links between the apps and their conditions, journeys")
async def get_catalogue(refresh: bool = Query(False, description="re-read storage (at most every 30 s)")):
    try:
        bundle = await product_catalogue.load(refresh=refresh)
    except product_catalogue.CatalogueMissing as e:
        raise HTTPException(status_code=404, detail=str(e))
    except product_catalogue.CatalogueUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    return JSONResponse({**bundle, "cache_ttl_s": docs_reader.CACHE_TTL_S}, headers=_NO_STORE)
