"""AA-882 (S241 GĐ1 + GĐ3) — server-side staff authorization: capabilities per role, policy per route.

Until now the backend only checked the shared X-Admin-Secret (held by the staff BFF) and trusted
whatever came with it, so a reviewer or content user could change a stage's model, a spend budget
or a Jev question by calling /api/admin/* directly: the BFF lets every staff role write by default
(frontend/lib/rbac.ts canWriteAdminPath). This module is the backend half of the rule.

Pieces
------
- ROLE_PERMS: staff role -> capabilities. The four roles are the shared.admin_role enum and the
  frontend/lib/rbac.ts StaffRole union (tests/unit/test_aa882_staff_authz.py keeps them in sync).
  A new kind of staff (ops, sales, content lead) is one more row here, later one Cognito group.
- POLICY: (method, route template) -> the capability the route needs. Keyed by method + template,
  not by router: six routers share the /admin prefix and mix reads every role needs (GET
  /admin/llm-config for the S1 page) with admin-only writes (PATCH /admin/llm-config/{stage}).
- MACHINE_ROUTES: the routes a machine caller (secret, no role header) may call — the CI deploy
  preflight/smoke and the ops-probe Lambda scheduler. Callers that never send x-admin-role must keep
  working, or the next deploy's preflight gets 403 and every CIS deploy is blocked.
- staff_policy: the FastAPI dependency, attached router-level. It checks the secret itself
  (admin.verify_admin_secret, called directly) and then the route's POLICY entry.
  staff_policy_if_staff: the same, but only for a request carrying the valid secret — for routers whose
  own guard also admits a tenant JWT or answers 401 (see its docstring).

Principal (GĐ1, until the staff JWT of GĐ4)
------------------------------------------
- secret + x-admin-role  -> staff principal with that role. The BFF forwards the role from the
  staff JWT it verified (/auth/verify-admin). Header trust ends with the Bearer staff JWT (GĐ4) and
  Cognito (ADR 0006): whoever holds the secret can still claim any role until then.
- secret, no x-admin-role -> machine principal: allowed only on MACHINE_ROUTES.
- a role header that is not a staff role (empty, "tenant", typo) -> 403, never a machine.
- AA-816 C9 (api/core/staff_credential.py): a verified Cognito Bearer needs no secret (role from its groups);
  the Preview credential is always the viewer (x-admin-role rewritten by the staff bearer middleware), never
  the machine. The main secret keeps every rule above.

Coverage (GĐ3)
--------------
Every non-GET route under /admin, /v1/pipeline, /v1/rules and /v1/social has a POLICY entry, and every
router behind those prefixes runs staff_policy (or staff_policy_if_staff, for the routers whose own
guard also takes a tenant JWT or answers 401): tests/unit/test_aa882_route_policy.py enforces both and
keeps the snapshot. GETs stay open to every staff role unless they return data that only an admin
needs (tenant accounts, money, system ops, the staff list); those are in POLICY too. A GET with no
entry is open to every staff role and to the machine.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog
from fastapi import Header, HTTPException, Request

from api.core.secret_compare import secrets_equal

logger = structlog.get_logger()

# ── Capabilities ────────────────────────────────────────────────────────────────────────────────

CONTENT_READ = "content.read"
CONTENT_WRITE = "content.write"              # upload, S1 rewrite, regenerate
REVIEW_ACT = "review.act"
MASTER_ADMIN = "master.admin"
ATOMS_WRITE = "atoms.write"
PHOTOS_WRITE = "photos.write"
A4_UNPUBLISH = "a4.unpublish"
QUALITY_WRITE = "quality.write"              # T2/T9 quality: sentence cut/rewrite, labels, place cards
CONTENT_SETTINGS_WRITE = "content.settings.write"   # gates, brand, SEO
TENANT_READ = "tenant.read"
TENANT_WRITE = "tenant.write"
OPS_READ = "ops.read"
OPS_WRITE = "ops.write"                      # job cancel/retry/retention, scheduled ops checks
AI_ADMIN = "ai.admin"                        # stage models + routes, model catalog, Jev questions
SPEND_READ = "spend.read"
SPEND_WRITE = "spend.write"                  # budgets, Jev credit ledger, Cost Explorer fetch
STAFF_READ = "staff.read"
STAFF_WRITE = "staff.write"                  # change a staff role / active flag: owners only
AUDIT_READ = "audit.read"
# AA-913 — change the platform alert recipients (SNS alerts topic + ops e-mail). Admin role; the router also requires
# a verified Cognito session for every change but the status refresh (api/routers/admin_alert_recipients.py).
ALERTS_WRITE = "alerts.write"

_CONTENT_WORK = frozenset({CONTENT_READ, CONTENT_WRITE, REVIEW_ACT})

ROLE_PERMS: dict[str, frozenset[str]] = {
    "admin": _CONTENT_WORK | {
        MASTER_ADMIN, ATOMS_WRITE, PHOTOS_WRITE, A4_UNPUBLISH, QUALITY_WRITE, CONTENT_SETTINGS_WRITE,
        TENANT_READ, TENANT_WRITE, OPS_READ, OPS_WRITE, AI_ADMIN, SPEND_READ, SPEND_WRITE,
        STAFF_READ, AUDIT_READ, ALERTS_WRITE,
    },
    "reviewer": _CONTENT_WORK,
    "content": _CONTENT_WORK,
    # AA-878 — read-only staff (the UI-smoke E2E account, migration 249). Reads the content pages; refused on
    # every write (READ_ONLY_ROLES below, whatever the route's capability) and on every admin-only read. Not
    # assignable from the Staff page (api/services/staff_directory.py CIS_ROLES) and no Booking token.
    "viewer": frozenset({CONTENT_READ}),
}

# AA-878 — roles that may never write: denial_reason refuses them on every method but GET/HEAD/OPTIONS, even on
# a write whose capability they hold (the notification bell's PUTs need content.read) and on a write with no
# POLICY entry. The BFF refuses the same roles before it forwards (frontend/lib/auth-server.ts requireAdmin).
READ_ONLY_ROLES: frozenset[str] = frozenset({"viewer"})
READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

# staff.write is not a role capability: it needs the owner flag (shared.admin_users.is_owner, migration 247,
# S241 Q2) AND a verified Cognito session (AA-816 C6): the caller's identity must come from the staff bearer
# middleware (api/middleware/staff_bearer.py), never from x-admin-role, which any secret holder can forge.
# The caller must also hold the admin role (staff.read). A legacy password session or the machine gets 403 even
# when the row is an owner. The staff directory writer
# (api/services/staff_directory.py) re-reads is_owner from the DB inside its transaction before each write.
OWNER_PERMS: frozenset[str] = frozenset({STAFF_WRITE})

CAPABILITIES: frozenset[str] = frozenset().union(*ROLE_PERMS.values(), OWNER_PERMS)

STAFF_ROLES: tuple[str, ...] = tuple(ROLE_PERMS)

# ── Route policy ─────────────────────────────────────────────────────────────────────────────────
# (method, route template) -> capability. Every staff write is here (GĐ3, enforced by the route-policy
# test); a GET is here only when its data is admin-only. Mapping rule: the S241 §5.1 table, then the
# page that calls the route (frontend/lib/rbac.ts PAGE_RULES) — an every-staff content page (Upload,
# S1, Review) keeps content.write / review.act, an admin-only page gets an admin capability.

POLICY: dict[tuple[str, str], str] = {
    # ── admin_llm_ops — stage model, fallback route, catalog (GĐ1)
    ("PATCH", "/admin/llm-config/{stage}"): AI_ADMIN,
    ("PATCH", "/admin/llm-config/{stage}/route"): AI_ADMIN,
    ("PATCH", "/admin/llm-catalog/{model_key}"): AI_ADMIN,
    # admin_llm_ops — on-demand / scheduled spend + Jev probes (the Lambda ones are MACHINE_ROUTES)
    ("POST", "/admin/dfs-balance/check"): OPS_WRITE,
    ("POST", "/admin/cost-explorer/check"): SPEND_WRITE,
    ("POST", "/admin/jev-canary/check"): OPS_WRITE,
    ("POST", "/admin/jev/402-sweep"): OPS_WRITE,
    # admin_llm_ops — money reads (External Spend page, admin-only)
    ("GET", "/admin/cost-explorer"): SPEND_READ,
    ("GET", "/admin/dfs-balance"): SPEND_READ,
    ("GET", "/admin/dfs-usage"): SPEND_READ,
    ("GET", "/admin/dfs-usage/by-country"): SPEND_READ,
    ("GET", "/admin/llm-usage/calls"): SPEND_READ,
    ("GET", "/admin/llm-usage/tree"): SPEND_READ,
    ("GET", "/admin/spend/daily"): SPEND_READ,
    # admin_llm_ops — primary vs shadow model cost (Settings › LLM Models, admin-only tab)
    ("GET", "/admin/llm-shadow/report"): SPEND_READ,
    # admin_llm_ops — market requests tenants made that no market covers (Tenants page, admin-only)
    ("GET", "/admin/unmapped-market-requests"): TENANT_READ,
    # ── admin_budgets
    ("GET", "/admin/budgets"): SPEND_READ,
    ("PUT", "/admin/budgets/{provider}/{scope}"): SPEND_WRITE,
    # ── admin_job_runner — job control + retention
    ("POST", "/admin/job-runner/jobs/{job_id}/cancel"): OPS_WRITE,
    ("POST", "/admin/job-runner/jobs/{job_id}/retry"): OPS_WRITE,
    ("POST", "/admin/job-runner/retention/run"): OPS_WRITE,
    ("POST", "/admin/job-runner/retention/schedule"): OPS_WRITE,
    ("POST", "/admin/job-runner/staff-reconcile/schedule"): OPS_WRITE,   # AA-816 C7, daily (MACHINE_ROUTES)
    # ── admin_decisions — Jev question mode/floors, Jev credit ledger
    ("PUT", "/admin/decisions/questions/{question_key}"): AI_ADMIN,
    ("POST", "/admin/decisions/jev-credit/topups"): SPEND_WRITE,
    ("DELETE", "/admin/decisions/jev-credit/topups/{topup_id}"): SPEND_WRITE,
    ("GET", "/admin/decisions/jev-credit"): SPEND_READ,   # top-ups + estimated balance (money)
    # ── admin_settings — SEO config of the aa_internal tenant
    ("PATCH", "/admin/settings/seo"): CONTENT_SETTINGS_WRITE,

    # ── admin (admin.py) — tenant accounts (Tenants page, admin-only; AA-797 already re-checks writes)
    ("GET", "/admin/tenants"): TENANT_READ,
    ("POST", "/admin/tenants"): TENANT_WRITE,
    ("PATCH", "/admin/tenants/{tenant_id}"): TENANT_WRITE,
    ("GET", "/admin/tenants/{tenant_id}/usage"): TENANT_READ,
    ("GET", "/admin/tenants/{tenant_id}/audit"): TENANT_READ,
    ("GET", "/admin/tenants/{tenant_id}/rewrite-activity"): TENANT_READ,
    ("GET", "/admin/tenants/{tenant_id}/config"): TENANT_READ,
    ("PUT", "/admin/tenants/{tenant_id}/config"): TENANT_WRITE,
    ("PUT", "/admin/tenants/{tenant_id}/brand-identity"): TENANT_WRITE,
    ("POST", "/admin/tenants/{tenant_id}/generate-key"): TENANT_WRITE,
    ("POST", "/admin/tenants/{tenant_id}/offboard"): TENANT_WRITE,
    ("POST", "/admin/tenants/{tenant_id}/restore"): TENANT_WRITE,
    # GET /admin/tenants/{tenant_id}/details stays open: Master Content (every staff role) reads the
    # aa_internal catalog through it.
    # admin — master tour lifecycle (S241 §5.1 master.admin; Master Content page)
    ("PATCH", "/admin/master/{tour_id}/activate"): MASTER_ADMIN,
    ("PATCH", "/admin/master/{tour_id}/deactivate"): MASTER_ADMIN,
    ("PATCH", "/admin/master/{tour_id}/status"): MASTER_ADMIN,
    ("PATCH", "/admin/master/{tour_id}/trash"): MASTER_ADMIN,
    ("PATCH", "/admin/master/{tour_id}/restore"): MASTER_ADMIN,
    # admin — source tours (Upload page, every staff role)
    ("PATCH", "/admin/tours/{tour_id}/trash"): CONTENT_WRITE,
    ("PATCH", "/admin/tours/{tour_id}/restore"): CONTENT_WRITE,
    # admin — the notification bell (Topbar, every staff role)
    ("PUT", "/admin/notifications/read-all"): CONTENT_READ,
    ("PUT", "/admin/notifications/{notif_id}/read"): CONTENT_READ,
    # ── admin_tenant360 — Tenant 360 tabs (admin-only)
    ("GET", "/admin/tenants/{tenant_id}/cost"): TENANT_READ,
    ("GET", "/admin/tenants/{tenant_id}/quality"): TENANT_READ,
    ("GET", "/admin/tenants/{tenant_id}/markets"): TENANT_READ,

    # ── admin_pipeline — Upload (S0), every staff role
    ("POST", "/admin/upload-url"): CONTENT_WRITE,
    ("POST", "/admin/ingest-s3"): CONTENT_WRITE,
    ("POST", "/admin/upload-staging/{staging_id}/decide"): CONTENT_WRITE,
    ("POST", "/admin/raw-tours/{tour_id}/set-active"): CONTENT_WRITE,
    ("DELETE", "/admin/raw-tours/{tour_id}"): CONTENT_WRITE,
    # admin_pipeline — S1 rewrite, every staff role
    ("POST", "/admin/run-tour"): CONTENT_WRITE,
    ("POST", "/admin/run-tour-async"): CONTENT_WRITE,
    ("POST", "/admin/s1-batch/submit"): CONTENT_WRITE,
    ("POST", "/admin/s1/seo-prefetch"): CONTENT_WRITE,
    # admin_pipeline — Review Queue + tour edits, every staff role
    ("POST", "/admin/review-queue/{review_id}/approve"): REVIEW_ACT,
    ("POST", "/admin/review-queue/{review_id}/reject"): REVIEW_ACT,
    ("POST", "/admin/review-queue/{review_id}/dismiss"): REVIEW_ACT,
    ("POST", "/admin/review-queue/{review_id}/supersede"): REVIEW_ACT,
    # AA-845 — a reviewer marks one sentence "unsupported" (a1_claim_supported truth=False)
    ("POST", "/admin/review-queue/{review_id}/claims/label"): REVIEW_ACT,
    ("PATCH", "/admin/tours/{tour_id}/country"): CONTENT_WRITE,
    ("PATCH", "/admin/tours/{tour_id}/raw"): CONTENT_WRITE,
    ("PATCH", "/admin/tours/{tour_id}/generated/{content_id}"): CONTENT_WRITE,
    ("POST", "/admin/tours/{tour_id}/generated/{content_id}/revalidate"): CONTENT_WRITE,
    ("POST", "/admin/tours/{tour_id}/versions/{version_num}/promote"): CONTENT_WRITE,
    # admin_pipeline — brand rules + AA brand identity (Settings › Brand; §5.1 content.settings.write).
    # The brand-identity routes also take a tenant JWT (own brand, AA-424): POLICY applies to the
    # staff branch only (the router runs staff_policy_if_staff).
    ("POST", "/admin/brands"): CONTENT_SETTINGS_WRITE,
    ("PUT", "/admin/brands/{brand_name}"): CONTENT_SETTINGS_WRITE,
    ("DELETE", "/admin/brands/{brand_name}"): CONTENT_SETTINGS_WRITE,
    ("POST", "/admin/brands/{brand_name}/activate"): CONTENT_SETTINGS_WRITE,
    ("POST", "/admin/brands/parse-docx"): CONTENT_SETTINGS_WRITE,
    ("POST", "/admin/brand-identity"): CONTENT_SETTINGS_WRITE,
    ("POST", "/admin/brand-identity/upload"): CONTENT_SETTINGS_WRITE,
    ("POST", "/admin/brand-identity/parse"): CONTENT_SETTINGS_WRITE,
    # admin_pipeline — admin-only reads: any tenant's billing, live AWS infra state
    ("GET", "/admin/billing"): TENANT_READ,
    ("GET", "/admin/infra/status"): OPS_READ,

    # ── admin_atoms — Social Content atoms (Atom Curation page, admin-only)
    ("POST", "/admin/atoms/atomize"): ATOMS_WRITE,
    ("PATCH", "/admin/atoms/{atom_id}"): ATOMS_WRITE,
    ("GET", "/admin/atoms"): TENANT_READ,
    ("GET", "/admin/atoms/summary"): TENANT_READ,
    ("GET", "/admin/atoms/unatomized-tours"): TENANT_READ,
    # ── admin_a4 — Content Trace force-unpublish (admin-only)
    ("POST", "/admin/a4/publish-log/{publish_id}/unpublish"): A4_UNPUBLISH,
    # admin_a4 — every tenant's A4 content, publish and review logs (Content Trace, A4 Quality, Tenants)
    ("GET", "/admin/a4/content-log"): TENANT_READ,
    ("GET", "/admin/a4/gate-telemetry"): TENANT_READ,
    ("GET", "/admin/a4/platform-stats"): TENANT_READ,
    ("GET", "/admin/a4/publish-log"): TENANT_READ,
    ("GET", "/admin/a4/review-log"): TENANT_READ,
    # ── admin_content_quality — T2/T9 quality (admin-only page; the BFF API_WRITE_RULES agree).
    # The reads show every tenant's pieces and tour versions: tenant.read.
    ("GET", "/admin/content-quality/pieces"): TENANT_READ,
    ("GET", "/admin/content-quality/pieces/{piece_id}"): TENANT_READ,
    ("GET", "/admin/content-quality/pieces/{piece_id}/label"): TENANT_READ,
    ("GET", "/admin/content-quality/place-cards"): TENANT_READ,
    ("GET", "/admin/content-quality/place-cards/duplicates"): TENANT_READ,
    ("GET", "/admin/content-quality/place-cards/{card_id}"): TENANT_READ,
    ("GET", "/admin/content-quality/stats"): TENANT_READ,
    ("GET", "/admin/content-quality/tour-versions"): TENANT_READ,
    ("GET", "/admin/content-quality/tour-versions/{version_id}"): TENANT_READ,
    ("POST", "/admin/content-quality/pieces/{piece_id}/label"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/pieces/{piece_id}/sentences/suggest"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/pieces/{piece_id}/sentences/apply"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/tour-versions/{version_id}/sentences/suggest"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/tour-versions/{version_id}/sentences/apply"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/requests/{request_id}/regenerate-angles"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/place-cards/build"): QUALITY_WRITE,
    ("PATCH", "/admin/content-quality/place-cards/{card_id}"): QUALITY_WRITE,
    ("PATCH", "/admin/content-quality/place-cards/{card_id}/facts"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/place-cards/{keep_id}/merge"): QUALITY_WRITE,
    # ── admin_photos — Drive sync + assignment (Photos page, admin-only)
    ("POST", "/admin/photos/sync"): PHOTOS_WRITE,
    ("POST", "/admin/photos/{photo_id}/assign"): PHOTOS_WRITE,
    # ── admin_places — place_extract backfill (paid geocoder jobs)
    ("POST", "/admin/places/backfill"): OPS_WRITE,
    # ── admin_segment_research — DFS demand research (paid run; preview is a free dry run)
    ("POST", "/admin/segment-research/preview"): OPS_READ,
    ("POST", "/admin/segment-research/run"): OPS_WRITE,
    # ── admin_waves — rerun waves (check is the ops-probe Lambda, a MACHINE_ROUTE)
    ("POST", "/admin/waves"): OPS_WRITE,
    ("POST", "/admin/waves/{wave_id}/close"): OPS_WRITE,
    ("POST", "/admin/waves/check"): OPS_WRITE,
    ("GET", "/admin/waves"): OPS_READ,
    ("GET", "/admin/waves/{wave_id}/report"): OPS_READ,   # incl. the wave's LLM cost
    # ── admin_overview — whole-system snapshot incl. spend (admin-only page; CI smoke = machine)
    ("GET", "/admin/overview"): OPS_READ,
    ("GET", "/admin/overview/ops"): OPS_READ,   # AA-858 business view incl. cost per app (admin-only page)
    # ── v1_trip_page admin_router — public trip pages of AA content
    ("POST", "/admin/trip-pages/{tour_id}/publish"): MASTER_ADMIN,
    ("POST", "/admin/trip-pages/recheck"): OPS_WRITE,
    # ── admin_platform (AA-883, GĐ2) — the staff directory; who has access is admin-only (staff.read)
    ("GET", "/admin/platform/staff"): STAFF_READ,
    ("GET", "/admin/platform/staff/{staff_id}"): STAFF_READ,
    # admin_platform (AA-816 C6) — staff directory writes: owners on a Cognito session only (staff.write)
    ("POST", "/admin/platform/staff"): STAFF_WRITE,
    ("PATCH", "/admin/platform/staff/{staff_id}/roles"): STAFF_WRITE,
    ("POST", "/admin/platform/staff/{staff_id}/disable"): STAFF_WRITE,
    ("POST", "/admin/platform/staff/{staff_id}/enable"): STAFF_WRITE,
    ("POST", "/admin/platform/staff/{staff_id}/sign-out"): STAFF_WRITE,
    ("POST", "/admin/platform/staff/{staff_id}/sync"): STAFF_WRITE,
    # S242 — resend the staff invitation e-mail (queues a staff_invite_email job; owners only, like every write)
    ("POST", "/admin/platform/staff/{staff_id}/invite"): STAFF_WRITE,

    # ── /v1 routers the staff BFF calls with the admin secret (/api/pipeline, the /api/tenant staff
    # branch). Their own guard keeps tenant JWTs out; POLICY narrows the staff roles.
    ("POST", "/v1/pipeline/run"): CONTENT_WRITE,
    ("POST", "/v1/pipeline/review-queue/{review_id}/approve"): REVIEW_ACT,
    ("POST", "/v1/pipeline/review-queue/{review_id}/reject"): REVIEW_ACT,
    ("PATCH", "/v1/rules/{rule_id}"): CONTENT_SETTINGS_WRITE,      # global output rules, every tenant
    ("POST", "/v1/social/batch-review"): ATOMS_WRITE,              # A3 social HITL, every tenant

    # ── admin_platform (AA-816 C7) — staff directory health: the reconcile report (WORKFORCE vs the
    # directory) and an on-demand run. Report only, so staff.read (not the owner-only staff.write).
    ("GET", "/admin/platform/staff-directory/health"): STAFF_READ,
    ("POST", "/admin/platform/staff-directory/reconcile"): STAFF_READ,

    # ── admin_platform_ops (AA-884, GĐ7/8/10) — Apps (health/version), the staff audit trail, cost per app.
    ("GET", "/admin/platform/apps"): OPS_READ,
    ("GET", "/admin/platform/apps/{app_id}"): OPS_READ,
    ("GET", "/admin/platform/audit"): AUDIT_READ,
    ("GET", "/admin/platform/audit/verify"): AUDIT_READ,
    ("GET", "/admin/platform/cost"): SPEND_READ,

    # ── admin_alert_recipients (AA-913) — Platform › Alert recipients. Reads ops.read (admin); changes alerts.write
    # (admin) and, in the router, a Cognito session for add / remove / resend / test (not for the status refresh).
    ("GET", "/admin/platform/alert-recipients"): OPS_READ,
    ("POST", "/admin/platform/alert-recipients"): ALERTS_WRITE,
    ("POST", "/admin/platform/alert-recipients/refresh"): ALERTS_WRITE,
    ("POST", "/admin/platform/alert-recipients/test-email"): ALERTS_WRITE,
    ("DELETE", "/admin/platform/alert-recipients/{recipient_id}"): ALERTS_WRITE,
    ("POST", "/admin/platform/alert-recipients/{recipient_id}/resend"): ALERTS_WRITE,
}

# POLICY entries whose route is not on main yet. (method, route) -> the change that adds it. The
# route-policy test allows these to be missing from the app; once the route exists it must match the
# template exactly (every /admin/platform/* route needs a POLICY entry, so a different parameter name
# fails CI there instead of leaving the route open to every staff role). Empty since AA-883's two
# /admin/platform/staff routes landed.
AWAITING_ROUTE: dict[tuple[str, str], str] = {}

# Path prefixes where the staff secret is accepted and POLICY applies (route-policy test scope).
STAFF_PATH_PREFIXES: tuple[str, ...] = ("/admin", "/v1/pipeline", "/v1/rules", "/v1/social")

# Machine callers send the secret and no x-admin-role. (method, route) -> who calls it. Of the GETs only
# /admin/overview has a POLICY entry (ops.read); the job-runner ones are open to any secret holder and
# are listed so the allowlist stays complete if they ever get one.
MACHINE_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/admin/job-runner/deploy-gate"): "CI deploy-dev.yml: pre-deploy gate + post-deploy smoke",
    ("GET", "/admin/job-runner/summary"): "CI deploy-dev.yml: post-deploy smoke",
    ("GET", "/admin/job-runner/workers"): "CI deploy-dev.yml: post-deploy smoke (worker parity)",
    ("GET", "/admin/overview"): "CI deploy-dev.yml: post-deploy smoke",
    ("POST", "/admin/dfs-balance/check"): "Infra Lambda dfs_balance_check (daily, AA-627)",
    ("POST", "/admin/jev-canary/check"): "Infra Lambda dfs_balance_check (daily, AA-720)",
    ("POST", "/admin/jev/402-sweep"): "Infra Lambda dfs_balance_check (daily, AA-720)",
    ("POST", "/admin/waves/check"): "Infra Lambda dfs_balance_check (AA-727)",
    ("POST", "/admin/job-runner/retention/schedule"): "Infra Lambda dfs_balance_check (daily, AA-825)",
    ("POST", "/admin/job-runner/staff-reconcile/schedule"): "Infra Lambda dfs_balance_check (daily, AA-816 C7)",
}


# ── Principal + decision ────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StaffPrincipal:
    """Who is calling: a staff role (from x-admin-role) or the machine. ``auth`` is "cognito" only when the
    staff bearer middleware verified a WORKFORCE token for this request (then x-admin-role was rewritten from
    it); ``is_owner`` is that verified row's owner flag (False on every other path)."""
    kind: str                       # "staff" | "machine"
    role: Optional[str] = None
    perms: frozenset[str] = field(default_factory=frozenset)
    auth: Optional[str] = None      # "cognito" | None (legacy session, secret-only, machine)
    is_owner: bool = False

    @property
    def is_machine(self) -> bool:
        return self.kind == "machine"


MACHINE = StaffPrincipal(kind="machine")


def principal_from_role(role_header: Optional[str], identity: Any = None) -> StaffPrincipal:
    """The principal behind a request that already passed the secret check.

    No header at all -> machine. A header that is not a known staff role -> a staff principal with no
    capabilities (refused on every POLICY route), so a typo can never fall back to machine rights.
    ``identity``: the Cognito StaffIdentity the staff bearer middleware verified for this request (None
    otherwise); it is the only source of ``auth="cognito"`` and ``is_owner``.
    """
    if role_header is None:
        return MACHINE
    role = role_header.strip()
    cognito = identity is not None and getattr(identity, "auth", None) == "cognito"
    return StaffPrincipal(
        kind="staff", role=role, perms=ROLE_PERMS.get(role, frozenset()),
        auth="cognito" if cognito else None,
        is_owner=bool(cognito and getattr(identity, "is_owner", False)),
    )


def has_perm(role: Optional[str], capability: str) -> bool:
    return capability in ROLE_PERMS.get(role or "", frozenset())


def denial_reason(principal: StaffPrincipal, method: str, route: str) -> Optional[str]:
    """None when the principal may call (method, route); otherwise the 403 detail."""
    if principal.role in READ_ONLY_ROLES and method.upper() not in READ_METHODS:
        return READ_ONLY_DENIED.format(role=principal.role)
    capability = POLICY.get((method, route))
    if capability is None:
        return None  # a GET open to every staff role and the machine (writes all have an entry)
    if principal.is_machine:
        if (method, route) in MACHINE_ROUTES:
            return None
        return "This route needs a staff role (x-admin-role); machine callers are not allowed"
    if principal.role not in ROLE_PERMS:
        return "Unknown staff role"
    if capability in OWNER_PERMS:
        # An owner writes the directory only as an admin (the role that may read it, staff.read), from a verified
        # Cognito session, with the owner flag of that verified row.
        if STAFF_READ not in principal.perms:
            return f"Role '{principal.role}' lacks the '{capability}' permission"
        if principal.auth != "cognito":
            return STAFF_WRITE_NEEDS_COGNITO
        if not principal.is_owner:
            return STAFF_WRITE_NEEDS_OWNER
        return None
    if capability not in principal.perms:
        return f"Role '{principal.role}' lacks the '{capability}' permission"
    return None


STAFF_WRITE_NEEDS_COGNITO = "Changing staff accounts needs a company sign-in (Cognito); password sessions cannot"
STAFF_WRITE_NEEDS_OWNER = "Only a platform owner can change staff accounts"
# Starts with "Role '" like the capability refusal, so the UI's staffErrorMessage() reads it the same way.
READ_ONLY_DENIED = "Role '{role}' is read-only"


def request_staff_identity(request: Request) -> Any:
    """The Cognito StaffIdentity the staff bearer middleware put on this request (scope state, or the contextvar
    it also sets), or None."""
    state = request.scope.get("state")
    identity = state.get("staff_identity") if isinstance(state, dict) else None
    if identity is None:
        from api.core.staff_identity import current_staff

        identity = current_staff.get()
    return identity


def _route_template(request: Request) -> Optional[str]:
    """The matched route's path template, e.g. /admin/llm-config/{stage}.

    FastAPI puts the matched APIRoute in scope["route"]. Its .path holds the APIRouter(prefix=...)
    but, on FastAPI >= 0.143, NOT an include_router(prefix=...): POLICY routers are therefore
    included without a prefix (tests/unit/test_aa882_route_policy.py checks it).
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else None


def _check_secret(x_admin_secret: Optional[str]) -> None:
    """admin.verify_admin_secret (503 / 403 "Invalid admin secret"), imported here, not at module top:
    api/routers/admin.py itself attaches staff_policy, so a top-level import would be circular."""
    from api.routers import admin

    admin.verify_admin_secret(x_admin_secret)


def _enforce(request: Request, x_admin_role: Optional[str]) -> StaffPrincipal:
    principal = principal_from_role(x_admin_role, request_staff_identity(request))
    template = _route_template(request)
    if template is None:
        logger.error("staff_policy_no_route", path=request.url.path, method=request.method)
        raise HTTPException(status_code=403, detail="Route policy unavailable")
    reason = denial_reason(principal, request.method, template)
    if reason is not None:
        logger.warning("staff_policy_denied", method=request.method, route=template,
                       role=principal.role, kind=principal.kind,
                       capability=POLICY.get((request.method, template)),
                       admin_user=request.headers.get("x-admin-user-id"))
        raise HTTPException(status_code=403, detail=reason)
    return principal


def staff_policy(
    request: Request,
    x_admin_secret: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
) -> StaffPrincipal:
    """Router-level dependency: the admin secret, then the route's POLICY capability for the caller.

    503/403 from verify_admin_secret as before; 403 when the role lacks the capability, the role is
    unknown, or a machine caller hits a non-machine POLICY route. Fails closed (403) when the matched
    route can not be read from the request, since the policy can not be looked up then.
    """
    _check_secret(x_admin_secret)
    return _enforce(request, x_admin_role)


def staff_policy_if_staff(
    request: Request,
    x_admin_secret: Optional[str] = Header(None),
    x_admin_role: Optional[str] = Header(None),
) -> Optional[StaffPrincipal]:
    """POLICY for a request that carries a staff credential (the main admin secret, or a principal the staff
    bearer middleware verified: Cognito Bearer or the Preview credential, AA-816 C9); a no-op for any other
    request.

    For routers whose own guard decides who else gets in and how it refuses: admin_pipeline (the
    brand-identity routes also take a tenant JWT, AA-424), admin_atoms (logs a refused tenant Bearer,
    AA-859), /v1/pipeline, /v1/rules, /v1/social (401 without credentials, 403 for any JWT, secret read
    from the environment per call). A missing or wrong secret is left to that guard, so its status,
    detail and logging stay exactly as they were. This is NOT an auth check by itself: every route of
    such a router keeps its own (the S241 route-auth test proves it), and every staff call — the only
    kind that carries a staff credential — goes through POLICY.
    """
    if not has_staff_credential(x_admin_secret):
        return None
    return _enforce(request, x_admin_role)


def main_secrets() -> list[str]:
    """The main admin secret as admin.verify_admin_secret knows it (read at import) and as the /v1 guards
    read it (os.environ per call). Both are the ECS ADMIN_SECRET; tests set one or the other."""
    from api.routers import admin

    return [s for s in (admin.ADMIN_SECRET, os.environ.get("ADMIN_SECRET", "")) if s]


def _is_valid_secret(x_admin_secret: Optional[str]) -> bool:
    """True when the header is the MAIN admin secret (constant time). Not the Preview credential."""
    if not x_admin_secret:
        return False
    return any(secrets_equal(x_admin_secret, s) for s in main_secrets())


def has_staff_credential(x_admin_secret: Optional[str]) -> bool:
    """AA-816 C9: the request is a staff (or machine) call — the staff bearer middleware verified its principal
    (a Cognito Bearer, or the Preview credential capped at viewer: api/core/staff_credential.py), or it carries
    the main admin secret."""
    from api.core.staff_credential import principal_verified

    return principal_verified() or _is_valid_secret(x_admin_secret)
