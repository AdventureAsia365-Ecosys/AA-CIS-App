// frontend/instrumentation-client.ts
// AA-1111 (S282 Sentry coverage) — Sentry in the browser. Replaces sentry.client.config.ts, which Next 16 with
// Turbopack never loads. The DSN is a public build-time value (NEXT_PUBLIC_SENTRY_DSN, the Docker build arg fed by the
// `dev` environment variable ADMIN_PUBLIC_SENTRY_DSN in Deploy Admin Dev); empty = no init, nothing is sent.
// No PII, no tracing, no replay (lib/sentryConfig.ts).
import * as Sentry from "@sentry/nextjs";
import { sentryInitOptions } from "./lib/sentryConfig";

const options = sentryInitOptions({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.NEXT_PUBLIC_ENV,
  release: process.env.NEXT_PUBLIC_BUILD_SHA,
});
if (options) Sentry.init(options);

// Navigation hook the SDK asks for (a no-op while Sentry is not initialised; no tracing is sampled).
export const onRouterTransitionStart = Sentry.captureRouterTransitionStart;
