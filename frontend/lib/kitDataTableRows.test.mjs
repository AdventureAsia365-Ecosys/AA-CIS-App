// frontend/lib/kitDataTableRows.test.mjs
// AA-1008 (S6f, root ADR 0016 decision 7) — the kit DataTable's row hooks (app/_kit/core/DataTable.tsx): `rowTestId`
// names each row (AA-Partner's trip ledger `line-<id>`, which replaced its raw <table>), every row keeps the kit's own
// `data-kit-row` hook, and without `rowTestId` the rows stay "kit-datatable-row" (the smoke helpers count those).
// Run: npm run test:unit. Same harness as kitModal.test.mjs (transpiled core, react-dom/server).

import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createRequire } from "node:module";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const CORE = join(FRONTEND, "app", "_kit", "core");
const require = createRequire(join(FRONTEND, "package.json"));
const ts = require("typescript");

let dir;
let D; // DataTable module
let S; // dataTableSearch module (S280)
let React;
let render;

before(async () => {
  dir = mkdtempSync(join(FRONTEND, ".kit-dtrows-"));
  for (const name of readdirSync(CORE).filter((n) => /\.(ts|tsx)$/.test(n))) {
    const src = readFileSync(join(CORE, name), "utf8");
    const out = ts.transpileModule(src, {
      compilerOptions: {
        module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
        verbatimModuleSyntax: false, isolatedModules: true,
      },
      fileName: name,
    }).outputText.replace(/(from\s+|import\s*\(\s*|import\s+)(["'])(\.\/[^"']+)\2/g, "$1$2$3.mjs$2");
    writeFileSync(join(dir, name.replace(/\.(ts|tsx)$/, ".mjs")), out);
  }
  D = await import(pathToFileURL(join(dir, "DataTable.mjs")).href);
  S = await import(pathToFileURL(join(dir, "dataTableSearch.mjs")).href);
  React = require("react");
  ({ renderToStaticMarkup: render } = require("react-dom/server"));
});

after(() => {
  if (dir) rmSync(dir, { recursive: true, force: true });
});

const h = (...a) => React.createElement(...a);
const ROWS = [{ id: "a1", name: "Alpha" }, { id: "b2", name: "Beta" }];
const COLUMNS = [{ id: "name", header: "Name", accessorFn: (r) => r.name }];

function table(extra = {}) {
  return render(h(D.DataTable, { data: ROWS, columns: COLUMNS, tableId: "t", getRowId: (r) => r.id, searchable: false, ...extra }));
}

test("rowTestId names every row; data-kit-row stays on each", () => {
  const html = table({ rowTestId: (r) => `line-${r.id}` });
  assert.match(html, /<tr data-testid="line-a1" data-kit-row=""/);
  assert.match(html, /<tr data-testid="line-b2" data-kit-row=""/);
  assert.doesNotMatch(html, /kit-datatable-row/);
});

test("without rowTestId the rows keep the kit test id", () => {
  const html = table();
  assert.equal((html.match(/data-testid="kit-datatable-row" data-kit-row=""/g) ?? []).length, 2);
});

// S280 (AA-1096): the search box has an accessible name; `searchText` lets it match what the cells do not show (a
// tenant slug, a request id); `initialSearch` opens the table filtered.

test("the search box is named for a screen reader", () => {
  const html = table({ searchable: true });
  assert.match(html, /<input[^>]*aria-label="Search this table"/);
  assert.match(table({ searchable: true, searchLabel: "Search requests" }), /aria-label="Search requests"/);
});

test("searchText matches values the cells do not show", () => {
  const rows = (html) => (html.match(/data-kit-row=""/g) ?? []).length;
  const extra = (r) => (r.id === "b2" ? "beta-slug req-42" : "alpha-slug");
  assert.equal(rows(table({ searchable: true, initialSearch: "req-42", searchText: extra })), 1);
  assert.equal(rows(table({ searchable: true, initialSearch: "beta", searchText: extra })), 1);
  assert.equal(rows(table({ searchable: true, initialSearch: "req-42" })), 0);   // without it the slug is not searched
  assert.equal(rows(table({ searchable: true, initialSearch: "", searchText: extra })), 2);
});

test("dataTableSearchMatch: extra text or a text/number cell, case-insensitive", () => {
  assert.equal(S.dataTableSearchMatch("Lotus lotus-trails", "x", "LOTUS-T"), true);
  assert.equal(S.dataTableSearchMatch(null, 42, "4"), true);
  assert.equal(S.dataTableSearchMatch(null, { a: 1 }, "a"), false);
  assert.equal(S.dataTableSearchMatch(undefined, undefined, "  "), true);
});

test("searchFilterState keys the filter on the rows' search texts, by value", () => {
  const extra = (r) => r.slug;
  const rows = [{ slug: "lotus" }, { slug: "mekong" }];
  assert.equal(S.searchFilterState(rows, undefined, "lo"), "lo");          // no searchText: the plain query
  assert.equal(S.searchFilterState(rows, extra, "  "), "  ");              // no query: nothing to key
  const a = S.searchFilterState(rows, extra, "lo");
  assert.equal(a, S.searchFilterState(rows.map((r) => ({ ...r })), extra, "lo"));   // same texts: same state
  assert.notEqual(a, S.searchFilterState([{ slug: "lotus-trails" }, { slug: "mekong" }], extra, "lo"));
  assert.equal(S.searchQueryOf(a), "lo");
  assert.equal(S.searchQueryOf("plain"), "plain");
  assert.equal(S.searchQueryOf(undefined), "");
  assert.equal(S.dataTableSearchMatch("lotus", "x", a), true);            // the match reads the query only
  assert.equal(S.dataTableSearchMatch("mekong", "x", a), false);
});

test("a row text that changes after the query re-runs the search (a lookup loaded late)", () => {
  const rows = (html) => (html.match(/data-kit-row=""/g) ?? []).length;
  const before = (r) => r.id;                                   // the tenant map has not loaded yet
  const after = (r) => (r.id === "b2" ? "b2 lotus-trails" : r.id);
  assert.equal(rows(table({ searchable: true, initialSearch: "lotus", searchText: before })), 0);
  assert.equal(rows(table({ searchable: true, initialSearch: "lotus", searchText: after })), 1);
});
