// tests/e2e/smoke/aa1133-docs-shared-heading.spec.ts
// AA-1133 follow-up (S284 review): a search hit puts the heading in the address (?path=…#doc-<slug>) so the place can
// be shared, but a fresh load of that address stayed at the top of the page: the browser's own fragment scroll runs
// before the page text is loaded, and the reader only jumped after in-app clicks. The reader now seeds its jump from
// the address (lib/docsToc.ts addressHeading; lib/docsToc.test.mjs is the unit guard) and makes it once the reader is
// measured. This spec checks the real page.
// Every staff role reads /admin/docs (frontend/lib/rbac.ts), so it runs in any smoke role. Read-only. Every wait is
// bounded. At 1920 x 1080 (the table of contents sits beside the text):
//   (a) a fresh load of the catalog page at one of its later headings shows that heading on screen;
//   (b) the table of contents marks that heading (aria-current="location").
import { test, expect } from '@playwright/test';
import { ADMIN_STATE, baseUrl, installBypassRoute } from './smoke-helpers';

// The catalog page (root docs/README.md): published for every staff role and long enough to need a jump.
const DOC_PATH = 'docs/README.md';

test.describe('AA-1133 — a shared docs heading link opens at that heading', { tag: '@staff' }, () => {
  test('(a)+(b) ?path=…#doc-<slug> lands on the heading and marks it', async ({ browser }) => {
    const context = await browser.newContext({
      baseURL: baseUrl(), storageState: ADMIN_STATE, viewport: { width: 1920, height: 1080 },
    });
    await installBypassRoute(context);
    const page = await context.newPage();
    try {
      const reader = `/admin/docs?path=${encodeURIComponent(DOC_PATH)}`;
      await page.goto(reader, { waitUntil: 'domcontentloaded' });
      const toc = page.locator('nav[data-testid="docs-toc"]');
      await expect(toc).toBeVisible({ timeout: 30000 });
      const hrefs = await toc.locator('a').evaluateAll((els) => els.map((a) => a.getAttribute('href') ?? ''));
      expect(hrefs.length, 'the catalog page lists several headings').toBeGreaterThan(3);
      // A heading well down the page (not the last one: a short last section cannot reach the top of the screen).
      const hash = hrefs[hrefs.length - 2];
      expect(hash).toMatch(/^#doc-/);

      // A fresh load of the shared address (a new page, so nothing is left from the first visit).
      const shared = await context.newPage();
      await shared.goto(`${reader}${hash}`, { waitUntil: 'domcontentloaded' });
      const heading = shared.locator(`[id="${decodeURIComponent(hash.slice(1))}"]`);
      await expect(heading).toBeAttached({ timeout: 30000 });
      // (a) the reader jumped to it (smooth scroll: allow it to finish)
      await expect(heading).toBeInViewport({ timeout: 10000 });
      // (b) the side table of contents marks it
      const mark = shared.locator(`nav[data-testid="docs-toc"] a[href="${hash}"]`);
      await expect(mark).toHaveAttribute('aria-current', 'location', { timeout: 10000 });
    } finally {
      await context.close();
    }
  });
});
