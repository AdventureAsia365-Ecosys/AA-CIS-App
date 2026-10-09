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
  /** AA-601 — optional CSS selector that marks "this page's real content has rendered". Set it for
   * pages whose loaded state is neither a kit/table row nor the kit empty state (card lists, a
   * config view, a custom empty-state). The element MUST render only after data has loaded, never
   * on the loading skeleton, so a blank/broken page still fails. */
  readySelector?: string;
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
      // Bounded: a streaming / long-poll body would otherwise never resolve.
      await Promise.race([Promise.all(pending), new Promise((r) => setTimeout(r, 10000))]);
    },
  };
}

/**
 * True when the page shows ≥1 data row or the kit empty state — "page rendered real content, not a
 * blank/broken shell". Kit testids first; then a legacy `table tbody tr` fallback (legacy pages have no <main>; the sidebar has no table) that
 * excludes the kit's loading skeleton (the kit renders its skeleton as shimmer <div>s, NOT table
 * rows, so a real `<tbody><tr>` is genuine data); then the exact kit empty-state wording scoped to
 * `main`. No broad text regex.
 */
export async function hasContentOrEmptyState(page: Page, readySelector?: string): Promise<boolean> {
  // AA-601 — a page-specific "content rendered" signal (card lists, config view, custom empty
  // state). It renders only after data loads, never on the skeleton, so it is a genuine signal.
  if (readySelector && (await page.locator(readySelector).count()) > 0) return true;

  // Kit DataTable row / kit EmptyState — the robust UI-v2 signal.
  if ((await page.locator('[data-testid="kit-datatable-row"]').count()) > 0) return true;
  if ((await page.locator('[data-testid="kit-empty-state"]').count()) > 0) return true;

  // Legacy fallback: a real data row inside a <main> table body (kit skeleton is <div>, not <tr>).
  if ((await page.locator('table tbody tr').count()) > 0) return true;

  // Legacy empty-state copy — exact phrases only (no broad regex).
  const emptyPhrases = ['Nothing here yet', 'No results', 'No data'];
  for (const phrase of emptyPhrases) {
    if ((await page.locator('body', { hasText: phrase }).count()) > 0) return true;
  }
  return false;
}

/** Wait for the kit loading skeleton to resolve into real content (data row, empty state, or a
 * legacy table row) before reading content. Bounded + non-fatal. */
export async function waitForSkeletonGone(page: Page, readySelector?: string): Promise<void> {
  // The kit shows a shimmer skeleton (not a <tbody>) while loading; once data (or empty) resolves,
  // either the tbody appears, the EmptyState renders, or the page's own readySelector appears. Give
  // it a bounded wait, non-fatal.
  await page
    .waitForFunction(
      (sel) => {
        const hasBody = document.querySelector('[data-testid="kit-datatable-body"]');
        const hasEmpty = document.querySelector('[data-testid="kit-empty-state"]');
        const hasLegacy = document.querySelector('table tbody tr');
        const hasReady = sel ? document.querySelector(sel) : null;
        return Boolean(hasBody || hasEmpty || hasLegacy || hasReady);
      },
      readySelector ?? null, // waitForFunction(fn, arg, options) — options is the THIRD param
      { timeout: 8000 },
    )
    .catch(() => {});
}

/** AA-601 — at 390px, assert the admin page is mobile-safe:
 *   - no PAGE-LEVEL horizontal overflow (wide tables must scroll inside their own container, not
 *     the document);
 *   - the off-canvas sidebar takes no width, so the main content starts near the left edge
 *     (< 40px — it is no longer pushed ~236px right by the sidebar).
 * Call after the page is loaded and content has rendered. Resets to the desktop viewport after. */
export async function assertMobileLayout(page: Page, label: string): Promise<void> {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(250); // let the layout settle at the new width

  const metrics = await page.evaluate(() => {
    const doc = document.documentElement;
    const innerWidth = window.innerWidth;
    // The admin main container is tagged `.aa-admin-main` (sibling of the sidebar).
    const main = document.querySelector('.aa-admin-main') as HTMLElement | null;
    const mainLeft = main ? main.getBoundingClientRect().left : 0;

    // AA-752 (feedback 1): when the page overflows, list the elements whose right edge is past the
    // viewport and that are NOT inside an overflow-x:auto/hidden ancestor (those scroll internally,
    // so they are allowed). The failure message then explains itself.
    const scrollable = (el: HTMLElement): boolean => {
      let node: HTMLElement | null = el.parentElement;
      while (node && node !== document.documentElement) {
        const ox = getComputedStyle(node).overflowX;
        if (ox === 'auto' || ox === 'hidden' || ox === 'scroll') return true;
        node = node.parentElement;
      }
      return false;
    };
    const offenders: { tag: string; cls: string; testid: string; width: number; right: number }[] = [];
    for (const el of Array.from(document.body.querySelectorAll<HTMLElement>('*'))) {
      const r = el.getBoundingClientRect();
      if (r.right <= innerWidth + 1) continue;
      if (r.width === 0 || r.height === 0) continue;
      if (scrollable(el)) continue;
      offenders.push({
        tag: el.tagName.toLowerCase(),
        cls: (typeof el.className === 'string' ? el.className : '').slice(0, 48),
        testid: el.getAttribute('data-testid') ?? '',
        width: Math.round(r.width),
        right: Math.round(r.right),
      });
    }
    // Keep the widest / furthest-right few so the message stays readable.
    offenders.sort((a, b) => b.right - a.right);
    return {
      scrollWidth: doc.scrollWidth,
      innerWidth,
      mainLeft,
      hasMain: Boolean(main),
      offenders: offenders.slice(0, 12),
    };
  });

  const offenderText = metrics.offenders
    .map((o) => `    <${o.tag} class="${o.cls}" data-testid="${o.testid}"> width=${o.width} right=${o.right}`)
    .join('\n');

  expect(
    metrics.scrollWidth,
    `${label}: page-level horizontal overflow at 390px (scrollWidth ${metrics.scrollWidth} > innerWidth ${metrics.innerWidth}). ` +
      `Wide content must scroll inside its own container, not the page. Offenders (right > innerWidth, not in an overflow-x ancestor):\n${offenderText || '    (none found — overflow may be from a scrollWidth-only element)'}`,
  ).toBeLessThanOrEqual(metrics.innerWidth + 1);

  if (metrics.hasMain) {
    expect(
      metrics.mainLeft,
      `${label}: main content left edge is ${metrics.mainLeft}px at 390px — the sidebar is still taking width (it must be an off-canvas drawer < 768px).`,
    ).toBeLessThan(40);
  }

  // Back to desktop for any later work on this page.
  await page.setViewportSize({ width: 1440, height: 900 });
}

/** AA-601 part B — assert the admin page actually renders a DARK theme under
 * `prefers-color-scheme: dark` and a LIGHT theme under light. Bounded + deterministic (no network,
 * no screenshot diff): it reads the computed background-color of `.aa-admin-main` (fallback: body),
 * converts to relative luminance (WCAG), and asserts luminance < 0.2 for dark and > 0.8 for light.
 * The admin follows the emulated color scheme because the layout.tsx restore script resolves
 * "no stored choice" via matchMedia, and the theme store listens for scheme changes. Resets the
 * color scheme to light after. */
export async function assertThemeSwitches(page: Page, label: string): Promise<void> {
  // Measure under a given emulated scheme after letting the theme store + CSS vars settle.
  const measure = async (scheme: 'light' | 'dark'): Promise<number> => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.waitForTimeout(300);
    return page.evaluate(() => {
      const el = (document.querySelector('.aa-admin-main') as HTMLElement | null) ?? document.body;
      // Walk up for the first non-transparent background so a transparent main still resolves.
      let node: HTMLElement | null = el;
      let bg = '';
      while (node) {
        const c = getComputedStyle(node).backgroundColor;
        if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') { bg = c; break; }
        node = node.parentElement;
      }
      const m = bg.match(/rgba?\(([^)]+)\)/);
      if (!m) return 1; // no colour read → treat as light (fails the dark assertion loudly)
      const [r, g, b] = m[1].split(',').slice(0, 3).map((s) => parseInt(s.trim(), 10) / 255);
      const lin = (v: number) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
      return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
    });
  };

  const darkLum = await measure('dark');
  expect(
    darkLum,
    `${label}: expected a DARK admin background under colorScheme=dark (relative luminance < 0.2) but got ${darkLum.toFixed(3)} — the dark theme did not apply.`,
  ).toBeLessThan(0.2);

  // AA-601 part B (feedback 2) — regression guard. Still under colorScheme=dark: fail if any
  // visible element has a background with relative luminance > 0.75 and an area ≥ 2,000 px²,
  // EXCLUDING the accent/badge surfaces (whose background IS the brand accent / a solid pill — they
  // are meant to stay bright in both themes). This catches a surface (header, input, panel, tab
  // strip, table row) that stayed white in dark. Bounded + deterministic — pure DOM read.
  const offenders = await page.evaluate(() => {
    const lin = (v: number) => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4));
    const lum = (r: number, g: number, b: number) =>
      0.2126 * lin(r / 255) + 0.7152 * lin(g / 255) + 0.0722 * lin(b / 255);
    // Resolve the brand accent (gold) in the current theme so we can exclude accent-coloured
    // surfaces (gold buttons/badges) regardless of the hex.
    const cs = getComputedStyle(document.documentElement);
    const accentRaw = cs.getPropertyValue('--aa-accent').trim();
    const probe = document.createElement('span');
    probe.style.color = accentRaw || '#DB9628';
    document.body.appendChild(probe);
    const accentRgb = getComputedStyle(probe).color;
    probe.remove();
    const am = accentRgb.match(/rgba?\(([^)]+)\)/);
    const accentLum = am
      ? lum(...(am[1].split(',').slice(0, 3).map((s) => parseInt(s.trim(), 10)) as [number, number, number]))
      : 0.5;

    const scope = (document.querySelector('.aa-admin-main') as HTMLElement | null) ?? document.body;
    const out: { tag: string; cls: string; text: string; rgb: string; lum: number; area: number }[] = [];
    const els = scope.querySelectorAll<HTMLElement>('*');
    for (const el of Array.from(els)) {
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
      const bg = st.backgroundColor;
      const m = bg.match(/rgba?\(([^)]+)\)/);
      if (!m) continue;
      const parts = m[1].split(',').map((s) => parseFloat(s.trim()));
      const [r, g, b] = parts;
      const a = parts.length > 3 ? parts[3] : 1;
      if (a < 0.5) continue; // translucent overlays are not a solid white surface
      const L = lum(r, g, b);
      if (L <= 0.75) continue;
      // Exclude accent/badge surfaces (their bg ≈ the bright gold accent). If the accent itself is
      // bright (dark theme uses a lighter gold), skip elements whose bg luminance is close to it.
      if (Math.abs(L - accentLum) < 0.12) continue;
      const rect = el.getBoundingClientRect();
      const area = rect.width * rect.height;
      if (area < 2000) continue;
      out.push({
        tag: el.tagName.toLowerCase(),
        cls: (el.className && typeof el.className === 'string' ? el.className : '').slice(0, 40),
        text: (el.textContent ?? '').trim().replace(/\s+/g, ' ').slice(0, 40),
        rgb: bg,
        lum: Math.round(L * 100) / 100,
        area: Math.round(area),
      });
    }
    return out;
  });

  expect(
    offenders,
    `${label}: ${offenders.length} bright surface(s) remained light under colorScheme=dark (bg luminance > 0.75, area ≥ 2000px², not the accent). Offenders:\n` +
      offenders
        .map((o) => `  <${o.tag} class="${o.cls}"> lum=${o.lum} area=${o.area} rgb=${o.rgb} text="${o.text}"`)
        .join('\n'),
  ).toEqual([]);

  const lightLum = await measure('light');
  expect(
    lightLum,
    `${label}: expected a LIGHT admin background under colorScheme=light (relative luminance > 0.8) but got ${lightLum.toFixed(3)} — the light theme did not apply.`,
  ).toBeGreaterThan(0.8);

  await page.emulateMedia({ colorScheme: 'light' }).catch(() => {});
}


/** Capture desktop (1440) + mobile (390) screenshots in both light and dark color schemes on the
 * given (already-loaded, already-checked) page.
 *
 * No reload per shot: viewport size and `prefers-color-scheme` apply live, and reloading a long page
 * 4 times with networkidle + full-page capture blew the 60 s test timeout on Master Content (S220
 * live run). Desktop is full-page; mobile is the first screen (a 390px full-page list is huge). */
export async function screenshotMatrix(page: Page, slug: string): Promise<void> {
  const viewports = [
    { name: 'desktop', width: 1440, height: 900, fullPage: true },
    { name: 'mobile', width: 390, height: 844, fullPage: false },
  ];
  const schemes: ('light' | 'dark')[] = ['light', 'dark'];
  for (const vp of viewports) {
    await page.setViewportSize({ width: vp.width, height: vp.height });
    for (const scheme of schemes) {
      await page.emulateMedia({ colorScheme: scheme });
      await page.waitForTimeout(300); // let layout / theme transitions settle
      await page.screenshot({
        path: path.join(SMOKE_RESULTS_DIR, `${slug}-${vp.name}-${scheme}.png`),
        fullPage: vp.fullPage,
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
  // Bounded: a page that polls (or keeps a long-lived request open) never reaches networkidle, and an
  // unbounded wait burned the whole 60 s test timeout on Master Content (S220 live run).
  await page.waitForLoadState('networkidle', { timeout: 15000 }).catch(() => {});
}
