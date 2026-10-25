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
// AA-878 — viewer mode. When the smoke account is the read-only `viewer` (auth.setup.ts records the session's
// role), the same list runs as: the pages the viewer may read get the full page check;
// the pages it may not open (`viewerRefused`) must be refused (the proxy sends it to its landing page); one
// write through the BFF must answer 403, and the staff list API must answer 403. Admin mode is unchanged.
//
// AA-816 S2 path — the authenticated describes carry the tag @staff: only the `smoke` project (after smoke-setup)
// runs them, on the two Cognito callback hosts (the fixed Preview host as the capped viewer, Production as the
// Cognito E2E user, a reviewer). Per-PR previews run the `smoke-public` project, which skips @staff. Any other
// non-admin role (reviewer, content) runs the page list against frontend/lib/rbac.ts PAGE_RULES: a page it may not
// open must be refused like the viewer's; it does no write and no admin-only check (Production stays read-only).
//
// S251 (AA-835, AA-916) — also: Supply content (Booking) list + a made-up id's not-found card; the Review Queue's
// Reject / Dismiss / Regenerate modals opened and cancelled (reason chips in a screenshot, every admin write blocked
// and asserted absent); the sidebar labels fit at 1440; /login follows the colour scheme (public, no session).
import { randomUUID } from 'crypto';
import { test, expect, type Locator } from '@playwright/test';
import { can } from '../../../frontend/lib/rbac';
import {
  ADMIN_STATE,
  SMOKE_RESULTS_DIR,
  backgroundLuminance,
  baseOrigin,
  baseUrl,
  installBypassRoute,
  ensureResultsDir,
  assertNavOk,
  isAdminRun,
  isViewerRun,
  landingPathFor,
  roleMaySeePage,
  runPageCheck,
  smokeRole,
  waitForSkeletonGone,
  watchPage,
  describeApiFailures,
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
  // AA-839 — Jobs › Data erasure (admin only; DRY RUN by default, apply needs the env flag + a Cognito sign-in).
  { id: 'data-erasure', label: 'Data erasure', path: '/admin/data-erasure', readySelector: '[data-testid="data-erasure-ready"]', viewerRefused: true },
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
  // AA-913 — Platform › People & access › Alert recipients (admin only; changes need a Cognito sign-in).
  { id: 'platform-alerts', label: 'Alert recipients', path: '/admin/platform/alerts', readySelector: '[data-testid="alert-recipients-ready"]', viewerRefused: true },
  // AA-941 — Platform › Tenants › Agencies (CRM): the partner CRM's L1 console (admin only; needs a Cognito sign-in).
  { id: 'platform-agencies', label: 'Agencies (CRM)', path: '/admin/platform/agencies', viewerRefused: true },
  { id: 'photos', label: 'Photos', path: '/admin/photos', viewerRefused: true },
  // AA-977 — CIS Content › Assets › AA content (CRM): every staff role reads (the viewer too; write controls hidden).
  { id: 'aa-content', label: 'AA content (CRM)', path: '/admin/aa-content' },
  // AA-835 — CIS Content › Assets › Supply (Booking): every staff role reads (lib/rbac.ts PAGE_RULES ALL_STAFF).
  { id: 'supply-content', label: 'Supply content (Booking)', path: '/admin/supply-content' },
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
// to the role's landing page (/admin/review), so the final URL is that page, never the asked one. AA-816: the same
// for any non-admin role (reviewer on Production).
async function assertViewerRefused(browser: import('@playwright/test').Browser, spec: PageSpec): Promise<void> {
  const context = await browser.newContext({ baseURL: baseUrl(), storageState: ADMIN_STATE, viewport: { width: 1440, height: 900 } });
  await installBypassRoute(context);
  const page = await context.newPage();
  try {
    const landing = landingPathFor(smokeRole());
    await page.goto(spec.path, { waitUntil: 'domcontentloaded' });
    await page.waitForURL((u) => u.pathname === landing, { timeout: 15000 }).catch(() => {});
    const asked = new URL(spec.path, baseUrl()).pathname;
    expect(new URL(page.url()).pathname, `${spec.label}: ${smokeRole()} was not refused (stayed on ${page.url()})`)
      .toBe(landing);
    expect(new URL(page.url()).pathname).not.toBe(asked);
    await page.screenshot({ path: `${SMOKE_RESULTS_DIR}/${smokeRole()}-refused-${spec.id}-desktop-light.png` });
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

/** Screenshot / id prefix of a non-admin run (the page list does the same with `${smokeRole()}-`). */
function rolePrefix(): string {
  return isAdminRun() ? '' : `${smokeRole()}-`;
}

// S251 — /login follows the colour scheme like the admin (it pinned light before). No session: runs in smoke-public on
// every per-PR preview. Dark: background luminance < 0.2; light: > 0.8 (same thresholds as assertThemeSwitches).
test.describe('UI smoke — /login follows the colour scheme', () => {
  for (const scheme of ['light', 'dark'] as const) {
    test(`/login renders ${scheme} under prefers-color-scheme: ${scheme}`, async ({ browser }) => {
      const context = await browser.newContext({ baseURL: baseUrl(), colorScheme: scheme, viewport: { width: 1440, height: 900 } });
      await installBypassRoute(context);
      const page = await context.newPage();
      try {
        await assertNavOk(page, '/login');
        await expect(page.getByTestId('login-page')).toBeVisible({ timeout: 15000 });
        const lum = await backgroundLuminance(page, '[data-testid="login-page"]');
        if (scheme === 'dark') expect(lum, `/login background luminance ${lum.toFixed(3)} under dark`).toBeLessThan(0.2);
        else expect(lum, `/login background luminance ${lum.toFixed(3)} under light`).toBeGreaterThan(0.8);
        await page.screenshot({ path: `${SMOKE_RESULTS_DIR}/login-desktop-${scheme}.png` });
      } finally {
        await context.close();
      }
    });
  }
});

// AA-816 S2 path — no session needed (runs in smoke-public too): the viewer flags on the page list say exactly
// what the frontend page matrix says, so viewer mode and the rbac-driven reviewer mode never disagree.
test.describe('UI smoke — page list matches the page matrix', () => {
  test('viewerRefused equals !canSeePage(viewer) for every listed page', () => {
    const mismatched = ADMIN_PAGES.filter((p) => !!p.viewerRefused === roleMaySeePage('viewer', p.path))
      .map((p) => `${p.path} (viewerRefused=${!!p.viewerRefused})`);
    expect(mismatched, 'fix viewerRefused or frontend/lib/rbac.ts PAGE_RULES').toEqual([]);
  });
});

test.describe('UI smoke — admin', { tag: '@staff' }, () => {
  for (const spec of ADMIN_PAGES) {
    test(`admin: ${spec.label} (${spec.path})`, async ({ browser }) => {
      if (!isAdminRun()) {
        const refused = isViewerRun() ? !!spec.viewerRefused : !roleMaySeePage(smokeRole(), spec.path);
        if (refused) {
          await assertViewerRefused(browser, spec);
          return;
        }
        const readySelector = spec.viewerReadySelector ?? spec.readySelector;
        await runPageCheck(browser, ADMIN_STATE, { ...spec, id: `${smokeRole()}-${spec.id}`, readySelector },
          { checkMobileLayout: true });
        return;
      }
      await runPageCheck(browser, ADMIN_STATE, spec, { checkMobileLayout: true });
    });
  }

  test('admin: Staff · detail (/admin/platform/staff/{id}, first account)', async ({ browser }) => {
    test.skip(!isAdminRun(), 'AA-878: the staff directory is admin-only; viewer mode checks its 403 below');
    const id = await firstStaffId(browser);
    await runPageCheck(browser, ADMIN_STATE, {
      id: 'platform-staff-detail',
      label: 'Staff · detail',
      path: `/admin/platform/staff/${id}`,
      readySelector: '[data-testid="staff-detail-ready"]',
    }, { checkMobileLayout: true });
  });

  // AA-835 — Booking's "Edit in CIS" link lands on /admin/supply-content/<id>; an id CIS does not know answers 404
  // and the page shows its not-found card (not a blank page, not an error). Every staff role may open it.
  test('admin: Supply content · unknown id shows the not-found card', async ({ browser }) => {
    const id = randomUUID();
    const detailPath = `/api/admin/supply-content/${id}`;
    await runPageCheck(browser, ADMIN_STATE, {
      id: `${rolePrefix()}supply-content-not-found`,
      label: 'Supply content · not found',
      path: `/admin/supply-content/${id}`,
      readySelector: '[data-testid="supply-error"]',
    }, {
      checkMobileLayout: true,
      // the one expected failure: this page's own detail read of the made-up id
      expectApiFailure: (f) => f.status === 404 && new URL(f.url).pathname === detailPath,
      afterChecks: async (page) => {
        await expect(page.getByTestId('supply-error')).toContainText('No hotel or service with this id');
      },
    });
  });

  // S251 — every visible sidebar label fits at 1440 (the kit cuts a long one with an ellipsis: "Supply content
  // (Booking)" was cut). Checked on the Supply content page, which opens the CIS Content section.
  test('admin: sidebar labels are not cut at 1440 (CIS Content section)', async ({ browser }) => {
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: ADMIN_STATE, viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      await assertNavOk(page, '/admin/supply-content');
      await expect(page.locator('.aa-sidebar-label').first()).toBeVisible({ timeout: 15000 });
      const cut = await page.evaluate(() => Array.from(document.querySelectorAll<HTMLElement>('.aa-sidebar-label'))
        .filter((el) => el.offsetParent !== null && el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 1)
        .map((el) => `"${(el.textContent ?? '').trim()}" (${el.scrollWidth}px in ${el.clientWidth}px)`));
      expect(cut, `sidebar labels cut with an ellipsis at 1440:\n  ${cut.join('\n  ')}`).toEqual([]);
      await page.screenshot({ path: `${SMOKE_RESULTS_DIR}/${rolePrefix()}sidebar-cis-desktop-light.png` });
    } finally {
      await context.close();
    }
  });

  // AA-916 WM-07 — Reject, Dismiss and Regenerate open a modal with the reason chip row (one chip preselected). Each
  // is opened, captured (light + dark) and CANCELLED. Every same-origin non-GET /api/admin/ call is aborted and the
  // test fails if one was attempted, so the run can never act on the queue (Production stays read-only).
  test('admin: Review Queue · Reject / Dismiss / Regenerate modals show the reason chips and cancel', async ({ browser }) => {
    test.skip(!can(smokeRole(), 'review.act'), `${smokeRole()} has no review.act: the queue shows no action buttons`);
    const context = await browser.newContext({ baseURL: baseUrl(), storageState: ADMIN_STATE, viewport: { width: 1440, height: 900 } });
    await installBypassRoute(context);
    const writes: string[] = [];
    const origin = baseOrigin();
    await context.route('**/api/admin/**', async (route) => {
      const req = route.request();
      if (new URL(req.url()).origin === origin && !['GET', 'HEAD', 'OPTIONS'].includes(req.method())) {
        writes.push(`${req.method()} ${req.url()}`);
        await route.abort();
        return;
      }
      await route.fallback(); // on to the bypass route (installed first)
    });
    const page = await context.newPage();
    const watcher = watchPage(page);
    try {
      await assertNavOk(page, '/admin/review');
      await waitForSkeletonGone(page);
      const rows = page.locator('[data-testid="kit-datatable-row"]');
      const rowCount = await rows.count();
      test.skip(rowCount === 0, 'the Review Queue has no rows to open the modals on');

      const opened: string[] = [];
      const openAndCancel = async (name: string, button: Locator) => {
        if ((await button.count()) === 0) {
          test.info().annotations.push({ type: 'note', description: `${name}: no row offers this action now` });
          return;
        }
        await button.first().click();
        const dialog = page.getByRole('dialog');
        await expect(dialog, `${name}: the modal did not open`).toBeVisible({ timeout: 10000 });
        const chips = dialog.getByTestId('wm-reason-chips');
        await expect(chips, `${name}: no reason chip row in the modal`).toBeVisible();
        await expect(chips.locator('[role="radio"][aria-checked="true"]'), `${name}: no chip preselected`).toHaveCount(1);
        for (const scheme of ['light', 'dark'] as const) {
          await page.emulateMedia({ colorScheme: scheme });
          await page.waitForTimeout(300);
          await page.screenshot({ path: `${SMOKE_RESULTS_DIR}/${rolePrefix()}review-modal-${name.toLowerCase()}-desktop-${scheme}.png` });
        }
        await page.emulateMedia({ colorScheme: 'light' });
        await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
        await expect(dialog, `${name}: Cancel did not close the modal`).toHaveCount(0);
        opened.push(name);
      };

      // enabled buttons only (a row being regenerated disables its actions)
      const actions = rows.locator('button:not([disabled])');
      const reject = actions.filter({ hasText: /^\s*Reject\s*$/ });
      const dismiss = actions.and(page.locator('[title^="Dismiss"]'));
      // every row busy (being regenerated) leaves no enabled action: nothing to open, not a failure
      test.skip((await reject.count()) + (await dismiss.count()) === 0,
        'every Review Queue row is busy (regenerating): no enabled Reject / Dismiss to open');
      await openAndCancel('Reject', reject);
      await openAndCancel('Dismiss', dismiss);
      await openAndCancel('Regenerate', actions.and(page.locator('[title^="Regenerate"]')));

      expect(opened, 'Reject and Dismiss are on every actionable row').toEqual(expect.arrayContaining(['Reject', 'Dismiss']));
      expect(writes, 'a modal sent a write although it was cancelled').toEqual([]);
      await watcher.settle();
      expect(watcher.apiFailures, `Review Queue: failing same-origin /api/ responses:\n${describeApiFailures(watcher.apiFailures)}`)
        .toEqual([]);
      expect(watcher.consoleErrors.map((e) => e.text), 'Review Queue: console errors').toEqual([]);
    } finally {
      await context.close();
    }
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
test.describe('UI smoke — viewer (read-only)', { tag: '@staff' }, () => {
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
// portal smoke moved to AA-Partner with the portal (AA-979: e2e/smoke-portal/, ui-smoke-portal.yml there).
