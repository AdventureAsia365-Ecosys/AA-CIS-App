// tests/e2e/smoke/smoke-helpers.ts
// AA-732 — shared helpers for the UI smoke suite that runs against a Vercel preview on every PR.
//
// What a "page check" means (skill aa-ui-verify): the page navigates with an OK status, logs no
// console errors, makes no failing same-origin /api/ call, and shows real content (≥1 table row or
// the kit empty state). We also capture screenshots at desktop + mobile, light + dark.
import { expect, type BrowserContext, type Page, type Browser } from '@playwright/test';
import fs from 'fs';
import path from 'path';

export const SMOKE_RESULTS_DIR = 'tests/e2e/results/smoke';

/** Known-benign console messages. Empty by default — add a precise substring to silence real
 * third-party noise, never to hide an app error. */
export const CONSOLE_ALLOWLIST: string[] = [];

export type PageSpec = {
  /** Short slug for screenshot filenames. */
  id: string;
  /** Human label used in test titles. */
  label: string;
  /** Route to navigate to (path only; BASE_URL is prepended by Playwright). */
  path: string;
};

/** The Vercel protection-bypass headers, only when the secret is present (preview is behind SSO).
 * Returning the header set lets the browser context forward them on every request, and the
 * `set-bypass-cookie` tells Vercel to set a cookie so client-side navigations stay bypassed. */
export function bypassHeaders(): Record<string, string> {
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  if (!secret) return {};
  return {
    'x-vercel-protection-bypass': secret,
    'x-vercel-set-bypass-cookie': 'samesitenone',
  };
}

export type ApiFailure = { url: string; status: number; bodyPreview: string };
export type ConsoleError = { url: string; text: string };

/** Attach listeners that record console errors and failing same-origin /api/ responses. The
 * returned arrays fill as the page runs; assert on them after each navigation. */
export function watchPage(page: Page): { consoleErrors: ConsoleError[]; apiFailures: ApiFailure[] } {
  const consoleErrors: ConsoleError[] = [];
  const apiFailures: ApiFailure[] = [];

  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    if (CONSOLE_ALLOWLIST.some((a) => text.includes(a))) return;
    consoleErrors.push({ url: page.url(), text });
  });
  page.on('pageerror', (err) => {
    consoleErrors.push({ url: page.url(), text: err.message });
  });

  page.on('response', async (res) => {
    const url = res.url();
    // Only same-origin API calls the page itself makes.
    if (!url.includes('/api/')) return;
    const status = res.status();
    if (status < 400) return;
    // S212 lesson: read the body, a 500 once looked like a 401 — record the first 300 chars.
    let bodyPreview = '';
    try {
      bodyPreview = (await res.text()).slice(0, 300);
    } catch {
      bodyPreview = '<body unavailable>';
    }
    apiFailures.push({ url, status, bodyPreview });
  });

  return { consoleErrors, apiFailures };
}

/** True when the page shows ≥1 data row or an empty state — the "page rendered real content, not a
 * blank/broken shell" check. Works for both the UI-v2 kit (data-testid) and legacy tables/empty
 * copy. */
export async function hasContentOrEmptyState(page: Page): Promise<boolean> {
  const candidates = [
    '[data-testid="kit-datatable-row"]',
    '[data-testid="kit-empty-state"]',
    'table tbody tr',
    'text=/no .* yet|nothing here|empty|no results|no data/i',
  ];
  for (const sel of candidates) {
    if ((await page.locator(sel).count()) > 0) return true;
  }
  return false;
}

/** Capture desktop (1440) + mobile (390) screenshots in both light and dark color schemes. */
export async function screenshotMatrix(browser: Browser, pathToVisit: string, slug: string, headers: Record<string, string>): Promise<void> {
  const viewports = [
    { name: 'desktop', width: 1440, height: 900 },
    { name: 'mobile', width: 390, height: 844 },
  ];
  const schemes: ('light' | 'dark')[] = ['light', 'dark'];
  for (const vp of viewports) {
    for (const scheme of schemes) {
      const ctx = await browser.newContext({
        viewport: { width: vp.width, height: vp.height },
        colorScheme: scheme,
        extraHTTPHeaders: headers,
        storageState: STORAGE_STATE,
      });
      const page = await ctx.newPage();
      try {
        await page.goto(pathToVisit, { waitUntil: 'networkidle' }).catch(() => {});
        await page.screenshot({
          path: path.join(SMOKE_RESULTS_DIR, `${slug}-${vp.name}-${scheme}.png`),
          fullPage: true,
        });
      } finally {
        await ctx.close();
      }
    }
  }
}

export const STORAGE_STATE = path.join(SMOKE_RESULTS_DIR, 'admin-state.json');

export function ensureResultsDir(): void {
  fs.mkdirSync(SMOKE_RESULTS_DIR, { recursive: true });
}

/** Build a failure message that embeds the recorded API failures (URL + status + body preview). */
export function describeApiFailures(failures: ApiFailure[]): string {
  return failures
    .map((f) => `  ${f.status} ${f.url}\n    body: ${f.bodyPreview.replace(/\n/g, ' ')}`)
    .join('\n');
}

/** Assert a navigation response was OK (2xx/3xx). Playwright's goto returns the main response. */
export async function assertNavOk(page: Page, pathToVisit: string, headers: Record<string, string>): Promise<void> {
  const resp = await page.goto(pathToVisit, { waitUntil: 'domcontentloaded' });
  expect(resp, `no response for ${pathToVisit}`).not.toBeNull();
  const status = resp!.status();
  expect(status, `navigation to ${pathToVisit} returned HTTP ${status}`).toBeLessThan(400);
  // Give client-side fetches a moment to run and fail, if they will.
  await page.waitForLoadState('networkidle').catch(() => {});
}
