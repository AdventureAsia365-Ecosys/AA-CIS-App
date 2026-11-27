// AA-1185 (S295 security audit SA-73): public/.well-known/security.txt (RFC 9116) is served on this app's host.
// The file needs Contact and Expires; Expires must be in the future and at most a year ahead (RFC 9116 section 2.5.5
// recommends less than a year), so this test turns red when the file is due for its yearly refresh.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const APP = join(dirname(fileURLToPath(import.meta.url)), "..");
const TEXT = readFileSync(join(APP, "public", ".well-known", "security.txt"), "utf8");

function fields(text) {
  const out = {};
  for (const line of text.split("\n")) {
    if (!line.trim() || line.startsWith("#")) continue;
    const m = /^([A-Za-z-]+): (.+)$/.exec(line);
    assert.ok(m, `not a "Field: value" line: ${line}`);
    (out[m[1].toLowerCase()] ??= []).push(m[2]);
  }
  return out;
}

test("security.txt has one or more mailto/https Contact lines and exactly one Expires", () => {
  const f = fields(TEXT);
  assert.ok(f.contact?.length >= 1);
  for (const c of f.contact) assert.match(c, /^(mailto:[^@\s]+@[^@\s]+|https:\/\/\S+)$/);
  assert.equal(f.expires?.length, 1);
  assert.match(f.expires[0], /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$/);
});

test("security.txt Expires is in the future and at most 366 days ahead (refresh it yearly)", () => {
  const expires = Date.parse(fields(TEXT).expires[0]);
  const now = Date.now();
  assert.ok(expires > now, "security.txt has expired: move Expires a year ahead");
  assert.ok(expires - now <= 366 * 24 * 3600 * 1000, "Expires more than a year ahead (RFC 9116 recommends less)");
});

test("security.txt is plain ASCII with LF line endings and ends with a newline", () => {
  assert.ok(/^[\x09\x0a\x20-\x7e]*$/.test(TEXT));
  assert.ok(TEXT.endsWith("\n") && !TEXT.includes("\r"));
});
