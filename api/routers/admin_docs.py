"""AA-898 — the docs reader for the admin page /admin/docs (Platform › Developer › Docs).

  GET /admin/docs/manifest[?refresh=1]          the published docs catalog this caller may see     ops.read (admin)
  GET /admin/docs/page?repo=AA-Ecosys&path=...  one redacted page, only if listed for the caller   ops.read (admin)

Read only. The bundle comes from the root repo "Docs sync" workflow through S3 (api/services/docs_reader.py), cached
in this process; nothing here touches the database. Admin only: POLICY = ops.read, and the BFF refuses every other
role first (frontend/lib/rbac.ts API_READ_RULES "docs"). Owner-only pages (N-3) are listed and served only when the
verified Cognito session carries the owner flag (StaffPrincipal.is_owner); for anyone else they do not exist (404).

Included in main.py WITHOUT an include prefix: staff_policy reads the route template from scope["route"].path.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from api.core.staff_authz import StaffPrincipal, staff_policy
from api.services import docs_reader

# staff_policy checks the admin secret (or a verified staff Bearer) first, then the POLICY capability. The routes take
# the same dependency again for its value (the principal); FastAPI runs it once per request.
router = APIRouter(prefix="/admin/docs", tags=["admin-docs"], dependencies=[Depends(staff_policy)])

_NO_STORE = {"Cache-Control": "private, no-store"}


def _owner(principal: StaffPrincipal) -> bool:
    return bool(principal.is_owner and principal.auth == "cognito")


@router.get("/manifest", summary="The published docs catalog (owner-only pages for owners only)")
async def get_manifest(
    refresh: bool = Query(False, description="re-read storage (at most every 30 s)"),
    principal: StaffPrincipal = Depends(staff_policy),
):
    try:
        view = await docs_reader.load_manifest(_owner(principal), refresh=refresh)
    except docs_reader.DocsMissing as e:
        raise HTTPException(status_code=404, detail=str(e))
    except docs_reader.DocsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    return JSONResponse(view, headers=_NO_STORE)


@router.get("/page", summary="One published page (redacted Markdown) by repo and path")
async def get_page(
    path: str = Query(..., min_length=4, max_length=300, description="repo-relative path, e.g. docs/ops/README.md"),
    repo: str = Query("AA-Ecosys", min_length=1, max_length=64),
    principal: StaffPrincipal = Depends(staff_policy),
):
    try:
        view = await docs_reader.load_page(repo, path, _owner(principal))
    except docs_reader.DocsMissing as e:
        raise HTTPException(status_code=404, detail=str(e))
    except docs_reader.DocsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    return JSONResponse(view, headers=_NO_STORE)
