"""S241 — staff-only guard for /v1 routers that act on every tenant's data (/v1/rules, /v1/social).

Before S241 those routers used a local `_get_admin` that accepted the X-Admin-Secret OR any valid
JWT. A tenant can trade its API key for a JWT (POST /auth/tenant-login, role='tenant'), so any
tenant could list every tenant's social posts, approve/reject them, and switch off global output
rules. AA-799 fixed the same pattern in v1_pipeline._require_admin; this is that guard, shared.

Rules (same as v1_pipeline._require_admin):
- X-Admin-Secret present: 503 if the server secret is not configured, 403 if it does not match
  (constant-time compare), else the master principal.
- No secret but a Bearer token: the token is verified (401 if bad/expired) and then refused with 403 —
  authenticated is not authorized; no JWT (tenant or staff) is accepted here.
- Nothing: 401.

The env is read per call, not at import, so tests and secret rotation behave the same.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.core.secret_compare import secrets_equal
from api.routers.auth import verify_jwt

MASTER_PRINCIPAL = {"sub": "00000000-0000-0000-0000-000000000001", "role": "admin"}

_bearer = HTTPBearer(auto_error=False)


def require_staff_secret(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    admin_secret = os.environ.get("ADMIN_SECRET", "")
    x_admin = request.headers.get("x-admin-secret")
    if x_admin:
        if not admin_secret:
            raise HTTPException(status_code=503, detail="Admin secret not configured")
        if secrets_equal(x_admin, admin_secret):
            return dict(MASTER_PRINCIPAL)
        raise HTTPException(status_code=403, detail="Invalid admin secret")
    if credentials:
        verify_jwt(credentials.credentials)  # 401 on a bad/expired token
        raise HTTPException(status_code=403, detail="Admin access required")
    raise HTTPException(status_code=401, detail="Not authenticated")
