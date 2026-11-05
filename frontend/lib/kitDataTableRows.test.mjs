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
