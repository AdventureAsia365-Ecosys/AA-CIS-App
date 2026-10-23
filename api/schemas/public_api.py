"""AA-899 — response models of public CIS routes that used to return a bare dict.

A route whose response is a bare ``dict`` (or a hand-made ``JSONResponse``) shows up in the exported OpenAPI document
(``docs/api/openapi.json``) as an empty schema, so the breaking-change check (oasdiff, workflow "API contract")
cannot see a field disappear. Each model here names every field the route returns today; nothing else changed.

- ``response_model=`` where the handler returns a dict (FastAPI validates and serializes it): ``Health``,
  ``TenantSession``, ``Quota``. Nullable where the value can be null today, so no new 500 is possible.
- ``responses={code: {"model": ...}}`` (documentation only) where the handler returns a ``JSONResponse`` with its own
  status or headers: ``Readiness`` (200 or 503), ``Jwks`` (Cache-Control header).

The rest of the public surface is listed in ``tests/unit/snapshots/aa899_untyped_public_routes.json``; that list may
only shrink (``tests/unit/test_aa899_openapi_export.py``).
"""
from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class Health(BaseModel):
    """GET /health — liveness; ``status`` is ``ok`` (ALB check and the deploy smoke read it)."""
    status: str
    service: str
    version: str
    git_sha: str


class Readiness(BaseModel):
    """GET /health/ready — ``status`` ``ok`` (200) or ``schema_behind`` / ``unavailable`` (503); ``db`` ``ok``,
    ``error`` or ``not_ready``."""
    status: str
    db: str
    schema_version: Optional[str]
    required_schema: str
    git_sha: str


class Jwk(BaseModel):
    """One RSA public key of the Booking bridge token (RFC 7517)."""
    model_config = ConfigDict(extra="allow")

    kty: str
    n: str
    e: str
    kid: str
    use: str
    alg: str


class Jwks(BaseModel):
    """GET /auth/.well-known/jwks.json — current key first, then previous, then next."""
    keys: List[Jwk]


class TenantSession(BaseModel):
    """POST /auth/verify-tenant — the claims of a valid tenant session token."""
    valid: bool
    tenant_id: str
    name: Optional[str]
    plan_tier: Optional[str]


class Quota(BaseModel):
    """GET /v1/quota — this month's rewrite quota of the calling tenant (read-only)."""
    plan_tier: Optional[str]
    tours_per_month: int
    rewrites_used: int
    rewrites_remaining: int
    resets_at: str
