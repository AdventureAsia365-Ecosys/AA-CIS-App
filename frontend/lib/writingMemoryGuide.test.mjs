// frontend/lib/writingMemoryGuide.test.mjs
// AA-919: the Writing memory Guide content (writingMemoryGuide.ts) stays English-only, complete and in step with the
// page. Run: npm run test:unit (node --test with --experimental-strip-types; the guide module holds only type-level TS).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { GUIDE_LOCALE, WRITING_MEMORY_GUIDE as guide } from "./writingMemoryGuide.ts";
import { LINT_LABEL } from "./writingMemory.ts";

const blocks = () => guide.sections.flatMap((s) => s.blocks);
const text = JSON.stringify(guide);

test("one English locale, the seven sections in order", () => {
  assert.equal(GUIDE_LOCALE, "en");
  assert.equal(guide.locale, "en");
  assert.deepEqual(guide.sections.map((s) => s.id),
    ["purpose", "loop", "tabs", "labels", "routine", "not-in-ui", "glossary"]);
  assert.doesNotMatch(text.replaceAll("Nghiệp", ""), /[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]/i, "no Vietnamese text");
});

test("every block is well formed", () => {
  for (const b of blocks()) {
    if (b.type === "list") assert.ok(b.items.length > 0);
    if (b.type === "flow") assert.ok(b.steps.length >= 2);
    if (b.type === "terms") for (const t of b.items) assert.ok(t.term && t.text, t.term);
    if (b.type === "action") for (const k of ["label", "does", "cost", "when"]) assert.ok(b[k], `${b.label} ${k}`);
  }
});

test("the loop opens with a diagram from edits to the writers", () => {
  const flow = guide.sections.find((s) => s.id === "loop").blocks.find((b) => b.type === "flow");
  assert.ok(flow, "loop has a flow");
  assert.match(flow.steps[0], /edits/);
  assert.match(flow.steps.at(-1), /Writers/);
});

test("the guide names the page's own labels", () => {
  const page = readFileSync(new URL("../app/admin/writing-memory/WritingMemory.tsx", import.meta.url), "utf8");
  for (const label of Object.values(LINT_LABEL)) assert.ok(text.includes(label), label);
  for (const label of ["has a rule", "next run drafts it", "needs 3 signals", "Activate (L0)", "Run R0", "Roll back",
    "Save and lint", "Not activatable yet", "recurs (R2 eligible)", "under k",
    "stale: the rule changed after this run", "Clusters over threshold", "Learning-loop spend this month",
    "Next automatic run", "Refresh"]) {
    assert.ok(page.includes(label), `page lost ${label}`);
    assert.ok(text.includes(label), `guide lacks ${label}`);
  }
  const buttons = blocks().filter((b) => b.type === "action").map((b) => b.label);
  for (const b of buttons) assert.ok(page.includes(b), `no header button ${b}`);
  assert.deepEqual(buttons, ["Backfill (dry run)", "Backfill signals", "Run fact filter", "R0 baseline", "Mine rules"]);
});

test("the glossary covers the core terms", () => {
  const terms = guide.sections.find((s) => s.id === "glossary").blocks.flatMap((b) => b.items.map((t) => t.term));
  for (const t of ["Signal", "Cluster", "Rule", "Candidate", "R0", "L0", "Release", "Golden set", "Memory switch",
    "Scope"]) assert.ok(terms.includes(t), t);
});

test("no ids or account numbers in a public repo", () => {
  assert.doesNotMatch(text, /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
  assert.doesNotMatch(text, /\b\d{12}\b/);
  assert.doesNotMatch(text, /arn:aws/);
});
