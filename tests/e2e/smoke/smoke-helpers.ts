// tests/e2e/smoke/smoke-helpers.ts
// AA-732 — shared helpers for the UI smoke suite that runs against a Vercel preview on every PR.
//
// What a "page check" means (skill aa-ui-verify): the page navigates with an OK status, logs no
// console errors, makes no failing same-origin /api/ call, and shows real content (≥1 kit/table
// row or the kit empty state). Screenshots at desktop + mobile, light + dark are also captured.
import { expect, type BrowserContext, type Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';

export const SMOKE_RESULTS_DIR = 'tests/e2e/results/smoke';
// Auth storage state holds LIVE session cookies — keep it OUT of the results dir that CI uploads
// as an artifact, and out of git (see .gitignore). A workflow step deletes it after the run.
export const AUTH_DIR = 'tests/e2e/.auth';
export const ADMIN_STATE = path.join(AUTH_DIR, 'admin-state.json');
export const TENANT_STATE = path.join(AUTH_DIR, 'tenant-state.json');

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

export function baseUrl(): string {
  return process.env.BASE_URL || 'http://localhost:3001';
}

export function baseOrigin(): string {
  try {
    return new URL(baseUrl()).origin;
  } catch {
    return baseUrl();
  }
}

/**
 * Install the Vercel protection-bypass as an origin-scoped route, NOT context-level
 * extraHTTPHeaders. The secret must only ever reach the BASE_URL origin — never a third-party
 * host the page talks to. Requests to any other origin pass through untouched. No-op when the
 * secret is absent (local run).
 */
export async function installBypassRoute(context: BrowserContext): Promise<void> {
  const secret = process.env.VERCEL_AUTOMATION_BYPASS_SECRET;
  if (!secret) return;
  const origin = baseOrigin();
  await context.route('**/*', async (route) => {
    const req = route.request();
    // The route handler fires per request with that request's OWN URL — a cross-origin redirect
    // (e.g. BASE_URL → an SSO host) is a NEW request the handler re-evaluates by its new URL, so
    // the bypass header is never carried off-origin. Only requests whose origin equals BASE_URL's
    // get the header.
    let reqOrigin = '';
    try {
      reqOrigin = new URL(req.url()).origin;
    } catch {
      reqOrigin = '';
    }
    if (reqOrigin === origin) {
      const headers = {
        ...req.headers(),
        'x-vercel-protection-bypass': secret,
        // Keep the bypass cookie so client-side navigations stay bypassed.
        'x-vercel-set-bypass-cookie': 'samesitenone',
      };
      await route.continue({ headers });
    } else {
      await route.continue();
    }
  });
}

export type ApiFailure = { url: string; status: number; bodyPreview: string };
export type ConsoleError = { url: string; text: string };

export type PageWatcher = {
  consoleErrors: ConsoleError[];
  apiFailures: ApiFailure[];
  /** Resolve after all in-flight API body reads have completed. Call before asserting. */
  settle: () => Promise<void>;
};

/**
 * Attach listeners that record console errors and failing SAME-ORIGIN `/api/` responses. Body
 * reads are async, so we track their promises and expose `settle()` to await them before the
 * assertion (otherwise a late 500 can be missed). S212: read the body — a 500 once looked like a
 * 401.
 */
export function watchPage(page: Page): PageWatcher {
  const consoleErrors: ConsoleError[] = [];
  const apiFailures: ApiFailure[] = [];
  const pending: Promise<void>[] = [];
  const origin = baseOrigin();

  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    if (CONSOLE_ALLOWLIST.some((a) => text.includes(a))) return;
    consoleErrors.push({ url: page.url(), text });
  });
  page.on('pageerror', (err) => {
    consoleErrors.push({ url: page.url(), text: err.message });
  });

  page.on('response', (res) => {
    const url = res.url();
    let u: URL;
    try {
      u = new URL(url);
    } catch {
      return;
    }
    // Same-origin API calls the page itself makes.
    if (u.origin !== origin || !u.pathname.startsWith('/api/')) return;
    const status = res.status();
    if (status < 400) return;
    pending.push(
      (async () => {
        let bodyPreview = '';
        try {
          bodyPreview = (await res.text()).slice(0, 300);
        } catch {
          bodyPreview = '<body unavailable>';
        }
        apiFailures.push({ url, status, bodyPreview });
      })(),
    );
  });

  return {
    consoleErrors,
    apiFailures,
    settle: async () => {
      await Promise.all(pending);
    },
  };
}

/**
 * True when the page shows ≥1 data row or the kit empty state — "page rendered real content, not a
 * blank/broken shell". Kit testids first; then a legacy `main table tbody tr` fallback that
 * excludes the kit's loading skeleton (the kit renders its skeleton as shimmer <div>s, NOT table
 * rows, so a real `<tbody><tr>` is genuine data); then the exact kit empty-state wording scoped to
 * `main`. No broad text regex.
 */
export async function hasContentOrEmptyState(page: Page): Promise<boolean> {
  // Kit DataTable row / kit EmptyState — the robust UI-v2 signal.
  if ((await page.locator('[data-testid="kit-datatable-row"]').count()) > 0) return true;
  if ((await page.locator('[data-testid="kit-empty-state"]').count()) > 0) return true;

  // Legacy fallback: a real data row inside a <main> table body (kit skeleton is <div>, not <tr>).
  if ((await page.locator('main table tbody tr').count()) > 0) return true;

  // Legacy empty-state copy — scoped to <main>, exact phrases only (no broad regex).
  const emptyPhrases = ['Nothing here yet', 'No results', 'No data'];
  for (const phrase of emptyPhrases) {
    if ((await page.locator('main', { hasText: phrase }).count()) > 0) return true;
  }
  return false;
}

/** Wait for the kit loading skeleton to resolve into real content (data row, empty state, or a
 * legacy table row) before reading content. Bounded + non-fatal. */
export async function waitForSkeletonGone(page: Page): Promise<void> {
  // The kit shows a shimmer skeleton (not a <tbody>) while loading; once data (or empty) resolves,
  // either the tbody appears or the EmptyState renders. Give it a bounded wait, non-fatal.
  await page
    .waitForFunction(
      () => {
        const hasBody = document.querySelector('[data-testid="kit-datatable-body"]');
        const hasEmpty = document.querySelector('[data-testid="kit-empty-state"]');
        const hasLegacy = document.querySelector('main table tbody tr');
        return Boolean(hasBody || hasEmpty || hasLegacy);
      },
      { timeout: 8000 },
    )
    .catch(() => {});
}

/** Capture desktop (1440) + mobile (390) screenshots in both light and dark color schemes on the
 * given (already-authenticated) page. */
export async function screenshotMatrix(
  page: Page,
  pathToVisit: string,
  slug: string,
): Promise<void> {
  const viewports = [
    { name: 'desktop', width: 1440, height: 900 },
    { name: 'mobile', width: 390, height: 844 },
  ];
  const schemes: ('light' | 'dark')[] = ['light', 'dark'];
  for (const vp of viewports) {
    for (const scheme of schemes) {
      await page.setViewportSize({ width: vp.width, height: vp.height });
      await page.emulateMedia({ colorScheme: scheme });
      await page.goto(pathToVisit, { waitUntil: 'networkidle' }).catch(() => {});
      await page.screenshot({
        path: path.join(SMOKE_RESULTS_DIR, `${slug}-${vp.name}-${scheme}.png`),
        fullPage: true,
      });
    }
  }
  // Reset color scheme so it does not leak into later navigations on the same page.
  await page.emulateMedia({ colorScheme: 'light' }).catch(() => {});
}

export function ensureResultsDir(): void {
  fs.mkdirSync(SMOKE_RESULTS_DIR, { recursive: true });
}

export function ensureAuthDir(): void {
  fs.mkdirSync(AUTH_DIR, { recursive: true });
}

/** Build a failure message that embeds the recorded API failures (URL + status + body preview). */
export function describeApiFailures(failures: ApiFailure[]): string {
  return failures
    .map((f) => `  ${f.status} ${f.url}\n    body: ${f.bodyPreview.replace(/\n/g, ' ')}`)
    .join('\n');
}

/** Navigate and assert the main response was OK (2xx/3xx), then let client fetches settle. */
export async function assertNavOk(page: Page, pathToVisit: string): Promise<void> {
  const resp = await page.goto(pathToVisit, { waitUntil: 'domcontentloaded' });
  expect(resp, `no response for ${pathToVisit}`).not.toBeNull();
  const status = resp!.status();
  expect(status, `navigation to ${pathToVisit} returned HTTP ${status}`).toBeLessThan(400);
  await page.waitForLoadState('networkidle').catch(() => {});
}
