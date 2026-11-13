// AA-1093 (S279) — live DRY RUN drill of the tenant offboarding erasure saga (admin › Data erasure › Tenant
// offboarding) as the Dev-only E2E CIS admin (A10b, root ADR 0006a). Agent-only, NEVER CI (the sign-in code lands
// under inbound-admin/, readable only by the MFA admin role):
//
//   cd apps/AA-CIS-App && AWS_PROFILE=aa365-admin E2EADMIN=1 [DRILL_SLUGS=audit-wildkind-travel,audit-trail-pulse] \
//     [EVIDENCE_DIR=<dir>] [E2E_DRILL_FULL=1] npx playwright test --project admin-evidence erasure-drill
//
// Always (read-only, plus write PROBES that cannot change anything):
//   1. picks the first tenant of DRILL_SLUGS that is synthetic (members/access answers invite=true for the A10b
//      session only on is_canary AND NOT is_pilot) and not offboarded;
//   2. reads the saga list (settings + requests) and screenshots the Tenant offboarding tab, 1440 light + dark;
//   3. probes the saga writes: drill on that (not offboarded) tenant — 409 "not offboarded" when the route runs,
//      nothing written — advance / stop on a random request id (404 when the route runs), restore on that (not
//      offboarded) tenant (409 when the route runs). Each answers 403 e2e_read_only while the saga routes and
//      offboard / restore are not on the A10b write allow-list (api/core/e2e_staff.E2E_ALLOWED_WRITES). The offboard
//      itself is never probed (it would offboard the tenant if the guard let it through).
// E2E_DRILL_FULL=1, only when the probes show the routes run for this session: offboard the tenant, Start drill in
// the UI, Run now, poll the request up to 10 min (per-app DRY RUN status / error_code / plan hash prefix), screenshot,
// Stop for good in the UI (phase failed + blocker stopped_by_tech_lead), then ALWAYS restore the tenant (cancels the
// request) and check it is active again. Never approves, never applies.
// Evidence (JSON + PNG) goes to EVIDENCE_DIR (default tests/e2e/results/admin-evidence/aa1093-saga-drill, gitignored).
import { test, expect, type Page } from '@playwright/test';
import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import { E2E_CIS_ADMIN_EMAIL, MAILBOX_ADMIN_PREFIX } from '../helpers/mailbox';
import { STAFF_AUTH_HOST, signInByEmailCode } from '../helpers/cognitoSignIn';
import { growViewportToContent } from '../smoke/smoke-helpers';

const CIS = 'https://aa-cis.lumiguides.it.com';
const OUT = process.env.EVIDENCE_DIR ?? 'tests/e2e/results/admin-evidence/aa1093-saga-drill';
const SLUGS = (process.env.DRILL_SLUGS ?? 'audit-wildkind-travel,audit-trail-pulse').split(',').map((s) => s.trim());
const FULL = process.env.E2E_DRILL_FULL === '1';
const API = '/api/admin/job-runner/erasure/requests';
const PAGE = '/admin/tenants/data-erasure?tab=offboarding';
const POLL_MS = 15000;
const POLL_LIMIT_MS = 10 * 60 * 1000;

type Answer = { status: number; body: Record<string, unknown> | null };
type Step = { app: string; mode: string; round: number; status: string; error_code: string | null;
  plan_hash: string | null; blockers: unknown };
type Req = { request_id: string; tenant_id: string; phase: string; mode: string; round: number; round_apps: string[];
  blockers: { app: string; kind: string; detail: string }[]; combined_fingerprint: string | null;
  steps?: Step[]; job?: { status: string } | null };

const evidence: Record<string, unknown> = { started_at: new Date().toISOString() };
const save = () => {
  fs.mkdirSync(OUT, { recursive: true });
  fs.writeFileSync(path.join(OUT, 'drill-evidence.json'), JSON.stringify(evidence, null, 1));
};

/** A same-origin call from the signed-in page (through the BFF, as the UI sends it). */
async function call(page: Page, method: string, url: string, body?: unknown): Promise<Answer> {
  return page.evaluate(async ({ method, url, body }) => {
    const res = await fetch(url, {
      method, credentials: 'same-origin',
      headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    let json: Record<string, unknown> | null = null;
    try { json = (await res.json()) as Record<string, unknown>; } catch { json = null; }
    return { status: res.status, body: json };
  }, { method, url, body });
}

async function shots(page: Page, name: string) {
  fs.mkdirSync(OUT, { recursive: true });
  for (const scheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.waitForTimeout(400);
    await growViewportToContent(page, 1440, 900);
    await page.screenshot({ path: path.join(OUT, `${name}-1440-${scheme}.png`), fullPage: true });
  }
  await page.emulateMedia({ colorScheme: 'light' });
  await page.setViewportSize({ width: 1440, height: 900 });
}

const short = (h: string | null | undefined) => (h ? h.slice(0, 12) : null);
const stepSummary = (r: Req) => (r.steps ?? []).filter((s) => s.round === r.round).map((s) => ({
  app: s.app, mode: s.mode, status: s.status, error_code: s.error_code, plan_hash: short(s.plan_hash),
  blockers: s.blockers,
}));

async function detail(page: Page, id: string): Promise<Req> {
  const a = await call(page, 'GET', `${API}/${id}`);
  expect(a.status, JSON.stringify(a.body)).toBe(200);
  return (a.body as { request: Req }).request;
}

test('AA-1093 erasure saga: DRY RUN drill of a synthetic tenant (never approve, never apply)', async ({ page }) => {
  test.skip(process.env.E2EADMIN !== '1', 'opt-in, agent-only: E2EADMIN=1 with AWS_PROFILE=aa365-admin');
  test.setTimeout(20 * 60 * 1000);
  page.on('dialog', (d) => void d.accept());   // the Stop confirm (only reached in the full drill)

  try {
    // ── sign in ──
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/login', { waitUntil: 'domcontentloaded' });
    await page.getByRole('link', { name: /sign in with company email/i }).click({ timeout: 15000 });
    await page.waitForURL((u) => u.host === STAFF_AUTH_HOST, { timeout: 20000 });
    await signInByEmailCode(page, { email: E2E_CIS_ADMIN_EMAIL, prefix: MAILBOX_ADMIN_PREFIX });
    await page.waitForURL((u) => u.origin === CIS && u.pathname.startsWith('/admin'), { timeout: 30000 });

    // ── 1. the tenant: synthetic, not the pilot, not offboarded ──
    const list = await call(page, 'GET', '/api/admin/tenants?include_inactive=true');
    expect(list.status).toBe(200);
    const rows = ((list.body as { tenants?: unknown[] } | null)?.tenants ?? (list.body as unknown as unknown[]) ?? []) as
      { tenant_id: string; slug: string; status?: string; cancelled_at?: string | null }[];
    const candidates: Record<string, unknown>[] = [];
    let tenant: { tenant_id: string; slug: string } | null = null;
    for (const slug of SLUGS) {
      const row = rows.find((r) => r.slug === slug);
      if (!row) { candidates.push({ slug, found: false }); continue; }
      const access = await call(page, 'GET', `/api/admin/tenants/${row.tenant_id}/members/access`);
      const synthetic = access.status === 200 && (access.body as { actions?: { invite?: boolean } }).actions?.invite === true;
      const offboarded = Boolean(row.cancelled_at) || row.status === 'offboarded';
      candidates.push({ slug, found: true, status: row.status ?? null, synthetic, offboarded });
      if (!tenant && synthetic && !offboarded) tenant = { tenant_id: row.tenant_id, slug };
    }
    evidence.candidates = candidates;
    expect(tenant, `no synthetic, not offboarded tenant among ${SLUGS.join(', ')}`).not.toBeNull();
    const T = tenant!;
    evidence.tenant = T.slug;
    console.log(`[drill] tenant ${T.slug} (synthetic, active)`);

    // ── 2. the saga list + the tab ──
    const before = await call(page, 'GET', API);
    expect(before.status).toBe(200);
    const bl = before.body as { requests: Req[]; settings: Record<string, unknown> };
    evidence.settings = bl.settings;
    evidence.requests_before = bl.requests.map((r) => ({ tenant: r.tenant_id === T.tenant_id ? T.slug : 'other',
      mode: r.mode, phase: r.phase, round: r.round, blockers: r.blockers.map((b) => `${b.app}:${b.kind}`) }));
    expect(bl.settings.apply_enabled, 'the apply switch must be off for a drill').toBe(false);
    await page.goto(PAGE, { waitUntil: 'domcontentloaded' });
    await expect(page.getByTestId('tenant-offboarding-ready')).toBeVisible({ timeout: 30000 });
    await expect(page.getByTestId('tenant-erasure-settings')).toBeVisible();
    await shots(page, '01-offboarding-tab');

    // ── 3. write probes (safe whatever the guard answers) ──
    const fake = crypto.randomUUID();
    const probes = {
      drill: await call(page, 'POST', `${API}/drill`, { tenant_id: T.tenant_id }),
      advance: await call(page, 'POST', `${API}/${fake}/advance`, {}),
      stop: await call(page, 'POST', `${API}/${fake}/stop`, {}),
      restore: await call(page, 'POST', `/api/admin/tenants/${T.tenant_id}/restore`, {}),
    };
    evidence.write_probes = Object.fromEntries(Object.entries(probes).map(([k, a]) => [k,
      { status: a.status, detail: a.body?.detail ?? a.body?.error ?? null }]));
    console.log(`[drill] probes ${JSON.stringify(evidence.write_probes)}`);
    const refused = Object.values(probes).some((a) => a.status === 403 && a.body?.detail === 'e2e_read_only');
    if (refused) {
      evidence.outcome = 'blocked: the A10b session is read-only on the saga routes (403 e2e_read_only)';
      return;
    }
    expect(probes.drill.status, 'drill on an active tenant').toBe(409);
    if (!FULL) {
      evidence.outcome = 'probes only (set E2E_DRILL_FULL=1 for the full drill)';
      return;
    }

    // ── 4. full drill: offboard → drill → Run now → poll → Stop → restore ──
    const off = await call(page, 'POST', `/api/admin/tenants/${T.tenant_id}/offboard`,
      { reason: 'AA-1093 S279 erasure saga DRY RUN drill (synthetic tenant; restored at the end)' });
    evidence.offboard = { status: off.status, erasure_request: off.body?.erasure_request ?? null };
    expect(off.status, JSON.stringify(off.body)).toBe(200);
    try {
      await page.goto(PAGE, { waitUntil: 'domcontentloaded' });
      await expect(page.getByTestId('tenant-offboarding-ready')).toBeVisible({ timeout: 30000 });
      const drillBox = page.getByTestId('tenant-erasure-drill');
      await drillBox.locator('select').first().selectOption(T.tenant_id);
      const started = page.waitForResponse((r) => r.request().method() === 'POST' && r.url().endsWith(`${API}/drill`));
      await drillBox.getByRole('button', { name: /start drill/i }).click();
      const sr = await started;
      expect(sr.status(), await sr.text()).toBe(200);
      const rid = ((await sr.json()) as { request: Req }).request.request_id;
      evidence.request = { mode: 'drill', request_id_prefix: rid.slice(0, 8) };
      const detailPane = page.getByTestId('tenant-erasure-detail');
      await expect(detailPane).toBeVisible({ timeout: 30000 });
      await detailPane.getByRole('button', { name: /run now/i }).click();
      const polls: unknown[] = [];
      const t0 = Date.now();
      let r = await detail(page, rid);
      while (Date.now() - t0 < POLL_LIMIT_MS) {
        r = await detail(page, rid);
        const cur = stepSummary(r);
        polls.push({ s: Math.round((Date.now() - t0) / 1000), phase: r.phase, job: r.job?.status ?? null, steps: cur });
        const reported = r.round_apps.length > 0 && r.round_apps.every((a) =>
          cur.some((s) => s.app === a && s.mode === 'dry_run' && (s.plan_hash || s.error_code)));
        if (r.phase !== 'dry_run' || (reported && !['queued', 'running'].includes(r.job?.status ?? ''))) break;
        await page.waitForTimeout(POLL_MS);
      }
      evidence.polls = polls;
      evidence.dry_run = { phase: r.phase, round: r.round, round_apps: r.round_apps, job: r.job?.status ?? null,
        fingerprint: short(r.combined_fingerprint), blockers: r.blockers, steps: stepSummary(r) };
      await page.goto(PAGE, { waitUntil: 'domcontentloaded' });
      await page.getByTestId('tenant-erasure-requests').getByRole('button', { name: /^open$/i }).first().click();
      await expect(page.getByTestId('tenant-erasure-steps')).toBeVisible({ timeout: 30000 });
      await shots(page, '03-drill-dry-run');

      // Stop for good (never approve)
      const stopRes = page.waitForResponse((x) => x.request().method() === 'POST' && x.url().endsWith(`/${rid}/stop`));
      await page.getByTestId('tenant-erasure-stop').click();
      const st = await stopRes;
      expect(st.status(), await st.text()).toBe(200);
      const stopped = await detail(page, rid);
      evidence.stopped = { phase: stopped.phase, blockers: stopped.blockers.map((b) => `${b.app}:${b.kind}`) };
      expect(stopped.phase).toBe('failed');
      expect(stopped.blockers.some((b) => b.kind === 'stopped_by_tech_lead')).toBe(true);
      await page.waitForTimeout(1000);
      await shots(page, '04-drill-stopped');
    } finally {
      const res = await call(page, 'POST', `/api/admin/tenants/${T.tenant_id}/restore`, {});
      evidence.restore = { status: res.status, erasure_request_cancelled: Boolean(res.body?.erasure_request_cancelled),
        tenant_status: res.body?.status ?? null, detail: res.body?.detail ?? null };
      save();
    }
    expect((evidence.restore as { status: number }).status).toBe(200);
    const again = await call(page, 'GET', '/api/admin/tenants?include_inactive=true');
    const row = (((again.body as { tenants?: unknown[] } | null)?.tenants ?? again.body) as
      { tenant_id: string; status?: string; cancelled_at?: string | null }[]).find((x) => x.tenant_id === T.tenant_id);
    evidence.after_restore = { status: row?.status ?? null, offboarded: Boolean(row?.cancelled_at) };
    expect(row?.cancelled_at ?? null).toBeNull();
    evidence.outcome = 'drill ran to DRY RUN, stopped, tenant restored; nothing approved or applied';
  } finally {
    evidence.finished_at = new Date().toISOString();
    save();
  }
});
