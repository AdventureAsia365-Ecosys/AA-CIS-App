// frontend/lib/llmAvailability.test.mjs
// AA-900: unit tests for the LLM availability card helpers in llmAvailability.ts.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  coverageReasons, coverageRows, jobLink, roleSkips, sortProviders, statusTone,
} from "./llmAvailability.ts";

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

test("roleSkips lists the stages whose route models the role filter left out (AA-642 A5)", () => {
  const stages = [
    { stage: "s1_writer_demo", role: "writer", role_skipped: ["nova-lite@acc2", "llama4-scout@acc2"] },
    { stage: "s1_judge_demo", role: "judge", role_skipped: [] },
    { stage: "t2_check_demo", role: "validate" },
    { role: "writer", role_skipped: ["nova-lite@acc2"] },
    null,
  ];
  assert.deepEqual(roleSkips(stages), [
    { stage: "s1_writer_demo", role: "writer", skipped: ["nova-lite@acc2", "llama4-scout@acc2"] },
  ]);
  assert.deepEqual(roleSkips(undefined), []);
});

test("coverageRows lists stages without a live model first, then by name, and drops malformed rows", () => {
  const report = {
    stages: [
      { stage: "zz_writer", status: "live", live_model: "model-a", live_connection: "conn-1" },
      { stage: "aa_judge", status: "live", live_model: "model-b", live_connection: "conn-2" },
      { stage: "mm_validate", status: "none", models: [] },
      { status: "none" },
      null,
    ],
  };
  assert.deepEqual(coverageRows(report).map((s) => s.stage), ["mm_validate", "aa_judge", "zz_writer"]);
  assert.deepEqual(coverageRows(undefined), []);
  assert.deepEqual(coverageRows({ ready: false }), []);
});

test("coverageReasons names each model, its connection and its state", () => {
  const stage = {
    stage: "s", status: "none",
    models: [
      { model_key: "model-a", connection_key: "conn-1", state: "refused" },
      { model_key: "model-b", connection_key: null, state: "off" },
      { model_key: "model-c", connection_key: "conn-2", state: "unproven" },
      { model_key: "model-d", connection_key: "conn-2", state: "circuit_open" },
    ],
  };
  assert.deepEqual(coverageReasons(stage), ["model-a@conn-1 refused", "model-b@- off", "model-c@conn-2 unproven"]);
  assert.deepEqual(coverageReasons({ stage: "t", status: "none" }), []);
});
