// frontend/sentry.edge.config.ts
// AA-1111 — the edge SDK, imported by instrumentation.ts register() in the edge runtime. Same DSN rule and options as
// sentry.server.config.ts (lib/sentryConfig.ts).
import * as Sentry from "@sentry/nextjs";
import { sentryInitOptions } from "./lib/sentryConfig";

const options = sentryInitOptions({
  dsn: process.env.SENTRY_DSN || process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.NEXT_PUBLIC_ENV,
  release: process.env.NEXT_PUBLIC_BUILD_SHA,
});
if (options) Sentry.init(options);
