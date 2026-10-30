// frontend/lib/kitButtons.test.mjs
// AA-994 — the kit button variants in app/_kit/core/Buttons.tsx (and the underline Tabs in primitives.tsx) render the
// accessible markup the pages rely on. Run: npm run test:unit.
//
// The core is TSX, which `node --test --experimental-strip-types` cannot load, so the test transpiles every core
// module with the TypeScript compiler into a temporary folder inside frontend/ (so `react` and `lucide-react` resolve
// from node_modules), then renders with react-dom/server. Checks:
//   1. the re-export: primitives exports every button component and type (the consumer barrels `export *` from it),
//      by name (no `export *` in a "use client" core module: Next.js refuses it);
//   2. Button keeps its look and gains passthrough (aria-*, data-*, testId, className, type), dashed variant;
//   3. LinkButton / IconButton / ListButton / Pressable: role, name, ARIA, tones;
//   4. SegmentedControl is a labelled radio group with one tab stop; ButtonGroup is a labelled group; ChipButton and
//      Switch carry their state in ARIA;
//   5. Tabs underline is a tablist with aria-selected, a disabled tab and a count; the track variant's markup has no
//      tab roles (unchanged);
//   6. rovingTarget: arrows, Home / End, wrap-around, disabled items skipped, other keys ignored.

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
let B; // Buttons module
let P; // primitives module
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
  B = await import(pathToFileURL(join(dir, "Buttons.mjs")).href);
  P = await import(pathToFileURL(join(dir, "primitives.mjs")).href);
  React = require("react");
  ({ renderToStaticMarkup: render } = require("react-dom/server"));
});

after(() => {
  if (dir) rmSync(dir, { recursive: true, force: true });
});

const h = (...a) => React.createElement(...a);
/** The markup without the hoisted <style> (React may or may not inline it in a static render). */
const html = (el) => render(el).replace(/<style[^>]*>[\s\S]*?<\/style>/g, "");

test("primitives re-exports every button component, so the consumer barrels need no change", () => {
  for (const name of ["Button", "LinkButton", "IconButton", "SegmentedControl", "ButtonGroup", "ListButton",
    "Pressable", "ChipButton", "Switch", "ButtonStyles", "BUTTON_CLASS", "rovingTarget"]) {
    assert.ok(B[name] !== undefined, `${name} exists`);
  }
  for (const name of Object.keys(B)) assert.equal(P[name], B[name], `${name} is re-exported by primitives`);
  const types = [...readFileSync(join(CORE, "Buttons.tsx"), "utf8").matchAll(/^export type (\w+)/gm)].map((m) => m[1]);
  const prim = readFileSync(join(CORE, "primitives.tsx"), "utf8");
  const typeBlock = prim.match(/export type \{([^}]*)\} from "\.\/Buttons";/);
  assert.ok(typeBlock, "primitives re-exports the Buttons types");
  for (const t of types) assert.match(typeBlock[1], new RegExp(`\\b${t}\\b`), `type ${t} is re-exported by primitives`);
  const barrel = readFileSync(join(CORE, "..", "index.ts"), "utf8");
  assert.match(barrel, /export \* from "\.\/core\/primitives";/);
});

test('no "use client" core module uses `export *` (Next.js refuses it in a client boundary)', () => {
  for (const name of readdirSync(CORE).filter((n) => /\.(ts|tsx)$/.test(n))) {
    const text = readFileSync(join(CORE, name), "utf8");
    if (!/^"use client";$/m.test(text)) continue;
    assert.doesNotMatch(text.replace(/^\s*\/\/.*$/gm, ""), /^export \*/m, `${name}: use named re-exports`);
  }
});

test("Button: same look as before, type=button by default, ARIA / data / testId / className pass through", () => {
  const out = html(h(B.Button, {
    variant: "primary", testId: "save", "aria-haspopup": "menu", "aria-expanded": false, "data-row": "7",
    className: "mt-3", ariaLabel: "Save the case", id: "b1",
  }, "Save"));
  assert.match(out, /^<button /);
  assert.match(out, /type="button"/);
  assert.match(out, /data-testid="save"/);
  assert.match(out, /aria-haspopup="menu"/);
  assert.match(out, /aria-expanded="false"/);
  assert.match(out, /data-row="7"/);
  assert.match(out, /aria-label="Save the case"/);
  assert.match(out, /id="b1"/);
  assert.match(out, /class="aa-kit-btn aa-kit-btn--primary mt-3"/);
  assert.match(out, /background:var\(--aa-accent\)/);
  assert.match(out, /color:var\(--aa-on-accent\)/);
  assert.match(out, /text-transform:uppercase/, "md primary keeps the brand uppercase");
  const sm = html(h(B.Button, { size: "sm", variant: "secondary" }, "Edit"));
  assert.doesNotMatch(sm, /text-transform:uppercase/, "sm stays non-uppercase (AA-605)");
  const submit = html(h(B.Button, { type: "submit", disabled: true }, "Send"));
  assert.match(submit, /type="submit"/);
  assert.match(submit, /disabled=""/);
  assert.match(submit, /cursor:not-allowed/);
  const dashed = html(h(B.Button, { variant: "dashed" }, "Add another day"));
  assert.match(dashed, /border:1px dashed var\(--aa-accent-border\)/);
  assert.doesNotMatch(dashed, /text-transform:uppercase/, "dashed is never uppercase");
});

test("LinkButton: a borderless text button in four tones, three sizes, truncation", () => {
  const out = html(h(B.LinkButton, { "aria-expanded": true }, "Hide changes"));
  assert.match(out, /^<button [^>]*type="button"/);
  assert.match(out, /aria-expanded="true"/);
  assert.match(out, /class="aa-kit-btn aa-kit-link aa-kit-link--accent"/);
  assert.match(out, /border:none/);
  assert.match(out, /padding:0/);
  assert.match(out, /color:var\(--aa-accent-deep\)/);
  assert.match(out, /font-size:13px/);
  assert.match(out, /font-weight:600/);
  assert.match(html(h(B.LinkButton, { tone: "danger", size: "sm" }, "Remove")), /color:var\(--aa-red\);font-size:12px/);
  assert.match(html(h(B.LinkButton, { tone: "muted" }, "x")), /aa-kit-link--muted/);
  assert.match(html(h(B.LinkButton, { tone: "inherit", size: "inherit" }, "Unblock")), /color:inherit;font-size:inherit/);
  assert.match(html(h(B.LinkButton, { weight: "regular" }, "a.pdf")), /font-weight:400/);
  const long = html(h(B.LinkButton, { truncate: true, title: "very-long-name.pdf" }, h("svg", { "data-icon": "" }), "very-long-name.pdf"));
  assert.match(long, /text-overflow:ellipsis/);
  assert.match(long, /<span style="display:inline-flex;flex-shrink:0"><svg data-icon=""><\/svg><\/span>/, "the icon keeps its size");
  assert.match(long, /<span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">very-long-name.pdf<\/span>/);
});

test("IconButton: the label is the accessible name and the default tooltip; variants, sizes, danger tone, pressed", () => {
  const out = html(h(B.IconButton, { label: "Edit internal notes" }, h("svg")));
  assert.match(out, /aria-label="Edit internal notes"/);
  assert.match(out, /title="Edit internal notes"/);
  assert.match(out, /width:32px;height:32px/);
  assert.match(out, /border:1px solid var\(--aa-line\)/);
  assert.match(out, /class="aa-kit-btn aa-kit-icon aa-kit-icon--outline"/);
  const own = html(h(B.IconButton, { label: "Send", title: "Only a draft can be sent", disabled: true }, h("svg")));
  assert.match(own, /title="Only a draft can be sent"/);
  assert.match(own, /aria-label="Send"/);
  assert.match(own, /disabled=""/);
  assert.doesNotMatch(html(h(B.IconButton, { label: "Close", title: "" }, h("svg"))), /title=/, 'title="" switches the tooltip off');
  const del = html(h(B.IconButton, { label: "Remove price row 2", variant: "plain", size: "sm", tone: "danger" }, h("svg")));
  assert.match(del, /aa-kit-icon--plain aa-kit-icon--danger/);
  assert.match(del, /width:28px;height:28px/);
  assert.match(del, /background:transparent/);
  const ov = html(h(B.IconButton, { label: "Next photo", variant: "overlay", size: "lg" }, h("svg")));
  assert.match(ov, /border-radius:999px/);
  assert.match(ov, /background:var\(--aa-scrim\);color:var\(--aa-on-solid\)/);
  assert.match(html(h(B.IconButton, { label: "Remove Vietnam", size: "xs", round: true }, h("svg"))), /width:20px;height:20px.*border-radius:999px/);
  assert.match(html(h(B.IconButton, { label: "Pin", pressed: true }, h("svg"))), /aria-pressed="true"/);
  const collapse = html(h(B.IconButton, { label: "Collapse day 2", "aria-expanded": true, variant: "plain" }, h("svg")));
  assert.match(collapse, /aria-expanded="true"/);
});

test("IconButton without a label reports the problem in development", () => {
  const seen = [];
  const orig = console.error;
  console.error = (m) => seen.push(String(m));
  try {
    html(h(B.IconButton, { label: " " }, h("svg")));
  } finally {
    console.error = orig;
  }
  assert.ok(seen.some((m) => /label` is required/.test(m)), seen.join(" | "));
});

test("SegmentedControl: a labelled radio group, one tab stop on the chosen option, chips or track, counts", () => {
  const opts = [
    { value: "open", label: "Waiting for an answer" },
    { value: "answered", label: "Answered", count: 3 },
    { value: "all", label: "All", disabled: true, title: "Coming later" },
  ];
  const out = html(h(B.SegmentedControl, { label: "Request state", options: opts, value: "answered", onChange: () => {},
    appearance: "chips", size: "sm", testId: "seg" }));
  assert.match(out, /^<div role="radiogroup" aria-label="Request state" data-testid="seg"/);
  const radios = [...out.matchAll(/<button [^>]*>/g)].map((m) => m[0]);
  assert.equal(radios.length, 3);
  for (const r of radios) assert.match(r, /role="radio"/);
  assert.match(radios[0], /aria-checked="false"[^>]*tabindex="-1"/);
  assert.match(radios[1], /aria-checked="true"[^>]*tabindex="0"/);
  assert.match(radios[2], /disabled=""[^>]*title="Coming later"/);
  assert.match(radios[1], /border:1px solid var\(--aa-accent\)/, "the chosen chip is gold");
  assert.match(out, /Answered<span[^>]*>3<\/span>/);
  const none = html(h(B.SegmentedControl, { label: "Customer", options: opts, value: null, onChange: () => {} }));
  const first = none.match(/<button [^>]*>/)[0];
  assert.match(first, /tabindex="0"/, "nothing chosen: Tab enters on the first enabled option");
  assert.match(none, /background:var\(--aa-line2\)/, "track appearance is the default");
});

test("ButtonGroup: role=group with a name; attached joins edges", () => {
  const out = html(h(B.ButtonGroup, { label: "Actions for BK-1042", gap: "sm" },
    h(B.IconButton, { label: "Preview" }, h("svg")), h(B.IconButton, { label: "Send" }, h("svg"))));
  assert.match(out, /^<div role="group" aria-label="Actions for BK-1042" class="aa-kit-btngroup"/);
  assert.match(out, /gap:4px/);
  assert.doesNotMatch(out, /data-attached/);
  const att = html(h(B.ButtonGroup, { label: "View", attached: true }, h(B.Button, {}, "Grid"), h(B.Button, {}, "Table")));
  assert.match(att, /data-attached=""/);
  assert.match(att, /flex-wrap:nowrap;gap:0/);
});

test("ChipButton: aria-pressed by default, aria-checked for role checkbox / radio; gold when on", () => {
  const on = html(h(B.ChipButton, { selected: true, size: "sm" }, "Waiting for an answer"));
  assert.match(on, /aria-pressed="true"/);
  assert.match(on, /data-on=""/);
  assert.match(on, /border:1px solid var\(--aa-accent\)/);
  assert.match(on, /font-size:12px/);
  const cat = html(h(B.ChipButton, { selected: false, role: "checkbox" }, "Hotel"));
  assert.match(cat, /role="checkbox"/);
  assert.match(cat, /aria-checked="false"/);
  assert.doesNotMatch(cat, /aria-pressed/);
  assert.doesNotMatch(cat, /data-on/);
  assert.match(cat, /border:1px solid var\(--aa-line\)/);
});

test("Switch: role=switch with aria-checked and a required name; the click flips the value", () => {
  const out = html(h(B.Switch, { label: "Email me when a payment arrives", checked: true, onChange: () => {} }));
  assert.match(out, /role="switch"/);
  assert.match(out, /aria-checked="true"/);
  assert.match(out, /aria-label="Email me when a payment arrives"/);
  assert.match(out, /background:var\(--aa-green\)/);
  assert.match(out, /left:22px/);
  const off = html(h(B.Switch, { label: "x", checked: false, onChange: () => {}, disabled: true }));
  assert.match(off, /aria-checked="false"/);
  assert.match(off, /disabled=""/);
  assert.match(off, /left:2px/);
  // Off track muted2 (not --aa-line: 1.25:1 on card), knob in the card colour (lib/tokensContrast.test.mjs, 3:1).
  assert.match(off, /background:var\(--aa-muted2\)/);
  assert.match(off, /<span aria-hidden="true" style="[^"]*background:var\(--aa-card\)/);
  // The element's onClick calls onChange(!checked).
  const seen = [];
  const el = B.Switch({ label: "x", checked: false, onChange: (v) => seen.push(v) });
  const btn = React.Children.toArray(el.props.children).find((c) => c.type === "button");
  btn.props.onClick();
  assert.deepEqual(seen, [true]);
});

test("ListButton: option rows carry aria-selected, menu items do not; danger tone", () => {
  const opt = html(h(B.ListButton, { role: "option", selected: true }, "Hanoi Old Quarter"));
  assert.match(opt, /role="option"/);
  assert.match(opt, /aria-selected="true"/);
  assert.match(opt, /width:100%/);
  assert.match(opt, /text-align:left/);
  assert.match(html(h(B.ListButton, { role: "option" }, "x")), /aria-selected="false"/);
  const item = html(h(B.ListButton, { role: "menuitem", tone: "danger" }, "Cancel case…"));
  assert.doesNotMatch(item, /aria-selected/);
  assert.match(item, /data-tone="danger"/);
  assert.match(item, /color:var\(--aa-red\)/);
  assert.match(html(h(B.ListButton, { density: "compact" }, "Clear status filter")), /padding:6px 8px.*font-size:12px/);
});

test("Pressable: a reset button with the kit focus class; block or inline", () => {
  const tile = html(h(B.Pressable, { ariaLabel: "Open photo 1" }, h("img", { alt: "" })));
  assert.match(tile, /class="aa-kit-btn aa-kit-pressable"/);
  assert.match(tile, /aria-label="Open photo 1"/);
  assert.match(tile, /display:block/);
  assert.match(tile, /width:100%/);
  assert.match(tile, /border:none;background:transparent;color:inherit;font:inherit/);
  const trigger = html(h(B.Pressable, { display: "inline", "aria-haspopup": "listbox", "aria-expanded": false,
    style: { height: 36 } }, "Status"));
  assert.match(trigger, /aria-haspopup="listbox"/);
  assert.match(trigger, /display:inline-flex/);
  assert.match(trigger, /height:36px/);
});

test("Tabs underline: a tablist with aria-selected, a disabled tab with its reason, counts; track unchanged", () => {
  const tabs = [
    { key: "overview", label: "Overview" },
    { key: "proposals", label: "Proposals", count: 3 },
    { key: "documents", label: "Documents", disabled: true, title: "Coming in wave 3" },
  ];
  const out = html(h(P.Tabs, { tabs, active: "proposals", onChange: () => {}, variant: "underline",
    ariaLabel: "Trip case sections", gap: 20 }));
  assert.match(out, /^<div role="tablist" aria-label="Trip case sections"/);
  assert.match(out, /gap:20px/);
  const btns = [...out.matchAll(/<button [^>]*>/g)].map((m) => m[0]);
  assert.equal(btns.length, 3);
  for (const b of btns) assert.match(b, /role="tab"/);
  assert.match(btns[1], /aria-selected="true"[^>]*tabindex="0"/);
  assert.match(btns[0], /aria-selected="false"[^>]*tabindex="-1"/);
  assert.match(btns[2], /disabled=""[^>]*title="Coming in wave 3"/);
  assert.match(btns[1], /border-bottom:2px solid var\(--aa-accent\)/);
  assert.match(out, /Proposals<span[^>]*>3<\/span>/);
  // The focus ring goes round the label; the tablist keeps 4px of room for it on top / left / right (not the bottom,
  // where a className margin or baseline sits) and takes it back with negative margins.
  assert.match(out, /aria-selected="true"[^>]*><span class="aa-kit-tab__label">Proposals/);
  assert.match(out, /^<div [^>]*style="[^"]*max-width:calc\(100% \+ 8px\);overflow-x:auto;padding-top:4px;padding-left:4px;padding-right:4px;margin-top:-4px;margin-left:-4px;margin-right:-4px/);
  assert.doesNotMatch(out.match(/^<div [^>]*>/)[0], /padding-bottom|margin-bottom/);
  const marked = html(h(P.Tabs, { tabs, active: "overview", onChange: () => {}, variant: "underline",
    ariaLabel: "x", style: { marginBottom: -1 } }));
  assert.match(marked.match(/^<div [^>]*>/)[0], /margin-bottom:-1px/, "a caller's bottom margin is kept");

  const track = html(h(P.Tabs, { tabs: [{ key: "a", label: "A" }, { key: "b", label: "B" }], active: "a",
    onChange: () => {} }));
  assert.doesNotMatch(track, /role=/, "the track variant keeps its plain-button markup");
  assert.match(track, /^<div style="display:flex;gap:4px;padding:4px;background:var\(--aa-line2\)/);
});

test("focus styles: one accent-deep ring (3:1 on every surface), inset in list rows, round the label in tabs", () => {
  const css = B.ButtonStyles().props.children;
  assert.equal(typeof css, "string");
  assert.match(css, /\.aa-kit-btn:focus-visible\{outline:2px solid var\(--aa-accent-deep\);outline-offset:2px\}/);
  assert.doesNotMatch(css, /outline:2px solid var\(--aa-accent\)/, "light --aa-accent is 2.5:1 on card");
  assert.match(css, /\.aa-kit-listbtn:focus-visible\{outline:2px solid var\(--aa-accent-deep\);outline-offset:-2px\}/);
  assert.doesNotMatch(css, /aa-kit-listbtn[^{]*\{[^}]*outline:none/, "list rows keep a visible focus ring");
  assert.match(css, /\.aa-kit-tab:focus-visible\{outline:none\}/);
  assert.match(css, /\.aa-kit-tab:focus-visible>\.aa-kit-tab__label\{outline:2px solid var\(--aa-accent-deep\);outline-offset:2px;/);
  // The later rule wins at equal specificity: the list row and tab rules must come after the generic ring.
  const generic = css.indexOf(".aa-kit-btn:focus-visible{");
  assert.ok(generic < css.indexOf(".aa-kit-listbtn:focus-visible{") && generic < css.indexOf(".aa-kit-tab:focus-visible{"));
});

test("rovingTarget: arrows, Home / End, wrap-around, disabled skipped, other keys ignored", () => {
  const items = [{}, { disabled: true }, {}, {}];
  assert.equal(B.rovingTarget(items, 0, "ArrowRight"), 2, "skips the disabled item");
  assert.equal(B.rovingTarget(items, 0, "ArrowDown"), 2);
  assert.equal(B.rovingTarget(items, 3, "ArrowRight"), 0, "wraps to the start");
  assert.equal(B.rovingTarget(items, 0, "ArrowLeft"), 3, "wraps to the end");
  assert.equal(B.rovingTarget(items, 2, "ArrowUp"), 0, "skips the disabled item backwards");
  assert.equal(B.rovingTarget(items, 2, "Home"), 0);
  assert.equal(B.rovingTarget(items, 0, "End"), 3);
  assert.equal(B.rovingTarget([{ disabled: true }, {}], 1, "Home"), 1, "Home = first enabled");
  assert.equal(B.rovingTarget([{ disabled: true }], 0, "ArrowRight"), -1, "nothing enabled");
  assert.equal(B.rovingTarget(items, 0, "Enter"), null);
  assert.equal(B.rovingTarget(items, 0, "constructor"), null, "only own keys of the map");
});
