// tests/e2e/smoke-portal/portal-smoke.spec.ts
// AA-885 — the tenant portal page smoke (moved out of tests/e2e/smoke/ui-smoke.spec.ts when the
// portal became its own app). For each page, logged in as the test tenant (storage state from
// auth.setup.ts), the shared runPageCheck asserts: OK navigation, no failing same-origin /api/ call,
// no console error, not blank. The portal adds a 390px no-sideways-scroll check and screenshots at
// 1440 and 390 (light; the portal stays light).
//
// "Not blank": pages built on the kit show a DataTable row or the kit empty state; the others pass
// on their page title (`main :is(h1, h2)`; every portal page renders one). Data errors on those pages
// still fail through the /api/ and console checks.
import { test, expect } from '@playwright/test';
import { baseUrl, installBypassRoute, runPageCheck, type PageSpec } from '../smoke/smoke-helpers';
import {
  NO_KEY_REASON,
  TENANT_STATE,
  assertPortalNoOverflowAt390,
  ensurePortalResultsDir,
  hasTenantState,
  portalScreenshots,
} from './portal-helpers';

ensurePortalResultsDir();

// Page title: PageHeader renders an h1; the older tabs (Brand Identity, Social Content, Marketplace) an h2.
const RENDERED = 'main :is(h1, h2)';

// Add a page = add one line here (the portal Sidebar + the deep-link-only pages).
const PORTAL_PAGES: PageSpec[] = [
  { id: 'dashboard', label: 'Dashboard', path: '/portal/dashboard', readySelector: RENDERED },
  { id: 't1-rewrite', label: 'Browse Tours', path: '/portal/t1-rewrite', readySelector: RENDERED },
  { id: 't4-pool', label: 'My Catalog Tours', path: '/portal/t4-pool', readySelector: RENDERED },
  { id: 't0-brand', label: 'Brand Identity', path: '/portal/t0-brand', readySelector: RENDERED },
  { id: 't7-planning', label: 'Social Content', path: '/portal/t7-planning', readySelector: RENDERED },
  { id: 't10-review', label: 'My Content', path: '/portal/t10-review', readySelector: RENDERED },
  { id: 't11-publish', label: 'Publish', path: '/portal/t11-publish', readySelector: RENDERED },
  { id: 'marketplace', label: 'Marketplace', path: '/portal/marketplace', readySelector: RENDERED },
  { id: 'api', label: 'API Access', path: '/portal/api', readySelector: RENDERED },
  { id: 'activity', label: 'Activity Log', path: '/portal/activity', readySelector: RENDERED },
  { id: 'billing', label: 'Billing', path: '/portal/billing', readySelector: RENDERED },
  { id: 'settings', label: 'Settings', path: '/portal/settings', readySelector: RENDERED },
  { id: 'help-facebook', label: 'Connect your Facebook Page', path: '/portal/help/facebook', readySelector: RENDERED },
];

test.describe('UI smoke — portal pages', () => {
  test.skip(() => !hasTenantState(), NO_KEY_REASON);

  for (const spec of PORTAL_PAGES) {
    test(`portal: ${spec.label} (${spec.path})`, async ({ browser }) => {
      await runPageCheck(browser, TENANT_STATE, spec, {
        afterChecks: (page, s) => assertPortalNoOverflowAt390(page, s.label),
        screenshots: portalScreenshots,
      });
    });
  }

  // AA-831 (S239 UI verify D) — at 390px the longest portal breadcrumb ("Connect your Facebook
  // Page") was hard-clipped next to the bell. The kit Breadcrumb must end the title in an ellipsis
  // and stay inside the top bar, with no page-level horizontal overflow. Every wait is bounded.
  test('portal: top-bar breadcrumb truncates with an ellipsis at 390px', async ({ browser }) => {
    const context = await browser.newContext({
      baseURL: baseUrl(),
      storageState: TENANT_STATE,
      viewport: { width: 390, height: 844 },
    });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/portal/help/facebook', { waitUntil: 'domcontentloaded' });
      const crumb = page.getByTestId('topbar-breadcrumb');
      const title = page.getByTestId('topbar-breadcrumb-title');
      await expect(title).toHaveText('Connect your Facebook Page', { timeout: 15000 });
      // The title is already in the SSR HTML, but the portal shell only switches to its mobile
      // layout (hamburger, 16px padding, no search box) after hydration, when its matchMedia
      // effect sets narrow=true. Wait for the hamburger so we measure the 390px layout, not the
      // desktop one squeezed into 390px.
      await expect(page.getByRole('button', { name: 'Open menu' })).toBeVisible({ timeout: 15000 });

      const m = await page.evaluate(() => {
        const t = document.querySelector<HTMLElement>('[data-testid="topbar-breadcrumb-title"]')!;
        const c = document.querySelector<HTMLElement>('[data-testid="topbar-breadcrumb"]')!;
        const header = c.closest('header')!;
        const cs = getComputedStyle(t);
        const cr = c.getBoundingClientRect();
        const hr = header.getBoundingClientRect();
        // The first header control after the crumb (search box or bell) must not be overlapped.
        const after = Array.from(header.querySelectorAll<HTMLElement>('button'))
          .map((b) => b.getBoundingClientRect())
          .filter((r) => r.width > 0 && r.left >= cr.left + 1)
          .map((r) => r.left);
        return {
          textOverflow: cs.textOverflow,
          whiteSpace: cs.whiteSpace,
          overflowX: cs.overflowX,
          titleScroll: t.scrollWidth,
          titleClient: t.clientWidth,
          crumbRight: Math.round(cr.right),
          headerRight: Math.round(hr.right),
          nextControlLeft: after.length ? Math.round(Math.min(...after)) : null,
          scrollWidth: document.documentElement.scrollWidth,
          innerWidth: window.innerWidth,
        };
      });

      await expect(crumb).toBeVisible();
      expect(m.textOverflow, 'breadcrumb title must use text-overflow: ellipsis').toBe('ellipsis');
      expect(m.whiteSpace).toBe('nowrap');
      expect(m.overflowX).toBe('hidden');
      // A crumb squeezed to almost nothing would satisfy every bound below; the title must keep
      // usable width (at 390px the mobile bar leaves roughly 180px for it).
      expect(m.titleClient, `breadcrumb title has no usable width (clientWidth ${m.titleClient})`).toBeGreaterThan(40);
      expect(m.crumbRight, 'breadcrumb must stay inside the top bar').toBeLessThanOrEqual(m.headerRight);
      if (m.nextControlLeft !== null) {
        expect(m.crumbRight, 'breadcrumb must not run under the next top-bar control').toBeLessThanOrEqual(m.nextControlLeft);
      }
      expect(
        m.scrollWidth,
        `page-level horizontal overflow at 390px (scrollWidth ${m.scrollWidth} > innerWidth ${m.innerWidth})`,
      ).toBeLessThanOrEqual(m.innerWidth + 1);
      // Informational: at 390px this title is expected to be truncated (scrollWidth > clientWidth).
      test.info().annotations.push({
        type: 'breadcrumb',
        description: `title scrollWidth=${m.titleScroll} clientWidth=${m.titleClient} truncated=${m.titleScroll > m.titleClient}`,
      });
    } finally {
      await context.close();
    }
  });
});
