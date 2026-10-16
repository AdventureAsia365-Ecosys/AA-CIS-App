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
//
// S239 (part 2): the tour rewrite uses the same panel. The "tour rewrite" describe block below
// mocks My Catalog Tours (/portal/t4-pool): "Request Rewrite" in the version drawer opens the
// panel (tour title, language, channel in the header), the t2_rewrite live steps + text show, a
// failed job offers Retry, the result opens in My Catalog, closing mid-write toasts on completion,
// no page scroll at 1440x900, full-width sheet at 390.
// S239 review fixes: the failed version has the shape the backend writes (status 'failed', set by
// mark_version_failed) and the mocked /retry answers 409 for anything else, like the real route; the
// retry case keeps serving the previous run's "failed" progress snapshot for a while after Retry
// (the real store keeps it until the worker starts the new run) and the panel must not show it; a
// rewrite of the open tour started elsewhere neither closes the version drawer (unsaved edit kept)
// nor reopens it when that rewrite finishes.
// S239 live-run fixes: the Write card shows goal/channel names (not keys), and reopening the panel
// mid-write shows the text already written at once (no "Starting…", no retype from empty).
// S239 review: the reopen checks hold the reopened view's progress polls (ProgressHold), so what
// they see can only come from the view's own remembered text, for the Social "Continue" reopen
// as well as the tour "View progress"; a Retry after the panel was closed mid-write never opens on
// the failed run's text.
import { test, expect, type Page, type Route } from '@playwright/test';

/** Holds every /v1/progress answer while `on`: a view reopened under a hold paints only what it
 *  had before any answer arrived. `held` counts the polls waiting; release() answers them. */
class ProgressHold {
  on = false;
  held = 0;
  private waiters: (() => void)[] = [];
  async wait() {
    if (!this.on) return;
    this.held += 1;
    await new Promise<void>(r => this.waiters.push(r));
  }
  release() {
    this.on = false;
    this.waiters.splice(0).forEach(r => r());
  }
}

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
  /** Set once the write call answered with the piece (latest-piece then returns it, like the API). */
  written?: boolean;
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

async function mockTenantApi(page: Page, s: State, opts: { slowPiece?: number; noBrandCta?: boolean; slowWriteMs?: number; hold?: ProgressHold } = {}) {
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
    if (p === `/v1/content-writing/requests/${REQ}/latest-piece`) {
      if (!s.written) return json({ piece: null });
      const done = s.piecePolls > slowPiece;
      return json({ piece: { piece_id: PIECE, angle_gate_request_id: REQ, channel: 'facebook', content_text: done ? FINAL_TEXT : null, ready_state: done ? 'ready' : 'in_progress' } });
    }
    if (p === `/v1/content-writing/requests/${REQ}/write`) {
      const body = JSON.parse(req.postData() || '{}');
      s.writeBodies.push(body);
      // The server's MissingCTAError: no request CTA, no override and no brand default_cta.
      if (opts.noBrandCta && !body.cta) return json({ detail: 'no CTA' }, 422);
      if (opts.slowWriteMs) await new Promise(r => setTimeout(r, opts.slowWriteMs));
      s.written = true;
      return json({ piece_id: PIECE, angle_gate_request_id: REQ, channel: 'facebook', ready_state: 'in_progress' }, 202);
    }
    if (p === `/v1/content-writing/pieces/${PIECE}`) {
      s.piecePolls += 1;
      const done = s.piecePolls > slowPiece;
      return json({ piece_id: PIECE, angle_gate_request_id: REQ, channel: 'facebook', content_text: done ? FINAL_TEXT : null, ready_state: done ? 'ready' : 'in_progress' });
    }
    if (p === `/v1/progress/piece/${PIECE}`) {
      await opts.hold?.wait();
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
    // S239 live fix: the Write card names the goal and channel, never their keys.
    await expect(panel.getByText(/Goal: Inspire · Channel: Facebook/)).toBeVisible();
    await expect(panel.getByText(/inspiration|Channel: facebook/)).toHaveCount(0);
    await expect(page.getByTestId('ai-content-notice').first()).toBeVisible();
    await expect(page.getByRole('button', { name: /Export text/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /^Publish$/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Open in My Content/ })).toBeVisible();
    await pageDoesNotScroll(page);
    await page.screenshot({ path: `${SHOTS}/05-panel-finished-desktop.png` });

    await page.getByRole('button', { name: /^Publish$/ }).click();
    await page.screenshot({ path: `${SHOTS}/06-panel-publish-confirm-desktop.png` });
    // Escape closes only the confirm; the panel behind it stays open.
    await expect(page.getByText(/Publish to Facebook\?/)).toBeVisible();
    await page.keyboard.press('Escape');
    await expect(page.getByText(/Publish to Facebook\?/)).toHaveCount(0);
    await expect(page.getByTestId('writer-panel')).toBeVisible();

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

  test('reopening the panel mid-write with Continue shows the text already written at once', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    const s: State = { picked: false, status: 'pending_goal', goal: null, chosen: null, anglePolls: 0, piecePolls: 0, writeBodies: [] };
    const hold = new ProgressHold();
    await mockTenantApi(page, s, { slowPiece: 10_000, hold }); // stays "writing" for the whole test
    await login(page, baseURL!);
    await openFacebookSlate(page);
    await page.getByText('Mekong River — slow boat at dawn').locator('xpath=ancestor::div[3]').getByRole('button', { name: /Pick to write/ }).click();
    await page.getByTestId('goal-awareness').click();
    await page.getByRole('button', { name: /Generate 3 angles/ }).click();
    await page.getByTestId('angle-0').getByRole('button', { name: /Write with this angle/ }).click();
    const panel = page.getByTestId('writer-panel');
    await expect(panel.getByText(/Mist lifts off the water/)).toBeVisible({ timeout: 15_000 });
    await page.getByRole('button', { name: 'Close' }).click();
    await expect(panel).toHaveCount(0);

    hold.on = true; // the reopened view gets no progress answer until release()
    await page.getByRole('button', { name: /Continue/ }).click();
    await expect(panel).toBeVisible();
    await expect.poll(() => hold.held, { timeout: 15_000 }).toBeGreaterThan(0);
    await expect(panel.getByText(/Mist lifts off the water/)).toBeVisible();
    await expect(panel.getByText('Starting…')).toHaveCount(0);
    hold.release();
  });

  test('closing before the write call answers still toasts when the piece is ready', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    const s: State = { picked: false, status: 'pending_goal', goal: null, chosen: null, anglePolls: 0, piecePolls: 0, writeBodies: [] };
    await mockTenantApi(page, s, { slowWriteMs: 2500 });
    await login(page, baseURL!);
    await openFacebookSlate(page);
    await page.getByText('Mekong River — slow boat at dawn').locator('xpath=ancestor::div[3]').getByRole('button', { name: /Pick to write/ }).click();
    await page.getByTestId('goal-awareness').click();
    await page.getByRole('button', { name: /Generate 3 angles/ }).click();
    await page.getByTestId('angle-2').getByRole('button', { name: /Write with this angle/ }).click();
    await expect.poll(() => s.writeBodies.length, { timeout: 15_000 }).toBe(1); // write sent, not answered yet
    await page.getByRole('button', { name: 'Close' }).click();
    await expect(page.getByTestId('writer-panel')).toHaveCount(0);
    await expect(page.getByText(/is ready — find it in My Content/)).toBeVisible({ timeout: 30_000 });
  });

  test('brand without a default CTA: the 422 shows the inline CTA field, then the write goes through', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    const s: State = { picked: false, status: 'pending_goal', goal: null, chosen: null, anglePolls: 0, piecePolls: 0, writeBodies: [] };
    await mockTenantApi(page, s, { noBrandCta: true });
    await login(page, baseURL!);
    await openFacebookSlate(page);
    await page.getByText('Mekong River — slow boat at dawn').locator('xpath=ancestor::div[3]').getByRole('button', { name: /Pick to write/ }).click();
    await page.getByTestId('goal-conversion').click();
    await page.getByRole('button', { name: /Generate 3 angles/ }).click();
    await page.getByTestId('angle-0').getByRole('button', { name: /Write with this angle/ }).click();
    await expect(page.getByText(/needs a call to action/)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('link', { name: /Set a default call to action in Brand Identity/ })).toHaveAttribute('href', '/portal/t0-brand');
    await page.getByPlaceholder(/Book a consultation/).fill('Ask us about the village cycling trip');
    await page.getByRole('button', { name: /Write content/ }).click();
    await expect(page.getByTestId('writer-panel-result')).toBeVisible({ timeout: 30_000 });
    expect(s.writeBodies).toEqual([{}, { cta: 'Ask us about the village cycling trip' }]);
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

// ── Tour rewrite (S239) ────────────────────────────────────────────────────────────────────────
const TOUR = '66666666-6666-4666-8666-666666666666';       // published_tour_id
const V1 = '77777777-7777-4777-8777-777777777777';         // the finished version the drawer opens
const V2 = '88888888-8888-4888-8888-888888888888';         // the new version the rewrite creates
const TOUR_NAME = 'Mekong Slow Boat & Village Cycling';
const NEW_SUMMARY = 'Drift down the Mekong at dawn, then pedal between weaving villages on quiet farm tracks.';

type TourState = {
  rewriteAt: number | null;      // when the rewrite POST arrived
  writeMs: number;               // how long the job "runs"
  failFirst: boolean;            // the first run fails (Retry test)
  retriedAt: number | null;
  rewriteBodies: unknown[];
  retries: number;
  /** After Retry, the progress route keeps serving the previous run's "failed" snapshot this long
   *  (the job is queued, the worker has not started the new run yet). */
  staleSnapshotMs?: number;
  /** Another tour that stays "writing" — keeps My Catalog's 5 s my-versions poll running. */
  otherWriting?: boolean;
  hold?: ProgressHold;
};

function v2Status(t: TourState): 'writing' | 'failed' | 'ready' | null {
  if (t.rewriteAt === null) return null;
  const now = Date.now();
  if (t.failFirst && t.retriedAt === null) return now - t.rewriteAt > t.writeMs ? 'failed' : 'writing';
  if (t.retriedAt !== null) return now - t.retriedAt > t.writeMs + (t.staleSnapshotMs ?? 0) ? 'ready' : 'writing';
  return now - t.rewriteAt > t.writeMs ? 'ready' : 'writing';
}

function version(id: string, n: number, st: 'writing' | 'failed' | 'ready', tourId = TOUR) {
  const content = st === 'ready'
    ? JSON.stringify({ name: TOUR_NAME, summary: id === V2 ? NEW_SUMMARY : 'The first version of this tour.', highlights: ['Dawn boat ride', 'Weaving village'] })
    : JSON.stringify({ name: TOUR_NAME, status: 'generating' });
  return {
    id, version_number: n, published_tour_id: tourId, tour_id: tourId === TOUR ? 'T-1' : 'T-2',
    // A failed rewrite is status 'failed' (+ job 'failed'): what on_terminal=mark_version_failed writes.
    status: st === 'ready' ? 'ai_generated' : st === 'failed' ? 'failed' : 'pending', edit_source: 'ai_generated',
    job_status: st === 'writing' ? 'running' : st === 'failed' ? 'failed' : 'succeeded',
    rewrite_language: 'en-US', created_at: `2026-10-16T0${n}:00:00Z`, edited_at: null, rewritten_content: content,
    seo_mode: 'standard', aa_name: TOUR_NAME, aa_subtitle: '5 days in Laos', aa_summary: 'Original summary.',
    aa_highlights: 'Dawn boat ride|Weaving village', aa_itineraries: null, aa_seo_title: '', aa_seo_meta: '',
    country: 'Laos', duration: '5 days',
  };
}

const OTHER_TOUR = '99999999-9999-4999-8999-999999999999';
const OTHER_V = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';

function progressSnapshot(st: 'writing' | 'failed' | 'ready', since: number, revision: number) {
  const done = st === 'ready';
  const failed = st === 'failed';
  const typed = NEW_SUMMARY.slice(0, Math.min(NEW_SUMMARY.length, 20 + Math.floor((Date.now() - since) / 40)));
  return {
    found: true, status: done ? 'done' : failed ? 'failed' : 'running', revision,
    message: failed ? STALE_FAILED_MESSAGE : null,
    steps: [
      { key: 'research', label: 'Researching the tour', state: 'done' },
      { key: 'write', label: 'Writing in your brand voice', state: done ? 'done' : failed ? 'failed' : 'active' },
      { key: 'check', label: 'Checking it', state: done ? 'done' : 'pending' },
      { key: 'save', label: 'Saving', state: done ? 'done' : 'pending' },
    ],
    sections: [
      { key: 'name', label: '', text: TOUR_NAME },
      { key: 'summary', label: 'Summary', text: done ? NEW_SUMMARY : typed },
    ],
  };
}
const STALE_FAILED_MESSAGE = "Writing didn't finish. Please try again.";

async function mockCatalogApi(page: Page, t: TourState) {
  await page.route('**/api/tenant/**', async (route: Route) => {
    const req = route.request();
    const p = new URL(req.url()).pathname.replace(/^\/api\/tenant/, '');
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    const st = v2Status(t);

    if (p === '/me') return json({ tenant_name: 'Trail Pulse (test)', plan_tier: 'growth' });
    if (p === '/v1/tours/my-versions') {
      const rows = [version(V1, 1, 'ready')];
      if (st) rows.push(version(V2, 2, st));
      if (t.otherWriting) rows.push({ ...version(OTHER_V, 1, 'writing', OTHER_TOUR), aa_name: 'Other tour', created_at: '2026-10-15T00:00:00Z' });
      return json({ data: rows, pagination: { total: rows.length } });
    }
    if (p === `/v1/tours/versions/${V1}`) return json(version(V1, 1, 'ready'));
    if (p === `/v1/tours/versions/${V2}`) return json(version(V2, 2, st ?? 'writing'));
    if (p === `/v1/tours/pool/${TOUR}`) return json({ aa_summary: 'Original summary.', aa_highlights: 'Dawn boat ride|Weaving village', aa_itineraries: null });
    if (p === `/v1/tours/pool/${TOUR}/rewrite` && req.method() === 'POST') {
      t.rewriteBodies.push(JSON.parse(req.postData() || '{}'));
      t.rewriteAt = Date.now();
      return json({ version_id: V2, published_tour_id: TOUR, version_number: 2, status: 'pending', job_id: 'job-1' });
    }
    if (p === `/v1/tours/versions/${V2}/retry` && req.method() === 'POST') {
      // Like api/routers/v1_tours.py retry_rewrite: only a status='failed' version can be retried.
      if (st !== 'failed') return json({ detail: 'Only a failed AI rewrite can be retried' }, 409);
      t.retries += 1;
      t.retriedAt = Date.now();
      return json({ version_id: V2, status: 'pending', job_id: 'job-1' });
    }
    if (p === `/v1/progress/tour/${V2}`) {
      await t.hold?.wait();
      const now = v2Status(t); // after a hold, the state at answer time
      if (now === null) return json({ found: false });
      // Retried, but the worker has not started the new run: the store still holds the old run's
      // "failed" snapshot (revision 1).
      if (t.retriedAt !== null && Date.now() - t.retriedAt < (t.staleSnapshotMs ?? 0)) {
        return json(progressSnapshot('failed', t.rewriteAt ?? Date.now(), 1));
      }
      return json(progressSnapshot(now, t.retriedAt ?? t.rewriteAt ?? Date.now(), 1 + t.retries));
    }
    return json({});
  });
}

async function openCatalogVersion(page: Page) {
  await page.goto('/portal/t4-pool');
  await page.getByRole('button', { name: 'Open →', exact: true }).click(); // not the phone's "Open menu"
  await expect(page.getByRole('button', { name: 'Request Rewrite' })).toBeVisible({ timeout: 30_000 });
}

test.describe('AA-668 Writer panel — tour rewrite (mocked API)', () => {
  test('desktop 1440x900: Request Rewrite opens the panel, live progress, result opens in My Catalog', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    const t: TourState = { rewriteAt: null, writeMs: 4000, failFirst: false, retriedAt: null, rewriteBodies: [], retries: 0 };
    await mockCatalogApi(page, t);
    await login(page, baseURL!);
    const consoleErrors: string[] = [];
    page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });

    await openCatalogVersion(page);
    await page.screenshot({ path: `${SHOTS}/10-catalog-drawer-desktop.png` });
    await page.getByRole('button', { name: 'Request Rewrite' }).click();

    const panel = page.getByTestId('writer-panel');
    await expect(panel).toBeVisible();
    await expect(page.getByTestId('writer-panel-title')).toHaveText(TOUR_NAME);
    await expect(page.getByTestId('writer-panel-context')).toHaveText(/Rewriting in en-US · for Tour catalog/);
    await expect(page.getByRole('dialog')).toHaveCount(1); // the version drawer closed, the panel replaced it
    expect(t.rewriteBodies).toEqual([{ rewrite_language: 'en-US' }]);
    await expect(panel.getByText('Writing in your brand voice')).toBeVisible({ timeout: 15_000 });
    await pageDoesNotScroll(page);
    await page.waitForTimeout(800);
    await page.screenshot({ path: `${SHOTS}/11-tour-panel-writing-desktop.png` });

    await expect(page.getByTestId('tour-rewrite-ready')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId('ai-content-notice').first()).toBeVisible();
    await expect(page.getByRole('button', { name: /Export DOCX/ })).toBeVisible();
    await pageDoesNotScroll(page);
    await page.screenshot({ path: `${SHOTS}/12-tour-panel-ready-desktop.png` });

    await page.getByRole('button', { name: /Open in My Catalog/ }).click();
    await expect(page.getByTestId('writer-panel')).toHaveCount(0);
    await expect(page.getByText(NEW_SUMMARY)).toBeVisible({ timeout: 15_000 }); // the new version in the drawer
    await expect(page.getByRole('button', { name: 'Request Rewrite' })).toBeVisible();
    await page.screenshot({ path: `${SHOTS}/13-tour-result-in-catalog-desktop.png` });

    expect(consoleErrors.filter(e => !/favicon|Failed to load resource|sentry|data-sidebar|hydrat/i.test(e))).toEqual([]);
  });

  test('a failed rewrite shows Retry in the panel; the retried job finishes there', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    const t: TourState = { rewriteAt: null, writeMs: 1500, failFirst: true, retriedAt: null, rewriteBodies: [], retries: 0, staleSnapshotMs: 3000 };
    await mockCatalogApi(page, t);
    await login(page, baseURL!);
    await openCatalogVersion(page);
    await page.getByRole('button', { name: 'Request Rewrite' }).click();
    await expect(page.getByTestId('tour-rewrite-failed')).toBeVisible({ timeout: 30_000 });
    await pageDoesNotScroll(page);
    await page.screenshot({ path: `${SHOTS}/14-tour-panel-failed-desktop.png` });
    await page.getByRole('button', { name: /Retry writing/ }).click();
    await expect.poll(() => t.retries).toBe(1);
    await expect(page.getByTestId('tour-rewrite-failed')).toHaveCount(0, { timeout: 15_000 });
    // The progress route still serves the old run's failed snapshot (staleSnapshotMs): the panel
    // shows "Starting…", never the old failure, until the new run is live.
    const panel = page.getByTestId('writer-panel');
    await expect(panel.getByText('Starting…')).toBeVisible({ timeout: 5_000 });
    const staleUntil = t.retriedAt! + t.staleSnapshotMs!;
    while (Date.now() < staleUntil) {
      await expect(panel.getByText(STALE_FAILED_MESSAGE)).toHaveCount(0);
      await page.waitForTimeout(250);
    }
    await page.screenshot({ path: `${SHOTS}/18-tour-panel-retry-starting-desktop.png` });
    await expect(panel.getByText('Writing in your brand voice')).toBeVisible({ timeout: 15_000 });
    await expect(panel.getByText(STALE_FAILED_MESSAGE)).toHaveCount(0);
    await expect(page.getByTestId('tour-rewrite-ready')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId('tour-rewrite-failed')).toHaveCount(0);
  });

  test('closing mid-rewrite keeps the job and toasts when it is ready; View progress reopens', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    const t: TourState = { rewriteAt: null, writeMs: 6000, failFirst: false, retriedAt: null, rewriteBodies: [], retries: 0 };
    await mockCatalogApi(page, t);
    await login(page, baseURL!);
    await openCatalogVersion(page);
    await page.getByRole('button', { name: 'Request Rewrite' }).click();
    await expect(page.getByTestId('writer-panel')).toBeVisible();
    await page.getByRole('button', { name: 'Close' }).click();
    await expect(page.getByTestId('writer-panel')).toHaveCount(0);
    // While it is still writing, the row offers the live view again.
    await page.getByRole('button', { name: /View progress/ }).click();
    await expect(page.getByTestId('writer-panel-title')).toHaveText(TOUR_NAME);
    await page.getByRole('button', { name: 'Close' }).click();
    await expect(page.getByText(/is ready — find it in My Catalog Tours/)).toBeVisible({ timeout: 30_000 });
    await page.screenshot({ path: `${SHOTS}/15-tour-toast-after-close-desktop.png` });
  });

  test('reopening the panel mid-write shows the text already written at once', async ({ page, baseURL }) => {
    // S239 live run: "View progress" remounted the live view empty — "Starting…", then the whole
    // text typed again from the start, so it looked truncated for seconds.
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    const hold = new ProgressHold();
    const t: TourState = { rewriteAt: null, writeMs: 30_000, failFirst: false, retriedAt: null, rewriteBodies: [], retries: 0, hold };
    await mockCatalogApi(page, t);
    await login(page, baseURL!);
    await openCatalogVersion(page);
    await page.getByRole('button', { name: 'Request Rewrite' }).click();
    const panel = page.getByTestId('writer-panel');
    await expect(panel.getByText(NEW_SUMMARY)).toBeVisible({ timeout: 15_000 }); // fully written live
    await page.getByRole('button', { name: 'Close' }).click();
    await expect(panel).toHaveCount(0);
    // The reopened panel gets no progress answer while the hold is on: what it shows is what the
    // view remembered — the steps and the full text, no "Starting…".
    hold.on = true;
    await page.getByRole('button', { name: /View progress/ }).click();
    await expect(page.getByTestId('writer-panel-title')).toHaveText(TOUR_NAME);
    await expect.poll(() => hold.held, { timeout: 15_000 }).toBeGreaterThan(0);
    await expect(panel.getByText(NEW_SUMMARY)).toBeVisible();
    await expect(panel.getByText('Writing in your brand voice')).toBeVisible();
    await expect(panel.getByText('Starting…')).toHaveCount(0);
    await page.screenshot({ path: `${SHOTS}/19-tour-panel-reopened-desktop.png` });
    hold.release();
  });

  test('a Retry after the panel was closed mid-write never opens on the failed run', async ({ page, baseURL }) => {
    // S239 review: the view remembers a job's last snapshot by version id, and a Retry keeps the
    // id. Run 1 is watched, the panel closed, run 1 fails; after Retry the live view must not
    // open on run 1's half-written text and steps as if they were live.
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    const hold = new ProgressHold();
    const t: TourState = { rewriteAt: null, writeMs: 3000, failFirst: true, retriedAt: null, rewriteBodies: [], retries: 0, staleSnapshotMs: 1500, hold };
    await mockCatalogApi(page, t);
    await login(page, baseURL!);
    await openCatalogVersion(page);
    await page.getByRole('button', { name: 'Request Rewrite' }).click();
    const panel = page.getByTestId('writer-panel');
    await expect(panel.getByText(/Drift down the Mekon/)).toBeVisible({ timeout: 15_000 }); // run 1, part written
    await page.getByRole('button', { name: 'Close' }).click();
    await expect(panel).toHaveCount(0);

    // Run 1 fails while the panel is closed; the row offers Retry.
    const rowRetry = page.getByRole('button', { name: 'Retry', exact: true });
    await expect(rowRetry).toBeVisible({ timeout: 20_000 });
    await rowRetry.click();
    await expect.poll(() => t.retries).toBe(1);
    hold.on = true;
    await page.getByRole('button', { name: /View progress/ }).click();
    await expect(page.getByTestId('writer-panel-title')).toHaveText(TOUR_NAME);
    await expect.poll(() => hold.held, { timeout: 15_000 }).toBeGreaterThan(0);
    // Nothing answered yet: no text or steps from run 1.
    await expect(panel.getByText('Starting…')).toBeVisible();
    await expect(panel.getByText(/Drift down the Mekon/)).toHaveCount(0);
    await expect(panel.getByText('Writing in your brand voice')).toHaveCount(0);
    hold.release();
    await expect(page.getByTestId('tour-rewrite-ready')).toBeVisible({ timeout: 30_000 });
  });

  test('a rewrite of the open tour started elsewhere keeps the drawer and its edit, and does not reopen it', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 1440, height: 900 });
    const t: TourState = { rewriteAt: null, writeMs: 7000, failFirst: false, retriedAt: null, rewriteBodies: [], retries: 0, otherWriting: true };
    await mockCatalogApi(page, t);
    await login(page, baseURL!);
    await openCatalogVersion(page);
    const drawer = page.getByRole('dialog');
    // An unsaved edit in v1's drawer.
    await drawer.getByRole('button', { name: /Edit/ }).first().click();
    const EDIT = 'My own words about the dawn boat ride.';
    await drawer.locator('textarea').first().fill(EDIT);
    await drawer.getByRole('button', { name: 'Save', exact: true }).last().click(); // the summary editor's own Save
    await expect(drawer.getByRole('button', { name: 'Save', exact: true })).toHaveCount(1); // the drawer's (dirty) Save

    // A rewrite of the same tour starts elsewhere (another tab / Browse Tours batch Rewrite); the
    // 5 s my-versions poll (another tour is writing) picks up v2 as "writing".
    t.rewriteAt = Date.now();
    await expect(page.getByRole('button', { name: /View progress/ })).toHaveCount(2, { timeout: 15_000 });
    await expect(drawer).toBeVisible();
    await expect(drawer.getByText(EDIT)).toBeVisible();
    await expect(drawer.getByRole('button', { name: 'Save', exact: true })).toHaveCount(1);

    // The tenant closes the drawer; v2 finishes later — the drawer must not pop open on its own.
    await page.getByRole('button', { name: 'Close' }).first().click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect.poll(() => v2Status(t), { timeout: 15_000 }).toBe('ready');
    await page.waitForTimeout(6_000); // one more 5 s poll after v2 is ready
    await expect(page.getByRole('dialog')).toHaveCount(0);
  });

  test('phone 390x844: the tour-rewrite panel is a full-screen sheet', async ({ page, baseURL }) => {
    test.setTimeout(120_000);
    await page.setViewportSize({ width: 390, height: 844 });
    const t: TourState = { rewriteAt: null, writeMs: 3000, failFirst: false, retriedAt: null, rewriteBodies: [], retries: 0 };
    await mockCatalogApi(page, t);
    await login(page, baseURL!);
    await openCatalogVersion(page);
    await page.getByRole('button', { name: 'Request Rewrite' }).click();
    await expect(page.getByTestId('writer-panel')).toBeVisible();
    const box = await page.getByRole('dialog').boundingBox();
    expect(box!.width).toBeGreaterThanOrEqual(389);
    await pageDoesNotScroll(page);
    await page.screenshot({ path: `${SHOTS}/16-tour-panel-writing-mobile.png` });
    await expect(page.getByTestId('tour-rewrite-ready')).toBeVisible({ timeout: 30_000 });
    await pageDoesNotScroll(page);
    await page.screenshot({ path: `${SHOTS}/17-tour-panel-ready-mobile.png` });
  });
});
