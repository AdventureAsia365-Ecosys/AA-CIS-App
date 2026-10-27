// frontend/lib/writingMemoryGuide.test.mjs
// AA-919: the Writing memory Guide (app/admin/writing-memory/guide.ts) keeps its outline, its header buttons and the
// facts that were corrected against the code. The shape, English-only text and page-label checks every guide shares
// are in guides.test.mjs. Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import { WRITING_MEMORY_GUIDE as guide } from "../app/admin/writing-memory/guide.ts";
import { guideControlLabels, guideUiLabels } from "../app/_kit/guideContent.ts";
import { LINT_LABEL } from "./writingMemory.ts";

const text = JSON.stringify(guide);
const section = (id) => guide.sections.find((s) => s.id === id);

test("six sections in order, then the glossary", () => {
  assert.deepEqual(guide.sections.map((s) => s.id), ["purpose", "loop", "tabs", "labels", "routine", "not-in-ui"]);
  for (const t of ["Signal", "Cluster", "Rule", "Candidate", "R0", "L0", "Release", "Golden set", "Memory switch",
    "Scope"]) assert.ok(guide.glossary.some((g) => g.term === t), t);
});

test("the loop diagram and the numbered steps under it name the same 6 steps", () => {
  const loop = section("loop");
  const flow = loop.blocks.find((b) => b.kind === "flow");
  const list = loop.blocks.find((b) => b.kind === "list" && b.ordered);
  assert.ok(flow, "loop has a flow");
  assert.ok(list, "loop has a numbered list");
  const n = Number(/in (\d+) steps/.exec(loop.heading)?.[1]);
  assert.equal(flow.steps.length, n, "the heading counts the diagram's boxes");
  assert.equal(list.items.length, n, "the heading counts the numbered steps");
  // Each numbered step opens with the name of its box ("Capture (edits…)" → "Capture. …").
  flow.steps.forEach((step, i) => {
    const name = step.replace(/\s*\(.*$/, "");
    assert.ok(list.items[i].startsWith(name), `step ${i + 1}: "${name}"`);
  });
  assert.match(flow.steps[0], /edits/);
  assert.match(flow.steps.at(-1), /Go live/);
});

test("no dated snapshot in the guide: it is shown in the product and would go stale", () => {
  assert.doesNotMatch(text, /\b\d{1,2}\/\d{1,2}\/20\d\d\b/, "no dd/mm/yyyy date");
  assert.doesNotMatch(text, /State on/);
});

test("plain words: no HTTP codes or field names the page does not show", () => {
  for (const re of [/\b(409|422)\b/, /no_previous/, /applies_to/, /max\(2/])
    assert.doesNotMatch(text, re, String(re));
});

test("one control card per header button, with the page's labels", () => {
  assert.deepEqual(guideControlLabels(guide),
    ["Backfill (dry run)", "Backfill signals", "Run fact filter", "R0 baseline", "Mine rules"]);
  const labels = guideUiLabels(guide);
  for (const l of Object.values(LINT_LABEL)) assert.ok(labels.includes(l), `lint badge ${l}`);
  for (const l of ["has a rule", "next run drafts it", "needs 3 signals", "L0 passed", "L0 failed",
    "Texts it would newly flag"]) assert.ok(labels.includes(l), l);
});

test("the facts corrected against the code stay corrected", () => {
  assert.doesNotMatch(text, /Would fire on/, "R0 counts texts, not pieces");
  assert.match(text, /NONE/, "a paid draft can store nothing");
  assert.match(text, /\$2 per day for mining/, "the day cap is mining only");
  assert.match(text, /approved and active rules of one writer/, "the token cap counts approved and active rules");
  assert.match(text, /rollback drill/, "the drill approves as an agent too");
  assert.match(text, /'L0 passed' alone does not enable Activate/);
  assert.match(text, /nothing to prove/, "the most common L0 failure is listed");
  assert.match(text, /For an A1 or T2 rule it reads the signals only/);
  assert.match(text, /reason-only signals/, "thresholds count human signals");
  assert.match(text, /A tenant rule names no other tenant's brand or forbidden word/);
  assert.match(text, /model id, not a person/);
});
