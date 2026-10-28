import type { NextConfig } from "next";
import path from "path";
import { withSentryConfig } from "@sentry/nextjs";
import { PORTAL_BASE_URL, TRIP_PAGE_BASE_URL } from "./lib/apps";

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
  poweredByHeader: false,
  async headers() {
    // AA-911: Preview builds (served on aa-cis-preview.lumiguides.it.com when aliased) are never indexed.
    const robots =
      process.env.VERCEL_ENV === "preview" ? [{ key: "X-Robots-Tag", value: "noindex, nofollow" }] : [];
    return [{ source: "/(.*)", headers: [...SECURITY_HEADERS, ...robots] }];
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
      // AA-665 — External Spend became Cost Operations. 307 (not 308) so the old URL can be reused; the query string
      // (?tab=…) carries over.
      { source: "/admin/llm-usage", destination: "/admin/cost-ops", permanent: false },
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
