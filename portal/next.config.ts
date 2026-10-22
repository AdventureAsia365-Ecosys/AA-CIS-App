// portal/next.config.ts
// AA-885 — the tenant portal is its own Next.js app (Vercel project aa-cis-portal, Root Directory
// `portal`, host portal.lumiguides.it.com). It holds no admin code and no admin secret.
import type { NextConfig } from "next";
import path from "path";

// AA-886 — /privacy and /terms are portal pages (app/privacy, app/terms): drafts with a "pending legal
// review" banner. The /privacy redirect to adventure.asia/privacy-policy (AA-885) is gone; that policy
// does not describe portal data, so the portal notice links it as the company-wide policy instead.

// AA-885 — security headers on every portal response. The CSP is deliberately minimal (no
// script-src / style-src yet: the pages use inline styles and Next inline scripts); it only stops
// framing, <base> hijacking, foreign form posts and plugins.
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

// AA-759 — portal routes carry no T-number slug any more. Every old path answers 308 (permanent) to
// its new one; Next passes the query string through, so ?piece= / ?tour_id= / ?resume_request_id=
// deep links keep working. The map mirrors ROUTES in app/portal/_shell/portalNav.tsx (guarded by
// tests/unit/test_aa885_portal_split.py and the portal smoke). Exact paths only: none of the old
// pages had sub-routes except t11-publish/connection, listed on its own.
const OLD_SLUG_REDIRECTS: { source: string; destination: string }[] = [
  { source: "/portal/t1-rewrite", destination: "/portal/tours" },
  { source: "/portal/t4-pool", destination: "/portal/tours/mine" },
  { source: "/portal/marketplace", destination: "/portal/tours/marketplace" },
  { source: "/portal/t0-brand", destination: "/portal/brand" },
  { source: "/portal/t7-planning", destination: "/portal/create/social" },
  { source: "/portal/t8-angle-gate", destination: "/portal/create/write" },
  { source: "/portal/t10-review", destination: "/portal/content" },
  { source: "/portal/t11-publish", destination: "/portal/content/publish" },
  { source: "/portal/t11-publish/connection", destination: "/portal/account/integrations" },
  { source: "/portal/activity", destination: "/portal/account/activity" },
  { source: "/portal/billing", destination: "/portal/account/billing" },
  { source: "/portal/settings", destination: "/portal/account/settings" },
  { source: "/portal/api", destination: "/portal/account/api" },
];

const nextConfig: NextConfig = {
  turbopack: {
    root: path.resolve(__dirname),
  },
  poweredByHeader: false,
  async redirects() {
    return [
      // The portal keeps its /portal/* paths on its own host (AA-885: no href rewrite). The bare
      // host lands on the dashboard; proxy.ts sends a visitor without a session to /tenant-login.
      { source: "/", destination: "/portal/dashboard", permanent: false },
      ...OLD_SLUG_REDIRECTS.map((r) => ({ ...r, permanent: true })),
    ];
  },
  async headers() {
    return [{ source: "/(.*)", headers: SECURITY_HEADERS }];
  },
};

export default nextConfig;
