// frontend/lib/kitNavGroup.test.mjs
// S264 — collapsible sidebar nav groups in the kit core: the fold store (app/_kit/core/sidebarStore.ts, end of file),
// the useNavGroups hook (core/useSidebar.ts) and the NavGroup / NavGroupHeader components (core/NavGroup.tsx). Every
// app's sidebar uses these (CIS admin, Booking admin, CRM web, Content studio). Run: npm run test:unit.
//
// Like kitButtons.test.mjs, the core TSX is transpiled with the TypeScript compiler into a temporary folder inside
// frontend/ (so react and lucide-react resolve) and rendered with react-dom/server. A fake `window` (localStorage +
// event listeners) stands in for the browser where a test needs storage. Checks:
//   1. group ids, the stored list format (sorted, malformed entries skipped) and the open rule (active group open
//      unless folded on this page, others stored or folded);
//   2. the store: one key per app scope, subscribers notified, the storage event of another tab, blocked storage
//      falls back to memory, no window = no-op (SSR);
//   3. NavGroupHeader: aria-expanded / aria-controls / title, the count pill only while folded, 99+, the active
//      accent, data-* passthrough;
//   4. NavGroup: folded items hidden but present, icon rail = no header and items shown, a group without label has
//      no header;
//   5. useNavGroups on the server: the server snapshot (saved state ignored, so SSR and hydration agree), the active
//      group open; toggle stores the choice under the app's key.

import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { after, afterEach, before, test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createRequire } from "node:module";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const CORE = join(FRONTEND, "app", "_kit", "core");
const require = createRequire(join(FRONTEND, "package.json"));
const ts = require("typescript");

let dir;
let S; // sidebarStore
let H; // useSidebar
let N; // NavGroup
let React;
let render;

before(async () => {
  dir = mkdtempSync(join(FRONTEND, ".kit-navgroup-"));
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
  S = await import(pathToFileURL(join(dir, "sidebarStore.mjs")).href);
  H = await import(pathToFileURL(join(dir, "useSidebar.mjs")).href);
  N = await import(pathToFileURL(join(dir, "NavGroup.mjs")).href);
  React = require("react");
  ({ renderToStaticMarkup: render } = require("react-dom/server"));
});

after(() => {
  if (dir) rmSync(dir, { recursive: true, force: true });
});

afterEach(() => {
  delete globalThis.window;
});

const h = (...a) => React.createElement(...a);

/** A minimal browser window: localStorage (optionally throwing) and storage-event listeners. */
function fakeWindow({ blocked = false } = {}) {
  const data = new Map();
  const listeners = new Set();
  const localStorage = {
    getItem: (k) => {
      if (blocked) throw new Error("blocked");
      return data.has(k) ? data.get(k) : null;
    },
    setItem: (k, v) => {
      if (blocked) throw new Error("blocked");
      data.set(k, String(v));
    },
    removeItem: (k) => {
      if (blocked) throw new Error("blocked");
      data.delete(k);
    },
  };
  return {
    data,
    listeners,
    localStorage,
    addEventListener: (type, fn) => type === "storage" && listeners.add(fn),
    removeEventListener: (type, fn) => type === "storage" && listeners.delete(fn),
    fire: (key) => { for (const fn of listeners) fn({ key }); },
  };
}

// ── 1. ids, format, open rule ──

test("navGroupId turns an English label into a stable slug", () => {
  assert.equal(S.navGroupId("People & access"), "people-access");
  assert.equal(S.navGroupId("AI & spend"), "ai-spend");
  assert.equal(S.navGroupId("Pipeline"), "pipeline");
  assert.equal(S.navGroupId("  Tenant output "), "tenant-output");
  assert.equal(S.navGroupId("&&"), "group");
});

test("the stored list is sorted by id and skips malformed entries", () => {
  assert.equal(S.serializeNavGroups({ zeta: "open", alpha: "closed" }), "alpha=closed,zeta=open");
  assert.deepEqual(S.parseNavGroups("alpha=closed,zeta=open"), { alpha: "closed", zeta: "open" });
  assert.deepEqual(S.parseNavGroups(""), {});
  assert.deepEqual(S.parseNavGroups("=open,beta,gamma=maybe,delta=open"), { delta: "open" });
  const map = { assets: "open", jobs: "closed", pipeline: "open" };
  assert.deepEqual(S.parseNavGroups(S.serializeNavGroups(map)), map);
});

test("the active group is open unless folded on this page; the others follow the store, folded by default", () => {
  const stored = { assets: "open", pipeline: "closed" };
  assert.equal(S.resolveNavGroupOpen("pipeline", stored, "pipeline"), true, "active wins over a stored fold");
  assert.equal(S.resolveNavGroupOpen("pipeline", stored, "pipeline", true), false, "folded by hand on this page");
  assert.equal(S.resolveNavGroupOpen("assets", stored, "pipeline"), true, "stored open");
  assert.equal(S.resolveNavGroupOpen("jobs", stored, "pipeline"), false, "nothing stored = folded");
  assert.equal(S.resolveNavGroupOpen("jobs", {}, null), false);
});

// ── 2. the store ──

test("setNavGroupState stores per app scope and notifies subscribers", () => {
  const w = fakeWindow();
  globalThis.window = w;
  let calls = 0;
  const off = S.subscribeNavGroups("cis-admin", () => { calls += 1; });
  S.setNavGroupState("cis-admin", "pipeline", "open");
  S.setNavGroupState("cis-admin", "assets", "closed");
  S.setNavGroupState("booking", "operations", "open");
  assert.equal(w.data.get("aa_nav_groups:cis-admin"), "assets=closed,pipeline=open");
  assert.equal(w.data.get("aa_nav_groups:booking"), "operations=open");
  assert.equal(S.readNavGroupsRaw("cis-admin"), "assets=closed,pipeline=open");
  assert.equal(calls, 3);
  S.setNavGroupState("cis-admin", "pipeline", "closed");
  assert.equal(S.readNavGroupsRaw("cis-admin"), "assets=closed,pipeline=closed");
  off();
  S.setNavGroupState("cis-admin", "pipeline", "open");
  assert.equal(calls, 4, "unsubscribed listener is not called");
});

test("a storage event from another tab reaches only the subscribers of that scope's key", () => {
  const w = fakeWindow();
  globalThis.window = w;
  let cis = 0;
  let booking = 0;
  const offA = S.subscribeNavGroups("cis-admin", () => { cis += 1; });
  const offB = S.subscribeNavGroups("booking", () => { booking += 1; });
  w.fire("aa_nav_groups:cis-admin");
  w.fire("cis_sidebar");
  assert.deepEqual([cis, booking], [1, 0]);
  offA();
  offB();
  assert.equal(w.listeners.size, 0, "listeners removed on unsubscribe");
});

test("blocked storage keeps the fold in memory; no window (SSR) is a no-op", () => {
  globalThis.window = fakeWindow({ blocked: true });
  S.setNavGroupState("crm-web", "money", "open");
  assert.equal(S.readNavGroupsRaw("crm-web"), "money=open");
  delete globalThis.window;
  assert.equal(S.readNavGroupsRaw("crm-web"), "");
  S.setNavGroupState("crm-web", "money", "closed"); // must not throw
  const off = S.subscribeNavGroups("crm-web", () => {});
  off();
});

// ── 3. NavGroupHeader ──

test("NavGroupHeader is a disclosure button with the state in ARIA", () => {
  const open = render(h(N.NavGroupHeader, { label: "Pipeline", open: true, onToggle: () => {}, controls: "r1" }));
  assert.match(open, /<button[^>]*type="button"/);
  assert.match(open, /aria-expanded="true"/);
  assert.match(open, /aria-controls="r1"/);
  assert.match(open, /title="Hide Pipeline"/);
  const folded = render(h(N.NavGroupHeader, { label: "Pipeline", open: false, onToggle: () => {} }));
  assert.match(folded, /aria-expanded="false"/);
  assert.match(folded, /title="Show Pipeline"/);
});

test("the count pill shows only on a folded header with a count, capped at 99+", () => {
  const props = { label: "Pipeline", onToggle: () => {} };
  const folded = render(h(N.NavGroupHeader, { ...props, open: false, badge: 25 }));
  assert.match(folded, /data-nav-group-badge="true"[^>]*aria-label="25 in this group"[^>]*>25</);
  assert.doesNotMatch(render(h(N.NavGroupHeader, { ...props, open: true, badge: 25 })), /data-nav-group-badge/);
  assert.doesNotMatch(render(h(N.NavGroupHeader, { ...props, open: false, badge: 0 })), /data-nav-group-badge/);
  assert.doesNotMatch(render(h(N.NavGroupHeader, { ...props, open: false, badge: null })), /data-nav-group-badge/);
  assert.match(render(h(N.NavGroupHeader, { ...props, open: false, badge: 140 })), />99\+</);
  assert.match(
    render(h(N.NavGroupHeader, { ...props, open: false, badge: 3, badgeLabel: "3 tours available" })),
    /aria-label="3 tours available"/,
  );
  assert.equal(N.navBadgeText(99), "99");
  assert.equal(N.navBadgeText(100), "99+");
});

test("a folded active header is marked and accented; data-* hooks pass through", () => {
  const html = render(h(N.NavGroupHeader, {
    label: "Assets", open: false, active: true, onToggle: () => {}, testId: "g-toggle",
    className: "my-title", headerProps: { "data-pshell-label": "true" },
  }));
  assert.match(html, /data-active="true"/);
  assert.match(html, /color:var\(--aa-accent\)/);
  assert.match(html, /data-pshell-label="true"/);
  assert.match(html, /data-testid="g-toggle"/);
  assert.match(html, /class="my-title"/);
  assert.match(render(h(N.NavGroupHeader, { label: "Assets", open: true, active: true, onToggle: () => {} })),
    /color:var\(--aa-side-text3\)/, "an open active group keeps the normal colour (its item shows the page)");
});

// ── 4. NavGroup ──

const item = (label) => h("a", { href: `/${label}` }, label);

test("a folded NavGroup keeps its items in the DOM, hidden, and the header points at them", () => {
  const html = render(h(N.NavGroup, { id: "assets", label: "Assets", open: false, onToggle: () => {} }, item("photos")));
  assert.match(html, /data-testid="nav-group-assets"/);
  assert.match(html, /data-open="false"/);
  assert.match(html, /data-testid="nav-group-assets-toggle"/);
  const controls = html.match(/aria-controls="([^"]+)"/)[1];
  assert.match(html, new RegExp(`id="${controls.replace(/[^\w]/g, "\\$&")}"[^>]*hidden`));
  assert.match(html, /role="group"[^>]*aria-label="Assets"|aria-label="Assets"[^>]*role="group"/);
  assert.match(html, />photos</);
});

test("an open NavGroup shows its items; the icon rail drops the header and shows every item", () => {
  const open = render(h(N.NavGroup, { id: "assets", label: "Assets", open: true, onToggle: () => {} }, item("photos")));
  assert.match(open, /data-open="true"/);
  assert.doesNotMatch(open, /hidden=""/);
  const rail = render(h(N.NavGroup, { id: "assets", label: "Assets", open: false, rail: true, onToggle: () => {} },
    item("photos")));
  assert.doesNotMatch(rail, /<button/);
  assert.match(rail, /data-open="true"/);
  assert.doesNotMatch(rail, /hidden=""/);
});

test("a NavGroup without a label has no header and is always open", () => {
  const html = render(h(N.NavGroup, { id: "apps", label: null, open: false, onToggle: () => {}, testId: "grp" },
    item("apps")));
  assert.doesNotMatch(html, /<button/);
  assert.match(html, /data-testid="grp"/);
  assert.match(html, /data-open="true"/);
});

// ── 5. useNavGroups ──

test("useNavGroups renders the server snapshot (saved state ignored) with the active group open", () => {
  const w = fakeWindow();
  w.data.set("aa_nav_groups:cis-admin", "assets=open,pipeline=closed");
  globalThis.window = w;
  let api;
  function Probe() {
    api = H.useNavGroups("cis-admin", "pipeline", "/admin/review");
    return h("span", null, ["pipeline", "assets", "jobs"].map((id) => `${id}:${api.isOpen(id)}`).join(" "));
  }
  const html = render(h(Probe));
  assert.match(html, /pipeline:true assets:false jobs:false/);
});

test("useNavGroups toggle stores the user's choice under the app's key", () => {
  const w = fakeWindow();
  globalThis.window = w;
  let api;
  function Probe() {
    api = H.useNavGroups("booking", "operations", "/cases");
    return null;
  }
  render(h(Probe));
  api.toggle("finance");
  assert.equal(w.data.get("aa_nav_groups:booking"), "finance=open");
  api.toggle("operations"); // the active group, open: folding it is stored too
  assert.equal(w.data.get("aa_nav_groups:booking"), "finance=open,operations=closed");
});
