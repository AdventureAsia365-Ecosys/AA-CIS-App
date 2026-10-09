// tests/e2e/smoke/ui-smoke.spec.ts
// AA-732 — UI smoke run against a PR's Vercel preview. For each key page, logged in as an admin:
//   • navigation returns an OK status
//   • 0 console errors (CONSOLE_ALLOWLIST is empty by default — see smoke-helpers.ts)
//   • no 4xx/5xx on the page's own SAME-ORIGIN /api/ calls (failure message carries URL + status
//     + first 300 chars of the body — S212: read the body, a 500 once looked like a 401)
//   • the main content shows ≥1 kit/table row OR the kit empty state (not a blank/broken shell)
// and screenshots are captured at desktop 1440 + mobile 390, light + dark.
//
// The admin login happens once in global-setup.ts (storageState). Each test opens its OWN context
// from that state, so the tests are independent — one red page does not skip the others.
// This spec is its own Playwright project ("smoke"), so legacy specs are not run by CI.
import { test, expect } from '@playwright/test';
import {
  ADMIN_STATE,
  TENANT_STATE,
  baseUrl,
  installBypassRoute,
  ensureResultsDir,
  ensureAuthDir,
  watchPage,
  hasContentOrEmptyState,
  waitForSkeletonGone,
  assertNavOk,
  describeApiFailures,
  screenshotMatrix,
  type PageSpec,
} from './smoke-helpers';

ensureResultsDir();

// ── Admin pages (routes verified under frontend/app/admin/). Add a page = add one line here. ──
const ADMIN_PAGES: PageSpec[] = [
  { id: 'overview', label: 'Overview', path: '/admin/overview' },
  { id: 'master-content', label: 'Master Content', path: '/admin/master-content' },
  { id: 'review', label: 'Review Queue', path: '/admin/review' },
  { id: 'seo-intelligence', label: 'SEO Intelligence', path: '/admin/seo-intelligence' },
  { id: 'jobs', label: 'Jobs', path: '/admin/jobs' },
  { id: 'tenants', label: 'Tenants', path: '/admin/tenants' },
  { id: 's1-rewrite', label: 'S1 Rewrite', path: '/admin/s1-rewrite' },
];

// ── Portal pages (tenant). The real tenant login takes an API key, so this is gated on
// E2E_TENANT_API_KEY. No tenant test account exists yet (AA-741), so the block skips when absent. ──
const PORTAL_PAGES: PageSpec[] = [
  { id: 't7-planning', label: 'Social Content', path: '/portal/t7-planning' },
  { id: 't10-review', label: 'My Content', path: '/portal/t10-review' },
  { id: 't11-publish', label: 'Publish', path: '/portal/t11-publish' },
];

async function runPageCheck(
  browser: import('@playwright/test').Browser,
  storageStatePath: string,
  spec: PageSpec,
): Promise<void> {
  const context = await browser.newContext({
    baseURL: baseUrl(),
    storageState: storageStatePath,
    viewport: { width: 1440, height: 900 },
  });
  await installBypassRoute(context);
  const page = await context.newPage();
  const watcher = watchPage(page);
  try {
    await assertNavOk(page, spec.path);
    await waitForSkeletonGone(page);

    // Let every in-flight same-origin /api/ body read complete before asserting.
    await watcher.settle();

    expect(
      watcher.apiFailures,
      `${spec.label}: failing same-origin /api/ responses:\n${describeApiFailures(watcher.apiFailures)}`,
    ).toEqual([]);

    const ok = await hasContentOrEmptyState(page);
    expect(ok, `${spec.label}: no table rows and no empty state — page looks blank/broken`).toBe(true);

    expect(
      watcher.consoleErrors,
      `${spec.label}: console errors:\n${watcher.consoleErrors.map((e) => `  ${e.text}`).join('\n')}`,
    ).toEqual([]);

    // Screenshots: desktop + mobile, light + dark (on the same, already-authenticated page).
    await screenshotMatrix(page, spec.path, spec.id);
  } finally {
    await context.close();
  }
}

test.describe('UI smoke — admin', () => {
  for (const spec of ADMIN_PAGES) {
    test(`admin: ${spec.label} (${spec.path})`, async ({ browser }) => {
      await runPageCheck(browser, ADMIN_STATE, spec);
    });
  }
});

const hasTenantKey = Boolean(process.env.E2E_TENANT_API_KEY);

test.describe('UI smoke — portal', () => {
  // The real tenant login (frontend/app/tenant-login → /api/auth/tenant-login) takes an API key.
  test.skip(
    !hasTenantKey,
    'No tenant test account yet (AA-741): set E2E_TENANT_API_KEY to enable the portal smoke.',
  );

  test.beforeAll(async ({ browser }) => {
    // TODO(AA-741): tenant test account — once it exists, log in with the API key and persist a
    // tenant storageState. The real form has a single API-key input (type=password).
    ensureAuthDir();
    const context = await browser.newContext({ baseURL: baseUrl() });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/tenant-login');
      const apiKey = process.env.E2E_TENANT_API_KEY!;
      await page.fill('input[type="password"]', apiKey);
      await page.click('button:has-text("Access Portal")');
      await page.waitForURL(/\/portal/, { timeout: 20000 });
      await context.storageState({ path: TENANT_STATE });
    } finally {
      await context.close();
    }
  });

  for (const spec of PORTAL_PAGES) {
    test(`portal: ${spec.label} (${spec.path})`, async ({ browser }) => {
      await runPageCheck(browser, TENANT_STATE, spec);
    });
  }
});
