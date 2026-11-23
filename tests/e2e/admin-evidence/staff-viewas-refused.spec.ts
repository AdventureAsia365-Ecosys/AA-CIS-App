// AA-1157: the owner's read-only "view as a lower role" cannot be started by the Dev-only E2E CIS admin (not an owner,
// read-only): POST /api/view-as/roles is refused 403 (the API refuses the write of a read-only E2E identity before the
// owner check, so the detail is generic: e2e_read_only), and the generic proxy refuses the start path for everyone
// (403 view_as_bff_only, the token would reach the page). The Roles & permissions page shows no "View as" button to
// it. The owner rule itself (detail "Only a platform owner can view as another role") is proven by the unit tests with
// a non-owner Cognito admin. Agent-only, NEVER CI:
//
//   cd apps/AA-CIS-App && AWS_PROFILE=aa365-admin E2EADMIN=1 npx playwright test --project admin-evidence staff-viewas-refused
//
// Writes on Dev: nothing (every call is refused before a row); the refusals are staff.request audit rows (expected).
import { test, expect } from '@playwright/test';
import fs from 'fs';
import { E2E_CIS_ADMIN_EMAIL, MAILBOX_ADMIN_PREFIX } from '../helpers/mailbox';
import { STAFF_AUTH_HOST, signInByEmailCode } from '../helpers/cognitoSignIn';

const CIS = 'https://aa-cis.lumiguides.it.com';
const SHOTS = 'tests/e2e/results/admin-evidence';

test('A10b admin: cannot start a view as a lower role', async ({ page }) => {
  test.skip(process.env.E2EADMIN !== '1', 'opt-in: E2EADMIN=1');
  test.setTimeout(240000);
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/login', { waitUntil: 'domcontentloaded' });
  await page.getByRole('link', { name: /sign in with company email/i }).click({ timeout: 15000 });
  await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });
  await signInByEmailCode(page, { email: E2E_CIS_ADMIN_EMAIL, prefix: MAILBOX_ADMIN_PREFIX });
  await page.waitForURL((u) => u.origin === CIS && u.pathname.startsWith('/admin'), { timeout: 30000 });

  const body = { role: 'content', reason: 'Live refusal check', minutes: 15 };
  const viaBff = await page.request.post(`${CIS}/api/view-as/roles`, { data: body });
  expect(viaBff.status()).toBe(403);
  const viaProxy = await page.request.post(`${CIS}/api/admin/view-as/roles`, { data: body });
  expect(viaProxy.status()).toBe(403);
  expect((await viaProxy.json()).detail).toBe('view_as_bff_only');

  await page.goto('/admin/platform/roles', { waitUntil: 'domcontentloaded' });
  await expect(page.getByTestId('roles-app-cis')).toBeVisible({ timeout: 30000 });
  await expect(page.locator('[data-testid^="role-view-as-"]')).toHaveCount(0);
  await page.screenshot({ path: `${SHOTS}/staff-viewas-refused-roles.png`, fullPage: true });
});
