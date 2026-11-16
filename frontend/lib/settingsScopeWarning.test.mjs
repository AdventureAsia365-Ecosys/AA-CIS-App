// frontend/lib/settingsScopeWarning.test.mjs
// AA-963 / AA-970 (S282) — Platform › Settings, CRM tab: the warning of the change dialog when an agency feature
// switch that is off by default (crm.search.enabled, crm.release.preview) is about to be changed for the whole
// platform (app/admin/platform/settings/ScopeWarning.tsx). Rendered with react-dom/server: the warning shows for
// Platform only, never for One agency or for another key, in English outside an I18nProvider and in Vietnamese inside
// one set to vi. Like kitAiNotice.test.mjs, the kit core, lib/settingsRegistry.ts, the page's messages.ts and the
// component are transpiled into a temporary folder inside frontend/ (so `react` resolves). Run: npm run test:unit.

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { after, before, test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import { createRequire } from "node:module";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const CORE = join(FRONTEND, "app", "_kit", "core");
const PAGE = join(FRONTEND, "app", "admin", "platform", "settings");
const require = createRequire(join(FRONTEND, "package.json"));
const ts = require("typescript");

let dir;
let W; // ScopeWarning module
let I; // useI18n module
let L; // i18n module
let SETTINGS;
let React;
let render;

/** Transpile one TS/TSX file to an .mjs file, its relative imports pointed at .mjs files (`rewrite` maps the rest). */
function emit(src, name, out, rewrite = (s) => s) {
  const js = ts.transpileModule(readFileSync(src, "utf8"), {
    compilerOptions: {
      module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
      verbatimModuleSyntax: false, isolatedModules: true,
    },
    fileName: name,
  }).outputText;
  const fixed = rewrite(js).replace(/(from\s+|import\s*\(\s*|import\s+)(["'])(\.\.?\/[^"']+?)(?<!\.mjs)\2/g,
    "$1$2$3.mjs$2");
  writeFileSync(out, fixed);
}

before(async () => {
  dir = mkdtempSync(join(FRONTEND, ".settings-render-"));
  mkdirSync(join(dir, "core"));
  for (const name of readdirSync(CORE).filter((n) => /\.(ts|tsx)$/.test(n))) {
    emit(join(CORE, name), name, join(dir, "core", name.replace(/\.(ts|tsx)$/, ".mjs")));
  }
  const toCore = (js) => js.replace(/(["'])\.\.\/\.\.\/\.\.\/_kit\/core\//g, "$1./core/");
  emit(join(FRONTEND, "lib", "settingsRegistry.ts"), "settingsRegistry.ts", join(dir, "settingsRegistry.mjs"));
  emit(join(PAGE, "messages.ts"), "messages.ts", join(dir, "messages.mjs"), toCore);
  emit(join(PAGE, "ScopeWarning.tsx"), "ScopeWarning.tsx", join(dir, "ScopeWarning.mjs"),
    (js) => toCore(js).replace(/(["'])@\/lib\/settingsRegistry\1/g, "$1./settingsRegistry$1"));
  W = await import(pathToFileURL(join(dir, "ScopeWarning.mjs")).href);
  I = await import(pathToFileURL(join(dir, "core", "useI18n.mjs")).href);
  L = await import(pathToFileURL(join(dir, "core", "i18n.mjs")).href);
  ({ SETTINGS } = await import(pathToFileURL(join(dir, "messages.mjs")).href));
  React = require("react");
  ({ renderToStaticMarkup: render } = require("react-dom/server"));
});

after(() => {
  if (dir) rmSync(dir, { recursive: true, force: true });
});

const h = (...a) => React.createElement(...a);
const SWITCH = { key: "crm.search.enabled", app: "crm", scope_kind: "platform+tenant", value_type: "bool",
  default_value: false };
const PLATFORM = { kind: "platform", tenantId: "" };
const AGENCY = "00000000-0000-4000-8000-0000000000d1";

test("Platform picked on an agency feature switch: the warning, in English outside a provider", () => {
  const html = render(h(W.PlatformSwitchWarning, { item: SWITCH, scope: PLATFORM }));
  assert.match(html, /data-testid="settings-scope-platform-warning"/);
  assert.match(html, /role="alert"/);
  assert.ok(html.includes(SETTINGS.en.platformSwitchWarn), html);
  assert.match(SETTINGS.en.platformSwitchWarn, /every agency/);
  assert.match(SETTINGS.en.platformSwitchWarn, /the pilot included/);
});

test("One agency picked (or none yet), or another key: no warning", () => {
  for (const scope of [{ kind: "tenant", tenantId: "" }, { kind: "tenant", tenantId: AGENCY }]) {
    assert.equal(render(h(W.PlatformSwitchWarning, { item: SWITCH, scope })), "");
  }
  assert.equal(render(h(W.PlatformSwitchWarning, { item: { ...SWITCH, default_value: true }, scope: PLATFORM })), "");
  assert.equal(render(h(W.PlatformSwitchWarning, { item: { ...SWITCH, scope_kind: "platform" }, scope: PLATFORM })), "");
  assert.equal(render(h(W.PlatformSwitchWarning, { item: { ...SWITCH, app: "cis" }, scope: PLATFORM })), "");
});

test("the warning in Vietnamese inside a provider set to vi; every new string has both languages", () => {
  const store = L.createLangStore({ cookieName: "settings_scope_warning_test" });
  const html = render(h(I.I18nProvider, { store, initialLang: "vi" },
    h(W.PlatformSwitchWarning, { item: SWITCH, scope: PLATFORM })));
  assert.ok(html.includes("kể cả đại lý thí điểm"), html);
  for (const k of ["scopePlatformAgency", "scopeAgency", "scopeAgencyHint", "agencyPick", "pickAgency",
    "agenciesLoading", "agenciesNone", "agencyStateLocked", "agencyStateOffboarding", "platformSwitchWarn"]) {
    assert.equal(typeof SETTINGS.en[k], "string", k);
    assert.ok(SETTINGS.vi[k].trim().length > 0, k);
    assert.notEqual(SETTINGS.vi[k], SETTINGS.en[k], k);
  }
  assert.match(SETTINGS.en.agenciesError("timeout"), /timeout/);
  assert.match(SETTINGS.vi.agenciesError("timeout"), /timeout/);
});
