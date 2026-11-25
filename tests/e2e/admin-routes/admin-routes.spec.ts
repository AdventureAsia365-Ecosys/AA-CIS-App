// tests/e2e/admin-routes/admin-routes.spec.ts
// S292 (follow-up of the S291 hotfix, release safety) — the admin route check: every CIS admin page the administrator
// may open (routes.ts, derived from frontend/app/admin/**/page.tsx and lib/rbac.ts) rendered AS THE ADMINISTRATOR on
// the runner's own `next start`, with the CIS API replaced by stub-api.mjs. No real credential exists anywhere in this
// run: the session cookie is an unsigned stand-in that only the stub accepts (its POST /auth/verify-admin answers the
// administrator for any token), and the build that serves it is a throwaway CI build pointed at the stub. Production
// sign-in, proxy.ts and the BFF run unchanged; nothing here ships in the runtime bundle.
//
// A page fails on: an uncaught error, a console error (other than the failed /api/* reads the stub answers 503 on
// purpose), an error boundary in place of the admin shell, no kit frame, or a bounce out of /admin. The S291 crash
// ("useToast must be used within a <ToastProvider>" on the Ecosystem status page) is exactly such an error.
//
// The planted cases at the end prove the detectors: each opens the overview with one planted fault (an uncaught
// provider error, a console error, the global error page in place of the shell) and passes only when the run names it.
// Project "admin-routes" (playwright.config.ts); workflow .github/workflows/admin-routes.yml. Local run:
//   node tests/e2e/admin-routes/stub-api.mjs 55491 &   then in frontend/:
//   NEXT_PUBLIC_API_URL=http://127.0.0.1:55491 API_URL=http://127.0.0.1:55491 ADMIN_SECRET=stub npm run build
//   NEXT_PUBLIC_API_URL=http://127.0.0.1:55491 API_URL=http://127.0.0.1:55491 ADMIN_SECRET=stub npx next start -p 3001
//   BASE_URL=http://localhost:3001 npx playwright test -c playwright.config.ts --project=admin-routes

import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { PAGES } from "../visual/pages";
import { adminRoutes, problemsOf, triage, type PageSignals } from "./routes";

const REPO = join(__dirname, "..", "..", "..");
const RESULTS = join(REPO, "tests", "e2e", "results", "admin-routes");
// The stand-in session: an opaque value (not a JWT, so lib/staffSession.ts treats it as a legacy session and never
// tries a Cognito refresh). Only the stub's verify-admin accepts it.
const STAND_IN_TOKEN = "admin-route-check-stand-in";
const FRAME_TIMEOUT_MS = 20_000;
const QUIET_MS = 1_200;      // no same-origin /api request in flight for this long = the page has settled
const SETTLE_CAP_MS = 8_000; // react-query retries a failed read once after ~1 s (app/_kit/core/Providers.tsx)

const ROUTES = adminRoutes(join(REPO, "frontend", "app"), PAGES.filter((p) => p.auth === "staff"));

test.describe.configure({ mode: "parallel" });

test.beforeEach(async ({ context, baseURL }) => {
  const url = baseURL ?? "http://localhost:3001";
  await context.addCookies([
    { name: "cis_admin_token", value: STAND_IN_TOKEN, url, httpOnly: true, sameSite: "Lax" },
    // Display hints the admin shell reads (lib/staffSession.ts): the role and the name in the top bar.
    { name: "cis_role", value: "admin", url, sameSite: "Lax" },
    { name: "cis_user", value: "Route%20Check%20Admin", url, sameSite: "Lax" },
  ]);
});

/** Open `path` and collect what the page did, after it settled. */
async function visit(page: Page, path: string, origin: string): Promise<PageSignals> {
  const pageErrors: string[] = [];
  const consoleErrors: { text: string; url: string }[] = [];
  page.on("pageerror", (e) => pageErrors.push(String(e.message || e).split("\n")[0]));
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push({ text: m.text().split("\n")[0], url: m.location().url ?? "" });
  });
  let inflight = 0;
  let last = Date.now();
  const isApi = (u: string) => u.startsWith(origin) && new URL(u).pathname.startsWith("/api/");
  page.on("request", (r) => { if (isApi(r.url())) { inflight++; last = Date.now(); } });
  const done = (r: { url(): string }) => { if (isApi(r.url())) { inflight = Math.max(0, inflight - 1); last = Date.now(); } };
  page.on("requestfinished", done);
  page.on("requestfailed", done);

  await page.goto(path, { waitUntil: "load" });
  // The frame, or whatever replaced it: wait with its own cap (lesson S220), never on networkidle (pages poll).
  await page.locator("[data-kit-frame]").first().waitFor({ state: "attached", timeout: FRAME_TIMEOUT_MS }).catch(() => {});
  const start = Date.now();
  while (Date.now() - start < SETTLE_CAP_MS && (inflight > 0 || Date.now() - last < QUIET_MS)) {
    await page.waitForTimeout(200);
  }
  return {
    finalPath: new URL(page.url()).pathname,
    pageErrors,
    consoleErrors,
    frame: (await page.locator("[data-kit-frame]").count()) > 0,
    bodyText: (await page.locator("body").innerText({ timeout: 5_000 }).catch(() => "")).slice(0, 4_000),
  };
}

function record(name: string, data: object) {
  mkdirSync(RESULTS, { recursive: true });
  writeFileSync(join(RESULTS, `${name.replace(/[^a-z0-9]+/gi, "_")}.json`), JSON.stringify(data, null, 2));
}

test("the route list covers the admin (derived from the code)", () => {
  // Never a silently empty run: the admin has dozens of pages, most of them admin-only.
  expect(ROUTES.length).toBeGreaterThan(30);
  expect(ROUTES.filter((r) => r.adminOnly).length).toBeGreaterThan(20);
  expect(ROUTES.map((r) => r.path)).toContain("/admin/overview");
});

for (const r of ROUTES) {
  test(`${r.adminOnly ? "admin-only" : "shared"} ${r.path}`, async ({ page, baseURL }) => {
    const origin = new URL(baseURL ?? "http://localhost:3001").origin;
    const signals = await visit(page, r.path, origin);
    const { fresh, known, stale } = triage(r.path, problemsOf(signals, origin));
    record(r.path, { ...r, problems: fresh, known, stale, pageErrors: signals.pageErrors,
      consoleErrors: signals.consoleErrors });
    expect(fresh, `${r.path} as the administrator`).toEqual([]);
    expect(stale, `${r.path}: KNOWN_PROBLEMS entry no longer happens, delete it (routes.ts)`).toEqual([]);
  });
}

// --- planted faults: each must be caught (a detector that misses its fault turns the run red) -----------------------

const PLANTS: { name: string; script: string; expect: RegExp }[] = [
  {
    // What React reports when a component calls useToast() outside a <ToastProvider> (the S291 crash).
    name: "uncaught provider error",
    script: `window.addEventListener("load", () => setTimeout(() => {
      throw new Error("useToast must be used within a <ToastProvider>");
    }, 0));`,
    expect: /^uncaught error: useToast must be used within a <ToastProvider>/,
  },
  {
    name: "console error",
    script: `window.addEventListener("load", () => console.error("planted console error (admin route check self-test)"));`,
    expect: /^console error: planted console error/,
  },
  {
    // app/global-error.tsx in place of the admin shell.
    name: "error boundary",
    script: `window.addEventListener("load", () => setTimeout(() => {
      document.body.innerHTML = "<div><h2>Something went wrong</h2><button>Try again</button></div>";
    }, 300));`,
    expect: /^error boundary on screen: "Something went wrong"/,
  },
];

for (const plant of PLANTS) {
  test(`self-test: a planted ${plant.name} is caught`, async ({ page, baseURL }) => {
    const origin = new URL(baseURL ?? "http://localhost:3001").origin;
    await page.addInitScript(plant.script);
    const problems = problemsOf(await visit(page, "/admin/overview", origin), origin);
    expect(problems.some((p) => plant.expect.test(p)), `planted ${plant.name}; problems: ${JSON.stringify(problems)}`)
      .toBe(true);
  });
}
