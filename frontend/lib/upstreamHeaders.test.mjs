// frontend/lib/upstreamHeaders.test.mjs — AA-697: the BFF adds the sandbox headers to an SVG and to nothing else.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import { upstreamHeaders } from "./upstreamHeaders.ts";

test("JSON and other types forward Content-Type only", () => {
  assert.deepEqual(upstreamHeaders("application/json"), { "Content-Type": "application/json" });
  assert.deepEqual(upstreamHeaders(null), { "Content-Type": "application/json" });
  assert.deepEqual(upstreamHeaders("text/markdown; charset=utf-8"), { "Content-Type": "text/markdown; charset=utf-8" });
});

test("an SVG gets the sandbox CSP and nosniff", () => {
  for (const ct of ["image/svg+xml", "Image/SVG+XML; charset=utf-8"]) {
    const h = upstreamHeaders(ct);
    assert.equal(h["Content-Type"], ct);
    assert.match(h["Content-Security-Policy"], /sandbox/);
    assert.match(h["Content-Security-Policy"], /default-src 'none'/);
    assert.equal(h["X-Content-Type-Options"], "nosniff");
  }
});
