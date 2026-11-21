"""AA-1155 spec task T11 (R5.5) — routes that take a verified Cognito staff session only.

The AI budget and AI gateway routers (every route under /admin/budgets and /admin/ai-gateway) add
:func:`require_cognito_session` to their router dependencies: a request with the admin secret and a forged
x-admin-role (any role, admin included), a legacy password session, the Preview credential or the machine gets 403
before the route runs, whatever role it names. The staff bearer middleware is the only source of a verified identity.
"""
from __future__ import annotations

from typing import Any

import structlog
from fastapi import HTTPException, Request

from api.core.staff_authz import request_staff_identity

logger = structlog.get_logger()

AI_CONTROLS_NEED_COGNITO = "AI budgets and the AI gateway need a company sign-in (Cognito); other sessions cannot"


def require_cognito_session(request: Request) -> Any:
    """Router dependency: the verified Cognito StaffIdentity of this request, or 403."""
    identity = request_staff_identity(request)
    if identity is None or getattr(identity, "auth", None) != "cognito" or not getattr(identity, "admin_id", None):
        logger.warning("staff_cognito_required", path=request.url.path, method=request.method,
                       role_header=request.headers.get("x-admin-role"))
        raise HTTPException(status_code=403, detail=AI_CONTROLS_NEED_COGNITO)
    return identity
