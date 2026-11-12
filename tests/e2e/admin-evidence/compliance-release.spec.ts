// AA-1087 (S278, root ADR 0006a A10b-3): the Dev-only E2E CIS admin (A10b) releases a held piece of a SYNTHETIC tenant
// (Tenant content QA > Compliance, the staff review "approve") through its signed-in browser session, and the same
// release on a real tenant's flag is refused 403 e2e_read_only. Agent-only, NEVER CI (codes in inbound-admin/, which
// only the MFA admin role reads; no workflow sets E2EADMIN):
//
//   cd apps/AA-CIS-App && AWS_PROFILE=aa365-admin E2EADMIN=1 FLAG_SYNTHETIC=<flag uuid> [FLAG_REAL=<flag uuid>] \
//     [E2E_DRY_RUN=1] npx playwright test --project admin-evidence compliance-release
//
// FLAG_SYNTHETIC = an OPEN subject_gate flag of a synthetic tenant (shared.tenants.is_canary true, not the pilot), as
// GET /admin/tenant-qa/compliance lists it; FLAG_REAL = an open flag of a real tenant (optional, the 403 check; the
// route refuses it before anything is written, so it stays open). E2E_DRY_RUN=1 signs in, opens the page and reads the
// flags only. Writes on Dev (not a dry run): the synthetic flag resolved as released, its content_compliance.released
// audit row and the AA-884 staff.request row, both with the A10b admin as the actor.
import { test, expect, type Page } from '@playwright/test';
import fs from 'fs';
import { E2E_CIS_ADMIN_EMAIL, MAILBOX_ADMIN_PREFIX } from '../helpers/mailbox';
import { STAFF_AUTH_HOST, signInByEmailCode } from '../helpers/cognitoSignIn';

const CIS = 'https://aa-cis.lumiguides.it.com';
const SHOTS = 'tests/e2e/results/admin-evidence';
const SYNTHETIC = process.env.FLAG_SYNTHETIC ?? '';
const REAL = process.env.FLAG_REAL ?? '';
const DRY_RUN = process.env.E2E_DRY_RUN === '1';

type Flag = { flag_id: string; tenant_id: string; state: string; detail: { content_sha256?: string } };

async function flagById(page: Page, id: string): Promise<Flag | undefined> {
  const res = await page.request.get('/api/admin/tenant-qa/compliance?state=all&kind=subject_gate&limit=200');
  expect(res.status()).toBe(200);
  const body = (await res.json()) as { data: Flag[] };
  return body.data.find((f) => f.flag_id === id);
}

/** The release from the signed-in page itself (same-origin fetch through the BFF, as the Compliance page sends it). */
async function release(page: Page, flag: Flag) {
  return page.evaluate(
    async ({ id, sha }) => {
      const res = await fetch(`/api/admin/tenant-qa/compliance/${id}/release`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ reason: 'Synthetic pilot rehearsal: links checked', content_sha256: sha }),
        credentials: 'same-origin',
      });
      let body: Record<string, unknown> | null = null;
      try {
        body = (await res.json()) as Record<string, unknown>;
      } catch {
        body = null;
      }
      return { status: res.status, body };
    },
    { id: flag.flag_id, sha: flag.detail.content_sha256 ?? '' },
  );
}

test('A10b admin: release a held piece of a synthetic tenant; refused on a real tenant', async ({ page }) => {
  test.skip(process.env.E2EADMIN !== '1' || !SYNTHETIC, 'opt-in: E2EADMIN=1 FLAG_SYNTHETIC=<open flag of a synthetic tenant>');
  test.setTimeout(240000);
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/login', { waitUntil: 'domcontentloaded' });
  await page.getByRole('link', { name: /sign in with company email/i }).click({ timeout: 15000 });
  await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });
  await signInByEmailCode(page, { email: E2E_CIS_ADMIN_EMAIL, prefix: MAILBOX_ADMIN_PREFIX });
  await page.waitForURL((u) => u.origin === CIS && u.pathname.startsWith('/admin'), { timeout: 30000 });

  await page.goto('/admin/tenant-qa/compliance', { waitUntil: 'domcontentloaded' });
  const flag = await flagById(page, SYNTHETIC);
  expect(flag, 'FLAG_SYNTHETIC is a subject_gate flag').toBeTruthy();
  expect(flag!.state).toBe('open');
  await page.screenshot({ path: `${SHOTS}/compliance-release-before-desktop-light.png`, fullPage: true });
  if (DRY_RUN) {
    console.log('[compliance-release] dry run: the flag reads open; no write');
    return;
  }

  if (REAL) {
    const real = await flagById(page, REAL);
    expect(real, 'FLAG_REAL is a subject_gate flag').toBeTruthy();
    const refused = await release(page, real!);
    expect([refused.status, refused.body?.detail]).toEqual([403, 'e2e_read_only']);
    expect((await flagById(page, REAL))!.state).toBe('open');
  }

  const released = await release(page, flag!);
  expect(released.status, JSON.stringify(released.body)).toBe(200);
  expect(released.body?.resolution).toBe('released');
  expect(String(released.body?.resolved_by ?? '')).toMatch(/^admin/);
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.screenshot({ path: `${SHOTS}/compliance-release-after-desktop-light.png`, fullPage: true });
});
