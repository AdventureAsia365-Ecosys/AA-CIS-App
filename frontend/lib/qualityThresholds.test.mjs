// S279 thresholds lane L2 — the score colour / review level / meta band helpers read the thresholds in force
// (GET /admin/quality/thresholds) and stay neutral until they load; the generated registry carries no copy of them.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { inMetaBand, QUALITY_THRESHOLDS_PATH, reviewLevel, scoreLevel, scoreTone } from "./qualityThresholds.ts";

const BANDS = { top: 9.5, pass: 7.5, low: 4.5 };   // invented values, not the seeded ones
const SEO = { meta: { min: 120, max: 150 }, title: { min: 30, max: 50 }, slug: { max: 40 } };

test("score level and tone follow the bands passed in", () => {
  assert.equal(scoreLevel(9.5, BANDS), "top");
  assert.equal(scoreLevel(7.5, BANDS), "pass");
  assert.equal(scoreLevel(7.4, BANDS), "fail");
  assert.equal(scoreTone(9.6, BANDS), "success");
  assert.equal(scoreTone(8, BANDS), "warning");
  assert.equal(scoreTone(1, BANDS), "danger");
});

test("nothing is told before the bands load or without a score", () => {
  assert.equal(scoreLevel(8, undefined), null);
  assert.equal(scoreTone(null, BANDS), undefined);
  assert.equal(reviewLevel(8, null), null);
  assert.equal(inMetaBand(130, undefined), null);
});

test("review level uses the pass line and the critical / low boundary", () => {
  assert.equal(reviewLevel(7.5, BANDS), "ok");
  assert.equal(reviewLevel(4.5, BANDS), "low");
  assert.equal(reviewLevel(4.4, BANDS), "critical");
});

test("meta band check is inclusive", () => {
  assert.equal(inMetaBand(120, SEO), true);
  assert.equal(inMetaBand(150, SEO), true);
  assert.equal(inMetaBand(151, SEO), false);
});

test("the path goes through the BFF and the generated registry has no band copy", () => {
  assert.equal(QUALITY_THRESHOLDS_PATH, "/api/admin/quality/thresholds");
  const generated = readFileSync(new URL("./quality-registry.generated.ts", import.meta.url), "utf8");
  for (const name of ["PASS_LINE", "SCORE_BANDS", "SEO_BANDS"]) assert.ok(!generated.includes(`export const ${name}`));
});
