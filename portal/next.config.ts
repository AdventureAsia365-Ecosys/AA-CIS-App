// portal/next.config.ts
// AA-885 — the tenant portal is its own Next.js app (Vercel project aa-cis-portal, Root Directory
// `portal`, host portal.lumiguides.it.com). It holds no admin code and no admin secret.
import type { NextConfig } from "next";
import path from "path";

// AA-885 — the only legal page that exists on adventure.asia today (curl -sI → 200, 19/10/2026).
// adventure.asia has no terms page, so /terms is a portal page (app/terms/page.tsx) until AA-820.
const PRIVACY_POLICY_URL = "https://adventure.asia/privacy-policy";

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
      { source: "/privacy", destination: PRIVACY_POLICY_URL, permanent: false },
    ];
  },
  async headers() {
    return [{ source: "/(.*)", headers: SECURITY_HEADERS }];
  },
};

export default nextConfig;
