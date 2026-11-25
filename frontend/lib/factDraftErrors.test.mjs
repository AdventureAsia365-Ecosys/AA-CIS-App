// frontend/lib/factDraftErrors.test.mjs
// AA-1181: a refused sheet-draft action shows the page's own text for a known code (the reader's language), else the
// API's message, else the fallback; the hand-made refusal is wired into both pages and has EN + VI text; the Duplicate
// Review offers only Bypass and Create a new tour for a hand-made match.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import { FACT_DRAFT_HAND_MADE, refusalText } from "./factDraftErrors.ts";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");
const HAND_MADE = { detail: { code: "fact_draft_hand_made", message: "This tour was made by hand" } };

test("refusalText: a known code shows the page's own text", () => {
  assert.equal(FACT_DRAFT_HAND_MADE, "fact_draft_hand_made");
  assert.equal(refusalText(HAND_MADE, "failed", { [FACT_DRAFT_HAND_MADE]: "Tour này được tạo tay" }),
    "Tour này được tạo tay");
});

test("refusalText: an unknown code falls back to the API message, then the fallback", () => {
  assert.equal(refusalText(HAND_MADE, "failed"), "This tour was made by hand");
  assert.equal(refusalText({ detail: { code: "fact_draft_stale", message: "" } }, "failed"), "failed");
  assert.equal(refusalText({ detail: "Staging row not found" }, "failed"), "Staging row not found");
  assert.equal(refusalText(null, "failed"), "failed");
  assert.equal(refusalText({}, "failed", { toString: "x" }), "failed");
});

test("both pages map the hand-made refusal and its text exists in English and Vietnamese", () => {
  const drawer = read("../app/admin/_components/TourFactDraft.tsx");
  const merge = read("../app/admin/upload/MergePanel.tsx");
  assert.match(drawer, /\[FACT_DRAFT_HAND_MADE\]: t\("factHandMade"\)/);
  assert.match(drawer, /state\.hand_made === true/);               // Publish is off for a hand-made tour
  assert.match(merge, /\[FACT_DRAFT_HAND_MADE\]: t\("mergeHandMade"\)/);
  const msgs = read("../app/admin/_components/tourDraft.messages.ts");
  for (const key of ["factHandMade", "mergeHandMade"]) {
    const lines = msgs.split("\n").filter(l => l.trim().startsWith(`${key}:`));
    assert.equal(lines.length, 2, `${key} in EN and VI`);
    for (const l of lines) assert.doesNotMatch(l, /AA-\d+/);       // no issue ids in UI text
  }
});

test("Duplicate Review offers only Bypass and Create a new tour when the match was made by hand", async () => {
  const { decisionLabels } = await import("../app/admin/upload/stagingDecisions.ts");
  assert.deepEqual(Object.keys(decisionLabels({ matched_hand_made: true })), ["bypass", "create_new"]);
  // a deleted match keeps its own choices (Restore brings the hand-made tour back)
  assert.deepEqual(Object.keys(decisionLabels({ matched_deleted: true, matched_hand_made: true })),
    ["bypass", "restore", "create_new"]);
  assert.deepEqual(Object.keys(decisionLabels({})), ["bypass", "merge", "replace", "keep_both"]);
  const page = read("../app/admin/upload/page.tsx");
  assert.match(page, /item\.matched_hand_made && \(/);
  const msgs = read("../app/admin/upload/messages.ts");
  const lines = msgs.split("\n").filter(l => l.trim().startsWith("handMadeMatch:"));
  assert.equal(lines.length, 2, "handMadeMatch in EN and VI");
  for (const l of lines) assert.doesNotMatch(l, /AA-\d+/);
});
