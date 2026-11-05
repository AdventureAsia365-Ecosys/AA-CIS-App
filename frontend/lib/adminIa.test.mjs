// frontend/lib/adminIa.test.mjs
// AA-1039 R6 slice 1 (S266) — the CIS admin information architecture (lib/adminIa.ts, root ADR 0019 d13). Checks:
//   1. the seven groups of d13 plus Trade (d9, AA-1037), in order, and unique hrefs;
//   2. every admin page URL the S265 navigation linked still opens a page after the moves (no page dropped without a
//      redirect), and an admin may open it;
//   3. every nav item points at a real page the PAGE_RULES allow, never at a moved (old) URL;
//   4. each move: the old route has no page any more, the new one has, one hop, a 307 with the query condition;
//   5. every admin page is in the nav or on the short list of pages reached from another page;
//   6. the reserved Plans & add-ons slot is hidden while its page does not exist;
//   7. next.config.ts serves the moves; no link in the app still points at an old URL;
//   8. every nav label has an English and a Vietnamese row (nav.messages.ts);
//   9. a content worker sees the AA Catalogue group only;
//  10. AA-1039 R6 slice 3: the AI & spend group (AI gateway, one Spend page) and the Writing Lab (stages, Writing
//      memory moved under it), their moves and the query merge of a move whose target names a tab;
//  11. AA-1039 R6 rest: Tenant 360 as a route (old ?tenant= links replaced), Catalogue settings without its tenant and
//      UI kit tabs (UI kit a Platform page), SEO Intelligence's Spend tab folded into Spend › DataForSEO;
//  12. AA-1039 R6 rest (tech lead, option A): Place cards is an AA Catalogue route with the Cards tab's roles; nobody
//      gains or loses access, the old tab URLs move there in one hop.
//  13. AA-1037 (root ADR 0019 d9 "Screens now"): the Trade group opens Booking admin pages through the app registry
//      (external, same tab, never active), shown to billing.read / tenant.book.account.read; the Tenant 360 "Open in
//      Booking" deep link.
// Run: npm run test:unit.

import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  AA_INTERNAL_TENANT_ID, ADMIN_GROUP_LABELS, ADMIN_IA, ADMIN_ROUTE_MOVES, PRE_R6_ADMIN_ROUTES, adminIaRedirects,
  legacyTenantLinkTarget, resolveAdminRoute, tenant360Href,
} from "./adminIa.ts";
import {
  API_WRITE_RULES, PAGE_RULES, STAFF_ROLES, can, canReadAdminPath, canSeeNavItem, canSeePage, canWriteAdminPath, pageRule,
} from "./rbac.ts";
import { BOOKING_ADMIN_URL, appPageUrl, bookingAgencyInvoicesUrl } from "./apps.ts";

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
// CIS pages (the Trade group's items are pages of another app: test 13)
const visibleCis = visible.filter((i) => !i.external);

test("1. the seven ADR 0019 d13 groups in order, plus Trade (d9) before Platform, every href once", () => {
  assert.deepEqual(ADMIN_IA.map((g) => g.label), [...ADMIN_GROUP_LABELS]);
  // AA-1037: d9 "Screens now" adds the eighth group, Trade, to the seven of d13.
  assert.equal(ADMIN_IA.length, 8);
  assert.deepEqual(ADMIN_IA.map((g) => g.label).filter((l) => l !== "Trade"),
    ["Home", "Tenants", "AA Catalogue", "Tenant content QA", "AI & spend", "Writing Lab", "Platform"]);
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
  for (const it of visibleCis) {
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
  "/admin/lab/[stage]",              // Writing Lab › one stage (the stage bar of /admin/lab)
  "/admin/tenants/view-as",          // AA-1035 R2 slice 2: opened from Tenant 360 › View as partner
  "/admin/tenants/[id]",             // AA-1039 R6 rest: Tenant 360, opened from a Directory row (and ⌘K, Jobs, Budgets)
]);

test("5. every admin page is in the nav or reached from another page", () => {
  const navHrefs = new Set(visibleCis.map((i) => i.href));
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
  const old = ADMIN_ROUTE_MOVES.filter((m) => !m.query).map((m) => m.from.replace(/[.*+?^${}()|[\]\\/-]/g, "\\$&"));
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
  for (const role of ["content"]) {
    const groups = ADMIN_IA.filter((g) => g.items.some((i) => !i.hidden && canSeeNavItem(role, i))).map((g) => g.label);
    assert.deepEqual(groups, ["AA Catalogue"], role);
  }
  // AA-1035: the reviewer alias is gone (CIS 375): the value opens no page at all
  assert.deepEqual(visible.filter((i) => canSeeNavItem("reviewer", i)), []);
  const viewer = visible.filter((i) => canSeeNavItem("viewer", i)).map((i) => i.href);
  assert.ok(viewer.includes("/admin/review") && viewer.includes("/admin/settings"));
  assert.ok(!viewer.includes("/admin/upload"), "the viewer never gets the upload page");
});

test("10. AI & spend and Writing Lab (R6 slice 3): groups, moves, query merge", () => {
  const group = (label) => ADMIN_IA.find((g) => g.label === label).items.map((i) => i.href);
  // AA-1045: + the usage explorer
  assert.deepEqual(group("AI & spend"), ["/admin/ai-gateway", "/admin/spend", "/admin/usage"]);
  assert.deepEqual(group("Writing Lab"), ["/admin/lab", "/admin/lab/writing-memory", "/admin/decisions"]);
  assert.equal(resolveAdminRoute("/admin/writing-memory"), "/admin/lab/writing-memory");
  assert.equal(resolveAdminRoute("/admin/cost-ops?tab=spend&view=dfs"), "/admin/spend?tab=spend&view=dfs");
  assert.equal(resolveAdminRoute("/admin/platform/cost"), "/admin/spend?tab=apps");
  // the target's own tab wins; the request's other keys carry over
  assert.equal(resolveAdminRoute("/admin/platform/cost?month=2026-10"), "/admin/spend?month=2026-10&tab=apps");
  assert.equal(resolveAdminRoute("/admin/platform/cost?tab=x"), "/admin/spend?tab=apps");
  // the old pages are gone, the stage page is admin only
  for (const old of ["/admin/writing-memory", "/admin/cost-ops", "/admin/platform/cost"]) assert.ok(!pageExists(old));
  assert.ok(pageExists("/admin/lab/t9-social") && canSeePage("admin", "/admin/lab/t9-social"));
  for (const role of ["viewer", "content", "reviewer"]) {
    assert.ok(!canSeePage(role, "/admin/lab/t9-social") && !canSeePage(role, "/admin/spend"), role);
  }
  // AA-1035 (S267 fix pass): Writing Lab W is ai_ops' (matrix), R content_lead's and tenant_qa's; never the tenant desk
  for (const role of ["ai_ops", "content_lead", "tenant_qa"]) {
    for (const href of ["/admin/lab", "/admin/lab/writing-memory", "/admin/lab/t9-social"]) {
      assert.ok(canSeePage(role, href), `${role} may open ${href}`);
    }
  }
  for (const role of ["customer_success", "support", "finance"]) assert.ok(!canSeePage(role, "/admin/lab"), role);
});

test("11. R6 rest: Tenant 360 route, Catalogue settings split, SEO Spend folded into Spend", () => {
  // Tenant 360 is a route every tenant desk role may open; the old drawer link goes there with its other keys.
  const t = "11111111-2222-3333-4444-555555555555";
  assert.equal(tenant360Href(t), `/admin/tenants/${t}`);
  assert.equal(tenant360Href(t, "cost"), `/admin/tenants/${t}?tab=cost`);
  assert.ok(pageExists(tenant360Href(t)));
  for (const role of ["admin", "customer_success", "support", "finance"]) assert.ok(canSeePage(role, tenant360Href(t)), role);
  for (const role of ["viewer", "content", "tenant_qa"]) assert.ok(!canSeePage(role, tenant360Href(t)), role);
  assert.equal(legacyTenantLinkTarget(`tenant=${t}`), `/admin/tenants/${t}`);
  assert.equal(legacyTenantLinkTarget(`tenant=${t}&tab=cost`), `/admin/tenants/${t}?tab=cost`);
  assert.equal(legacyTenantLinkTarget(""), null);
  assert.equal(legacyTenantLinkTarget("tab=cost"), null);
  // The static Tenants sub-pages keep winning over the [id] segment.
  for (const sub of ["assignments", "agencies", "data-erasure", "plans", "view-as"]) {
    assert.ok(existsSync(join(APP, "admin", "tenants", sub, "page.tsx")), sub);
  }

  // Catalogue settings: the nav label, its old tenant / UI kit tabs moved (307, one hop, page exists).
  const settings = items.find((i) => i.href === "/admin/settings");
  assert.equal(settings?.label, "Catalogue settings");
  assert.equal(ADMIN_IA.find((g) => g.items.includes(settings))?.label, "AA Catalogue");
  assert.equal(resolveAdminRoute("/admin/settings?tab=tenant"), `/admin/tenants/${AA_INTERNAL_TENANT_ID}?tab=settings`);
  assert.equal(resolveAdminRoute("/admin/settings?tab=ui-kit"), "/admin/platform/ui-kit?tab=ui-kit");
  assert.equal(resolveAdminRoute("/admin/settings?tab=seo"), "/admin/settings?tab=seo");
  assert.equal(resolveAdminRoute("/admin/settings"), "/admin/settings");
  const uiKit = items.find((i) => i.href === "/admin/platform/ui-kit");
  assert.equal(ADMIN_IA.find((g) => g.items.includes(uiKit))?.label, "Platform");
  assert.ok(canSeePage("admin", "/admin/platform/ui-kit") && !canSeePage("content", "/admin/platform/ui-kit"));
  const settingsSrc = read("app/admin/settings/page.tsx");
  assert.doesNotMatch(settingsSrc, /key: "(tenant|ui-kit)"/, "Settings still lists a moved tab");

  // SEO Intelligence: no Spend tab; its tab link lands on Spend › DataForSEO; the page links there only for a role
  // that may open Spend (content_lead reads SEO, holds no spend.read).
  assert.equal(resolveAdminRoute("/admin/seo-intelligence?tab=spend"), "/admin/spend?tab=spend&view=dfs");
  assert.equal(resolveAdminRoute("/admin/seo-intelligence"), "/admin/seo-intelligence");
  const seoSrc = read("app/admin/seo-intelligence/page.tsx");
  assert.doesNotMatch(seoSrc, /key: "spend"/);
  assert.match(seoSrc, /canSeePage\(useAdminIdentity\(\)\.role, "\/admin\/spend"\)/);
  assert.ok(canSeePage("content_lead", "/admin/seo-intelligence") && !canSeePage("content_lead", "/admin/spend"));
  const r = adminIaRedirects().find((x) => x.source === "/admin/seo-intelligence");
  assert.deepEqual(r?.has, [{ type: "query", key: "tab", value: "spend" }]);

  // No code builds the old drawer link any more (lib/adminIa.ts keeps the reader of it).
  const offenders = [];
  const walk = (dir) => {
    for (const d of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, d.name);
      if (d.isDirectory()) { if (d.name !== "node_modules" && !d.name.startsWith(".")) walk(p); continue; }
      if (!/\.(ts|tsx)$/.test(d.name) || p.endsWith(join("lib", "adminIa.ts"))) continue;
      const code = readFileSync(p, "utf8").split("\n").filter((l) => !/^\s*(\/\/|\*|\/\*|\{\/\*)/.test(l)).join("\n");
      if (/admin\/tenants\?tenant=/.test(code)) offenders.push(p.slice(FRONTEND.length));
    }
  };
  walk(APP);
  walk(join(FRONTEND, "lib"));
  assert.deepEqual(offenders, []);
});

test("12. Place cards (option A): its own route, the Cards tab's roles, nobody gains or loses access", () => {
  const cards = "/admin/places/cards";
  assert.ok(pageExists(cards));
  const item = items.find((i) => i.href === cards);
  assert.equal(item?.label, "Place cards");
  assert.equal(ADMIN_IA.find((g) => g.items.includes(item))?.label, "AA Catalogue");
  // The role matrix: exactly the roles that opened Content quality's Cards tab (TENANT_QA), every role checked.
  // (the TENANT_QA rule: every Tenant QA page but Content quality, which customer_success also reads since the AA-1035
  // close-out: its book's lists, no Cards tab)
  const before = (role) => canSeePage(role, "/admin/tenant-qa/compliance");
  for (const role of STAFF_ROLES) {
    assert.equal(canSeePage(role, cards), before(role), `${role}: Place cards = the old Cards tab`);
    assert.equal(canSeePage(role, "/admin/tenant-qa/quality"), before(role) || role === "customer_success",
      `${role}: Content quality = TENANT_QA + customer_success`);
    // The Places pages stay admin only; only the cards route opened up.
    assert.equal(canSeePage(role, "/admin/places"), role === "admin", `${role}: /admin/places`);
    assert.equal(canSeePage(role, "/admin/places/other"), role === "admin", `${role}: other Places sub-pages`);
    // The API the page calls is unchanged (read and write rules of the BFF).
    assert.equal(canReadAdminPath(role, "content-quality/place-cards"), canReadAdminPath(role, "content-quality/pieces"), role);
    assert.equal(canWriteAdminPath(role, "PATCH", "content-quality/place-cards/x"),
      canWriteAdminPath(role, "PATCH", "content-quality/pieces/x"), role);
  }
  assert.deepEqual(STAFF_ROLES.filter((r) => canSeePage(r, cards)), ["admin", "content_lead", "tenant_qa"]);
  // The specific rule sits before the admin-only /admin/places rule (pageRule takes the first match).
  const at = (prefix) => PAGE_RULES.findIndex((r) => r.prefix === prefix);
  assert.ok(at(cards) >= 0 && at(cards) < at("/admin/places"));
  assert.equal(pageRule(cards)?.prefix, cards);
  assert.equal(pageRule("/admin/places")?.prefix, "/admin/places");
  assert.ok(API_WRITE_RULES.some((r) => r.prefix === "content-quality/place-cards"));
  // The old tab URLs: one hop, 307, the other keys kept; the other Content quality tabs stay.
  assert.equal(resolveAdminRoute("/admin/tenant-qa/quality?tab=cards"), "/admin/places/cards?tab=cards");
  assert.equal(resolveAdminRoute("/admin/tenant-qa/quality?tab=cards&status=verified&page=2"),
    "/admin/places/cards?tab=cards&status=verified&page=2");
  assert.equal(resolveAdminRoute("/admin/content-quality?tab=cards"), "/admin/places/cards?tab=cards");
  assert.equal(resolveAdminRoute("/admin/tenant-qa/quality?tab=versions"), "/admin/tenant-qa/quality?tab=versions");
  assert.equal(resolveAdminRoute("/admin/tenant-qa/quality"), "/admin/tenant-qa/quality");
  for (const source of ["/admin/tenant-qa/quality", "/admin/content-quality"]) {
    const r = adminIaRedirects().find((x) => x.source === source && x.destination === cards);
    assert.deepEqual(r?.has, [{ type: "query", key: "tab", value: "cards" }], source);
  }
  // The Cards UI left Content quality.
  const quality = read("app/admin/tenant-qa/quality/page.tsx");
  assert.doesNotMatch(quality, /key: "cards"|PlaceCardsTab|place-cards/);
  assert.match(read("app/admin/places/cards/page.tsx"), /\/api\/admin\/content-quality\/place-cards/);
});

test("13. Trade (AA-1037, ADR 0019 d9): Booking admin pages through the app registry, the billing roles, the deep link", () => {
  const trade = ADMIN_IA.find((g) => g.label === "Trade");
  assert.ok(trade, "the Trade group exists");
  assert.deepEqual(trade.items.map((i) => [i.label, i.href]), [
    ["Booking admin", "/"], ["Agency billing", "/agency-billing"], ["Agency tiers", "/pricing/agency-tiers"],
    ["B2B desk", "/b2b-desk"],
  ]);
  for (const it of trade.items) {
    assert.equal(it.external, "booking", `${it.label} opens Booking admin`);
    assert.ok(!it.hidden);
    // built only through the registry: the Booking admin origin plus the item's path
    assert.equal(appPageUrl("booking", it.href), it.href === "/" ? `${BOOKING_ADMIN_URL}/` : `${BOOKING_ADMIN_URL}${it.href}`);
  }
  // Only the Trade group is external; no CIS item names another app.
  assert.deepEqual(items.filter((i) => i.external).map((i) => i.label), trade.items.map((i) => i.label));

  // Who sees it: billing.read or tenant.book.account.read (the Tenant 360 › Billing roles), never the others.
  const sees = STAFF_ROLES.filter((r) => trade.items.every((i) => canSeeNavItem(r, i)));
  assert.deepEqual(sees, STAFF_ROLES.filter((r) => can(r, "billing.read") || can(r, "tenant.book.account.read")));
  assert.deepEqual([...sees].sort(), ["admin", "customer_success", "finance"]);
  for (const role of ["support", "viewer", "content", "content_lead", "tenant_qa", "ai_ops", "reviewer", "", null]) {
    assert.ok(trade.items.every((i) => !canSeeNavItem(role, i)), String(role));
  }
  // An external item with no capabilities shows to nobody; a CIS item still follows its PAGE_RULE.
  assert.equal(canSeeNavItem("admin", { href: "/x", external: "booking" }), false);
  assert.equal(canSeeNavItem("admin", { href: "/admin/jobs" }), canSeePage("admin", "/admin/jobs"));

  // The registry refuses anything that is not a path of the app (no open redirect through a nav entry).
  for (const bad of ["https://elsewhere.example", "//elsewhere.example", "agency-billing", "/\\elsewhere", ""]) {
    assert.throws(() => appPageUrl("booking", bad), /not an app path/, bad);
  }

  // Tenant 360 › Billing › "Open in Booking": Agency billing, Invoices tab, this tenant (the id is encoded).
  const t = "11111111-2222-3333-4444-555555555555";
  assert.equal(bookingAgencyInvoicesUrl(t), `${BOOKING_ADMIN_URL}/agency-billing?tab=invoices&tenant=${t}`);
  assert.equal(bookingAgencyInvoicesUrl("a&b=c"), `${BOOKING_ADMIN_URL}/agency-billing?tab=invoices&tenant=a%26b%3Dc`);

  // adminNav.tsx: external entries are built through the registry, never active, followed in the same tab.
  const nav = read("app/_kit/adminNav.tsx");
  assert.match(nav, /it\.external \? appPageUrl\(it\.external, it\.href\)/);
  assert.match(nav, /canSeeNavItem\(role, n\)/);
  assert.match(nav, /if \(entry\.external\) continue;/);
  assert.match(nav, /window\.location\.assign\(entry\.href\)/);
  assert.doesNotMatch(nav, /target="_blank"|window\.open\(/, "same tab");
  assert.match(read("app/_kit/Sidebar.tsx"), /followExternal\(entry\)/);
  assert.match(read("app/_kit/CommandPalette.tsx"), /followExternal\(row\)/);
  // The Booking URL is built in lib/apps.ts only.
  assert.doesNotMatch(read("app/admin/tenants/_components/Tenant360BookingBilling.tsx"), /agency-billing/);
});
