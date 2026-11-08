// lib/editGate.test.mjs — AA-1052 W6: the gated stage list matches the backend, and the hold-switch rule.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { GATED_STAGE_PREFIXES, holdChangeNeedsEvidence, isGatedStage } from "./editGate.ts";

test("the gated prefixes are the backend's change_control.GATED_STAGE_PREFIXES", () => {
  const py = readFileSync(new URL("../../services/quality/change_control.py", import.meta.url), "utf8");
  const m = py.match(/^GATED_STAGE_PREFIXES = \(([^)]*)\)/m);
  assert.ok(m, "GATED_STAGE_PREFIXES not found in change_control.py");
  const backend = [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]);
  assert.deepEqual([...GATED_STAGE_PREFIXES], backend);
});

test("gated stages: the prefixes and the policy defaults row", () => {
  assert.equal(isGatedStage("s1_generate"), true);
  assert.equal(isGatedStage("t9_blog_write"), true);
  assert.equal(isGatedStage("writing_memory_eval"), true);
  assert.equal(isGatedStage("*"), true);
  assert.equal(isGatedStage("a3_atomize"), false);
  assert.equal(isGatedStage("place_card_draft"), false);
});

test("a hold switch change of a gated stage needs evidence; thresholds and other stages do not", () => {
  assert.equal(holdChangeNeedsEvidence("t2_rewrite", null, true), true);
  assert.equal(holdChangeNeedsEvidence("t2_rewrite", true, null), true, "a reset of a set hold");
  assert.equal(holdChangeNeedsEvidence("t2_rewrite", true, true), false);
  assert.equal(holdChangeNeedsEvidence("t2_rewrite", undefined, null), false);
  assert.equal(holdChangeNeedsEvidence("a3_atomize", null, true), false);
});
