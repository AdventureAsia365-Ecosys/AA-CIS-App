// AA-791 — Content Quality "Label" UI check with a MOCKED admin API (no Dev data, no LLM).
//
// What it proves: (1) an unlabelled piece opens with NO verdict selected and Save disabled until a
// verdict is picked (S239 fix: it used to pre-select "As-is", so a stray Save stored a wrong
// label); (2) the saved body carries the chosen verdict, the trimmed reason and the defects;
// (3) a piece the admin already labelled opens with that verdict selected and "Update my label";
// (4) the LABEL column is inside the viewport at 1440x900 without scrolling the table sideways,
// even with long gate-chip lists in the Blocking fails / Warnings columns.
//
// How to run (local only — every /api/admin call is answered by page.route below, so it must be
// a `next dev` server whose middleware fails open when the API is unreachable; point the two API
// URL env vars of the frontend at 127.0.0.1:9 when starting it):
//   cd frontend && npx next dev -p 3791
//   BASE_URL=http://localhost:3791 AA791_SHOTS=<dir> npx playwright test tests/e2e/aa791-label-form.spec.ts --project=chromium
// The live check (one real save on Dev as the e2e admin) is in docs/implementation-notes/AA-791.md.
import { test, expect, type Page, type Route } from '@playwright/test';

const SHOTS = process.env.AA791_SHOTS || 'tests/e2e/results/aa791';
const UNLABELLED = '79100000-0000-4000-8000-000000000001';
const LABELLED = '79100000-0000-4000-8000-000000000002';

const MANY_GATES = ['F1_hook', 'F2_brand_forbidden', 'F3_length', 'F5_cta_tone', 'F7_hashtags', 'F12_emoji', 'F14_first_line'];

function row(id: string, label: string | null) {
  return {
    piece_id: id, tenant_id: 't1', tenant_name: 'Trail Pulse (test)', channel: 'facebook', status: 'approved',
    held_reason: null, attempt_number: 2, created_at: '2026-10-15T08:00:00Z', job_id: null,
    title: 'Dawn on the Mekong — a long angle title that the table has to ellipsize somewhere', angle_name: 'Dawn on the Mekong',
    goal: 'inspiration', content_chars: 900, has_writer_input: true,
    failed_gates: MANY_GATES.slice(0, 4), flag_gates: MANY_GATES, cost_usd: 0.0123,
    label, label_count: label ? 2 : 0,
  };
}

function detail(id: string) {
  return {
    ...row(id, null),
    content_text: 'Mist lifts off the water as the long-tail boat noses away from Luang Prabang.',
    seo_title: null, meta_description: null, slug: null,
    gate_ledger: [{ gate: 'F1_hook', passed: true, blocking: true, violations: [] }],
    flags: null, repair_log: [], discarded_attempts: [], writer_input: null, angle_why: null, angle_style: null,
    highlights: [], flagged_sentences: [], published: false, exported: false,
    tenant_copy: { published_at: null, exported_at: null, delivered_at: null },
    admin_edits: [], cost_by_stage: [], other_pieces_for_request: [],
  };
}

const MINE = {
  labelled_by: 'admin:e2e', label: 'heavy', defects: { voice: 'heavy' }, minutes_to_fix: 25,
  reason: 'Voice drifts', labelled_at: '2026-10-15T09:00:00Z', text_changed_since_label: false,
};

async function mockAdminApi(page: Page, posts: unknown[]) {
  await page.route('**/api/admin/**', async (route: Route) => {
    const req = route.request();
    const p = new URL(req.url()).pathname.replace(/^\/api\/admin/, '');
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (p === '/content-quality/pieces') {
      return json({ data: [row(UNLABELLED, null), row(LABELLED, 'heavy')], total: 2, limit: 50, offset: 0, labels_enabled: true });
    }
    if (p === '/content-quality/stats') return json({ gates: [], tenants: [{ tenant_id: 't1', tenant_name: 'Trail Pulse (test)' }] });
    const lab = p.match(/^\/content-quality\/pieces\/([^/]+)\/label$/);
    if (lab) {
      const id = lab[1];
      if (req.method() === 'POST') {
        const body = JSON.parse(req.postData() || '{}');
        posts.push(body);
        const mine = { ...body, labelled_by: 'admin:e2e', labelled_at: '2026-10-16T10:00:00Z', text_changed_since_label: false };
        return json({ piece_id: id, labels: [mine], mine });
      }
      const mine = id === LABELLED ? MINE : null;
      return json({ piece_id: id, labels: mine ? [mine] : [], mine });
    }
    const one = p.match(/^\/content-quality\/pieces\/([^/]+)$/);
    if (one) return json(detail(one[1]));
    return json({});
  });
}

async function login(page: Page, baseURL: string) {
  const host = new URL(baseURL).hostname;
  await page.context().addCookies([
    { name: 'cis_admin_token', value: 'aa791-mock', domain: host, path: '/' },
    { name: 'cis_role', value: 'admin', domain: host, path: '/' },
    { name: 'cis_user', value: 'e2e', domain: host, path: '/' },
  ]);
}

test.describe('AA-791 Content Quality label (mocked API)', () => {
  test('1440x900: LABEL column on screen; unlabelled piece has no verdict and Save waits for one', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    const posts: unknown[] = [];
    await mockAdminApi(page, posts);
    await login(page, baseURL!);
    const consoleErrors: string[] = [];
    page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });

    await page.goto('/admin/content-quality');
    await expect(page.getByTestId('content-quality-ready')).toBeVisible({ timeout: 30_000 });
    const header = page.getByRole('columnheader', { name: /^Label/ });
    await expect(header).toBeVisible({ timeout: 30_000 });
    // In the viewport without any horizontal scroll of the table or the page.
    const box = await header.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x + box!.width).toBeLessThanOrEqual(1440);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
    await page.screenshot({ path: `${SHOTS}/01-pieces-label-column-1440.png` });

    await page.getByText('Dawn on the Mekong — a long').first().click();
    const form = page.getByTestId('cq-label-form');
    await expect(form).toBeVisible({ timeout: 15_000 });
    for (const l of ['as_is', 'light', 'heavy', 'reject']) {
      await expect(page.getByTestId(`cq-label-${l}`)).toHaveAttribute('aria-checked', 'false');
    }
    const save = page.getByTestId('cq-label-save').getByRole('button');
    await expect(save).toBeDisabled();
    await expect(page.getByTestId('cq-label-pick-hint')).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/02-unlabelled-no-verdict-1440.png` });

    await page.getByTestId('cq-label-light').click();
    await expect(page.getByTestId('cq-label-light')).toHaveAttribute('aria-checked', 'true');
    await page.getByLabel('Voice defects').selectOption('light');
    await page.getByLabel('Minutes to fix').fill('8');
    await page.getByLabel('Reason').fill('  Two sentences read like an ad.  ');
    await expect(save).toBeEnabled();
    await save.click();
    await expect.poll(() => posts.length).toBe(1);
    expect(posts[0]).toEqual({ label: 'light', defects: { voice: 'light' }, minutes_to_fix: 8, reason: 'Two sentences read like an ad.' });
    await expect(page.getByText('Labelled Light edit')).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/03-saved-light-1440.png` });

    expect(consoleErrors.filter(e => !/favicon|Failed to load resource|sentry|data-sidebar|hydrat/i.test(e))).toEqual([]);
  });

  test('a piece I already labelled opens with my verdict and "Update my label"', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    await mockAdminApi(page, []);
    await login(page, baseURL!);
    await page.goto(`/admin/content-quality?piece=${LABELLED}`);
    await expect(page.getByTestId('cq-label-form')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId('cq-label-heavy')).toHaveAttribute('aria-checked', 'true');
    await expect(page.getByTestId('cq-label-as_is')).toHaveAttribute('aria-checked', 'false');
    await expect(page.getByRole('button', { name: 'Update my label' })).toBeEnabled();
    await expect(page.getByTestId('cq-label-pick-hint')).toHaveCount(0);
  });
});
