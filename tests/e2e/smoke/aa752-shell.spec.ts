// tests/e2e/smoke/aa752-shell.spec.ts
// AA-752 — smoke for the shared admin shell. Runs in the "smoke" project (admin storageState from
// smoke-setup). Every wait is bounded. Checks:
//   (a) the sidebar logo links to /admin/overview;
//   (b) the collapse toggle narrows the rail and the collapsed state survives a reload;
//   (c) ⌘K opens the palette and typing 2+ chars of a REAL tour name (read from the Master Content
//       table on the page, not hardcoded) returns ≥ 1 tour row;
//   (d) at 390px the bottom nav is visible and the page has no horizontal overflow (prints the
//       offending elements on failure);
//   (e) AA-1039 (S266, root ADR 0019 d13): an admin sees the seven nav groups in order (Home, Tenants,
//       AA Catalogue, Tenant content QA, AI & spend, Writing Lab, Platform), each with a fold toggle, the Apps
//       block links to Booking admin in a new tab (no Tenant portal link), and the collapsed rail has no group
//       headers (AA-883's two sections are gone).
//   (f) S264: the nav groups fold (kit NavGroup): the group of the current page is open on arrival, folding it
//       hides its items, a reload opens it again, and (admin) an unfolded other group stays unfolded after a
//       reload (localStorage aa_nav_groups:cis-admin).
// AA-878 — viewer mode (the smoke account is the read-only viewer): the shell is checked on pages the viewer
// may open (/admin/review, /admin/master-content), the logo lands on the viewer's landing page (Overview is
// admin-only, the proxy redirects), and (e) — the admin groups — is skipped.
import { test, expect } from '@playwright/test';
import {
  ADMIN_STATE, baseUrl, installBypassRoute, isAdminRun, landingPathFor, smokeRole, waitForShellHydrated,
} from './smoke-helpers';

/** A page with the full shell that the smoke account may open (Jobs is admin-only). */
function shellPath(): string {
  return isAdminRun() ? '/admin/jobs' : '/admin/review';
}

async function adminPage(browser: import('@playwright/test').Browser) {
  const context = await browser.newContext({
    baseURL: baseUrl(),
    storageState: ADMIN_STATE,
    viewport: { width: 1440, height: 900 },
  });
  await installBypassRoute(context);
  const page = await context.newPage();
  return { context, page };
}

// AA-816 S2 path: @staff = needs the smoke session (project `smoke`, never the per-PR `smoke-public` run).
test.describe('AA-752 — admin shell', { tag: '@staff' }, () => {
  test('(a) sidebar logo links to /admin/overview', async ({ browser }) => {
    const { context, page } = await adminPage(browser);
    // The logo pushes /admin/overview; a viewer is redirected to its landing page (/admin/review), so it
    // starts elsewhere to see the move.
    const target = landingPathFor(smokeRole());
    try {
      await page.goto(isAdminRun() ? '/admin/jobs' : '/admin/master-content', { waitUntil: 'domcontentloaded' });
      const logo = page.getByTestId('sidebar-logo');
      await expect(logo).toBeVisible({ timeout: 10000 });
      // The logo is a client-component button whose onClick (router.push) only works AFTER React
      // hydrates. Clicking right after domcontentloaded can land before hydration, so the click is
      // a no-op and waitForURL times out (the flake this test had). Wait for a bounded hydration
      // signal — the page's own <main className="aa-admin-main"> mounts only once the client page
      // component has rendered — then retry the click within a bound in case it still raced.
      await expect(page.locator('.aa-admin-main')).toBeVisible({ timeout: 15000 });
      // AA-834: the sidebar's own post-hydration marker; the retry-click poll below stays as a backstop.
      await waitForShellHydrated(page);
      await expect
        .poll(
          async () => {
            if (new URL(page.url()).pathname === target) return target;
            await logo.click();
            // Give the client navigation a short, bounded moment to update the URL before re-reading.
            await page.waitForURL((u) => u.pathname === target, { timeout: 3000 }).catch(() => {});
            return new URL(page.url()).pathname;
          },
          { timeout: 15000, intervals: [500, 1000, 1500] },
        )
        .toBe(target);
      expect(new URL(page.url()).pathname).toBe(target);
    } finally {
      await context.close();
    }
  });

  test('(b) collapse toggles the rail and survives reload', async ({ browser }) => {
    const { context, page } = await adminPage(browser);
    try {
      await page.goto(shellPath(), { waitUntil: 'domcontentloaded' });
      const rail = page.getByTestId('admin-sidebar');
      await expect(rail).toBeVisible({ timeout: 10000 });
      // AA-834: the rail is in the SSR HTML, so "visible" is not "interactive". Clicking before React
      // hydrates the toggle was a silent no-op (gh run 38684738547: 236px on both tries). Wait for
      // the sidebar's post-hydration marker before clicking.
      await waitForShellHydrated(page);
      await expect(rail).toHaveAttribute('data-collapsed', 'false', { timeout: 2000 });

      const expandedWidth = await rail.evaluate((el) => el.getBoundingClientRect().width);
      await page.getByTestId('sidebar-collapse').click();
      // The state flips in the same commit as the click — assert it first, then the width.
      await expect(rail).toHaveAttribute('data-collapsed', 'true', { timeout: 5000 });
      // Bounded wait for the width transition to land on the collapsed rail.
      await expect
        .poll(async () => rail.evaluate((el) => Math.round(el.getBoundingClientRect().width)), { timeout: 5000 })
        .toBeLessThan(expandedWidth - 50);

      // Survives a reload (localStorage cis_sidebar + the no-flash restore script). The SSR HTML
      // always says data-collapsed="false"; the stored state is applied by the hydrated client.
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(rail).toBeVisible({ timeout: 10000 });
      await waitForShellHydrated(page);
      await expect(rail).toHaveAttribute('data-collapsed', 'true', { timeout: 5000 });
      // Poll the width (the 0.18s transition can be mid-flight on a single read).
      await expect
        .poll(async () => rail.evaluate((el) => Math.round(el.getBoundingClientRect().width)), { timeout: 5000 })
        .toBeLessThan(expandedWidth - 50);
    } finally {
      await context.close();
    }
  });

  test('(c) ⌘K palette returns a real tour', async ({ browser }) => {
    const { context, page } = await adminPage(browser);
    try {
      await page.goto('/admin/master-content', { waitUntil: 'domcontentloaded' });
      // Read a real tour name from the Master Content table (first non-empty cell with letters). The page uses the
      // split frame (`.aa-admin-split`, #1056), so select by the <main> element, not the frame class.
      const name = await page.waitForFunction(() => {
        const cells = Array.from(document.querySelectorAll('main table tbody tr td'));
        for (const c of cells) {
          const t = (c.textContent ?? '').trim();
          if (t.length >= 4 && /[A-Za-z]/.test(t)) return t;
        }
        return null;
      }, undefined, { timeout: 15000 }).then((h) => h.jsonValue() as Promise<string>).catch(() => '');

      const frag = (name.match(/[A-Za-z]{2,}/)?.[0] ?? name).slice(0, 4);
      expect(frag.length, 'could not read a real tour name from the Master Content table').toBeGreaterThanOrEqual(2);

      // AA-834: the ⌘K listener is client-only — make sure the shell has hydrated before the shortcut.
      await waitForShellHydrated(page);
      await page.keyboard.press('ControlOrMeta+KeyK');
      const dialog = page.getByRole('dialog', { name: /Global search/i });
      const input = dialog.getByRole('textbox');
      await expect(input).toBeVisible({ timeout: 5000 });
      await input.fill(frag);

      // A tour row (its deep link href carries ?tour=) appears for the real name. Bounded.
      const tourRow = dialog.locator('button', { hasText: new RegExp(frag, 'i') });
      await expect(tourRow.first()).toBeVisible({ timeout: 10000 });
    } finally {
      await context.close();
    }
  });

  test('(e) AA-1039: the seven nav groups and the Apps links', async ({ browser }) => {
    test.skip(!isAdminRun(), 'AA-878: most groups are admin-only');
    const { context, page } = await adminPage(browser);
    try {
      await page.goto(shellPath(), { waitUntil: 'domcontentloaded' });
      await waitForShellHydrated(page);
      const rail = page.getByTestId('admin-sidebar');
      await expect(rail).toHaveAttribute('data-collapsed', 'false', { timeout: 5000 });
      const ids = ['home', 'tenants', 'aa-catalogue', 'tenant-content-qa', 'ai-spend', 'writing-lab', 'platform'];
      for (const id of ids) {
        await expect(page.getByTestId(`nav-group-${id}-toggle`)).toBeVisible({ timeout: 10000 });
      }
      // In display order: the groups' top edges increase down the sidebar.
      const tops: number[] = [];
      for (const id of ids) tops.push((await page.getByTestId(`nav-group-${id}`).boundingBox())?.y ?? -1);
      expect(tops, 'nav groups in the ADR 0019 d13 order').toEqual([...tops].sort((x, y) => x - y));
      await expect(page.getByTestId('nav-section-toggle-platform')).toHaveCount(0);

      const booking = page.getByTestId('sidebar-app-booking');
      await expect(booking).toBeVisible();
      await expect(booking).toHaveAttribute('href', /^https:\/\/[^/]+/);
      await expect(booking).toHaveAttribute('target', '_blank');
      await expect(page.getByTestId('sidebar-app-portal')).toHaveCount(0);

      // Collapsed rail: no clickable group header, every item icon still there.
      await page.getByTestId('sidebar-collapse').click();
      await expect(rail).toHaveAttribute('data-collapsed', 'true', { timeout: 5000 });
      await expect(page.getByTestId('nav-group-platform-toggle')).toHaveCount(0);
      await expect(page.getByTestId('nav-group-platform').getByRole('button', { name: 'Jobs' })).toBeVisible();
    } finally {
      await context.close();
    }
  });

  test('(f) S264: nav groups fold, the current page group opens on arrival, choices persist', async ({ browser }) => {
    const { context, page } = await adminPage(browser);
    try {
      await page.goto(shellPath(), { waitUntil: 'domcontentloaded' });
      await waitForShellHydrated(page);
      await expect(page.getByTestId('admin-sidebar')).toHaveAttribute('data-collapsed', 'false', { timeout: 5000 });
      // AA-1039: /admin/jobs sits in Platform, /admin/review in AA Catalogue.
      const active = isAdminRun() ? 'platform' : 'aa-catalogue';
      const toggle = page.getByTestId(`nav-group-${active}-toggle`);
      const group = page.getByTestId(`nav-group-${active}`);
      await expect(toggle).toHaveAttribute('aria-expanded', 'true', { timeout: 10000 });
      await expect(group.getByRole('group')).toBeVisible();

      await toggle.click();
      await expect(toggle).toHaveAttribute('aria-expanded', 'false', { timeout: 5000 });
      await expect(group.getByRole('group')).toBeHidden();

      await page.reload({ waitUntil: 'domcontentloaded' });
      await waitForShellHydrated(page);
      await expect(page.getByTestId(`nav-group-${active}-toggle`)).toHaveAttribute('aria-expanded', 'true', { timeout: 10000 });

      if (isAdminRun()) {
        const lab = page.getByTestId('nav-group-writing-lab-toggle');
        const was = await lab.getAttribute('aria-expanded');
        if (was === 'false') {
          await lab.click();
          await expect(lab).toHaveAttribute('aria-expanded', 'true', { timeout: 5000 });
        }
        await page.reload({ waitUntil: 'domcontentloaded' });
        await waitForShellHydrated(page);
        await expect(page.getByTestId('nav-group-writing-lab-toggle')).toHaveAttribute('aria-expanded', 'true', { timeout: 10000 });
      }
    } finally {
      await context.close();
    }
  });

  test('(d) phone: bottom nav visible and no horizontal overflow at 390px', async ({ browser }) => {
    const { context, page } = await adminPage(browser);
    try {
      await page.goto(shellPath(), { waitUntil: 'domcontentloaded' });
      await page.setViewportSize({ width: 390, height: 844 });
      await page.waitForTimeout(300);

      await expect(page.locator('.aa-bottom-nav')).toBeVisible({ timeout: 5000 });

      const metrics = await page.evaluate(() => {
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
        const offenders: { tag: string; cls: string; width: number; right: number }[] = [];
        for (const el of Array.from(document.body.querySelectorAll<HTMLElement>('*'))) {
          const r = el.getBoundingClientRect();
          if (r.right <= innerWidth + 1 || r.width === 0 || r.height === 0) continue;
          if (scrollable(el)) continue;
          offenders.push({
            tag: el.tagName.toLowerCase(),
            cls: (typeof el.className === 'string' ? el.className : '').slice(0, 48),
            width: Math.round(r.width),
            right: Math.round(r.right),
          });
        }
        offenders.sort((a, b) => b.right - a.right);
        return { scrollWidth: document.documentElement.scrollWidth, innerWidth, offenders: offenders.slice(0, 12) };
      });

      const offenderText = metrics.offenders
        .map((o) => `    <${o.tag} class="${o.cls}"> width=${o.width} right=${o.right}`)
        .join('\n');
      expect(
        metrics.scrollWidth,
        `page-level horizontal overflow at 390px (scrollWidth ${metrics.scrollWidth} > innerWidth ${metrics.innerWidth}). Offenders:\n${offenderText || '    (none found)'}`,
      ).toBeLessThanOrEqual(metrics.innerWidth + 1);
    } finally {
      await context.close();
    }
  });
});
