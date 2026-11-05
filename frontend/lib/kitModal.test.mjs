// frontend/lib/kitModal.test.mjs
// AA-1008 (S6f, root ADR 0016 decision 7) — the kit core Modal's pinned-footer layout (app/_kit/core/Modal.tsx), the
// long-form dialog AA-Partner's stage engine used to hand-roll (aa-kit/no-raw-dialog). Run: npm run test:unit.
//
// Like kitShellFrame.test.mjs, the core TSX is transpiled with the TypeScript compiler into a temporary folder inside
// frontend/ (so react resolves) and rendered with react-dom/server. Checks:
//   1. MODAL_CSS: a 90vh fallback before the dvh height, the body scrolls and the footer stays, the whole box scrolls
//      under 480 px of window height, child selectors scoped to a pinned box only;
//   2. moreBelow (the cue's measure) with its 4 px tolerance;
//   3. the server markup: a pinned box carries its hooks and no inline height or overflow (the CSS owns them), the body
//      its test id; a plain Modal keeps its inline 90vh box and flex footer; closed renders nothing.

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
let M; // Modal module
let React;
let render;

before(async () => {
  dir = mkdtempSync(join(FRONTEND, ".kit-modal-"));
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
  M = await import(pathToFileURL(join(dir, "Modal.mjs")).href);
  React = require("react");
  ({ renderToStaticMarkup: render } = require("react-dom/server"));
});

after(() => {
  if (dir) rmSync(dir, { recursive: true, force: true });
});

const h = (...a) => React.createElement(...a);
const flat = (css) => css.replace(/\s+/g, " ");

test("MODAL_CSS: a 90vh fallback before dvh, the body scrolls, the whole box under 480 px of height", () => {
  const css = flat(M.MODAL_CSS);
  const box = css.match(/\[data-kit-modal="box"\]\[data-pin-footer\] \{([^}]*)\}/)[1];
  assert.ok(box.includes("max-height: 90vh;"));
  assert.ok(box.indexOf("max-height: 90vh;") < box.indexOf("max-height: min(90vh, calc(100dvh - 32px))"),
    "the fallback comes first, so a browser that drops the dvh declaration keeps 90vh");
  assert.match(box, /display: flex; flex-direction: column; overflow: hidden;/);
  assert.ok(css.includes('[data-kit-modal="box"][data-pin-footer] > [data-kit-modal="body"] { flex: 1 1 auto; min-height: 0; overflow-y: auto; }'));
  assert.match(css, /> \[data-kit-modal="footer"\] \{ flex: none; display: grid;/);
  const short = css.slice(css.indexOf("@media (max-height: 479px)"));
  assert.match(short, /\[data-kit-modal="box"\]\[data-pin-footer\] \{ overflow-y: auto; \}/);
  assert.match(short, /> \[data-kit-modal="body"\] \{ flex: none; overflow-y: visible; \}/);
  // Every rule is scoped to a pinned box: a plain Modal is never restyled.
  for (const rule of css.split("}").map((r) => r.trim()).filter((r) => r.includes("{") && !r.startsWith("@media"))) {
    const selector = rule.slice(0, rule.indexOf("{")).replace(/^@media[^{]*\{\s*/, "");
    if (selector) assert.ok(selector.includes("[data-pin-footer]"), `scoped: ${selector}`);
  }
});

test("moreBelow: more than 4 px under the fold", () => {
  assert.equal(M.moreBelow(null), false);
  assert.equal(M.moreBelow({ scrollHeight: 600, scrollTop: 0, clientHeight: 600 }), false);
  assert.equal(M.moreBelow({ scrollHeight: 604, scrollTop: 0, clientHeight: 600 }), false);
  assert.equal(M.moreBelow({ scrollHeight: 800, scrollTop: 0, clientHeight: 350 }), true);
  assert.equal(M.moreBelow({ scrollHeight: 800, scrollTop: 450, clientHeight: 350 }), false);
});

test("a pinned box: hooks, no inline height or overflow, the body test id; the style element is rendered", () => {
  const html = render(h(M.Modal, {
    open: true, onClose: () => {}, title: "Move", testId: "move", bodyTestId: "move-scroll", pinFooter: true,
    moreLabel: "More below", footer: h("button", null, "Confirm"),
  }, h("p", null, "Body")));
  const box = html.slice(html.indexOf('role="dialog"'), html.indexOf(">", html.indexOf('role="dialog"')));
  assert.match(box, /data-testid="move"/);
  assert.match(box, /data-kit-modal="box"/);
  assert.match(box, /data-pin-footer=""/);
  assert.doesNotMatch(box, /max-height|overflow/);
  assert.match(html, /data-kit-modal="body" data-testid="move-scroll"/);
  assert.match(html, /data-kit-modal="footer"/);
  assert.doesNotMatch(html.slice(html.indexOf('data-kit-modal="footer"')), /^[^>]*display:flex/);
  assert.ok(html.includes("aa-kit-modal"), "the hoisted style element");
  assert.doesNotMatch(html, /kit-modal-more/, "the cue appears only once a measure says there is more");
});

test("a plain Modal keeps its inline box and flex footer; closed renders nothing", () => {
  const html = render(h(M.Modal, { open: true, onClose: () => {}, title: "Delete", footer: h("button", null, "OK") }, "Sure?"));
  assert.match(html, /max-height:90vh;overflow:auto/);
  assert.doesNotMatch(html, /data-pin-footer/);
  assert.doesNotMatch(html, /aa-kit-modal/);
  assert.match(html, /data-kit-modal="footer" style="display:flex;justify-content:flex-end/);
  assert.equal(render(h(M.Modal, { open: false, onClose: () => {}, title: "x" }, "y")), "");
});
