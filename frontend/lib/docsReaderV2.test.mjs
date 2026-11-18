// frontend/lib/docsReaderV2.test.mjs
// AA-1133: the docs reader v2 helpers (sidebar groups, GitHub links, code labels, H1 split, slugs that match the
// search index, search URLs) and the rbac rule that opens /admin/docs to every staff role. Invented paths only.
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DOC_NAV_GROUPS,
  codeLanguage,
  githubLinks,
  groupByNav,
  hitHref,
  outline,
  ownerLabel,
  readingMinutes,
  searchUrl,
  splitLeadingH1,
  tocColumnFits,
} from "./docsReader.ts";
import { numberGroups } from "./docsGroups.ts";
import { STAFF_ROLES, canReadAdminPath, canSeePage, canWriteAdminPath } from "./rbac.ts";

const page = (path, over = {}) => ({
  repo: "AA-Ecosys", path, id: "X-01", title: path, description: null, type: "how-to", status: "active",
  audience: ["staff"], owner: "tech-lead", last_reviewed: null, generated: false, access: "staff", bytes: 1,
  ...over,
});

test("groupByNav: groups in sidebar order, nav_order first, then folder and title", () => {
  const groups = groupByNav([
    page("docs/ops/runbooks/z.md", { nav: "operations", title: "Zebra crossing" }),
    page("docs/ops/a.md", { nav: "operations", title: "Alpha", nav_order: 5 }),
    page("docs/ops/runbooks/b.md", { nav: "operations", title: "Bravo" }),
    page("docs/README.md", { nav: "start", title: "Home", nav_order: 0 }),
    page("docs/legal/x.md", { nav: "legal-security" }),
  ]);
  assert.deepEqual(groups.map((g) => g.section), ["start", "operations", "legal-security"]);
  assert.deepEqual(groups[1].pages.map((p) => p.title), ["Alpha", "Bravo", "Zebra crossing"]);
});

test("groupByNav: null for an older bundle; a page without nav lands in reference", () => {
  assert.equal(groupByNav([page("docs/a.md"), page("docs/b.md")]), null);
  const g = groupByNav([page("docs/a.md", { nav: "guides" }), page("docs/b.md")]);
  assert.deepEqual(g.map((x) => x.section), ["guides", "reference"]);
  assert.deepEqual(DOC_NAV_GROUPS, ["start", "guides", "operations", "architecture", "reference", "legal-security"]);
});

test("numberGroups follows the sidebar groups when the bundle has them", () => {
  const n = numberGroups([page("docs/ops/r.md", { nav: "operations" }), page("docs/README.md", { nav: "start" })]);
  assert.deepEqual(n.map((g) => [g.section, g.number]), [["start", 1], ["operations", 2]]);
  assert.equal(numberGroups([page("docs/ops/r.md")])[0].section, "ops");   // older bundle: by folder
});

test("githubLinks: source at the commit, history and edit on the branch, edit for admins only", () => {
  const src = { repo: "AA-Ecosys", sha: "a".repeat(40), ref: "master", github: "Org-x/Repo-y" };
  assert.deepEqual(githubLinks(src, "docs/ops/run book.md", true), {
    source: `https://github.com/Org-x/Repo-y/blob/${"a".repeat(40)}/docs/ops/run%20book.md`,
    history: "https://github.com/Org-x/Repo-y/commits/master/docs/ops/run%20book.md",
    edit: "https://github.com/Org-x/Repo-y/edit/master/docs/ops/run%20book.md",
  });
  assert.equal(githubLinks(src, "docs/a.md", false).edit, null);
  assert.equal(githubLinks({ ...src, github: null }, "docs/a.md", true), null);
  assert.equal(githubLinks({ ...src, github: "javascript:alert(1)//x" }, "docs/a.md", true), null);
  assert.equal(githubLinks({ repo: null, sha: null, ref: null, github: "Org-x/Repo-y" }, "docs/a.md", false).source,
    "https://github.com/Org-x/Repo-y/blob/master/docs/a.md");
});

test("codeLanguage: known labels, raw short names, nothing for no language", () => {
  assert.equal(codeLanguage(["language-bash"]), "Bash");
  assert.equal(codeLanguage("language-hcl"), "HCL");
  assert.equal(codeLanguage(["language-zig"]), "zig");
  assert.equal(codeLanguage(["language-<script>"]), "");
  assert.equal(codeLanguage(undefined), "");
  assert.equal(codeLanguage(["other"]), "");
});

test("splitLeadingH1 + outline seed: ids stay the ones the search index computed on the whole page", () => {
  const md = "\n# Lantern market\n\nIntro.\n\n## Lantern market\n\n## Steps\n";
  const { body, h1 } = splitLeadingH1(md);
  assert.equal(h1, "Lantern market");
  assert.ok(!body.includes("# Lantern market\n\nIntro"));
  // the root builder slugs the H1 first, so the second heading with the same text is "-1"
  assert.deepEqual(outline(body, [h1]).map((h) => h.slug), ["lantern-market-1", "steps"]);
  assert.deepEqual(splitLeadingH1("Intro first.\n# Later\n"), { body: "Intro first.\n# Later\n", h1: null });
  assert.equal(splitLeadingH1("## Not an H1\n").h1, null);
});

test("search URL, hit link, owner label and reading time", () => {
  assert.equal(searchUrl("  đèn lồng "), "/api/admin/docs/search?q=%C4%91%C3%A8n%20l%E1%BB%93ng&limit=20");
  assert.equal(hitHref({ path: "docs/a b.md" }, "đèn-lồng"), "/admin/docs?path=docs%2Fa%20b.md#doc-%C4%91%C3%A8n-l%E1%BB%93ng");
  assert.equal(hitHref({ path: "docs/a.md" }, ""), "/admin/docs?path=docs%2Fa.md");
  assert.equal(ownerLabel("tech-lead"), "Tech lead");
  assert.equal(ownerLabel(null), "");
  assert.equal(readingMinutes("word ".repeat(10)), 1);
  assert.equal(readingMinutes("word ".repeat(1100)), 5);
});

test("rbac: every staff role opens /admin/docs and reads its API; the viewer cannot post feedback", () => {
  for (const role of STAFF_ROLES) {
    assert.ok(canSeePage(role, "/admin/docs"), role);
    assert.ok(canReadAdminPath(role, "docs/manifest"), role);
    assert.ok(canReadAdminPath(role, "docs/search"), role);
  }
  assert.ok(!canWriteAdminPath("viewer", "POST", "docs/feedback"));
  assert.ok(canWriteAdminPath("support", "POST", "docs/feedback"));
  assert.ok(!canSeePage("tenant", "/admin/docs"));
});

test("tocColumnFits: the sticky contents need a reader wide enough to keep the text near 660px", () => {
  // The reader's own width, not the viewport: 1280px with the admin sidebar and the docs list leaves about 540px.
  assert.equal(tocColumnFits(540), false);
  assert.equal(tocColumnFits(742), false);   // 1440px: the text keeps the whole reader instead
  assert.equal(tocColumnFits(897), false);
  assert.equal(tocColumnFits(898), true);    // 660 text + 28 gap + 210 contents
  assert.equal(tocColumnFits(1222), true);   // 1920px
  assert.equal(tocColumnFits(0), false);     // not measured yet: the contents stay above the text
  assert.equal(tocColumnFits(Number.NaN), false);
});
