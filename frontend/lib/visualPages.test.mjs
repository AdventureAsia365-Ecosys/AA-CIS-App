// frontend/lib/visualPages.test.mjs
// AA-1008 (root ADR 0016 decision 9, guard G16) — the visual run's page list (tests/e2e/visual/pages.ts) and its
// helpers. Run: npm run test:unit (CI: Frontend Build, "Kit button render tests").
//   1. every frontend/app/**/page.tsx route is listed in PAGES or in EXCLUDED with a reason, and nothing else is;
//   2. slugs are unique and file-name safe; staff pages render the kit frame unless they say otherwise;
//   3. changed-pages.mjs: a page folder touches its pages, a private folder its parent's, app/layout.tsx / lib / the
//      visual files every page, a backend file none;
//   4. selectPages: public vs staff, the gallery only in a gallery build, the planted pages only in the self-test.

import assert from "node:assert/strict";
import { readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const REPO = join(FRONTEND, "..");
const VISUAL = join(REPO, "tests", "e2e", "visual");
const { PAGES, EXCLUDED, selectPages } = await import(pathToFileURL(join(VISUAL, "pages.ts")).href);
const { changedSlugs } = await import(pathToFileURL(join(VISUAL, "changed-pages.mjs")).href);

function appRoutes(dir = join(FRONTEND, "app")) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name === "node_modules" || name === "api") continue;
      out.push(...appRoutes(p));
    } else if (name === "page.tsx") {
      const rel = relative(join(FRONTEND, "app"), dir).split(sep).filter((s) => !/^\(.*\)$/.test(s)).join("/");
      out.push(`/${rel}`);
    }
  }
  return out;
}

test("G16: every app route is listed or excluded with a reason, and nothing else is", () => {
  const routes = new Set(appRoutes());
  const listed = new Set(PAGES.map((p) => p.route).filter((r) => r.startsWith("/")));
  const missing = [...routes].filter((r) => !listed.has(r) && !EXCLUDED[r]);
  assert.deepEqual(missing, [], "add these routes to tests/e2e/visual/pages.ts (or EXCLUDED with a reason)");
  const stale = [...listed, ...Object.keys(EXCLUDED)].filter((r) => !routes.has(r));
  assert.deepEqual(stale, [], "these listed routes have no page.tsx any more");
  for (const [r, why] of Object.entries(EXCLUDED)) assert.ok(why.length > 20, `${r} needs a real reason`);
});

test("slugs are unique and file-name safe; staff pages render the kit frame", () => {
  const slugs = PAGES.map((p) => p.slug);
  assert.equal(new Set(slugs).size, slugs.length);
  for (const p of PAGES) {
    assert.match(p.slug, /^[a-z0-9-]+$/);
    if (p.auth === "staff") assert.notEqual(p.frame, false, `${p.slug}: every admin page renders PageFrame`);
  }
  assert.equal(PAGES.filter((p) => p.selftest).length, 2);
});

test("changed-pages: a folder touches its pages, shared files every page, a backend file none", () => {
  assert.deepEqual(changedSlugs(["frontend/app/admin/jobs/page.tsx"]), ["jobs"]);
  assert.deepEqual(changedSlugs(["frontend/app/admin/tenants/_components/TenantDetail.tsx"]),
    PAGES.filter((p) => p.route.startsWith("/admin/tenants")).map((p) => p.slug).sort());
  assert.deepEqual(changedSlugs(["frontend/app/admin/places/cards/page.tsx"]), ["places-cards"]);
  assert.deepEqual(changedSlugs(["frontend/app/kit-gallery/GalleryView.tsx"]), ["kit-gallery", "selftest-document", "selftest-frame"]);
  assert.equal(changedSlugs(["frontend/app/layout.tsx"]), "all");
  assert.equal(changedSlugs(["frontend/app/_kit/core/ShellFrame.tsx"]), "all");
  assert.equal(changedSlugs(["frontend/lib/rbac.ts"]), "all");
  assert.equal(changedSlugs(["tests/e2e/visual/visual.spec.ts"]), "all");
  assert.equal(changedSlugs([".github/workflows/visual.yml"]), "all");
  assert.deepEqual(changedSlugs(["api/routers/jobs.py", "docs/x.md"]), []);
});

test("selectPages: public vs staff, the gallery only in a gallery build, the planted pages only in the self-test", () => {
  const pub = selectPages(PAGES, { auth: "public", slugs: "all", gallery: false, selftest: false }).map((p) => p.slug);
  assert.deepEqual(pub.sort(), ["home", "login", "not-found"]);
  const pr = selectPages(PAGES, { auth: "public", slugs: "", gallery: true, selftest: false }).map((p) => p.slug);
  assert.deepEqual(pr, ["kit-gallery"], "a PR that changes no public page still captures the gallery");
  const staff = selectPages(PAGES, { auth: "staff", slugs: "jobs,login", gallery: true, selftest: false }).map((p) => p.slug);
  assert.deepEqual(staff, ["jobs"]);
  const self = selectPages(PAGES, { auth: "public", slugs: "all", gallery: true, selftest: true }).map((p) => p.slug);
  assert.deepEqual(self, ["selftest-document", "selftest-frame"]);
});
