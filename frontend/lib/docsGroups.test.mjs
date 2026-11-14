// frontend/lib/docsGroups.test.mjs
// AA-1104: unit tests for the numbered, collapsible groups of the docs reader list (invented paths).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  defaultOpenGroups,
  filterGroups,
  numberGroups,
  openSections,
  parseOpenGroups,
  sectionOfPath,
  serializeOpenGroups,
  toggleGroup,
  withOpenGroup,
} from "./docsGroups.ts";

const page = (path, over = {}) => ({
  repo: "AA-Ecosys", path, id: "X-01", title: path, description: null, type: "how-to", status: "active",
  audience: ["engineer"], owner: null, last_reviewed: null, generated: false, access: "staff", bytes: 1,
  ...over,
});

const catalog = [
  page("docs/ops/runbooks/b.md"),
  page("docs/glossary.md"),
  page("docs/adr/0002-y.md"),
  page("docs/ops/runbooks/a.md"),
  page("docs/adr/0001-x.md"),
  page("docs/README.md"),
];

test("numberGroups: group n and page n.m in the list order (path order, as groupBySection sorts)", () => {
  const g = numberGroups(catalog);
  assert.deepEqual(g.map((x) => [x.number, x.section]), [[1, "docs"], [2, "adr"], [3, "ops/runbooks"]]);
  assert.deepEqual(g.map((x) => x.pages.map((p) => [p.number, p.doc.path])), [
    [["1.1", "docs/glossary.md"], ["1.2", "docs/README.md"]],
    [["2.1", "docs/adr/0001-x.md"], ["2.2", "docs/adr/0002-y.md"]],
    [["3.1", "docs/ops/runbooks/a.md"], ["3.2", "docs/ops/runbooks/b.md"]],
  ]);
  // The input order does not matter: the same catalog gets the same numbers.
  assert.deepEqual(numberGroups([...catalog].reverse()), g);
  assert.deepEqual(numberGroups([]), []);
});

test("filterGroups keeps the numbers and drops empty groups", () => {
  const g = filterGroups(numberGroups(catalog), (d) => d.path.endsWith("b.md") || d.path.includes("0002"));
  assert.deepEqual(g.map((x) => [x.number, x.section, x.pages.map((p) => p.number)]), [
    [2, "adr", ["2.2"]],
    [3, "ops/runbooks", ["3.2"]],
  ]);
});

test("sectionOfPath and defaultOpenGroups: the open page's group, else the first", () => {
  const g = numberGroups(catalog);
  assert.equal(sectionOfPath(g, "docs/adr/0001-x.md"), "adr");
  assert.equal(sectionOfPath(g, "docs/nowhere.md"), null);
  assert.equal(sectionOfPath(g, null), null);
  assert.deepEqual(defaultOpenGroups(g, "adr"), ["adr"]);
  assert.deepEqual(defaultOpenGroups(g, null), ["docs"]);
  assert.deepEqual(defaultOpenGroups(g, "gone"), ["docs"]);
  assert.deepEqual(defaultOpenGroups([], null), []);
});

test("parseOpenGroups / serializeOpenGroups round trip and bad input", () => {
  assert.deepEqual(parseOpenGroups(serializeOpenGroups(["ops", "adr", "ops"])), ["adr", "ops"]);
  assert.equal(parseOpenGroups(null), null);
  assert.equal(parseOpenGroups("not json"), null);
  assert.equal(parseOpenGroups('{"a":1}'), null);
  assert.deepEqual(parseOpenGroups('["a", 2, null, "b"]'), ["a", "b"]);
  assert.deepEqual(parseOpenGroups("[]"), []);
});

test("toggleGroup and withOpenGroup", () => {
  assert.deepEqual(toggleGroup(["a"], "b"), ["a", "b"]);
  assert.deepEqual(toggleGroup(["a", "b"], "a"), ["b"]);
  const open = ["a"];
  assert.equal(withOpenGroup(open, "a"), open);
  assert.deepEqual(withOpenGroup(open, "b"), ["a", "b"]);
});

test("openSections: a search opens every group with a match, minus the ones folded in it", () => {
  const all = numberGroups(catalog);
  const matches = filterGroups(all, (d) => d.path.includes("a"));
  const sections = (s) => [...s].sort();
  // No search: the viewer's groups, whatever matches.
  assert.deepEqual(sections(openSections({ groups: all, open: ["adr"], query: "", foldedInSearch: [] })), ["adr"]);
  assert.deepEqual(sections(openSections({ groups: all, open: [], query: "   ", foldedInSearch: ["adr"] })), []);
  // Search: the groups with matches open on their own, the viewer's closed groups included.
  assert.deepEqual(
    sections(openSections({ groups: matches, open: [], query: "a", foldedInSearch: [] })),
    matches.map((g) => g.section).sort(),
  );
  assert.deepEqual(
    sections(openSections({ groups: matches, open: [], query: "a", foldedInSearch: ["adr"] })),
    matches.map((g) => g.section).filter((s) => s !== "adr").sort(),
  );
});
