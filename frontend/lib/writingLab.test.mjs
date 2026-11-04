// frontend/lib/writingLab.test.mjs — AA-1039 R6 slice 3: the Writing Lab stages and helpers (lib/writingLab.ts), and
// that every stage label and tab label has an English and a Vietnamese message (app/admin/lab/messages.ts).
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  DEFAULT_STAGE, LAB_STAGES, LAB_TABS, countRows, evalTone, gateTone, parseLabTab, share, stageBySlug, stageHref,
  verdictTone,
} from "./writingLab.ts";

test("four stages, one per writing-memory writer, unique slugs that never shadow a Lab page", () => {
  assert.deepEqual(LAB_STAGES.map((s) => s.writer), ["a1", "t2", "t9_blog", "t9_social"]);
  const slugs = LAB_STAGES.map((s) => s.slug);
  assert.equal(new Set(slugs).size, slugs.length);
  for (const s of slugs) assert.match(s, /^[a-z0-9-]+$/);
  assert.ok(!slugs.includes("writing-memory"), "a stage slug must not collide with /admin/lab/writing-memory");
  assert.equal(DEFAULT_STAGE, "a1-s1");
});

test("stage lookup, links and tabs", () => {
  assert.equal(stageBySlug("t9-social")?.writer, "t9_social");
  assert.equal(stageBySlug("t9_social"), null);
  assert.equal(stageBySlug(undefined), null);
  assert.equal(stageHref("t2-rewrite"), "/admin/lab/t2-rewrite");
  assert.equal(stageHref("t2-rewrite", "gate"), "/admin/lab/t2-rewrite");
  assert.equal(stageHref("t2-rewrite", "samples"), "/admin/lab/t2-rewrite?tab=samples");
  assert.deepEqual([...LAB_TABS], ["gate", "samples", "labels", "experiments", "prompts"]);
  assert.equal(parseLabTab("prompts"), "prompts");
  assert.equal(parseLabTab("nope"), "gate");
  assert.equal(parseLabTab(null), "gate");
});

test("tones, shares and count rows", () => {
  assert.equal(gateTone("passing"), "success");
  assert.equal(gateTone("regression"), "danger");
  assert.equal(gateTone("reverify"), "warning");
  assert.equal(gateTone("memory_off"), "warning");
  assert.equal(gateTone("no_release"), "neutral");
  assert.equal(verdictTone("pass"), "success");
  assert.equal(verdictTone("fail"), "danger");
  assert.equal(verdictTone("baseline"), "info");
  assert.equal(verdictTone(null), "warning");
  // the live-release evaluation tile: release 0's baseline import is green, a fail or a regression red
  assert.equal(evalTone(null), "neutral");
  assert.equal(evalTone({ verdict: "baseline", regression_detected: false }), "success");
  assert.equal(evalTone({ verdict: "pass", regression_detected: false }), "success");
  assert.equal(evalTone({ verdict: "pass", regression_detected: true }), "danger");
  assert.equal(evalTone({ verdict: "fail", regression_detected: false }), "danger");
  assert.equal(evalTone({ verdict: "informative", regression_detected: false }), "neutral");
  assert.equal(share(1, 4), "25%");
  assert.equal(share(1, 0), "—");
  assert.deepEqual(countRows({ b: 2, a: 2, c: 5 }), [{ key: "c", n: 5 }, { key: "a", n: 2 }, { key: "b", n: 2 }]);
  assert.deepEqual(countRows(null), []);
});

test("every Lab message has a Vietnamese twin; stage and gate words exist; no issue ids", () => {
  // messages.ts imports the kit with an extensionless path, so it is read as text (like lib/adminIa.test.mjs does).
  const src = readFileSync(new URL("../app/admin/lab/messages.ts", import.meta.url), "utf8");
  const [en, vi] = src.split(/\n  \},\n  \{\n/);
  assert.ok(en && vi, "two catalogues");
  const keys = (block) => [...block.matchAll(/^    ([A-Za-z0-9_]+):/gm)].map((m) => m[1]);
  assert.deepEqual(keys(vi), keys(en), "same keys, same order");
  for (const code of ["no_release", "memory_off", "reverify", "regression", "passing"]) {
    assert.ok(keys(en).includes(`gate_${code}`), code);
  }
  for (const k of ["stageA1", "stageT2", "stageT9Blog", "stageT9Social"]) assert.ok(keys(en).includes(k), k);
  assert.doesNotMatch(src.replace(/^\/\/.*$/gm, ""), /AA-\d+/);
});
