// frontend/lib/adminIa.ts
// AA-1039 R6 slice 1 (S266) — the CIS admin information architecture as plain data: the seven nav groups (root ADR 0019
// d13: Home, Tenants, AA Catalogue, Tenant content QA, AI & spend, Writing Lab, Platform), the pages that moved and
// where their old URLs go. Dependency-free (no React, no Node APIs) so next.config.ts (redirects), app/_kit/adminNav.tsx
// (the sidebar, breadcrumb, ⌘K palette), the unit test (lib/adminIa.test.mjs) and the smoke spec
// (tests/e2e/smoke/aa1039-admin-ia-redirects.spec.ts) all read the same table.
//
// Visibility still follows lib/rbac.ts PAGE_RULES (an item shows only when the role may open its page). An item marked
// `hidden` is a reserved slot: it is in the model (so its place is decided) but no sidebar, palette or breadcrumb shows
// it until its page ships.
//
// AA-1037 (root ADR 0019 d9 "Screens now"): an eighth group, Trade, next to the seven of d13. Its items are `external`:
// pages of AA-Booking's admin (same WORKFORCE sign-in), `href` being the path in that app; adminNav.tsx builds the URL
// through the app registry (lib/apps.ts appPageUrl) and opens it in the same tab; an external item never matches the
// current route. It has no CIS page rule, so it shows to a role holding one of its `capabilities` (lib/rbac.ts
// canSeeNavItem); Booking checks its own grant (shared.staff_app_grant is not in the CIS session) and answers 403 to a
// person without one.

import type { Capability } from "./rbac";

/** Icon keys; app/_kit/adminNav.tsx maps each to a lucide icon (TypeScript checks the map is complete). */
export type NavIcon =
  | "overview" | "appHealth" | "reports" | "dataCoverage"
  | "tenants" | "agencies" | "erasure" | "plans" | "assignments"
  | "upload" | "rewrite" | "review" | "master" | "atoms" | "places" | "placeCards" | "photos" | "facts" | "supply"
  | "aaContent" | "referencePrices"
  | "seo" | "settings"
  | "quality" | "trace" | "reviewLog" | "slate" | "compliance"
  | "aiGateway" | "spend" | "usage"
  | "lab" | "writingMemory" | "decisions"
  | "staff" | "audit" | "jobs" | "alerts" | "apiDocs" | "dataModel" | "docs" | "uiKit"
  | "bookingAdmin" | "agencyBilling" | "agencyTiers" | "b2bDesk";

export interface IaItem {
  href: string;
  label: string;
  icon: NavIcon;
  /** Other routes that belong to this entry (its sub-pages), for the active state. */
  also?: string[];
  /** A reserved slot: kept in the model, shown nowhere until its page ships. */
  hidden?: boolean;
  /** AA-1037: a page of another app of the registry (lib/apps.ts AppId); `href` is its path in that app. */
  external?: "booking";
  /** AA-1037: an external item shows to a role holding any of these (it has no CIS page rule). */
  capabilities?: readonly Capability[];
}

export interface IaGroup {
  label: string;
  items: IaItem[];
}

/** AA-1037: who sees the Trade group: billing.read (admin, finance) or tenant.book.account.read (customer_success), the
 *  roles that read Tenant 360 › Billing. Booking admin decides the rest from its own grant. */
export const TRADE_CAPABILITIES: readonly Capability[] = ["billing.read", "tenant.book.account.read"];

/** The groups in display order: the seven of root ADR 0019 d13, plus Trade (d9 "Screens now", AA-1037) before
 *  Platform. */
export const ADMIN_GROUP_LABELS = [
  "Home", "Tenants", "AA Catalogue", "Tenant content QA", "AI & spend", "Writing Lab", "Trade", "Platform",
] as const;

export const ADMIN_IA: IaGroup[] = [
  { label: "Home", items: [
    { href: "/admin/overview", label: "Overview", icon: "overview" },
    // Health and running commit of every app (until the Home health strip replaces it).
    { href: "/admin/platform/apps", label: "App health", icon: "appHealth" },
    { href: "/admin/reports", label: "Reports", icon: "reports" },
    { href: "/admin/data-coverage", label: "Data coverage", icon: "dataCoverage" },
  ] },
  { label: "Tenants", items: [
    // The directory; a row opens the Tenant 360 drawer (?tenant=<id>).
    { href: "/admin/tenants", label: "Directory", icon: "tenants" },
    // AA-1035 R2 slice 2: who looks after which tenant (owner, backup, support; the scope of the tenant desk roles).
    { href: "/admin/tenants/assignments", label: "Assignments", icon: "assignments" },
    { href: "/admin/tenants/agencies", label: "CRM agencies", icon: "agencies" },
    { href: "/admin/tenants/data-erasure", label: "Data erasure", icon: "erasure" },
    // The plans and add-ons catalogue (AA-1028 R1 slice 3, tenant control plane).
    { href: "/admin/tenants/plans", label: "Plans & add-ons", icon: "plans" },
  ] },
  { label: "AA Catalogue", items: [
    { href: "/admin/upload", label: "Upload (S0)", icon: "upload" },
    { href: "/admin/s1-rewrite", label: "Rewrite (S1)", icon: "rewrite" },
    { href: "/admin/review", label: "Review Queue", icon: "review" },
    { href: "/admin/master-content", label: "Master Content", icon: "master" },
    { href: "/admin/atom-curation", label: "Atoms & routes", icon: "atoms" },
    { href: "/admin/places", label: "Places", icon: "places" },
    // AA-1039 R6 rest (IA §6.2, Places & photos): the place cards (was the Cards tab of Content quality), TENANT_QA
    // roles on their own route (lib/rbac.ts), so content_lead and tenant_qa keep the card review.
    { href: "/admin/places/cards", label: "Place cards", icon: "placeCards" },
    { href: "/admin/photos", label: "Photos", icon: "photos" },
    { href: "/admin/facts", label: "Facts", icon: "facts" },
    // AA-1091: the tours' structured reference prices (the customer-service team enters and reviews them)
    { href: "/admin/reference-prices", label: "Reference prices", icon: "referencePrices" },
    { href: "/admin/supply-content", label: "Supply copy", icon: "supply" },
    { href: "/admin/aa-content", label: "AA content", icon: "aaContent" },
    { href: "/admin/seo-intelligence", label: "SEO Intelligence", icon: "seo" },
    // AA-1039 R6 rest: the catalogue's own settings (pipeline gates, the AA house style, SEO config); the aa_internal
    // tenant row went to its Tenant 360 and the UI kit reference to Platform (IA §6.2).
    { href: "/admin/settings", label: "Catalogue settings", icon: "settings" },
  ] },
  { label: "Tenant content QA", items: [
    { href: "/admin/tenant-qa/quality", label: "Content quality", icon: "quality" },
    { href: "/admin/tenant-qa/trace", label: "Content Trace", icon: "trace" },
    { href: "/admin/tenant-qa/review-log", label: "Review log", icon: "reviewLog" },
    { href: "/admin/tenant-qa/slate", label: "Slate", icon: "slate" },
    // AA-1039 R6 slice 2: the AA-subject gate holds and the retired-tour flags of live posts (root ADR 0019 d20 / d21).
    { href: "/admin/tenant-qa/compliance", label: "Compliance", icon: "compliance" },
  ] },
  { label: "AI & spend", items: [
    // AA-1039 R6 slice 3: the AI gateway (tabs Providers with its Connections / credit views, Routing, Models &
    // trials, Budgets with app profiles, Calls, Guide; was "AI providers") and one Spend page (was Cost Operations and
    // Cost per app; /admin/cost-ops and /admin/platform/cost redirect).
    { href: "/admin/ai-gateway", label: "AI gateway", icon: "aiGateway" },
    { href: "/admin/spend", label: "Spend", icon: "spend" },
    // AA-1045: requests, tokens, cost and latency of every paid AI and data provider by any one or two dimensions.
    { href: "/admin/usage", label: "Usage explorer", icon: "usage" },
  ] },
  { label: "Writing Lab", items: [
    // AA-1039 R6 slice 3: one page per pipeline stage (/admin/lab/<stage>; /admin/lab opens the first) over the
    // existing learning tables, then the Writing memory tools (moved from /admin/writing-memory) and Jev Decisions.
    { href: "/admin/lab", label: "Stages", icon: "lab" },
    { href: "/admin/lab/writing-memory", label: "Writing memory", icon: "writingMemory" },
    { href: "/admin/decisions", label: "Jev Decisions", icon: "decisions" },
  ] },
  // AA-1037 (root ADR 0019 d9 "Screens now"): Booking admin's B2B screens, opened through the app registry (same tab).
  // Merging them into this frontend is deferred to R8 (d9).
  { label: "Trade", items: [
    { href: "/", label: "Booking admin", icon: "bookingAdmin", external: "booking", capabilities: TRADE_CAPABILITIES },
    { href: "/agency-billing", label: "Agency billing", icon: "agencyBilling", external: "booking",
      capabilities: TRADE_CAPABILITIES },
    { href: "/pricing/agency-tiers", label: "Agency tiers", icon: "agencyTiers", external: "booking",
      capabilities: TRADE_CAPABILITIES },
    { href: "/b2b-desk", label: "B2B desk", icon: "b2bDesk", external: "booking", capabilities: TRADE_CAPABILITIES },
  ] },
  { label: "Platform", items: [
    { href: "/admin/platform/staff", label: "Staff & access", icon: "staff" },
    { href: "/admin/platform/audit", label: "Audit log", icon: "audit" },
    { href: "/admin/jobs", label: "Jobs", icon: "jobs" },
    { href: "/admin/platform/alerts", label: "Alert recipients", icon: "alerts" },
    { href: "/admin/api-docs", label: "API Docs", icon: "apiDocs" },
    { href: "/admin/data-model", label: "Data model", icon: "dataModel" },
    { href: "/admin/docs", label: "Docs", icon: "docs" },
    // AA-1039 R6 rest: the design-system reference (was the UI Kit tab of Settings).
    { href: "/admin/platform/ui-kit", label: "UI kit", icon: "uiKit" },
  ] },
];

/** The AA internal tenant (shared.tenants, the catalogue's own tenant row). */
export const AA_INTERNAL_TENANT_ID = "00000000-0000-0000-0000-000000000001";

/** A page that moved: `from` (exact path, optionally only with `query`) answers 307 to `to`; the query string carries
 *  over (Next.js passes it through, merged with a query of `to`, whose own keys win). */
export interface RouteMove {
  from: string;
  to: string;
  query?: { key: string; value: string };
}

// 307 (permanent: false), not 308: browsers cache a 308 for good, and the later R6 slices may move these again.
export const ADMIN_ROUTE_MOVES: RouteMove[] = [
  // AA-1039 R6 rest: the Cards tab of Content quality is AA Catalogue › Place cards (tech lead, option A, 05/11/2026).
  // The old Content quality URL with that tab goes there in one hop too (the first matching move wins).
  { from: "/admin/content-quality", query: { key: "tab", value: "cards" }, to: "/admin/places/cards" },
  { from: "/admin/tenant-qa/quality", query: { key: "tab", value: "cards" }, to: "/admin/places/cards" },
  { from: "/admin/content-quality", to: "/admin/tenant-qa/quality" },
  { from: "/admin/tenant-activity", to: "/admin/tenant-qa/trace" },
  { from: "/admin/platform-stats", to: "/admin/tenant-qa/review-log" },
  // Slate was section 05 of the atom-curation page; it is tenant content, so it moved to Tenant content QA.
  { from: "/admin/atom-curation", query: { key: "section", value: "slate" }, to: "/admin/tenant-qa/slate" },
  { from: "/admin/platform/agencies", to: "/admin/tenants/agencies" },
  { from: "/admin/data-erasure", to: "/admin/tenants/data-erasure" },
  // AA-1039 R6 slice 3: Writing memory moved under the Writing Lab; Cost Operations and Cost per app became one
  // Spend page (Cost per app is its By app tab).
  { from: "/admin/writing-memory", to: "/admin/lab/writing-memory" },
  { from: "/admin/cost-ops", to: "/admin/spend" },
  { from: "/admin/platform/cost", to: "/admin/spend?tab=apps" },
  // AA-1039 R6 rest (IA §6.2, AA Catalogue): SEO Intelligence's Spend tab folded into Spend › DataForSEO (the same
  // dfs_call_log rollup, by endpoint and tenant, plus balance, runs and markets); the tab never had a URL of its own,
  // so `?tab=spend` is the one a link would carry. Settings lost its tenant and UI kit tabs: the aa_internal tenant
  // row is its Tenant 360 (Settings tab: plan, limits), the UI kit reference is a Platform page.
  { from: "/admin/seo-intelligence", query: { key: "tab", value: "spend" }, to: "/admin/spend?tab=spend&view=dfs" },
  { from: "/admin/settings", query: { key: "tab", value: "tenant" },
    to: `/admin/tenants/${AA_INTERNAL_TENANT_ID}?tab=settings` },
  { from: "/admin/settings", query: { key: "tab", value: "ui-kit" }, to: "/admin/platform/ui-kit" },
];

/** Tenant 360 of one tenant (AA-1039 R6 rest: a route, /admin/tenants/<id>; was the Directory drawer at
 *  /admin/tenants?tenant=<id>), optionally on one of its tabs (`?tab=cost`). */
export function tenant360Href(tenantId: string, tab?: string): string {
  const path = `/admin/tenants/${encodeURIComponent(tenantId)}`;
  return tab ? `${path}?tab=${encodeURIComponent(tab)}` : path;
}

/** Where an old Directory deep link (`/admin/tenants?tenant=<id>[&tab=…]`, query string without "?") goes now: its
 *  Tenant 360 route with the other keys kept; null when the query names no tenant. A client-side replace in the
 *  Directory page, because a Next.js redirect cannot move a query value into the path through this table. */
export function legacyTenantLinkTarget(query: string): string | null {
  const params = new URLSearchParams(query);
  const tenantId = params.get("tenant");
  if (!tenantId) return null;
  params.delete("tenant");
  const rest = params.toString();
  const path = tenant360Href(tenantId);
  return rest ? `${path}?${rest}` : path;
}

/** The Next.js redirect entries of ADMIN_ROUTE_MOVES (next.config.ts `redirects()`). */
export function adminIaRedirects(): {
  source: string; destination: string; permanent: false; has?: { type: "query"; key: string; value: string }[];
}[] {
  return ADMIN_ROUTE_MOVES.map((m) => ({
    source: m.from,
    destination: m.to,
    permanent: false as const,
    ...(m.query ? { has: [{ type: "query" as const, key: m.query.key, value: m.query.value }] } : {}),
  }));
}

/** Where `url` (path + optional query) lands after the moves, as Next.js would answer it (query kept; a key the
 *  destination sets itself keeps the destination's value). */
export function resolveAdminRoute(url: string): string {
  let current = url;
  for (let hop = 0; hop < 5; hop += 1) {
    const [rawPath, query = ""] = current.split("?", 2);
    const path = rawPath.length > 1 ? rawPath.replace(/\/+$/, "") : rawPath;
    const params = new URLSearchParams(query);
    const move = ADMIN_ROUTE_MOVES.find((m) => m.from === path && (!m.query || params.get(m.query.key) === m.query.value));
    if (!move) return current;
    // Next.js prepareDestination: { ...request query, ...destination query } (the destination's keys win).
    const [toPath, toQuery = ""] = move.to.split("?", 2);
    new URLSearchParams(toQuery).forEach((v, k) => params.set(k, v));
    const q = params.toString();
    current = q ? `${toPath}?${q}` : toPath;
  }
  return current;
}

/** Every admin page URL the S265 navigation linked (sidebar, footer Settings, the atom-curation sub-nav). Each must
 *  still open a page after the moves (lib/adminIa.test.mjs). Frozen: add to it, never remove. */
export const PRE_R6_ADMIN_ROUTES: readonly string[] = [
  "/admin/overview", "/admin/data-coverage", "/admin/reports",
  "/admin/platform/staff", "/admin/platform/audit", "/admin/platform/alerts", "/admin/platform/apps",
  "/admin/tenants", "/admin/platform/agencies",
  "/admin/jobs", "/admin/data-erasure",
  "/admin/ai-gateway", "/admin/cost-ops", "/admin/platform/cost", "/admin/decisions",
  "/admin/api-docs", "/admin/data-model", "/admin/docs",
  "/admin/upload", "/admin/s1-rewrite", "/admin/review", "/admin/master-content",
  "/admin/photos", "/admin/places", "/admin/aa-content", "/admin/facts", "/admin/supply-content",
  "/admin/atom-curation", "/admin/seo-intelligence",
  "/admin/tenant-activity", "/admin/platform-stats", "/admin/content-quality", "/admin/writing-memory",
  "/admin/settings",
  "/admin/atom-curation?section=atomize", "/admin/atom-curation?section=segment",
  "/admin/atom-curation?section=score", "/admin/atom-curation?section=route_hub",
  "/admin/atom-curation?section=slate",
];
