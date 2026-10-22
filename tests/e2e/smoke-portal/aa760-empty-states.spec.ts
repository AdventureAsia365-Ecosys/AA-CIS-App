// tests/e2e/smoke-portal/aa760-empty-states.spec.ts
// AA-760 — the portal's empty states, captured and asserted without an empty tenant ($0, nothing written).
//
// Logged in as the test tenant (storage state from auth.setup.ts, like portal-smoke.spec.ts), the page's own list
// calls are answered in the browser with EMPTY payloads (Playwright route interception), so the real components
// render their real empty states. Nothing reaches the API for these calls: no read of tenant data, no Slate
// proposal job (GET /v1/slate can enqueue one), no metering. Every other call (session, quota, pool total) goes
// through unchanged. Any write the page tried would be aborted and fails the test.
//
// Per page, in light and dark: the empty-state copy and its call to action render, nothing is left loading
// (`aria-busy`), no "0 tours" / "0 topics ready" count is shown, no console error, no failing /api/ call;
// screenshots at 1440 (whole page) and 400.
import { test, expect, type BrowserContext, type Page, type Route } from '@playwright/test';
import { baseUrl, installBypassRoute, watchPage, describeApiFailures } from '../smoke/smoke-helpers';
import {
  NO_KEY_REASON,
  PORTAL_RESULTS_DIR,
  TENANT_STATE,
  ensurePortalResultsDir,
  fitViewportToMain,
  hasTenantState,
  waitForPortalData,
} from './portal-helpers';

ensurePortalResultsDir();

const CHANNELS: { key: string; onDemand: boolean }[] = [
  { key: 'blog', onDemand: false },
  { key: 'linkedin', onDemand: false },
  { key: 'facebook', onDemand: false },
  { key: 'instagram', onDemand: false },
  { key: 'tiktok', onDemand: false },
  { key: 'email', onDemand: true },
  { key: 'landing_page', onDemand: true },
  { key: 'ads', onDemand: true },
];

function billingMonth(): string {
  const d = new Date();
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
}

/** The empty answers, by BFF path (`/api/tenant/<api path>`). Shapes follow the API routes the pages read. */
function emptyBody(pathname: string): unknown | undefined {
  switch (pathname) {
    case '/api/tenant/v1/tours/my-versions': // v1_tours.py my-versions
      return { data: [], pagination: { page: 1, page_size: 100, total: 0, pages: 0 } };
    case '/api/tenant/v1/marketplace': // v1_marketplace.py
      return { tenant_id: '00000000-0000-0000-0000-000000000000', posts_per_week: null, tours: [], total_tours: 0 };
    case '/api/tenant/v1/slate': // v1_planning.py get_slate — no proposal yet
      return {
        channels: Object.fromEntries(
          CHANNELS.map((c) => [c.key, { channel: c.key, on_demand: c.onDemand, eligible_count: 0, subjects: [] }]),
        ),
        posts_per_week: 3,
        unmatched_markets: [],
        refresh: { status: 'none', refreshing: false, finished_at: null },
      };
    case '/api/tenant/v1/billing': // v1_tours.py get_my_billing — a plan, nothing used yet
      return {
        tenant_name: null, plan_tier: 'starter', plan_attached: true, billing_month: billingMonth(),
        tours_quota_monthly: 50, api_calls_quota_monthly: 5000, price_usd_monthly: 99,
        tours_rewritten: 0, api_calls_used: 0, quota_tours_pct: 0, quota_calls_pct: 0,
        tours_overage: 0, overage_usd: 0, overage_rate_usd_per_tour: 2, rate_limit_rpm: 60,
        plans: [], daily: [], activity: [],
      };
    case '/api/tenant/v1/content-writing/reviews': // Getting started: no post written
      return { data: [] };
    case '/api/tenant/admin/brand-identity': // Getting started: no brand voice yet
      return {};
    case '/api/tenant/v1/integrations/wordpress': // Getting started: not connected
      return { connected: false };
    default:
      return undefined;
  }
}

/** Installs the empty answers on the context; returns the writes it refused (must stay empty). */
async function installEmptyTenant(context: BrowserContext, origin: string): Promise<string[]> {
  const refusedWrites: string[] = [];
  // Registered after installBypassRoute, so it runs first; `fallback()` hands everything else to the bypass route.
  await context.route('**/api/**', async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin !== origin) return route.fallback();
    if (req.method() !== 'GET' && req.method() !== 'HEAD') {
      refusedWrites.push(`${req.method()} ${url.pathname}`);
      return route.abort();
    }
    const body = emptyBody(url.pathname);
    if (body === undefined) return route.fallback();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
  });
  return refusedWrites;
}

type EmptyPage = {
  id: string;
  label: string;
  path: string;
  /** The empty state's own copy (visible). */
  copy: string | RegExp;
  /** Where the copy and its call to action live (scopes the CTA check to this empty state). */
  scope: string;
  /** Extra checks for the page (e.g. the Slate's Update topics button). */
  extra?: (page: Page) => Promise<void>;
};

// The kit DataTable's empty state (My Tours) and the portal's own EmptyState (ui.tsx: Tour overview, the Slate).
const KIT_EMPTY = '[data-testid="kit-empty-state"]';
const PORTAL_EMPTY = '[data-testid="portal-empty-state"]';

const EMPTY_PAGES: EmptyPage[] = [
  {
    id: 'dashboard', label: 'Dashboard', path: '/portal/dashboard',
    copy: 'No activity yet — browse tours to rewrite your first one',
    scope: '[data-testid="dashboard-activity-empty"]',
    // The getting-started card is the dashboard's other empty-tenant guide.
    extra: async (page) => {
      await expect(page.getByText('Get started · 0 of 4')).toBeVisible({ timeout: 15000 });
    },
  },
  {
    id: 'tours-mine', label: 'My Tours', path: '/portal/tours/mine',
    copy: 'No rewrites yet', scope: KIT_EMPTY,
  },
  {
    id: 'tours-marketplace', label: 'Tour overview', path: '/portal/tours/marketplace',
    copy: 'Once you rewrite your first tour from Browse Tours, it shows up here.', scope: PORTAL_EMPTY,
  },
  {
    id: 'create-social', label: 'Social Posts (Slate)', path: '/portal/create/social',
    copy: 'We add topics here as your tours are written and updated.', scope: PORTAL_EMPTY,
    extra: async (page) => {
      await expect(page.getByRole('button', { name: 'Update topics' })).toBeVisible({ timeout: 15000 });
    },
  },
  {
    id: 'create-article', label: 'Articles (Slate)', path: '/portal/create/article',
    copy: 'We add topics here as your tours are written and updated.', scope: PORTAL_EMPTY,
    extra: async (page) => {
      await expect(page.getByRole('button', { name: 'Update topics' })).toBeVisible({ timeout: 15000 });
    },
  },
];

/** A count that reads "you have nothing" next to an empty state that already says so. */
const ZERO_COUNT = /(^|\s)0 (tours?|topics? ready)\b/i;

async function assertEmptyState(page: Page, spec: EmptyPage, scheme: string): Promise<void> {
  const where = `${spec.label} (${scheme})`;
  const box = page.locator(spec.scope).filter({ hasText: spec.copy }).first();
  await expect(box, `${where}: empty-state copy not shown`).toBeVisible({ timeout: 15000 });
  const cta = box.getByTestId('empty-state-cta');
  await expect(cta, `${where}: empty state has no call to action`).toBeVisible();
  await expect(cta).toHaveText(/Browse tours/i);
  await expect(cta).toHaveAttribute('href', '/portal/tours');
  await expect(page.locator('[aria-busy="true"]'), `${where}: a loading placeholder is still shown`).toHaveCount(0, {
    timeout: 15000,
  });
  const mainText = await page.locator('[data-pshell="main"]').innerText();
  expect(mainText, `${where}: shows a zero count next to the empty state`).not.toMatch(ZERO_COUNT);
  if (spec.extra) await spec.extra(page);
}

test.describe('UI smoke — portal empty states (AA-760)', () => {
  test.skip(() => !hasTenantState(), NO_KEY_REASON);

  for (const spec of EMPTY_PAGES) {
    test(`portal empty state: ${spec.label} (${spec.path})`, async ({ browser }) => {
      const context = await browser.newContext({
        baseURL: baseUrl(),
        storageState: TENANT_STATE,
        viewport: { width: 1440, height: 900 },
      });
      await installBypassRoute(context);
      const refusedWrites = await installEmptyTenant(context, new URL(baseUrl()).origin);
      const page = await context.newPage();
      const watcher = watchPage(page);
      try {
        for (const scheme of ['light', 'dark'] as const) {
          await page.emulateMedia({ colorScheme: scheme });
          const resp = await page.goto(spec.path, { waitUntil: 'domcontentloaded' });
          expect(resp?.status() ?? 0, `${spec.label}: navigation failed`).toBeLessThan(400);
          await waitForPortalData(page);
          await assertEmptyState(page, spec, scheme);
          for (const vp of [
            { name: 'desktop', width: 1440, height: 900 },
            { name: 'phone', width: 400, height: 844 },
          ]) {
            await page.setViewportSize({ width: vp.width, height: vp.height });
            await page.waitForTimeout(300);
            if (vp.name === 'desktop') await fitViewportToMain(page, vp.width, vp.height);
            // The empty state must still be there at this width (the phone layout renders the same component).
            await expect(page.locator(spec.scope).filter({ hasText: spec.copy }).first()).toBeVisible();
            await page.screenshot({ path: `${PORTAL_RESULTS_DIR}/empty-${spec.id}-${vp.name}-${scheme}.png` });
          }
          await page.setViewportSize({ width: 1440, height: 900 });
        }
        await watcher.settle();
        expect(watcher.apiFailures, `${spec.label}: failing /api/ calls:\n${describeApiFailures(watcher.apiFailures)}`)
          .toEqual([]);
        expect(watcher.consoleErrors.map((e) => e.text), `${spec.label}: console errors`).toEqual([]);
        expect(refusedWrites, `${spec.label}: the page tried to write`).toEqual([]);
      } finally {
        await context.close();
      }
    });
  }
});
