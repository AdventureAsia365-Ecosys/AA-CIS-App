// tests/e2e/smoke-portal/portal-helpers.ts
// AA-885 — helpers for the tenant portal smoke (project `smoke-portal`, BASE_URL = a portal host:
// a portal preview, https://portal.lumiguides.it.com, or http://localhost:3004). The generic page
// check, bypass route and watcher come from ../smoke/smoke-helpers.ts.
import { expect, type Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';
import { TENANT_STATE } from '../smoke/smoke-helpers';

export const PORTAL_RESULTS_DIR = 'tests/e2e/results/smoke-portal';

export { TENANT_STATE };

/** True when the tenant test key is in the environment (CI secret E2E_TENANT_API_KEY). */
export function hasTenantKey(): boolean {
  return Boolean(process.env.E2E_TENANT_API_KEY);
}

/** True when the setup project wrote a tenant storage state in this run. */
export function hasTenantState(): boolean {
  return hasTenantKey() && fs.existsSync(TENANT_STATE);
}

export const NO_KEY_REASON =
  'No tenant test key: set E2E_TENANT_API_KEY (Secrets Manager aa-cis/dev/e2e-tenant/<slug>) to run the logged-in portal smoke.';

export function ensurePortalResultsDir(): void {
  fs.mkdirSync(PORTAL_RESULTS_DIR, { recursive: true });
}

/**
 * AA-885 / AA-605 — at 390px the portal must not scroll sideways (the sidebar is an off-canvas
 * drawer below 900px). Waits for the shell's mobile layout (the hamburger; CSS-driven since AA-759),
 * measures `documentElement.scrollWidth <= innerWidth` and lists the elements that stick out when it
 * fails (lesson S221: measure the offender, do not guess). Restores the desktop viewport after.
 */
export async function assertPortalNoOverflowAt390(page: Page, label: string): Promise<void> {
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('button', { name: 'Open menu' }), `${label}: portal shell did not switch to its phone layout`)
    .toBeVisible({ timeout: 15000 });
  await page.waitForTimeout(250);
  const m = await page.evaluate(() => {
    const innerWidth = window.innerWidth;
    const scrollable = (el: HTMLElement): boolean => {
      let node: HTMLElement | null = el.parentElement;
      while (node && node !== document.documentElement) {
        const ox = getComputedStyle(node).overflowX;
        if (ox === 'auto' || ox === 'hidden' || ox === 'scroll') return true;
        node = node.parentElement;
      }
      return false;
    };
    const offenders: string[] = [];
    for (const el of Array.from(document.body.querySelectorAll<HTMLElement>('*'))) {
      const r = el.getBoundingClientRect();
      if (r.right <= innerWidth + 1 || r.width === 0 || r.height === 0 || scrollable(el)) continue;
      offenders.push(`<${el.tagName.toLowerCase()} data-testid="${el.getAttribute('data-testid') ?? ''}"> right=${Math.round(r.right)}`);
    }
    return { scrollWidth: document.documentElement.scrollWidth, innerWidth, offenders: offenders.slice(0, 12) };
  });
  expect(
    m.scrollWidth,
    `${label}: page-level horizontal overflow at 390px (scrollWidth ${m.scrollWidth} > innerWidth ${m.innerWidth}). Offenders:\n    ${m.offenders.join('\n    ') || '(none found)'}`,
  ).toBeLessThanOrEqual(m.innerWidth + 1);
  await page.setViewportSize({ width: 1440, height: 900 });
}

/** Portal screenshots: desktop 1440 (full page) + phone 390 (first screen), in light and dark.
 * AA-759: the portal follows the theme; with no stored choice it follows `prefers-color-scheme`,
 * so emulating the colour scheme is enough (the page reloads so the pre-paint restore script runs). */
export async function portalScreenshots(page: Page, slug: string): Promise<void> {
  ensurePortalResultsDir();
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    if (scheme === 'dark') await page.reload({ waitUntil: 'domcontentloaded' });
    for (const vp of [
      { name: 'desktop', width: 1440, height: 900, fullPage: true },
      { name: 'mobile', width: 390, height: 844, fullPage: false },
    ]) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.waitForTimeout(300);
      await page.screenshot({ path: path.join(PORTAL_RESULTS_DIR, `${slug}-${vp.name}-${scheme}.png`), fullPage: vp.fullPage });
    }
  }
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 1440, height: 900 });
}
