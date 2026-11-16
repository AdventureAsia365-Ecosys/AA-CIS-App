// frontend/lib/settingsLinks.test.mjs — S282 (AA-1122): every deep link to Platform › Settings uses only the query
// params the page reads (SettingsRegistry useAskedTab: app, area, key). The Research tab linked `?q=research`,
// which the page ignores, so the link opened the unfiltered CIS tab.
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";

const ROOTS = ["app", "lib", "components"];
const ALLOWED = new Set(["app", "area", "key"]);

function* sources(dir) {
  let names = [];
  try { names = readdirSync(dir); } catch { return; }
  for (const n of names) {
    if (n === "node_modules" || n.startsWith(".")) continue;
    const p = join(dir, n);
    if (statSync(p).isDirectory()) yield* sources(p);
    else if (/\.(tsx?|mjs)$/.test(n) && !n.endsWith(".test.mjs")) yield p;
  }
}

test("settings deep links use only app / area / key", () => {
  const bad = [];
  for (const root of ROOTS) {
    for (const file of sources(root)) {
      for (const m of readFileSync(file, "utf8").matchAll(/\/admin\/platform\/settings\?([^"'`\s)]*)/g)) {
        for (const pair of m[1].split("&")) {
          const name = pair.split("=")[0];
          if (name && !name.startsWith("$") && !ALLOWED.has(name)) bad.push(`${file}: ${m[0]}`);
        }
      }
    }
  }
  assert.deepEqual(bad, []);
});
