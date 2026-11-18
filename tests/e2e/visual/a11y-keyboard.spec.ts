// tests/e2e/visual/a11y-keyboard.spec.ts
// AA-1131 (S283 UI audit, batch K1) — the keyboard half of the kit fixes, on the kit gallery's "Table and drawer" entry
// (core/KitGallery.tsx TableDemo), in the visual job's per-PR run (a KIT_GALLERY=1 build, no session, no backend) at
// 1440 and 390 (390 is the DataTable's phone card mode: the card is the keyboard target there):
//   - the page-size select is named "Rows per page";
//   - Tab reaches a row (tabindex 0) and it shows a focus ring; Enter opens the row's Drawer;
//   - the Drawer is a dialog named by its title, the focus starts inside it (the note field, initialFocusRef);
//   - Tab and Shift+Tab never leave it (more presses than it has controls);
//   - Escape closes it and the focus is back on the row that opened it.
// Screenshots (tests/e2e/results/visual/shots/a11y-*.png): the focused row and the open drawer, the evidence the issue
// asks for. Runs only where the gallery exists (VISUAL_GALLERY=1, public, not the self-test).
import path from 'path';
import { expect, test } from '@playwright/test';

const GALLERY = process.env.VISUAL_GALLERY === '1';
const PUBLIC = process.env.VISUAL_AUTH !== 'staff';
const SELFTEST = process.env.VISUAL_SELFTEST === '1';
const SLUGS = (process.env.VISUAL_SLUGS ?? 'all').trim();
const SHOT_DIR = 'tests/e2e/results/visual/shots';

test('kit table rows and drawer work from the keyboard', async ({ page }, info) => {
  test.skip(!GALLERY || !PUBLIC || SELFTEST, 'the kit gallery exists only in the per-PR gallery build');
  test.skip(SLUGS !== 'all' && !SLUGS.split(',').includes('kit-gallery'), 'the kit gallery is not in this run');
  const viewport = info.project.name.replace(/^visual-(staff-)?/, '');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/kit-gallery', { waitUntil: 'load' });
  const entry = page.locator('[data-kit-gallery="table"]');
  await entry.scrollIntoViewIfNeeded();

  await expect(entry.getByRole('combobox', { name: 'Rows per page' })).toBeVisible();

  // Tab into the table from the entry's title: the toolbar's Columns button comes first, then the first row.
  const firstRow = entry.locator('[data-kit-row-open]').first();
  await expect(firstRow).toHaveAttribute('tabindex', '0');
  await entry.evaluate((el) => {
    const title = el.firstElementChild as HTMLElement | null;
    title?.setAttribute('tabindex', '-1');
    title?.focus();
  });
  let reached = false;
  for (let i = 0; i < 6 && !reached; i += 1) {
    await page.keyboard.press('Tab');
    reached = await firstRow.evaluate((el) => el === document.activeElement);
  }
  expect(reached, 'Tab reaches the first row within a few presses').toBe(true);
  const ring = await firstRow.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(ring, 'the focused row shows the kit focus ring').toBe('solid');
  await page.screenshot({ path: path.join(SHOT_DIR, `a11y-row-focus__${viewport}.png`) });

  await page.keyboard.press('Enter');
  const dialog = page.getByRole('dialog', { name: 'Lantern Valley Walk' });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('textbox', { name: 'Note for the operator' })).toBeFocused();
  // The slide-in animation (0.18 s) ends before the capture.
  await dialog.evaluate((el) => Promise.all(el.getAnimations().map((a) => a.finished)));
  await page.screenshot({ path: path.join(SHOT_DIR, `a11y-drawer-open__${viewport}.png`) });

  const inside = () => page.evaluate(() => !!document.activeElement?.closest('[role="dialog"]'));
  for (let i = 0; i < 6; i += 1) {
    await page.keyboard.press('Tab');
    expect(await inside(), `Tab ${i + 1} stays in the drawer`).toBe(true);
  }
  for (let i = 0; i < 6; i += 1) {
    await page.keyboard.press('Shift+Tab');
    expect(await inside(), `Shift+Tab ${i + 1} stays in the drawer`).toBe(true);
  }

  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(firstRow).toBeFocused();
});
