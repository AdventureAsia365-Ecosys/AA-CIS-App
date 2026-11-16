// frontend/lib/sentryConfig.ts
// AA-1111 (S282 Sentry coverage) — the one place that decides what the admin app sends to Sentry, used by
// instrumentation-client.ts (browser) and sentry.server.config.ts / sentry.edge.config.ts (loaded by instrumentation.ts
// register()). Same rules as the AA-Partner web (web/lib/sentryScrub.ts): no DSN = no init (nothing is sent); no
// personal data and no credential (dataCollection off for users, cookies, headers, query strings, bodies and frame
// variables, and scrubEvent drops what could still ride along); no tracing, no replay.
// Dependency-free (no imports) so the unit test runs it under node --test (lib/sentryConfig.test.mjs).

type Dict = Record<string, unknown>;

const DROP_HEADERS = new Set(["authorization", "cookie", "set-cookie", "x-api-key", "x-admin-secret", "cloudfront-viewer-address"]);
const BREADCRUMB_URL_KEYS = ["url", "from", "to"];

function isDict(value: unknown): value is Dict {
  return !!value && typeof value === "object";
}

function stripQuery(holder: Dict, key: string): void {
  if (typeof holder[key] === "string") holder[key] = (holder[key] as string).split("?")[0];
}

function scrubRequest(request: Dict): void {
  delete request.cookies;
  delete request.data;
  delete request.query_string;
  const headers = request.headers;
  if (isDict(headers)) {
    for (const name of Object.keys(headers)) {
      if (DROP_HEADERS.has(name.toLowerCase())) delete headers[name];
    }
  }
  stripQuery(request, "url");
}

function scrubBreadcrumbs(breadcrumbs: unknown[]): void {
  for (const crumb of breadcrumbs) {
    if (!isDict(crumb) || !isDict(crumb.data)) continue;
    for (const key of BREADCRUMB_URL_KEYS) stripQuery(crumb.data, key);
  }
}

/**
 * beforeSend: drops the user, cookies, credential headers and the request body, and the query string wherever a URL
 * is recorded (the request, the request path onRequestError writes in contexts.nextjs, the browser breadcrumbs:
 * navigation from / to, fetch and xhr url).
 */
export function scrubEvent<T>(event: T): T {
  if (!isDict(event)) return event;
  delete event.user;
  if (isDict(event.request)) scrubRequest(event.request);
  const contexts = event.contexts;
  if (isDict(contexts) && isDict(contexts.nextjs)) stripQuery(contexts.nextjs, "request_path");
  if (Array.isArray(event.breadcrumbs)) scrubBreadcrumbs(event.breadcrumbs);
  return event;
}

export type SentryEnv = {
  /** The DSN; empty or unset = Sentry stays off. */
  dsn?: string;
  environment?: string;
  release?: string;
};

/**
 * The Sentry.init options for one runtime, or null when there is no DSN (the caller then skips Sentry.init, so an
 * image built without ADMIN_PUBLIC_SENTRY_DSN sends nothing and loads no transport).
 */
export function sentryInitOptions(env: SentryEnv) {
  const dsn = (env.dsn ?? "").trim();
  if (!dsn) return null;
  return {
    dsn,
    environment: (env.environment ?? "").trim() || "dev",
    release: (env.release ?? "").trim() || undefined,
    // No user fields, cookies, headers, query strings, bodies or frame variables (no PII, no tokens).
    dataCollection: {
      userInfo: false,
      cookies: false,
      httpHeaders: false,
      urlQueryParams: false,
      httpBodies: [],
      stackFrameVariables: false,
    },
    // Errors only: no performance tracing (the AA-Partner web rule; tracing would spend the Sentry span quota).
    tracesSampleRate: 0,
    beforeSend: scrubEvent,
  };
}
