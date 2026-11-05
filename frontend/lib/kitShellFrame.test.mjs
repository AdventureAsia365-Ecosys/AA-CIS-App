// frontend/lib/kitShellFrame.test.mjs
// AA-1008 (S6e, root ADR 0016 decision 1) — the kit core ShellFrame (app/_kit/core/ShellFrame.tsx), the frame every
// app shell configures (CIS AdminShell, Booking AdminShell, the AA-Partner CRM and Content studio shells). Run: npm run
// test:unit.
//
// Like kitNavGroup.test.mjs, the core TSX is transpiled with the TypeScript compiler into a temporary folder inside
// frontend/ (so react resolves) and rendered with react-dom/server. Checks:
//   1. the drawer CSS: one block per breakpoint (767 / 899 px), slid out and hidden by default, in while the frame
//      says data-drawer="open", the backdrop only below the breakpoint, the sticky sidebar of the document scroll;
//   2. the palette shortcut (⌘K / Ctrl K, any case, never a bare K);
//   3. the server markup: frame hooks (breakpoint, scroll, drawer closed), the sidebar wrapper id, slot order
//      (topbar, banner, main, footer in the column; bottom bar and palette after it), the slots get the controls;
//   4. main="shell" renders one <main id>, main="page" none (the page's PageFrame is the <main>);
//   5. the scroll models (one screen high column vs min-height + document scroll), embedded = the parent's height;
//   6. the CIS AdminShell is a configuration of it (no keydown listener or drawer state of its own).

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
let S; // ShellFrame module
let React;
let render;

before(async () => {
  dir = mkdtempSync(join(FRONTEND, ".kit-shellframe-"));
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
  S = await import(pathToFileURL(join(dir, "ShellFrame.mjs")).href);
  React = require("react");
  ({ renderToStaticMarkup: render } = require("react-dom/server"));
});

after(() => {
  if (dir) rmSync(dir, { recursive: true, force: true });
});

const h = (...a) => React.createElement(...a);

function shell(props, children = h("p", null, "page")) {
  return render(h(S.ShellFrame, props, children));
}

test("the drawer CSS has one block per breakpoint: out of view and hidden, in while the frame says open", () => {
  const css = S.SHELL_CSS;
  for (const bp of [768, 900]) {
    const media = `@media (max-width: ${bp - 1}px)`;
    const at = css.indexOf(media);
    assert.ok(at >= 0, `a block below ${bp}px`);
    const block = css.slice(at, css.indexOf("\n}", at));
    const f = `[data-kit-shell="frame"][data-kit-shell-bp="${bp}"]`;
    assert.match(block, new RegExp(`${escape(f)} > \\[data-kit-shell="sidebar"\\] \\{[^}]*position: fixed`));
    assert.match(block, /transform: translateX\(-100%\); visibility: hidden;/);
    assert.ok(block.includes(`${f}[data-drawer="open"] > [data-kit-shell="sidebar"] { transform: translateX(0); visibility: visible; }`));
    assert.ok(block.includes(`${f} > [data-kit-shell="backdrop"] { display: block; }`));
    // The slot fills the drawer (100dvh), never a 100vh of its own inline style (the large viewport on a phone,
    // whose last rows would sit under the browser toolbar); !important beats the inline height.
    assert.ok(block.includes(`${f} > [data-kit-shell="sidebar"] > * { height: 100% !important; max-height: 100%; }`));
  }
  // The backdrop is hidden outside the phone blocks (a drawer opened on a phone, then the window widened).
  assert.match(css.slice(0, css.indexOf("@media")), /\[data-kit-shell="backdrop"\] \{ display: none;/);
  // Document scroll: the sidebar stays in view.
  assert.match(css, /\[data-kit-shell-scroll="document"\] > \[data-kit-shell="sidebar"\] \{\s*position: sticky; top: 0; height: 100vh;/);
  assert.match(css, /prefers-reduced-motion: reduce\)[^}]*transition: none/);
  // Child selectors only, so a shell nested in a page (the gallery demo) never styles the outer one.
  assert.ok(!/\[data-kit-shell="frame"\][^>{]*\s\[data-kit-shell=/.test(css.replace(/\[data-drawer="open"\]/g, "")));
});

function escape(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

test("the palette shortcut is the modifier + K, in either case, never a bare K", () => {
  assert.equal(S.isPaletteShortcut({ key: "k", metaKey: true, ctrlKey: false }), true);
  assert.equal(S.isPaletteShortcut({ key: "K", metaKey: false, ctrlKey: true }), true);
  assert.equal(S.isPaletteShortcut({ key: "k", metaKey: false, ctrlKey: false }), false);
  assert.equal(S.isPaletteShortcut({ key: "j", metaKey: true, ctrlKey: false }), false);
});

test("the server markup: frame hooks, sidebar wrapper, slot order, the slots get the controls", () => {
  const seen = [];
  const html = shell({
    drawerBelow: 900,
    sidebarId: "crm-sidebar",
    testId: "shell",
    frameProps: { "data-crm": "frame" },
    sidebar: (c) => {
      seen.push(c);
      return h("nav", { "data-x": "sidebar" }, "nav");
    },
    topbar: (c) => h("header", { "aria-controls": c.sidebarId, "aria-expanded": c.drawerOpen }, "top"),
    banner: h("div", { "data-x": "banner" }),
    footer: h("footer", null, "terms"),
    bottomNav: h("nav", { "data-x": "bottom" }),
    palette: (c) => (c.paletteOpen ? h("div", { "data-x": "palette-open" }) : h("div", { "data-x": "palette-closed" })),
  });
  assert.match(html, /data-kit-shell="frame"/);
  assert.match(html, /data-kit-shell-bp="900"/);
  assert.match(html, /data-kit-shell-scroll="main"/);
  assert.match(html, /data-drawer="closed"/);
  assert.match(html, /data-crm="frame"/);
  assert.match(html, /data-testid="shell"/);
  assert.match(html, /<div data-kit-shell="sidebar" id="crm-sidebar"><nav data-x="sidebar">/);
  assert.match(html, /aria-controls="crm-sidebar" aria-expanded="false"/);
  assert.ok(!html.includes('<div data-kit-shell="backdrop"'), "no backdrop while the drawer is closed");
  assert.match(html, /data-x="palette-closed"/);
  const order = ['data-kit-shell="sidebar"', 'data-kit-shell="column"', "<header", 'data-x="banner"', "<main",
    "<footer", 'data-x="bottom"', 'data-x="palette-closed"'].map((m) => html.indexOf(m));
  assert.deepEqual([...order].sort((a, b) => a - b), order, `slot order ${order}`);
  assert.equal(seen.length, 1);
  for (const k of ["openDrawer", "closeDrawer", "openPalette", "closePalette", "togglePalette"]) {
    assert.equal(typeof seen[0][k], "function", k);
  }
  assert.equal(seen[0].drawerOpen, false);
  assert.equal(seen[0].paletteOpen, false);
});

test("main=shell renders one <main> with its id; main=page leaves the <main> to the page", () => {
  const own = shell({ sidebar: h("nav"), mainId: "main", mainStyle: { padding: 20 }, mainProps: { "data-pshell": "main" } });
  assert.equal((own.match(/<main /g) ?? []).length, 1);
  assert.match(own, /<main data-pshell="main" data-kit-shell="main" id="main" style="[^"]*overflow-y:auto[^"]*padding:20px/);
  const page = shell({ sidebar: h("nav"), main: "page" }, h("main", { "data-kit-frame": "page" }, "x"));
  assert.equal((page.match(/<main /g) ?? []).length, 1);
  assert.ok(!page.includes('data-kit-shell="main"'));
});

test("scroll models: one screen high column, or min-height with the document scrolling; embedded = parent height", () => {
  const mainScroll = shell({ sidebar: h("nav") });
  assert.match(mainScroll, /data-kit-shell="column" style="[^"]*height:100vh/);
  assert.match(mainScroll, /data-kit-shell="frame"[^>]*style="[^"]*min-height:100vh/);
  const doc = shell({ sidebar: h("nav"), scroll: "document" });
  assert.match(doc, /data-kit-shell-scroll="document"/);
  assert.match(doc, /data-kit-shell="column" style="[^"]*min-height:100vh/);
  assert.ok(!/<main[^>]*overflow-y:auto/.test(doc), "the document scrolls, not the main");
  const box = shell({ sidebar: h("nav"), embedded: true });
  assert.match(box, /data-kit-shell="frame"[^>]*style="display:flex;height:100%/);
  assert.match(box, /data-kit-shell="column" style="[^"]*height:100%/);
});

test("the CIS AdminShell is a configuration of the ShellFrame (no drawer state or key listener of its own)", () => {
  const src = readFileSync(join(FRONTEND, "app", "_kit", "AdminShell.tsx"), "utf8");
  assert.match(src, /from "\.\/core\/ShellFrame"/);
  assert.match(src, /<ShellFrame/);
  assert.ok(!/useState|addEventListener|aa-admin-backdrop/.test(src));
  const css = readFileSync(join(FRONTEND, "app", "globals.css"), "utf8");
  assert.ok(!css.includes("aa-admin-sidebar--open"), "the drawer slide is the ShellFrame's, not globals.css");
});

test("the gallery shows the shell (entry `shell`)", () => {
  const src = readFileSync(join(CORE, "KitGallery.tsx"), "utf8");
  assert.match(src, /<Entry id="shell"/);
  assert.match(src, /<ShellFrame\s+embedded/);
});
