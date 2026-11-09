// tests/e2e/admin-evidence/aa1042-1045-evidence.spec.ts
// AA-1042 + AA-1045 (S270) — admin evidence as the Dev-only E2E CIS admin (e2e-cis-admin@e2e-mail.lumiguides.it.com,
// cis-admin only, read-only session: GET only, writes 403 e2e_read_only).
//
// Agent-only, same rules as admin-pages.spec.ts (never CI: the code mail lands under inbound-admin/, readable only by
// the MFA admin role; E2EADMIN=1 is set by no workflow, unit guard tests/unit/test_aa816_e2e_cis_admin.py). Run:
//
//   cd apps/AA-CIS-App && AWS_PROFILE=aa365-admin E2EADMIN=1 \
//     npx playwright test --project=admin-evidence aa1042-1045-evidence
//
// (`--project=admin-evidence`, with the "=": `--project` takes several names, so a space would read the file filter
// as a second project name and fail with "Project(s) not found".)
//
// One sign-in (one code). Then:
//   1. AI gateway tabs (AA-1042): Route control, Trace, Routing (Stage coverage card), Calls, Guide, at 1440 (grown to
//      the inner scroll region) and 390, light and dark.
//   2. Usage explorer (AA-1045) on "last 7 days, provider x stage, cost desc", same sizes and themes.
//   2b. AA-1045 Done-when filter: the busiest (app, tenant) pair of the window is read through the BFF, then picked in
//      the filter drawer (one App chip, one Tenant chip); the active-filter chips and the address show both, and the
//      filtered view is shot at the same sizes and themes. Only the app and the tenant id's first 8 characters are
//      printed (stdout "FILTER ..."); the shots can show a tenant name, so review them before they go to a public repo.
//   3. AA-1045 Done-when walk: CSV export (browser download, header + rows), drill one cell to the Calls list, then
//      the Calls row's "Route" link opens the Route trace drawer (screenshots of both).
//   4. CRM total (AA-1045 open item a): GET /api/admin/usage-explorer/query?range=7d&granularity=day&group=app through
//      the BFF with this signed-in session (the API relays the staff token to crm.ops.v1 GET /llm/usage).
//      Expected: crm.status == "ok"; the crm app row's requests / cost_usd are printed (stdout line "CRM_TOTAL ...")
//      for comparison with a read of crm.llm_usage_daily in the CRM container over the same days.
//   5. Rollup = ledger: GET /api/admin/usage-explorer/rollup/check from the signed-in session says ok.
// $0: reads only (GET), no LLM call; the CSV is deleted after counting its lines. Screenshots:
// tests/e2e/results/admin-evidence/ (gitignored) -> copy to the root repo's docs/audits/ (public repo: check that no
// e-mail, account id or tenant name is visible first).
import { test, expect, type Page, type TestInfo } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { E2E_CIS_ADMIN_EMAIL, MAILBOX_ADMIN_PREFIX } from '../helpers/mailbox';
import { STAFF_AUTH_HOST, onStaffAuth, signInByEmailCode } from '../helpers/cognitoSignIn';
import { growViewportToContent } from '../smoke/smoke-helpers';

const CIS_PROD_ORIGIN = 'https://aa-cis.lumiguides.it.com';
const SHOTS_DIR = 'tests/e2e/results/admin-evidence';
const EXPLORER_VIEW = '/admin/usage?range=7d&granularity=day&group=provider,stage&sort=cost_usd&dir=desc';
// lib/usageExplorer.ts NONE: the dimension value of rows without one (no tenant).
const NONE = '(none)';
type ExplorerRow = { dims?: Record<string, string | null>; cost_usd?: number; requests?: number };

// path -> ready marker (rendered only once the page's data answered)
const PAGES: { path: string; slug: string; ready: string }[] = [
  { path: '/admin/ai-gateway?tab=controls', slug: 'aa1042-route-control', ready: '[data-testid="route-control"]' },
  { path: '/admin/ai-gateway?tab=trace', slug: 'aa1042-trace', ready: '[data-testid="ai-gateway-trace-tab"]' },
  { path: '/admin/ai-gateway?tab=routing', slug: 'aa1042-routing-coverage', ready: '[data-testid="llm-stage-coverage-status"]' },
  { path: '/admin/ai-gateway?tab=calls', slug: 'aa1042-calls', ready: '[data-testid="ai-gateway-calls-ready"]' },
  { path: '/admin/ai-gateway?tab=guide', slug: 'aa1042-guide', ready: '[data-testid="ai-gateway-guide"]' },
  { path: EXPLORER_VIEW, slug: 'aa1045-explorer-7d-provider-stage', ready: '[data-testid="usage-explorer-ready"]' },
];

async function shot(page: Page, name: string, fullPage = false) {
  fs.mkdirSync(SHOTS_DIR, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS_DIR, `${name}.png`), fullPage });
}

async function fourShots(page: Page, slug: string) {
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.waitForTimeout(400);
    await growViewportToContent(page, 1440, 900);
    await shot(page, `${slug}-desktop-${scheme}`, true);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(400);
    await shot(page, `${slug}-mobile-${scheme}`);
  }
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 1440, height: 900 });
}

test.describe('AA-1042 / AA-1045 — evidence as the Dev-only E2E CIS admin', () => {
  test.skip(process.env.E2EADMIN !== '1', 'opt-in, agent-only: E2EADMIN=1 with AWS_PROFILE=aa365-admin (MFA admin role)');

  test.afterEach(async ({ page }, testInfo: TestInfo) => {
    if (testInfo.status === testInfo.expectedStatus) return;
    if (onStaffAuth(page)) {
      await testInfo.attach('managed-login-state', { body: `url=${page.url().split('?')[0]}`, contentType: 'text/plain' });
    } else {
      await shot(page, 'aa1042-99-failure').catch(() => {});
    }
  });

  test('AI gateway tabs, usage explorer walk, CRM total', async ({ page }) => {
    test.setTimeout(420000);
    // ── sign in (same steps as admin-pages.spec.ts) ──
    await page.goto('/login', { waitUntil: 'domcontentloaded' });
    await page.getByRole('link', { name: /sign in with company email/i }).click({ timeout: 15000 });
    await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });
    await signInByEmailCode(page, { email: E2E_CIS_ADMIN_EMAIL, prefix: MAILBOX_ADMIN_PREFIX });
    await page.waitForURL((u) => u.origin === CIS_PROD_ORIGIN && u.pathname.startsWith('/admin'), { timeout: 30000 });
    const account = page.getByRole('button', { name: 'Account menu' });
    await expect(account).toBeVisible({ timeout: 15000 });

    // ── 1-2. pages ──
    for (const p of PAGES) {
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.goto(p.path, { waitUntil: 'domcontentloaded' });
      expect(new URL(page.url()).pathname, `${p.path} not bounced`).toBe(p.path.split('?')[0]);
      await expect(page.locator(p.ready).first()).toBeVisible({ timeout: 45000 });
      await expect(page.locator('[aria-busy="true"], .aa-skeleton')).toHaveCount(0, { timeout: 30000 });
      await fourShots(page, p.slug);
    }

    // ── 2b. filter one app + one tenant in the drawer (AA-1045 Done-when walk) ──
    let pair: { app: string; tenant: string; range: string } | null = null;
    for (const range of ['7d', '30d']) {
      const r = await page.evaluate(async (rg) => {
        const res = await fetch(`/api/admin/usage-explorer/query?range=${rg}&granularity=day&group=app,tenant`
          + '&sort=cost_usd&dir=desc');
        return { status: res.status, body: await res.text() };
      }, range);
      expect(r.status, `explorer query by app x tenant (${range})`).toBe(200);
      const row = ((JSON.parse(r.body).rows || []) as ExplorerRow[]).find((x) => {
        const d = x.dims || {};
        return !!d.app && d.app !== NONE && !!d.tenant && d.tenant !== NONE;
      });
      if (row) { pair = { app: String(row.dims!.app), tenant: String(row.dims!.tenant), range }; break; }
    }
    if (!pair) {
      console.log('FILTER skipped: no usage row with both an app and a tenant in the last 30 days');
      test.info().annotations.push({ type: 'note', description: 'filter step skipped: no (app, tenant) usage row' });
    } else {
      console.log(`FILTER range=${pair.range} app=${pair.app} tenant=${pair.tenant.slice(0, 8)}`);
      await page.goto(EXPLORER_VIEW.replace('range=7d', `range=${pair.range}`), { waitUntil: 'domcontentloaded' });
      await expect(page.getByTestId('usage-explorer-ready')).toBeVisible({ timeout: 45000 });
      await page.getByTestId('usage-explorer-open-filters').getByRole('button').click();
      const drawer = page.getByTestId('usage-explorer-filter-drawer');
      await expect(drawer).toBeVisible({ timeout: 15000 });
      // The chips carry the raw value as their title (the tenant chip shows the tenant's name).
      for (const [group, value] of [['App', pair.app], ['Tenant', pair.tenant]] as const) {
        const chip = drawer.getByRole('group', { name: group, exact: true }).getByTitle(value, { exact: true });
        await expect(chip, `${group} chip in the filter drawer`).toBeVisible({ timeout: 30000 });
        await chip.click();
        await expect(chip).toHaveAttribute('aria-checked', 'true');
      }
      await page.getByRole('dialog').getByRole('button', { name: 'Done', exact: true }).click();
      await expect(drawer).toBeHidden({ timeout: 10000 });
      const params = new URL(page.url()).searchParams;
      expect(params.get('f_app'), 'the address carries the app filter').toBe(pair.app);
      expect(params.get('f_tenant'), 'the address carries the tenant filter').toBe(pair.tenant);
      const active = page.getByTestId('usage-explorer-active-filters');
      await expect(active).toContainText(`App: ${pair.app}`, { timeout: 15000 });
      await expect(active).toContainText('Tenant: ');
      await expect(page.getByTestId('usage-explorer-ready')).toBeVisible({ timeout: 45000 });
      await expect(page.locator('[aria-busy="true"], .aa-skeleton')).toHaveCount(0, { timeout: 30000 });
      await fourShots(page, 'aa1045-explorer-filtered');
    }

    // ── 3. explorer walk: CSV, drill to Calls, Route trace drawer ──
    await page.goto(EXPLORER_VIEW, { waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('usage-explorer-ready')).toBeVisible({ timeout: 45000 });
    const [download] = await Promise.all([
      page.waitForEvent('download', { timeout: 20000 }),
      page.getByTestId('usage-explorer-csv').getByRole('button').click(),
    ]);
    const csvPath = path.join(SHOTS_DIR, 'aa1045-explorer.csv');
    await download.saveAs(csvPath);
    const lines = fs.readFileSync(csvPath, 'utf8').trim().split('\n');
    expect(lines.length, 'CSV: a header and at least one row').toBeGreaterThan(1);
    console.log(`CSV_LINES ${lines.length} header=${lines[0].slice(0, 160)}`);
    fs.unlinkSync(csvPath);   // the rows may name tenants: keep the line count only

    await page.getByTestId('usage-explorer-drill').first().getByRole('button').click();
    await page.waitForURL((u) => u.pathname === '/admin/ai-gateway' && u.searchParams.get('tab') === 'calls',
      { timeout: 30000 });
    await expect(page.getByTestId('ai-gateway-calls-ready')).toBeVisible({ timeout: 45000 });
    console.log(`DRILL_URL ${new URL(page.url()).search.slice(0, 300)}`);
    await shot(page, 'aa1045-drill-calls-desktop-light', true);
    const routeLink = page.getByTestId('ai-gateway-calls-route').first();
    if (await routeLink.isVisible().catch(() => false)) {
      await routeLink.getByRole('button').click();
      await expect(page.getByTestId('route-trace-drawer')).toBeVisible({ timeout: 30000 });
      await page.waitForTimeout(800);
      await shot(page, 'aa1045-drill-route-trace-desktop-light');
    } else {
      test.info().annotations.push({ type: 'note', description: 'no Route link on the first drilled rows (pre-trace rows)' });
    }

    // ── 4. CRM total through the BFF (the staff token is relayed to the CRM) ──
    const res = await page.evaluate(async () => {
      const r = await fetch('/api/admin/usage-explorer/query?range=7d&granularity=day&group=app&sort=cost_usd&dir=desc');
      return { status: r.status, body: await r.text() };
    });
    expect(res.status, 'explorer query').toBe(200);
    const body = JSON.parse(res.body);
    const crmRow = (body.rows || []).find((r: { dims?: { app?: string } }) => r.dims?.app === 'crm');
    console.log(`CRM_STATUS ${JSON.stringify(body.crm)}`);
    console.log(`CRM_TOTAL range=${body.range?.from}..${body.range?.to} requests=${crmRow?.requests ?? 0} `
      + `cost_usd=${crmRow?.cost_usd ?? 0} total_cost_usd=${body.totals?.cost_usd}`);
    expect(body.crm?.status, 'the CRM daily totals were merged (not unavailable)').toBe('ok');

    // ── 5. rollup = ledger, re-checked from the signed-in session (AA-1045 Done-when 2, live part) ──
    const chk = await page.evaluate(async () => {
      const r = await fetch('/api/admin/usage-explorer/rollup/check');
      return { status: r.status, body: await r.text() };
    });
    console.log(`ROLLUP_CHECK http=${chk.status} ${chk.body.slice(0, 600)}`);
    expect(chk.status).toBe(200);
    expect(JSON.parse(chk.body).ok, 'rollup totals equal the raw ledger').toBe(true);

    // ── sign out ──
    await account.click();
    await page.getByRole('button', { name: 'Sign out' }).click({ timeout: 5000 });
    await page.waitForURL((u) => u.origin === CIS_PROD_ORIGIN && u.pathname === '/login', { timeout: 30000 });
  });
});
