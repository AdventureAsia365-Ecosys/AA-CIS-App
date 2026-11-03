// frontend/lib/adminIa.test.mjs
// AA-1039 R6 slice 1 (S266) — the CIS admin information architecture (lib/adminIa.ts, root ADR 0019 d13). Checks:
//   1. the seven groups, in order, and unique hrefs;
//   2. every admin page URL the S265 navigation linked still opens a page after the moves (no page dropped without a
//      redirect), and an admin may open it;
//   3. every nav item points at a real page the PAGE_RULES allow, never at a moved (old) URL;
//   4. each move: the old route has no page any more, the new one has, one hop, a 307 with the query condition;
//   5. every admin page is in the nav or on the short list of pages reached from another page;
//   6. the reserved Plans & add-ons slot is hidden while its page does not exist;
//   7. next.config.ts serves the moves; no link in the app still points at an old URL;
//   8. every nav label has an English and a Vietnamese row (nav.messages.ts);
//   9. a content worker sees the AA Catalogue group only.
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  ADMIN_GROUP_LABELS, ADMIN_IA, ADMIN_ROUTE_MOVES, PRE_R6_ADMIN_ROUTES, adminIaRedirects, resolveAdminRoute,
} from "./adminIa.ts";
import { canSeePage } from "./rbac.ts";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const APP = join(FRONTEND, "app");

/** Does `url` (query ignored) render an App Router page? Dynamic segments ([id]) match any value. */
function pageExists(url) {
  const segments = url.split("?")[0].split("/").filter(Boolean);
  let dir = APP;
  for (const seg of segments) {
    if (existsSync(join(dir, seg)) && statSync(join(dir, seg)).isDirectory()) {
      dir = join(dir, seg);
      continue;
    }
    const dyn = readdirSync(dir, { withFileTypes: true }).find((d) => d.isDirectory() && /^\[[^.\]]+\]$/.test(d.name));
    if (!dyn) return false;
    dir = join(dir, dyn.name);
  }
  return existsSync(join(dir, "page.tsx"));
}

const items = ADMIN_IA.flatMap((g) => g.items);
const visible = items.filter((i) => !i.hidden);

test("1. seven groups in the ADR 0019 d13 order, every href once", () => {
  assert.deepEqual(ADMIN_IA.map((g) => g.label), [...ADMIN_GROUP_LABELS]);
  assert.equal(ADMIN_IA.length, 7);
  const hrefs = items.map((i) => i.href);
  assert.deepEqual(hrefs.filter((h, i) => hrefs.indexOf(h) !== i), [], "duplicate hrefs");
  for (const g of ADMIN_IA) assert.ok(g.items.some((i) => !i.hidden), `${g.label} has a visible item`);
});

test("2. every route the S265 nav linked still resolves to a page an admin may open", () => {
  assert.ok(PRE_R6_ADMIN_ROUTES.length >= 39);
  const broken = [];
  for (const old of PRE_R6_ADMIN_ROUTES) {
    const to = resolveAdminRoute(old);
    if (!pageExists(to)) broken.push(`${old} → ${to}: no page`);
    else if (!canSeePage("admin", to.split("?")[0])) broken.push(`${old} → ${to}: PAGE_RULES refuse admin`);
  }
  assert.deepEqual(broken, []);
});

test("3. nav items point at real pages the PAGE_RULES allow, never at a moved URL", () => {
  const sources = new Set(ADMIN_ROUTE_MOVES.filter((m) => !m.query).map((m) => m.from));
  for (const it of visible) {
    assert.ok(pageExists(it.href), `${it.label}: ${it.href} has a page`);
    assert.ok(canSeePage("admin", it.href), `${it.label}: an admin may open ${it.href}`);
    assert.ok(!sources.has(it.href), `${it.label}: ${it.href} is an old URL`);
    assert.equal(resolveAdminRoute(it.href), it.href, `${it.href} is not redirected`);
  }
  for (const it of items.filter((i) => i.hidden)) {
    assert.ok(canSeePage("admin", it.href), `reserved ${it.href} sits under an admin PAGE_RULE`);
  }
});

test("4. each move: the old page is gone, the new page exists, one hop, 307", () => {
  for (const m of ADMIN_ROUTE_MOVES) {
    if (!m.query) assert.ok(!pageExists(m.from), `${m.from} still has a page (it would never be reached)`);
    assert.ok(pageExists(m.to), `${m.to} has a page`);
    assert.equal(resolveAdminRoute(m.to), m.to, `${m.to} is not moved again`);
  }
  // Query string carries over; the query-conditioned move only fires on its value.
  assert.equal(resolveAdminRoute("/admin/content-quality?piece=abc&tab=versions"),
    "/admin/tenant-qa/quality?piece=abc&tab=versions");
  assert.equal(resolveAdminRoute("/admin/content-quality/"), "/admin/tenant-qa/quality");
  assert.equal(resolveAdminRoute("/admin/atom-curation?section=slate"), "/admin/tenant-qa/slate?section=slate");
  assert.equal(resolveAdminRoute("/admin/atom-curation?section=score"), "/admin/atom-curation?section=score");
  assert.equal(resolveAdminRoute("/admin/atom-curation"), "/admin/atom-curation");
  for (const r of adminIaRedirects()) {
    assert.equal(r.permanent, false, `${r.source} is a 307`);
    assert.ok(r.source.startsWith("/admin/") && r.destination.startsWith("/admin/"));
  }
  const slate = adminIaRedirects().find((r) => r.destination === "/admin/tenant-qa/slate");
  assert.deepEqual(slate?.has, [{ type: "query", key: "section", value: "slate" }]);
});

// Pages opened from another page (a row, a drawer, an index redirect), not from the sidebar.
const UNLISTED = new Set([
  "/admin/platform",                 // index → /admin/platform/staff
  "/admin/platform/staff/[id]",      // Staff › one account
  "/admin/supply-content/[id]",      // Booking's "Edit in CIS" deep link
  "/admin/tenant-qa",                // index → /admin/tenant-qa/quality
]);

test("5. every admin page is in the nav or reached from another page", () => {
  const navHrefs = new Set(visible.map((i) => i.href));
  const pages = [];
  const walk = (dir, url) => {
    for (const d of readdirSync(dir, { withFileTypes: true })) {
      if (!d.isDirectory() || d.name.startsWith("_")) continue;
      const next = `${url}/${d.name}`;
      if (existsSync(join(dir, d.name, "page.tsx"))) pages.push(next);
      walk(join(dir, d.name), next);
    }
  };
  walk(join(APP, "admin"), "/admin");
  const orphans = pages.filter((p) => !navHrefs.has(p) && !UNLISTED.has(p));
  assert.deepEqual(orphans, [], "admin pages with no nav entry");
  assert.ok(pages.length >= 38);
});

test("6. the Plans & add-ons slot stays hidden until its page ships", () => {
  const plans = items.find((i) => i.href === "/admin/tenants/plans");
  assert.ok(plans, "slot reserved in Tenants");
  assert.equal(ADMIN_IA.find((g) => g.items.includes(plans))?.label, "Tenants");
  if (pageExists(plans.href)) {
    assert.ok(!plans.hidden, "the plans page exists: drop `hidden` from its nav slot");
  } else {
    assert.equal(plans.hidden, true, "no plans page yet: the slot must stay hidden");
  }
});

const read = (rel) => readFileSync(join(FRONTEND, rel), "utf8");

test("7. next.config.ts serves the moves; no link still points at an old URL", () => {
  assert.match(read("next.config.ts"), /\.\.\.adminIaRedirects\(\)/);
  const old = ADMIN_ROUTE_MOVES.filter((m) => !m.query).map((m) => m.from.replace(/[/-]/g, "\\$&"));
  // A quote or backtick right before the path: links and pushes, never "/api/admin/…" (BFF paths keep their names).
  const linkRe = new RegExp(`["'\`](${old.join("|")})(?![\\w/-])`);
  const slateRe = /atom-curation\?section=slate/;
  const offenders = [];
  const walk = (dir) => {
    for (const d of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, d.name);
      if (d.isDirectory()) { if (d.name !== "node_modules" && !d.name.startsWith(".")) walk(p); continue; }
      if (!/\.(ts|tsx)$/.test(d.name) || p.endsWith(join("lib", "adminIa.ts"))) continue;
      // Code only: comment lines keep their history ("was /admin/tenant-activity").
      const code = readFileSync(p, "utf8").split("\n").filter((l) => !/^\s*(\/\/|\*|\/\*|\{\/\*)/.test(l)).join("\n");
      if (linkRe.test(code) || slateRe.test(code)) offenders.push(p.slice(FRONTEND.length));
    }
  };
  walk(APP);
  walk(join(FRONTEND, "lib"));
  walk(join(FRONTEND, "components"));
  assert.deepEqual(offenders, []);
});

test("8. every nav label has an English and a Vietnamese row", () => {
  const src = read("app/_kit/nav.messages.ts");
  const missing = [];
  for (const label of [...ADMIN_GROUP_LABELS, ...items.map((i) => i.label)]) {
    const n = src.split(`${JSON.stringify(label)}:`).length - 1;
    if (n < 2) missing.push(label);
  }
  assert.deepEqual(missing, []);
});

test("9. a content worker sees the AA Catalogue group only; the viewer sees its read pages", () => {
  for (const role of ["reviewer", "content"]) {
    const groups = ADMIN_IA.filter((g) => g.items.some((i) => !i.hidden && canSeePage(role, i.href))).map((g) => g.label);
    assert.deepEqual(groups, ["AA Catalogue"], role);
  }
  const viewer = visible.filter((i) => canSeePage("viewer", i.href)).map((i) => i.href);
  assert.ok(viewer.includes("/admin/review") && viewer.includes("/admin/settings"));
  assert.ok(!viewer.includes("/admin/upload"), "the viewer never gets the upload page");
});
