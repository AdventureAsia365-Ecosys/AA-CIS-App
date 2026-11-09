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
//      provider;
//   4. AA-1040: the narration variant (a traveller reading what the AI wrote on request, EU AI Act Art. 50(1)) and
//      the badge form ("AI-assisted", the variant's sentence as its accessible name and tooltip).
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
let AI_BADGE_TEXT;
let aiBadgeText;
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
  ({ AI_NOTICE_MESSAGES: AI_NOTICE_TEXT, AI_NOTICE_VARIANTS, aiNoticeText, AI_BADGE_MESSAGES: AI_BADGE_TEXT,
    aiBadgeText } = await import(
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
  assert.deepEqual([...AI_NOTICE_VARIANTS].sort(), ["narration", "publisher", "staff", "traveller"]);
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
  assert.match(barrel, /export \{ AiNotice\b[^}]*\} from "\.\/core\/AiNotice"/);
  assert.match(barrel, /aiNoticeText[^;]*from "\.\/core\/aiNotice\.messages"/);
  assert.match(barrel, /aiBadgeText[^;]*from "\.\/core\/aiNotice\.messages"/);
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

test("placements: every CIS admin screen where a reviewer acts on AI output renders the staff notice", () => {
  const APP = join(FRONTEND, "app", "admin");
  const src = (...p) => readFileSync(join(APP, ...p), "utf8");
  // Review Queue editor and the S1 rewrite tab (also Master Content).
  for (const file of [["review", "ReviewEditor.tsx"], ["_components", "TourDetailPanelV2.tsx"]]) {
    const s = src(...file);
    assert.match(s, /import \{[^}]*\bAiNotice\b[^}]*\} from "\.\.\/\.\.\/_kit"/, file.join("/"));
    assert.match(s, /<AiNotice\b/, file.join("/"));
  }
  // Content quality: the model's rewrite of a flagged sentence, shared by the T9 piece drawer and the T2 version drawer.
  // AA-1039 (S266): the page moved to /admin/tenant-qa/quality, one folder deeper.
  const sa = src("tenant-qa", "quality", "SentenceActions.tsx");
  assert.match(sa, /import \{[^}]*\bAiNotice\b[^}]*\} from "\.\.\/\.\.\/\.\.\/_kit"/);
  const panel = sa.slice(sa.indexOf("export function RewritePanel"), sa.indexOf("export interface EditOwner"));
  assert.match(panel, /\{sug && <AiNotice testId="cq-rewrite-ai-notice" \/>\}/);
  assert.match(src("tenant-qa", "quality", "TourVersionDrawer.tsx"), /<RewritePanel\b/);
  assert.match(sa, /<RewritePanel\b/, "the piece drawer's flagged sentences use the same panel");
});

test("narration (AA-1040): says the AI wrote it on request, may be wrong, the quote binds", () => {
  const en = AI_NOTICE_TEXT.en.narration;
  assert.match(en, /written by AI when you asked for it/);
  assert.match(en, /may contain mistakes/);
  assert.match(en, /confirmed in your official quote/);
  assert.equal(aiNoticeText("narration", "vi"), AI_NOTICE_TEXT.vi.narration);
  assert.notEqual(AI_NOTICE_TEXT.vi.narration, en);
});

test("badge text (AA-1040): AI-assisted in English, Vietnamese for vi, the same language rule", () => {
  assert.equal(AI_BADGE_TEXT.en.badge, "AI-assisted");
  assert.ok(AI_BADGE_TEXT.vi.badge.trim().length > 0);
  assert.equal(aiBadgeText(), "AI-assisted");
  assert.equal(aiBadgeText("vi-VN"), AI_BADGE_TEXT.vi.badge);
  assert.equal(aiBadgeText("fr"), "AI-assisted");
});

test("badge form (AA-1040): a note span, short text shown, the variant sentence as name and tooltip", () => {
  const html = render(h(N.AiNotice, { variant: "publisher", form: "badge", testId: "piece-ai-badge" }));
  assert.match(html, /^<span role="note"/);
  assert.match(html, /data-testid="piece-ai-badge"/);
  assert.match(html, /data-ai-notice="publisher"/);
  assert.match(html, /data-ai-notice-form="badge"/);
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
  assert.ok(html.includes(`title="${esc(AI_NOTICE_TEXT.en.publisher)}"`), "tooltip = the sentence");
  assert.ok(html.includes(`aria-label="AI-assisted. ${esc(AI_NOTICE_TEXT.en.publisher)}"`), "name = badge + sentence");
  assert.match(html, />AI-assisted<\/span><\/span>$/);
  assert.match(html, /<svg[^>]*aria-hidden="true"/);
  assert.doesNotMatch(html, /<p\b/, "a badge is inline, never a paragraph");
  const vi = render(h(N.AiNotice, { variant: "staff", form: "badge", lang: "vi" }));
  assert.ok(vi.includes(AI_BADGE_TEXT.vi.badge) && vi.includes(esc(AI_NOTICE_TEXT.vi.staff)));
  // the default form stays the line
  assert.match(render(h(N.AiNotice, { variant: "narration" })), /^<p role="note" data-testid="ai-notice" data-ai-notice="narration"/);
});
