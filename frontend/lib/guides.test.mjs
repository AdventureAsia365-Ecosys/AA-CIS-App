// frontend/lib/guides.test.mjs
// AA-919: one test for every admin page's Guide tab (the kit pattern: app/_kit/guideContent.ts + GuidePanel).
// Run: npm run test:unit (node --test with --experimental-strip-types; the guide modules hold only type-level TS).
//
// A new page with a Guide tab adds one row to GUIDES. Each guide is checked for: a valid GuideContent shape
// (guideProblems: ids, headings, no empty strings), English text, no ids in a public repo, every page label it
// documents (`uiLabels` terms) and every control it describes still printed by the page, and the page wiring the
// pattern the same way (a "Guide" tab rendered with GuidePanel, plus the "New here? Read the Guide" link).

import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  guideAnchor, guideControlLabels, guideOutline, guideProblems, guideUiLabels,
} from "../app/_kit/guideContent.ts";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const read = (p) => readFileSync(join(FRONTEND, p), "utf8");

/** Every guide: its module and export, the page that renders it, and the files that print its labels. */
const GUIDES = [
  { name: "Writing memory", module: "app/admin/writing-memory/guide.ts", exportName: "WRITING_MEMORY_GUIDE",
    page: "app/admin/writing-memory/WritingMemory.tsx", sources: ["lib/writingMemory.ts"] },
];

// ── the shape check itself ──────────────────────────────────────────────────────────────────────────────────────

const ok = {
  title: "How to use X",
  sections: [
    { id: "purpose", heading: "What it is for", blocks: [{ kind: "paragraph", text: "Text." }] },
    { id: "labels", heading: "Labels", blocks: [{ kind: "terms", uiLabels: true, items: [
      { term: "L0 passed / L0 failed", text: "Meaning." }, { term: "active", text: "Meaning." }] }] },
  ],
  glossary: [{ term: "Rule", text: "A rule." }],
};

test("guideProblems accepts a well formed guide", () => {
  assert.deepEqual(guideProblems(ok), []);
});

test("guideProblems names each defect", () => {
  const bad = {
    title: " ",
    sections: [
      { id: "Bad Id", heading: "", blocks: [] },
      { id: "dup", heading: "A", blocks: [{ kind: "list", items: [] }] },
      { id: "dup", heading: "B", blocks: [
        { kind: "paragraph", text: "" },
        { kind: "callout", tone: "warning", title: "", text: "x" },
        { kind: "flow", steps: ["only one"] },
        { kind: "control", label: "Run", does: "" },
        { kind: "terms", items: [{ term: "a", text: "" }, { term: "A", text: "x" }] },
      ] },
      { id: "glossary", heading: "Clash", blocks: [{ kind: "paragraph", text: "x" }] },
    ],
    glossary: [],
  };
  const p = guideProblems(bad).join("\n");
  for (const m of ["no title", "not lower-case-with-dashes", "no heading", "no block", "used twice", "no items",
    "empty text", "empty title", "at least 2 steps", "says nothing", "has no text", "\"A\" twice",
    "kept for the glossary", "glossary: no terms"]) assert.match(p, new RegExp(m.replace(/[()]/g, "\\$&")), m);
});

test("outline numbers the sections and puts the glossary last; labels split on ' / '", () => {
  assert.deepEqual(guideOutline(ok).map((o) => [o.id, o.n]), [["purpose", 1], ["labels", 2], ["glossary", 3]]);
  assert.deepEqual(guideUiLabels(ok), ["L0 passed", "L0 failed", "active"]);
  assert.equal(guideAnchor("labels"), "guide-labels");
});

// ── every registered guide ──────────────────────────────────────────────────────────────────────────────────────

for (const g of GUIDES) {
  test(`${g.name} guide: shape, English, no ids`, async () => {
    const guide = (await import(`../${g.module}`))[g.exportName];
    assert.ok(guide, `${g.module} exports ${g.exportName}`);
    assert.deepEqual(guideProblems(guide), []);
    const text = JSON.stringify(guide);
    assert.doesNotMatch(text.replaceAll("Nghiệp", ""), /[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]/i,
      "English only");
    assert.doesNotMatch(text, /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i, "no UUID");
    assert.doesNotMatch(text, /\b\d{12}\b/, "no account number");
    assert.doesNotMatch(text, /arn:aws/, "no ARN");
  });

  test(`${g.name} guide: every documented label and control is still on the page`, async () => {
    const guide = (await import(`../${g.module}`))[g.exportName];
    const code = [g.page, ...g.sources].map(read).join("\n");
    const labels = guideUiLabels(guide);
    assert.ok(labels.length > 0, "the guide documents the page's labels in uiLabels blocks");
    for (const l of labels) assert.ok(code.includes(l), `the page no longer prints "${l}"`);
    for (const c of guideControlLabels(guide)) assert.ok(code.includes(c), `the page has no control "${c}"`);
  });

  test(`${g.name} page wires the Guide tab the kit way`, () => {
    const page = read(g.page);
    assert.match(page, /\{ key: "guide", label: "Guide" \}/, "a Guide tab");
    assert.match(page, /<GuidePanel content=\{/, "rendered with the kit GuidePanel");
    assert.ok(page.includes("New here? Read the Guide"), "the description links to the Guide");
  });
}

test("every page that renders a GuidePanel is registered here", () => {
  const found = [];
  const walk = (dir) => {
    for (const f of readdirSync(dir)) {
      const p = join(dir, f);
      if (statSync(p).isDirectory()) walk(p);
      else if (/\.tsx$/.test(f) && readFileSync(p, "utf8").includes("<GuidePanel")) found.push(relative(FRONTEND, p));
    }
  };
  walk(join(FRONTEND, "app"));
  const pages = found.filter((p) => !p.startsWith("app/_kit/") && !p.endsWith("SettingsKitTab.tsx"));
  assert.deepEqual(pages.sort(), GUIDES.map((g) => g.page).sort());
});
