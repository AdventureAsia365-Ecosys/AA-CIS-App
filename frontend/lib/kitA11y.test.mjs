// frontend/lib/kitA11y.test.mjs
// AA-1131 (S283 UI audit, batch K1) — the accessibility fixes in the kit core (app/_kit/core), the copy every app
// vendors. Run: npm run test:unit. Same harness as kitDataTableRows.test.mjs (transpiled core, react-dom/server); the
// browser half (Tab to a row, Enter, the trap, Escape and the focus return) is the visual job's keyboard check on the
// kit gallery (tests/e2e/visual/a11y.spec.ts).
// Checks:
//   1. DataTable: the page-size select is named ("Rows per page", EN + VI) and marked data-kit-page-size; rows are
//      keyboard targets (tabindex 0, data-kit-row-open) only when the table has onRowClick; the focus ring rule;
//      rowKeyOpens (Enter / Space on the row itself, never a key on a control inside the row);
//   2. Drawer: named by its title (aria-labelledby = the title's id), else ariaLabel, else "Dialog"; focusable as a
//      whole (tabindex -1) for the no-control case; trapTarget (the Tab cycle) and focusables (hidden and nested-dialog
//      elements left out);
//   3. PageFrame: the <main> of both variants is position: relative (an sr-only child cannot grow the document);
//   4. PageHeader: the kit's own layout CSS (text basis 420 px when description AND actions, stack below 768 px);
//   5. CIS shell sidebar (app/_kit/Sidebar.tsx, not core): the Collapse toggle sits outside the scrolling part.

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
let D; // DataTable
let Dr; // Drawer
let F; // PageFrame
let P; // primitives
let I; // useI18n (the provider)
let M; // kit.messages
let React;
let render;

before(async () => {
  dir = mkdtempSync(join(FRONTEND, ".kit-a11y-"));
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
  const load = (n) => import(pathToFileURL(join(dir, n)).href);
  D = await load("DataTable.mjs");
  Dr = await load("Drawer.mjs");
  F = await load("PageFrame.mjs");
  P = await load("primitives.mjs");
  I = await load("useI18n.mjs");
  M = await load("kit.messages.mjs");
  React = require("react");
  ({ renderToStaticMarkup: render } = require("react-dom/server"));
});

after(() => {
  if (dir) rmSync(dir, { recursive: true, force: true });
});

const h = (...a) => React.createElement(...a);
const ROWS = [{ id: "a1", name: "Lantern Valley Walk" }, { id: "b2", name: "Misty Ridge Cycle" }];
const COLUMNS = [{ id: "name", header: "Trip", accessorFn: (r) => r.name }];

function table(extra = {}) {
  return render(h(D.DataTable, { data: ROWS, columns: COLUMNS, tableId: "t", getRowId: (r) => r.id, searchable: false, ...extra }));
}

// ── 1. DataTable ─────────────────────────────────────────────────────────────────────────────────────────────────

test("the page-size select has an accessible name and the kit hook", () => {
  const html = table();
  const select = html.match(/<select[^>]*>/)?.[0] ?? "";
  assert.match(select, /aria-label="Rows per page"/);
  assert.match(select, /data-kit-page-size=""/);
});

test("the page-size name is translated (kit messages, VI)", () => {
  assert.equal(M.KIT_MESSAGES.en.rowsPerPage, "Rows per page");
  assert.equal(M.KIT_MESSAGES.vi.rowsPerPage, "Số dòng mỗi trang");
  assert.equal(M.KIT_MESSAGES.vi.dialog, "Hộp thoại");
});

test("rows are keyboard targets only when the table opens them (onRowClick)", () => {
  const off = table();
  assert.doesNotMatch(off, /data-kit-row-open/);
  assert.doesNotMatch(off, /<tr[^>]*tabindex/i);
  const on = table({ onRowClick: () => {} });
  const rows = on.match(/<tr[^>]*data-kit-row=""[^>]*>/g) ?? [];
  assert.equal(rows.length, 2);
  for (const tr of rows) {
    assert.match(tr, /tabindex="0"/i);
    assert.match(tr, /data-kit-row-open=""/);
  }
  // The header row is never a target.
  assert.doesNotMatch(on.match(/<thead>.*<\/thead>/s)?.[0] ?? "", /tabindex/i);
});

test("a clickable row shows the kit focus ring on :focus-visible (inset, accent-deep)", () => {
  assert.match(D.DATATABLE_CSS, /\[data-kit-row-open\]:focus-visible\{outline:2px solid var\(--aa-accent-deep\);outline-offset:-2px\}/);
});

test("rowKeyOpens: Enter or Space on the row itself; a key inside the row stays that control's", () => {
  assert.equal(D.rowKeyOpens("Enter", true), true);
  assert.equal(D.rowKeyOpens(" ", true), true);
  assert.equal(D.rowKeyOpens("Spacebar", true), true);
  assert.equal(D.rowKeyOpens("Enter", false), false); // a button, link or checkbox in the row
  assert.equal(D.rowKeyOpens(" ", false), false);
  for (const key of ["Tab", "Escape", "ArrowDown", "a"]) assert.equal(D.rowKeyOpens(key, true), false, key);
});

// ── 2. Drawer ────────────────────────────────────────────────────────────────────────────────────────────────────

function drawer(props = {}) {
  return render(h(Dr.Drawer, { open: true, onClose: () => {}, ...props }, h("p", null, "Body")));
}

test("a drawer with a title is named by it (aria-labelledby = the title's id)", () => {
  const html = drawer({ title: "Pine Hollow Trek" });
  const dialog = html.match(/<div[^>]*role="dialog"[^>]*>/)?.[0] ?? "";
  assert.match(dialog, /aria-modal="true"/);
  const id = dialog.match(/aria-labelledby="([^"]+)"/)?.[1];
  assert.ok(id, "aria-labelledby is set");
  assert.doesNotMatch(dialog, /aria-label="/);
  assert.match(html, new RegExp(`<div id="${id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"[^>]*>Pine Hollow Trek</div>`));
  assert.match(dialog, /tabindex="-1"/i);
});

test("a drawer without a title takes ariaLabel, else the kit word", () => {
  assert.match(drawer({ ariaLabel: "Trip detail" }), /role="dialog"[^>]*aria-label="Trip detail"/);
  assert.match(drawer(), /role="dialog"[^>]*aria-label="Dialog"/);
  // The server render reads initialLang (the store's server snapshot).
  const store = { subscribe: () => () => {}, getSnapshot: () => "vi", setLang: () => {} };
  const vi = render(h(I.I18nProvider, { store, initialLang: "vi" }, h(Dr.Drawer, { open: true, onClose: () => {} }, "x")));
  assert.match(vi, /aria-label="Hộp thoại"/);
  assert.doesNotMatch(drawer(), /aria-labelledby/);
});

test("a closed drawer renders nothing", () => {
  assert.equal(render(h(Dr.Drawer, { open: false, onClose: () => {}, title: "x" }, "x")), "");
});

test("trapTarget: Tab from the last wraps to the first, Shift+Tab from the first to the last", () => {
  assert.equal(Dr.trapTarget(3, 2, false), 0);
  assert.equal(Dr.trapTarget(3, 0, true), 2);
  assert.equal(Dr.trapTarget(3, 1, false), null); // the browser's own move stays inside
  assert.equal(Dr.trapTarget(3, 1, true), null);
  assert.equal(Dr.trapTarget(3, -1, false), 0); // focus on the panel or outside: back in at the start
  assert.equal(Dr.trapTarget(3, -1, true), 2);
  assert.equal(Dr.trapTarget(1, 0, false), 0); // one control: it keeps the focus
  assert.equal(Dr.trapTarget(1, 0, true), 0);
  assert.equal(Dr.trapTarget(0, -1, false), -1); // nothing focusable: the panel itself
});

test("focusables leaves out hidden elements and those of a nested dialog", () => {
  const root = { querySelectorAll: () => list };
  const nested = { id: "confirm" };
  const el = (name, { shown = true, dialog = root } = {}) => ({
    name,
    getClientRects: () => (shown ? [{}] : []),
    closest: () => dialog,
  });
  const list = [el("close"), el("hidden", { shown: false }), el("confirm-button", { dialog: nested }), el("save")];
  assert.deepEqual(Dr.focusables(root).map((e) => e.name), ["close", "save"]);
  assert.match(Dr.FOCUSABLE, /button:not\(\[disabled\]\)/);
  assert.match(Dr.FOCUSABLE, /\[tabindex\]:not\(\[tabindex="-1"\]\)/);
});

// ── 3. PageFrame ─────────────────────────────────────────────────────────────────────────────────────────────────

test("the PageFrame <main> is the containing block of absolutely positioned children (both variants)", () => {
  for (const variant of ["page", "split"]) {
    const html = render(h(F.PageFrame, { variant }, h("p", null, "x")));
    const main = html.match(/<main[^>]*>/)?.[0] ?? "";
    assert.match(main, /position:relative/, variant);
  }
  // A page's own style still wins (a page that wants another position says so).
  const own = render(h(F.PageFrame, { style: { position: "static" } }, "x"));
  assert.match(own, /position:static/);
});

// ── 4. PageHeader ────────────────────────────────────────────────────────────────────────────────────────────────

test("the PageHeader layout CSS is the kit's: a 420 px text basis beside actions, a stack below 768 px", () => {
  const css = P.PAGE_HEADER_CSS;
  assert.ok(css.includes(".aa-page-header-text{flex:1 1 240px}"));
  assert.ok(css.includes(".aa-page-header--stack>.aa-page-header-text{flex:1 1 420px}"));
  const phone = css.slice(css.indexOf("@media (max-width: 767px)"));
  assert.ok(phone.includes(".aa-page-header--stack>.aa-page-header-text{flex-basis:100%}"));
  assert.ok(phone.includes(".aa-page-header--stack>.aa-page-header-actions{justify-content:flex-start;flex-basis:100%;min-width:0}"));
  // The phone rule comes after the desktop one (same specificity: the later wins).
  assert.ok(css.indexOf("flex:1 1 420px") < css.indexOf("flex-basis:100%"));
});

test("a PageHeader with a description and actions is the stacking kind and loads the kit CSS", () => {
  const html = render(h(P.PageHeader, { title: "Writing memory", description: "Long text", actions: h("button", null, "Run") }));
  assert.match(html, /class="aa-page-header aa-page-header--stack"/);
  assert.match(html, /aa-kit-page-header/); // the hoisted style element
  const plain = render(h(P.PageHeader, { title: "Jobs" }));
  assert.match(plain, /class="aa-page-header"/);
});

// ── 5. CIS shell sidebar (not core) ──────────────────────────────────────────────────────────────────────────────

test("the CIS sidebar keeps the Collapse toggle outside the scrolling part", () => {
  const src = readFileSync(join(FRONTEND, "app", "_kit", "Sidebar.tsx"), "utf8");
  const aside = src.slice(src.indexOf("<aside"), src.indexOf("{/* Brand"));
  assert.match(aside, /overflow: "hidden"/);
  assert.doesNotMatch(aside, /overflowY: "auto"/);
  const scroll = src.indexOf('data-testid="sidebar-scroll"');
  assert.ok(scroll > 0, "a scrolling wrapper");
  assert.match(src.slice(scroll, scroll + 200), /minHeight: 0, overflowY: "auto"/);
  assert.ok(src.indexOf('data-testid="admin-nav"') > scroll && src.indexOf('data-testid="sidebar-apps"') > scroll);
  assert.ok(src.indexOf('data-testid="sidebar-collapse"') > src.indexOf('data-testid="sidebar-apps"'));
});
