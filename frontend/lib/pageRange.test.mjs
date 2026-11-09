// frontend/lib/pageRange.test.mjs
// AA-1006 follow-up (S263 F4): row numbers, range and pager follow the rows on screen. Run: npm run test:unit.
//
// Master Content keeps the previous page on screen while the next one loads (react-query placeholderData); it used to
// number those old rows 21–40 and say "21–40 of 892" from the page just asked for.

import assert from "node:assert/strict";
import { test } from "node:test";

import { shownRange } from "./pageRange.ts";

test("the answer's own page wins over the page just asked for (previous rows still on screen)", () => {
  // Asked for page 2, the rows on screen still came with page 1.
  const r = shownRange({ page: 1, page_size: 20, total: 892 }, { page: 2, pageSize: 20 }, 20);
  assert.deepEqual(r, { page: 1, pageSize: 20, offset: 0, from: 1, to: 20, total: 892 });
});

test("once page 2 arrives the range moves with it", () => {
  const r = shownRange({ page: 2, page_size: 20, total: 892 }, { page: 2, pageSize: 20 }, 20);
  assert.equal(r.offset, 20);
  assert.equal(r.from, 21);
  assert.equal(r.to, 40);
  assert.equal(r.page, 2);
});

test("a page-size change keeps the old size until the answer with the new size arrives", () => {
  const r = shownRange({ page: 3, page_size: 20, total: 892 }, { page: 1, pageSize: 100 }, 20);
  assert.equal(r.pageSize, 20);
  assert.equal(r.from, 41);
  assert.equal(r.to, 60);
});

test("an offset-paged answer (offset / limit) works the same way", () => {
  const r = shownRange({ offset: 100, limit: 50, total: 230 }, { page: 4, pageSize: 50 }, 50);
  assert.deepEqual(r, { page: 3, pageSize: 50, offset: 100, from: 101, to: 150, total: 230 });
});

test("the last, short page ends at the last row", () => {
  const r = shownRange({ offset: 200, limit: 50, total: 230 }, { page: 5, pageSize: 50 }, 30);
  assert.equal(r.from, 201);
  assert.equal(r.to, 230);
});

test("before the first answer the requested page is used; no rows reads 0–0", () => {
  assert.deepEqual(shownRange(undefined, { page: 3, pageSize: 25 }, 0), {
    page: 3, pageSize: 25, offset: 50, from: 0, to: 0, total: 0,
  });
});

test("a total smaller than the rows on screen never yields a range past the total", () => {
  const r = shownRange({ page: 1, page_size: 20, total: 3 }, { page: 1, pageSize: 20 }, 5);
  assert.equal(r.total, 5);
  assert.equal(r.to, 5);
});

test("bad numbers in an answer fall back to the request", () => {
  const r = shownRange({ page: 0, page_size: -5, offset: null, total: null }, { page: 2, pageSize: 10 }, 10);
  assert.deepEqual(r, { page: 2, pageSize: 10, offset: 10, from: 11, to: 20, total: 20 });
});
