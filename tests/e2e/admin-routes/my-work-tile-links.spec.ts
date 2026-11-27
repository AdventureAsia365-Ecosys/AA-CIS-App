// tests/e2e/admin-routes/my-work-tile-links.spec.ts
// AA-1190 (S296 live check) — every My work tile opens its target page WITH the tile's filter after a CLIENT-SIDE
// click (the tile is a next/link <Link>), not only on a direct load. The live check found the Review Queue
// (?policy=none, ?policy=elephant_riding) and Data erasure (?tab=offboarding) opening unfiltered on a click while a
// typed URL filtered correctly: they read window.location.search once in a lazy useState, and on a client navigation
// the new page renders before the browser URL changes. lib/placeCardsView.test.mjs guards the source pattern; this
// spec proves the behaviour in a real browser.
//
// Same project and setup as admin-routes.spec.ts (the runner's own `next start` against stub-api.mjs, the unsigned
// stand-in administrator session). My work's summary is answered in the browser from the fixture below (one tile per
// key of lib/myWork.ts MY_WORK_TILES, each with the backend's filter, api/routers/admin_my_work.py); the Review Queue
// list is answered empty so its toolbar renders, Data erasure gets empty settings so its tabs render; every other read
// goes to the stub (503, the page's error state).
//
// For each tile with a filter: click it on My work, record the page's /api/* reads, then load the same URL directly
// and record again. Every direct-load read that carries a filter pair (key=value of the tile's filter) must also be
// made after the click; a filter that never reaches an API read (a tab, a client-side list filter) is checked on
// screen instead (UI_CHECKS). A tile whose filter is visible neither way fails the run, so a new tile needs one.
// The last test walks Back / Forward on the Review Queue: the page follows the URL, not its first render.

import { expect, test, type Page } from "@playwright/test";
import { MY_WORK_TILES, tileHref, type MyWorkKey } from "../../../frontend/lib/myWork";

const STAND_IN_TOKEN = "admin-route-check-stand-in";
const QUIET_MS = 1_200;
const SETTLE_CAP_MS = 8_000;

// The backend's filter of each tile (api/routers/admin_my_work.py; the settings tile adds the app of its oldest
// proposal). Fixed values only.
const FILTERS: Record<MyWorkKey, Record<string, string>> = {
  settings_to_approve: { pending: "1", app: "cis" },
  compliance_exceptions_to_approve: { tab: "exceptions" },
  erasure_to_approve: { tab: "offboarding" },
  review_waiting: { policy: "none" },
  supply_drafts: { has_draft: "1" },
  supply_no_description: { status: "draft" },
  aa_content_drafts: { status: "draft" },
  compliance_holds: {},
  ops_open_problems: {},
  review_held_by_policy: { policy: "elephant_riding" },
  place_cards_to_review: { status: "auto_checked" },
};

const SUMMARY = {
  role: "admin",
  generated_at: "2026-11-27T08:00:00Z",
  tiles: MY_WORK_TILES.map((t) => ({
    key: t.key, capability: t.capability, group: "work", counts_toward_total: true, filter: FILTERS[t.key],
    available: true, count: 3, oldest_at: "2026-11-20T08:00:00Z",
  })),
};

// Data erasure draws its tabs only once its settings have loaded (an invented, empty registry).
const ERASURE_SETTINGS = {
  kind: "data_erasure", apply_enabled: false, tenant_retention_days: 30, dry_run_max_age_hours: 24, tenant_tables: [],
  traveller_scans: [], tenant_checklist: [], traveller_checklist: [],
};

const EMPTY_REVIEW_PAGE = { data: [], pagination: { total: 0, page: 1, page_size: 25 }, facets: { countries: [], policy_reviews: 0 } };

const policySelect = (page: Page) => page.getByRole("combobox", { name: "Elephant riding policy" });
const selectedTab = (page: Page, testId: string) => page.locator(`[data-testid="${testId}"] [role="tab"][aria-selected="true"]`);

// What the target page shows for a filter that never reaches an API read.
const UI_CHECKS: Partial<Record<MyWorkKey, (page: Page) => Promise<void>>> = {
  erasure_to_approve: async (page) => { await expect(selectedTab(page, "data-erasure-tabs")).toHaveText(/offboard/i); },
  compliance_exceptions_to_approve: async (page) => {
    await expect(selectedTab(page, "compliance-tabs")).toHaveText(/exception/i);
  },
  settings_to_approve: async (page) => { await expect(page.getByTestId("settings-pending-only")).toBeVisible(); },
  review_waiting: async (page) => { await expect(policySelect(page)).toHaveValue("none"); },
  review_held_by_policy: async (page) => { await expect(policySelect(page)).toHaveValue("elephant_riding"); },
};

test.describe.configure({ mode: "parallel" });

test.beforeEach(async ({ context, baseURL, page }) => {
  const url = baseURL ?? "http://localhost:3001";
  await context.addCookies([
    { name: "cis_admin_token", value: STAND_IN_TOKEN, url, httpOnly: true, sameSite: "Lax" },
    { name: "cis_role", value: "admin", url, sameSite: "Lax" },
    { name: "cis_user", value: "Route%20Check%20Admin", url, sameSite: "Lax" },
  ]);
  await page.route("**/api/admin/my-work/summary*", (r) => r.fulfill({ status: 200, json: SUMMARY }));
  await page.route("**/api/admin/review-queue?*", (r) => r.fulfill({ status: 200, json: EMPTY_REVIEW_PAGE }));
  await page.route("**/api/admin/job-runner/erasure", (r) => r.fulfill({ status: 200, json: ERASURE_SETTINGS }));
});

/** Records the same-origin /api/* reads (path + query) and waits until none has been in flight for a while. */
function recorder(page: Page, origin: string) {
  const seen: string[] = [];
  let inflight = 0;
  let last = Date.now();
  let on = false;
  const isApi = (u: string) => u.startsWith(origin) && new URL(u).pathname.startsWith("/api/");
  page.on("request", (r) => {
    if (!isApi(r.url())) return;
    inflight++;
    last = Date.now();
    if (on) { const u = new URL(r.url()); seen.push(`${u.pathname}${u.search}`); }
  });
  const done = (r: { url(): string }) => { if (isApi(r.url())) { inflight = Math.max(0, inflight - 1); last = Date.now(); } };
  page.on("requestfinished", done);
  page.on("requestfailed", done);
  return {
    // the quiet window starts now: a page whose first read only starts after hydration is still waited for
    start() { seen.length = 0; on = true; last = Date.now(); },
    async settle(): Promise<string[]> {
      const t0 = Date.now();
      while (Date.now() - t0 < SETTLE_CAP_MS && (inflight > 0 || Date.now() - last < QUIET_MS)) await page.waitForTimeout(200);
      on = false;
      return [...seen];
    },
  };
}

/** The reads that carry one of the filter's key=value pairs. */
function filtered(reads: string[], filter: Record<string, string>): string[] {
  const pairs = Object.entries(filter);
  return reads.filter((r) => {
    const q = new URL(r, "http://x").searchParams;
    return pairs.some(([k, v]) => q.get(k) === v);
  });
}

async function openMyWork(page: Page) {
  await page.goto("/admin/my-work", { waitUntil: "load" });
  await expect(page.getByTestId("my-work-total")).toBeVisible({ timeout: 20_000 });
}

test("the fixture has a filter for every tile key", () => {
  expect(Object.keys(FILTERS).sort()).toEqual(MY_WORK_TILES.map((t) => t.key).sort());
});

for (const def of MY_WORK_TILES) {
  const filter = FILTERS[def.key];
  if (!Object.keys(filter).length) continue; // opens the page's default view: nothing to carry
  const href = tileHref(def, filter);

  test(`tile ${def.key}: a click opens ${href} filtered`, async ({ page, baseURL }) => {
    const origin = new URL(baseURL ?? "http://localhost:3001").origin;
    const rec = recorder(page, origin);
    await openMyWork(page);
    const link = page.getByTestId(`my-work-tile-${def.key}`);
    await expect(link).toHaveAttribute("href", href);

    // the client-side click
    rec.start();
    await link.click();
    await page.waitForURL((u) => `${u.pathname}${u.search}` === href, { timeout: 20_000 });
    const clicked = await rec.settle();
    const ui = UI_CHECKS[def.key];
    if (ui) await ui(page);

    // the same URL loaded directly (the reference: it always filtered)
    rec.start();
    await page.goto(href, { waitUntil: "load" });
    const direct = await rec.settle();
    if (ui) await ui(page);

    const wanted = [...new Set(filtered(direct, filter))];
    expect(wanted.length > 0 || !!ui, `${href}: the filter reaches no API read and the tile has no UI check`).toBe(true);
    for (const r of wanted) expect(clicked, `${href}: a direct load reads ${r}; after the click it did not`).toContain(r);
  });
}

test("Review Queue follows Back / Forward and a link without a filter", async ({ page, baseURL }) => {
  const origin = new URL(baseURL ?? "http://localhost:3001").origin;
  const rec = recorder(page, origin);
  await openMyWork(page);
  await page.getByTestId("my-work-tile-review_waiting").click();
  await page.waitForURL(/\/admin\/review\?policy=none$/);
  await expect(policySelect(page)).toHaveValue("none");

  // another link to the same page (the router pushes; the page stays mounted)
  rec.start();
  await page.evaluate(() => window.history.pushState(null, "", "/admin/review?policy=elephant_riding"));
  await expect(policySelect(page)).toHaveValue("elephant_riding");
  expect(await rec.settle()).toContain("/api/admin/review-queue?status=pending&page=1&page_size=25&policy=elephant_riding");

  await page.goBack();
  await page.waitForURL(/\/admin\/review\?policy=none$/);
  await expect(policySelect(page)).toHaveValue("none");
  await page.goForward();
  await page.waitForURL(/policy=elephant_riding$/);
  await expect(policySelect(page)).toHaveValue("elephant_riding");

  // a person's pick holds on its URL; the menu link without a filter shows every review again
  await policySelect(page).selectOption("none");
  await expect(policySelect(page)).toHaveValue("none");
  await page.evaluate(() => window.history.pushState(null, "", "/admin/review"));
  await expect(policySelect(page)).toHaveValue("all");
});
