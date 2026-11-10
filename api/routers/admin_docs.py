"""AA-898 — the docs reader for the admin page /admin/docs (Platform › Developer › Docs).

  GET /admin/docs/manifest[?refresh=1]          the published docs catalog this caller may see     ops.read (admin)
  GET /admin/docs/page?repo=AA-Ecosys&path=...  one redacted page, only if listed for the caller   ops.read (admin)
  GET /admin/docs/asset?repo=AA-Ecosys&path=... one checked image: SVG (AA-697, rule 9) or     ops.read (admin)
                                                PNG / WebP screenshot (AA-1064, rule 10); &encoding=base64 answers JSON

Read only. The bundle comes from the root repo "Docs sync" workflow through S3 (api/services/docs_reader.py), cached
in this process; nothing here touches the database. Admin only: POLICY = ops.read, and the BFF refuses every other
role first (frontend/lib/rbac.ts API_READ_RULES "docs"). Owner-only pages (N-3) are listed and served only to a
principal holding docs.owner_read (staff_authz.can_read_owner_docs: a verified Cognito owner; Dev only and temporary,
the E2E CIS admin of ADR 0006a A10b, read only, is_owner still False); for anyone else they do not exist (404).

AA-1064: the REST API Gateway in front of this service has no binaryMediaTypes, so it would corrupt a PNG / WebP body
on the way to the admin BFF. The BFF therefore asks for ``encoding=base64`` and gets JSON {repo, path, type, bytes,
sha256, data}, decodes it and sends the bytes with the image headers itself (frontend/lib/docsAsset.ts). The raw form
(the default) stays for a caller that reaches the service directly.

Included in main.py WITHOUT an include prefix: staff_policy reads the route template from scope["route"].path.
"""
from __future__ import annotations

from typing import Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from api.core.staff_authz import StaffPrincipal, docs_owner_read_via, staff_policy
from api.services import docs_reader

logger = structlog.get_logger()

# staff_policy checks the admin secret (or a verified staff Bearer) first, then the POLICY capability. The routes take
# the same dependency again for its value (the principal); FastAPI runs it once per request.
router = APIRouter(prefix="/admin/docs", tags=["admin-docs"], dependencies=[Depends(staff_policy)])

_NO_STORE = {"Cache-Control": "private, no-store"}
# AA-697: an SVG opened on its own (not through <img>) is a document; this CSP keeps it inert even then: no script, no
# fetch except inline images and styles, sandboxed (unique origin), never framed. AA-1064: the same headers go with a
# PNG / WebP (harmless there, and one header set for every image); 5 minutes private cache, like the bundle cache.
_IMAGE_HEADERS = {
    "Cache-Control": "private, max-age=300",
    "Content-Security-Policy": ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox; "
                                "frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Content-Disposition": "inline",
}


def _owner_read(principal: StaffPrincipal, route: str) -> bool:
    via = docs_owner_read_via(principal)
    if via == "e2e_dev":
        logger.info("docs_owner_read_e2e_dev", route=route)   # A10b: every use is visible in the logs
    return via is not None


def _is_owner(principal: StaffPrincipal) -> bool:
    return bool(principal.is_owner and principal.auth == "cognito")


@router.get("/manifest", summary="The published docs catalog (owner-only pages for owners only)")
async def get_manifest(
    refresh: bool = Query(False, description="re-read storage (at most every 30 s)"),
    principal: StaffPrincipal = Depends(staff_policy),
):
    try:
        view = await docs_reader.load_manifest(_owner_read(principal, "manifest"), refresh=refresh,
                                               is_owner=_is_owner(principal))
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
        view = await docs_reader.load_page(repo, path, _owner_read(principal, "page"))
    except docs_reader.DocsMissing as e:
        raise HTTPException(status_code=404, detail=str(e))
    except docs_reader.DocsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    return JSONResponse(view, headers=_NO_STORE)


@router.get("/asset", summary="One published image (SVG diagram, PNG or WebP screenshot) by repo and path")
async def get_asset(
    path: str = Query(..., min_length=5, max_length=300,
                      description="repo-relative path of an .svg, .png or .webp in the bundle"),
    repo: str = Query("AA-Ecosys", min_length=1, max_length=64),
    encoding: Literal["raw", "base64"] = Query(
        "raw", description="raw: the image bytes; base64: JSON {repo, path, type, bytes, sha256, data} (the admin BFF "
                           "reads this form: the API Gateway in front is not binary safe)"),
    principal: StaffPrincipal = Depends(staff_policy),
):
    try:
        data = await docs_reader.load_asset(repo, path, _owner_read(principal, "asset"))
    except docs_reader.DocsMissing as e:
        raise HTTPException(status_code=404, detail=str(e))
    except docs_reader.DocsUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    return asset_response(repo, path, data, encoding)


def asset_response(repo: str, path: str, data: bytes, encoding: str) -> Response:
    """A checked image as the route sends it: raw bytes with the image headers, or the base64 JSON (no-store) the
    admin BFF reads. Also used by the AI gateway Guide tab's image route (AA-1064)."""
    if encoding == "base64":
        return JSONResponse(docs_reader.asset_json(repo, path, data), headers=_NO_STORE)
    return Response(content=data, media_type=docs_reader.asset_type(path), headers=_IMAGE_HEADERS)
