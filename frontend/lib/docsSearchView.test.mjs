// frontend/lib/docsSearchView.test.mjs
// AA-1133 follow-up (S284 live check): the docs search hit list (lib/docsSearchView.ts): the reader's language first
// inside a page / translation pair, nothing dropped, and no bare-heading section rows. Invented pages only.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import { pairKey, rankByLanguage, sectionsWithText } from "./docsSearchView.ts";

const hit = (path, lang) => ({ path, lang });
const paths = (list) => list.map((r) => r.path);

const SERVER_ORDER = [
  hit("docs/guides/lantern.vi.md", "vi"),
  hit("docs/guides/ferry.md", "en"),
  hit("docs/guides/lantern.md", "en"),
  hit("docs/guides/ferry.vi.md", "vi"),
  hit("docs/guides/kite.vi.md", "vi"),
];

test("pairKey: a translation pairs with its original", () => {
  assert.equal(pairKey("docs/guides/lantern.vi.md"), "docs/guides/lantern.md");
  assert.equal(pairKey("docs/guides/lantern.md"), "docs/guides/lantern.md");
  assert.equal(pairKey("docs/guides/v1.2.md"), "docs/guides/v1.2.md");
});

test("rankByLanguage: an English reader gets the English copy above its Vietnamese copy", () => {
  assert.deepEqual(paths(rankByLanguage(SERVER_ORDER, "en")), [
    "docs/guides/lantern.md", "docs/guides/lantern.vi.md",
    "docs/guides/ferry.md", "docs/guides/ferry.vi.md",
    "docs/guides/kite.vi.md",
  ]);
});

test("rankByLanguage: a Vietnamese reader gets the reverse; pairs keep their best place", () => {
  assert.deepEqual(paths(rankByLanguage(SERVER_ORDER, "vi")), [
    "docs/guides/lantern.vi.md", "docs/guides/lantern.md",
    "docs/guides/ferry.vi.md", "docs/guides/ferry.md",
    "docs/guides/kite.vi.md",
  ]);
});

test("rankByLanguage: nothing is dropped and the input is not changed", () => {
  const input = [...SERVER_ORDER];
  const out = rankByLanguage(input, "en");
  assert.equal(out.length, SERVER_ORDER.length);
  assert.deepEqual(new Set(paths(out)), new Set(paths(SERVER_ORDER)));
  assert.deepEqual(input, SERVER_ORDER);
  assert.deepEqual(rankByLanguage([], "en"), []);
});

test("sectionsWithText: a section without snippet words gets no row", () => {
  const sections = [
    { heading: "Opening hours", slug: "opening-hours", snippet: [{ text: "Stalls open at ", hit: false }, { text: "dusk", hit: true }] },
    { heading: "Prices", slug: "prices", snippet: [] },
    { heading: "Ferry", slug: "ferry", snippet: [{ text: "   ", hit: false }] },
    { heading: "", slug: "", snippet: [{ text: "Lantern", hit: true }] },
  ];
  assert.deepEqual(sectionsWithText(sections).map((s) => s.slug), ["opening-hours", ""]);
  assert.deepEqual(sectionsWithText([]), []);
});
