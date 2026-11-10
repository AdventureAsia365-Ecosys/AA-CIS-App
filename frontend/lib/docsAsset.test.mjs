// frontend/lib/docsAsset.test.mjs — AA-1064: the BFF decodes the docs image JSON and sends the image headers.
// Run: npm run test:unit (node --test with --experimental-strip-types). Images are built in code.

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { test } from "node:test";

import { DOC_IMAGE_MAX_BYTES, base64Search, decodeDocsAsset, imageHeaders, isDocsAssetRead } from "./docsAsset.ts";

const PNG = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 0, 0x49, 0x45, 0x4e, 0x44]);
const WEBP = Buffer.concat([Buffer.from("RIFF"), Buffer.from([4, 0, 0, 0]), Buffer.from("WEBP")]);
const SVG = Buffer.from('<svg xmlns="http://www.w3.org/2000/svg"/>');

function answer(type, body, over = {}) {
  return {
    repo: "AA-Ecosys", path: "docs/guides/images/x", type, bytes: body.length,
    sha256: createHash("sha256").update(body).digest("hex"), data: body.toString("base64"), ...over,
  };
}

test("only GET of docs/asset and of the Guide tab's image route is decoded by the BFF", () => {
  assert.equal(isDocsAssetRead("GET", "docs/asset"), true);
  assert.equal(isDocsAssetRead("GET", "ai-gateway/guide/asset"), true);
  assert.equal(isDocsAssetRead("GET", "ai-gateway/guide"), false);
  assert.equal(isDocsAssetRead("PUT", "ai-gateway/guide/asset"), false);
  assert.equal(isDocsAssetRead("POST", "docs/asset"), false);
  assert.equal(isDocsAssetRead("GET", "docs/page"), false);
  assert.equal(isDocsAssetRead("GET", "docs/asset/x"), false);
});

test("the upstream query always asks for base64", () => {
  assert.equal(base64Search("?repo=AA-Ecosys&path=docs%2Fa.png"), "?repo=AA-Ecosys&path=docs%2Fa.png&encoding=base64");
  assert.equal(base64Search("?path=a.png&encoding=raw"), "?path=a.png&encoding=base64");
  assert.equal(base64Search(""), "?encoding=base64");
});

test("PNG, WebP and SVG decode byte for byte with the image headers", () => {
  for (const [type, body] of [["image/png", PNG], ["image/webp", WEBP], ["image/svg+xml", SVG]]) {
    const got = decodeDocsAsset(answer(type, body));
    assert.ok(got, type);
    assert.deepEqual(Buffer.from(got.body), body);
    assert.equal(got.headers["Content-Type"], type);
    assert.equal(got.headers["Cache-Control"], "private, max-age=300");
    assert.equal(got.headers["X-Content-Type-Options"], "nosniff");
    assert.match(got.headers["Content-Security-Policy"], /sandbox/);
  }
});

test("anything else is refused", () => {
  const big = Buffer.concat([PNG, Buffer.alloc(DOC_IMAGE_MAX_BYTES["image/png"])]);
  const bad = [
    null, "x", 3, {},
    answer("image/jpeg", PNG),
    answer("text/html", SVG),
    answer("image/png", PNG, { data: "not base64!" }),
    answer("image/png", PNG, { data: "abc" }),
    answer("image/png", PNG, { bytes: PNG.length + 1 }),
    answer("image/png", PNG, { sha256: "0".repeat(64) }),
    answer("image/png", big),
    answer("image/png", WEBP), // a WebP labelled PNG
    answer("image/webp", PNG),
  ];
  for (const json of bad) assert.equal(decodeDocsAsset(json), null, JSON.stringify(json)?.slice(0, 80));
});

test("imageHeaders is the same set for every type", () => {
  assert.deepEqual(Object.keys(imageHeaders("image/png")).sort(), Object.keys(imageHeaders("image/svg+xml")).sort());
});
