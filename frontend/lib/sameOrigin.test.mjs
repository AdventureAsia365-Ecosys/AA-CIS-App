// frontend/lib/sameOrigin.test.mjs — AA-911: the same-origin check behind the ALB (staff web on ECS), where the Next
// standalone server's nextUrl names its own bind address, not the public host.
// Run: npm run test:unit (node --test with --experimental-strip-types). Host names are invented.

import assert from "node:assert/strict";
import { test } from "node:test";

import { isCrossSiteWrite, isSameOrigin, requestOrigin } from "./sameOrigin.ts";

const BIND = "https://0.0.0.0:3000"; // what Next standalone puts in nextUrl behind the ALB

function req(headers, { method = "POST", origin = BIND } = {}) {
  const h = new Map(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return { method, headers: { get: (n) => h.get(n.toLowerCase()) ?? null }, nextUrl: { origin } };
}

test("behind the ALB the public origin comes from Host when the request came over HTTPS", () => {
  const r = req({ host: "admin.example-staff.test", "x-forwarded-proto": "https" });
  assert.equal(requestOrigin(r), "https://admin.example-staff.test");
  assert.equal(requestOrigin(req({ host: "Admin.Example-Staff.test", "x-forwarded-proto": "https, http" })),
    "https://admin.example-staff.test");
});

test("without X-Forwarded-Proto https (next dev, plain HTTP) nextUrl stays the origin", () => {
  assert.equal(requestOrigin(req({ host: "localhost:3000" }, { origin: "http://localhost:3000" })), "http://localhost:3000");
  assert.equal(requestOrigin(req({ host: "evil.example.test", "x-forwarded-proto": "http" })), BIND);
});

test("a write from the page's own host passes, one from a sibling host is cross-site", () => {
  const own = req({ host: "admin.example-staff.test", "x-forwarded-proto": "https", origin: "https://admin.example-staff.test" });
  assert.equal(isSameOrigin(own), true);
  assert.equal(isCrossSiteWrite(own), false);
  const sibling = req({ host: "admin.example-staff.test", "x-forwarded-proto": "https", origin: "https://preview.example-staff.test" });
  assert.equal(isCrossSiteWrite(sibling), true);
  // http page on the same name is another origin.
  const plain = req({ host: "admin.example-staff.test", "x-forwarded-proto": "https", origin: "http://admin.example-staff.test" });
  assert.equal(isCrossSiteWrite(plain), true);
});

test("Vercel and next dev keep the old rule (Origin equals nextUrl.origin)", () => {
  const vercel = req({ host: "admin.example-staff.test", "x-forwarded-proto": "https", origin: "https://admin.example-staff.test" },
    { origin: "https://admin.example-staff.test" });
  assert.equal(isSameOrigin(vercel), true);
  const dev = req({ host: "localhost:3000", origin: "http://localhost:3000" }, { origin: "http://localhost:3000" });
  assert.equal(isSameOrigin(dev), true);
});

test("safe methods and Sec-Fetch-Site keep their meaning", () => {
  assert.equal(isCrossSiteWrite(req({ origin: "https://other.example.test" }, { method: "GET" })), false);
  assert.equal(isSameOrigin(req({ "sec-fetch-site": "same-origin" })), true);
  assert.equal(isSameOrigin(req({ "sec-fetch-site": "same-site" })), false);
  assert.equal(isSameOrigin(req({}), { strict: true }), false);
});
