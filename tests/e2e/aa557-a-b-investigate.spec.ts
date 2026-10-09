// tests/e2e/aa557-a-b-investigate.spec.ts — AA-557 mục A/B investigation (2026-09-07).
//
// AA-557 flags 2 items AA-551/554 both reported Done that Nghiệp says are still broken on the
// real production URL he tested (aa-cis.lumiguides.it.com/admin/atom-curation):
//   A. sticky header (Tour/Market filter + stat bar) disappears entirely on scroll
//   B. bulk-select checkboxes have no "Star selected (N)" button, and star doesn't update without F5
//
// This spec runs against the REAL production domain (BASE_URL=https://aa-cis.lumiguides.it.com),
// the exact URL Nghiệp used, with a real admin login through the real /login form — not
// a local dev server — specifically to rule out "Nghiệp saw a stale/cached build" as the
// explanation before concluding regression.
import { test, expect } from '@playwright/test';
import { adminUsername, adminPassword } from './helpers/auth';
import fs from 'fs';

const SHOT_DIR = 'tests/e2e/results/aa557';
fs.mkdirSync(SHOT_DIR, { recursive: true });

async function loginAsAdmin(page) {
  await page.goto('/login');
  await page.fill('input[type="text"]', adminUsername());
  await page.fill('input[type="password"]', adminPassword());
  await page.click('button:has-text("Login")');
  await page.waitForTimeout(2500);
  console.log('POST-LOGIN URL:', page.url());
  await page.screenshot({ path: `${SHOT_DIR}/00-post-login.png`, fullPage: false });
}

test.describe('AA-557 mục A/B', () => {
  test('A — does the sticky header survive a real scroll?', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/atom-curation');
    await page.waitForSelector('text=Social Content', { timeout: 15000 });
    await page.waitForTimeout(1000);

    await page.screenshot({ path: `${SHOT_DIR}/A-01-before-scroll.png`, fullPage: false });
    const headerBefore = await page.getByText('Tours', { exact: true }).first().boundingBox();
    const firstCardBefore = await page.locator('text=Luxury Taj Mahal').first().boundingBox().catch(() => null);

    // Real scroll: mouse over the MAIN atom-card column (not the 280px Tours sidebar card,
    // which has its own nested overflow:auto and would silently absorb the wheel event instead
    // — confirmed by a first attempt at x=700 which only scrolled that inner list while the
    // header/main content never moved). x=1050 sits inside the outer page scroll container's
    // main-content column instead.
    await page.mouse.move(1050, 500);
    await page.mouse.wheel(0, 1500);
    await page.waitForTimeout(800);
    await page.screenshot({ path: `${SHOT_DIR}/A-02-after-scroll.png`, fullPage: false });
    const firstCardAfter = await page.locator('text=Luxury Taj Mahal').first().boundingBox().catch(() => null);
    console.log('first atom card bbox before:', JSON.stringify(firstCardBefore));
    console.log('first atom card bbox after:', JSON.stringify(firstCardAfter));

    const headerAfter = await page.getByText('Tours', { exact: true }).first().boundingBox();
    const statBarVisible = await page.getByText('ATOMS', { exact: false }).first().isVisible().catch(() => false);

    console.log('HEADER BBOX before:', JSON.stringify(headerBefore));
    console.log('HEADER BBOX after:', JSON.stringify(headerAfter));
    console.log('Stat bar visible after scroll (loose text match):', statBarVisible);

    // Explicit check on the actual header stat-bar labels from page.tsx (TOURS/ATOMS/SEGMENTS/
    // SCORE ROWS/ROUTES/HUBS render as "Tours"/"Atoms"/etc via CSS textTransform, DOM text is
    // mixed-case) — assert whether they are still within the viewport bounding box after scroll.
    const viewport = page.viewportSize();
    const label = page.locator('div', { hasText: /^Atoms$/ }).first();
    const labelBox = await label.boundingBox().catch(() => null);
    console.log('Atoms stat-bar label bbox after scroll:', JSON.stringify(labelBox), 'viewport:', JSON.stringify(viewport));
  });

  test('A2 — same scroll test but at a narrow/mobile viewport (real-mobile 100vh suspicion)', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 }); // iPhone 12-ish
    await loginAsAdmin(page);
    await page.goto('/admin/atom-curation');
    await page.waitForSelector('text=Social Content', { timeout: 15000 });
    await page.waitForTimeout(1000);
    await page.screenshot({ path: `${SHOT_DIR}/A2-01-mobile-before.png`, fullPage: false });

    // On mobile the media query collapses the inner-nav to a horizontal row and the section
    // content stacks in one column — scroll the whole page (real touch-like wheel over body),
    // which is the only way to reach section content on this layout.
    await page.mouse.move(300, 500);
    await page.mouse.wheel(0, 1500);
    await page.waitForTimeout(800);
    await page.screenshot({ path: `${SHOT_DIR}/A2-02-mobile-after.png`, fullPage: false });

    const bodyScrollTop = await page.evaluate(() => document.documentElement.scrollTop || document.body.scrollTop);
    console.log('document/body scrollTop after mobile wheel:', bodyScrollTop);
  });

  test('B — bulk-select: is there a Star selected button, and does star update without reload?', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/admin/atom-curation');
    await page.waitForSelector('text=Social Content', { timeout: 15000 });
    await page.waitForTimeout(1500);

    await page.screenshot({ path: `${SHOT_DIR}/B-01-initial.png`, fullPage: false });

    // Select 2 atom checkboxes (title="Select for bulk star", per page.tsx AtomCard).
    const checkboxes = page.locator('input[type="checkbox"][title="Select for bulk star"]');
    const count = await checkboxes.count();
    console.log('bulk-select checkboxes found:', count);
    if (count >= 2) {
      await checkboxes.nth(0).check();
      await checkboxes.nth(1).check();
      await page.waitForTimeout(300);
      await page.screenshot({ path: `${SHOT_DIR}/B-02-two-selected.png`, fullPage: false });

      const starBtn = page.getByRole('button', { name: /Star selected/ });
      const starBtnVisible = await starBtn.isVisible().catch(() => false);
      console.log('"Star selected" button visible:', starBtnVisible);

      if (starBtnVisible) {
        await starBtn.click();
        await page.waitForTimeout(1500);
        await page.screenshot({ path: `${SHOT_DIR}/B-03-after-bulk-star-no-reload.png`, fullPage: false });
      }
    }
  });
});
