// frontend/lib/adminRoutes.test.mjs
// S292 (follow-up of the S291 hotfix) — the admin route check's pure parts (tests/e2e/admin-routes): the route list is
// derived from the code and covers every admin page, dynamic segments get the sample id, what counts as a failure, the
// tolerated known problems and their stale check, the stub API's answers and the job summary.
// Run: node --test --experimental-strip-types --no-warnings lib/adminRoutes.test.mjs (CI: .github/workflows/admin-routes.yml,
// step "Unit tests of the check").

import assert from "node:assert/strict";
import { readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const APP = join(FRONTEND, "app");
const CHECK = join(FRONTEND, "..", "tests", "e2e", "admin-routes");
const R = await import(pathToFileURL(join(CHECK, "routes.ts")).href);
const STUB = await import(pathToFileURL(join(CHECK, "stub-api.mjs")).href);
const { summarize } = await import(pathToFileURL(join(CHECK, "summary.mjs")).href);
const { PAGES } = await import(pathToFileURL(join(FRONTEND, "..", "tests", "e2e", "visual", "pages.ts")).href);
const { canSeePage } = await import(pathToFileURL(join(FRONTEND, "lib", "rbac.ts")).href);

const ORIGIN = "http://localhost:3001";

function pageFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) out.push(...pageFiles(p));
    else if (name === "page.tsx") out.push(relative(APP, dir).split(sep).join("/"));
  }
  return out;
}

test("every admin page.tsx outside a private folder is a route of the check", () => {
  const files = pageFiles(join(APP, "admin")).filter((d) => !d.split("/").some((s) => s.startsWith("_")));
  const routes = R.adminPageRoutes(APP);
  assert.equal(routes.length, files.length);
  assert.ok(routes.includes("/admin/overview"));
  assert.ok(routes.includes("/admin/platform/compliance"));
  assert.ok(routes.includes("/admin/tenants/[id]"));
});

test("the administrator opens every admin page, and the run takes them all", () => {
  const routes = R.adminPageRoutes(APP);
  const run = R.adminRoutes(APP);
  // A page the administrator cannot open would be refused by proxy.ts (PAGE_RULES deny by default): name it.
  const refused = routes.filter((r) => !canSeePage("admin", R.samplePath(r)));
  assert.deepEqual(refused, []);
  assert.equal(run.length, routes.length);
  assert.equal(new Set(run.map((r) => r.path)).size, run.length, "no duplicate path");
});

test("adminOnly marks exactly the pages the viewer (the E2E smoke account) cannot open", () => {
  const run = R.adminRoutes(APP);
  for (const r of run) assert.equal(r.adminOnly, !canSeePage("viewer", r.path.split("?")[0]), r.path);
  const byPath = Object.fromEntries(run.map((r) => [r.path, r.adminOnly]));
  assert.equal(byPath["/admin/overview"], true);
  assert.equal(byPath["/admin/platform/compliance"], true);
  assert.equal(byPath["/admin/master-content"], false);
});

test("dynamic segments get the sample id", () => {
  assert.equal(R.samplePath("/admin/tenants/[id]"), `/admin/tenants/${R.SAMPLE_ID}`);
  assert.equal(R.samplePath("/admin/docs/[...slug]"), `/admin/docs/${R.SAMPLE_ID}`);
  assert.equal(R.samplePath("/admin/docs/[[...slug]]"), `/admin/docs/${R.SAMPLE_ID}`);
  assert.equal(R.samplePath("/admin/jobs"), "/admin/jobs");
});

test("query variants of the visual page list join for administrator routes only, once", () => {
  const run = R.adminRoutes(APP, PAGES.filter((p) => p.auth === "staff"));
  const paths = run.map((r) => r.path);
  assert.ok(paths.includes("/admin/platform/compliance?tab=controls"));
  assert.ok(!paths.some((p) => p.startsWith("/login") || p.startsWith("/kit-gallery")));
  assert.equal(new Set(paths).size, paths.length);
  const extra = R.adminRoutes(APP, [{ route: "/admin/nope", path: "/admin/nope?x=1" }]);
  assert.ok(!extra.some((r) => r.path === "/admin/nope?x=1"), "an unknown route never joins");
});

const clean = { finalPath: "/admin/overview", pageErrors: [], consoleErrors: [], frame: true, bodyText: "" };

test("a clean page has no problems; the stub's failed /api reads are not problems", () => {
  assert.deepEqual(R.problemsOf(clean, ORIGIN), []);
  const stubbed = { ...clean, consoleErrors: [{
    text: "Failed to load resource: the server responded with a status of 503 (Service Unavailable)",
    url: `${ORIGIN}/api/admin/overview/views`,
  }] };
  assert.deepEqual(R.problemsOf(stubbed, ORIGIN), []);
});

test("the S291 crash and the other faults are problems", () => {
  const provider = { ...clean, frame: false, bodyText: "Something went wrong\nTry again",
    consoleErrors: [{ text: "Error: useToast must be used within a <ToastProvider>", url: "" }] };
  assert.deepEqual(R.problemsOf(provider, ORIGIN), [
    "console error: Error: useToast must be used within a <ToastProvider>",
    'error boundary on screen: "Something went wrong"',
  ]);
  assert.deepEqual(R.problemsOf({ ...clean, pageErrors: ["boom"] }, ORIGIN), ["uncaught error: boom"]);
  assert.deepEqual(R.problemsOf({ ...clean, finalPath: "/login" }, ORIGIN),
    ["left the admin for /login (stand-in session refused?)"]);
  assert.match(R.problemsOf({ ...clean, frame: false }, ORIGIN)[0], /^no kit frame/);
  // A failed load that is not a same-origin /api read (a missing chunk, another host) is a problem.
  const chunk = { ...clean, consoleErrors: [{ text: "Failed to load resource: 404", url: `${ORIGIN}/_next/static/x.js` }] };
  assert.equal(R.problemsOf(chunk, ORIGIN).length, 1);
  // The words of the error page inside a rendered frame are page text (an error toast), not a boundary.
  assert.deepEqual(R.problemsOf({ ...clean, bodyText: "Something went wrong" }, ORIGIN), []);
});

test("known problems are tolerated while they happen and fail the run once they stop", () => {
  const path = Object.keys(R.KNOWN_PROBLEMS)[0];
  assert.ok(path, "the list has its first-run entry");
  const p = 'uncaught error: {"detail":"admin_route_check_stub","message":"x"}';
  assert.deepEqual(R.triage(path, [p]), { fresh: [], known: [p], stale: [] });
  assert.equal(R.triage(path, []).stale.length, 1);
  assert.deepEqual(R.triage(path, ["uncaught error: other"]).fresh, ["uncaught error: other"]);
  assert.deepEqual(R.triage("/admin/overview", ["x"]), { fresh: ["x"], known: [], stale: [] });
  for (const entries of Object.values(R.KNOWN_PROBLEMS)) {
    for (const e of entries) assert.ok(e.reason.length > 40 && /follow-up/i.test(e.reason), "a reason and its follow-up");
  }
});

test("the stub answers the administrator, a writable session, and 503 to everything else", () => {
  const [s1, verify] = STUB.answer("POST", "/auth/verify-admin");
  assert.equal(s1, 200);
  assert.equal(verify.role, "admin");
  assert.deepEqual(STUB.answer("GET", "/admin/session?x=1"), [200, { role: "admin", read_only: false, reason: null }]);
  assert.equal(STUB.answer("GET", "/health")[0], 200);
  const [s2, body] = STUB.answer("GET", "/admin/overview/views");
  assert.equal(s2, 503);
  assert.equal(body.detail, STUB.STUB_DETAIL);
  assert.equal(STUB.answer("GET", "/auth/verify-admin")[0], 503, "verify-admin is a POST");
});

test("the summary names pages, failures, tolerated entries and the stub's answers", () => {
  const md = summarize([
    { path: "/admin/a", adminOnly: true, problems: ["uncaught error: x|y"], known: [], stale: [] },
    { path: "/admin/b", adminOnly: false, problems: [], known: ["k"], stale: [] },
  ], ["503 GET /admin/a", "200 POST /auth/verify-admin", "503 GET /admin/b"]);
  assert.match(md, /\*\*2\*\* \(1 admin-only, 1 shared/);
  assert.match(md, /new problem: \*\*1\*\*/);
  assert.match(md, /\| `\/admin\/a` \| uncaught error: x\\\|y \|/);
  assert.match(md, /Tolerated known problems .*`\/admin\/b`/);
  assert.match(md, /200: 1, 503: 2/);
});
