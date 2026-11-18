// frontend/lib/securityHeaders.ts
// AA-1111 (S284, ZAP baseline triage of S283) — the admin's security headers in one place.
//
// - STATIC_SECURITY_HEADERS: sent on every response by next.config.ts headers() (AA-876 set + COOP same-origin).
// - contentSecurityPolicy(): the full Content-Security-Policy. proxy.ts sends it on every response it handles with a
//   fresh nonce per request (script-src 'nonce-…' 'strict-dynamic', no 'unsafe-inline' for scripts); Next reads the
//   nonce from the request's CSP header and puts it on its own scripts, app/layout.tsx on the three restore scripts.
//   The paths the proxy does not run on (the BFF under /api, the build chunks under /_next) get the same policy
//   without a nonce from next.config.ts (no inline script is ever served there).
// - style-src keeps 'unsafe-inline': the kit and the pages use React inline styles everywhere (a nonce cannot cover
//   style attributes). Moving them to classes is a kit-wide change, listed as a follow-up in AA-1111's notes.
// - img-src allows any https: image (tour photos, partner logos and source images come from several hosts); every
//   other fetch directive is 'self' plus the Google Fonts and Sentry origins the browser really talks to.
//
// Dependency-free (no imports) so lib/securityHeaders.test.mjs runs it under node --test.

export const NONCE_HEADER = "x-nonce";

export const HSTS_VALUE = "max-age=63072000; includeSubDomains";

/** The headers that do not change per request (next.config.ts, every path). */
export const STATIC_SECURITY_HEADERS: { key: string; value: string }[] = [
  { key: "Strict-Transport-Security", value: HSTS_VALUE },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
];

const GOOGLE_FONTS_CSS = "https://fonts.googleapis.com";
const GOOGLE_FONTS_FILES = "https://fonts.gstatic.com";

/** The origin of a Sentry DSN (where the browser SDK posts events), or null when there is no valid DSN. */
export function sentryOrigin(dsn: string | undefined | null): string | null {
  const raw = (dsn ?? "").trim();
  if (!raw) return null;
  try {
    const url = new URL(raw);
    return url.protocol === "https:" ? url.origin : null;
  } catch {
    return null;
  }
}

/** A fresh base64 nonce (128 bits) for one response. */
export function newNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

export interface CspOptions {
  /** Per-request nonce; without one scripts are 'self' only (paths that never serve an inline script). */
  nonce?: string;
  /** `next dev` needs eval for its refresh runtime; never set on a build. */
  dev?: boolean;
  /** Extra connect-src origins (the Sentry ingest origin). */
  connectSrc?: (string | null | undefined)[];
}

export function contentSecurityPolicy(opts: CspOptions = {}): string {
  const script = opts.nonce ? ["'self'", `'nonce-${opts.nonce}'`, "'strict-dynamic'"] : ["'self'"];
  if (opts.dev) script.push("'unsafe-eval'");
  const connect = ["'self'", ...(opts.connectSrc ?? []).filter((o): o is string => Boolean(o))];
  const directives: [string, string[]][] = [
    ["default-src", ["'self'"]],
    ["script-src", script],
    ["style-src", ["'self'", "'unsafe-inline'", GOOGLE_FONTS_CSS]],
    ["font-src", ["'self'", "data:", GOOGLE_FONTS_FILES]],
    ["img-src", ["'self'", "data:", "blob:", "https:"]],
    ["connect-src", connect],
    ["worker-src", ["'self'", "blob:"]],
    ["frame-src", ["'self'"]],
    ["manifest-src", ["'self'"]],
    ["frame-ancestors", ["'none'"]],
    ["base-uri", ["'self'"]],
    ["form-action", ["'self'"]],
    ["object-src", ["'none'"]],
  ];
  return directives.map(([name, sources]) => `${name} ${sources.join(" ")}`).join("; ");
}
