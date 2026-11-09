// frontend/lib/uploadStepper.test.mjs
// AA-1006 follow-up (S264 UI check): /admin/upload at 390 px was wider than the phone (main 675 px in a 390 viewport)
// because the five-step stepper was one nowrap row of labelled pills. Source guard, so a later edit does not bring the
// overflow back: the row wraps inside its container, and on a phone only the current step shows its label (the
// other steps keep it as their accessible name and tooltip), so all five fit on one 358 px line. Run: npm run test:unit.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const PAGE = fileURLToPath(new URL("../app/admin/upload/page.tsx", import.meta.url));

/** Source without comments, so a comment that quotes a style never satisfies or trips a check. */
function code() {
  return readFileSync(PAGE, "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, "")
    .replace(/^\s*\/\/.*$/gm, "")
    .replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
}

function stepper() {
  const src = code();
  const start = src.indexOf("function StepIndicator(");
  const end = src.indexOf("\nfunction ", start + 1);
  assert.ok(start > 0 && end > start, "StepIndicator not found (renamed? update this guard)");
  return src.slice(start, end);
}

test("the stepper row wraps and never grows wider than its container", () => {
  const src = stepper();
  assert.match(src, /data-testid="upload-stepper"/);
  const row = src.slice(src.indexOf('data-testid="upload-stepper"'), src.indexOf("STEPS.map("));
  assert.match(row, /flexWrap: "wrap"/, "the step row must wrap instead of running off the right edge");
  assert.match(row, /minWidth: 0/);
  assert.match(row, /maxWidth: "100%"/);
  assert.doesNotMatch(row, /overflowX: "(visible|scroll)"/);
});

test("on a phone only the current step shows its label; the others keep it for screen readers and as a tooltip", () => {
  const src = stepper();
  assert.match(src, /const isPhone = useIsPhone\(\);/);
  assert.match(src, /const showLabel = active \|\| !isPhone;/);
  assert.match(src, /\{showLabel && \(/, "the label must be conditional on showLabel");
  assert.match(src, /aria-label=\{showLabel \? undefined : s\.label\}/);
  assert.match(src, /title=\{showLabel \? undefined : s\.label\}/);
  assert.match(src, /aria-current=\{active \? "step" : undefined\}/);
});

test("the phone pills and connectors are compact (five steps fit 358 px)", () => {
  const src = stepper();
  assert.match(src, /padding: isPhone \? "7px 8px" : "7px 14px"/);
  assert.match(src, /width: isPhone \? 8 : 20/);
  // A label is one line; a step never shrinks its number dot.
  assert.match(src, /whiteSpace: "nowrap"/);
  assert.match(src, /flexShrink: 0/);
});

test("the page renders the stepper once, above the steps", () => {
  const src = code();
  assert.equal(src.match(/<StepIndicator step=\{step\} \/>/g)?.length, 1);
});
