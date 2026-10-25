"""AA-899 — response models of public CIS routes that used to return a bare dict.

A route whose response is a bare ``dict`` (or a hand-made ``JSONResponse``) shows up in the exported OpenAPI document
(``docs/api/openapi.json``) as an empty schema, so the breaking-change check (oasdiff, workflow "API contract")
cannot see a field disappear. Each model here names every field the route returns today; nothing else changed.

- ``response_model=`` where the handler returns a dict (FastAPI validates and serializes it): ``Health``,
  ``TenantSession``, ``Quota``. Nullable where the value can be null today, so no new 500 is possible.
- ``responses={code: {"model": ...}}`` (documentation only) where the handler returns a ``JSONResponse`` with its own
  status or headers: ``Readiness`` (200 or 503), ``Jwks`` (Cache-Control header), and the ``Catalog*`` bodies of
  catalog.http.v1 (AA-976; ETag / 304).

The rest of the public surface is listed in ``tests/unit/snapshots/aa899_untyped_public_routes.json``; that list may
only shrink (``tests/unit/test_aa899_openapi_export.py``).
"""
from typing import List, Literal, Optional

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


# ── /v1/account (AA-761) ────────────────────────────────────────────────────────

class AccountActivityItem(BaseModel):
    """One row of the calling tenant's activity trail (an allowlisted ``acp_shared.audit_log`` action)."""
    id: str
    at: Optional[str]
    action: str
    category: Literal["content", "tours", "account"]
    label: str
    actor: Literal["you", "adventure_asia", "system"]
    resource_type: Optional[str]
    resource_id: Optional[str]
    detail: str


class AccountPagination(BaseModel):
    page: int
    page_size: int
    total: int
    pages: int


class AccountActivityCategory(BaseModel):
    id: str
    label: str


class AccountActivityAction(BaseModel):
    id: str
    label: str
    category: str


class AccountActivityPage(BaseModel):
    """GET /v1/account/activity — one page, newest first, plus the filter options."""
    data: List[AccountActivityItem]
    pagination: AccountPagination
    categories: List[AccountActivityCategory]
    actions: List[AccountActivityAction]


class AccountApiKey(BaseModel):
    """GET /v1/account/api-key — the key is never readable (only its sha256 is stored): a mask and its dates."""
    has_key: bool
    masked: Optional[str]
    issued_at: Optional[str]
    last_sign_in_at: Optional[str]
    last_api_call_at: Optional[str]
    # AA-825 rollup fallback: newest month (YYYY-MM) with calls when the 90-day raw window holds none
    last_api_call_month: Optional[str]
    last_used_at: Optional[str]
    # set (and last_used_at null) when that month is newer than every exact use
    last_used_month: Optional[str]


# ── catalog.http.v1 (AA-976) — documentation only ──────────────────────────────
# The /catalog/v1 handlers (api/routers/catalog_http.py) return their own JSONResponse (ETag, Cache-Control, 304),
# so these models are declared with ``responses={200: {"model": ...}}`` and never validate or filter a body. They
# mirror the root contract docs/contracts/catalog.http.v1.openapi.yaml (v1.2), which stays the source of truth (its
# test: tests/unit/test_aa976_catalog_http_contract.py). Prefixed ``Catalog`` so no other CIS schema name collides.

class _CatalogModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CatalogTourSummary(_CatalogModel):
    """One tour in a ``listTours`` page."""
    source_system: Literal["cis"]
    source_id: str
    title: str
    country_code: str
    duration_days: int
    content_hash: str
    updated_at: str


class CatalogTourPage(_CatalogModel):
    """GET /catalog/v1/tours — one page ordered by source_id; ``next_cursor`` null on the last page."""
    items: List[CatalogTourSummary]
    next_cursor: Optional[str] = None


class CatalogPlace(_CatalogModel):
    destination_id: str
    name: str
    time_zone: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    precision: Optional[Literal["point", "area", "line"]] = None


class CatalogTourDay(_CatalogModel):
    day_no: int
    title: str
    description: Optional[str] = None
    time_zone: Optional[str] = None
    places: List[CatalogPlace]


class CatalogExclusion(_CatalogModel):
    code: Optional[str] = None
    text: str
    suggests_collect_on_arrival: bool = False


class CatalogFacets(_CatalogModel):
    activities: List[str] = []
    intensity: Optional[Literal["easy", "moderate", "challenging", "strenuous"]] = None


class CatalogPhoto(_CatalogModel):
    url: str
    alt: Optional[str] = None


class CatalogTour(_CatalogModel):
    """GET /catalog/v1/tours/{source_system}/{source_id} — the served tour (ETag = ``content_hash``)."""
    source_system: Literal["cis"]
    source_id: str
    title: str
    summary: Optional[str] = None
    country_code: str
    duration_days: int
    content_hash: str
    days: List[CatalogTourDay]
    inclusions: List[str]
    exclusions: List[CatalogExclusion]
    facets: CatalogFacets
    photos: List[CatalogPhoto] = []


class CatalogDestination(_CatalogModel):
    """GET /catalog/v1/destinations/{destination_id} — one destination with its IANA time zone."""
    destination_id: str
    name: str
    country_code: Optional[str]
    time_zone: str
    lat: Optional[float] = None
    lng: Optional[float] = None


class CatalogAaContentSummary(_CatalogModel):
    """One item in a ``listAaContent`` page: the latest published version of an item that is not retired (AA-977)."""
    content_id: str
    kind: Literal["prep_pack", "starter_template", "campaign_block"]
    version: int
    locale: Literal["en", "vi"]
    group_key: Optional[str] = None
    title: str
    published_at: str
    content_hash: str


class CatalogAaContentPage(_CatalogModel):
    """GET /catalog/v1/aa-content — one page ordered by content_id; ``next_cursor`` null on the last page."""
    items: List[CatalogAaContentSummary]
    next_cursor: Optional[str] = None


class CatalogAaTourKey(_CatalogModel):
    source_system: Literal["cis"]
    source_id: str


class CatalogAaAppliesTo(_CatalogModel):
    country_codes: List[str] = []
    tours: List[CatalogAaTourKey] = []


class CatalogAaContent(_CatalogModel):
    """GET /catalog/v1/aa-content/{content_id}/versions/{version} — one published version (ETag = ``content_hash``);
    served also after a newer version is published or the item is retired (AA-977)."""
    content_id: str
    kind: Literal["prep_pack", "starter_template", "campaign_block"]
    version: int
    locale: Literal["en", "vi"]
    group_key: Optional[str] = None
    title: str
    body_markdown: str
    applies_to: CatalogAaAppliesTo
    published_at: str
    content_hash: str


# ── tenant.membership.v1 (AA-978) — documentation only ─────────────────────────
# The /tenant/membership/v1 handlers (api/routers/tenant_membership.py) return their own JSONResponse (ETag, 304,
# Cache-Control), so these models are declared with ``responses={200: {"model": ...}}`` and never validate a body.
# They mirror the root contract docs/contracts/tenant.membership.v1.openapi.yaml (v1.0), the source of truth (its
# test: tests/unit/test_aa978_tenant_membership_contract.py). Prefixed ``Membership``.

class MembershipIdentity(_CatalogModel):
    iss: str
    sub: str


class MembershipItem(_CatalogModel):
    """One CIS membership: the Content studio role, never a CRM role; no e-mail or name."""
    membership_id: str
    tenant_id: str
    identity: Optional[MembershipIdentity] = None
    content_studio_role: Literal["owner", "editor", "viewer"]
    status: Literal["invited", "active", "removed"]
    invitation_id: Optional[str] = None
    updated_at: str


class MembershipTenantMembers(_CatalogModel):
    """GET /tenant/membership/v1/tenants/{tenant_id}/members — invited and active members (ETag, 304)."""
    tenant_id: str
    tenant_state: Literal["active", "locked", "deleted"]
    members: List[MembershipItem]
    as_of: str


class MembershipIdentityMemberships(_CatalogModel):
    """GET /tenant/membership/v1/memberships?iss=&sub= — the identity's memberships that are not removed."""
    identity: MembershipIdentity
    memberships: List[MembershipItem]


class MembershipEvent(_CatalogModel):
    event_id: str
    cursor: str
    type: Literal["tenant.created", "tenant.locked", "tenant.unlocked", "tenant.deleted", "membership.invited",
                  "membership.activated", "membership.role_changed", "membership.removed"]
    occurred_at: str
    tenant_id: str
    membership: Optional[MembershipItem] = None


class MembershipEventPage(_CatalogModel):
    """GET /tenant/membership/v1/events — oldest first; ``next_cursor`` is always set (store it and send it back)."""
    items: List[MembershipEvent]
    next_cursor: str
