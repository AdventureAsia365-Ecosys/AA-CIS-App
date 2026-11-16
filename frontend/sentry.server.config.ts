// frontend/sentry.server.config.ts
// AA-1111 — the server SDK, imported by instrumentation.ts register() in the nodejs runtime. The DSN: SENTRY_DSN when
// the task maps one at run time, else the public build-time NEXT_PUBLIC_SENTRY_DSN (inlined by next build). None = off.
import * as Sentry from "@sentry/nextjs";
import { sentryInitOptions } from "./lib/sentryConfig";

const options = sentryInitOptions({
  dsn: process.env.SENTRY_DSN || process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.NEXT_PUBLIC_ENV,
  release: process.env.NEXT_PUBLIC_BUILD_SHA,
});
if (options) Sentry.init(options);
