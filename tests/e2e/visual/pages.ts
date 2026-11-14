// tests/e2e/visual/pages.ts
// AA-1008 (S1d/S2c, root ADR 0016 decision 9, guard G16) — the pages of the visual run. Pure data (no Playwright
// import), so frontend/lib/visualPages.test.mjs can check that every frontend/app/**/page.tsx route is listed here or
// excluded with a reason, and changed-pages.mjs can map a diff to the pages it touches.
//
// auth "public": captured by the per-PR run (`next build && next start` in the runner, no session, no backend) and
//   after the production deploy; "staff": only after the production deploy, signed in as the Cognito E2E user (a
//   reviewer: a page its role may not open is skipped, lib/rbac.ts PAGE_RULES).
// gallery: the gated kit gallery route (frontend/app/kit-gallery), served only by a KIT_GALLERY=1 build (the per-PR
//   run); selftest: the planted-overflow pages of the same route, run only by the self-test step, which must FAIL.

export interface VisualPage {
  /** File-name-safe id, used in the screenshot names: `<slug>__<1440|390>__<light|dark>.png`. */
  slug: string;
  /** The app route (`frontend/app/<route>/page.tsx`); dynamic segments in brackets. */
  route: string;
  /** The path to open. */
  path: string;
  auth: "public" | "staff";
  /** The page renders the kit frame (`[data-kit-frame]`, PageFrame); default true. */
  frame?: boolean;
  /** The document's expected HTTP status (default 200). */
  status?: number;
  gallery?: boolean;
  /** "document" or "frame": the planted overflow the self-test expects the run to catch. */
  selftest?: "document" | "frame";
}

export const PAGES: VisualPage[] = [
  // "/" redirects (to the upload page, which sends a visitor without a session to /login).
  { slug: "home", route: "/", path: "/", auth: "public", frame: false },
  { slug: "login", route: "/login", path: "/login", auth: "public", frame: false },
  // app/not-found.tsx (no page.tsx of its own): the 404 page of an unknown path.
  { slug: "not-found", route: "(not-found)", path: "/this-page-does-not-exist", auth: "public", frame: false, status: 404 },
  { slug: "kit-gallery", route: "/kit-gallery", path: "/kit-gallery", auth: "public", gallery: true },
  { slug: "selftest-document", route: "/kit-gallery", path: "/kit-gallery?plant=overflow", auth: "public", gallery: true, selftest: "document" },
  { slug: "selftest-frame", route: "/kit-gallery", path: "/kit-gallery?plant=frame", auth: "public", gallery: true, selftest: "frame" },
  { slug: "aa-content", route: "/admin/aa-content", path: "/admin/aa-content", auth: "staff" },
  { slug: "ai-gateway", route: "/admin/ai-gateway", path: "/admin/ai-gateway", auth: "staff" },
  { slug: "api-docs", route: "/admin/api-docs", path: "/admin/api-docs", auth: "staff" },
  { slug: "atom-curation", route: "/admin/atom-curation", path: "/admin/atom-curation", auth: "staff" },
  { slug: "data-coverage", route: "/admin/data-coverage", path: "/admin/data-coverage", auth: "staff" },
  { slug: "data-model", route: "/admin/data-model", path: "/admin/data-model", auth: "staff" },
  { slug: "decisions", route: "/admin/decisions", path: "/admin/decisions", auth: "staff" },
  { slug: "docs", route: "/admin/docs", path: "/admin/docs", auth: "staff" },
  { slug: "facts", route: "/admin/facts", path: "/admin/facts", auth: "staff" },
  { slug: "reference-prices", route: "/admin/reference-prices", path: "/admin/reference-prices", auth: "staff" },   // AA-1091
  { slug: "jobs", route: "/admin/jobs", path: "/admin/jobs", auth: "staff" },
  { slug: "lab", route: "/admin/lab", path: "/admin/lab", auth: "staff" },
  { slug: "lab-writing-memory", route: "/admin/lab/writing-memory", path: "/admin/lab/writing-memory", auth: "staff" },
  { slug: "master-content", route: "/admin/master-content", path: "/admin/master-content", auth: "staff" },
  { slug: "overview", route: "/admin/overview", path: "/admin/overview", auth: "staff" },
  { slug: "photos", route: "/admin/photos", path: "/admin/photos", auth: "staff" },
  { slug: "places", route: "/admin/places", path: "/admin/places", auth: "staff" },
  { slug: "places-cards", route: "/admin/places/cards", path: "/admin/places/cards", auth: "staff" },
  { slug: "platform", route: "/admin/platform", path: "/admin/platform", auth: "staff" },
  { slug: "platform-alerts", route: "/admin/platform/alerts", path: "/admin/platform/alerts", auth: "staff" },
  { slug: "platform-apps", route: "/admin/platform/apps", path: "/admin/platform/apps", auth: "staff" },
  { slug: "platform-audit", route: "/admin/platform/audit", path: "/admin/platform/audit", auth: "staff" },
  { slug: "platform-infra", route: "/admin/platform/infra", path: "/admin/platform/infra", auth: "staff" },
  { slug: "platform-settings", route: "/admin/platform/settings", path: "/admin/platform/settings", auth: "staff" },
  { slug: "platform-staff", route: "/admin/platform/staff", path: "/admin/platform/staff", auth: "staff" },
  { slug: "platform-ui-kit", route: "/admin/platform/ui-kit", path: "/admin/platform/ui-kit", auth: "staff" },
  { slug: "reports", route: "/admin/reports", path: "/admin/reports", auth: "staff" },
  { slug: "review", route: "/admin/review", path: "/admin/review", auth: "staff" },
  { slug: "s1-rewrite", route: "/admin/s1-rewrite", path: "/admin/s1-rewrite", auth: "staff" },
  { slug: "seo-intelligence", route: "/admin/seo-intelligence", path: "/admin/seo-intelligence", auth: "staff" },
  { slug: "settings", route: "/admin/settings", path: "/admin/settings", auth: "staff" },
  { slug: "spend", route: "/admin/spend", path: "/admin/spend", auth: "staff" },
  { slug: "supply-content", route: "/admin/supply-content", path: "/admin/supply-content", auth: "staff" },
  { slug: "tenant-qa", route: "/admin/tenant-qa", path: "/admin/tenant-qa", auth: "staff" },
  { slug: "tenant-qa-compliance", route: "/admin/tenant-qa/compliance", path: "/admin/tenant-qa/compliance", auth: "staff" },
  { slug: "tenant-qa-quality", route: "/admin/tenant-qa/quality", path: "/admin/tenant-qa/quality", auth: "staff" },
  { slug: "tenant-qa-review-log", route: "/admin/tenant-qa/review-log", path: "/admin/tenant-qa/review-log", auth: "staff" },
  { slug: "tenant-qa-slate", route: "/admin/tenant-qa/slate", path: "/admin/tenant-qa/slate", auth: "staff" },
  { slug: "tenant-qa-trace", route: "/admin/tenant-qa/trace", path: "/admin/tenant-qa/trace", auth: "staff" },
  { slug: "tenants", route: "/admin/tenants", path: "/admin/tenants", auth: "staff" },
  { slug: "tenants-agencies", route: "/admin/tenants/agencies", path: "/admin/tenants/agencies", auth: "staff" },
  { slug: "tenants-assignments", route: "/admin/tenants/assignments", path: "/admin/tenants/assignments", auth: "staff" },
  { slug: "tenants-data-erasure", route: "/admin/tenants/data-erasure", path: "/admin/tenants/data-erasure", auth: "staff" },
  { slug: "tenants-plans", route: "/admin/tenants/plans", path: "/admin/tenants/plans", auth: "staff" },
  { slug: "tenants-view-as", route: "/admin/tenants/view-as", path: "/admin/tenants/view-as", auth: "staff" },
  { slug: "upload", route: "/admin/upload", path: "/admin/upload", auth: "staff" },
  { slug: "usage", route: "/admin/usage", path: "/admin/usage", auth: "staff" },
];

/** The pages one run captures (visual.spec.ts; pure, so the unit guard tests it too). */
export function selectPages(pages: VisualPage[], opts: { auth: "public" | "staff"; slugs: string; gallery: boolean; selftest: boolean }): VisualPage[] {
  if (opts.selftest) return pages.filter((p) => p.selftest);
  const wanted = opts.slugs === "all" ? null : new Set(opts.slugs.split(",").map((s) => s.trim()).filter(Boolean));
  return pages.filter((p) => {
    if (p.selftest) return false;
    if (p.gallery) return opts.gallery && opts.auth === "public"; // the gallery is always captured where it exists
    if (p.auth !== opts.auth) return false;
    return wanted === null || wanted.has(p.slug);
  });
}

/** Routes with a page.tsx that the run does not open, each with its reason. */
export const EXCLUDED: Record<string, string> = {
  "/admin/lab/[stage]": "a Writing Lab stage, opened from the Lab cards by router push (no stable link to read an id from)",
  "/admin/platform/staff/[id]": "a staff account, opened from the directory rows by router push; staff names are personal data",
  "/admin/supply-content/[id]": "a supplier content item, opened from its list rows by router push",
  "/admin/tenants/[id]": "Tenant 360 of one agency, opened from the agency rows by router push",
};
