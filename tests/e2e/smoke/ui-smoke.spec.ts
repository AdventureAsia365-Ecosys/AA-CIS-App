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
//
// AA-878 — viewer mode. When the smoke account is the read-only `viewer` (auth.setup.ts records the role the
// server answered at login), the same list runs as: the pages the viewer may read get the full page check;
// the pages it may not open (`viewerRefused`) must be refused (the proxy sends it to its landing page); one
// write through the BFF must answer 403, and the staff list API must answer 403. Admin mode is unchanged.
import { test, expect } from '@playwright/test';
import {
  ADMIN_STATE,
  SMOKE_RESULTS_DIR,
  baseUrl,
  installBypassRoute,
  ensureResultsDir,
  assertNavOk,
  isViewerRun,
  landingPathFor,
  runPageCheck,
  smokeRole,
  type PageSpec,
} from './smoke-helpers';

ensureResultsDir();

// ── Admin pages — all 15 routes under frontend/app/admin/*/page.tsx (AA-601, AA-722 retired
// dashboard). Add a page = add one line here. `readySelector` is set only for pages whose loaded
// content is neither a kit/table row nor the kit empty state; it marks real content that renders
// only after data has loaded. ──
// AA-878 — `viewerRefused`: a page the read-only viewer may not open (admin-only, or Upload / S1, which only
// write). A new admin-only page must carry it, or viewer mode fails on it (the proxy redirects it away).
const ADMIN_PAGES: PageSpec[] = [
  { id: 'atom-curation', label: 'Social Content', path: '/admin/atom-curation', readySelector: '[data-testid="admin-content-ready"]', viewerRefused: true },
  { id: 'decisions', label: 'Jev Decisions', path: '/admin/decisions', viewerRefused: true },
  { id: 'jobs', label: 'Jobs', path: '/admin/jobs', viewerRefused: true },
  { id: 'llm-usage', label: 'External Spend', path: '/admin/llm-usage', viewerRefused: true },
  { id: 'master-content', label: 'Master Content', path: '/admin/master-content' },
  { id: 'overview', label: 'Overview', path: '/admin/overview', viewerRefused: true },
  // AA-883 — Platform › People & access › Staff (backend live on Dev since CIS main c2f9e919).
  { id: 'platform-staff', label: 'Staff', path: '/admin/platform/staff', viewerRefused: true },
  // AA-884 — Platform › Apps, Audit log, Cost per app. ADMIN-ONLY pages (viewer refused, AA-878) (PAGE_RULES /admin/platform; backend
  // ops.read / audit.read / spend.read): a non-admin smoke account must be refused here, never pass. The
  // Apps and Cost pages are cards / a custom table, so their ready marker renders only once data loaded.
  { id: 'platform-apps', label: 'Apps', path: '/admin/platform/apps', readySelector: '[data-testid="platform-apps-ready"]', viewerRefused: true },
  { id: 'platform-audit', label: 'Audit log', path: '/admin/platform/audit', viewerRefused: true },
  { id: 'platform-cost', label: 'Cost per app', path: '/admin/platform/cost', readySelector: '[data-testid="platform-cost-ready"]', viewerRefused: true },
  { id: 'photos', label: 'Photos', path: '/admin/photos', viewerRefused: true },
  { id: 'platform-stats', label: 'A4 Quality', path: '/admin/platform-stats', readySelector: '[data-testid="admin-content-ready"]', viewerRefused: true },
  // AA-769 — Content Quality (T2/T9 quality signals for every tenant; tenants never see them).
  { id: 'content-quality', label: 'Content Quality', path: '/admin/content-quality', readySelector: '[data-testid="content-quality-ready"]', viewerRefused: true },
  { id: 'review', label: 'Review Queue', path: '/admin/review' },
  { id: 's1-rewrite', label: 'S1 Rewrite', path: '/admin/s1-rewrite', viewerRefused: true },
  { id: 'seo-intelligence', label: 'SEO Intelligence', path: '/admin/seo-intelligence', viewerRefused: true },
  { id: 'settings', label: 'Settings', path: '/admin/settings', readySelector: '[data-testid="admin-content-ready"]' },
  // AA-686 — the LLM Models tab (route/shadow editor, catalog, shadow A/B). `?tab=models` opens it
  // directly; `llm-models-ready` renders once the tab mounts, so a blank/broken tab still fails.
  // AA-878: the tab needs ai.admin; a viewer's deep link falls back to the first tab (Settings content).
  { id: 'settings-models', label: 'Settings · LLM Models', path: '/admin/settings?tab=models', readySelector: '[data-testid="llm-models-ready"]', viewerReadySelector: '[data-testid="admin-content-ready"]' },
  { id: 'tenant-activity', label: 'Content Trace', path: '/admin/tenant-activity', readySelector: '[data-testid="admin-content-ready"]', viewerRefused: true },
  { id: 'tenants', label: 'Tenants', path: '/admin/tenants', viewerRefused: true },
  { id: 'upload', label: 'Upload', path: '/admin/upload', viewerRefused: true },
];

// AA-878 — viewer mode: an admin-only page must not render for the viewer. The proxy answers with a redirect
// to the role's landing page (/admin/review), so the final URL is that page, never the asked one.
async function assertViewerRefused(browser: import('@playwright/test').Browser, spec: PageSpec): Promise<void> {
  const context = await browser.newContext({ baseURL: baseUrl(), storageState: ADMIN_STATE, viewport: { width: 1440, height: 900 } });
  await installBypassRoute(context);
  const page = await context.newPage();
  try {
    const landing = landingPathFor(smokeRole());
    await page.goto(spec.path, { waitUntil: 'domcontentloaded' });
    await page.waitForURL((u) => u.pathname === landing, { timeout: 15000 }).catch(() => {});
    const asked = new URL(spec.path, baseUrl()).pathname;
    expect(new URL(page.url()).pathname, `${spec.label}: viewer was not refused (stayed on ${page.url()})`).toBe(landing);
    expect(new URL(page.url()).pathname).not.toBe(asked);
    await page.screenshot({ path: `${SMOKE_RESULTS_DIR}/viewer-refused-${spec.id}-desktop-light.png` });
  } finally {
    await context.close();
  }
}

/** AA-878 — a same-origin BFF call from the page (so the Vercel bypass route applies), status + body head. */
async function bffCall(browser: import('@playwright/test').Browser, method: string, path: string) {
  const context = await browser.newContext({ baseURL: baseUrl(), storageState: ADMIN_STATE });
  await installBypassRoute(context);
  const page = await context.newPage();
  try {
    await assertNavOk(page, '/admin/review');
    return await page.evaluate(async ({ method, path }) => {
      const r = await fetch(path, {
        method, credentials: 'same-origin',
        headers: method === 'GET' ? {} : { 'Content-Type': 'application/json' },
        body: method === 'GET' ? undefined : '{}',
      });
      return { status: r.status, text: (await r.text()).slice(0, 300) };
    }, { method, path });
  } finally {
    await context.close();
  }
}

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
      if (isViewerRun()) {
        if (spec.viewerRefused) {
          await assertViewerRefused(browser, spec);
          return;
        }
        const readySelector = spec.viewerReadySelector ?? spec.readySelector;
        await runPageCheck(browser, ADMIN_STATE, { ...spec, id: `viewer-${spec.id}`, readySelector },
          { checkMobileLayout: true });
        return;
      }
      await runPageCheck(browser, ADMIN_STATE, spec, { checkMobileLayout: true });
    });
  }

  test('admin: Staff · detail (/admin/platform/staff/{id}, first account)', async ({ browser }) => {
    test.skip(isViewerRun(), 'AA-878: the staff directory is admin-only; viewer mode checks its 403 below');
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
  test('admin: /admin/dashboard redirects to the landing page (/admin/overview for an admin)', async ({ browser }) => {
    const landing = landingPathFor(smokeRole());
    const context = await browser.newContext({
      baseURL: baseUrl(),
      storageState: ADMIN_STATE,
      viewport: { width: 1440, height: 900 },
    });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await page.goto('/admin/dashboard', { waitUntil: 'domcontentloaded' });
      await page.waitForURL((u) => u.pathname === landing, { timeout: 15000 });
      expect(new URL(page.url()).pathname).toBe(landing);
    } finally {
      await context.close();
    }
  });
});

// AA-878 — viewer mode only: the read-only account is refused on a write and on the staff list, through the
// same BFF the pages use. The write is harmless even if a gate ever failed open: trashing a tour id that does
// not exist (the backend's content.write policy and the 404 stop it too).
test.describe('UI smoke — viewer (read-only)', () => {
  test('viewer: a BFF write answers 403', async ({ browser }) => {
    test.skip(!isViewerRun(), 'runs only when the smoke account is the read-only viewer');
    const res = await bffCall(browser, 'PATCH', '/api/admin/tours/00000000-0000-0000-0000-000000000878/trash');
    expect(res.status, `viewer write → HTTP ${res.status}: ${res.text}`).toBe(403);
  });

  test('viewer: the staff list API answers 403', async ({ browser }) => {
    test.skip(!isViewerRun(), 'runs only when the smoke account is the read-only viewer');
    const res = await bffCall(browser, 'GET', '/api/admin/platform/staff');
    expect(res.status, `viewer GET staff list → HTTP ${res.status}: ${res.text}`).toBe(403);
  });

  test('viewer: an open content read answers 200', async ({ browser }) => {
    test.skip(!isViewerRun(), 'runs only when the smoke account is the read-only viewer');
    const res = await bffCall(browser, 'GET', '/api/admin/review-queue?status=pending&page=1&page_size=1');
    expect(res.status, `viewer GET review queue → HTTP ${res.status}: ${res.text}`).toBe(200);
  });
});

// AA-885 — the tenant portal is its own app (portal/, its own host); its smoke is the
// `smoke-portal` project (tests/e2e/smoke-portal/), run by ui-smoke-portal.yml on portal previews.
