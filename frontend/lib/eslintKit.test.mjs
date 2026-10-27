// frontend/lib/eslintKit.test.mjs
// AA-994 S1c (root ADR 0016 decision 7): the UI kit conformance rules in app/_kit/core/eslint-kit.mjs — one
// RuleTester suite per rule (valid + invalid TSX, parsed by the TypeScript parser the app lints with), the presets,
// and the frontend's eslint.config.mjs wiring (kit preset at `error` on page code, the kit itself exempt).
// Needs node_modules (eslint, @typescript-eslint/parser). Run: npm run test:unit (CI: Frontend Lint).

import assert from "node:assert/strict";
import { join } from "node:path";
import { after, describe, it, test } from "node:test";
import { fileURLToPath } from "node:url";

import tsParser from "@typescript-eslint/parser";
import { ESLint, RuleTester } from "eslint";

import {
  LEGACY_MODULES, PRESETS, RULES, isModule, kitConfig, kitRules, plugin, resolveImport,
} from "../app/_kit/core/eslint-kit.mjs";

RuleTester.describe = describe;
RuleTester.it = it;
RuleTester.itOnly = it.only;
RuleTester.afterAll = after;

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const PAGE = join(FRONTEND, "app", "admin", "example", "page.tsx");

const tester = new RuleTester({
  languageOptions: {
    parser: tsParser,
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
});
const page = (code, extra = {}) => ({ code, filename: PAGE, ...extra });
const bad = (code, n = 1, extra = {}) => page(code, { errors: Array.from({ length: n }, () => ({ messageId: "kit" })), ...extra });

tester.run("aa-kit/no-raw-main", RULES["no-raw-main"], {
  valid: [
    page(`const P = () => <AdminMain><PageHeader title="x" /></AdminMain>;`),
    page(`const P = () => <PageFrame variant="split" />;`),
    page(`const s = "<main>"; const P = () => <section role="main" />;`),
  ],
  invalid: [
    bad(`const P = () => <main style={{ padding: 28 }}>x</main>;`),
    bad(`const P = () => <div><main /></div>;`),
  ],
});

tester.run("aa-kit/no-raw-table", RULES["no-raw-table"], {
  valid: [page(`const P = () => <DataTable columns={c} data={d} />;`), page(`const P = () => <Table />;`)],
  invalid: [bad(`const P = () => <table><tbody><tr><td>1</td></tr></tbody></table>;`)],
});

tester.run("aa-kit/no-raw-dialog", RULES["no-raw-dialog"], {
  valid: [
    page(`const P = () => <Modal open title="x" />;`),
    page(`const P = () => <div role="region" />;`),
    page(`const P = ({ r }) => <div role={r} />;`),
  ],
  invalid: [
    bad(`const P = () => <div role="dialog" aria-modal="true" />;`),
    bad(`const P = () => <div role={"alertdialog"} />;`),
    bad(`const P = () => <div role={\`dialog\`} />;`),
    bad(`const P = () => <dialog open />;`),
  ],
});

tester.run("aa-kit/no-hex-colour", RULES["no-hex-colour"], {
  valid: [
    page(`const P = () => <div style={{ color: K.accent, border: \`1px solid \${K.line}\` }} />;`),
    page(`const P = () => <a href="#add">Add</a>;`),
    page(`const P = () => <section id="#fed" data-x="#abc" aria-label="#cafe" />;`),
    page(`const label = "Order #1234 is ready";`),
    page(`const t = "Issue #12 and #abc in text";`),
    page(`const v = "rgb(var(--x))";`),
    page(`import x from "#abc"; type T = "#fff";`),
  ],
  invalid: [
    bad(`const RED = "#ff0000";`),
    bad(`const P = () => <div style={{ color: "#fff" }} />;`),
    bad(`const P = () => <div style={{ border: "1px solid #E5E7EB" }} />;`),
    bad(`const P = () => <div style={{ boxShadow: \`0 1px 2px \${a} #0003\` }} />;`),
    bad(`const C = { background: "linear-gradient(#000, #fff)" };`),
    bad(`const shade = "rgba(0, 0, 0, 0.4)";`),
    bad(`const P = () => <div style={{ background: "hsl(210 40% 50%)" }} />;`),
    bad(`const P = () => <div className="bg-[#1F2933] p-2" />;`),
    bad(`const P = () => <svg><path fill="#DB9628" /></svg>;`),
    bad(`const P = () => <input type="color" value="#123456" />;`),
  ],
});

tester.run("aa-kit/no-hand-styled-button", RULES["no-hand-styled-button"], {
  valid: [
    page(`const P = () => <Button variant="ghost" onClick={f}>x</Button>;`),
    page(`const P = () => <button type="button" onClick={f} aria-label="Close">x</button>;`),
  ],
  invalid: [
    bad(`const P = () => <button style={{ padding: 4 }}>x</button>;`),
    bad(`const P = () => <button className="btn">x</button>;`),
  ],
});

tester.run("aa-kit/no-raw-aa-var", RULES["no-raw-aa-var"], {
  valid: [
    page(`const P = () => <div style={{ color: K.muted }} />;`),
    page(`const c = useChartColors({ a: "--aa-gold" });`),
  ],
  invalid: [
    bad(`const P = () => <div style={{ color: "var(--aa-muted)" }} />;`),
    bad(`const b = \`1px solid var( --aa-line)\`;`),
    bad(`const P = () => <div className="text-[var(--aa-ink)]" />;`),
  ],
});

tester.run("aa-kit/no-aa-var-definition", RULES["no-aa-var-definition"], {
  valid: [
    page(`const P = () => <div style={{ ["--bk-gap"]: "4px", color: K.ink }} />;`),
    page(`el.style.setProperty("--tp-hero", "1");`),
    page(`const c = useChartColors({ a: "--aa-gold" }); const s = "color: var(--aa-ink)";`),
  ],
  invalid: [
    bad(`const P = () => <div style={{ "--aa-accent": "red" }} />;`),
    bad(`const P = () => <div style={{ [\`--aa-x\`]: 1 }} />;`),
    bad(`document.documentElement.style.setProperty("--aa-ink", v);`),
    bad(`const css = \`:root { --aa-ink: #000; }\`;`),
  ],
});

tester.run("aa-kit/no-legacy-kit", RULES["no-legacy-kit"], {
  valid: [
    page(`import { Button } from "@/app/_kit";`),
    page(`import { Btn } from "./adminUiNext";`),
    page(`import x from "adminUi";`),
    page(`import { Btn } from "../_components/adminUi";`, { filename: join(FRONTEND, "app", "portal", "x", "page.tsx") }),
  ],
  invalid: [
    bad(`import { Btn } from "@/app/admin/_components/adminUi";`),
    bad(`import { Btn } from "../_components/adminUi";`),
    bad(`import { Card } from "./adminUi";`, 1, { filename: join(FRONTEND, "app", "admin", "_components", "X.tsx") }),
    bad(`export { Btn } from "../_components/adminUi.tsx";`),
    bad(`const m = import("@/app/portal/_components/ui");`),
    bad(`import { PageHeader } from "../_components/ui";`, 1, { filename: join(FRONTEND, "app", "portal", "x", "page.tsx") }),
    bad(`import { Thing } from "./old";`, 1, { options: [{ modules: [{ path: "app/admin/example/old" }] }] }),
    bad(`import { Btn } from "../_components/adminUi";`, 1, { options: [{ root: "/somewhere/else" }] }),
  ],
});

tester.run("aa-kit/no-class-name", RULES["no-class-name"], {
  valid: [page(`const P = () => <div style={{ padding: 4 }} />;`)],
  invalid: [bad(`const P = () => <div className="p-4" />;`)],
});

test("resolveImport maps the @/ alias and relative paths to app-root paths without extension", () => {
  const root = "/r";
  assert.equal(resolveImport("@/app/_kit", "/r/app/x/page.tsx", root), "app/_kit");
  assert.equal(resolveImport("../_components/adminUi.tsx", "/r/app/admin/x/page.tsx", root), "app/admin/_components/adminUi");
  assert.equal(resolveImport("./ui/index", "/r/app/portal/_components/a.tsx", root), "app/portal/_components/ui");
  assert.equal(resolveImport("react", "/r/app/x.tsx", root), null);
  // ESLint started from another folder (an editor at the repo root): still recognised by the path suffix.
  const outside = resolveImport("../_components/adminUi", "/repo/frontend/app/admin/x/page.tsx", "/elsewhere");
  assert.equal(outside, "/repo/frontend/app/admin/_components/adminUi");
  assert.ok(isModule(outside, "app/admin/_components/adminUi"));
  assert.ok(!isModule("/repo/frontend/app/admin/_components/notadminUi", "app/admin/_components/adminUi"));
  assert.deepEqual(LEGACY_MODULES.map((m) => m.path), ["app/admin/_components/adminUi", "app/portal/_components/ui"]);
});

test("presets: inline = rules 1-7, 9 + no-class-name; tailwind = rules 1-7, 9; every rule exists and is error", () => {
  const shared = [
    "no-raw-main", "no-raw-table", "no-raw-dialog", "no-hex-colour", "no-hand-styled-button", "no-raw-aa-var",
    "no-aa-var-definition", "no-legacy-kit",
  ];
  assert.deepEqual(PRESETS.tailwind, shared);
  assert.deepEqual(PRESETS.inline, [...shared, "no-class-name"]);
  for (const n of PRESETS.inline) assert.ok(plugin.rules[n], `rule ${n} is defined`);
  assert.ok(Object.values(kitRules("inline")).every((s) => s === "error"));
  assert.throws(() => kitRules("nope"), /unknown preset/);
  const cfg = kitConfig({ preset: "tailwind", files: ["app/**/*.tsx"], rules: { "aa-kit/no-raw-table": "off" } });
  assert.equal(cfg.rules["aa-kit/no-raw-table"], "off");
  assert.equal(cfg.rules["aa-kit/no-raw-main"], "error");
  assert.equal(cfg.plugins["aa-kit"], plugin);
  assert.throws(() => kitConfig({ preset: "inline", files: [] }), /needs `files`/);
});

test("frontend eslint.config.mjs: kit preset at error on page code, the kit folder exempt", async () => {
  const eslint = new ESLint({ cwd: FRONTEND });
  const pageCfg = await eslint.calculateConfigForFile(join(FRONTEND, "app", "admin", "x", "page.tsx"));
  for (const n of PRESETS.inline) {
    assert.deepEqual(pageCfg.rules[`aa-kit/${n}`], [2], `aa-kit/${n} is error on admin pages`);
  }
  const loginCfg = await eslint.calculateConfigForFile(join(FRONTEND, "app", "login", "LoginForm.tsx"));
  assert.deepEqual(loginCfg.rules["aa-kit/no-raw-main"], [2], "every app/** file, not only admin");
  const kitCfg = await eslint.calculateConfigForFile(join(FRONTEND, "app", "_kit", "AdminShell.tsx"));
  assert.equal(kitCfg.rules["aa-kit/no-raw-main"], undefined, "the kit (shell, DataTable, Modal) is exempt");
  const libCfg = await eslint.calculateConfigForFile(join(FRONTEND, "lib", "contrast.mjs"));
  assert.equal(libCfg.rules["aa-kit/no-hex-colour"], undefined, "lib/ helpers are not page code");
});

test("a raw <main> in a new page fails lint with the kit message (the PR evidence case)", async () => {
  const eslint = new ESLint({ cwd: FRONTEND });
  const [r] = await eslint.lintText(
    `export default function Page() {\n  return <main>hello</main>;\n}\n`,
    { filePath: join(FRONTEND, "app", "admin", "s1c-proof", "page.tsx") },
  );
  const kit = r.messages.filter((m) => m.ruleId === "aa-kit/no-raw-main");
  assert.equal(kit.length, 1);
  assert.equal(kit[0].severity, 2);
  assert.match(kit[0].message, /PageFrame/);
});
