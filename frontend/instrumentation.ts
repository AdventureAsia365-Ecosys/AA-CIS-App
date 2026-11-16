// frontend/instrumentation.ts
// AA-1111 (S282 Sentry coverage) — Next 16 (Turbopack build) with @sentry/nextjs 10 loads the server and edge SDK only
// through this hook: the legacy sentry.server.config.ts / sentry.edge.config.ts are not injected by the build any
// more, so before this file the admin sent nothing from the server. register() runs once per server start and imports
// the config of the runtime it runs in; each config initialises Sentry only when a DSN is set (lib/sentryConfig.ts).
import * as Sentry from "@sentry/nextjs";

export async function register() {
  if (process.env.NEXT_RUNTIME === "nodejs") await import("./sentry.server.config");
  if (process.env.NEXT_RUNTIME === "edge") await import("./sentry.edge.config");
}

// Errors of server components, route handlers and the proxy (a no-op while Sentry is not initialised).
export const onRequestError = Sentry.captureRequestError;
