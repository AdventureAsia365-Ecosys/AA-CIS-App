// frontend/lib/adminFrame.test.mjs
// S252 (AA-919 follow-up): every admin page looks and behaves the same — one content frame, the kit AdminMain
// (standard padding, the phone rule, its own scroll, minWidth 0), and the kit PageHeader at the top.
// Run: npm run test:unit.
//
// Checks, over every .tsx file under app/admin:
//   1. no hand-written <main> and no hand-set `aa-admin-main` / `aa-admin-split` / `aa-admin-pane` class: the frame
//      and its pane classes come only from app/_kit/AdminMain.tsx;
//   2. every admin page (page.tsx, except a redirect-only page) reaches the kit AdminMain and the kit PageHeader,
//      in the page file itself or in a module it imports from app/admin (followed transitively).

import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const ADMIN = join(FRONTEND, "app", "admin");

function walk(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...walk(p));
    else if (p.endsWith(".tsx") || p.endsWith(".ts")) out.push(p);
  }
  return out;
}

/** Source without comments, so a comment that mentions `<main>` or the class names never trips the guard. */
function code(p) {
  // Whole-line // comments first (one may hold a "/*", e.g. "API: /admin/x/* via the BFF"), then block comments,
  // then trailing // comments (not the "//" of a URL).
  return readFileSync(p, "utf8")
    .replace(/^\s*\/\/.*$/gm, "")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:"'`])\/\/.*$/gm, "$1");
}

const FILES = walk(ADMIN);
const rel = (p) => relative(FRONTEND, p);

test("no admin file hand-writes the page frame", () => {
  const offenders = [];
  for (const p of FILES) {
    if (!p.endsWith(".tsx")) continue;
    const src = code(p);
    if (/<main[\s>]/.test(src)) offenders.push(`${rel(p)}: <main> (render the kit AdminMain)`);
    if (/["'`\s]aa-admin-(main|split|pane(-head|-body)?)["'`\s]/.test(src)) {
      offenders.push(`${rel(p)}: sets an aa-admin-main / aa-admin-split / aa-admin-pane class by hand (use AdminMain, AdminPaneHeader, AdminPaneBody)`);
    }
  }
  assert.deepEqual(offenders, [], offenders.join("\n"));
});

/** Relative imports of a module that resolve to a file under app/admin. */
function localImports(p) {
  const src = code(p);
  const out = [];
  for (const m of src.matchAll(/(?:import|export)\s[^;]*?from\s+["'](\.{1,2}\/[^"']+)["']/g)) {
    const base = resolve(dirname(p), m[1]);
    for (const cand of [base, `${base}.tsx`, `${base}.ts`, join(base, "index.tsx"), join(base, "index.ts")]) {
      if (cand.startsWith(ADMIN) && existsSync(cand) && statSync(cand).isFile()) { out.push(cand); break; }
    }
  }
  return out;
}

/** The page file plus every app/admin module it reaches through relative imports. */
function pageTree(page) {
  const seen = new Set([page]);
  const queue = [page];
  while (queue.length) {
    for (const dep of localImports(queue.shift())) {
      if (!seen.has(dep)) { seen.add(dep); queue.push(dep); }
    }
  }
  return [...seen];
}

const PAGES = FILES.filter((p) => p.endsWith(`${"/"}page.tsx`));
const isRedirectOnly = (p) => /\bredirect\(/.test(code(p)) && !/<[A-Za-z]/.test(code(p));

test("the page list is not empty (the walk found app/admin)", () => {
  assert.ok(PAGES.length >= 20, `found only ${PAGES.length} admin pages`);
});

for (const page of PAGES) {
  if (isRedirectOnly(page)) continue;
  test(`${rel(page)} renders the kit AdminMain and PageHeader`, () => {
    const tree = pageTree(page).map(code).join("\n");
    assert.match(tree, /<AdminMain[\s>]/, `${rel(page)}: no <AdminMain> in the page or the app/admin modules it imports`);
    assert.match(tree, /<PageHeader[\s>]/, `${rel(page)}: no kit <PageHeader> in the page or the modules it imports`);
  });
}
