// frontend/lib/rbac.ts
// AA-656 — the single staff RBAC matrix. middleware.ts (page gating), lib/auth-server.ts (BFF proxy
// gating) and the admin nav (app/_kit/adminNav.tsx) all read these tables; nothing else decides who
// sees what. The role always comes from a VERIFIED JWT (/auth/verify-admin), never from the
// client-writable `cis_role` cookie — that cookie is only a display hint for the UI.
//
// Staff roles live in shared.admin_users.role (enum shared.admin_role: admin, reviewer, content —
// migration 218). The matrix is documented in CONTEXT.md ("Staff roles and permissions"); AA-Booking
// staff roles map onto the same three (admin → admin, sales → reviewer tier, content_creator →
// content) so the ecosystem keeps one staff identity model.
//
// Edge-safe: no React, no Node APIs (middleware.ts imports it).

export type StaffRole = "admin" | "reviewer" | "content";
export type Role = StaffRole | "tenant";

export const STAFF_ROLES: readonly StaffRole[] = ["admin", "reviewer", "content"];
const ALL_STAFF: StaffRole[] = ["admin", "reviewer", "content"];

export function isStaffRole(role: string | null | undefined): role is StaffRole {
  return !!role && (STAFF_ROLES as readonly string[]).includes(role);
}

export interface PageRule {
  prefix: string;
  roles: Role[];
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
  // Content work — every staff role.
  { prefix: "/admin/upload", roles: ALL_STAFF },
  { prefix: "/admin/pipeline", roles: ALL_STAFF },
  { prefix: "/admin/master-content", roles: ALL_STAFF },
  { prefix: "/admin/review", roles: ALL_STAFF },
  { prefix: "/admin/s1-rewrite", roles: ALL_STAFF },
  { prefix: "/admin/settings", roles: ALL_STAFF },
  // Tenant portal — tenants, plus admin for support.
  { prefix: "/portal", roles: ["admin", "tenant"] },
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
  tenantProxy: ALL_STAFF,    // /api/tenant/[...path] called by a staff page (e.g. Master Content)
} satisfies Record<string, StaffRole[]>;

// AA-769 (S234) — writes that change tenant content are narrower than the route's read rule: a
// non-GET request under one of these /api/admin/ path prefixes needs one of `roles` (the backend
// re-checks the forwarded role). Today the Content Quality sentence actions (cut / rewrite) and the
// per-request "regenerate angles".
export const API_WRITE_RULES: { prefix: string; roles: StaffRole[] }[] = [
  { prefix: "content-quality/pieces/", roles: ["admin"] },
  { prefix: "content-quality/requests/", roles: ["admin"] },
  // AA-774 (S235) — place-card writes (verdict, merge, build job, fact edits) change what every
  // tenant's T9 seed reads; the backend re-checks the role on the fact edit.
  { prefix: "content-quality/place-cards", roles: ["admin"] },
  // AA-797 — every write to a tenant account: POST tenants (create), PATCH tenants/{id}
  // (plan/status), generate-key, brand-identity, config, offboard, restore. Prefix "tenants" (no slash)
  // so the bare create path is covered; reads (GET) stay open to every staff role.
  { prefix: "tenants", roles: ["admin"] },
];

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

export const ROLE_LABEL: Record<StaffRole, string> = {
  admin: "Administrator",
  reviewer: "Reviewer",
  content: "Content Team",
};
