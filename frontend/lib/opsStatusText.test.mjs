// AA-1140 (S289 live check, VI mode): the Ecosystem status page builds its item and event texts from the API's
// structured fields (lib/opsStatusText.ts) with EN + VI templates in app/admin/overview/status.messages.ts. Fixtures
// are invented. Run: npm run test:unit.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { MSG_KEYS, REASON_CODES, freeLimitText, phrase, phraseKeys, withUnit } from "./opsStatusText.ts";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..");
const catalogue = readFileSync(join(FRONTEND, "app/admin/overview/status.messages.ts"), "utf8");
const words = {
  app: (id) => ({ cis: "CIS", crm: "CRM-word" }[id] ?? id),
  level: (lv) => `lv:${lv}`,
  tile: (id) => `tile:${id}`,
  unit: (_tile, unit) => unit,
  why: (code, running, desired) => (code === "x" ? null : `why:${code}:${running}:${desired}`),
};

test("every key the API sends has a phrase, every phrase a message in English and Vietnamese", () => {
  for (const key of MSG_KEYS) {
    assert.ok(phrase({ key, tile: "cpu", level: "warn", value: 1, limit: 2, unit: "%" }, words), key);
  }
  // the catalogue has two blocks (EN, VI): each message key appears in both
  for (const key of phraseKeys()) {
    const n = catalogue.split(`\n    ${key}:`).length - 1;
    assert.equal(n, 2, `${key} in EN and VI (found ${n})`);
  }
  const whyKeys = ["whyScaledToZero", "whyNoneRunning", "whyRolloutFailed", "whyBelowDesired", "whyRolloutInProgress",
    "whyRunning", "showMore", "showLess", "srcLabel", "freeFallback", "appCrm", "appShared"];
  for (const key of whyKeys) assert.match(catalogue, new RegExp(`${key}:[\\s\\S]*${key}:`), key);
  assert.equal(REASON_CODES.length, 6);
});

test("an unknown key or no message falls back to the API's English text", () => {
  assert.equal(phrase(null, words), null);
  assert.equal(phrase({ key: "other" }, words), null);
  assert.equal(phrase({ key: "zz_new" }, words), null);
});

test("structured fields become formatted arguments", () => {
  assert.deepEqual(phrase({ key: "client_errors", app: "cis", value: 14.96, limit: 10, unit: "%" }, words),
    { key: "m_client_errors", args: ["CIS", "14.96 %", "10 %"] });
  assert.deepEqual(phrase({ key: "p95", app: "crm", value: 1800, limit: 1500, unit: "ms" }, words),
    { key: "m_p95", args: ["CRM-word", "1.80 s", "1.50 s"] });
  assert.deepEqual(phrase({ key: "service", subject: "zz-api", state: "down", code: "x" }, words).args,
    ["zz-api", "lv:down"]);
  assert.deepEqual(phrase({ key: "service_down", subject: "zz", running: 0, desired: 2 }, words).args,
    ["zz", "why:none_running:0:2"]);
  assert.deepEqual(phrase({ key: "deploy", subject: "zz-api", tag: null, rev: "9" }, words).args, ["zz-api", "", "9"]);
});

test("free storage says the share and the GiB it equals; other tiles their unit", () => {
  const free = phrase({ key: "resource", tile: "free_storage", value: 3.1, unit: "GiB", limit: 4, dir: "below",
    level: "warn", value_pct: 15.5, limit_pct: 20, allocated_gib: 20 }, words);
  assert.deepEqual(free, { key: "m_res_free_pct",
    args: ["tile:free_storage", "3.1 GiB", "15.5 %", "20 GiB", "lv:warn", "20 %", "4 GiB"] });
  const gib = phrase({ key: "resource", tile: "free_storage", value: 14.83, unit: "GiB", limit: 15, dir: "below",
    level: "warn" }, words);
  assert.deepEqual(gib, { key: "m_res_below", args: ["tile:free_storage", "14.83 GiB", "lv:warn", "15 GiB"] });
  assert.equal(phrase({ key: "resource", tile: "cpu", value: 95, unit: "%", limit: 90, dir: "above", level: "crit" },
    words).key, "m_res_above");
  assert.equal(freeLimitText(20, 4), "20 % = 4 GiB");
  assert.equal(freeLimitText(null, 15), "15 GiB");
  assert.equal(withUnit(null, "%"), "—");
});
