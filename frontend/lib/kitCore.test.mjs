// frontend/lib/kitCore.test.mjs
// AA-994 S1a (root ADR 0016 decisions 1, 3, 4, 5): the UI kit core in app/_kit/core is import-closed, carries the
// header line, has a current MANIFEST.json, and its copy check (core/check-kit-copy.mjs) catches an edited, missing or
// extra file in a consumer copy. Also pins the theme store factory and its restore script to the S252 behaviour, and
// (AA-994 S4a) the money / calendar-day / relative-time formatters upstreamed from AA-Booking (core/format.ts).
// Run: npm run test:unit (CI: Frontend Build runs this file and `check-kit-copy.mjs --self`).

import assert from "node:assert/strict";
import { cpSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  HEADER, MANIFEST, aaDefinitions, buildManifest, closureProblems, copyProblems, cssDefinitionProblems, headerProblems,
  importProblem, importSpecifiers, kitDisableDirectives, kitDisableProblems, main, manifestText, readFileList,
  readSuppressions, selfProblems, sha256, suppressionProblems, suppressionTotals,
} from "../app/_kit/core/check-kit-copy.mjs";
import { createThemeStore, themeRestoreScript } from "../app/_kit/core/theme.ts";
import {
  formatDay, formatMoney, isIsoDay, minorUnits, parseDayInput, relativeParts,
} from "../app/_kit/core/format.ts";

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

// A client-only React hook in a module without "use client" breaks a consumer's Next build as soon as a server
// component imports the kit barrel (AA-994: useMediaQuery.ts did, so the CRM web barrel had to drop it).
const CLIENT_HOOKS =
  /\b(useState|useEffect|useLayoutEffect|useInsertionEffect|useReducer|useRef|useCallback|useMemo|useContext|useId|useTransition|useDeferredValue|useSyncExternalStore|useOptimistic|useActionState|createContext)\b/;

test('every core module that imports a client-only React hook starts with "use client"', () => {
  const missing = [];
  for (const f of readdirSync(CORE).filter((n) => /\.(ts|tsx)$/.test(n))) {
    const text = readFileSync(join(CORE, f), "utf8");
    const reactValueImports = [...text.matchAll(/^import\s+(?!type\b)\{([^}]*)\}\s+from\s+"react";/gm)]
      .map((m) => m[1].split(",").filter((s) => !/^\s*type\s/.test(s)).join(","));
    if (!reactValueImports.some((names) => CLIENT_HOOKS.test(names))) continue;
    // The directive must be the first statement; only the header comment and other comments may precede it.
    const firstStatement = text.split("\n").find((l) => l.trim() && !l.trim().startsWith("//"));
    if (firstStatement?.trim() !== '"use client";') missing.push(f);
  }
  assert.deepEqual(missing, []);
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

test("suppressions: fixing an old violation never buys a new entry; only a deleted/renamed file's entries move", (t) => {
  const d = scratch(t);
  mkdirSync(join(d, "app"));
  for (const f of ["a2.tsx", "b.tsx", "c.tsx"]) writeFileSync(join(d, "app", f), "x");
  const base = readSuppressions(JSON.stringify(supp({ "app/a.tsx": { R: 2 }, "app/b.tsx": { R: 2 } })));
  const check = (head, opts = {}) =>
    suppressionProblems(base, readSuppressions(JSON.stringify(supp(head))), { root: d, ...opts });
  // Trade: b.tsx (still there) drops one, c.tsx gains a new entry; the R total stays 4.
  const trade = check({ "app/a.tsx": { R: 2 }, "app/b.tsx": { R: 1 }, "app/c.tsx": { R: 1 } });
  writeFileSync(join(d, "app", "a.tsx"), "x");
  const tradeWithA = check({ "app/a.tsx": { R: 2 }, "app/b.tsx": { R: 1 }, "app/c.tsx": { R: 1 } });
  assert.match(tradeWithA.join("\n"), /R: 1 new suppressed entries but only 0 came from deleted\/renamed files/);
  assert.equal(tradeWithA.length, 1, tradeWithA.join("\n"));
  rmSync(join(d, "app", "a.tsx"));
  assert.ok(trade.some((p) => /app\/a\.tsx: listed but the file no longer exists/.test(p)), "a.tsx entry is stale");
  // A new rule key on an existing file's entry is a new entry too.
  writeFileSync(join(d, "app", "a.tsx"), "x");
  const base2 = readSuppressions(JSON.stringify(supp({ "app/b.tsx": { R: 2, S: 1 } })));
  const newKey = suppressionProblems(base2, readSuppressions(JSON.stringify(supp({ "app/b.tsx": { R: 2 }, "app/a.tsx": { S: 1 } }))),
    { root: d });
  assert.match(newKey.join("\n"), /S: 1 new suppressed entries but only 0/);
  rmSync(join(d, "app", "a.tsx"));
  // Rename: a.tsx is gone, a2.tsx carries its entries (same or fewer) -> OK; more than a.tsx had -> total grows.
  assert.deepEqual(check({ "app/a2.tsx": { R: 2 }, "app/b.tsx": { R: 2 } }), [], "a.tsx renamed to a2.tsx");
  assert.deepEqual(check({ "app/a2.tsx": { R: 1 }, "app/c.tsx": { R: 1 }, "app/b.tsx": { R: 2 } }), [], "a.tsx split");
  // --base-files: a new entry must be in a file the base commit did not have.
  const baseFiles = readFileList("frontend/app/a.tsx\nfrontend/app/b.tsx\nfrontend/app/c.tsx\n", "frontend/");
  assert.deepEqual([...baseFiles].sort(), ["app/a.tsx", "app/b.tsx", "app/c.tsx"]);
  assert.deepEqual(check({ "app/a2.tsx": { R: 2 }, "app/b.tsx": { R: 2 } }, { baseFiles }), [], "rename to a new path");
  const intoOld = check({ "app/c.tsx": { R: 2 }, "app/b.tsx": { R: 2 } }, { baseFiles });
  assert.match(intoOld.join("\n"), /app\/c\.tsx: new R suppression in a file that already existed at the base/,
    "deleting a.tsx does not buy a new violation in the old c.tsx");
});

test("--no-kit-disable: inline switches of kit rules fail, other rules' disables pass", (t) => {
  const ok = [
    "// eslint-disable-next-line react-hooks/exhaustive-deps -- mentions aa-kit/ in the reason only",
    "/* eslint-disable react-hooks/set-state-in-effect -- fetch on mount */",
    "/** eslint-disable */", "/* eslint-enable */", "/* eslint-env browser */",
    "const s = \"Order #1\"; // a comment about eslint",
  ];
  for (const src of ok) assert.deepEqual(kitDisableDirectives(src), [], src);
  const bad = [
    ["{/* eslint-disable-next-line aa-kit/no-raw-main */}", /names an aa-kit\/ rule/],
    ["x(); // eslint-disable-line aa-kit/no-hex-colour", /names an aa-kit\/ rule/],
    ["// eslint-disable-line react-hooks/x, aa-kit/no-raw-table", /names an aa-kit\/ rule/],
    ["/* eslint-disable */", /bare eslint-disable switches every rule off/],
    ["// eslint-disable-next-line -- no rule named", /bare eslint-disable-next-line/],
    ["/*\n  eslint-disable\n  aa-kit/no-raw-table\n*/", /names an aa-kit\/ rule/],
    ["/* eslint aa-kit/no-raw-main: off */", /inline config comment/],
  ];
  for (const [src, re] of bad) {
    const got = kitDisableDirectives(src);
    assert.equal(got.length, 1, src);
    assert.match(got[0][1], re, src);
  }
  const d = scratch(t);
  mkdirSync(join(d, "admin"), { recursive: true });
  mkdirSync(join(d, "_kit"), { recursive: true });
  writeFileSync(join(d, "admin", "page.tsx"), "const a = 1;\n{/* eslint-disable-next-line aa-kit/no-raw-main */}\n");
  writeFileSync(join(d, "admin", "ok.tsx"), "// eslint-disable-next-line react-hooks/exhaustive-deps\n");
  writeFileSync(join(d, "_kit", "Shell.tsx"), "/* eslint-disable */\n");
  writeFileSync(join(d, "admin", "notes.css"), "/* eslint-disable */\n");
  assert.deepEqual(kitDisableProblems(d, { ignore: ["_kit"] }),
    ["admin/page.tsx:2: eslint-disable-next-line names an aa-kit/ rule"]);
  assert.equal(kitDisableProblems(d).length, 2, "without --ignore the kit folder is scanned too");
  const quiet = (fn) => {
    const [log, err] = [console.log, console.error];
    console.log = console.error = () => {};
    try { return fn(); } finally { [console.log, console.error] = [log, err]; }
  };
  assert.equal(quiet(() => main(["--no-kit-disable", d, "--ignore", "_kit"])), 1);
  rmSync(join(d, "admin", "page.tsx"));
  assert.equal(quiet(() => main(["--no-kit-disable", d, "--ignore", "_kit"])), 0);
  assert.equal(quiet(() => main(["--no-kit-disable", join(d, "nope")])), 2);
  assert.deepEqual(kitDisableProblems(join(FRONTEND, "app"), { ignore: ["_kit"] }), [],
    "CIS page code switches no kit rule off inline");
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
  const list = join(d, "base-files.txt");
  writeFileSync(list, "page.tsx\nother.tsx\n");
  writeFileSync(join(d, "other.tsx"), "x");
  writeFileSync(head, JSON.stringify(supp({ "page.tsx": { R: 1 }, "other.tsx": { R: 1 } })));
  assert.equal(quiet(() => main(["--suppressions", base, head, "--base-files", list])), 1, "new entry in an old file");
  assert.equal(quiet(() => main(["--suppressions", base, head, "--base-files", join(d, "nope.txt")])), 2);
});

test("eslint-kit.mjs is core, in the tokens profile, and the committed suppressions file parses", () => {
  const manifest = buildManifest(CORE);
  assert.ok("eslint-kit.mjs" in manifest.files);
  assert.ok(manifest.profiles.tokens.includes("eslint-kit.mjs"), "TripPlanner gets the rules with the tokens");
  const s = readSuppressions(readFileSync(join(FRONTEND, "eslint-suppressions.json"), "utf8"));
  assert.ok(Object.keys(s).every((f) => !f.startsWith("app/_kit/")), "the kit itself is never suppressed");
});

// ── AA-994 S4a: core/format.ts (upstreamed from AA-Booking Money.tsx / dates.ts / DateText.tsx) ─────────────────

test("formatMoney: amount plus ISO code, the currency's minor units, UI-language digits, '—' when empty", () => {
  assert.equal(formatMoney("1845", "USD"), "1,845.00 USD");
  assert.equal(formatMoney(2390.5, "eur"), "2,390.50 EUR", "the code is upper-cased");
  assert.equal(formatMoney("12500000", "VND"), "12,500,000 VND", "VND has no minor units");
  assert.equal(formatMoney("1.2345", "KWD"), "1.235 KWD", "KWD has three");
  assert.equal(formatMoney("10", null), "10.00", "no code: the number with 2 decimals");
  for (const empty of [null, undefined, "", "abc", Number.NaN]) assert.equal(formatMoney(empty, "USD"), "—");
  assert.equal(formatMoney("1845", "USD", "vi"), "1.845,00 USD");
  assert.equal(minorUnits("usd"), 2, "a bad code falls back to 2");
  assert.equal(minorUnits("JPY"), 0);
});

test("calendar days: real days only, formatted without a time-zone shift, typed day first", () => {
  assert.ok(isIsoDay("2026-10-09"));
  for (const v of ["2026-02-30", "2026-13-01", "9 Oct 2026", "", null, undefined]) assert.ok(!isIsoDay(v), String(v));
  assert.equal(formatDay("2026-10-09"), "9 Oct 2026");
  assert.equal(formatDay("2026-01-01"), "1 Jan 2026", "UTC midnight never becomes the day before");
  assert.match(formatDay("2026-10-09", "vi"), /^9 .*10.* 2026$/);
  assert.equal(formatDay("2026-02-30"), "—", "shaped like a day but not one");
  assert.equal(formatDay(""), "—");
  assert.equal(formatDay("not a date"), "—");
  assert.equal(parseDayInput("28/10/2026"), "2026-10-28");
  assert.equal(parseDayInput("10/11/2026"), "2026-11-10", "never month first");
  assert.equal(parseDayInput("28.10.26"), "2026-10-28");
  assert.equal(parseDayInput("28 Oct 2026"), "2026-10-28");
  assert.equal(parseDayInput("Oct 28, 2026"), "2026-10-28");
  assert.equal(parseDayInput("2026-10-28"), "2026-10-28");
  assert.equal(parseDayInput("  "), "", "empty field");
  assert.equal(parseDayInput("31/11/2026"), null, "a day that does not exist");
  assert.equal(parseDayInput("tomorrow"), null);
});

test("relativeParts: now / minutes / hours / days, null after 7 days or when unreadable", () => {
  const now = Date.UTC(2026, 9, 9, 7, 5);
  const ago = (ms) => new Date(now - ms).toISOString();
  assert.deepEqual(relativeParts(ago(20_000), now), { unit: "now" });
  assert.deepEqual(relativeParts(ago(-60_000), now), { unit: "now" }, "the future reads as now");
  assert.deepEqual(relativeParts(ago(25 * 60_000), now), { unit: "min", n: 25 });
  assert.deepEqual(relativeParts(ago(3 * 3_600_000 + 1), now), { unit: "h", n: 3 });
  assert.deepEqual(relativeParts(new Date(now - 2 * 86_400_000), now), { unit: "d", n: 2 });
  assert.equal(relativeParts(ago(8 * 86_400_000), now), null);
  assert.equal(relativeParts("nonsense", now), null);
});

test("S4a components are core files, outside the tokens profile", () => {
  const manifest = buildManifest(CORE);
  for (const f of ["Section.tsx", "KpiTile.tsx", "FilterBar.tsx", "DateField.tsx", "RowMenu.tsx", "Money.tsx",
    "DateText.tsx", "format.ts", "KitGallery.tsx", "kitGallery.messages.ts"]) {
    assert.ok(f in manifest.files, f);
    assert.ok(!manifest.profiles.tokens.includes(f), `${f} is not a token file`);
  }
});
