// tests/e2e/smoke/ui-smoke.spec.ts
// AA-732 — UI smoke run against a PR's Vercel preview. For each key page, logged in as an admin:
//   • navigation returns an OK status
//   • 0 console errors (CONSOLE_ALLOWLIST is empty by default — see smoke-helpers.ts)
//   • no 4xx/5xx on the page's own same-origin /api/ calls (failure message carries URL + status
//     + first 300 chars of the body — S212: read the body, a 500 once looked like a 401)
//   • the main content shows ≥1 table row OR the kit empty state (not a blank/broken shell)
// and screenshots are captured at desktop 1440 + mobile 390, light + dark.
//
// This spec is its own Playwright project (see playwright.config.ts, project "smoke"), so the
// legacy live-verify specs are not run by CI.
import { test, expect } from '@playwright/test';
import { loginAsAdmin } from '../helpers/auth';
import {
  SMOKE_RESULTS_DIR,
  STORAGE_STATE,
  bypassHeaders,
  ensureResultsDir,
  watchPage,
  hasContentOrEmptyState,
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

// ── Portal pages (tenant). Env-gated: no tenant test account exists yet (AA-741), so the whole
// block skips with a clear reason when E2E_TENANT_EMAIL / E2E_TENANT_PASSWORD are absent. ──
const PORTAL_PAGES: PageSpec[] = [
  { id: 't7-planning', label: 'Social Content', path: '/portal/t7-planning' },
  { id: 't10-review', label: 'My Content', path: '/portal/t10-review' },
  { id: 't11-publish', label: 'Publish', path: '/portal/t11-publish' },
];

test.describe('UI smoke — admin', () => {
  test.describe.configure({ mode: 'serial' });

  // Log in once; reuse the storage state for the per-page checks and the screenshot matrix.
  test.beforeAll(async ({ browser }) => {
    const context = await browser.newContext({ extraHTTPHeaders: bypassHeaders() });
    const page = await context.newPage();
    await loginAsAdmin(page);
    await page.waitForURL(/\/admin\//, { timeout: 20000 });
    await context.storageState({ path: STORAGE_STATE });
    await context.close();
  });

  for (const spec of ADMIN_PAGES) {
    test(`admin: ${spec.label} (${spec.path})`, async ({ browser }) => {
      const context = await browser.newContext({
        storageState: STORAGE_STATE,
        extraHTTPHeaders: bypassHeaders(),
        viewport: { width: 1440, height: 900 },
      });
      const page = await context.newPage();
      const { consoleErrors, apiFailures } = watchPage(page);
      try {
        await assertNavOk(page, spec.path, bypassHeaders());

        expect(
          apiFailures,
          `${spec.label}: failing same-origin /api/ responses:\n${describeApiFailures(apiFailures)}`,
        ).toEqual([]);

        const ok = await hasContentOrEmptyState(page);
        expect(ok, `${spec.label}: no table rows and no empty state — page looks blank/broken`).toBe(true);

        expect(
          consoleErrors,
          `${spec.label}: console errors:\n${consoleErrors.map((e) => `  ${e.text}`).join('\n')}`,
        ).toEqual([]);
      } finally {
        await context.close();
      }

      // Screenshots: desktop + mobile, light + dark.
      await screenshotMatrix(browser, spec.path, spec.id, bypassHeaders());
    });
  }
});

const hasTenantCreds = Boolean(process.env.E2E_TENANT_EMAIL && process.env.E2E_TENANT_PASSWORD);

test.describe('UI smoke — portal', () => {
  test.describe.configure({ mode: 'serial' });
  test.skip(
    !hasTenantCreds,
    'No tenant test account yet (AA-741): set E2E_TENANT_EMAIL + E2E_TENANT_PASSWORD to enable.',
  );

  // NOTE: the real tenant login form (frontend/app/tenant-login) takes an API key, not an
  // email+password. When the tenant test account lands (AA-741) this login step must match the
  // then-current form fields; see result.md Open questions.
  test.beforeAll(async ({ browser }) => {
    const context = await browser.newContext({ extraHTTPHeaders: bypassHeaders() });
    const page = await context.newPage();
    await page.goto('/tenant-login');
    // Placeholder for the real tenant login once AA-741 defines the account + form contract.
    await context.storageState({ path: `${SMOKE_RESULTS_DIR}/tenant-state.json` });
    await context.close();
  });

  for (const spec of PORTAL_PAGES) {
    test(`portal: ${spec.label} (${spec.path})`, async ({ browser }) => {
      const context = await browser.newContext({
        storageState: `${SMOKE_RESULTS_DIR}/tenant-state.json`,
        extraHTTPHeaders: bypassHeaders(),
        viewport: { width: 1440, height: 900 },
      });
      const page = await context.newPage();
      const { consoleErrors, apiFailures } = watchPage(page);
      try {
        await assertNavOk(page, spec.path, bypassHeaders());
        expect(
          apiFailures,
          `${spec.label}: failing same-origin /api/ responses:\n${describeApiFailures(apiFailures)}`,
        ).toEqual([]);
        const ok = await hasContentOrEmptyState(page);
        expect(ok, `${spec.label}: no table rows and no empty state`).toBe(true);
        expect(
          consoleErrors,
          `${spec.label}: console errors:\n${consoleErrors.map((e) => `  ${e.text}`).join('\n')}`,
        ).toEqual([]);
      } finally {
        await context.close();
      }
      await screenshotMatrix(browser, spec.path, spec.id, bypassHeaders());
    });
  }
});
