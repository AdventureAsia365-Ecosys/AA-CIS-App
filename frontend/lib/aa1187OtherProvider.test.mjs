// frontend/lib/aa1187OtherProvider.test.mjs
// AA-1187: a re-uploaded row that matched a live supplier tour on Name + country but names another Provider may be
// another operator's tour of the same name. Duplicate Review then offers Create a new tour as well as Merge, says why
// (EN + VI, no issue ids), and a merge shows the Provider as a choice whose default keeps the current value.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

test("Duplicate Review offers Create a new tour when the match names another provider", async () => {
  const { decisionLabels, decisionBody } = await import("../app/admin/upload/stagingDecisions.ts");
  assert.deepEqual(Object.keys(decisionLabels({ matched_other_provider: true })),
    ["bypass", "merge", "create_new", "replace", "keep_both"]);
  assert.equal(decisionLabels({ matched_other_provider: true }).create_new, "decCreateNew");
  // the same provider keeps the old choices
  assert.deepEqual(Object.keys(decisionLabels({ matched_other_provider: false })),
    ["bypass", "merge", "replace", "keep_both"]);
  // a deleted or hand-made match keeps its own choices whatever the provider
  assert.deepEqual(Object.keys(decisionLabels({ matched_deleted: true, matched_other_provider: true })),
    ["bypass", "restore", "create_new"]);
  assert.deepEqual(Object.keys(decisionLabels({ matched_hand_made: true, matched_other_provider: true })),
    ["bypass", "create_new"]);
  // create_new is sent as a plain decision (the API accepts it for a live match)
  assert.deepEqual(decisionBody("create_new", undefined), { decision: "create_new" });
});

test("the note and the merge choice explain the other provider in English and Vietnamese", () => {
  const page = read("../app/admin/upload/page.tsx");
  assert.match(page, /else if \(item\.matched_other_provider\)/);
  assert.match(page, /t\("otherProviderMatch", item\.existing\.provider \|\| "—", item\.incoming\.provider \|\| "—"\)/);
  const msgs = read("../app/admin/upload/messages.ts");
  const lines = msgs.split("\n").filter(l => l.includes("in the catalog: ${was}") || l.includes("trong danh mục: ${was}"));
  assert.equal(lines.length, 2, "otherProviderMatch in EN and VI");
  for (const l of lines) assert.doesNotMatch(l, /AA-\d+/);
  const merge = read("../app/admin/upload/MergePanel.tsx");
  assert.match(merge, /t\(c\.hand_edited \? "keepCurrent" : "keepCurrentValue"\)/);
  const draft = read("../app/admin/_components/tourDraft.messages.ts");
  assert.equal(draft.split("\n").filter(l => l.trim().startsWith("keepCurrentValue:")).length, 2);
});
