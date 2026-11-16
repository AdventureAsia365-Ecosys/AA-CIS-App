// frontend/lib/sentryConfig.test.mjs
// AA-1111 (S282 Sentry coverage) — lib/sentryConfig.ts: no DSN = no Sentry.init (the client, server and edge configs
// skip it), the options send no personal data and no trace, and scrubEvent drops what could still ride along.
// Run: node --test --experimental-strip-types --no-warnings lib/sentryConfig.test.mjs (npm run test:unit). The CI-run
// copy of the wiring checks is tests/unit/test_aa1111_admin_sentry_instrumentation.py. Hosts and values are invented.

import assert from "node:assert/strict";
import { test } from "node:test";

import { scrubEvent, sentryInitOptions } from "./sentryConfig.ts";

test("no DSN means no init options (the client init is skipped)", () => {
  assert.equal(sentryInitOptions({}), null);
  assert.equal(sentryInitOptions({ dsn: undefined, environment: "dev" }), null);
  assert.equal(sentryInitOptions({ dsn: "" }), null);
  assert.equal(sentryInitOptions({ dsn: "   " }), null);
});

test("a DSN gives errors-only options without personal data", () => {
  const opts = sentryInitOptions({ dsn: " https://k@ingest.example.test/7 ", environment: "", release: "abc123" });
  assert.ok(opts);
  assert.equal(opts.dsn, "https://k@ingest.example.test/7");
  assert.equal(opts.environment, "dev");
  assert.equal(opts.release, "abc123");
  assert.equal(opts.tracesSampleRate, 0);
  assert.equal(opts.beforeSend, scrubEvent);
  assert.deepEqual(opts.dataCollection, {
    userInfo: false,
    cookies: false,
    httpHeaders: false,
    urlQueryParams: false,
    httpBodies: [],
    stackFrameVariables: false,
  });
  assert.equal(sentryInitOptions({ dsn: "https://k@ingest.example.test/7", release: "" }).release, undefined);
  assert.equal(sentryInitOptions({ dsn: "https://k@ingest.example.test/7", environment: "stg" }).environment, "stg");
});

test("scrubEvent drops the user, cookies, credentials, body and query string", () => {
  const event = scrubEvent({
    message: "boom",
    user: { email: "someone@example.test" },
    request: {
      url: "https://admin.example.test/admin/tours?tour=1&token=x",
      cookies: { session: "t" },
      data: "{}",
      query_string: "token=x",
      headers: { Authorization: "Bearer t", Cookie: "c", "X-Admin-Secret": "s", "X-API-Key": "k", Accept: "text/html" },
    },
  });
  assert.deepEqual(event, {
    message: "boom",
    request: { url: "https://admin.example.test/admin/tours", headers: { Accept: "text/html" } },
  });
  assert.equal(scrubEvent(null), null);
  assert.equal(scrubEvent("text"), "text");
});

test("scrubEvent drops the query string of the request path onRequestError records", () => {
  const event = scrubEvent({
    contexts: { nextjs: { request_path: "/api/things?token=x&id=1", router_path: "/api/things" }, runtime: { name: "node" } },
  });
  assert.deepEqual(event.contexts, {
    nextjs: { request_path: "/api/things", router_path: "/api/things" },
    runtime: { name: "node" },
  });
  assert.deepEqual(scrubEvent({ contexts: { nextjs: null } }), { contexts: { nextjs: null } });
});

test("scrubEvent drops the query string of breadcrumb URLs (navigation, fetch, xhr)", () => {
  const event = scrubEvent({
    breadcrumbs: [
      { category: "navigation", data: { from: "/login?next=%2Fadmin&token=x", to: "/admin?tab=1" } },
      { category: "fetch", data: { method: "GET", url: "/api/admin/v1/things?id=7", status_code: 200 } },
      { category: "console", message: "hello" },
      null,
    ],
  });
  assert.deepEqual(event.breadcrumbs, [
    { category: "navigation", data: { from: "/login", to: "/admin" } },
    { category: "fetch", data: { method: "GET", url: "/api/admin/v1/things", status_code: 200 } },
    { category: "console", message: "hello" },
    null,
  ]);
});
