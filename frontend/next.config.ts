import type { NextConfig } from "next";
import path from "path";
import { withSentryConfig } from "@sentry/nextjs";
import { PORTAL_BASE_URL, TRIP_PAGE_BASE_URL } from "./lib/apps";
import { adminIaRedirects } from "./lib/adminIa";

// AA-876 — security headers on every admin response, same list as the portal (AA-885). The CSP is
// deliberately minimal (no script-src / style-src yet: the pages use inline styles and Next inline
// scripts); it only stops framing, <base> hijacking, foreign form posts and plugins. The public
// white-label /trip/[tourId] pages are covered too: nothing embeds them in an iframe today; a partner
// embed would need an explicit frame-ancestors allow-list for /trip/:path*.
const SECURITY_HEADERS = [
  { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  {
    key: "Content-Security-Policy",
    value: "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'",
  },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
];

const nextConfig: NextConfig = {
  turbopack: {
    root: path.resolve(__dirname),
  },
  // AA-911 (root ADR 0019 d23): the ECS image (frontend/Dockerfile) builds the standalone server; the CI builds
  // (Frontend Build, Visual's `next start`) keep the default output.
  ...(process.env.NEXT_OUTPUT === "standalone" ? { output: "standalone" as const } : {}),
  poweredByHeader: false,
  async headers() {
    return [{ source: "/(.*)", headers: SECURITY_HEADERS }];
  },
  // AA-663 — admin IA consolidation. The legacy (internal) route group duplicated /admin/* and was
  // deleted; Brand Identity moved into Settings. Old URLs keep working. Redirects run before
  // proxy.ts, so the target's own role check applies.
  async redirects() {
    return [
      { source: "/upload", destination: "/admin/upload", permanent: false },
      { source: "/review", destination: "/admin/review", permanent: false },
      { source: "/catalog", destination: "/admin/master-content", permanent: false },
      { source: "/brand", destination: "/admin/settings?tab=brand", permanent: false },
      { source: "/admin/brand", destination: "/admin/settings?tab=brand", permanent: false },
      // AA-665 — External Spend became Cost Operations (AA-1039 R6 slice 3: now Spend at /admin/spend, one hop).
      // 307 (not 308) so the old URL can be reused. AA-804 UI part 2:
      // the old page was the LLM spend, so a bare link opens the AI & data spend tab (its LLM view, UI part 3); a
      // link that names a tab keeps its own (the query string carries over, so the destination adds no second `tab`;
      // a retired one such as ?tab=budgets or ?tab=llm is then sent on by the Spend page itself).
      { source: "/admin/llm-usage", has: [{ type: "query", key: "tab" }], destination: "/admin/spend",
        permanent: false },
      { source: "/admin/llm-usage", missing: [{ type: "query", key: "tab" }], destination: "/admin/spend?tab=spend",
        permanent: false },
      // AA-885 / AA-979 — the tenant portal is its own app and host (AA-Partner portal/, Fargate). Old portal
      // URLs answer 308 to the same path there (the query string carries over); the admin app serves
      // no portal page any more.
      { source: "/portal", destination: `${PORTAL_BASE_URL}/portal`, permanent: true },
      { source: "/portal/:path*", destination: `${PORTAL_BASE_URL}/portal/:path*`, permanent: true },
      { source: "/tenant-login", destination: `${PORTAL_BASE_URL}/tenant-login`, permanent: true },
      // AA-979 — the public trip page lives on the AA-Partner CRM web (client host). Permanent (308) since the P2b
      // clean-up (runbook §4.5); it was a 307 during the rollback window (critic S5). The query string carries over.
      // The CIS page (app/trip/[tourId]) was deleted at the clean-up.
      { source: "/trip/:path*", destination: `${TRIP_PAGE_BASE_URL}/trip/:path*`, permanent: true },
      // AA-1039 R6 slice 1 — the admin pages that moved into the Tenants and Tenant content QA groups (root ADR 0019
      // d13). 307s; the table and its test live in lib/adminIa.ts / lib/adminIa.test.mjs.
      ...adminIaRedirects(),
    ];
  },
};

export default withSentryConfig(nextConfig, {
  org: "dk-0ql",
  project: "aa-cis-frontend",
  silent: true,
  sourcemaps: {
    disable: false,
  },
  disableLogger: true,
});
