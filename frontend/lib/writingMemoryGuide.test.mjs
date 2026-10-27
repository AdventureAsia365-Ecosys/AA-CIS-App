// frontend/lib/writingMemoryGuide.test.mjs
// AA-919: the Writing memory Guide content (writingMemoryGuide.ts) stays bilingual and in step with the page.
// Run: npm run test:unit (node --test with --experimental-strip-types; the guide module holds only type-level TS).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { DEFAULT_GUIDE_LOCALE, GUIDE_LOCALES, WRITING_MEMORY_GUIDE, guideFor } from "./writingMemoryGuide.ts";
import { LINT_LABEL } from "./writingMemory.ts";

const { en, vi } = WRITING_MEMORY_GUIDE;

function shape(block) {
  if (block.type === "list") return `list:${block.ordered ? "ol" : "ul"}:${block.items.length}`;
  if (block.type === "terms") return `terms:${block.items.length}`;
  if (block.type === "note") return `note:${block.tone}:${block.title ? "t" : "-"}`;
  if (block.type === "action") return `action:${block.label}:${block.avoid ? "a" : "-"}`;
  return block.type;
}

function allText(content) {
  return JSON.stringify(content);
}

test("English is the default and both locales exist", () => {
  assert.equal(DEFAULT_GUIDE_LOCALE, "en");
  assert.deepEqual([...GUIDE_LOCALES], ["en", "vi"]);
  assert.equal(guideFor("vi"), vi);
  assert.equal(guideFor("fr"), en);
  assert.equal(guideFor(undefined), en);
});

test("the two locales carry the same sections and block shapes", () => {
  assert.deepEqual(vi.sections.map((s) => s.id), en.sections.map((s) => s.id));
  for (const [i, s] of en.sections.entries()) {
    assert.deepEqual(vi.sections[i].blocks.map(shape), s.blocks.map(shape), s.id);
  }
});

test("UI labels are kept verbatim in both locales (terms of the labels section, button names)", () => {
  const terms = (c) => c.sections.flatMap((s) => s.blocks).filter((b) => b.type === "terms")
    .flatMap((b) => b.items.map((t) => t.term));
  const labelsEn = en.sections.find((s) => s.id === "labels").blocks.filter((b) => b.type === "terms")
    .flatMap((b) => b.items.map((t) => t.term));
  const labelsVi = vi.sections.find((s) => s.id === "labels").blocks.filter((b) => b.type === "terms")
    .flatMap((b) => b.items.map((t) => t.term));
  assert.deepEqual(labelsVi, labelsEn);
  assert.equal(terms(vi).length, terms(en).length);
});

test("the guide names the page's own labels", () => {
  const page = readFileSync(new URL("../app/admin/writing-memory/WritingMemory.tsx", import.meta.url), "utf8");
  const text = allText(en);
  for (const label of Object.values(LINT_LABEL)) assert.ok(text.includes(label), label);
  for (const label of ["has a rule", "next run drafts it", "needs 3 signals", "Activate (L0)", "Run R0", "Roll back",
    "Save and lint", "Not activatable yet", "recurs (R2 eligible)", "under k",
    "stale: the rule changed after this run"]) {
    assert.ok(page.includes(label), `page lost ${label}`);
    assert.ok(text.includes(label), `guide lacks ${label}`);
  }
  const buttons = en.sections.flatMap((s) => s.blocks).filter((b) => b.type === "action").map((b) => b.label);
  for (const b of buttons) assert.ok(page.includes(b), `no header button ${b}`);
  assert.deepEqual(buttons, ["Backfill (dry run)", "Backfill signals", "Run fact filter", "R0 baseline", "Mine rules"]);
});

test("no ids or account numbers in a public repo", () => {
  for (const c of [en, vi]) {
    const text = allText(c);
    assert.doesNotMatch(text, /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    assert.doesNotMatch(text, /\b\d{12}\b/);
    assert.doesNotMatch(text, /arn:aws/);
  }
});
