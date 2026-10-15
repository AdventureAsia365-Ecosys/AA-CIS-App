"""Admin API docs (AA-577 follow-up) — the OpenAPI schema for the admin UI page /admin/api-docs.

The AA-577 routes (/docs, /redoc, /openapi.json, api/main.py) stay exactly as they are: on ECS they
need the X-Admin-Secret header, which a browser cannot send, so staff could not read them. This route
serves the same schema under /admin/*, where the admin BFF (frontend/app/api/admin/[...path]) already
adds X-Admin-Secret and the verified x-admin-role after checking the staff JWT.

Access: the admin secret (always, also local/CI) AND x-admin-role == "admin". Reviewer and content
staff share the backend admin secret (CONTEXT.md "Staff roles and permissions"), so the role check is
what keeps them out; the BFF also refuses them (lib/rbac.ts API_READ_RULES) before calling here.
"""
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request

from api.routers.admin import verify_admin_secret

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin_role(x_admin_role: Optional[str]) -> None:
    """Defense in depth behind the BFF rule: the schema is admin-role only."""
    if x_admin_role != "admin":
        raise HTTPException(status_code=403, detail="Only an admin can read the API docs")


@router.get("/openapi.json", include_in_schema=False)
async def get_admin_openapi(
    request: Request,
    x_admin_secret: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
):
    verify_admin_secret(x_admin_secret)
    require_admin_role(x_admin_role)
    # request.app, not `from api.main import app`: api.main imports this module (no cycle), and the
    # schema is built once and cached on app.openapi_schema by FastAPI.
    return request.app.openapi()
