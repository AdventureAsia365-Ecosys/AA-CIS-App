// AA-668 — Writer panel (Social Content) UI check with a MOCKED tenant API.
//
// What it proves, with no LLM spend and no Dev data: picking a Subject opens the right-side
// Writer panel; Goal cards → "Generate 3 angles" → Angle cards → one click on "Write with this
// angle" starts the write WITHOUT a CTA prompt (the request has cta NULL; the brand default is the
// server's job, AA-771) and without any `cta` in the write body; the live steps/text render; the
// finished piece shows with Export/Publish/My Content actions and the AI notice; the page itself
// never scrolls at 1440x900; on a phone the panel is a full-screen sheet; closing the panel while
// a piece is still being written raises a toast when it finishes.
//
// How to run (local only — every /api/tenant call is answered by page.route below, so it must be
// a `next dev` server whose middleware fails open when the API is unreachable):
//   cd frontend && <API url env vars pointed at 127.0.0.1:9> npx next dev -p 3668
//   BASE_URL=http://localhost:3668 AA668_SHOTS=<dir> npx playwright test tests/e2e/aa668-writer-panel.spec.ts --project=chromium
// (set NEXT_PUBLIC_API_URL and API_URL by hand). The real end-to-end flow (real T8/T9 jobs) is
// verified on the Vercel preview with a test tenant — see docs/implementation-notes/AA-668.md.
import { test, expect, type Page, type Route } from '@playwright/test';

const SHOTS = process.env.AA668_SHOTS || 'tests/e2e/results/aa668';
const REQ = '11111111-1111-4111-8111-111111111111';
const PIECE = '22222222-2222-4222-8222-222222222222';
const SUBJECT = '33333333-3333-4333-8333-333333333333';

const GOALS = [
  ['awareness', 'Build awareness', 'Introduce the place to people who have never heard of it.'],
  ['consideration', 'Drive consideration', 'Help travellers compare and shortlist this trip.'],
  ['conversion', 'Convert to enquiry', 'Turn interest into an enquiry or booking request.'],
  ['education', 'Educate', 'Answer the practical questions people ask before going.'],
  ['inspiration', 'Inspire', 'Paint the moment so readers want to be there.'],
  ['trust', 'Build trust', 'Show expertise and care in how the trip is run.'],
  ['retention', 'Re-engage past guests', 'Give past travellers a reason to come back.'],
  ['seasonal', 'Seasonal timing', 'Tie the moment to when it is best to go.'],
].map(([key, name, description]) => ({ key, name, description, logic: '', marketing_term: '' }));

const ANGLES = [
  { name: 'Dawn on the Mekong', why: 'Leads with the quiet river morning travellers search for.', rec: true },
  { name: 'Village by bicycle', why: 'Answers "is it hard?" with the real pace of the ride.', rec: false },
  { name: 'Slow travel, done right', why: 'Speaks to people tired of rushed itineraries.', rec: false },
].map((a, idx) => ({
  idx, name: a.name, why_it_works: a.why, formula_fit: 'PAS', best_final_style: 'Short, sensory paragraphs',
  recommended: a.rec, chosen: false, answers: ['Is the Mekong safe?'], violations: [],
}));

const FINAL_TEXT = [
  'Dawn on the Mekong',
  '',
  'Mist lifts off the water as the long-tail boat noses away from Luang Prabang. Monks in saffron',
  'walk the riverbank; the only sound is the engine and a kingfisher somewhere in the reeds.',
  '',
  'By mid-morning you are pedalling through rice paddies to a weaving village, where the looms',
  'have been clacking for generations.',
  '',
  'Ask us about the 5-day village cycling trip.',
].join('\n');

type State = {
  picked: boolean;
  status: 'pending_goal' | 'pending_choice' | 'approved';
  goal: string | null;
  chosen: number | null;
  anglePolls: number;
  piecePolls: number;
  writeBodies: unknown[];
};

function request(s: State) {
  return {
    request_id: REQ, atom_id: 'a1', channel: 'facebook', goal: s.goal, cta: null, status: s.status,
    generating: s.status === 'pending_goal' && s.goal !== null && s.anglePolls < 1,
    generation_failed: false,
    angles: s.status === 'pending_goal' ? [] : ANGLES.map(a => ({ ...a, chosen: a.idx === s.chosen })),
    dfs_paa_snapshot: { people_also_ask: ['Is the Mekong safe?', 'Best time to visit Laos?'] },
    subject_id: SUBJECT, subject_score: 3, subject_place: 'Mekong River', subject_action: 'slow boat at dawn',
    subject_hub_name: null,
  };
}

async function mockTenantApi(page: Page, s: State, opts: { slowPiece?: number } = {}) {
  const slowPiece = opts.slowPiece ?? 2;
  await page.route('**/api/tenant/**', async (route: Route) => {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname.replace(/^\/api\/tenant/, '');
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

    if (p === '/me') return json({ tenant_name: 'Trail Pulse (test)', plan_tier: 'growth' });
    if (p === '/v1/slate') {
      const state = s.picked ? 'picked' : 'proposed';
      const reason = { channel: 'facebook', on_demand: false, needs_demand: 0, demand: null, demand_ok: true, needs_questions: 0, questions: 0, questions_ok: true, needs_said: 400, said: 812, said_ok: true };
      const subject = (id: string, place: string, action: string, st: string) => ({
        subject_id: id, channel: 'facebook', state: st, score: 3, cleared_bar_reason: reason, segment_id: 'seg', route_id: null,
        place, action, hub_name: null, created_at: null,
      });
      const fb = [
        subject(SUBJECT, 'Mekong River', 'slow boat at dawn', state),
        subject('44444444-4444-4444-8444-444444444444', 'Ban Xang Khong', 'paper-making village visit', 'proposed'),
        subject('55555555-5555-4555-8555-555555555555', 'Kuang Si', 'swim below the falls', 'proposed'),
      ];
      const ch = (c: string, subjects: unknown[]) => ({ channel: c, on_demand: ['email', 'landing_page', 'ads'].includes(c), eligible_count: subjects.length, subjects });
      return json({
        channels: Object.fromEntries(['blog', 'linkedin', 'facebook', 'instagram', 'tiktok', 'email', 'landing_page', 'ads']
          .map(c => [c, ch(c, c === 'facebook' ? fb : [])])),
        posts_per_week: 3, unmatched_markets: [],
        refresh: { status: 'done', refreshing: false, finished_at: '2026-10-14T08:00:00Z' },
      });
    }
    if (p === `/v1/subjects/${SUBJECT}/pick` && req.method() === 'POST') { s.picked = true; return json({ request_id: REQ }); }
    if (p === '/v1/angle-gate/goals') return json({ goals: GOALS });
    if (p === `/v1/angle-gate/requests/${REQ}` && req.method() === 'GET') {
      if (s.goal !== null && s.status === 'pending_goal') {
        s.anglePolls += 1;
        if (s.anglePolls >= 1) s.status = 'pending_choice';
      }
      return json(request(s));
    }
    if (p === `/v1/angle-gate/requests/${REQ}/goal`) {
      s.goal = JSON.parse(req.postData() || '{}').goal;
      return json(request(s), 202);
    }
    if (p === `/v1/angle-gate/requests/${REQ}/choose`) {
      s.chosen = JSON.parse(req.postData() || '{}').idx;
      s.status = 'approved';
      return json(request(s));
    }
    if (p === `/v1/content-writing/requests/${REQ}/latest-piece`) return json({ piece: null });
    if (p === `/v1/content-writing/requests/${REQ}/write`) {
      s.writeBodies.push(JSON.parse(req.postData() || '{}'));
      return json({ piece_id: PIECE, angle_gate_request_id: REQ, channel: 'facebook', ready_state: 'in_progress' }, 202);
    }
    if (p === `/v1/content-writing/pieces/${PIECE}`) {
      s.piecePolls += 1;
      const done = s.piecePolls > slowPiece;
      return json({ piece_id: PIECE, angle_gate_request_id: REQ, channel: 'facebook', content_text: done ? FINAL_TEXT : null, ready_state: done ? 'ready' : 'in_progress' });
    }
    if (p === `/v1/progress/piece/${PIECE}`) {
      const done = s.piecePolls > slowPiece;
      const half = FINAL_TEXT.slice(0, Math.min(FINAL_TEXT.length, 120 + s.piecePolls * 120));
      return json({
        found: true, status: done ? 'done' : 'running', revision: 1,
        steps: [
          { key: 'write', label: 'Writing your post', state: done ? 'done' : 'active' },
          { key: 'check', label: 'Checking it', state: done ? 'done' : 'pending' },
          { key: 'done', label: 'Done', state: done ? 'done' : 'pending' },
        ],
        sections: [{ key: 'body', label: '', text: done ? FINAL_TEXT : half }],
      });
    }
    if (p.startsWith('/v1/planning/trip-reallocation')) return json({ suggestions: [], trips: [] });
    if (p.startsWith('/v1/tours/')) return json({ data: [], pagination: { total: 0 } });
    if (p === '/v1/billing') return json({});
    return json({});
  });
}

async function login(page: Page, baseURL: string) {
  const host = new URL(baseURL).hostname;
  await page.context().addCookies([
    { name: 'cis_tenant_token', value: 'aa668-mock', domain: host, path: '/' },
    { name: 'cis_role', value: 'tenant', domain: host, path: '/' },
  ]);
}

async function openFacebookSlate(page: Page) {
  await page.goto('/portal/t7-planning');
  await page.getByRole('button', { name: /^Facebook/ }).click();
  await expect(page.getByText('Mekong River — slow boat at dawn')).toBeVisible({ timeout: 30_000 });
}

async function pageDoesNotScroll(page: Page) {
  // The panel is a fixed overlay: the tenant never has to scroll the PAGE to use it, and the panel
  // itself fits the viewport.
  const box = await page.getByRole('dialog').boundingBox();
  const vp = page.viewportSize()!;
  expect(box).not.toBeNull();
  expect(box!.y).toBeGreaterThanOrEqual(0);
  expect(box!.y + box!.height).toBeLessThanOrEqual(vp.height + 1);
  const overflowX = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflowX).toBeLessThanOrEqual(0);
}

test.describe('AA-668 Writer panel (mocked API)', () => {
  test('desktop 1440x900: pick → goal → angle → live write → finished piece, no CTA prompt', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    const s: State = { picked: false, status: 'pending_goal', goal: null, chosen: null, anglePolls: 0, piecePolls: 0, writeBodies: [] };
    await mockTenantApi(page, s);
    await login(page, baseURL!);
    const consoleErrors: string[] = [];
    page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });

    await openFacebookSlate(page);
    await page.screenshot({ path: `${SHOTS}/01-slate-desktop.png` });

    await page.getByText('Mekong River — slow boat at dawn').locator('xpath=ancestor::div[3]').getByRole('button', { name: /Pick to write/ }).click();
    const panel = page.getByTestId('writer-panel');
    await expect(panel).toBeVisible();
    await expect(page.getByTestId('writer-panel-title')).toHaveText('Mekong River — slow boat at dawn');
    await expect(page.getByTestId('goal-awareness')).toBeVisible();
    await pageDoesNotScroll(page);
    await page.screenshot({ path: `${SHOTS}/02-panel-goal-desktop.png` });

    await page.getByTestId('goal-inspiration').click();
    await page.getByRole('button', { name: /Generate 3 angles/ }).click();
    await expect(page.getByTestId('angle-0')).toBeVisible({ timeout: 15_000 });
    await pageDoesNotScroll(page);
    await page.screenshot({ path: `${SHOTS}/03-panel-angles-desktop.png` });

    await page.getByTestId('angle-0').getByRole('button', { name: /Write with this angle/ }).click();
    await expect(page.getByText('Writing your post').first()).toBeVisible({ timeout: 15_000 });
    // No CTA step: the request's cta is NULL and the panel still went straight to writing.
    await expect(page.getByText(/needs a call to action/)).toHaveCount(0);
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `${SHOTS}/04-panel-writing-desktop.png` });

    await expect(page.getByTestId('writer-panel-result')).toBeVisible({ timeout: 30_000 });
    expect(s.writeBodies).toEqual([{}]); // write sent without a cta → server uses the brand default
    await expect(page.getByTestId('ai-content-notice').first()).toBeVisible();
    await expect(page.getByRole('button', { name: /Export text/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /^Publish$/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Open in My Content/ })).toBeVisible();
    await pageDoesNotScroll(page);
    await page.screenshot({ path: `${SHOTS}/05-panel-finished-desktop.png` });

    await page.getByRole('button', { name: /^Publish$/ }).click();
    await page.screenshot({ path: `${SHOTS}/06-panel-publish-confirm-desktop.png` });

    // Ignored: dev-only noise unrelated to the panel — the root layout's pre-paint script sets
    // <html data-sidebar>, which `next dev` reports as a hydration mismatch on every page.
    expect(consoleErrors.filter(e => !/favicon|Failed to load resource|sentry|data-sidebar|hydrat/i.test(e))).toEqual([]);
  });

  test('closing mid-write keeps the job and toasts on completion; Continue reopens', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    const s: State = { picked: false, status: 'pending_goal', goal: null, chosen: null, anglePolls: 0, piecePolls: 0, writeBodies: [] };
    await mockTenantApi(page, s, { slowPiece: 3 });
    await login(page, baseURL!);
    await openFacebookSlate(page);
    await page.getByText('Mekong River — slow boat at dawn').locator('xpath=ancestor::div[3]').getByRole('button', { name: /Pick to write/ }).click();
    await page.getByTestId('goal-awareness').click();
    await page.getByRole('button', { name: /Generate 3 angles/ }).click();
    await page.getByTestId('angle-1').getByRole('button', { name: /Write with this angle/ }).click();
    await expect(page.getByText('Writing your post').first()).toBeVisible({ timeout: 15_000 });
    await page.getByRole('button', { name: 'Close' }).click();
    await expect(page.getByTestId('writer-panel')).toHaveCount(0);
    await expect(page.getByText(/is ready — find it in My Content/)).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ path: `${SHOTS}/07-toast-after-close-desktop.png` });
    await page.getByRole('button', { name: /Continue/ }).click();
    await expect(page.getByTestId('writer-panel')).toBeVisible();
  });

  test('phone 390x844: the panel is a full-screen sheet', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 390, height: 844 });
    const s: State = { picked: false, status: 'pending_goal', goal: null, chosen: null, anglePolls: 0, piecePolls: 0, writeBodies: [] };
    await mockTenantApi(page, s);
    await login(page, baseURL!);
    await openFacebookSlate(page);
    await page.getByText('Mekong River — slow boat at dawn').locator('xpath=ancestor::div[3]').getByRole('button', { name: /Pick to write/ }).click();
    await expect(page.getByTestId('goal-awareness')).toBeVisible();
    const box = await page.getByRole('dialog').boundingBox();
    expect(box!.width).toBeGreaterThanOrEqual(389);
    await pageDoesNotScroll(page);
    await page.screenshot({ path: `${SHOTS}/08-panel-goal-mobile.png` });
    await page.getByTestId('goal-trust').click();
    await page.getByRole('button', { name: /Generate 3 angles/ }).click();
    await page.getByTestId('angle-0').getByRole('button', { name: /Write with this angle/ }).click();
    await expect(page.getByTestId('writer-panel-result')).toBeVisible({ timeout: 30_000 });
    await pageDoesNotScroll(page);
    await page.screenshot({ path: `${SHOTS}/09-panel-finished-mobile.png` });
  });
});
