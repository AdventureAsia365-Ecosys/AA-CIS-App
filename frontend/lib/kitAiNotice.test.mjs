// frontend/lib/kitAiNotice.test.mjs
// AA-1033 — the one AI notice of the ecosystem (app/_kit/core/AiNotice.tsx + aiNotice.messages.ts). Run: npm run test:unit.
//
// Checks:
//   1. the wording: every variant exists in English and Vietnamese, none empty; the staff and traveller sentences are
//      the issue's; the publisher sentence is the portal's notice word for word; no sentence claims to exclude
//      liability; `aiNoticeText` picks Vietnamese for "vi" / "vi-VN" only;
//   2. the files: AiNotice.tsx is a full-profile core file exported by the barrel, aiNotice.messages.ts is in the
//      tokens profile (TripPlanner renders the traveller sentence from it) and imports only i18n.ts, also a token file;
//   3. the markup: a `<p role="note">` with the test id and variant, the icon hidden from screen readers, English
//      outside an I18nProvider, Vietnamese inside one set to vi or with `lang="vi"`, the `lang` prop winning over the
//      provider.
// Like kitButtons.test.mjs, the TSX core is transpiled into a temporary folder inside frontend/ (so `react` and
// `lucide-react` resolve) and rendered with react-dom/server.

import assert from "node:assert/strict";
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createRequire } from "node:module";

import { buildManifest, importSpecifiers } from "../app/_kit/core/check-kit-copy.mjs";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const CORE = join(FRONTEND, "app", "_kit", "core");
const require = createRequire(join(FRONTEND, "package.json"));
const ts = require("typescript");

let dir;
let N; // AiNotice module
let I; // useI18n module
let L; // i18n module
// aiNotice.messages (imports ./i18n without an extension, so it is loaded from the transpiled folder too)
let AI_NOTICE_TEXT;
let AI_NOTICE_VARIANTS;
let aiNoticeText;
let React;
let render;

before(async () => {
  dir = mkdtempSync(join(FRONTEND, ".kit-render-"));
  for (const name of readdirSync(CORE).filter((n) => /\.(ts|tsx)$/.test(n))) {
    const src = readFileSync(join(CORE, name), "utf8");
    const out = ts.transpileModule(src, {
      compilerOptions: {
        module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
        verbatimModuleSyntax: false, isolatedModules: true,
      },
      fileName: name,
    }).outputText.replace(/(from\s+|import\s*\(\s*|import\s+)(["'])(\.\/[^"']+)\2/g, "$1$2$3.mjs$2");
    writeFileSync(join(dir, name.replace(/\.(ts|tsx)$/, ".mjs")), out);
  }
  N = await import(pathToFileURL(join(dir, "AiNotice.mjs")).href);
  I = await import(pathToFileURL(join(dir, "useI18n.mjs")).href);
  L = await import(pathToFileURL(join(dir, "i18n.mjs")).href);
  ({ AI_NOTICE_MESSAGES: AI_NOTICE_TEXT, AI_NOTICE_VARIANTS, aiNoticeText } = await import(
    pathToFileURL(join(dir, "aiNotice.messages.mjs")).href
  ));
  React = require("react");
  ({ renderToStaticMarkup: render } = require("react-dom/server"));
});

after(() => {
  if (dir) rmSync(dir, { recursive: true, force: true });
});

const h = (...a) => React.createElement(...a);

test("every variant has English and Vietnamese text, none empty", () => {
  assert.deepEqual([...AI_NOTICE_VARIANTS].sort(), ["publisher", "staff", "traveller"]);
  for (const lang of ["en", "vi"]) {
    assert.deepEqual(Object.keys(AI_NOTICE_TEXT[lang]).sort(), [...AI_NOTICE_VARIANTS].sort(), lang);
    for (const v of AI_NOTICE_VARIANTS) assert.ok(AI_NOTICE_TEXT[lang][v].trim().length > 20, `${lang}.${v}`);
  }
});

test("the wording: mistakes, the binding quote, the portal sentence kept, no liability waiver", () => {
  const en = AI_NOTICE_TEXT.en;
  assert.equal(
    en.staff, "AI can make mistakes. Check prices, dates, visa and safety information before you send or publish this.",
  );
  assert.match(en.traveller, /may contain mistakes/);
  assert.match(en.traveller, /confirmed in your official quote/);
  assert.equal(
    en.publisher,
    "This content was generated with AI. Review it before publishing; you are responsible for what you publish.",
  );
  for (const lang of ["en", "vi"]) {
    for (const v of AI_NOTICE_VARIANTS) {
      assert.doesNotMatch(AI_NOTICE_TEXT[lang][v], /liab|not responsible|no responsibility|disclaim|miễn trừ/i);
    }
  }
});

test("aiNoticeText: Vietnamese only for vi / vi-*, English otherwise", () => {
  assert.equal(aiNoticeText("staff"), AI_NOTICE_TEXT.en.staff);
  assert.equal(aiNoticeText("staff", "vi"), AI_NOTICE_TEXT.vi.staff);
  assert.equal(aiNoticeText("traveller", "vi-VN"), AI_NOTICE_TEXT.vi.traveller);
  assert.equal(aiNoticeText("traveller", "VI"), AI_NOTICE_TEXT.vi.traveller);
  for (const other of ["en", "en-AU", "fr", "", null, undefined]) {
    assert.equal(aiNoticeText("publisher", other), AI_NOTICE_TEXT.en.publisher, String(other));
  }
});

test("AiNotice is a full-profile core file; its text file travels with the tokens profile with i18n.ts", () => {
  const manifest = buildManifest(CORE);
  assert.ok("AiNotice.tsx" in manifest.files);
  assert.ok(!manifest.profiles.tokens.includes("AiNotice.tsx"), "the React component is not a token file");
  assert.ok(manifest.profiles.tokens.includes("aiNotice.messages.ts"), "TripPlanner gets the wording");
  assert.deepEqual(importSpecifiers(readFileSync(join(CORE, "aiNotice.messages.ts"), "utf8")), ["./i18n"]);
  assert.ok(manifest.profiles.tokens.includes("i18n.ts"), "its one import is a token file too");
  const barrel = readFileSync(join(CORE, "..", "index.ts"), "utf8");
  assert.match(barrel, /export \{ AiNotice \} from "\.\/core\/AiNotice"/);
  assert.match(barrel, /aiNoticeText[^;]*from "\.\/core\/aiNotice\.messages"/);
});

test("markup: a note with the test id and variant, icon hidden, English outside a provider", () => {
  const html = render(h(N.AiNotice));
  assert.match(html, /^<p role="note" data-testid="ai-notice" data-ai-notice="staff"/);
  assert.match(html, /<svg[^>]*aria-hidden="true"/);
  assert.ok(html.includes(AI_NOTICE_TEXT.en.staff));
  const t = render(h(N.AiNotice, { variant: "traveller", testId: "tour-ai-notice", style: { marginTop: 8 } }));
  assert.match(t, /data-testid="tour-ai-notice" data-ai-notice="traveller"/);
  assert.match(t, /margin-top:8px/);
  assert.ok(t.includes(AI_NOTICE_TEXT.en.traveller));
});

test("language: the provider's, or the lang prop, which wins", () => {
  const store = L.createLangStore({ cookieName: "kit_ai_notice_test" });
  const inVi = (child) => render(h(I.I18nProvider, { store, initialLang: "vi" }, child));
  assert.ok(inVi(h(N.AiNotice)).includes(AI_NOTICE_TEXT.vi.staff));
  assert.ok(render(h(N.AiNotice, { variant: "publisher", lang: "vi" })).includes(AI_NOTICE_TEXT.vi.publisher));
  assert.ok(inVi(h(N.AiNotice, { lang: "en" })).includes(AI_NOTICE_TEXT.en.staff));
});
