// frontend/lib/kitScrollHints.test.mjs
// S262 (AA-804 live findings F2 / F3 / F7) — the kit core's sideways-scroll hints. Run: npm run test:unit.
//
// Like kitTimeline.test.mjs, the core TSX is transpiled with the TypeScript compiler into a temporary folder inside
// frontend/ and rendered with react-dom/server. Checks:
//   1. tabStripMask: no mask when every tab fits, a fade on the edge that has more tabs;
//   2. scrollToShow: the active tab comes into view with a margin, nothing moves when it is already visible;
//   3. Tabs (both variants) mark the active tab for the strip (data-kit-tab-active) and keep their test ids / roles;
//   4. DataTable renders no scroll hint on the server (it appears only after a real overflow measurement) and its
//      scroll box says it does not overflow yet.

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
let P; // primitives module
let D; // DataTable module
let React;
let render;

before(async () => {
  dir = mkdtempSync(join(FRONTEND, ".kit-render-"));
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
  P = await import(pathToFileURL(join(dir, "primitives.mjs")).href);
  D = await import(pathToFileURL(join(dir, "DataTable.mjs")).href);
  React = require("react");
  ({ renderToStaticMarkup: render } = require("react-dom/server"));
});

after(() => {
  if (dir) rmSync(dir, { recursive: true, force: true });
});

const h = (...a) => React.createElement(...a);

test("tabStripMask fades only the edge with more tabs", () => {
  assert.equal(P.tabStripMask(false, false), "none");
  assert.equal(P.tabStripMask(false, true), "linear-gradient(to right, #000 0, #000 calc(100% - 28px), transparent 100%)");
  assert.equal(P.tabStripMask(true, false), "linear-gradient(to right, transparent 0, #000 28px, #000 100%)");
  assert.match(P.tabStripMask(true, true), /^linear-gradient\(to right, transparent 0, #000 28px, #000 calc/);
});

test("scrollToShow brings the active tab into view, leaves a visible one alone", () => {
  // box 300 wide at scrollLeft 0; a tab at 400..480 -> scroll so its right edge + margin is visible
  assert.equal(P.scrollToShow(0, 300, 400, 80), 400 + 80 + 28 - 300);
  // scrolled to 200; a tab at 50 -> back to 50 - margin
  assert.equal(P.scrollToShow(200, 300, 50, 80), 22);
  assert.equal(P.scrollToShow(200, 300, 10, 80), 0);
  // visible with room for the fade -> null
  assert.equal(P.scrollToShow(0, 300, 100, 80), null);
  assert.equal(P.scrollToShow(0, 300, 100, 80, 0), null);
});

test("Tabs mark the active tab and keep their markup", () => {
  const tabs = [{ key: "a", label: "Alpha" }, { key: "b", label: "Beta" }];
  const track = render(h(P.Tabs, { tabs, active: "b", onChange: () => {}, testId: "strip" }));
  assert.match(track, /data-testid="strip"/);
  assert.equal((track.match(/data-kit-tab-active="true"/g) ?? []).length, 1);
  assert.match(track, /data-kit-tab-active="true"[^>]*>Beta</);
  assert.doesNotMatch(track, /mask-image/);   // nothing measured on the server: no fade
  const underline = render(h(P.Tabs, { tabs, active: "a", onChange: () => {}, variant: "underline", ariaLabel: "x" }));
  assert.match(underline, /role="tablist"/);
  assert.match(underline, /aria-selected="true" data-kit-tab-active="true"/);
});

test("DataTable: no scroll hint before a measurement; the box is not focusable", () => {
  const html = render(h(D.DataTable, {
    data: [{ id: "1", a: "one" }], columns: [{ accessorKey: "a", header: "A" }], tableId: "t",
    getRowId: (r) => r.id, searchable: false, paginate: false, tableMinWidth: 2000,
  }));
  assert.match(html, /data-overflows="false"/);
  assert.doesNotMatch(html, /kit-datatable-scroll-hint/);
  assert.doesNotMatch(html, /data-kit-scroll="" [^>]*tabindex/);
});
