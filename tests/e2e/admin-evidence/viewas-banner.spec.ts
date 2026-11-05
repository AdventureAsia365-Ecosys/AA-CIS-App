// AA-1035 (root ADR 0006a A10b-2): the Dev-only E2E CIS admin (A10b) starts a read-only view-as on a SYNTHETIC tenant,
// the banner is screenshotted, and the view is ended from the banner. Evidence for the Done-when "view-as opens
// read-only with a banner and an audit row" (the audit rows are read separately in the container). Agent-only, NEVER CI:
//
//   cd apps/AA-CIS-App && AWS_PROFILE=aa365-admin E2EADMIN=1 TENANT_A=<synthetic tenant uuid> npx playwright test --project admin-evidence viewas-banner
//
// Writes on Dev: one shared.staff_view_as_session row (5 minutes, ended by the run) and its audit rows (expected).
import { test, expect } from '@playwright/test';
import fs from 'fs';
import { E2E_CIS_ADMIN_EMAIL, MAILBOX_ADMIN_PREFIX } from '../helpers/mailbox';
import { STAFF_AUTH_HOST, signInByEmailCode } from '../helpers/cognitoSignIn';

const CIS = 'https://aa-cis.lumiguides.it.com';
const SHOTS = 'tests/e2e/results/admin-evidence';
const A = process.env.TENANT_A ?? '';

test('A10b admin: view as partner on a synthetic tenant shows the read-only banner, then ends', async ({ page }) => {
  test.skip(process.env.E2EADMIN !== '1' || !A, 'opt-in: E2EADMIN=1 TENANT_A=<synthetic uuid>');
  test.setTimeout(240000);
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/login', { waitUntil: 'domcontentloaded' });
  await page.getByRole('link', { name: /sign in with company email/i }).click({ timeout: 15000 });
  await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });
  await signInByEmailCode(page, { email: E2E_CIS_ADMIN_EMAIL, prefix: MAILBOX_ADMIN_PREFIX });
  await page.waitForURL((u) => u.origin === CIS && u.pathname.startsWith('/admin'), { timeout: 30000 });

  await page.goto(`/admin/tenants/${A}`, { waitUntil: 'domcontentloaded' });
  await page.getByTestId('view-as-start').getByRole('button', { name: 'View as partner' }).click({ timeout: 30000 });
  const modal = page.getByTestId('view-as-start-modal');
  await modal.getByLabel(/reason/i).fill('S270 live check of the read-only banner');
  await modal.getByLabel(/length/i).selectOption({ label: '15 minutes' });
  await modal.getByRole('button', { name: 'Start view' }).click();
  const banner = page.getByTestId('view-as-banner');
  await expect(banner).toBeVisible({ timeout: 30000 });
  await expect(banner).toContainText('Read only');
  await page.waitForTimeout(1000);
  await page.screenshot({ path: `${SHOTS}/viewas-banner-desktop-light.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${SHOTS}/viewas-banner-mobile-light.png` });
  await page.setViewportSize({ width: 1440, height: 900 });
  await banner.getByRole('button', { name: 'End view' }).click();
  await expect(banner).toBeHidden({ timeout: 30000 });
});
