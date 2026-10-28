// frontend/lib/i18n.test.mjs
// AA-994 — the kit core UI-language module (app/_kit/core/i18n.ts): English by default, Vietnamese only when picked,
// typed catalogues with an English fallback, a cookie preference readable on the server, and a restore script that
// sets <html lang> before paint. Run: npm run test:unit.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_LANG, LANGS, LANG_LABEL, cookieValue, createLangStore, defineMessages, langFromCookie, langRestoreScript,
  localized, missingTranslations, parseLang, translator,
} from "../app/_kit/core/i18n.ts";

const M = defineMessages(
  { title: "Jobs", rows: (n) => `${n} ${n === 1 ? "row" : "rows"}`, only: "English only" },
  { title: "Công việc", rows: (n) => `${n} dòng`, only: "" },
);

test("English is the default; only 'vi' selects Vietnamese", () => {
  assert.equal(DEFAULT_LANG, "en");
  assert.deepEqual([...LANGS], ["en", "vi"]);
  for (const v of [undefined, null, "", "en", "fr", "vietnamese", 1, {}]) assert.equal(parseLang(v), "en", String(v));
  for (const v of ["vi", "VI", " vi "]) assert.equal(parseLang(v), "vi");
  assert.equal(LANG_LABEL.vi.name, "Tiếng Việt");
});

test("translator: strings, typed functions, English fallback, unknown key", () => {
  const en = translator(M, "en");
  const vi = translator(M, "vi");
  assert.equal(en("title"), "Jobs");
  assert.equal(vi("title"), "Công việc");
  assert.equal(en("rows", 1), "1 row");
  assert.equal(vi("rows", 3), "3 dòng");
  assert.equal(vi("only"), "", "an empty translation is shown as given (missingTranslations reports it)");
  const partial = { en: { a: "A", b: "B" }, vi: { a: "Á" } };
  assert.equal(translator(partial, "vi")("b"), "B", "a key missing from vi falls back to English");
  assert.equal(translator(partial, "vi")("nope"), "nope");
});

test("missingTranslations lists empty or absent Vietnamese keys", () => {
  assert.deepEqual(missingTranslations(M), ["only"]);
  assert.deepEqual(missingTranslations({ en: { a: "A", f: () => "x" }, vi: { a: "a", f: "x" } }), ["f"]);
});

test("localized picks the language's value, English when absent", () => {
  assert.equal(localized({ en: 1, vi: 2 }, "vi"), 2);
  assert.equal(localized({ en: 1 }, "vi"), 1);
});

test("cookie parsing for a server component", () => {
  assert.equal(cookieValue("a=1; cis_lang=vi; b=2", "cis_lang"), "vi");
  assert.equal(cookieValue("xcis_lang=vi", "cis_lang"), null);
  assert.equal(cookieValue("", "cis_lang"), null);
  assert.equal(langFromCookie("a=1; cis_lang=vi", "cis_lang"), "vi");
  assert.equal(langFromCookie("a=1", "cis_lang"), "en");
  assert.equal(langFromCookie("vi"), "vi", "a bare cookie value (cookies().get(name)?.value)");
  assert.equal(langFromCookie(undefined), "en");
});

test("the cookie name is checked", () => {
  assert.throws(() => createLangStore({ cookieName: "bad name;" }));
  assert.throws(() => langRestoreScript("x'); alert(1); //"));
});

/** A minimal browser: document.cookie as a jar, <html lang>, localStorage, storage events. */
function fakeBrowser(t, { protocol = "https:", cookie = "" } = {}) {
  const jar = new Map(cookie ? cookie.split("; ").map((c) => c.split("=")) : []);
  const writes = [];
  const attrs = { lang: "en" };
  const store = new Map();
  const listeners = new Set();
  globalThis.document = {
    get cookie() { return [...jar].map(([k, v]) => `${k}=${v}`).join("; "); },
    set cookie(v) {
      writes.push(v);
      const [pair, ...opts] = v.split("; ");
      const [k, val] = pair.split("=");
      if (opts.includes("Max-Age=0")) jar.delete(k); else jar.set(k, val);
    },
    documentElement: { setAttribute: (k, v) => { attrs[k] = v; } },
  };
  globalThis.window = {
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
    addEventListener: (type, f) => type === "storage" && listeners.add(f),
    removeEventListener: (type, f) => listeners.delete(f),
  };
  globalThis.location = { protocol };
  t.after(() => { delete globalThis.document; delete globalThis.window; delete globalThis.location; });
  return { jar, writes, attrs, store, fireStorage: (key) => listeners.forEach((f) => f({ key })) };
}

test("store: server snapshot and no-DOM reads are English", () => {
  const s = createLangStore({ cookieName: "cis_lang" });
  assert.equal(s.getServerSnapshot(), "en");
  assert.equal(s.readLang(), "en");
  s.setLang("vi"); // no document: a no-op, never throws
});

test("store: setLang writes the cookie, the mirror and <html lang>, and notifies", (t) => {
  const b = fakeBrowser(t);
  const s = createLangStore({ cookieName: "cis_lang" });
  let calls = 0;
  const off = s.subscribe(() => { calls += 1; });
  assert.equal(s.getSnapshot(), "en");
  s.setLang("vi");
  assert.equal(s.getSnapshot(), "vi");
  assert.equal(b.attrs.lang, "vi");
  assert.equal(b.store.get("cis_lang"), "vi");
  assert.match(b.writes.at(-1), /^cis_lang=vi; Path=\/; Max-Age=31536000; SameSite=Lax; Secure$/);
  s.setLang("en");
  assert.equal(b.jar.has("cis_lang"), false, "English clears the cookie (absent === English)");
  assert.equal(b.store.has("cis_lang"), false);
  assert.equal(s.getSnapshot(), "en");
  assert.equal(calls, 2);
  off();
  s.setLang("vi");
  assert.equal(calls, 2, "unsubscribed");
});

test("store: no Secure flag on http (local dev); another tab's change notifies", (t) => {
  const b = fakeBrowser(t, { protocol: "http:", cookie: "cis_lang=vi" });
  const s = createLangStore({ cookieName: "cis_lang" });
  assert.equal(s.getSnapshot(), "vi");
  s.setLang("vi");
  assert.doesNotMatch(b.writes.at(-1), /Secure/);
  let calls = 0;
  s.subscribe(() => { calls += 1; });
  b.jar.delete("cis_lang");
  b.fireStorage("other_key");
  b.fireStorage("cis_lang");
  assert.equal(calls, 1);
  assert.equal(b.attrs.lang, "en");
});

test("restore script sets <html lang> from the cookie before paint", () => {
  for (const [cookie, want] of [["cis_lang=vi", "vi"], ["a=1;  cis_lang=VI", "vi"], ["xcis_lang=vi", "en"], ["", "en"]]) {
    const attrs = {};
    const doc = { cookie, documentElement: { setAttribute: (k, v) => { attrs[k] = v; } } };
    new Function("document", createLangStore({ cookieName: "cis_lang" }).restoreScript)(doc);
    assert.equal(attrs.lang, want, cookie);
  }
});

test("store: blocked cookies keep the choice for this page view", (t) => {
  const b = fakeBrowser(t);
  Object.defineProperty(globalThis.document, "cookie", { get: () => "", set: () => {}, configurable: true });
  const s = createLangStore({ cookieName: "cis_lang" });
  s.setLang("vi");
  assert.equal(s.getSnapshot(), "vi");
  assert.equal(b.attrs.lang, "vi");
  s.setLang("en");
  assert.equal(s.getSnapshot(), "en");
});
