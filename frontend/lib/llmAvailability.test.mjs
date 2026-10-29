// frontend/lib/llmAvailability.test.mjs
// AA-900: unit tests for the LLM availability card helpers in llmAvailability.ts.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import { jobLink, sortProviders, statusTone } from "./llmAvailability.ts";

test("statusTone maps every status and falls back to neutral", () => {
  assert.equal(statusTone("ok"), "success");
  assert.equal(statusTone("degraded"), "warning");
  assert.equal(statusTone("blocked"), "danger");
  assert.equal(statusTone("unknown"), "neutral");
  assert.equal(statusTone("something-new"), "neutral");
  assert.equal(statusTone(null), "neutral");
});

test("sortProviders puts text routes before embeddings, newest first", () => {
  const row = (provider, embed, at) => ({
    provider, account: null, embed, calls: 1, ok_calls: 1, last_call_at: at, last_ok_at: at,
  });
  const out = sortProviders([
    row("embed-new", true, "2026-10-29T12:00:00Z"),
    row("text-old", false, "2026-10-28T12:00:00Z"),
    row("text-new", false, "2026-10-29T11:00:00Z"),
    row("text-none", false, null),
  ]);
  assert.deepEqual(out.map((r) => r.provider), ["text-new", "text-old", "text-none", "embed-new"]);
});

test("jobLink searches the Jobs page by id", () => {
  assert.equal(jobLink("abc-123"), "/admin/jobs?q=abc-123");
  assert.equal(jobLink(undefined), null);
  assert.equal(jobLink(""), null);
});
