// frontend/lib/rbac.ts
// AA-656 — the single staff RBAC matrix. proxy.ts (page gating), lib/auth-server.ts (BFF proxy
// gating) and the admin nav (app/_kit/adminNav.tsx) all read these tables; nothing else decides who
// sees what. The role always comes from a VERIFIED JWT (/auth/verify-admin), never from the
// client-writable `cis_role` cookie — that cookie is only a display hint for the UI.
//
// Staff roles live in shared.admin_users.role (enum shared.admin_role: admin, reviewer, content —
// migration 218). The matrix is documented in CONTEXT.md ("Staff roles and permissions"); AA-Booking
// staff roles map onto the same three (admin → admin, sales → reviewer tier, content_creator →
// content) so the ecosystem keeps one staff identity model.
//
// Dependency-free: no React, no Node APIs (proxy.ts imports it).

export type StaffRole = "admin" | "reviewer" | "content";

export const STAFF_ROLES: readonly StaffRole[] = ["admin", "reviewer", "content"];
const ALL_STAFF: StaffRole[] = ["admin", "reviewer", "content"];

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
  { prefix: "/admin/tenants", roles: ["admin"] },
  { prefix: "/admin/llm-usage", roles: ["admin"] },         // AA-505
  { prefix: "/admin/jobs", roles: ["admin"] },              // AA-650
  { prefix: "/admin/decisions", roles: ["admin"] },         // AA-660
  { prefix: "/admin/photos", roles: ["admin"] },            // AA-708
  { prefix: "/admin/seo-intelligence", roles: ["admin"] },  // AA-705
  { prefix: "/admin/atom-curation", roles: ["admin"] },     // AA-527
  { prefix: "/admin/tenant-activity", roles: ["admin"] },   // AA-551
  { prefix: "/admin/platform-stats", roles: ["admin"] },    // AA-560
  { prefix: "/admin/content-quality", roles: ["admin"] },   // AA-769
  { prefix: "/admin/api-docs", roles: ["admin"] },          // AA-577 follow-up — OpenAPI reader
  // AA-883 — Platform pages (staff directory first). Matches /admin/platform and /admin/platform/…
  // only, never /admin/platform-stats (pageRule compares prefix or prefix + "/").
  { prefix: "/admin/platform", roles: ["admin"] },
  // Content work — every staff role.
  { prefix: "/admin/upload", roles: ALL_STAFF },
  { prefix: "/admin/master-content", roles: ALL_STAFF },
  { prefix: "/admin/review", roles: ALL_STAFF },
  { prefix: "/admin/s1-rewrite", roles: ALL_STAFF },
  { prefix: "/admin/settings", roles: ALL_STAFF },
];

export function pageRule(pathname: string): PageRule | undefined {
  return PAGE_RULES.find((r) => pathname === r.prefix || pathname.startsWith(r.prefix + "/"));
}

export function canSeePage(role: string | null | undefined, pathname: string): boolean {
  const rule = pageRule(pathname);
  return !!rule && !!role && (rule.roles as string[]).includes(role);
}

// BFF proxy routes that attach the backend admin secret. Each handler calls requireStaff() with its
// own entry, so a valid staff JWT with the wrong role still gets 403.
export const API_RULES = {
  admin: ALL_STAFF,          // /api/admin/[...path]  → backend /admin/*
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
  { prefix: "content-quality/pieces/", roles: ["admin"] },
  { prefix: "content-quality/requests/", roles: ["admin"] },
  // AA-802 — the T2 silent correction (sentence suggest / apply on a tenant's tour version)
  { prefix: "content-quality/tour-versions/", roles: ["admin"] },
  // AA-774 (S235) — place-card writes (verdict, merge, build job, fact edits) change what every
  // tenant's T9 seed reads; the backend re-checks the role on the fact edit.
  { prefix: "content-quality/place-cards", roles: ["admin"] },
  // AA-797 — every write to a tenant account: POST tenants (create), PATCH tenants/{id}
  // (plan/status), generate-key, brand-identity, config, offboard, restore. Prefix "tenants" (no slash)
  // so the bare create path is covered; reads (GET) stay open to every staff role.
  { prefix: "tenants", roles: ["admin"] },
  // AA-816 C6 — staff directory writes (create, roles, disable/enable, sign-out, sync). The backend narrows
  // further: staff.write needs an owner on a Cognito session (api/core/staff_authz.py), see canWriteStaff().
  // "platform/" (with the slash) so the write rule never matches "platform-stats".
  { prefix: "platform/", roles: ["admin"] },
];

// AA-577 follow-up — /api/admin/ paths that NO method may reach unless the role is listed (reads
// included), narrower than API_RULES.admin. The OpenAPI schema is admin-only: the backend
// /admin/openapi.json re-checks the forwarded x-admin-role.
export const API_READ_RULES: { prefix: string; roles: StaffRole[] }[] = [
  { prefix: "openapi.json", roles: ["admin"] },
  // AA-883 — backend /admin/platform/* (staff directory): admin only, re-checked by the backend
  // (staff_policy, capability staff.read). Matches "platform" and "platform/…", not "platform-stats".
  { prefix: "platform", roles: ["admin"] },
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
  const rule = API_WRITE_RULES.find((r) => path.startsWith(r.prefix));
  return !rule || (!!role && (rule.roles as string[]).includes(role));
}

/** Landing page after login / for the retired /admin/dashboard. */
export function landingPage(role: string | null | undefined): string {
  return role === "admin" ? "/admin/overview" : "/admin/review";
}

// AA-882 (GĐ3 review) — the backend capability map (api/core/staff_authz.py ROLE_PERMS), mirrored so
// a page can hide or disable a control the backend would refuse with 403. The backend stays the
// authority; this is UI only. tests/unit/test_aa882_staff_authz.py keeps the two maps identical. One
// explicit list per role (no spreads) so that test can read it.
export type Capability =
  | "content.read" | "content.write" | "review.act" | "master.admin" | "atoms.write" | "photos.write"
  | "a4.unpublish" | "quality.write" | "content.settings.write" | "tenant.read" | "tenant.write"
  | "ops.read" | "ops.write" | "ai.admin" | "spend.read" | "spend.write" | "staff.read" | "audit.read";

export const ROLE_PERMS: Record<StaffRole, readonly Capability[]> = {
  admin: [
    "content.read", "content.write", "review.act", "master.admin", "atoms.write", "photos.write",
    "a4.unpublish", "quality.write", "content.settings.write", "tenant.read", "tenant.write",
    "ops.read", "ops.write", "ai.admin", "spend.read", "spend.write", "staff.read", "audit.read",
  ],
  reviewer: ["content.read", "content.write", "review.act"],
  content: ["content.read", "content.write", "review.act"],
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

/** A readable message for a failed staff call: the backend's policy 403 reads "Role 'x' lacks …". */
export function staffErrorMessage(status: number, detail: unknown, fallback: string): string {
  if (status === 403 && typeof detail === "string" && detail.startsWith("Role '")) {
    return "Admin only: your role cannot make this change.";
  }
  return typeof detail === "string" && detail ? detail : fallback;
}

export const ROLE_LABEL: Record<StaffRole, string> = {
  admin: "Administrator",
  reviewer: "Reviewer",
  content: "Content Team",
};
