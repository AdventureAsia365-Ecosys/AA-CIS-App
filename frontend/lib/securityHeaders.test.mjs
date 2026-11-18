// frontend/lib/securityHeaders.test.mjs
// AA-1111 (S284, ZAP baseline) — lib/securityHeaders.ts: the full CSP (no wildcard fetch directive, scripts by nonce,
// no 'unsafe-inline' for scripts), the static header list, the Sentry origin and the nonce.
// Run: node --test --experimental-strip-types --no-warnings lib/securityHeaders.test.mjs (npm run test:unit). The
// CI-run wiring checks are tests/unit/test_aa876_frontend_edge.py. Hosts are invented.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  S3_UPLOAD_ORIGINS,
  STATIC_SECURITY_HEADERS,
  contentSecurityPolicy,
  newNonce,
  sentryOrigin,
} from "./securityHeaders.ts";

function directives(csp) {
  return new Map(csp.split(";").map((d) => d.trim().split(/\s+/)).map(([name, ...src]) => [name, src]));
}

test("with a nonce, scripts need the nonce and never 'unsafe-inline'", () => {
  const d = directives(contentSecurityPolicy({ nonce: "abc123==" }));
  assert.deepEqual(d.get("script-src"), ["'self'", "'nonce-abc123=='", "'strict-dynamic'"]);
  assert.ok(!d.get("script-src").includes("'unsafe-inline'"));
  assert.ok(!d.get("script-src").includes("'unsafe-eval'"));
});

test("without a nonce, scripts are same-origin files only", () => {
  const d = directives(contentSecurityPolicy());
  assert.deepEqual(d.get("script-src"), ["'self'"]);
});

test("dev adds 'unsafe-eval' for the next dev runtime", () => {
  const d = directives(contentSecurityPolicy({ nonce: "n", dev: true }));
  assert.ok(d.get("script-src").includes("'unsafe-eval'"));
});

test("every fetch directive is explicit and none is a bare wildcard", () => {
  const d = directives(contentSecurityPolicy({ nonce: "n" }));
  for (const name of ["default-src", "script-src", "style-src", "font-src", "img-src", "connect-src", "frame-src",
    "worker-src", "object-src", "base-uri", "form-action", "frame-ancestors"]) {
    assert.ok(d.has(name), `missing ${name}`);
    assert.ok(!d.get(name).includes("*"), `${name} has a bare *`);
  }
  assert.deepEqual(d.get("default-src"), ["'self'"]);
  assert.deepEqual(d.get("frame-ancestors"), ["'none'"]);
  assert.deepEqual(d.get("object-src"), ["'none'"]);
  assert.deepEqual(d.get("form-action"), ["'self'"]);
});

test("connect-src adds the Sentry origin and skips empty entries", () => {
  const d = directives(contentSecurityPolicy({ nonce: "n", connectSrc: ["https://ingest.example.test", null, ""] }));
  assert.deepEqual(d.get("connect-src"), ["'self'", ...S3_UPLOAD_ORIGINS, "https://ingest.example.test"]);
});

// The S0 upload page PUTs the Excel file to a presigned S3 URL (a different origin): without the S3 hosts in
// connect-src the browser refuses it and no catalog upload starts (S284 review of this change).
// CSP host-source match for "https://*.<suffix>" (one or more labels before the suffix) and exact https hosts.
function hostMatches(source, url) {
  const u = new URL(url);
  if (u.protocol !== "https:" || !source.startsWith("https://")) return false;
  const host = source.slice("https://".length);
  return host.startsWith("*.") ? u.hostname.endsWith(host.slice(1)) : u.hostname === host;
}

for (const nonce of ["n", undefined]) {
  test(`connect-src lets the S0 upload PUT reach a presigned S3 URL (${nonce ? "proxy" : "static"} policy)`, () => {
    const d = directives(contentSecurityPolicy(nonce ? { nonce } : {}));
    const connect = d.get("connect-src");
    for (const origin of S3_UPLOAD_ORIGINS) assert.ok(connect.includes(origin), `connect-src without ${origin}`);
    for (const url of [
      "https://example-bronze-bucket.s3.amazonaws.com/raw-inbox/t/file.xlsx?X-Amz-Signature=abc",
      "https://example-bronze-bucket.s3.us-west-1.amazonaws.com/raw-inbox/t/file.xlsx?X-Amz-Signature=abc",
    ]) {
      assert.ok(connect.some((src) => hostMatches(src, url)), `no connect-src for ${url}`);
    }
    // a host pattern only: no bucket name, no account id in a public repo
    assert.ok(!connect.some((src) => /\d{12}/.test(src)), "an account id in connect-src");
  });
}

test("sentryOrigin keeps only the https origin of a DSN", () => {
  assert.equal(sentryOrigin("https://key@o1.ingest.example.test/42"), "https://o1.ingest.example.test");
  assert.equal(sentryOrigin(" https://key@ingest.example.test:8443/7 "), "https://ingest.example.test:8443");
  assert.equal(sentryOrigin("http://key@ingest.example.test/7"), null);
  assert.equal(sentryOrigin("not a url"), null);
  assert.equal(sentryOrigin(""), null);
  assert.equal(sentryOrigin(undefined), null);
});

test("the static list carries HSTS, nosniff, COOP and the rest, no CSP", () => {
  const h = new Map(STATIC_SECURITY_HEADERS.map(({ key, value }) => [key, value]));
  assert.equal(h.get("Strict-Transport-Security"), "max-age=63072000; includeSubDomains");
  assert.ok(!h.get("Strict-Transport-Security").includes("preload"));
  assert.equal(h.get("X-Content-Type-Options"), "nosniff");
  assert.equal(h.get("Cross-Origin-Opener-Policy"), "same-origin");
  assert.equal(h.get("X-Frame-Options"), "DENY");
  assert.equal(h.get("Referrer-Policy"), "strict-origin-when-cross-origin");
  assert.match(h.get("Permissions-Policy"), /camera=\(\)/);
  assert.ok(!h.has("Content-Security-Policy"));
});

test("a nonce is fresh base64 of 16 bytes", () => {
  const a = newNonce();
  const b = newNonce();
  assert.notEqual(a, b);
  assert.equal(Buffer.from(a, "base64").length, 16);
});
