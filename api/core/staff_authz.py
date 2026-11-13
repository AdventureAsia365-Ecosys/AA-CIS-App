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

Roles and tenant scope (AA-1035 R2 slice 1, root ADR 0019 d3 / d4)
------------------------------------------------------------------
- Eight roles plus the read-only viewer (clarifications.md section 3 in the root S265 audit): admin, content_lead,
  content, tenant_qa, customer_success, support, finance, ai_ops. reviewer merged into content (CIS 363) and its
  alias is gone (CIS 375). Each role is one explicit capability set below: deny by default.
- A POLICY entry is one capability, or a tuple of capabilities of which the caller needs ANY (``any_of``), e.g. the
  Tenants directory read is tenant.read (every tenant), tenant.book.read (the caller's book) or billing.read.
- tenant.read keeps its meaning "read every tenant": only admin holds it. customer_success and support hold
  tenant.book.read instead, and api/core/tenant_scope.py require_tenant_scope (a dependency of every staff route with
  a {tenant_id} path parameter, enforced by tests/unit/test_aa1035_tenant_scope_routes.py) narrows them to their
  assigned tenants (support also the pool). tenant.book.read opens only the non-money Tenant 360 reads (details,
  config, audit, rewrite activity, markets, members, assignments); the account-manager reads of the book (plans and
  add-ons, subscription, usage, LLM cost, Tenant QA verdicts, the AI budget tile) need tenant.book.account.read,
  which customer_success holds and support does not (matrix: support has "-" on plans / invoices, Tenant QA and AI
  & spend).
- A GET with no POLICY entry needs content.read (the content pages). Before R2 it was open to every staff role; the
  roles that held content.read then are the roles that hold it now, so only the new roles without it are refused.

Coverage (GĐ3)
--------------
Every non-GET route under /admin, /v1/pipeline, /v1/rules and /v1/social has a POLICY entry, and every
router behind those prefixes runs staff_policy (or staff_policy_if_staff, for the routers whose own
guard also takes a tenant JWT or answers 401): tests/unit/test_aa882_route_policy.py enforces both and
keeps the snapshot. GETs stay open to every content.read role unless they return data that only some roles
need (tenant accounts, money, system ops, the staff list); those are in POLICY too. A GET with no
entry is open to every content.read role and to the machine (AA-1035).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

import structlog
from fastapi import Header, HTTPException, Request

from api.core import e2e_staff
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
# AA-1035 R2 (root ADR 0019 d3 / d4; clarifications.md section 3: "tenant.read today gates three things: the directory,
# tenant text and the atom store. Split it"). tenant.read stays "read EVERY tenant's account" (admin).
TENANT_CONTENT_READ = "tenant_content.read"  # tenant text: Tenant QA, Content Trace, A4 logs, Writing Lab text
CATALOG_READ = "catalog.read"                # the AA atom store (Atoms & routes)
TENANT_BOOK_READ = "tenant.book.read"        # Tenants directory / Tenant 360 of the caller's book (tenant_scope.py)
# AA-1035 (S267 review): the account manager's reads of the own book: plans and add-ons, subscription, usage, LLM cost,
# Tenant QA verdicts, the AI budget tile (customer_success; never support)
TENANT_BOOK_ACCOUNT_READ = "tenant.book.account.read"
TENANT_MEMBER_WRITE = "tenant.member.write"  # Content studio members: invite, send again, change role, remove
# AA-1035 (S267 fix pass): support only sends an existing invitation again (matrix: "send invitations again"); never
# invites, changes a role or removes. An owner invitation (and every owner invite / role change / removal) needs
# tenant.write in the route (admin_tenant360.py _require_owner_tier), whatever member capability the caller holds.
TENANT_MEMBER_RESEND = "tenant.member.resend"
# AA-1035 (S267 fix pass): the non-billing Tenant 360 writes of the caller's book (customer_success: config, brand
# identity, the RPM and publish limit of PATCH /admin/tenants/{id}); plan, suspend, key, offboard / restore and the
# entitlement overrides stay tenant.write / billing.write. Scoped by require_tenant_scope like every {tenant_id} route.
# AA-1036 R3 (clarifications TL-7 step 4): also the decision on an agency's add-on request (approve / decline).
TENANT_BOOK_WRITE = "tenant.book.write"
TENANT_ASSIGN = "tenant.assign"              # who looks after which tenant (shared.staff_tenant_assignment)
BILLING_READ = "billing.read"                # plan, usage and billing columns of every tenant
BILLING_WRITE = "billing.write"              # plan change and suspend / resume of a tenant (PATCH, field-level)
WAVE_RUN = "wave.run"                        # rerun waves (create, pause, resume, close, report)
LAB_READ = "lab.read"                        # Writing Lab reads without tenant text (releases: ids, statuses, numbers)
# AA-1035 R2 slice 2 (root ADR 0019 d5): open a read-only "view as tenant" session (shared.staff_view_as_session, at
# most 60 minutes, reason required, every request logged; api/core/view_as.py). Matrix: admin, customer_success (own
# book), support (own book + pool); require_tenant_scope narrows the tenant.
TENANT_VIEW_AS = "tenant.view_as"
# AA-1035 R2 slice 2 (root ADR 0019, settled by the tech lead S265 item 1): "ai_ops may read tenant text in Writing Lab:
# permission tenant_content.read, Lab pages only, every view logged". A capability of its own so it opens ONLY the
# Writing Lab tenant-text reads (/admin/writing-memory/*, never Tenant QA, Content Trace or Tenant 360), and every read
# through it writes an audit row first (api/core/lab_read_log.py; the route refuses with 503 when it cannot).
LAB_TENANT_TEXT_READ = "lab.tenant_text.read"
# AA-1035 R2 slice 2: the staff notification bell (Topbar, every staff role). Before R2 it rode on content.read; the
# tenant desk roles (customer_success, support, finance) hold no content.read, so the bell answered them 403. Each
# role sees only the rows meant for it (api/routers/admin.py notification_scope).
NOTIFICATIONS_READ = "notifications.read"
# AA-1035 R2 close-out (matrix "Tenant QA: customer_success R (own)"): the Tenant QA lists and details (T9 pieces, T2
# tour versions, gate stats) of the caller's book only. The routes filter by api/core/tenant_scope.tenant_scope_filter
# (a piece or version of another tenant is a 404); labels, place cards, compliance and every write stay
# tenant_content.read / quality.write.
TENANT_CONTENT_BOOK_READ = "tenant_content.book.read"
# AA-1091 (S278, tech lead: "the customer-service team must be able to enter prices in CIS easily") - the structured
# reference price of the AA tours (AA Catalogue > Reference prices, /admin/reference-prices; CIS 402). Read: the
# catalogue roles, customer_success and finance (not the read-only viewer, AA-878: refused on every listed read).
# Write (inline edit, accept, parse, Excel import): admin, content_lead and customer_success (the customer-service
# team's CIS role). Never a selling price (root ADR 0005 d7).
REFERENCE_PRICE_READ = "reference_price.read"
REFERENCE_PRICE_WRITE = "reference_price.write"
# S279 thresholds L1 (tech lead S279 Q1; CIS 407) — Platform › Settings, the admin-editable settings registry
# (/admin/settings/registry): propose a new value of a threshold, limit, cap or policy value (settings.write) and
# approve / reject a proposal (settings.approve). Admin role only; the delegated pages (finance billing, ai_ops
# gateway) keep their own capabilities.
SETTINGS_WRITE = "settings.write"
SETTINGS_APPROVE = "settings.approve"

_CONTENT_WORK = frozenset({CONTENT_READ, CONTENT_WRITE, REVIEW_ACT})

# AA-1035 R2: one explicit set per role (deny by default: a capability a role does not list here is refused). The
# matrix is root docs/audits/2026-11-03-S265-ecosystem-restructure/clarifications.md section 3; the column a row
# implements is named after it. Slice 2 (S268) adds view-as (tenant.view_as), the logged Writing Lab read of ai_ops
# (lab.tenant_text.read) and the bell of every role (notifications.read). The close-out (S269) adds the Tenant QA
# reads of the own tenants for customer_success (tenant_content.book.read; matrix "Tenant QA R (own)").
ROLE_PERMS: dict[str, frozenset[str]] = {
    "admin": _CONTENT_WORK | {
        MASTER_ADMIN, ATOMS_WRITE, PHOTOS_WRITE, A4_UNPUBLISH, QUALITY_WRITE, CONTENT_SETTINGS_WRITE,
        TENANT_READ, TENANT_WRITE, OPS_READ, OPS_WRITE, AI_ADMIN, SPEND_READ, SPEND_WRITE,
        STAFF_READ, AUDIT_READ, ALERTS_WRITE,
        TENANT_CONTENT_READ, CATALOG_READ, TENANT_BOOK_READ, TENANT_BOOK_ACCOUNT_READ, TENANT_MEMBER_WRITE,
        TENANT_ASSIGN, BILLING_READ, BILLING_WRITE, WAVE_RUN, LAB_READ, TENANT_MEMBER_RESEND, TENANT_BOOK_WRITE,
        TENANT_VIEW_AS, NOTIFICATIONS_READ, REFERENCE_PRICE_READ, REFERENCE_PRICE_WRITE, SETTINGS_WRITE,
        SETTINGS_APPROVE,
    },
    # AA Catalogue W, Tenant QA W, Writing Lab R (wave, review queue, master content, atoms / photos, SEO).
    "content_lead": _CONTENT_WORK | {
        MASTER_ADMIN, ATOMS_WRITE, PHOTOS_WRITE, A4_UNPUBLISH, QUALITY_WRITE, CONTENT_SETTINGS_WRITE,
        CATALOG_READ, TENANT_CONTENT_READ, LAB_READ, WAVE_RUN, NOTIFICATIONS_READ, REFERENCE_PRICE_READ,
        REFERENCE_PRICE_WRITE,
    },
    # AA Catalogue R + W of the writing (upload, S1 rewrite, review); never master lifecycle, atoms or tenants.
    "content": _CONTENT_WORK | {CATALOG_READ, NOTIFICATIONS_READ, REFERENCE_PRICE_READ},
    # Tenant QA W, AA Catalogue R, Writing Lab R.
    "tenant_qa": frozenset({CONTENT_READ, CATALOG_READ, TENANT_CONTENT_READ, QUALITY_WRITE, A4_UNPUBLISH, LAB_READ,
                            NOTIFICATIONS_READ}),
    # Tenants directory R / Tenant 360 W of the own book except billing (tenant_scope.py; tenant.book.write), members
    # and invitations but never the owner role (tenant.write); plans and add-ons R, AI & spend R and Tenant QA R of the
    # own tenants (tenant.book.account.read). Slice 2: read-only view-as of the own book (tenant.view_as). Close-out:
    # the Tenant QA lists and details of the own book (tenant_content.book.read).
    "customer_success": frozenset({
        TENANT_BOOK_READ, TENANT_BOOK_ACCOUNT_READ, TENANT_BOOK_WRITE, TENANT_MEMBER_WRITE, TENANT_MEMBER_RESEND,
        TENANT_VIEW_AS, NOTIFICATIONS_READ, TENANT_CONTENT_BOOK_READ, REFERENCE_PRICE_READ, REFERENCE_PRICE_WRITE,
    }),
    # Tenants directory R / Tenant 360 R of the own book and the pool; sends an existing non-owner invitation again
    # (tenant.member.resend), nothing else. Never plans, billing, usage, cost, Tenant QA or AI spend (no
    # tenant.book.account.read). Reset MFA of a tenant member has no CIS route (the CRM owns it). Slice 2: read-only
    # view-as of the own book and the pool (tenant.view_as; matrix "R (own + pool)").
    "support": frozenset({TENANT_BOOK_READ, TENANT_MEMBER_RESEND, TENANT_VIEW_AS, NOTIFICATIONS_READ}),
    # Billing columns and plans of every tenant, plan change and suspend for debt (field-level), AI & spend R.
    "finance": frozenset({BILLING_READ, BILLING_WRITE, SPEND_READ, NOTIFICATIONS_READ, REFERENCE_PRICE_READ}),
    # AI & spend W, Writing Lab W, AA Catalogue R. Slice 2: the tenant text of the Writing Lab, logged per read
    # (lab.tenant_text.read; never tenant_content.read, which would open Tenant QA and Content Trace too).
    "ai_ops": frozenset({
        CONTENT_READ, CATALOG_READ, AI_ADMIN, SPEND_READ, SPEND_WRITE, LAB_READ, LAB_TENANT_TEXT_READ,
        NOTIFICATIONS_READ,
    }),
    # AA-878 — read-only staff (the UI-smoke E2E account, migration 249). Reads the content pages; refused on
    # every write (READ_ONLY_ROLES below, whatever the route's capability) and on every admin-only read. Not
    # assignable from the Staff page (api/services/staff_directory.py CIS_ROLES) and no Booking token.
    "viewer": frozenset({CONTENT_READ, NOTIFICATIONS_READ}),
}

# AA-1035: the capability an unlisted GET needs (deny by default for a role without it).
OPEN_READ = CONTENT_READ

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

def any_of(*capabilities: str) -> tuple[str, ...]:
    """AA-1035: a POLICY entry the caller passes with ANY of these capabilities."""
    assert len(capabilities) >= 2, "any_of needs two or more capabilities; use the capability itself"
    return tuple(capabilities)


def policy_capabilities(entry: Any) -> tuple[str, ...]:
    """The capabilities of a POLICY entry (one capability or an any_of tuple) as a tuple; () for no entry."""
    if entry is None:
        return ()
    return (entry,) if isinstance(entry, str) else tuple(entry)


def policy_label(entry: Any) -> Optional[str]:
    """A POLICY entry as text ("a" or "a|b"), for logs, refusals and the route-policy snapshot."""
    caps = policy_capabilities(entry)
    return "|".join(caps) if caps else None

# AA-898 — read the owner-only docs pages of /admin/docs (access "owner": privacy / legal, infrastructure maps). Not a
# role capability and not a POLICY entry: the docs routes need ops.read and then filter their entries by
# can_read_owner_docs(). Kept out of CAPABILITIES so no route can be keyed on it by mistake.
DOCS_OWNER_READ = "docs.owner_read"

STAFF_ROLES: tuple[str, ...] = tuple(ROLE_PERMS)

# ── Route policy ─────────────────────────────────────────────────────────────────────────────────
# (method, route template) -> capability. Every staff write is here (GĐ3, enforced by the route-policy
# test); a GET is here only when its data is admin-only. Mapping rule: the S241 §5.1 table, then the
# page that calls the route (frontend/lib/rbac.ts PAGE_RULES) — an every-staff content page (Upload,
# S1, Review) keeps content.write / review.act, an admin-only page gets an admin capability.

_TENANT_ONE_READ = any_of(TENANT_READ, TENANT_BOOK_READ)                 # non-money Tenant 360 tab (scoped)
_TENANT_ACCOUNT_READ = any_of(TENANT_READ, TENANT_BOOK_ACCOUNT_READ)     # cost, quality of one tenant (scoped)
_TENANT_DIRECTORY_READ = any_of(TENANT_READ, TENANT_BOOK_READ, BILLING_READ)  # directory (money columns: admin.py)
_TENANT_BILLING_READ = any_of(TENANT_READ, TENANT_BOOK_ACCOUNT_READ, BILLING_READ)  # usage, plans, subscription
_TENANT_MEMBER_WRITE = any_of(TENANT_WRITE, TENANT_MEMBER_WRITE)
_TENANT_MEMBER_RESEND = any_of(TENANT_WRITE, TENANT_MEMBER_WRITE, TENANT_MEMBER_RESEND)
_TENANT_BOOK_WRITE = any_of(TENANT_WRITE, TENANT_BOOK_WRITE)                 # non-billing Tenant 360 writes (scoped)
_LAB_READ = any_of(TENANT_CONTENT_READ, LAB_READ)                          # Writing Lab reads without tenant text
# AA-1035 slice 2: Writing Lab reads WITH tenant text. ai_ops passes through lab.tenant_text.read, and each such read is
# logged before it runs (api/core/lab_read_log.py LAB_TEXT_LOGGED on the route; tests/unit/test_aa1035_s2_lab_read.py
# keeps the two in step and keeps this capability off every route outside /admin/writing-memory/).
_LAB_TEXT_READ = any_of(TENANT_CONTENT_READ, LAB_TENANT_TEXT_READ)
# AA-1035 close-out: Tenant QA lists / details; customer_success sees its book only (the routes filter by scope).
_TENANT_QA_READ = any_of(TENANT_CONTENT_READ, TENANT_CONTENT_BOOK_READ)

POLICY: dict[tuple[str, str], Any] = {
    # ── admin_llm_ops — stage model, fallback route, catalog (GĐ1)
    ("PATCH", "/admin/llm-config/{stage}"): AI_ADMIN,
    ("PATCH", "/admin/llm-config/{stage}/route"): AI_ADMIN,
    # AA-642 A1 — the stage kill switch (llm_role_config.is_active, audited)
    ("PATCH", "/admin/llm-config/{stage}/switch"): AI_ADMIN,
    ("PATCH", "/admin/llm-catalog/{model_key}"): AI_ADMIN,
    # AA-1002 — the LLM connection registry names secrets, roles and endpoints: admin-only read
    ("GET", "/admin/llm-connections"): AI_ADMIN,
    # AA-1002 S2 / S4 — switch a connection on / off, edit its secret name, endpoint and limits (audited)
    ("PATCH", "/admin/llm-connections/{connection_key}"): AI_ADMIN,
    # AA-1002 S4 — one small real (paid) call on a connection (logged in llm_call_log, audited)
    ("POST", "/admin/llm-connections/{connection_key}/test"): AI_ADMIN,
    # AA-1002 S10 — month-to-date spend per connection budget + the nightly reconciliation rows (admin-only read), and
    # queueing a reconcile run ($0; reads a usage key only on a connection that names one)
    ("GET", "/admin/llm-connections/costs"): AI_ADMIN,
    ("POST", "/admin/llm-connections/reconcile"): AI_ADMIN,
    # admin_llm_ops — on-demand / scheduled spend + Jev probes (the Lambda ones are MACHINE_ROUTES)
    ("POST", "/admin/dfs-balance/check"): OPS_WRITE,
    ("POST", "/admin/cost-explorer/check"): SPEND_WRITE,
    ("POST", "/admin/jev-canary/check"): OPS_WRITE,
    ("POST", "/admin/jev/402-sweep"): OPS_WRITE,
    # admin_llm_ops — money reads (Cost Operations page, admin-only)
    ("GET", "/admin/cost-explorer"): SPEND_READ,
    ("GET", "/admin/dfs-balance"): SPEND_READ,
    ("GET", "/admin/dfs-usage"): SPEND_READ,
    ("GET", "/admin/dfs-usage/by-country"): SPEND_READ,
    ("GET", "/admin/dfs-usage/runs"): SPEND_READ,          # AA-665 C2 — DFS spend by job
    ("GET", "/admin/cost-ops/forecast"): SPEND_READ,       # AA-665 C3 — month-end forecast vs budget
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
    ("DELETE", "/admin/budgets/llm/{scope}"): SPEND_WRITE,        # AA-1025 A7: remove a central LLM cap
    # ── admin_ai_gateway (AA-804) — AI providers page: every paid source's health / spend / balance and the job trace
    ("GET", "/admin/ai-gateway/providers"): SPEND_READ,
    ("GET", "/admin/ai-gateway/trace"): SPEND_READ,
    ("GET", "/admin/ai-gateway/budgets"): SPEND_READ,   # AA-804 UI 2: every cap by tier (Budgets tab)
    ("GET", "/admin/ai-gateway/calls"): SPEND_READ,     # AA-804 UI 3: every logged paid call (Calls tab)
    ("GET", "/admin/ai-gateway/experiments"): SPEND_READ,   # AA-900 UI (C4): prompt register + eval runs
    ("GET", "/admin/ai-gateway/access"): SPEND_READ,        # AA-1025 C1 follow-up: page writes this caller may use
    # AA-1016 UI (C4): the Tenant 360 tile; AA-1035: also customer_success for its book (tenant_scope.py), not support
    ("GET", "/admin/ai-gateway/tenant-budgets/{tenant_id}"): any_of(SPEND_READ, TENANT_BOOK_ACCOUNT_READ),
    ("PUT", "/admin/ai-gateway/tenant-budgets/{tenant_id}"): SPEND_WRITE,     # AA-1016 UI (C4), audited
    ("DELETE", "/admin/ai-gateway/tenant-budgets/{tenant_id}"): SPEND_WRITE,  # AA-1016 UI (C4), audited
    ("GET", "/admin/ai-gateway/credit"): SPEND_READ,      # AA-1025 B5: vendor API credit ledger + estimate (money)
    ("POST", "/admin/ai-gateway/credit/{connection_key}/entries"): SPEND_WRITE,   # AA-1025 B5, audited
    ("DELETE", "/admin/ai-gateway/credit/entries/{entry_id}"): SPEND_WRITE,       # AA-1025 B5, audited
    ("POST", "/admin/ai-gateway/credit/check"): SPEND_WRITE,   # AA-1025 B5: the low-credit check now (bell + e-mail)
    ("GET", "/admin/ai-gateway/app-profiles"): SPEND_READ,           # AA-1025 H1a: the app profile of every app
    ("PATCH", "/admin/ai-gateway/app-profiles/{app}"): AI_ADMIN,     # AA-1025 H1a: AI on / off per app, audited
    ("GET", "/admin/ai-gateway/capacity"): SPEND_READ,               # AA-1025 H2: TPM limits, lines, claim hold
    # AA-1076: the gateway batch lane (Batches tab): status + spend per batch; send a wave (paid) and cancel one
    ("GET", "/admin/ai-gateway/batches"): SPEND_READ,
    ("GET", "/admin/ai-gateway/batches/{job_id}"): SPEND_READ,
    ("POST", "/admin/ai-gateway/batches"): AI_ADMIN,               # paid: the AI & spend owner (ai.admin)
    ("POST", "/admin/ai-gateway/batches/{job_id}/cancel"): AI_ADMIN,
    # AA-1045 — the usage explorer (AI & spend): money reads; the saved views are the caller's own (spend.read, like the
    # notification bell's own state); a re-roll of the rollup is an ops write ($0, audited)
    ("GET", "/admin/usage-explorer/query"): SPEND_READ,
    ("GET", "/admin/usage-explorer/options"): SPEND_READ,
    ("GET", "/admin/usage-explorer/invoices"): SPEND_READ,
    ("GET", "/admin/usage-explorer/views"): SPEND_READ,
    ("POST", "/admin/usage-explorer/views"): SPEND_READ,
    ("DELETE", "/admin/usage-explorer/views/{view_id}"): SPEND_READ,
    ("GET", "/admin/usage-explorer/rollup/check"): SPEND_READ,
    ("POST", "/admin/usage-explorer/rollup/run"): OPS_WRITE,
    ("PUT", "/admin/ai-gateway/capacity/{connection_key}/{model_key}"): AI_ADMIN,     # AA-1025 H2: audited
    ("DELETE", "/admin/ai-gateway/capacity/{connection_key}/{model_key}"): AI_ADMIN,  # AA-1025 H2: audited
    # AA-1042 (S268): the Route control tab (order mode, chains, provider order, presets, versions); writes audited
    ("GET", "/admin/ai-gateway/route-control"): SPEND_READ,
    # AA-1042 (S270): the Guide tab, one fixed staff-tier docs page (ARCH-01-ai-gateway) + the live gateway tables
    ("GET", "/admin/ai-gateway/guide"): SPEND_READ,
    # AA-1064: an image that guide page links (staff tier, only paths in the guide's doc.assets)
    ("GET", "/admin/ai-gateway/guide/asset"): SPEND_READ,
    ("POST", "/admin/ai-gateway/route-control/preview"): AI_ADMIN,              # $0, no model call
    ("POST", "/admin/ai-gateway/route-control/save"): AI_ADMIN,
    ("POST", "/admin/ai-gateway/route-control/presets/{preset}"): AI_ADMIN,
    ("GET", "/admin/ai-gateway/route-control/versions"): SPEND_READ,
    ("GET", "/admin/ai-gateway/route-control/versions/{version}"): SPEND_READ,
    ("POST", "/admin/ai-gateway/route-control/versions/{version}/revert"): AI_ADMIN,
    # AA-1042 (admin_llm_route) — AI gateway > Trace: route trace reads, the route alert policy, the route test
    ("GET", "/admin/ai-gateway/route-trace/requests"): SPEND_READ,
    ("GET", "/admin/ai-gateway/route-trace/requests/{trace_id}"): SPEND_READ,
    ("GET", "/admin/ai-gateway/route-trace/rungs"): SPEND_READ,
    ("GET", "/admin/ai-gateway/route-trace/fallback-content"): SPEND_READ,          # A1 catalog versions only
    ("POST", "/admin/ai-gateway/route-trace/test"): AI_ADMIN,          # a few tiny paid calls, audited
    ("GET", "/admin/ai-gateway/route-policy"): SPEND_READ,
    ("PUT", "/admin/ai-gateway/route-policy/{stage}"): AI_ADMIN,       # audited llm_route_policy_set
    ("DELETE", "/admin/ai-gateway/route-policy/{stage}"): AI_ADMIN,    # audited llm_route_policy_cleared
    ("POST", "/admin/ai-gateway/route-watch/run"): AI_ADMIN,           # enqueue llm_route_watch ($0)
    # ── admin_job_runner — job control + retention
    ("POST", "/admin/job-runner/jobs/{job_id}/cancel"): OPS_WRITE,
    ("POST", "/admin/job-runner/jobs/{job_id}/retry"): OPS_WRITE,
    ("POST", "/admin/job-runner/jobs/{job_id}/release-jev-cap"): OPS_WRITE,   # AA-1083 (audited)
    ("POST", "/admin/job-runner/jobs/retry-llm-denied"): OPS_WRITE,      # AA-1018 (DRY RUN by default, audited)
    ("POST", "/admin/job-runner/llm-probe/run"): AI_ADMIN,               # AA-1018 (DRY RUN by default)
    ("POST", "/admin/job-runner/embedding-reembed/run"): AI_ADMIN,       # AA-1026 D5 (DRY RUN by default)
    ("POST", "/admin/job-runner/llm-contract-smoke/run"): AI_ADMIN,      # AA-1026 I6 (DRY RUN by default)
    ("POST", "/admin/job-runner/llm-model-lifecycle/run"): AI_ADMIN,     # AA-1024 D2 ($0 Bedrock list + catalog write)
    ("POST", "/admin/job-runner/retention/run"): OPS_WRITE,
    ("POST", "/admin/job-runner/retention/schedule"): OPS_WRITE,
    ("POST", "/admin/job-runner/staff-reconcile/schedule"): OPS_WRITE,   # AA-816 C7, daily (MACHINE_ROUTES)
    # AA-839 — data erasure: tenant accounts and DSARs are admin-only (an apply also needs Cognito + the env flag)
    ("GET", "/admin/job-runner/erasure"): TENANT_READ,
    ("POST", "/admin/job-runner/erasure/run"): TENANT_WRITE,
    # ── admin_decisions — Jev question mode/floors, Jev credit ledger
    ("PUT", "/admin/decisions/questions/{question_key}"): AI_ADMIN,
    ("POST", "/admin/decisions/jev-credit/topups"): SPEND_WRITE,
    ("DELETE", "/admin/decisions/jev-credit/topups/{topup_id}"): SPEND_WRITE,
    ("GET", "/admin/decisions/jev-credit"): SPEND_READ,   # top-ups + estimated balance (money)
    ("POST", "/admin/decisions/jev-credit/check"): SPEND_WRITE,   # AA-845 low-credit check (+ test alerts); money
    # ── admin_settings — SEO config of the aa_internal tenant
    ("PATCH", "/admin/settings/seo"): CONTENT_SETTINGS_WRITE,

    # ── admin (admin.py) — tenant accounts (Tenants page, admin-only; AA-797 already re-checks writes)
    ("GET", "/admin/tenants"): _TENANT_DIRECTORY_READ,     # AA-1035: filtered to the caller's scope
    # AA-1035 (S267 fix pass): the shared tenant picker (id, name, slug, status only; filtered to the caller's scope):
    # every role whose pages pick or name a tenant (directory, Tenant QA, Writing Lab, AI & spend), never content.read
    ("GET", "/admin/tenant-names"): any_of(TENANT_READ, TENANT_CONTENT_READ, TENANT_BOOK_READ, BILLING_READ, LAB_READ,
                                           SPEND_READ),
    ("POST", "/admin/tenants"): TENANT_WRITE,
    # AA-1028 R1 slice 2 — the tenant_provision saga (admin_tenant_provision.py): create with subscriptions, add-ons
    ("POST", "/admin/tenants/provision"): TENANT_WRITE,
    ("POST", "/admin/tenants/{tenant_id}/provision"): TENANT_WRITE,
    ("GET", "/admin/tenants/provision/{job_id}"): TENANT_READ,
    # AA-1028 R1 slice 3 — saga cancel of add-ons, Tenant 360 › Subscription (overrides), the plan catalogue
    ("POST", "/admin/tenants/{tenant_id}/provision/cancel"): TENANT_WRITE,
    ("GET", "/admin/tenants/{tenant_id}/subscription"): _TENANT_BILLING_READ,
    ("PUT", "/admin/tenants/{tenant_id}/entitlement-overrides/{feature_key}"): TENANT_WRITE,
    ("DELETE", "/admin/tenants/{tenant_id}/entitlement-overrides/{feature_key}"): TENANT_WRITE,
    # AA-1038 R5 slice 3 follow-up (S272): the TripPlanner lead route of a tenant (CIS 390), on the Subscription tab
    ("GET", "/admin/tenants/{tenant_id}/tripplanner-site/lead-route"): _TENANT_BILLING_READ,
    ("PUT", "/admin/tenants/{tenant_id}/tripplanner-site/lead-route"): TENANT_WRITE,
    # AA-1036 R3 (root ADR 0019 d7; clarifications TL-7 step 4: the account manager approves): add-on requests. A
    # decision: admin, or customer_success for its own book (tenant.book.write; require_tenant_scope); never support or
    # finance. The approval runs the tenant_provision saga; the drawer polls the request route, not the job route.
    ("GET", "/admin/tenants/{tenant_id}/addon-requests"): _TENANT_BILLING_READ,
    ("GET", "/admin/tenants/{tenant_id}/addon-requests/{request_id}"): _TENANT_BILLING_READ,
    ("POST", "/admin/tenants/{tenant_id}/addon-requests/{request_id}/approve"): _TENANT_BOOK_WRITE,
    ("POST", "/admin/tenants/{tenant_id}/addon-requests/{request_id}/decline"): _TENANT_BOOK_WRITE,
    ("GET", "/admin/plans"): _TENANT_BILLING_READ,
    ("GET", "/admin/plans/features"): _TENANT_BILLING_READ,
    ("POST", "/admin/plans"): TENANT_WRITE,
    ("PATCH", "/admin/plans/{plan_key}"): TENANT_WRITE,
    ("POST", "/admin/plans/{plan_key}/retire"): TENANT_WRITE,
    # AA-1035: field-level in the route (finance: plan_tier / is_active only; customer_success: every other field;
    # admin.py _require_tenant_patch)
    ("PATCH", "/admin/tenants/{tenant_id}"): any_of(TENANT_WRITE, BILLING_WRITE, TENANT_BOOK_WRITE),
    ("GET", "/admin/tenants/{tenant_id}/usage"): _TENANT_BILLING_READ,
    # AA-1037 (root ADR 0019 d9 "Screens now"): Tenant 360 › Billing › Booking invoices, the agency's billing status
    # from Booking's contract view (read only). The Billing tab's rule: billing.read on every tenant, or
    # tenant.book.account.read on the caller's book (require_tenant_scope); never support.
    ("GET", "/admin/tenants/{tenant_id}/booking-billing"): _TENANT_BILLING_READ,
    ("GET", "/admin/tenants/{tenant_id}/audit"): _TENANT_ONE_READ,
    ("GET", "/admin/tenants/{tenant_id}/rewrite-activity"): _TENANT_ONE_READ,
    ("GET", "/admin/tenants/{tenant_id}/config"): _TENANT_ONE_READ,
    ("PUT", "/admin/tenants/{tenant_id}/config"): _TENANT_BOOK_WRITE,           # AA-1035: + customer_success, scoped
    ("PUT", "/admin/tenants/{tenant_id}/brand-identity"): _TENANT_BOOK_WRITE,   # AA-1035: + customer_success, scoped
    ("POST", "/admin/tenants/{tenant_id}/offboard"): TENANT_WRITE,
    ("POST", "/admin/tenants/{tenant_id}/restore"): TENANT_WRITE,
    # AA-1035: GET /admin/tenants/{tenant_id}/details — Master Content (every content.read role) reads the aa_internal
    # catalog through it; require_tenant_scope keeps those roles on the aa_internal tenant (scope "internal"). finance
    # (billing.read) opens Tenant 360 on it too and gets the header only (no tours, runs or brand text: admin.py).
    ("GET", "/admin/tenants/{tenant_id}/details"): any_of(CONTENT_READ, TENANT_READ, TENANT_BOOK_READ, BILLING_READ),
    # admin — master tour lifecycle (S241 §5.1 master.admin; Master Content page)
    ("PATCH", "/admin/master/{tour_id}/activate"): MASTER_ADMIN,
    ("PATCH", "/admin/master/{tour_id}/deactivate"): MASTER_ADMIN,
    ("PATCH", "/admin/master/{tour_id}/status"): MASTER_ADMIN,
    ("PATCH", "/admin/master/{tour_id}/trash"): MASTER_ADMIN,
    ("PATCH", "/admin/master/{tour_id}/restore"): MASTER_ADMIN,
    # AA-986 L2: the Retire / Trash confirm dialog's dry run. Admin-only like the actions it previews: it runs the
    # real action in a rolled-back transaction (row locks, job and notification writes) and returns per-tenant
    # derivative counts.
    ("GET", "/admin/tours/{tour_id}/lifecycle-preview"): MASTER_ADMIN,
    # admin — source tours (Upload page, every staff role)
    ("PATCH", "/admin/tours/{tour_id}/trash"): CONTENT_WRITE,
    ("PATCH", "/admin/tours/{tour_id}/restore"): CONTENT_WRITE,
    # admin — the notification bell (Topbar, every staff role)
    # AA-1035 slice 2: its own capability (every role holds it); each role sees only its rows (admin.py
    # notification_scope). The viewer still never marks a row read (READ_ONLY_ROLES).
    ("GET", "/admin/notifications"): NOTIFICATIONS_READ,
    ("GET", "/admin/notifications/count"): NOTIFICATIONS_READ,
    ("PUT", "/admin/notifications/read-all"): NOTIFICATIONS_READ,
    ("PUT", "/admin/notifications/{notif_id}/read"): NOTIFICATIONS_READ,
    # ── admin_tenant360 — Tenant 360 tabs (admin-only)
    ("GET", "/admin/tenants/{tenant_id}/cost"): _TENANT_ACCOUNT_READ,       # LLM cost, $/piece, margin
    ("GET", "/admin/tenants/{tenant_id}/quality"): _TENANT_ACCOUNT_READ,    # T10 verdicts, held reasons
    ("GET", "/admin/tenants/{tenant_id}/markets"): _TENANT_ONE_READ,
    # AA-978 — Tenant 360 Members (CIS memberships; a removal also signs the person out of the partner CRM)
    ("GET", "/admin/tenants/{tenant_id}/members"): _TENANT_ONE_READ,
    # AA-1031 (S264 F3): which member write controls this caller may use on the tenant (UI hint, denial_reason)
    ("GET", "/admin/tenants/{tenant_id}/members/access"): _TENANT_ONE_READ,
    ("PATCH", "/admin/tenants/{tenant_id}/members/{membership_id}"): _TENANT_MEMBER_WRITE,
    ("POST", "/admin/tenants/{tenant_id}/members/{membership_id}/remove"): _TENANT_MEMBER_WRITE,
    # AA-979 P2b — Content studio invitations (CIS row + the CRM delivers the AA system mail)
    ("POST", "/admin/tenants/{tenant_id}/members/invitations"): _TENANT_MEMBER_WRITE,
    # AA-1035 (S267 fix pass): support sends again (tenant.member.resend); an owner invitation needs tenant.write
    ("POST", "/admin/tenants/{tenant_id}/members/{membership_id}/invitation/send"): _TENANT_MEMBER_RESEND,
    # AA-1035 R2 (root ADR 0019 d4) — who looks after the tenant (shared.staff_tenant_assignment, CIS 364)
    ("GET", "/admin/tenants/{tenant_id}/assignments"): _TENANT_ONE_READ,
    ("PUT", "/admin/tenants/{tenant_id}/assignments/{admin_user_id}"): TENANT_ASSIGN,
    ("DELETE", "/admin/tenants/{tenant_id}/assignments/{admin_user_id}"): TENANT_ASSIGN,
    # AA-1035 R2 slice 2 — the Assignments page (admin_tenant_assignments.py): every assignment of the caller's scope
    # (tenant_scope_filter) and, for whoever assigns, the staff members that may be assigned.
    ("GET", "/admin/tenant-assignments"): _TENANT_ONE_READ,
    ("GET", "/admin/tenant-assignments/staff"): TENANT_ASSIGN,
    # AA-1035 R2 slice 2 (root ADR 0019 d5) — read-only "view as tenant" (admin_view_as.py, api/core/view_as.py): start
    # one (scoped: require_tenant_scope), read / end the caller's own (admin: every session; ending another's revokes
    # it)
    ("POST", "/admin/tenants/{tenant_id}/view-as"): TENANT_VIEW_AS,
    ("GET", "/admin/view-as/sessions"): TENANT_VIEW_AS,
    ("GET", "/admin/view-as/sessions/{session_id}"): TENANT_VIEW_AS,
    ("POST", "/admin/view-as/sessions/{session_id}/end"): TENANT_VIEW_AS,

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
    ("GET", "/admin/billing"): any_of(TENANT_READ, BILLING_READ),
    ("GET", "/admin/infra/status"): OPS_READ,

    # ── admin_atoms — Social Content atoms (Atom Curation page, admin-only)
    ("POST", "/admin/atoms/atomize"): ATOMS_WRITE,
    ("PATCH", "/admin/atoms/{atom_id}"): ATOMS_WRITE,
    # AA-1035: the AA atom store is catalog.read (was tenant.read, which also gated the directory and tenant text)
    ("GET", "/admin/atoms"): CATALOG_READ,
    ("GET", "/admin/atoms/summary"): CATALOG_READ,
    ("GET", "/admin/atoms/unatomized-tours"): CATALOG_READ,
    # ── admin_a4 — Content Trace force-unpublish (admin-only)
    ("POST", "/admin/a4/publish-log/{publish_id}/unpublish"): A4_UNPUBLISH,
    # admin_a4 — every tenant's A4 content, publish and review logs (Content Trace, A4 Quality, Tenants)
    ("GET", "/admin/a4/content-log"): TENANT_CONTENT_READ,
    ("GET", "/admin/a4/gate-telemetry"): TENANT_CONTENT_READ,
    ("GET", "/admin/a4/platform-stats"): TENANT_CONTENT_READ,
    ("GET", "/admin/a4/publish-log"): TENANT_CONTENT_READ,
    ("GET", "/admin/a4/review-log"): TENANT_CONTENT_READ,
    # ── admin_content_quality — T2/T9 quality (admin-only page; the BFF API_WRITE_RULES agree).
    # The reads show every tenant's pieces and tour versions: tenant_content.read; the lists, details and stats also
    # tenant_content.book.read (customer_success: the own book only, filtered in the routes).
    ("GET", "/admin/content-quality/pieces"): _TENANT_QA_READ,
    # AA-1042 (admin_llm_route.quality_router): every tenant's T2 versions / T9 pieces a fallback model wrote
    ("GET", "/admin/content-quality/fallback-writes"): TENANT_CONTENT_READ,
    ("GET", "/admin/content-quality/pieces/{piece_id}"): _TENANT_QA_READ,
    ("GET", "/admin/content-quality/pieces/{piece_id}/label"): TENANT_CONTENT_READ,
    ("GET", "/admin/content-quality/place-cards"): TENANT_CONTENT_READ,
    ("GET", "/admin/content-quality/place-cards/duplicates"): TENANT_CONTENT_READ,
    ("GET", "/admin/content-quality/place-cards/{card_id}"): TENANT_CONTENT_READ,
    ("GET", "/admin/content-quality/stats"): _TENANT_QA_READ,
    ("GET", "/admin/content-quality/tour-versions"): _TENANT_QA_READ,
    ("GET", "/admin/content-quality/tour-versions/{version_id}"): _TENANT_QA_READ,
    ("GET", "/admin/content-quality/tour-versions/{version_id}/label"): TENANT_CONTENT_READ,     # AA-923 WM-32
    ("GET", "/admin/content-quality/a1-content/{content_id}/label"): TENANT_CONTENT_READ,
    ("GET", "/admin/tenant-qa/compliance"): TENANT_CONTENT_READ,                                  # AA-1039 R6
    ("POST", "/admin/tenant-qa/compliance/{flag_id}/release"): QUALITY_WRITE,             # AA-1039 R6
    ("POST", "/admin/content-quality/pieces/{piece_id}/label"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/tour-versions/{version_id}/label"): QUALITY_WRITE,    # AA-923 WM-32
    ("POST", "/admin/content-quality/a1-content/{content_id}/label"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/pieces/{piece_id}/sentences/suggest"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/pieces/{piece_id}/sentences/apply"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/tour-versions/{version_id}/sentences/suggest"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/tour-versions/{version_id}/sentences/apply"): QUALITY_WRITE,
    # AA-916 WM-07 — the example preference on an admin sentence edit (writing signal) of this piece / version
    ("PATCH", "/admin/content-quality/pieces/{piece_id}/signals/{signal_id}"): QUALITY_WRITE,
    ("PATCH", "/admin/content-quality/tour-versions/{version_id}/signals/{signal_id}"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/requests/{request_id}/regenerate-angles"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/place-cards/build"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/place-cards/rebuild"): QUALITY_WRITE,  # AA-784
    ("PATCH", "/admin/content-quality/place-cards/{card_id}"): QUALITY_WRITE,
    ("PATCH", "/admin/content-quality/place-cards/{card_id}/facts"): QUALITY_WRITE,
    ("POST", "/admin/content-quality/place-cards/{keep_id}/merge"): QUALITY_WRITE,
    # AA-814 — platform Facts Entries (every tenant's T9 seed reads them); reads open to every staff role
    ("POST", "/admin/facts"): QUALITY_WRITE,
    ("PATCH", "/admin/facts/{fact_id}"): QUALITY_WRITE,
    ("DELETE", "/admin/facts/{fact_id}"): QUALITY_WRITE,
    # AA-1091 — AA Catalogue › Reference prices (the customer-service team's CIS page; customer_success has no
    # content.read, so every read has an entry too)
    ("GET", "/admin/reference-prices"): REFERENCE_PRICE_READ,
    ("GET", "/admin/reference-prices/export"): REFERENCE_PRICE_READ,
    ("GET", "/admin/reference-prices/{source_key}/history"): REFERENCE_PRICE_READ,
    ("PATCH", "/admin/reference-prices/{source_key}"): REFERENCE_PRICE_WRITE,
    ("DELETE", "/admin/reference-prices/{source_key}"): REFERENCE_PRICE_WRITE,
    ("POST", "/admin/reference-prices/accept"): REFERENCE_PRICE_WRITE,
    ("POST", "/admin/reference-prices/parse"): REFERENCE_PRICE_WRITE,
    ("POST", "/admin/reference-prices/import"): REFERENCE_PRICE_WRITE,
    # S279 thresholds L1 — Platform › Settings (the settings registry, CIS 407): admin only
    ("GET", "/admin/quality/thresholds"): any_of(CONTENT_READ, TENANT_BOOK_READ),   # S279 L2: every score view's bands
    ("GET", "/admin/settings/registry"): OPS_READ,
    ("GET", "/admin/settings/registry/{key}/history"): OPS_READ,
    ("POST", "/admin/settings/registry/{key}/proposals"): SETTINGS_WRITE,
    ("POST", "/admin/settings/registry/proposals/{value_id}/approve"): SETTINGS_APPROVE,
    ("POST", "/admin/settings/registry/proposals/{value_id}/reject"): SETTINGS_APPROVE,
    # ── admin_photos — Drive sync + assignment (Photos page, admin-only)
    ("POST", "/admin/photos/sync"): PHOTOS_WRITE,
    ("POST", "/admin/photos/sync/schedule"): PHOTOS_WRITE,   # AA-844, daily (MACHINE_ROUTES)
    ("POST", "/admin/photos/medium-backfill"): PHOTOS_WRITE,   # AA-995, 960 w rendition backfill
    ("POST", "/admin/photos/destination-match"): PHOTOS_WRITE,   # AA-671 S257, photo -> place of its tour
    ("POST", "/admin/photos/{photo_id}/assign"): PHOTOS_WRITE,
    # ── admin_places — place_extract backfill (paid geocoder jobs)
    ("POST", "/admin/places/backfill"): OPS_WRITE,
    # admin_places — AA-847 content-validity fixes (precision / hide / merge destinations; audit + undo)
    ("POST", "/admin/places/validity/fix"): OPS_WRITE,
    # admin_places — AA-847 line geometry: shapes backfill (Geoapify Place Details credits) and manual GeoJSON
    ("POST", "/admin/places/shapes/backfill"): OPS_WRITE,
    ("PUT", "/admin/places/{destination_id}/shape"): OPS_WRITE,
    ("DELETE", "/admin/places/{destination_id}/shape"): OPS_WRITE,
    # admin_places — AA-847 Problem 2: automatic duplicate merge (undo a merge, allow-list / approve a dry run, run)
    ("POST", "/admin/places/merges/{merge_id}/undo"): OPS_WRITE,
    ("POST", "/admin/places/dedupe/countries"): OPS_WRITE,
    ("POST", "/admin/places/dedupe/run"): OPS_WRITE,
    # admin_places — AA-997 re-locate model-placed destinations (Geoapify credits; dry run first)
    ("POST", "/admin/places/relocate"): OPS_WRITE,
    # ── admin_segment_research — DFS demand research (paid run; preview is a free dry run)
    ("POST", "/admin/segment-research/preview"): OPS_READ,
    ("POST", "/admin/segment-research/run"): OPS_WRITE,
    # ── admin_waves — rerun waves (check is the ops-probe Lambda, a MACHINE_ROUTE)
    # AA-1035: content_lead runs waves (wave.run); the scheduled check stays ops (the Lambda, a MACHINE_ROUTE)
    ("POST", "/admin/waves"): any_of(OPS_WRITE, WAVE_RUN),
    ("POST", "/admin/waves/{wave_id}/close"): any_of(OPS_WRITE, WAVE_RUN),
    ("POST", "/admin/waves/{wave_id}/pause"): any_of(OPS_WRITE, WAVE_RUN),    # AA-1025 H2: bulk jobs wait in the queue
    ("POST", "/admin/waves/{wave_id}/resume"): any_of(OPS_WRITE, WAVE_RUN),   # AA-1025 H2
    ("POST", "/admin/waves/check"): OPS_WRITE,
    ("GET", "/admin/waves"): any_of(OPS_READ, WAVE_RUN),
    ("GET", "/admin/waves/{wave_id}/report"): any_of(OPS_READ, WAVE_RUN),   # incl. the wave's LLM cost
    # ── admin_overview — whole-system snapshot incl. spend (admin-only page; CI smoke = machine)
    ("GET", "/admin/overview"): OPS_READ,
    ("GET", "/admin/overview/ops"): OPS_READ,   # AA-858 business view incl. cost per app (admin-only page)
    ("GET", "/admin/data-coverage"): OPS_READ,  # AA-992 data coverage counts, incl. CRM over crm.ops.v1 (admin-only)
    # ── admin_reports (AA-1003) — report register, runs, generate now (admin-only page)
    ("GET", "/admin/reports"): OPS_READ,
    ("GET", "/admin/reports/runs/{run_id}"): OPS_READ,
    ("GET", "/admin/reports/{report_id}/runs"): OPS_READ,
    ("POST", "/admin/reports/{report_id}/run"): OPS_WRITE,
    ("POST", "/admin/reports/snapshots"): OPS_WRITE,
    # AA-900 (S271 W0): the CIS-08 content quality funnel snapshot (numbers only, no tenant text)
    ("GET", "/admin/quality/funnel"): any_of(OPS_READ, LAB_READ),
    ("GET", "/admin/quality/lineage/coverage"): any_of(OPS_READ, LAB_READ),            # AA-1052 W6
    ("GET", "/admin/quality/lineage/fanout/{tour_id}"): any_of(OPS_READ, LAB_READ),        # AA-1052 W6 fan-out
    ("POST", "/admin/quality/lineage/fanout/{tour_id}/recheck"): QUALITY_WRITE,      # $0 re-check job
    ("GET", "/admin/quality/lineage/{workflow}/{object_id}"): any_of(OPS_READ, LAB_READ),
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
    ("POST", "/v1/pipeline/review-queue/{review_id}/approve"): REVIEW_ACT,
    ("POST", "/v1/pipeline/review-queue/{review_id}/reject"): REVIEW_ACT,

    # ── admin_platform (AA-816 C7) — staff directory health: the reconcile report (WORKFORCE vs the
    # directory) and an on-demand run. Report only, so staff.read (not the owner-only staff.write).
    ("GET", "/admin/platform/staff-directory/health"): STAFF_READ,
    ("POST", "/admin/platform/staff-directory/reconcile"): STAFF_READ,

    # ── admin_platform_ops (AA-884, GĐ7/8/10) — Apps (health/version), the staff audit trail, cost per app.
    ("GET", "/admin/platform/apps"): OPS_READ,
    ("GET", "/admin/platform/apps/{app_id}"): OPS_READ,
    ("GET", "/admin/platform/apps/crm/jobs"): OPS_READ,  # AA-936: CRM jobs (crm.ops.v1), staff token relayed
    ("GET", "/admin/platform/audit"): AUDIT_READ,
    ("GET", "/admin/platform/audit/verify"): AUDIT_READ,
    ("GET", "/admin/platform/cost"): SPEND_READ,
    # ── admin_crm_console (AA-941, CRM-13a S-01) — the partner CRM's L1 console, relayed with the staff member's own
    # Cognito token; the CRM re-checks its own groups (cis-admin / crm-platform-admin read, crm-platform-admin writes).
    # AA-1035 R2 close-out (root ADR 0019 d4): the desk roles read the CRM L1 console of their book. CIS scopes the
    # {tenant_id} reads (require_tenant_scope) and filters the agency list (admin_crm_console.py); the CRM serves a
    # desk caller only the tenants CIS reports for it (tenant.membership.v1 getStaffTenantScope). Usage and
    # entitlements are the account reads (customer_success, never support); writes stay tenant.write.
    ("GET", "/admin/platform/crm/console"): _TENANT_ONE_READ,
    ("GET", "/admin/platform/crm/agencies"): _TENANT_ONE_READ,
    ("POST", "/admin/platform/crm/agencies"): TENANT_WRITE,
    ("GET", "/admin/platform/crm/agencies/{tenant_id}"): _TENANT_ONE_READ,
    ("GET", "/admin/platform/crm/agencies/{tenant_id}/usage"): _TENANT_ACCOUNT_READ,
    ("GET", "/admin/platform/crm/agencies/{tenant_id}/entitlements"): _TENANT_ACCOUNT_READ,
    ("GET", "/admin/platform/crm/agencies/{tenant_id}/data-requests"): _TENANT_ONE_READ,
    ("GET", "/admin/platform/crm/agencies/{tenant_id}/members"): _TENANT_ONE_READ,
    ("GET", "/admin/platform/crm/agencies/{tenant_id}/jobs"): _TENANT_ONE_READ,
    ("POST", "/admin/platform/crm/agencies/{tenant_id}/lock"): TENANT_WRITE,
    ("POST", "/admin/platform/crm/agencies/{tenant_id}/unlock"): TENANT_WRITE,
    ("POST", "/admin/platform/crm/agencies/{tenant_id}/owner-invite"): TENANT_WRITE,
    ("POST", "/admin/platform/crm/agencies/{tenant_id}/owner-invite/withdraw"): TENANT_WRITE,  # S263
    ("POST", "/admin/platform/crm/agencies/{tenant_id}/delete"): TENANT_WRITE,  # S263: synthetic agencies only
    ("POST", "/admin/platform/crm/agencies/{tenant_id}/data-requests"): TENANT_WRITE,
    ("POST", "/admin/platform/crm/agencies/{tenant_id}/owner-recovery"): TENANT_WRITE,
    ("POST", "/admin/platform/crm/agencies/{tenant_id}/owner-recovery/{request_id}/complete"): TENANT_WRITE,
    ("POST", "/admin/platform/crm/agencies/{tenant_id}/members/{member_id}/deactivate"): TENANT_WRITE,
    ("PUT", "/admin/platform/crm/agencies/{tenant_id}/entitlements-allowed"): TENANT_WRITE,
    ("PUT", "/admin/platform/crm/agencies/{tenant_id}/insights-opt-out"): TENANT_WRITE,
    ("POST", "/admin/platform/crm/agencies/{tenant_id}/jobs/{job_id}/retry"): TENANT_WRITE,

    # ── admin_alert_recipients (AA-913) — Platform › Alert recipients. Reads ops.read (admin); changes alerts.write
    # (admin) and, in the router, a Cognito session for add / remove / resend / test (not for the status refresh).
    ("GET", "/admin/platform/alert-recipients"): OPS_READ,
    ("POST", "/admin/platform/alert-recipients"): ALERTS_WRITE,
    ("POST", "/admin/platform/alert-recipients/refresh"): ALERTS_WRITE,
    ("POST", "/admin/platform/alert-recipients/test-email"): ALERTS_WRITE,
    ("DELETE", "/admin/platform/alert-recipients/{recipient_id}"): ALERTS_WRITE,
    ("POST", "/admin/platform/alert-recipients/{recipient_id}/resend"): ALERTS_WRITE,

    # ── admin_aa_content (AA-977, CRM-B5) — AA-authored content for the partner CRM. Reads open (content.read, the
    # viewer reads); drafts are content work (content.write: the content team); publish / retire / restore change
    # what agencies receive over catalog.http.v1, so admin only (master.admin, the Master Content lifecycle tier).
    ("POST", "/admin/aa-content"): CONTENT_WRITE,
    ("PUT", "/admin/aa-content/{content_id}/draft"): CONTENT_WRITE,
    ("DELETE", "/admin/aa-content/{content_id}/draft"): CONTENT_WRITE,
    ("POST", "/admin/aa-content/{content_id}/publish"): MASTER_ADMIN,
    ("POST", "/admin/aa-content/{content_id}/retire"): MASTER_ADMIN,
    ("POST", "/admin/aa-content/{content_id}/restore"): MASTER_ADMIN,

    # ── admin_writing_memory (AA-917 WM-14) — Quality › Writing memory: signals and tenant rules hold tenant text, so
    # every read is admin only (tenant.read); every write (fact verdict, candidate edit / reject, a job run — the mining
    # run is paid) is ai.admin, the tier of the LLM stage config. Non-lexicon rules activate through releases (AA-919).
    ("GET", "/admin/writing-memory/summary"): _LAB_TEXT_READ,
    ("GET", "/admin/writing-memory/signals"): _LAB_TEXT_READ,
    ("GET", "/admin/writing-memory/signals/clusters"): _LAB_TEXT_READ,
    ("GET", "/admin/writing-memory/signals/{signal_id}"): _LAB_TEXT_READ,
    ("PATCH", "/admin/writing-memory/signals/{signal_id}/fact"): AI_ADMIN,
    ("GET", "/admin/writing-memory/candidates"): _LAB_TEXT_READ,
    ("GET", "/admin/writing-memory/candidates/{rule_id}"): _LAB_TEXT_READ,
    ("PATCH", "/admin/writing-memory/candidates/{rule_id}"): AI_ADMIN,
    ("POST", "/admin/writing-memory/candidates/{rule_id}/reject"): AI_ADMIN,
    ("POST", "/admin/writing-memory/run"): AI_ADMIN,
    # AA-918 (WM-15 / WM-16) — R0 replay ($0), the L0 lexicon fast path: activation is the one human click of an L0
    # rule (a prompt change of a live writer: ai.admin, the LLM stage config tier), rollback the same tier.
    ("POST", "/admin/writing-memory/candidates/{rule_id}/r0"): AI_ADMIN,
    ("POST", "/admin/writing-memory/candidates/{rule_id}/activate"): AI_ADMIN,
    ("POST", "/admin/writing-memory/candidates/{rule_id}/rollback"): AI_ADMIN,
    # AA-919 (WM-18) — releases: reads admin only (tenant.read; ids, statuses and numbers, no text); every write (draft,
    # evidence, approve, activate, rollback, reject, the reverify scan, the memory switch and the monthly close)
    # changes what a live writer is told: ai.admin, the tier of L0 activation and the LLM stage config.
    ("GET", "/admin/writing-memory/releases"): _LAB_READ,
    ("GET", "/admin/writing-memory/releases/active"): _LAB_READ,
    ("GET", "/admin/writing-memory/releases/{release_id}"): _LAB_READ,
    ("POST", "/admin/writing-memory/releases"): AI_ADMIN,
    ("POST", "/admin/writing-memory/releases/reverify-scan"): AI_ADMIN,
    ("POST", "/admin/writing-memory/releases/{release_id}/evidence"): AI_ADMIN,
    ("POST", "/admin/writing-memory/releases/{release_id}/approve"): AI_ADMIN,
    ("POST", "/admin/writing-memory/releases/{release_id}/activate"): AI_ADMIN,
    ("POST", "/admin/writing-memory/releases/{release_id}/rollback"): AI_ADMIN,
    ("POST", "/admin/writing-memory/releases/{release_id}/post-activation-check"): AI_ADMIN,   # AA-920
    ("POST", "/admin/writing-memory/releases/{release_id}/reject"): AI_ADMIN,
    ("GET", "/admin/writing-memory/writers"): _LAB_TEXT_READ,
    ("PATCH", "/admin/writing-memory/writers/{writer}"): AI_ADMIN,
    # AA-921 (WM-27 / WM-29): pinned exemplars (tenant text: reads tenant.read, every change ai.admin) and the Tenant
    # 360 voice card
    ("GET", "/admin/writing-memory/exemplars"): _LAB_TEXT_READ,
    ("GET", "/admin/writing-memory/exemplars/selection"): _LAB_TEXT_READ,
    ("POST", "/admin/writing-memory/exemplars/refresh"): AI_ADMIN,
    ("POST", "/admin/writing-memory/exemplars/{exemplar_id}/pin"): AI_ADMIN,
    ("POST", "/admin/writing-memory/exemplars/{exemplar_id}/unpin"): AI_ADMIN,
    ("POST", "/admin/writing-memory/exemplars/{exemplar_id}/never"): AI_ADMIN,
    ("GET", "/admin/writing-memory/tenants/{tenant_id}/voice"): TENANT_CONTENT_READ,
    # AA-922 (WM-30 / WM-31): the weekly KPI reports, keeps and the disagreement queue
    ("GET", "/admin/writing-memory/metrics"): _LAB_TEXT_READ,
    ("POST", "/admin/writing-memory/metrics/run"): AI_ADMIN,
    ("POST", "/admin/writing-memory/releases/{release_id}/keep"): AI_ADMIN,
    ("GET", "/admin/writing-memory/disagreements"): _LAB_TEXT_READ,
    ("POST", "/admin/writing-memory/disagreements/{item_id}/resolve"): AI_ADMIN,
    # AA-1039 R6 slice 3: the Writing Lab stage read (ids, labels, counts, hashes; no text). AA-1035 R2: Writing Lab R
    # without tenant text, like the release reads (content_lead, tenant_qa, ai_ops, admin)
    ("GET", "/admin/writing-memory/lab/{writer}"): _LAB_READ,
    # AA-1048 W2 (S273): the claim-level ground truth (api/routers/admin_claim_labels.py). Writes ai.admin; the counts
    # and the split plan carry no text (Writing Lab R); the finals carry claim text (Lab text read, logged).
    ("POST", "/admin/writing-memory/claim-labels/import"): AI_ADMIN,
    ("GET", "/admin/writing-memory/claim-labels/split-plan"): _LAB_READ,
    ("GET", "/admin/writing-memory/claim-labels/{set_id}/{set_version}/report"): _LAB_READ,
    ("POST", "/admin/writing-memory/claim-labels/{set_id}/{set_version}/enqueue"): AI_ADMIN,
    ("POST", "/admin/writing-memory/claim-labels/{set_id}/{set_version}/pull-resolutions"): AI_ADMIN,
    ("POST", "/admin/writing-memory/claim-labels/{set_id}/{set_version}/prefilter"): AI_ADMIN,   # AA-1063
    ("GET", "/admin/writing-memory/claim-labels/{set_id}/{set_version}/finals"): _LAB_TEXT_READ,
    # AA-1063 (S275): the Writing Lab claim review, agent proposals, the label set browser and corrections. Reads that
    # return claim text are Lab text reads (logged); the set counts carry no text; every write is ai.admin.
    ("GET", "/admin/writing-memory/claim-labels/review"): _LAB_TEXT_READ,
    ("GET", "/admin/writing-memory/claim-labels/review/{item_id}/context"): _LAB_TEXT_READ,
    ("POST", "/admin/writing-memory/claim-labels/review/accept-proposals"): AI_ADMIN,
    ("POST", "/admin/writing-memory/claim-labels/review/{item_id}/answer"): AI_ADMIN,
    ("POST", "/admin/writing-memory/claim-labels/proposals/import"): AI_ADMIN,
    ("GET", "/admin/writing-memory/claim-labels/sets"): _LAB_READ,
    ("GET", "/admin/writing-memory/claim-labels/{set_id}/{set_version}/claims"): _LAB_TEXT_READ,
    ("POST", "/admin/writing-memory/claim-labels/{set_id}/{set_version}/claims/{claim_hash}/relabel"): AI_ADMIN,
    # AA-1050 W4: the claim-instrument shadow summary (api/routers/admin_claim_shadow.py): counts and ids, no text.
    ("GET", "/admin/writing-memory/claim-shadow/summary"): _LAB_READ,

    # ── admin_data_model (AA-894 scope 2) — Developer › Data model: the live ERD from the nightly schema dump
    # (every schema of the shared RDS, PII tags included), admin only.
    ("GET", "/admin/data-model"): OPS_READ,

    # ── admin_docs (AA-898) — Developer › Docs: the docs catalog reader synced from the root repo by CI, admin only;
    # owner-only pages (N-3) are filtered in the route by the verified owner flag.
    ("GET", "/admin/docs/manifest"): OPS_READ,
    ("GET", "/admin/docs/page"): OPS_READ,
    ("GET", "/admin/docs/asset"): OPS_READ,   # AA-697 — SVG diagrams of the docs bundle

    # ── admin_supply_content (AA-835) — the review of the customer content of Booking hotels / services. An edit or
    # a (paid) regenerate is content work; publish, reject and unpublish are review actions. Reads: every staff role.
    ("POST", "/admin/supply-content/{entity_id}/versions"): CONTENT_WRITE,
    ("POST", "/admin/supply-content/{entity_id}/draft"): CONTENT_WRITE,
    ("POST", "/admin/supply-content/{entity_id}/versions/{version_id}/publish"): REVIEW_ACT,
    ("POST", "/admin/supply-content/{entity_id}/versions/{version_id}/reject"): REVIEW_ACT,
    ("POST", "/admin/supply-content/{entity_id}/unpublish"): REVIEW_ACT,
}

# POLICY entries whose route is not on main yet. (method, route) -> the change that adds it. The route-policy test
# allows these to be missing from the app; once the route exists it must match the template exactly (every
# /admin/platform/* route needs a POLICY entry, so a different parameter name fails CI there instead of leaving the
# route open to every staff role). Empty since AA-883's two /admin/platform/staff routes landed.
AWAITING_ROUTE: dict[tuple[str, str], str] = {}

# Path prefixes where the staff secret is accepted and POLICY applies (route-policy test scope). /v1/social has no
# route since AA-850 (its router read acp_silver_s4, dropped by CIS 299) and /v1/rules none since AA-923 (WM-34: the
# N8 output rules are retired); both prefixes stay staff-only (fail closed, and the BFF list in
# frontend/lib/auth-server.ts mirrors this tuple).
STAFF_PATH_PREFIXES: tuple[str, ...] = ("/admin", "/v1/pipeline", "/v1/rules", "/v1/social")

# Machine callers send the secret and no x-admin-role. (method, route) -> who calls it. Of the GETs only
# /admin/overview has a POLICY entry (ops.read); the job-runner ones are open to any secret holder and
# are listed so the allowlist stays complete if they ever get one.
MACHINE_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/admin/job-runner/deploy-gate"): ("CI deploy-dev.yml: pre-deploy gate (open wave / running "
                                               "deploy-blocking kind, AA-993 G5) + post-deploy smoke"),
    ("GET", "/admin/job-runner/summary"): "CI deploy-dev.yml: post-deploy smoke",
    ("GET", "/admin/job-runner/workers"): "CI deploy-dev.yml: post-deploy smoke (worker parity)",
    ("GET", "/admin/job-runner/released"): "CI post-deploy-verify.yml: jobs released on shutdown (AA-993 G1)",
    ("GET", "/admin/overview"): "CI deploy-dev.yml: post-deploy smoke",
    ("POST", "/admin/dfs-balance/check"): "Infra Lambda dfs_balance_check (daily, AA-627)",
    ("POST", "/admin/jev-canary/check"): "Infra Lambda dfs_balance_check (daily, AA-720)",
    ("POST", "/admin/jev/402-sweep"): "Infra Lambda dfs_balance_check (daily, AA-720)",
    ("POST", "/admin/waves/check"): "Infra Lambda dfs_balance_check (AA-727)",
    ("POST", "/admin/job-runner/retention/schedule"): "Infra Lambda dfs_balance_check (daily, AA-825)",
    ("POST", "/admin/job-runner/staff-reconcile/schedule"): "Infra Lambda dfs_balance_check (daily, AA-816 C7)",
    ("POST", "/admin/photos/sync/schedule"): "Infra Lambda dfs_balance_check (daily, AA-844)",
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
    # AA-816 A10b / AA-1035 A10d: the verified identity is a Dev-only E2E CIS identity (reads only).
    e2e_read_only: bool = False
    # AA-1035 A10d: the A10b identity only (api/core/e2e_staff.E2E_ALLOWED_WRITES, docs.owner_read "e2e_dev").
    e2e_allowlisted_writes: bool = False

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
        e2e_read_only=bool(identity is not None and getattr(identity, "e2e_read_only", False)),
        e2e_allowlisted_writes=bool(identity is not None and getattr(identity, "e2e_read_only", False)
                                    and getattr(identity, "e2e_allowlisted_writes", False)),
    )


def has_perm(role: Optional[str], capability: str) -> bool:
    return capability in ROLE_PERMS.get(role or "", frozenset())


def has_any_perm(role: Optional[str], *capabilities: str) -> bool:
    """AA-1035: the role holds at least one of ``capabilities``."""
    perms = ROLE_PERMS.get(role or "", frozenset())
    return any(c in perms for c in capabilities)


def sees_tenant_money(role: Optional[str]) -> bool:
    """AA-1035 (S267 review): may a caller who reaches a tenant read see its money (plan price, overage, LLM cost)?
    The machine (no role) and every role with a money or account read do; a role that reaches the tenant only through
    tenant.book.read (support) gets those fields as null. content.read keeps the Master Content cost of the aa_internal
    tenant it read before R2."""
    if role is None:
        return True
    return has_any_perm(role, TENANT_READ, TENANT_BOOK_ACCOUNT_READ, BILLING_READ, SPEND_READ, CONTENT_READ)


def docs_owner_read_via(principal: StaffPrincipal) -> Optional[str]:
    """Why this principal holds DOCS_OWNER_READ: "owner" (a verified Cognito session whose staff row is an owner),
    "e2e_dev" (see below) or None (it does not).

    "e2e_dev" — TEMPORARY, Dev only, removed with A10 / A10b (root ADR 0006a A10b removal list, AA-816): the Dev-only
    E2E CIS admin (e2e-cis-admin@, ``e2e_read_only`` on its verified identity) may READ owner-only docs pages so the
    agent's admin-evidence run can screenshot one (AA-898 Done-when). All of these must hold: a verified Cognito
    session (``auth == "cognito"``; the identity resolver flags it only on Dev, and the WORKFORCE pre token trigger
    issues its tokens only while E2E_DEV_ONLY_STAFF_ENABLED is exactly "true"), the admin role (the only scope the
    trigger grants it, and the one that holds ops.read), and ``ENVIRONMENT`` exactly "dev", re-read on every call.
    It grants nothing else: ``is_owner`` stays False (so staff.write and every owner-only write stay refused), the
    session stays read-only (bearer middleware + denial_reason), and no other route reads this capability.
    AA-1035 A10d: keyed on ``e2e_allowlisted_writes`` (the A10b identity only), so the Dev-only customer_success E2E
    identity never gets it.
    """
    if principal.is_machine or principal.auth != "cognito":
        return None
    if principal.is_owner:
        return "owner"
    if (principal.e2e_read_only and principal.e2e_allowlisted_writes
            and principal.role == e2e_staff.E2E_CIS_ADMIN_TEST_STAFF_ROLE
            and OPS_READ in principal.perms and e2e_staff.dev_only_e2e_environment()):
        return "e2e_dev"
    return None


def can_read_owner_docs(principal: StaffPrincipal) -> bool:
    """DOCS_OWNER_READ: see docs_owner_read_via()."""
    return docs_owner_read_via(principal) is not None


def denial_reason(principal: StaffPrincipal, method: str, route: str) -> Optional[str]:
    """None when the principal may call (method, route); otherwise the 403 detail."""
    if principal.role in READ_ONLY_ROLES and method.upper() not in READ_METHODS:
        return READ_ONLY_DENIED.format(role=principal.role)
    # AA-816 A10b, defence in depth behind the bearer middleware (covers a route reached by any other path). AA-1035
    # A10d: the allow-list is the A10b identity's alone (e2e_allowlisted_writes); any other e2e_read_only gets none.
    if (principal.e2e_read_only and method.upper() not in READ_METHODS
            and not (principal.e2e_allowlisted_writes and e2e_staff.is_allowed_e2e_write_route(method, route))):
        return e2e_staff.E2E_READ_ONLY
    entry = POLICY.get((method, route))
    if entry is None:
        # A GET with no entry (writes all have one): open to the machine and, since AA-1035, to every role that reads
        # the content pages (content.read); every other role is refused (deny by default).
        if principal.is_machine:
            return None
        if principal.role not in ROLE_PERMS:
            return "Unknown staff role"
        if OPEN_READ not in principal.perms:
            return f"Role '{principal.role}' lacks the '{OPEN_READ}' permission"
        return None
    if principal.is_machine:
        if (method, route) in MACHINE_ROUTES:
            return None
        return "This route needs a staff role (x-admin-role); machine callers are not allowed"
    if principal.role not in ROLE_PERMS:
        return "Unknown staff role"
    capabilities = policy_capabilities(entry)
    if len(capabilities) > 1:
        if not any(c in principal.perms for c in capabilities):
            return f"Role '{principal.role}' lacks the '{policy_label(entry)}' permission"
        return None
    capability = capabilities[0]
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
    # A direct call (tests, jobs) may pass a request stand-in without an ASGI scope: fall back to the contextvar.
    state = (getattr(request, "scope", None) or {}).get("state")
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
                       capability=policy_label(POLICY.get((request.method, template))),
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
    AA-859), /v1/pipeline (401 without credentials, 403 for any JWT, secret read
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
