// frontend/lib/rbac.ts
// AA-656 — the single staff RBAC matrix. proxy.ts (page gating), lib/auth-server.ts (BFF proxy
// gating) and the admin nav (app/_kit/adminNav.tsx) all read these tables; nothing else decides who
// sees what. The role always comes from a VERIFIED JWT (/auth/verify-admin), never from the
// client-writable `cis_role` cookie — that cookie is only a display hint for the UI.
//
// Staff roles live in shared.admin_users.role (enum shared.admin_role: admin, content — migration 218 — the read-only
// viewer, migration 249, AA-878, and the R2 roles below). The matrix is documented in CONTEXT.md ("Staff roles and
// permissions"); AA-Booking reads its own roles from the booking-* groups.
//
// Dependency-free: no React, no Node APIs (proxy.ts imports it).

// AA-1035 R2 (CIS 362, root ADR 0019 d3): six more roles. reviewer merged into content (CIS 363) and the alias is gone
// (CIS 375; the group cis-reviewer holds no role). The matrix is root
// docs/audits/2026-11-03-S265-ecosystem-restructure/clarifications.md section 3; the backend is the authority.
export type StaffRole =
  | "admin" | "content" | "viewer"
  | "content_lead" | "tenant_qa" | "customer_success" | "support" | "finance" | "ai_ops";

export const STAFF_ROLES: readonly StaffRole[] = [
  "admin", "content", "viewer", "content_lead", "tenant_qa", "customer_success", "support", "finance", "ai_ops",
];
// Every staff role: the BFF proxies let each through and the backend decides per route (api/core/staff_authz.py).
const EVERY_STAFF: StaffRole[] = [...STAFF_ROLES];
// AA-1035: the roles with content.read, which read the content pages (before R2 that was every staff role).
const ALL_STAFF: StaffRole[] = ["admin", "content", "viewer", "content_lead", "tenant_qa", "ai_ops"];
// Roles that do content work (upload, S1 rewrite, review actions): content.write.
const CONTENT_WORKERS: StaffRole[] = ["admin", "content", "content_lead"];
// AA-1035: tenant content QA (quality.write) and the Writing Lab readers.
const TENANT_QA: StaffRole[] = ["admin", "content_lead", "tenant_qa"];
// AA-1035 (S267 fix pass): the Writing Lab pages — the readers above plus ai_ops (Writing Lab W: lab.read and the
// ai.admin writes). Slice 2: ai_ops reads the tenant-text panels too, through lab.tenant_text.read, which the backend
// logs per read (api/core/lab_read_log.py); the Writing memory page says so to that role.
const WRITING_LAB: StaffRole[] = [...TENANT_QA, "ai_ops"];
// AA-1035: the Tenants directory (backend filters it to the caller's scope: api/core/tenant_scope.py).
const TENANT_DESK: StaffRole[] = ["admin", "customer_success", "support", "finance"];
// AA-1035 R2 slice 2: the roles that work a tenant book (Assignments, view as tenant: tenant.view_as).
const TENANT_BOOK: StaffRole[] = ["admin", "customer_success", "support"];
// AA-1035: AI & spend.
const AI_SPEND: StaffRole[] = ["admin", "ai_ops", "finance"];
// AA-1091: AA Catalogue › Reference prices — readers (reference_price.read) and writers (reference_price.write: the
// customer-service team's customer_success role, admin, content_lead).
const REFERENCE_PRICE_READERS: StaffRole[] = [
  "admin", "content_lead", "content", "customer_success", "finance",
];
const REFERENCE_PRICE_WRITERS: StaffRole[] = ["admin", "content_lead", "customer_success"];

// AA-878 — the read-only role (the UI-smoke E2E account). It reads the content pages and is refused on every
// write: requireAdmin() (lib/auth-server.ts) answers 403 to any non-GET from it before the admin secret is
// attached, and the backend refuses it too (api/core/staff_authz.py READ_ONLY_ROLES).
export const READ_ONLY_ROLES: readonly StaffRole[] = ["viewer"];

export function isReadOnlyRole(role: string | null | undefined): boolean {
  return !!role && (READ_ONLY_ROLES as readonly string[]).includes(role);
}

/** Is `method` a read (never refused for a read-only role)? */
export function isReadMethod(method: string): boolean {
  const m = method.toUpperCase();
  return m === "GET" || m === "HEAD" || m === "OPTIONS";
}

// The CIS roles the Staff page may assign (api/services/staff_directory.py CIS_ROLES): never the viewer.
export const ASSIGNABLE_STAFF_ROLES: readonly StaffRole[] = [
  "admin", "content_lead", "content", "tenant_qa", "customer_success", "support", "finance", "ai_ops",
];

export function isStaffRole(role: string | null | undefined): role is StaffRole {
  return !!role && (STAFF_ROLES as readonly string[]).includes(role);
}

// AA-885: every page rule is staff-only. The tenant portal (and its `tenant` role) lives in its own app
// (portal/, portal.lumiguides.it.com, gate portal/proxy.ts); /portal and /tenant-login here are 308s.
export interface PageRule {
  prefix: string;
  roles: StaffRole[];
}

// Allow-list (AA-252): a protected path with no entry here is denied. Prefixes are disjoint, so the
// first match is the only match.
export const PAGE_RULES: PageRule[] = [
  // Admin only — whole-system views, money, tenants, jobs, decisions, photos.
  { prefix: "/admin/overview", roles: ["admin"] },          // AA-664
  { prefix: "/admin/data-coverage", roles: ["admin"] },     // AA-992 — counts from raw upload to CRM (incl. CRM)
  { prefix: "/admin/reports", roles: ["admin"] },          // AA-1003 — report register, KPI scorecard runs
  // AA-1039 (S266): also the Tenants group pages /admin/tenants/agencies (was /admin/platform/agencies) and
  // /admin/tenants/data-erasure (was /admin/data-erasure).
  // AA-1035 R2: the more specific Tenants pages first (pageRule takes the first match); the directory and Tenant 360
  // open for the tenant desk roles, whose rows the backend filters to their scope.
  // AA-1035 R2 close-out (root ADR 0019 d4): Agencies (CRM) for the book roles too, read-only (the CRM says can_write
  // false to them; the backend scopes every read and the CRM serves a desk caller only the tenants CIS reports for it).
  { prefix: "/admin/tenants/agencies", roles: TENANT_BOOK },
  { prefix: "/admin/tenants/data-erasure", roles: ["admin"] },
  { prefix: "/admin/tenants/plans", roles: ["admin", "finance"] },
  // AA-1035 R2 slice 2: who looks after which tenant (the backend filters to the caller's scope; only admin assigns)
  // and the read-only view as tenant (backend capability tenant.view_as, scoped).
  { prefix: "/admin/tenants/assignments", roles: TENANT_BOOK },
  { prefix: "/admin/tenants/view-as", roles: TENANT_BOOK },
  { prefix: "/admin/tenants", roles: TENANT_DESK },
  { prefix: "/admin/ai-gateway", roles: AI_SPEND },        // AA-804 — AI gateway (health, spend, job trace)
  // AA-1039 R6 slice 3 — Spend (was Cost Operations at /admin/cost-ops, AA-505 / AA-665, and Cost per app at
  // /admin/platform/cost, AA-884; both 307 here from next.config.ts, which runs before proxy.ts).
  { prefix: "/admin/spend", roles: AI_SPEND },
  { prefix: "/admin/usage", roles: AI_SPEND },             // AA-1045 — usage explorer (AI & spend)
  { prefix: "/admin/jobs", roles: ["admin"] },              // AA-650
  { prefix: "/admin/decisions", roles: ["admin"] },         // AA-660
  { prefix: "/admin/photos", roles: ["admin", "content_lead"] },            // AA-708
  // AA-1039 R6 rest (tech lead, option A, 05/11/2026): Place cards moved from Content quality's Cards tab to AA
  // Catalogue; it keeps the Cards tab's roles (TENANT_QA) on its own route, so it must come before the admin-only
  // /admin/places rule (pageRule takes the first match). Nobody gains or loses access (lib/adminIa.test.mjs 12).
  { prefix: "/admin/places/cards", roles: TENANT_QA },
  { prefix: "/admin/places", roles: ["admin"] },            // AA-847 — coverage + content validity, fixes
  { prefix: "/admin/seo-intelligence", roles: ["admin", "content_lead"] },  // AA-705
  { prefix: "/admin/atom-curation", roles: ["admin", "content_lead"] },     // AA-527
  // AA-1039 (S266) — Tenant content QA: quality (was /admin/content-quality, AA-769), trace (was
  // /admin/tenant-activity, AA-551), review-log (was /admin/platform-stats, AA-560) and slate (was atom-curation
  // section 05). The old URLs are 307s in next.config.ts (lib/adminIa.ts), which run before proxy.ts.
  // AA-1035 R2 close-out (matrix "Tenant QA: customer_success R (own)"): the Content quality page for the own book
  // (backend tenant_content.book.read, filtered to the caller's scope; no labels, no writes). Before the group rule.
  { prefix: "/admin/tenant-qa/quality", roles: [...TENANT_QA, "customer_success"] },
  { prefix: "/admin/tenant-qa", roles: TENANT_QA },
  // AA-1039 R6 slice 3 — Writing Lab: the stage pages (/admin/lab/<stage>) and the Writing memory tools (AA-917,
  // signals and candidate rules hold tenant text; was /admin/writing-memory, a 307 now).
  { prefix: "/admin/lab", roles: WRITING_LAB },
  { prefix: "/admin/api-docs", roles: ["admin"] },          // AA-577 follow-up — OpenAPI reader
  { prefix: "/admin/data-model", roles: ["admin"] },        // AA-894 — live ERD from the nightly schema dump
  { prefix: "/admin/docs", roles: ["admin"] },              // AA-898 — docs catalog reader (synced by root CI)
  // AA-883 — Platform pages (staff directory first). Matches /admin/platform and /admin/platform/…
  // only, never a sibling such as /admin/platform-x (pageRule compares prefix or prefix + "/").
  { prefix: "/admin/platform", roles: ["admin"] },
  // Content work — every staff role but the read-only viewer (these pages only write).
  { prefix: "/admin/upload", roles: CONTENT_WORKERS },
  { prefix: "/admin/s1-rewrite", roles: CONTENT_WORKERS },
  // Content reads — every staff role, the viewer included (write controls hidden by can()).
  { prefix: "/admin/master-content", roles: ALL_STAFF },
  { prefix: "/admin/review", roles: ALL_STAFF },
  { prefix: "/admin/settings", roles: ALL_STAFF },
  // AA-977 — AA content for the partner CRM: every staff role reads; drafts need content.write, publish / retire /
  // restore need master.admin (backend staff_authz POLICY; the page hides the controls with can()).
  { prefix: "/admin/aa-content", roles: ALL_STAFF },
  // AA-835 — supply content of AA-Booking hotels / services (Booking's "Edit in CIS" link opens /admin/supply-content/<id>):
  // every staff role reads; edits / regenerate need content.write, publish / reject / unpublish review.act (backend
  // staff_authz POLICY; the page hides the controls with can(), the BFF refuses every write of the viewer).
  { prefix: "/admin/supply-content", roles: ALL_STAFF },
  // AA-814 — platform Facts Entries (sourced claims every agency's writer may cite): every staff role reads; add / edit /
  // delete need quality.write (admin; API_WRITE_RULES "facts" below, backend staff_authz POLICY).
  { prefix: "/admin/facts", roles: ALL_STAFF },
  // AA-1091 — the tours' structured reference prices (inline edit, accept, Excel export / import with DRY RUN); the
  // backend re-checks (reference_price.read / .write).
  { prefix: "/admin/reference-prices", roles: REFERENCE_PRICE_READERS },
];

export function pageRule(pathname: string): PageRule | undefined {
  return PAGE_RULES.find((r) => pathname === r.prefix || pathname.startsWith(r.prefix + "/"));
}

export function canSeePage(role: string | null | undefined, pathname: string): boolean {
  const rule = pageRule(pathname);
  return !!rule && !!role && (rule.roles as string[]).includes(role);
}

/** AA-1037 — may `role` see this admin nav item (lib/adminIa.ts IaItem)? A CIS page: its PAGE_RULE. A page of another
 *  app (`external`, root ADR 0019 d9 Trade group): a role holding one of the item's `capabilities`; none listed: no
 *  role. The other app still checks its own grant. */
export function canSeeNavItem(
  role: string | null | undefined,
  item: { href: string; external?: string | boolean; capabilities?: readonly Capability[] },
): boolean {
  if (item.external) return (item.capabilities ?? []).some((c) => can(role, c));
  return canSeePage(role, item.href);
}

// BFF proxy routes that attach the backend admin secret. Each handler calls requireStaff() with its
// own entry, so a valid staff JWT with the wrong role still gets 403.
export const API_RULES = {
  admin: EVERY_STAFF,        // /api/admin/[...path]  → backend /admin/* (AA-1035: the backend decides per route)
  pipeline: ALL_STAFF,       // /api/pipeline/[...path] → backend /v1/pipeline/*
  tourFull: ALL_STAFF,       // /api/tour-full/[id]
  brandIdentity: ALL_STAFF,  // /api/tenant/pipeline/brand-identity
  tenantProxy: ALL_STAFF,    // /api/tenant/[...path] — staff-only since AA-885 (e.g. Master Content)
} satisfies Record<string, StaffRole[]>;

// AA-769 (S234) — writes that change tenant content are narrower than the route's read rule: a
// non-GET request under one of these /api/admin/ path prefixes needs one of `roles` (the backend
// re-checks the forwarded role). Today the Content Quality sentence actions (cut / rewrite) and the
// per-request "regenerate angles".
export const API_WRITE_RULES: { prefix: string; roles: StaffRole[] }[] = [
  // AA-1035: quality.write = admin, content_lead, tenant_qa (TENANT_QA).
  { prefix: "content-quality/pieces/", roles: TENANT_QA },
  { prefix: "content-quality/requests/", roles: TENANT_QA },
  // AA-802 — the T2 silent correction (sentence suggest / apply on a tenant's tour version)
  { prefix: "content-quality/tour-versions/", roles: TENANT_QA },
  // AA-923 WM-32 — the human label of an A1 (S1) rewrite (T2 version labels sit under tour-versions/ above)
  { prefix: "content-quality/a1-content/", roles: TENANT_QA },
  // AA-774 (S235) — place-card writes (verdict, merge, build job, fact edits) change what every
  // tenant's T9 seed reads; the backend re-checks the role on the fact edit.
  { prefix: "content-quality/place-cards", roles: TENANT_QA },
  // AA-1039 R6 — releasing a held piece (AA-subject gate) lets that text publish (backend: QUALITY_WRITE)
  { prefix: "tenant-qa/compliance/", roles: TENANT_QA },
  // AA-797 — every write to a tenant account: POST tenants (create), PATCH tenants/{id}
  // (plan/status), brand-identity, config, offboard, restore. Prefix "tenants" (no slash)
  // so the bare create path is covered; reads (GET) stay open to every staff role.
  // AA-1035: the tenant desk roles reach the writes the backend allows them (members, invitations; finance the plan
  // and suspend / resume of PATCH tenants/{id}); every other tenant write stays admin in the backend.
  { prefix: "tenants", roles: TENANT_DESK },
  // AA-816 C6 — staff directory writes (create, roles, disable/enable, sign-out, sync). The backend narrows
  // further: staff.write needs an owner on a Cognito session (api/core/staff_authz.py), see canWriteStaff().
  // "platform/" (with the slash) so the write rule never matches "platform-stats".
  { prefix: "platform/", roles: ["admin"] },
  // AA-847 — place fixes (merge duplicates, hide trails, area precision) change the map every app reads;
  // the backend re-checks (ops.write). "places/" covers backfill and validity/fix.
  { prefix: "places/", roles: ["admin"] },
  // AA-839 — data erasure runs (DRY RUN and the apply); the backend re-checks (tenant.write, and for an apply the
  // env flag and a Cognito session).
  { prefix: "job-runner/erasure", roles: ["admin"] },
  // AA-917 — writing memory writes (fact verdict, candidate edit / reject, job runs incl. the paid mining run); the
  // backend re-checks (ai.admin).
  { prefix: "writing-memory", roles: ["admin", "ai_ops"] },
  // AA-814 — platform fact writes (add / edit / delete) change what every tenant's T9 seed reads; the backend re-checks
  // (quality.write). "facts" also covers the bare create path.
  { prefix: "facts", roles: TENANT_QA },
  // AA-1003 — "generate now" on /admin/reports; the backend re-checks (ops.write).
  { prefix: "reports/", roles: ["admin"] },
  // AA-1028 R1 slice 3 — the plan catalogue (create, edit, retire); the backend re-checks (tenant.write). Tenant 360
  // overrides and the add-on saga are under "tenants" above.
  { prefix: "plans", roles: ["admin"] },
  // AA-1091 — reference price writes (save, clear, accept, parse, import); the backend re-checks (reference_price.write).
  { prefix: "reference-prices", roles: REFERENCE_PRICE_WRITERS },
];

// AA-577 follow-up — /api/admin/ paths that NO method may reach unless the role is listed (reads
// included), narrower than API_RULES.admin. The OpenAPI schema is admin-only: the backend
// /admin/openapi.json re-checks the forwarded x-admin-role.
export const API_READ_RULES: { prefix: string; roles: StaffRole[] }[] = [
  { prefix: "openapi.json", roles: ["admin"] },
  // AA-1035 R2 close-out — backend /admin/platform/crm/* (the CRM L1 console relay): the book roles read their book
  // (re-checked by the backend: tenant.book.read and require_tenant_scope); writes stay admin (API_WRITE_RULES
  // "platform/"). Before the "platform" rule (first match wins).
  { prefix: "platform/crm", roles: TENANT_BOOK },
  // AA-883 — backend /admin/platform/* (staff directory): admin only, re-checked by the backend
  // (staff_policy, capability staff.read). Matches "platform" and "platform/…", not "platform-stats".
  { prefix: "platform", roles: ["admin"] },
  // AA-894 — backend /admin/data-model (the live ERD of every schema, PII tags included): admin only, re-checked by
  // the backend (staff_policy, capability ops.read).
  { prefix: "data-model", roles: ["admin"] },
  // AA-898 — backend /admin/docs/* (the docs catalog reader; owner-only pages filtered by the backend on the verified
  // owner flag): admin only, re-checked by the backend (staff_policy, capability ops.read).
  { prefix: "docs", roles: ["admin"] },
  // AA-992 — backend /admin/data-coverage (counts of every stage, CRM included): admin only, re-checked by the backend
  // (staff_policy, capability ops.read).
  { prefix: "data-coverage", roles: ["admin"] },
  // AA-1003 — backend /admin/reports/* (report register, runs and their output, generate now): admin only, re-checked
  // by the backend (staff_policy, ops.read / ops.write).
  { prefix: "reports", roles: ["admin"] },
  // AA-839 — backend /admin/job-runner/erasure (inventory + apply settings) and /erasure/run: admin only, re-checked
  // by the backend (staff_policy, tenant.read / tenant.write).
  { prefix: "job-runner/erasure", roles: ["admin"] },
  // AA-917 — backend /admin/writing-memory/* (signals and rules hold tenant text): admin only, re-checked by the
  // backend (staff_policy, tenant.read / ai.admin).
  { prefix: "writing-memory", roles: [...TENANT_QA, "ai_ops"] },
  // AA-804 — backend /admin/ai-gateway/* (every paid provider's spend, balance and the job trace): admin only,
  // re-checked by the backend (staff_policy, spend.read).
  // AA-1035: customer_success reads the Tenant 360 budget tile of its book (tenant.book.account.read); support never.
  { prefix: "ai-gateway", roles: [...AI_SPEND, "customer_success"] },
  // AA-1045 — backend /admin/usage-explorer/* (usage, cost and the caller's saved views): the AI & spend roles, re-checked
  // by the backend (staff_policy, spend.read; a re-roll ops.write).
  { prefix: "usage-explorer", roles: AI_SPEND },
];

/** May `role` call backend /admin/{path} at all (any method)? (path without the leading slash) */
export function canReadAdminPath(role: string | null | undefined, path: string): boolean {
  const rule = API_READ_RULES.find((r) => path === r.prefix || path.startsWith(r.prefix + "/"));
  return !rule || (!!role && (rule.roles as string[]).includes(role));
}

// AA-577 follow-up — the backend's own docs routes (/openapi.json, /docs, /redoc, gated by the admin
// secret on ECS). The staff branch of /api/tenant/[...path] forwards the secret to any backend path,
// so it refuses these for every role but admin (same rule as /admin/api-docs and API_READ_RULES).
export const API_DOCS_PATHS = ["openapi.json", "docs", "redoc"] as const;

/** Is `path` (backend path without the leading slash) one of the AA-577 docs routes? */
export function isApiDocsPath(path: string): boolean {
  return API_DOCS_PATHS.some((p) => path === p || path.startsWith(p + "/"));
}

/** May `role` send a non-GET request to backend /admin/{path}? (path without the leading slash) */
export function canWriteAdminPath(role: string | null | undefined, method: string, path: string): boolean {
  if (method === "GET" || method === "HEAD") return true;
  if (isReadOnlyRole(role)) return false;   // AA-878: no default allow for the viewer
  const rule = API_WRITE_RULES.find((r) => path.startsWith(r.prefix));
  return !rule || (!!role && (rule.roles as string[]).includes(role));
}

/** Landing page after login / for the retired /admin/dashboard. */
export function landingPage(role: string | null | undefined): string {
  if (role === "admin") return "/admin/overview";
  if (role === "customer_success" || role === "support" || role === "finance") return "/admin/tenants";   // AA-1035
  if (role === "ai_ops") return "/admin/ai-gateway";
  return "/admin/review";
}

// AA-882 (GĐ3 review) — the backend capability map (api/core/staff_authz.py ROLE_PERMS), mirrored so
// a page can hide or disable a control the backend would refuse with 403. The backend stays the
// authority; this is UI only. tests/unit/test_aa882_staff_authz.py keeps the two maps identical. One
// explicit list per role (no spreads) so that test can read it.
export type Capability =
  | "content.read" | "content.write" | "review.act" | "master.admin" | "atoms.write" | "photos.write"
  | "a4.unpublish" | "quality.write" | "content.settings.write" | "tenant.read" | "tenant.write"
  | "ops.read" | "ops.write" | "ai.admin" | "spend.read" | "spend.write" | "staff.read" | "audit.read"
  | "alerts.write"    // AA-913 — Platform › Alert recipients (changes also need a Cognito session)
  // AA-1035 R2 (root ADR 0019 d3 / d4)
  | "tenant_content.read" | "catalog.read" | "tenant.book.read" | "tenant.member.write" | "tenant.assign"
  | "billing.read" | "billing.write" | "wave.run" | "lab.read"
  | "tenant.book.account.read"    // AA-1035: plans, usage, cost, Tenant QA, AI budget of the own book (not support)
  | "tenant.member.resend"        // AA-1035: send a non-owner invitation again (support, customer_success)
  | "tenant.book.write"           // AA-1035: non-billing Tenant 360 writes of the own book (customer_success)
  // AA-1035 R2 slice 2
  | "tenant.view_as"              // read-only "view as tenant" (admin, customer_success, support), scoped
  | "lab.tenant_text.read"        // ai_ops: tenant text in the Writing Lab only, every read logged by the backend
  | "notifications.read"          // the staff bell (every role)
  // AA-1035 R2 close-out
  | "tenant_content.book.read"    // Tenant QA lists / details of the own book (customer_success), scoped
  // AA-1091 — AA Catalogue › Reference prices (the customer-service team enters the tours' reference prices)
  | "reference_price.read" | "reference_price.write";

export const ROLE_PERMS: Record<StaffRole, readonly Capability[]> = {
  admin: [
    "content.read", "content.write", "review.act", "master.admin", "atoms.write", "photos.write",
    "a4.unpublish", "quality.write", "content.settings.write", "tenant.read", "tenant.write",
    "ops.read", "ops.write", "ai.admin", "spend.read", "spend.write", "staff.read", "audit.read",
    "alerts.write", "tenant_content.read", "catalog.read", "tenant.book.read", "tenant.book.account.read",
    "tenant.member.write", "tenant.assign", "billing.read", "billing.write", "wave.run", "lab.read",
    "tenant.member.resend", "tenant.book.write", "tenant.view_as", "notifications.read",
    "reference_price.read", "reference_price.write",
  ],
  content_lead: [
    "content.read", "content.write", "review.act", "master.admin", "atoms.write", "photos.write",
    "a4.unpublish", "quality.write", "content.settings.write", "catalog.read", "tenant_content.read",
    "lab.read", "wave.run", "notifications.read", "reference_price.read", "reference_price.write",
  ],
  content: [
    "content.read", "content.write", "review.act", "catalog.read", "notifications.read", "reference_price.read",
  ],
  tenant_qa: [
    "content.read", "catalog.read", "tenant_content.read", "quality.write", "a4.unpublish", "lab.read",
    "notifications.read",
  ],
  customer_success: [
    "tenant.book.read", "tenant.book.account.read", "tenant.book.write", "tenant.member.write", "tenant.member.resend",
    "tenant.view_as", "notifications.read", "tenant_content.book.read", "reference_price.read",
    "reference_price.write",
  ],
  support: ["tenant.book.read", "tenant.member.resend", "tenant.view_as", "notifications.read"],
  finance: ["billing.read", "billing.write", "spend.read", "notifications.read", "reference_price.read"],
  ai_ops: [
    "content.read", "catalog.read", "ai.admin", "spend.read", "spend.write", "lab.read", "lab.tenant_text.read",
    "notifications.read",
  ],
  viewer: ["content.read", "notifications.read"],
};

/** May `role` use `capability`? (UI hint from the display role; the backend re-checks every call.) */
export function can(role: string | null | undefined, capability: Capability): boolean {
  return isStaffRole(role) && ROLE_PERMS[role].includes(capability);
}

// AA-816 C6 — staff.write is not a role capability (so it is not in Capability / ROLE_PERMS): it needs the
// owner flag (shared.admin_users.is_owner) AND a verified Cognito session. Neither is in a cookie, so the staff
// directory list returns who is looking (`viewer`) and the page shows the write controls from that. UI only:
// the backend refuses every write from a legacy password session or a non-owner.
export interface StaffViewer {
  auth: "cognito" | "legacy";
  is_owner: boolean;
  can_write: boolean;
  admin_id: string | null;
}

/** May this session change staff accounts? (owner on a Cognito session; the backend re-checks every call) */
export function canWriteStaff(viewer: StaffViewer | null | undefined): boolean {
  return !!viewer && viewer.auth === "cognito" && viewer.is_owner && viewer.can_write;
}

/** A readable message for a failed staff call: the backend's policy 403 reads "Role 'x' lacks …". `adminOnly` is
 *  the refusal text in the UI language (AA-994: lib/rbac.messages.ts RBAC_MESSAGES "adminOnly"). */
export function staffErrorMessage(
  status: number, detail: unknown, fallback: string, adminOnly = "Admin only: your role cannot make this change.",
): string {
  // The backend's capability and read-only refusals both start with "Role '" (api/core/staff_authz.py).
  if (status === 403 && typeof detail === "string" && detail.startsWith("Role '")) {
    return adminOnly;
  }
  return typeof detail === "string" && detail ? detail : fallback;
}

/** English role names; the UI language's names are RBAC_MESSAGES in lib/rbac.messages.ts (same keys). */
export const ROLE_LABEL: Record<StaffRole, string> = {
  admin: "Administrator",
  content: "Content Team",
  viewer: "Viewer (read-only)",
  content_lead: "Content Lead",
  tenant_qa: "Tenant Content QA",
  customer_success: "Customer Success",
  support: "Support",
  finance: "Finance",
  ai_ops: "AI Operations",
};
