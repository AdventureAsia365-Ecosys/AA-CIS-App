// frontend/lib/kitCore.test.mjs
// AA-994 S1a (root ADR 0016 decisions 1, 3, 4, 5): the UI kit core in app/_kit/core is import-closed, carries the
// header line, has a current MANIFEST.json, and its copy check (core/check-kit-copy.mjs) catches an edited, missing or
// extra file in a consumer copy. Also pins the theme store factory and its restore script to the S252 behaviour.
// Run: npm run test:unit (CI: Frontend Build runs this file and `check-kit-copy.mjs --self`).

import assert from "node:assert/strict";
import { cpSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  HEADER, MANIFEST, aaDefinitions, buildManifest, closureProblems, copyProblems, cssDefinitionProblems, headerProblems,
  importProblem, importSpecifiers, main, manifestText, readSuppressions, selfProblems, sha256, suppressionProblems,
  suppressionTotals,
} from "../app/_kit/core/check-kit-copy.mjs";
import { createThemeStore, themeRestoreScript } from "../app/_kit/core/theme.ts";

const FRONTEND = fileURLToPath(new URL("..", import.meta.url));
const CORE = join(FRONTEND, "app", "_kit", "core");

function scratch(t) {
  const d = mkdtempSync(join(tmpdir(), "kit-core-"));
  t.after(() => rmSync(d, { recursive: true, force: true }));
  return d;
}

/** A consumer checkout: <root>/app/_kit/core (a copy of CIS core) + <root>/kit.lock.json. */
function consumer(t, { profile = "full", files } = {}) {
  const root = scratch(t);
  const dest = join(root, "app", "_kit", "core");
  const manifest = buildManifest(CORE);
  const keep = profile === "tokens" ? manifest.profiles.tokens : null;
  const wanted = (src) => !keep || src === CORE || keep.some((f) => src === join(CORE, f));
  cpSync(CORE, dest, { recursive: true, filter: wanted });
  for (const [f, text] of Object.entries(files || {})) writeFileSync(join(dest, f), text);
  const lock = { ref: "test", profile, manifest_sha256: sha256(readFileSync(join(dest, MANIFEST))) };
  writeFileSync(join(root, "kit.lock.json"), JSON.stringify(lock));
  return { root, dest, lockPath: join(root, "kit.lock.json") };
}

// ── canonical copy ─────────────────────────────────────────────────────────────────────────────────────────────

test("the CIS core passes its self-check (manifest current, header lines, import closure)", () => {
  assert.deepEqual(selfProblems(CORE), []);
});

test("MANIFEST.json is current and lists every core file but itself", () => {
  assert.equal(readFileSync(join(CORE, MANIFEST), "utf8"), manifestText(CORE));
  const listed = Object.keys(JSON.parse(readFileSync(join(CORE, MANIFEST), "utf8")).files).sort();
  assert.deepEqual(listed, readdirSync(CORE).filter((f) => f !== MANIFEST).sort());
});

test("core imports nothing outside itself (no ../, no @/ alias, only allowed packages)", () => {
  assert.deepEqual(closureProblems(CORE), []);
});

test("ADR 0016 decision 1: the portable files are in core, the CIS-specific ones stay outside", () => {
  const core = new Set(readdirSync(CORE));
  for (const f of ["tokens.ts", "brand.ts", "primitives.tsx", "DataTable.tsx", "csv.ts", "savedViews.ts", "Drawer.tsx",
    "Modal.tsx", "Toast.tsx", "Providers.tsx", "theme.ts", "useTheme.ts", "useHydrated.ts", "useMediaQuery.ts",
    "sidebarStore.ts", "useSidebar.ts", "PageFrame.tsx", "GuidePanel.tsx", "check-kit-copy.mjs", "tokens.css", MANIFEST]) {
    assert.ok(core.has(f), `${f} is in core`);
  }
  for (const f of ["api.ts", "guideContent.ts", "SessionKeepAlive.tsx", "adminNav.tsx", "useNavSections.ts",
    "useAdminIdentity.ts", "useReviewPendingCount.ts", "AdminShell.tsx", "Sidebar.tsx", "Topbar.tsx", "BottomNav.tsx",
    "CommandPalette.tsx"]) {
    assert.ok(!core.has(f), `${f} stays CIS-local`);
  }
});

const MOVED = new RegExp("(^|/)_brand/tokens$|_kit/(tokens|primitives|DataTable|Modal|Drawer|Toast|Providers|AdminMain|"
  + "GuidePanel|csv|savedViews|useHydrated|useMediaQuery|sidebarStore|useSidebar)$");

test("every page and component still imports the barrel or a CIS-local file, never a moved path", () => {
  const offenders = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, name.name);
      if (name.isDirectory()) { if (name.name !== "node_modules" && name.name !== "core") walk(p); continue; }
      if (!/\.(ts|tsx|mjs)$/.test(name.name)) continue;
      for (const spec of importSpecifiers(readFileSync(p, "utf8"))) {
        if (MOVED.test(spec)) {
          offenders.push(`${p.slice(FRONTEND.length)}: ${spec}`);
        }
      }
    }
  };
  walk(join(FRONTEND, "app"));
  walk(join(FRONTEND, "lib"));
  assert.deepEqual(offenders, []);
});

test("the closure rule: what a core file may and may not import", () => {
  const ok = ["react", "react-dom", "next/link", "next/navigation", "lucide-react", "@tanstack/react-table",
    "@tanstack/react-query", "./tokens", "./brand"];
  for (const s of ok) assert.equal(importProblem(s, "x.tsx", CORE), null, s);
  assert.equal(importProblem("node:fs", "x.mjs", CORE), null);
  for (const s of ["../api", "../../lib/rbac", "@/lib/rbac", "~/x", "axios", "next", "recharts", "node:fs", "./nope",
    "./sub/../../x"]) {
    assert.notEqual(importProblem(s, "x.tsx", CORE), null, s);
  }
});

test("import specifiers: static, type, side-effect, dynamic, require; comments ignored", () => {
  const src = [
    `// import x from "commented";`,
    `/* import y from "block"; */`,
    `import a, { b,`,
    `  c } from "pkg-a";`,
    `import type { T } from './t';`,
    `export { d } from "./d";`,
    `export * from "./e";`,
    `import "./side.css";`,
    `const m = await import("./lazy");`,
    `const r = require("cjs");`,
    `const s = "import z from 'in-a-string'";`,
  ].join("\n");
  assert.deepEqual(importSpecifiers(src).sort(),
    ["./d", "./e", "./lazy", "./side.css", "./t", "cjs", "pkg-a"].sort());
});

test("header line: a core file without it is reported", (t) => {
  const d = scratch(t);
  writeFileSync(join(d, "good.ts"), `// ${HEADER}\nexport const a = 1;\n`);
  writeFileSync(join(d, "bad.ts"), `export const a = 1;\n`);
  writeFileSync(join(d, "MANIFEST.json"), JSON.stringify({ _header: HEADER }));
  assert.deepEqual(headerProblems(d), ["bad.ts: line 1 is not the kit core header line"]);
});

test("self-check fails on a stale manifest and on an import that leaves core", (t) => {
  const d = scratch(t);
  cpSync(CORE, d, { recursive: true });
  writeFileSync(join(d, "extra.ts"), `// ${HEADER}\nimport { apiGet } from "../api";\nexport const x = apiGet;\n`);
  const problems = selfProblems(d);
  assert.ok(problems.some((p) => p.startsWith(`${MANIFEST} is stale`)), problems.join("\n"));
  assert.ok(problems.some((p) => p.startsWith("extra.ts: ") && p.includes("../api")), problems.join("\n"));
});

// ── consumer copy ──────────────────────────────────────────────────────────────────────────────────────────────

test("an exact full copy passes, with its lock found above the core folder", (t) => {
  const c = consumer(t);
  assert.deepEqual(copyProblems(c.dest, { lockPath: c.lockPath }), []);
  assert.equal(main(["--dir", c.dest]), 0);
});

test("an edited, missing or extra file in a copy fails", (t) => {
  const edited = consumer(t, { files: { "Modal.tsx": "// edited in the copy\n" } });
  assert.match(copyProblems(edited.dest, { lockPath: edited.lockPath }).join("\n"), /Modal\.tsx: differs/);

  const missing = consumer(t);
  rmSync(join(missing.dest, "Toast.tsx"));
  assert.match(copyProblems(missing.dest, { lockPath: missing.lockPath }).join("\n"), /Toast\.tsx: missing/);

  const extra = consumer(t, { files: { "Local.tsx": "export {};\n" } });
  assert.match(copyProblems(extra.dest, { lockPath: extra.lockPath }).join("\n"), /Local\.tsx: extra file/);
  assert.equal(main(["--dir", extra.dest]), 1);
});

test("a regenerated manifest in a copy does not match the lock", (t) => {
  const c = consumer(t, { files: { "Modal.tsx": "// edited in the copy\n" } });
  writeFileSync(join(c.dest, MANIFEST), manifestText(c.dest)); // the copy's files now match its own manifest...
  assert.match(copyProblems(c.dest, { lockPath: c.lockPath }).join("\n"), /manifest_sha256/); // ...but not the lock
});

test("the tokens profile copy holds only the token files and passes", (t) => {
  const c = consumer(t, { profile: "tokens" });
  assert.deepEqual(readdirSync(c.dest).sort(), [...buildManifest(CORE).profiles.tokens].sort());
  assert.deepEqual(copyProblems(c.dest, { lockPath: c.lockPath }), []);
  // ...and the same folder checked as a full copy misses the rest.
  assert.match(copyProblems(c.dest, { profile: "full", lockPath: c.lockPath }).join("\n"), /DataTable\.tsx: missing/);
});

test("usage errors exit 2", () => {
  assert.equal(main(["--nope"]), 2);
  assert.equal(main(["--dir"]), 2);
  assert.equal(main(["--css"]), 2);
  assert.equal(main(["--css", join(FRONTEND, "no-such-folder")]), 2);
});

// ── tokens.css (AA-994 S1b, ADR 0016 decision 4) ────────────────────────────────────────────────────────────

test("tokens.css is in core, in the tokens profile, and globals.css imports it and defines no --aa-* itself", () => {
  const manifest = JSON.parse(readFileSync(join(CORE, MANIFEST), "utf8"));
  assert.ok(manifest.files["tokens.css"], "tokens.css is in MANIFEST.json");
  assert.ok(manifest.profiles.tokens.includes("tokens.css"), "tokens.css is in the tokens profile");
  const globals = readFileSync(join(FRONTEND, "app", "globals.css"), "utf8");
  assert.match(globals, /^@import "tailwindcss";\n(?:\/\*[\s\S]*?\*\/\n)?@import "\.\/_kit\/core\/tokens\.css";$/m);
  assert.deepEqual(aaDefinitions(globals), []);
  assert.ok(aaDefinitions(readFileSync(join(CORE, "tokens.css"), "utf8")).length > 150);
});

test("no --aa-* definition outside core/tokens.css anywhere in the CIS frontend", () => {
  assert.deepEqual(cssDefinitionProblems(FRONTEND, join(CORE, "tokens.css")), []);
  assert.equal(main(["--css", FRONTEND]), 0);
});

test("the CSS check finds definitions, skips comments, var() reads, node_modules and the core tokens file", (t) => {
  const root = scratch(t);
  const core = join(root, "ui", "core");
  mkdirSync(core, { recursive: true });
  mkdirSync(join(root, "node_modules", "pkg"), { recursive: true });
  mkdirSync(join(root, "app"), { recursive: true });
  writeFileSync(join(core, "tokens.css"), ":root { --aa-ink: #000; }\n");
  writeFileSync(join(root, "node_modules", "pkg", "x.css"), ":root { --aa-ink: #111; }\n");
  writeFileSync(join(root, "app", "ok.css"), [
    "/* --aa-ink: #222; in a comment */",
    ".a { color: var(--aa-ink); background: var(--aa-bg, #fff); }",
    ":root { --tp-band: #1F2933; --bk-font-script: x; }",
  ].join("\n"));
  writeFileSync(join(root, "app", "bad.css"), ":root {\n  --tp-x: 1px;\n  --aa-muted : #6B7380;\n}\n");
  assert.deepEqual(cssDefinitionProblems(root, join(core, "tokens.css")), ["app/bad.css:3: defines --aa-muted"]);
  assert.equal(main(["--css", root, "--dir", core]), 1);
  rmSync(join(root, "app", "bad.css"));
  assert.equal(main(["--css", root, "--dir", core]), 0);
});

// ── theme store (ADR 0016 decision 4) ────────────────────────────────────────────────────────────────────────

/** Run a restore script against a fake browser; returns the data-theme it set. */
function runRestore(script, { stored, prefersDark = false, throws = false }) {
  const attrs = {};
  const localStorage = { getItem: (k) => { if (throws) throw new Error("blocked"); return stored?.[k] ?? null; } };
  const window = { matchMedia: (q) => ({ matches: prefersDark && q === "(prefers-color-scheme: dark)" }) };
  const document = { documentElement: { setAttribute: (k, v) => { attrs[k] = v; } } };
  new Function("localStorage", "window", "document", script)(localStorage, window, document);
  return attrs["data-theme"];
}

test("the restore script reads only its own key and resolves like the S252 inline script", () => {
  const s = themeRestoreScript("cis_theme");
  assert.equal(runRestore(s, { stored: { cis_theme: "dark" } }), "dark");
  assert.equal(runRestore(s, { stored: { cis_theme: "light" }, prefersDark: true }), "light");
  assert.equal(runRestore(s, { stored: {}, prefersDark: true }), "dark");
  assert.equal(runRestore(s, { stored: { cis_theme: "system" } }), "light");
  assert.equal(runRestore(s, { stored: { bk_theme: "dark" } }), "light");
  assert.equal(runRestore(s, { throws: true }), "light");
  const bk = themeRestoreScript("bk_theme");
  assert.equal(runRestore(bk, { stored: { bk_theme: "dark", cis_theme: "light" } }), "dark");
});

test("createThemeStore keeps the key, refuses an unsafe key, and is SSR-safe", () => {
  const store = createThemeStore({ storageKey: "cis_theme" });
  assert.equal(store.storageKey, "cis_theme");
  assert.equal(store.restoreScript, themeRestoreScript("cis_theme"));
  assert.equal(store.readChoice(), "system"); // no window in node
  assert.equal(store.getServerSnapshot(), "system");
  assert.equal(store.resolveTheme("dark"), "dark");
  assert.equal(store.resolveTheme("system"), "light");
  store.setChoice("dark"); // no-op without window
  assert.throws(() => createThemeStore({ storageKey: "x'); alert(1); ('" }), /storage key/);
  assert.throws(() => createThemeStore({ storageKey: "" }), /storage key/);
});

test("the CIS binding uses the cis_theme key", () => {
  const src = readFileSync(join(FRONTEND, "app", "_kit", "theme.ts"), "utf8");
  assert.match(src, /THEME_KEY = "cis_theme"/);
  assert.match(src, /createThemeStore\(\{ storageKey: THEME_KEY \}\)/);
  assert.match(readFileSync(join(FRONTEND, "app", "layout.tsx"), "utf8"), /__html: themeStore\.restoreScript/);
});

// ---- AA-994 S1c: the ESLint suppressions file only shrinks (plan guard G11) --------------------------------------

const supp = (o) => Object.fromEntries(Object.entries(o).map(([f, rules]) =>
  [f, Object.fromEntries(Object.entries(rules).map(([r, count]) => [r, { count }]))]));

test("suppressions: fewer, equal or moved entries pass; a grown entry, a grown rule total or a gone file fail", (t) => {
  const base = readSuppressions(JSON.stringify(supp({ "app/a.tsx": { R: 3, S: 1 }, "app/b.tsx": { R: 2 } })));
  assert.deepEqual(suppressionTotals(base), { R: 5, S: 1 });
  const ok = (head) => suppressionProblems(base, readSuppressions(JSON.stringify(supp(head))));
  assert.deepEqual(ok({ "app/a.tsx": { R: 3, S: 1 }, "app/b.tsx": { R: 2 } }), [], "unchanged");
  assert.deepEqual(ok({ "app/a.tsx": { R: 1 } }), [], "shrunk");
  assert.deepEqual(ok({ "app/a.tsx": { R: 3, S: 1 }, "app/b2.tsx": { R: 2 } }), [], "b.tsx renamed to b2.tsx");
  assert.match(ok({ "app/a.tsx": { R: 4, S: 1 }, "app/b.tsx": { R: 1 } }).join("\n"), /app\/a\.tsx: R suppressed 3 -> 4/);
  assert.match(ok({ "app/a.tsx": { R: 3, S: 1 }, "app/b.tsx": { R: 2 }, "app/new.tsx": { R: 1 } }).join("\n"),
    /R: 5 -> 6 suppressed in total/, "a new file with new suppressions grows the total");
  assert.match(ok({ "app/a.tsx": { R: 3, S: 1, T: 1 } }).join("\n"), /T: 0 -> 1/, "a new rule");

  const d = scratch(t);
  mkdirSync(join(d, "app"));
  writeFileSync(join(d, "app", "a.tsx"), "x");
  const gone = suppressionProblems(base, readSuppressions(JSON.stringify(supp({ "app/a.tsx": { R: 1 }, "app/b.tsx": { R: 1 } }))),
    { root: d });
  assert.equal(gone.length, 1);
  assert.match(gone[0], /app\/b\.tsx: listed but the file no longer exists/);
  assert.throws(() => readSuppressions("[]"), /not an object/);
  assert.throws(() => readSuppressions(JSON.stringify({ "a.tsx": { R: { count: -1 } } })), /integer count/);
});

test("--suppressions CLI: exit 0 when it shrinks, 1 when it grows, 2 without a base file", (t) => {
  const d = scratch(t);
  writeFileSync(join(d, "page.tsx"), "x");
  const base = join(d, "base.json");
  const head = join(d, "eslint-suppressions.json");
  writeFileSync(base, JSON.stringify(supp({ "page.tsx": { R: 2 } })));
  const quiet = (fn) => {
    const [log, err] = [console.log, console.error];
    console.log = console.error = () => {};
    try { return fn(); } finally { [console.log, console.error] = [log, err]; }
  };
  writeFileSync(head, JSON.stringify(supp({ "page.tsx": { R: 1 } })));
  assert.equal(quiet(() => main(["--suppressions", base, head])), 0);
  writeFileSync(head, JSON.stringify(supp({ "page.tsx": { R: 3 } })));
  assert.equal(quiet(() => main(["--suppressions", base, head])), 1);
  assert.equal(quiet(() => main(["--suppressions", join(d, "nope.json"), head])), 2);
  assert.equal(quiet(() => main(["--suppressions", base])), 2);
});

test("eslint-kit.mjs is core, in the tokens profile, and the committed suppressions file parses", () => {
  const manifest = buildManifest(CORE);
  assert.ok("eslint-kit.mjs" in manifest.files);
  assert.ok(manifest.profiles.tokens.includes("eslint-kit.mjs"), "TripPlanner gets the rules with the tokens");
  const s = readSuppressions(readFileSync(join(FRONTEND, "eslint-suppressions.json"), "utf8"));
  assert.ok(Object.keys(s).every((f) => !f.startsWith("app/_kit/")), "the kit itself is never suppressed");
});
