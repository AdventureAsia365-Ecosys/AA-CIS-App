import type { NextConfig } from "next";
import path from "path";
import { withSentryConfig } from "@sentry/nextjs";

const nextConfig: NextConfig = {
  turbopack: {
    root: path.resolve(__dirname),
  },
  // AA-663 — admin IA consolidation. The legacy (internal) route group duplicated /admin/* and was
  // deleted; Brand Identity moved into Settings. Old URLs keep working. Redirects run before
  // middleware.ts, so the target's own role check applies.
  async redirects() {
    return [
      { source: "/upload", destination: "/admin/upload", permanent: false },
      { source: "/review", destination: "/admin/review", permanent: false },
      { source: "/catalog", destination: "/admin/master-content", permanent: false },
      { source: "/brand", destination: "/admin/settings?tab=brand", permanent: false },
      { source: "/admin/brand", destination: "/admin/settings?tab=brand", permanent: false },
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
