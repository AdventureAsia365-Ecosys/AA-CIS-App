// frontend/lib/kitDateField.test.mjs
// AA-1135 follow-up (S284 live check) — the kit core DateField (app/_kit/core/DateField.tsx) keeps its text box inside
// its wrapper. A flex item keeps its intrinsic width by default (min-width auto), so a field in a 150 px wrapper drew
// a 350 px box over the button next to it (Platform › Delivery custom range: elementFromPoint at Apply was the "To"
// box). Run: npm run test:unit.
//
// Like kitScrollHints.test.mjs, the core TSX is transpiled with the TypeScript compiler into a temporary folder inside
// frontend/ and rendered with react-dom/server. Checks:
//   1. the wrapper is a flex box with min-width 0 and the text box may shrink to it (min-width 0);
//   2. a page's own style still wins (width, min-width), and the calendar button keeps its room (padding-right);
//   3. the Delivery range picker gives both boxes the kit control look at the wrapper's width.

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
let F; // DateField module
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
  F = await import(pathToFileURL(join(dir, "DateField.mjs")).href);
  React = require("react");
  ({ renderToStaticMarkup: render } = require("react-dom/server"));
});

after(() => {
  if (dir) rmSync(dir, { recursive: true, force: true });
});

const field = (props) => render(React.createElement(F.DateField, { value: "2026-11-05", onChange: () => {}, ...props }));

/** The style attribute of the visible text box (the first input; the second is the hidden native picker). */
function textBoxStyle(html) {
  const m = html.match(/<input[^>]*type="text"[^>]*style="([^"]*)"/);
  assert.ok(m, `no text box in ${html}`);
  return m[1];
}

test("the text box may shrink to a narrower wrapper", () => {
  const html = field({ "aria-label": "To", wrapperStyle: { width: 150 } });
  assert.match(html, /data-kit-field="date"[^>]*style="[^"]*display:flex;min-width:0;width:150px/);
  assert.match(textBoxStyle(html), /(^|;)min-width:0(;|$)/);
  assert.match(textBoxStyle(html), /padding-right:34px/);
});

test("a page's own style still wins", () => {
  const style = textBoxStyle(field({ style: { width: 132, minWidth: 120, height: 36 } }));
  assert.match(style, /min-width:120px/);
  assert.doesNotMatch(style, /min-width:0/);
  assert.match(style, /width:132px/);
  assert.match(style, /padding-right:34px/);
});

test("the Delivery range picker sizes both boxes to their wrapper with the kit control look", () => {
  const src = readFileSync(join(FRONTEND, "app", "admin", "platform", "delivery", "RangeCharts.tsx"), "utf8");
  assert.match(src, /const DATE_BOX = \{ \.\.\.controlStyle\("sm"\), width: "100%" \}/);
  const fields = src.match(/<DateField [^>]*\/>/gs) ?? [];
  assert.equal(fields.length, 2);
  for (const f of fields) {
    assert.match(f, /style=\{DATE_BOX\}/);
    assert.match(f, /wrapperStyle=\{\{ width: 150 \}\}/);
  }
});
