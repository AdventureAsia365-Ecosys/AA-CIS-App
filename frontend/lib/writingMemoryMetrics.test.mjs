// frontend/lib/writingMemoryMetrics.test.mjs
// AA-922 WM-31: the pure helpers of the Metrics tab (lib/writingMemory.ts) agree with the backend
// (services/writing_memory/kpi.py: SOFT_STOP_CODES, "not measured" is None, the move_pointer actions). Run:
// npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import { SOFT_STOP_CODES, actionTone, pctOrDash } from "./writingMemory.ts";

test("the soft-stop codes are the backend's (and CIS 314's CHECK)", () => {
  assert.deepEqual([...SOFT_STOP_CODES],
    ["reflag_over_8pct", "light_edit_drop", "minutes_to_fix_up", "tenant_edit_up", "cost_up"]);
});

test("a rate that was not measured prints a dash, never 0%", () => {
  assert.equal(pctOrDash(null), "—");
  assert.equal(pctOrDash(undefined), "—");
  assert.equal(pctOrDash(0), "0.0%");
  assert.equal(pctOrDash(0.0833), "8.3%");
  assert.equal(pctOrDash(0.5, 0), "50%");
});

test("an out-of-service action is danger, a keep or dry run a warning, none a success", () => {
  for (const a of ["rolled_back", "retired_alone", "memory_off"]) assert.equal(actionTone(a), "danger");
  assert.equal(actionTone("kept"), "warning");
  assert.equal(actionTone("dry_run"), "warning");
  assert.equal(actionTone("none"), "success");
  assert.equal(actionTone("refused:no_previous"), "neutral");
});
