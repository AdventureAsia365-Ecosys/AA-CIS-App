// The docs reader's Mermaid diagrams wait for the brand font before drawing (lib/fontReady.ts).
import assert from "node:assert/strict";
import { test } from "node:test";

import { FONT_WAIT_MS, fontIsReady, labelFontSpecs, primaryFamily, waitForFont } from "./fontReady.ts";

const FAMILY = "'Poppins', system-ui, -apple-system, sans-serif";

/** A FontFaceSet stand-in: `loaded` says which specs are in; load() resolves when release() is called. */
function fakeFonts({ loaded = new Set(), failLoad = false } = {}) {
  const asked = [];
  let release;
  const gate = new Promise((r) => { release = r; });
  return {
    asked,
    release: () => release(),
    check: (spec) => loaded.has(spec),
    load: (spec) => {
      asked.push(spec);
      return failLoad ? Promise.reject(new Error("network")) : gate.then(() => loaded.add(spec));
    },
    get ready() { return gate; },
  };
}

test("the first family of the CSS list is the one waited for", () => {
  assert.equal(primaryFamily(FAMILY), "'Poppins'");
  assert.deepEqual(labelFontSpecs(FAMILY), ["400 16px 'Poppins'", "700 16px 'Poppins'"]);
});

test("ready only when every label weight is loaded", () => {
  assert.equal(fontIsReady(fakeFonts(), FAMILY), false);
  assert.equal(fontIsReady(fakeFonts({ loaded: new Set(["400 16px 'Poppins'"]) }), FAMILY), false);
  assert.equal(fontIsReady(fakeFonts({ loaded: new Set(labelFontSpecs(FAMILY)) }), FAMILY), true);
});

test("no font set (server, old browser) or a throwing check never blocks", () => {
  assert.equal(fontIsReady(undefined, FAMILY), true);
  assert.equal(fontIsReady({ check() { throw new Error("bad"); } }, FAMILY), true);
});

test("waits until the font has loaded, asking for both weights", async () => {
  const fonts = fakeFonts();
  let done = false;
  const wait = waitForFont(fonts, FAMILY, 5_000).then(() => { done = true; });
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(done, false, "must not draw before the font is in");
  assert.deepEqual(fonts.asked, labelFontSpecs(FAMILY));
  fonts.release();
  await wait;
  assert.equal(fontIsReady(fonts, FAMILY), true);
});

test("a font that never arrives does not hold the diagram past the cap", async () => {
  const started = Date.now();
  await waitForFont(fakeFonts(), FAMILY, 30);
  assert.ok(Date.now() - started < 1_000);
  assert.ok(FONT_WAIT_MS <= 5_000, "the default cap stays short");
});

test("a failed font download resolves (draw with the fallback face)", async () => {
  await waitForFont(fakeFonts({ failLoad: true }), FAMILY, 5_000);
  await waitForFont(undefined, FAMILY, 5_000);
});
