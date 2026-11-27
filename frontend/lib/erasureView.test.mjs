// frontend/lib/erasureView.test.mjs
// AA-1190 (S296 live check): the Data erasure page's view in the URL (lib/erasureView.ts). Checks:
//   1. the tab: "offboarding" only for ?tab=offboarding (the My work tile "Data erasure to approve");
//   2. the selected jobs: job ids only, lowercased; anything else is ignored;
//   3. a change of tab or jobs keeps every other key, and the round trip gives the same view back.
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import { erasureSearch, erasureTabFromSearch, erasureViewFromSearch } from "./erasureView.ts";

const JOB = "0f3c2a4e-1b2c-4d5e-8f90-a1b2c3d4e5f6";
const APPLY = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d";

test("1. the tab a link asks for", () => {
  assert.equal(erasureTabFromSearch("?tab=offboarding"), "offboarding");
  assert.equal(erasureTabFromSearch("tab=offboarding&job=x"), "offboarding");
  assert.equal(erasureTabFromSearch(""), "subjects");
  assert.equal(erasureTabFromSearch("?tab=subjects"), "subjects");
  assert.equal(erasureTabFromSearch("?tab=OFFBOARDING"), "subjects");
});

test("2. the selected jobs: ids only", () => {
  assert.deepEqual(erasureViewFromSearch(`?job=${JOB.toUpperCase()}&apply=${APPLY}`), { job: JOB, apply: APPLY });
  assert.deepEqual(erasureViewFromSearch("?job=not-a-job&apply="), { job: null, apply: null });
  assert.deepEqual(erasureViewFromSearch(""), { job: null, apply: null });
});

test("3. a change keeps the other keys and round-trips", () => {
  assert.equal(erasureSearch("tab=offboarding", { job: JOB, apply: null }), `tab=offboarding&job=${JOB}`);
  assert.equal(erasureSearch(`job=${JOB}`, { tab: "offboarding" }), `job=${JOB}&tab=offboarding`);
  assert.equal(erasureSearch(`tab=offboarding&job=${JOB}`, { tab: "subjects" }), `job=${JOB}`);
  assert.equal(erasureSearch(`job=${JOB}&apply=${APPLY}`, { apply: null }), `job=${JOB}`);
  assert.equal(erasureSearch("", {}), "");
  const qs = erasureSearch("tab=offboarding", { job: JOB, apply: APPLY });
  assert.deepEqual(erasureViewFromSearch(qs), { job: JOB, apply: APPLY });
  assert.equal(erasureTabFromSearch(qs), "offboarding");
});
