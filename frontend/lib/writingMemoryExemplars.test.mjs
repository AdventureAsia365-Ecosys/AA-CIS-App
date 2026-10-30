// frontend/lib/writingMemoryExemplars.test.mjs
// AA-921 WM-29: the pure helpers of the Exemplars tab (lib/writingMemory.ts) agree with the backend rules
// (services/writing_memory/exemplars.py: _TRANSITIONS, PER_CHANNEL, pick). Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  EXEMPLARS_PER_CHANNEL, EXEMPLAR_STATUSES, EXEMPLAR_WRITERS, byChannel, exemplarActions, exemplarTone,
} from "./writingMemory.ts";

test("the actions per status are the backend transitions", () => {
  assert.deepEqual(exemplarActions("candidate"), ["pin", "never"]);
  assert.deepEqual(exemplarActions("pinned"), ["unpin", "never"]);
  assert.deepEqual(exemplarActions("never"), ["unpin"]);
});

test("statuses, writers and the per-channel cap match the backend", () => {
  assert.deepEqual([...EXEMPLAR_STATUSES], ["candidate", "pinned", "never"]);
  assert.deepEqual([...EXEMPLAR_WRITERS], ["t2", "t9_blog", "t9_social"], "A1 never gets an example");
  assert.equal(EXEMPLARS_PER_CHANNEL, 2);
});

test("tones and the per-channel grouping", () => {
  assert.equal(exemplarTone("pinned"), "success");
  assert.equal(exemplarTone("never"), "danger");
  assert.equal(exemplarTone("candidate"), "neutral");
  const groups = byChannel([{ channel: "linkedin", id: 1 }, { channel: "facebook", id: 2 }, { channel: "linkedin", id: 3 }]);
  assert.deepEqual(groups.map(([c, items]) => [c, items.map((i) => i.id)]), [["facebook", [2]], ["linkedin", [1, 3]]]);
});
