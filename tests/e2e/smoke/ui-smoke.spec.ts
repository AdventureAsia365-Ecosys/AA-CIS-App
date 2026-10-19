// tests/e2e/smoke/ui-smoke.spec.ts
// AA-732 — UI smoke run against a PR's Vercel preview. For each key page, logged in as an admin:
//   • navigation returns an OK status
//   • 0 console errors (CONSOLE_ALLOWLIST is empty by default — see smoke-helpers.ts)
//   • no 4xx/5xx on the page's own SAME-ORIGIN /api/ calls (failure message carries URL + status
//     + first 300 chars of the body — S212: read the body, a 500 once looked like a 401)
//   • the main content shows ≥1 kit/table row OR the kit empty state (not a blank/broken shell)
// and screenshots are captured at desktop 1440 + mobile 390, light + dark.
//
// The admin login happens once in auth.setup.ts (storageState). Each test opens its OWN context
// from that state, so the tests are independent — one red page does not skip the others.
// This spec is its own Playwright project ("smoke"), so legacy specs are not run by CI.
import { test, expect } from '@playwright/test';
import {
  ADMIN_STATE,
  baseUrl,
  installBypassRoute,
  ensureResultsDir,
  assertNavOk,
  runPageCheck,
  type PageSpec,
} from './smoke-helpers';

ensureResultsDir();

// ── Admin pages — all 15 routes under frontend/app/admin/*/page.tsx (AA-601, AA-722 retired
// dashboard). Add a page = add one line here. `readySelector` is set only for pages whose loaded
// content is neither a kit/table row nor the kit empty state; it marks real content that renders
// only after data has loaded. ──
const ADMIN_PAGES: PageSpec[] = [
  { id: 'atom-curation', label: 'Social Content', path: '/admin/atom-curation', readySelector: '[data-testid="admin-content-ready"]' },
  { id: 'decisions', label: 'Jev Decisions', path: '/admin/decisions' },
  { id: 'jobs', label: 'Jobs', path: '/admin/jobs' },
  { id: 'llm-usage', label: 'External Spend', path: '/admin/llm-usage' },
  { id: 'master-content', label: 'Master Content', path: '/admin/master-content' },
  { id: 'overview', label: 'Overview', path: '/admin/overview' },
  // AA-883 — Platform › People & access › Staff (backend live on Dev since CIS main c2f9e919).
  { id: 'platform-staff', label: 'Staff', path: '/admin/platform/staff' },
  { id: 'photos', label: 'Photos', path: '/admin/photos' },
  { id: 'platform-stats', label: 'A4 Quality', path: '/admin/platform-stats', readySelector: '[data-testid="admin-content-ready"]' },
  // AA-769 — Content Quality (T2/T9 quality signals for every tenant; tenants never see them).
  { id: 'content-quality', label: 'Content Quality', path: '/admin/content-quality', readySelector: '[data-testid="content-quality-ready"]' },
  { id: 'review', label: 'Review Queue', path: '/admin/review' },
  { id: 's1-rewrite', label: 'S1 Rewrite', path: '/admin/s1-rewrite' },
  { id: 'seo-intelligence', label: 'SEO Intelligence', path: '/admin/seo-intelligence' },
  { id: 'settings', label: 'Settings', path: '/admin/settings', readySelector: '[data-testid="admin-content-ready"]' },
  // AA-686 — the LLM Models tab (route/shadow editor, catalog, shadow A/B). `?tab=models` opens it
  // directly; `llm-models-ready` renders once the tab mounts, so a blank/broken tab still fails.
  { id: 'settings-models', label: 'Settings · LLM Models', path: '/admin/settings?tab=models', readySelector: '[data-testid="llm-models-ready"]' },
  { id: 'tenant-activity', label: 'Content Trace', path: '/admin/tenant-activity', readySelector: '[data-testid="admin-content-ready"]' },
  { id: 'tenants', label: 'Tenants', path: '/admin/tenants' },
  { id: 'upload', label: 'Upload', path: '/admin/upload' },
];

// AA-883 — the Staff deep link /admin/platform/staff/{id} opens the drawer. A made-up id answers
// 404 (a real failure for this smoke), so the test reads the first account id from the list API in
// the browser (same cookie + bypass route as the page) and checks the deep link with that id.
async function firstStaffId(browser: import('@playwright/test').Browser): Promise<string> {
  const context = await browser.newContext({ baseURL: baseUrl(), storageState: ADMIN_STATE });
  await installBypassRoute(context);
  const page = await context.newPage();
  try {
    await assertNavOk(page, '/admin/platform/staff');
    const res = await page.evaluate(async () => {
      const r = await fetch('/api/admin/platform/staff', { credentials: 'same-origin' });
      return { ok: r.ok, status: r.status, text: await r.text() };
    });
    // Two separate checks so a failure says which one broke: the API call itself (status + body
    // snippet), or a 200 whose payload has no usable account id.
    expect(res.ok, `GET /api/admin/platform/staff → HTTP ${res.status}: ${res.text.slice(0, 300)}`).toBe(true);
    let id = '';
    try {
      const body = JSON.parse(res.text) as { staff?: { id?: string }[] };
      id = body.staff?.[0]?.id ?? '';
    } catch {
      id = '';
    }
    expect(id, `Staff list API (HTTP ${res.status}) returned no account id: ${res.text.slice(0, 300)}`)
      .toMatch(/^[0-9a-f-]{36}$/i);
    return id;
  } finally {
    await context.close();
  }
}

test.describe('UI smoke — admin', () => {
  for (const spec of ADMIN_PAGES) {
    test(`admin: ${spec.label} (${spec.path})`, async ({ browser }) => {
      await runPageCheck(browser, ADMIN_STATE, spec, { checkMobileLayout: true });
    });
  }

  test('admin: Staff · detail (/admin/platform/staff/{id}, first account)', async ({ browser }) => {
    const id = await firstStaffId(browser);
    await runPageCheck(browser, ADMIN_STATE, {
      id: 'platform-staff-detail',
      label: 'Staff · detail',
      path: `/admin/platform/staff/${id}`,
      readySelector: '[data-testid="staff-detail-ready"]',
    }, { checkMobileLayout: true });
  });

  // AA-722 — the legacy /admin/dashboard page is retired. As an admin, a request to it must land
  // on /admin/overview (middleware's role-aware redirect). Bounded wait; keep this independent of
  // the per-page checks above.
  test('admin: /admin/dashboard redirects to /admin/overview', async ({ browser }) => {
    const context = await browser.newContext({
      baseURL: baseUrl(),
      storageState: ADMIN_STATE,
      viewport: { width: 1440, height: 900 },
    });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/admin/dashboard', { waitUntil: 'domcontentloaded' });
      await page.waitForURL(/\/admin\/overview\b/, { timeout: 15000 });
      expect(new URL(page.url()).pathname).toBe('/admin/overview');
    } finally {
      await context.close();
    }
  });
});

// AA-885 — the tenant portal is its own app (portal/, its own host); its smoke is the
// `smoke-portal` project (tests/e2e/smoke-portal/), run by ui-smoke-portal.yml on portal previews.
