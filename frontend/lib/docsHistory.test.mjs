// frontend/lib/docsHistory.test.mjs
// AA-1132: the docs reader's page history and review flag helpers (lib/docsReader.ts pageHistory, lastChangedDate,
// historyLinks, needsReview, docsChangesPage). Every page, subject and repo name is invented (this repo is public).
// Run: npm run test:unit (node --test with --experimental-strip-types).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DOCS_CHANGES_ID,
  DOCS_CHANGES_PATH,
  docsChangesPage,
  historyLinks,
  lastChangedDate,
  needsReview,
  pageHistory,
} from "./docsReader.ts";

const page = (path, over = {}) => ({
  repo: "AA-Ecosys", path, id: "X-02", title: path, description: null, type: "how-to", status: "active",
  audience: ["staff"], owner: "tech-lead", last_reviewed: null, generated: false, access: "staff", bytes: 1,
  ...over,
});

const SHA = "c".repeat(40);

test("pageHistory keeps usable items in order and fills the optional parts with null", () => {
  const items = pageHistory(page("docs/kite.md", {
    history: [
      { sha: SHA, date: "2026-11-17T09:00:00+07:00", subject: " Kite route moved ", pr: 875 },
      { date: "2026-11-12", subject: "A change to 2 pages with restricted access" },
      { sha: "XYZ", date: "2026-11-10", subject: "", pr: 0 },
      { sha: SHA, date: "", subject: "no date" },
      { sha: SHA, date: "2026-11-09", subject: 7 },
      null,
      "text",
    ],
  }));
  assert.deepEqual(items, [
    { sha: SHA, date: "2026-11-17T09:00:00+07:00", subject: "Kite route moved", pr: 875 },
    { sha: null, date: "2026-11-12", subject: "A change to 2 pages with restricted access", pr: null },
    { sha: null, date: "2026-11-10", subject: "", pr: null },
  ]);
});

test("pageHistory is empty when the field is absent or not a list", () => {
  assert.deepEqual(pageHistory(page("docs/kite.md")), []);
  assert.deepEqual(pageHistory(page("docs/kite.md", { history: "x" })), []);
  assert.deepEqual(pageHistory(page("docs/kite.md", { history: { sha: SHA } })), []);
});

test("lastChangedDate prefers last_changed, then the newest history item", () => {
  const history = [{ sha: SHA, date: "2026-11-17", subject: "a", pr: 1 }];
  assert.equal(lastChangedDate(page("a.md", { last_changed: { sha: SHA, date: "2026-11-18" }, history })), "2026-11-18");
  assert.equal(lastChangedDate(page("a.md", { last_changed: null, history })), "2026-11-17");
  assert.equal(lastChangedDate(page("a.md")), null);
});

test("historyLinks point at the bundle's public repo, only for the parts an item has", () => {
  const item = { sha: SHA, date: "2026-11-17", subject: "a", pr: 875 };
  assert.deepEqual(historyLinks(item, "Org-x/Repo-y"), {
    commit: `https://github.com/Org-x/Repo-y/commit/${SHA}`,
    pr: "https://github.com/Org-x/Repo-y/pull/875",
  });
  assert.deepEqual(historyLinks({ ...item, sha: null, pr: null }, "Org-x/Repo-y"), { commit: null, pr: null });
  assert.deepEqual(historyLinks(item, null), { commit: null, pr: null });
  assert.deepEqual(historyLinks(item, "not a/repo/name"), { commit: null, pr: null });
});

test("needsReview shows only a true flag, with its reason when it has text", () => {
  assert.deepEqual(needsReview(page("a.md", { needs_review: true, needs_review_reason: " Route changed. " })),
    { reason: "Route changed." });
  assert.deepEqual(needsReview(page("a.md", { needs_review: true, needs_review_reason: "  " })), { reason: null });
  assert.deepEqual(needsReview(page("a.md", { needs_review: true })), { reason: null });
  assert.equal(needsReview(page("a.md", { needs_review: false, needs_review_reason: "old" })), null);
  assert.equal(needsReview(page("a.md", { needs_review: "true" })), null);
  assert.equal(needsReview(page("a.md")), null);
});

test("docsChangesPage finds the generated page by path, else by id, else nothing", () => {
  const changes = page(DOCS_CHANGES_PATH, { id: DOCS_CHANGES_ID, generated: true });
  assert.equal(docsChangesPage([page("docs/a.md"), changes]), changes);
  const moved = page("docs/elsewhere/changes.md", { id: DOCS_CHANGES_ID });
  assert.equal(docsChangesPage([page("docs/a.md"), moved]), moved);
  assert.equal(docsChangesPage([page("docs/a.md")]), undefined);
});
