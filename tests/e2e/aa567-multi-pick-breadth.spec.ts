import { test, expect } from '@playwright/test';
// AA-567 — breadth check: pick() itself makes no LLM call (only T8 goal-generation and T9 write
// do), so this can cheaply confirm the fix across MANY different Channels/Kinds (Segment vs
// Route) without the cost of running each all the way through Write. The one full end-to-end
// run (pick -> wizard -> Goal -> Angle -> Write -> Review -> Change angle -> Start over) is a
// separate spec (aa564-group42-embedded-wizard.spec.ts).
const WANDERLUX_API_KEY = process.env.WANDERLUX_API_KEY || '';

test('AA-567 — pick() succeeds across multiple different Channels and Kinds (Segment + Route)', async ({ page }) => {
  test.skip(!WANDERLUX_API_KEY, 'WANDERLUX_API_KEY env var not set');
  await page.goto('/tenant-login');
  await page.fill('input[type="password"]', WANDERLUX_API_KEY);
  await page.click('button:has-text("Access Portal")');
  await page.waitForURL('**/portal**', { timeout: 10000 });

  const data = await page.evaluate(async () => {
    const r = await fetch('/api/tenant/v1/slate');
    return r.ok ? r.json() : { error: r.status };
  });

  // Collect a spread: up to 2 proposed Subjects per Channel, prefer a mix of Segment (route_id
  // null) and Route (route_id set) kinds.
  const candidates: { subject_id: string; channel: string; kind: string }[] = [];
  for (const [channel, ch] of Object.entries<any>(data.channels ?? {})) {
    const proposed = ch.subjects.filter((s: any) => s.state === 'proposed');
    for (const s of proposed.slice(0, 2)) {
      candidates.push({ subject_id: s.subject_id, channel, kind: s.route_id ? 'Route' : 'Segment' });
    }
  }
  console.log('candidates:', candidates.length);
  expect(candidates.length).toBeGreaterThan(0);

  const results: any[] = [];
  for (const c of candidates.slice(0, 8)) {
    const result = await page.evaluate(async (id) => {
      const r = await fetch(`/api/tenant/v1/subjects/${id}/pick`, { method: 'POST' });
      const body = await r.json().catch(() => ({}));
      return { status: r.status, body };
    }, c.subject_id);
    results.push({ ...c, status: result.status, request_id: result.body.request_id, detail: result.body.detail });
    console.log(JSON.stringify({ ...c, status: result.status, request_id: result.body.request_id }));
  }

  const succeeded = results.filter(r => r.status === 200);
  const byChannel = new Set(succeeded.map(r => r.channel));
  const byKind = new Set(succeeded.map(r => r.kind));
  console.log(`succeeded: ${succeeded.length}/${results.length}, channels: ${[...byChannel]}, kinds: ${[...byKind]}`);

  expect(succeeded.length).toBe(results.length); // every single one must succeed now
});
