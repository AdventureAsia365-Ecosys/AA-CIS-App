// tests/e2e/visual/visual.spec.ts
// AA-1008 (S1d/S2c, root ADR 0016 decision 9) — the visual run of CIS admin. For every selected page of pages.ts, in
// the project's viewport (1440 x 900 or 390 x 844) and both themes:
//   - no page-level horizontal overflow: the document (`scrollWidth` of html / body vs the viewport) and the kit frame
//     (PageFrame `[data-kit-frame]` / ShellFrame main: they scroll on their own, so a wide child there never shows in
//     the document's scrollWidth; lesson S254) — the widest offenders are named;
//   - no console error and no uncaught page error (allowlist.json for known noise);
//   - no failed same-origin request (status >= 400 or a network failure), except the document's own expected status;
//   - the kit frame is on the page (`[data-kit-frame]`), unless the page says frame: false;
//   - a dynamic route (`openFirst`) is captured on the page its list's first matching link or row opens (a row by
//     Enter, never a click; skipped with the reason when the list has none or the row opens nothing);
//   - the theme applied is the one asked for (`<html data-theme>`);
//   - AA-1131: every visible form control, dialog and icon button has an accessible name (helpers/a11yNames.ts). A
//     control the kit core renders (a data-kit-* hook) must be named on every page; on the kit gallery every control
//     must be; a page's own unnamed controls elsewhere are listed in the report (`unnamed`), not failed (the S283 audit
//     batches fix them page by page);
//   - a PNG of the page (the inner scroll region grown into the viewport first: the admin document never scrolls,
//     lesson S251).
// Each result goes to tests/e2e/results/visual/report/<page>__<viewport>__<theme>.json; summary.mjs merges them.
// Read-only: the run only navigates.
//
// Selection (env): VISUAL_AUTH public | staff (default public); VISUAL_SLUGS a comma list of slugs or "all" (default
// all), from changed-pages.mjs; VISUAL_GALLERY=1 adds the gated gallery (a KIT_GALLERY=1 build only);
// VISUAL_SELFTEST=1 runs only the planted-overflow pages (expected to fail: the workflow checks they did).
// Staff runs load the smoke setup's storage state (Cognito E2E user) and skip the pages its role may not open.
import fs from 'fs';
import path from 'path';
import { expect, test, type Page } from '@playwright/test';
import { PAGES, selectPages } from './pages';
import { unnamedControls } from '../helpers/a11yNames';
import { ADMIN_STATE, baseOrigin, growViewportToContent, installBypassRoute, roleMaySeePage, smokeRole } from '../smoke/smoke-helpers';

const THEMES = ['light', 'dark'] as const;
const OUT = 'tests/e2e/results/visual';
const REPORT_DIR = path.join(OUT, 'report');
const SHOT_DIR = path.join(OUT, 'shots');
const ALLOW = JSON.parse(fs.readFileSync(path.join(__dirname, 'allowlist.json'), 'utf8')) as {
  console: { match: string; why: string }[];
  requests: { match: string; why: string }[];
};
const allowed = (list: { match: string }[], text: string) => list.some((a) => text.includes(a.match));

const AUTH = process.env.VISUAL_AUTH === 'staff' ? 'staff' : 'public';
const SLUGS = (process.env.VISUAL_SLUGS ?? 'all').trim();
const GALLERY = process.env.VISUAL_GALLERY === '1';
const SELFTEST = process.env.VISUAL_SELFTEST === '1';

if (AUTH === 'staff') test.use({ storageState: ADMIN_STATE });

/** The overflow probe: the document and every kit scroll frame, with the five widest offenders. */
async function probeOverflow(page: Page) {
  // The configured viewport width, not window.innerWidth: a mobile (isMobile) page that overflows widens its layout
  // viewport to the content, so innerWidth grows with the overflow and the document never looks wider than it.
  const width = page.viewportSize()?.width ?? 0;
  return page.evaluate((configured) => {
    const vw = configured || window.innerWidth;
    const doc = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
    const name = (el: Element) =>
      el.tagName.toLowerCase() + (el.id ? `#${el.id}` : '') +
      (el.getAttribute('data-testid') ? `[data-testid=${el.getAttribute('data-testid')}]` : '') +
      (typeof el.className === 'string' && el.className ? `.${el.className.split(' ').slice(0, 2).join('.')}` : '');
    // A frame's offenders are measured against the frame's own client edge (its scrollbar excluded), not the viewport:
    // a child that reaches the viewport edge under a 6 px scrollbar scrolls the frame yet is inside the viewport
    // (AA-1148, S294: the 25/11 run named no offender for content-workspace-new at 390).
    const frameOffenders = (frame: HTMLElement) => {
      const edge = frame.getBoundingClientRect().left + frame.clientLeft + frame.clientWidth;
      const over = [...frame.querySelectorAll<HTMLElement>('*')].filter((el) => {
        if (el.closest('[data-kit-scroll], [aria-hidden="true"]')) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.right > edge + 1;
      });
      // the outermost ones: a child of an offender says nothing new
      return over.filter((el) => !over.includes(el.parentElement as HTMLElement)).slice(0, 5)
        .map((el) => {
          const box = el.parentElement?.closest('[data-testid]');
          const where = box ? ` in [data-testid=${box.getAttribute('data-testid')}]` : '';
          return `${name(el)}${where} (right ${Math.round(el.getBoundingClientRect().right)} > ${Math.round(edge)})`;
        });
    };
    const frames = [...document.querySelectorAll<HTMLElement>('[data-kit-frame], [data-kit-shell="main"]')]
      .filter((el) => el.scrollWidth > el.clientWidth + 1)
      .map((el) => ({ frame: name(el), scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, offenders: frameOffenders(el) }));
    const offenders = [...document.querySelectorAll<HTMLElement>('body *')]
      .filter((el) => {
        if (el.closest('[data-kit-scroll], [data-kit-shell="sidebar"], [aria-hidden="true"]')) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.right > vw + 1;
      })
      .sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right)
      .slice(0, 5)
      .map((el) => `${name(el)} (right ${Math.round(el.getBoundingClientRect().right)})`);
    return { doc, vw, frames, offenders };
  }, width);
}

/** The address the first `selector` match of the list page opens, or why there is none (the test is then skipped).
 *  A link gives its href. A row with no href (a kit DataTable `onRowClick` row: router push, or a drawer whose URL
 *  follows it by history.replaceState) is focused and opened with Enter, never clicked, so none of its own controls
 *  (a status switch, an action menu) is hit; the address it moves to is the target. Read-only: nothing is written. */
async function openFirstTarget(page: Page, selector: string, listPath: string): Promise<{ href: string | null; reason: string }> {
  const first = page.locator(selector).first();
  const found = await first.waitFor({ state: 'attached', timeout: 10_000 }).then(() => true).catch(() => false);
  if (!found) return { href: null, reason: `no link ${selector} on ${listPath} to open (an empty list on this env)` };
  const href = await first.getAttribute('href');
  if (href) return { href, reason: '' };
  const from = new URL(page.url()).pathname;
  await first.focus();
  await first.press('Enter');
  const moved = await page
    .waitForFunction((start) => window.location.pathname !== start, from, { timeout: 10_000 })
    .then(() => true)
    .catch(() => false);
  if (!moved) return { href: null, reason: `the first ${selector} on ${listPath} opened no page` };
  const url = new URL(page.url());
  return { href: `${url.pathname}${url.search}`, reason: '' };
}

for (const p of selectPages(PAGES, { auth: AUTH, slugs: SLUGS, gallery: GALLERY, selftest: SELFTEST })) {
  for (const theme of THEMES) {
    test(`${p.slug} ${theme}`, async ({ page, context }, info) => {
      const viewport = info.project.name.replace(/^visual-(staff-)?/, '');
      if (AUTH === 'staff') {
        const role = smokeRole();
        test.skip(!roleMaySeePage(role, p.path), `the E2E role "${role}" may not open ${p.path}`);
      }
      const origin = baseOrigin();
      const consoleErrors: string[] = [];
      const failed: string[] = [];
      await installBypassRoute(context);
      await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
      await page.addInitScript((t) => {
        try {
          window.localStorage.setItem('cis_theme', t);
        } catch {
          /* storage blocked: the emulated colour scheme gives the same theme */
        }
      }, theme);
      const docUrl = new URL(p.path, origin).toString();

      page.on('console', (m) => {
        if (m.type() !== 'error' || allowed(ALLOW.console, m.text())) return;
        // Chromium logs the document's own non-200 answer ("Failed to load resource: … 404"): expected on the 404 page.
        if (p.status && m.location().url === docUrl && m.text().startsWith('Failed to load resource')) return;
        consoleErrors.push(m.text().slice(0, 300));
      });
      page.on('pageerror', (e) => consoleErrors.push(`pageerror: ${e.message.slice(0, 300)}`));
      page.on('requestfailed', (r) => {
        // net::ERR_ABORTED is a request the page itself cancelled (a router prefetch, a replaced image), not a failure.
        if (r.failure()?.errorText === 'net::ERR_ABORTED') return;
        if (r.url().startsWith(origin) && !allowed(ALLOW.requests, r.url())) failed.push(`${r.failure()?.errorText ?? 'failed'} ${r.url()}`);
      });
      page.on('response', (r) => {
        const url = r.url();
        // A redirect is not a failure ("/" sends a visitor on); the page it lands on is checked by its own answer.
        if (r.status() >= 300 && r.status() < 400) return;
        if (!url.startsWith(origin) || allowed(ALLOW.requests, url)) return;
        const expected = url === docUrl ? (p.status ?? 200) : null;
        if (expected !== null ? r.status() !== expected : r.status() >= 400) failed.push(`${r.status()} ${url}`);
      });

      let res = await page.goto(p.path, { waitUntil: 'load' });
      // Every wait has its own ceiling (lesson S220: polling pages never reach networkidle).
      await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
      if (p.openFirst) {
        // A dynamic route: open the first matching link or row of the list page (none on this env = nothing to
        // capture), then load the address it opens on its own (the captured document is the dynamic route's).
        const target = await openFirstTarget(page, p.openFirst, p.path);
        test.skip(!target.href, target.reason);
        res = await page.goto(target.href as string, { waitUntil: 'load' });
        await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => {});
      }
      await page.evaluate(() => document.fonts.ready.then(() => true));
      await page
        .locator('[data-testid="admin-content-ready"], [data-kit-frame], body')
        .first()
        .waitFor({ state: 'visible', timeout: 10_000 })
        .catch(() => {});

      const appliedTheme = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));
      const hasFrame = (await page.locator('[data-kit-frame]').count()) > 0;
      const overflow = await probeOverflow(page);
      const unnamed = await unnamedControls(page).catch(() => []);
      // The gate: kit-rendered controls everywhere, every control on the gallery (not on its self-test plants).
      const unnamedGate = p.gallery && !p.selftest ? unnamed : unnamed.filter((u) => u.kit);

      const name = `${p.slug}__${viewport}__${theme}`;
      fs.mkdirSync(SHOT_DIR, { recursive: true });
      const vp = page.viewportSize() ?? { width: 1440, height: 900 };
      await growViewportToContent(page, vp.width, vp.height).catch(() => 0);
      await page.screenshot({ path: path.join(SHOT_DIR, `${name}.png`), fullPage: true });
      await page.setViewportSize(vp);

      const result = {
        page: p.slug,
        path: p.path,
        auth: AUTH,
        viewport,
        theme,
        selftest: p.selftest ?? null,
        status: res?.status() ?? null,
        appliedTheme,
        hasFrame,
        frameExpected: p.frame !== false,
        overflow,
        consoleErrors,
        failedRequests: failed,
        unnamed,
        unnamedGate,
      };
      fs.mkdirSync(REPORT_DIR, { recursive: true });
      fs.writeFileSync(path.join(REPORT_DIR, `${name}.json`), JSON.stringify(result, null, 1));

      expect.soft(res?.status(), 'document status').toBe(p.status ?? 200);
      expect.soft(appliedTheme, 'data-theme on <html>').toBe(theme);
      if (p.frame !== false) expect.soft(hasFrame, 'the kit frame [data-kit-frame] is on the page').toBe(true);
      expect.soft(overflow.doc, `page scrolls sideways: ${overflow.offenders.join(', ')}`).toBeLessThanOrEqual(overflow.vw + 1);
      const frameOffenders = overflow.frames.flatMap((f) => f.offenders).join(', ');
      expect.soft(overflow.frames, `the page frame scrolls sideways: ${frameOffenders}`).toEqual([]);
      expect.soft(consoleErrors, 'console errors').toEqual([]);
      expect.soft(failed, 'failed same-origin requests').toEqual([]);
      expect.soft(unnamedGate, 'controls with no accessible name (kit controls, or any on the kit gallery)').toEqual([]);
    });
  }
}
