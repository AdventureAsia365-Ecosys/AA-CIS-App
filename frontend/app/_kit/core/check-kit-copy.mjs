// UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked byte for byte (kit.lock.json).
// app/_kit/core/check-kit-copy.mjs — integrity check of the UI kit core (AA-994 S1a, root ADR 0016 decision 5).
// Dependency-free Node (>= 18) script, vendored with the core into every consumer app.
//
// Usage:
//   node <core>/check-kit-copy.mjs            consumer copy: every file the profile lists is present and has the sha256
//                                             of MANIFEST.json; no extra file; MANIFEST.json matches kit.lock.json
//   node <core>/check-kit-copy.mjs --self     canonical copy (AA-CIS-App): MANIFEST.json is current, every file has
//                                             the header line, and core imports nothing outside itself
//   node <core>/check-kit-copy.mjs --css <app-root>
//                                             no CSS file under <app-root> defines an `--aa-*` variable, except this
//                                             core's tokens.css (ADR 0016 decisions 3c, 4; AA-994 S1b)
//   node <core>/check-kit-copy.mjs --suppressions <base.json> <head.json>
//                                             the ESLint bulk-suppressions file only shrinks (ADR 0016 decision 7,
//                                             AA-994 S1c, plan guard G11): no entry (file + rule) grows against the
//                                             base branch's file, no rule's total grows (an entry may move to a new
//                                             path with a rename), and no entry names a file that no longer exists
//                                             (paths are relative to <head.json>'s folder)
//   options: --dir <path> (default: this script's folder), --profile full|tokens (default: kit.lock.json's, else full),
//            --lock <path> (default: the nearest kit.lock.json in up to 4 parent folders)
// Exit codes: 0 fine, 1 problems found (listed on stderr), 2 usage error.
//
// The functions are exported for the CIS manifest generator (frontend/scripts/kit-manifest.mjs) and unit tests
// (frontend/lib/kitCore.test.mjs); the script only runs its CLI when executed directly.

import { createHash } from "node:crypto";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

export const HEADER =
  "UI kit core (root ADR 0016). Canonical: AA-CIS-App frontend/app/_kit/core. Edit only there; copies are checked " +
  "byte for byte (kit.lock.json).";
export const CANONICAL = { repo: "AdventureAsia365-Ecosys/AA-CIS-App", path: "frontend/app/_kit/core" };
export const MANIFEST = "MANIFEST.json";
/** ADR 0016 decision 2: the files of the `tokens` profile (TripPlanner), when they exist in core. */
export const TOKENS_PROFILE = [
  "tokens.css", "tokens.ts", "brand.ts", "theme.ts", "check-kit-copy.mjs", "eslint-kit.mjs", MANIFEST,
];
/** ADR 0016 decision 1: the only packages a core file may import (plus `next/*` and, in .mjs scripts, `node:*`). */
export const ALLOWED_PACKAGES = [
  "react", "react-dom", "next", "lucide-react", "@tanstack/react-table", "@tanstack/react-query",
];
const FIX_COPY = "edit AA-CIS-App frontend/app/_kit/core, then sync (tools/kit/kit-sync.sh); never edit the copy";
const FIX_SELF = "run `node scripts/kit-manifest.mjs` in frontend/ and commit MANIFEST.json";
const CODE_EXT = /\.(ts|tsx|mts|mjs|js|jsx)$/;
/** Folders the CSS check never enters (dependencies, build output, test reports). */
const CSS_SKIP_DIRS = new Set([
  "node_modules", ".next", ".vercel", ".git", "out", "build", "dist", "coverage", "test-results", "playwright-report",
]);
/** An `--aa-*` custom property definition (`--aa-name:`); a `var(--aa-name)` read never has the colon. */
const AA_DEFINITION = /(?<![\w-])--aa-[\w-]+\s*:/g;
const FIX_CSS = "define `--aa-*` values only in AA-CIS-App frontend/app/_kit/core/tokens.css; app variables use the " +
  "app's own prefix (--bk-*, --tp-*, --crm-*)";

export function sha256(buf) {
  return createHash("sha256").update(buf).digest("hex");
}

/** Every file under dir, as sorted "/"-separated relative paths. */
export function listFiles(dir) {
  const out = [];
  const walk = (d) => {
    for (const name of readdirSync(d)) {
      const p = join(d, name);
      if (statSync(p).isDirectory()) walk(p);
      else out.push(relative(dir, p).split(sep).join("/"));
    }
  };
  walk(dir);
  return out.sort();
}

/** A manifest entry's hash: `{ "sha256": "…" }`, or a bare string (the format kit-sync.sh also reads). */
function fileHash(entry) {
  return entry && typeof entry === "object" ? entry.sha256 : entry;
}

/** The manifest of a core folder: header, canonical source, profiles and the sha256 of every file but itself. */
export function buildManifest(dir) {
  const files = listFiles(dir).filter((f) => f !== MANIFEST);
  const present = new Set([...files, MANIFEST]);
  return {
    _header: HEADER,
    adr: "root ADR 0016",
    canonical: CANONICAL,
    generator: "frontend/scripts/kit-manifest.mjs",
    profiles: { tokens: TOKENS_PROFILE.filter((f) => present.has(f)) },
    // { sha256 } objects, not bare strings: a bare hex next to "tokens.ts" reads as an API key to gitleaks.
    files: Object.fromEntries(files.map((f) => [f, { sha256: sha256(readFileSync(join(dir, f))) }])),
  };
}

/** MANIFEST.json as written to disk (stable: sorted keys come from listFiles, 2-space indent, final newline). */
export function manifestText(dir) {
  return JSON.stringify(buildManifest(dir), null, 2) + "\n";
}

/** Source with comments removed (same length: comments become spaces). With `maskStrings`, the contents of string
 *  and template literals become spaces too, so text inside a string never looks like code. */
export function stripComments(src, { maskStrings = false } = {}) {
  let out = "";
  let i = 0;
  const blank = (s) => s.replace(/[^\n]/g, " ");
  while (i < src.length) {
    const c = src[i];
    const n = src[i + 1];
    if (c === "/" && n === "/") {
      const end = src.indexOf("\n", i);
      const stop = end < 0 ? src.length : end;
      out += blank(src.slice(i, stop));
      i = stop;
    } else if (c === "/" && n === "*") {
      const end = src.indexOf("*/", i + 2);
      const stop = end < 0 ? src.length : end + 2;
      out += blank(src.slice(i, stop));
      i = stop;
    } else if (c === '"' || c === "'" || c === "`") {
      let j = i + 1;
      while (j < src.length && src[j] !== c) j += src[j] === "\\" ? 2 : 1;
      const body = src.slice(i + 1, Math.min(j, src.length));
      out += c + (maskStrings ? blank(body) : body) + (j < src.length ? c : "");
      i = j + 1;
    } else {
      out += c;
      i++;
    }
  }
  return out;
}

/** The module specifiers a source file imports or re-exports (static, side-effect, dynamic and require). The
 *  patterns run on the masked source (strings blanked) and the specifier is read back from the same offsets. */
export function importSpecifiers(src) {
  const code = stripComments(src);
  const masked = stripComments(src, { maskStrings: true });
  const out = [];
  const patterns = [
    /(?:^|[^\w$.])(?:import|export)\s+(?:type\s+)?[^'"`;()]*?\bfrom\s*(["'])[^"'\n]*\1/dg,
    /(?:^|[^\w$.])import\s*(["'])[^"'\n]*\1/dg,
    /(?:^|[^\w$.])import\s*\(\s*(["'])[^"'\n]*\1\s*\)/dg,
    /(?:^|[^\w$.])require\s*\(\s*(["'])[^"'\n]*\1\s*\)/dg,
  ];
  for (const re of patterns) {
    for (const m of masked.matchAll(re)) {
      const open = m.indices[1][0];
      const close = masked.indexOf(m[1], open + 1);
      out.push(code.slice(open + 1, close));
    }
  }
  return out;
}

function packageName(spec) {
  const parts = spec.split("/");
  return spec.startsWith("@") ? parts.slice(0, 2).join("/") : parts[0];
}

/** Why one import of a core file breaks the closed set, or null when it is allowed. */
export function importProblem(spec, file, dir) {
  if (spec.startsWith("./")) {
    if (spec.split("/").includes("..")) return `"${spec}" climbs out of core`;
    const base = join(dir, dirname(file), spec);
    const cands = [base, ...[".ts", ".tsx", ".mjs", ".js"].map((e) => base + e),
      ...["index.ts", "index.tsx"].map((e) => join(base, e))];
    return cands.some((p) => existsSync(p) && statSync(p).isFile()) ? null : `"${spec}" is not a file in core`;
  }
  if (spec.startsWith(".") || spec.startsWith("/")) return `"${spec}" points outside core (only ./ imports)`;
  if (spec.startsWith("@/") || spec.startsWith("~/")) return `"${spec}" uses an app path alias`;
  if (spec.startsWith("node:")) return file.endsWith(".mjs") ? null : `"${spec}" (node builtins only in .mjs scripts)`;
  const pkg = packageName(spec);
  if (pkg === "next" ? spec.startsWith("next/") : ALLOWED_PACKAGES.includes(pkg)) return null;
  return `"${spec}" is not an allowed package (${ALLOWED_PACKAGES.join(", ")}, next/*)`;
}

/** Import-closure problems of a core folder: every code file imports only ./ files of core and allowed packages. */
export function closureProblems(dir) {
  const out = [];
  for (const f of listFiles(dir).filter((x) => CODE_EXT.test(x))) {
    for (const spec of importSpecifiers(readFileSync(join(dir, f), "utf8"))) {
      const why = importProblem(spec, f, dir);
      if (why) out.push(`${f}: ${why}`);
    }
  }
  return out;
}

/** The tokens profile must be closed too: its code files import only each other (TripPlanner gets only these). */
export function profileClosureProblems(dir, profile) {
  const set = new Set(profile);
  const out = [];
  for (const f of profile.filter((x) => CODE_EXT.test(x) && existsSync(join(dir, x)))) {
    for (const spec of importSpecifiers(readFileSync(join(dir, f), "utf8"))) {
      if (!spec.startsWith("./")) continue;
      const names = [spec, ...[".ts", ".tsx", ".mjs"].map((e) => spec + e)].map((s) => s.slice(2));
      const target = names.find((s) => set.has(s));
      if (!target) out.push(`${f}: tokens profile file imports "${spec}", which is not in the tokens profile`);
    }
  }
  return out;
}

/** Files of a core folder without the header line (line 1 of code/CSS files, `_header` of JSON). */
export function headerProblems(dir) {
  const out = [];
  for (const f of listFiles(dir)) {
    const text = readFileSync(join(dir, f), "utf8");
    if (f.endsWith(".json")) {
      let ok = false;
      try { ok = JSON.parse(text)._header === HEADER; } catch { /* reported below */ }
      if (!ok) out.push(`${f}: no "_header" with the kit core header line`);
    } else if (f.endsWith(".css")) {
      if (text.split("\n", 1)[0] !== `/* ${HEADER} */`) out.push(`${f}: line 1 is not the kit core header line`);
    } else if (CODE_EXT.test(f)) {
      if (text.split("\n", 1)[0] !== `// ${HEADER}`) out.push(`${f}: line 1 is not the kit core header line`);
    } else {
      out.push(`${f}: unexpected file type in core`);
    }
  }
  return out;
}

/** CIS self-check: manifest current, headers, closure (core and the tokens profile). */
export function selfProblems(dir) {
  const out = [];
  const path = join(dir, MANIFEST);
  if (!existsSync(path)) out.push(`${MANIFEST} is missing; ${FIX_SELF}`);
  else if (readFileSync(path, "utf8") !== manifestText(dir)) out.push(`${MANIFEST} is stale; ${FIX_SELF}`);
  out.push(...headerProblems(dir), ...closureProblems(dir));
  out.push(...profileClosureProblems(dir, buildManifest(dir).profiles.tokens));
  return out;
}

/** CSS with its comments blanked (same length, newlines kept, so offsets still give line numbers). */
export function stripCssComments(css) {
  return css.replace(/\/\*[\s\S]*?(?:\*\/|$)/g, (m) => m.replace(/[^\n]/g, " "));
}

/** The `--aa-*` definitions in a CSS text, as [line, name] pairs (comments ignored). */
export function aaDefinitions(css) {
  const code = stripCssComments(css);
  return [...code.matchAll(AA_DEFINITION)].map((m) => [
    code.slice(0, m.index).split("\n").length, m[0].replace(/\s*:$/, ""),
  ]);
}

/** ADR 0016 decisions 3c and 4 (G10 of the AA-994 plan): every `.css` file under root, except the core tokens file,
 *  defines no `--aa-*` variable. One problem per definition, as "path:line: …". */
export function cssDefinitionProblems(root, tokensPath) {
  const allowed = resolve(tokensPath);
  const out = [];
  const walk = (d) => {
    for (const ent of readdirSync(d, { withFileTypes: true })) {
      const p = join(d, ent.name);
      if (ent.isDirectory()) {
        if (!CSS_SKIP_DIRS.has(ent.name)) walk(p);
      } else if (ent.name.endsWith(".css") && resolve(p) !== allowed) {
        const rel = relative(root, p).split(sep).join("/");
        for (const [line, name] of aaDefinitions(readFileSync(p, "utf8"))) {
          out.push(`${rel}:${line}: defines ${name}`);
        }
      }
    }
  };
  walk(resolve(root));
  return out;
}

/** The nearest kit.lock.json above a consumer core folder (portal/app/_kit/core → portal/kit.lock.json). */
export function findLock(dir) {
  let d = resolve(dir);
  for (let i = 0; i < 4; i++) {
    d = dirname(d);
    const p = join(d, "kit.lock.json");
    if (existsSync(p)) return p;
  }
  return null;
}

/** Consumer check: the copy in dir equals its MANIFEST.json for the profile, and the lock names this manifest. */
export function copyProblems(dir, { profile, lockPath } = {}) {
  const out = [];
  const mpath = join(dir, MANIFEST);
  if (!existsSync(mpath)) return [`${MANIFEST} is missing in ${dir}; ${FIX_COPY}`];
  let manifest;
  try {
    manifest = JSON.parse(readFileSync(mpath, "utf8"));
  } catch (e) {
    return [`${MANIFEST} is not JSON: ${e.message}`];
  }
  const mf = manifest && manifest.files;
  const files = mf && typeof mf === "object" && !Array.isArray(mf) ? mf : null;
  if (!files) return [`${MANIFEST} has no "files" map; ${FIX_COPY}`];

  let lock = null;
  if (lockPath) {
    try {
      lock = JSON.parse(readFileSync(lockPath, "utf8"));
    } catch (e) {
      out.push(`${lockPath} is not JSON: ${e.message}`);
    }
  }
  const prof = profile || (lock && lock.profile) || "full";
  if (prof !== "full" && prof !== "tokens") return [...out, `unknown profile "${prof}" (full, tokens)`];
  if (lock && lock.manifest_sha256 && lock.manifest_sha256 !== sha256(readFileSync(mpath))) {
    out.push(`${MANIFEST} is not the one ${lockPath} records (manifest_sha256); ${FIX_COPY}`);
  }

  const listed = prof === "tokens"
    ? ((manifest.profiles && manifest.profiles.tokens) || TOKENS_PROFILE).filter((f) => f !== MANIFEST)
    : Object.keys(files);
  const expected = new Set([...listed, MANIFEST]);
  for (const f of listed) {
    const p = join(dir, f);
    if (!(f in files)) out.push(`${f}: in the ${prof} profile but not in ${MANIFEST}`);
    else if (!existsSync(p)) out.push(`${f}: missing; ${FIX_COPY}`);
    else if (sha256(readFileSync(p)) !== fileHash(files[f])) out.push(`${f}: differs from ${MANIFEST}; ${FIX_COPY}`);
  }
  for (const f of listFiles(dir)) if (!expected.has(f)) out.push(`${f}: extra file in the kit core copy; ${FIX_COPY}`);
  return out;
}

/** An ESLint suppressions file as { file: { rule: count } }; throws on a shape ESLint would not write. */
export function readSuppressions(text, label = "suppressions") {
  let data;
  try { data = JSON.parse(text); } catch (e) { throw new Error(`${label} is not JSON: ${e.message}`); }
  if (!data || typeof data !== "object" || Array.isArray(data)) throw new Error(`${label} is not an object`);
  const out = {};
  for (const [file, rules] of Object.entries(data)) {
    if (!rules || typeof rules !== "object" || Array.isArray(rules)) throw new Error(`${label}: "${file}" is not a rule map`);
    out[file] = {};
    for (const [rule, entry] of Object.entries(rules)) {
      const n = entry && entry.count;
      if (!Number.isInteger(n) || n < 0) throw new Error(`${label}: "${file}" ${rule} has no integer count`);
      out[file][rule] = n;
    }
  }
  return out;
}

/** Suppressed violations per rule, summed over files. */
export function suppressionTotals(supp) {
  const t = {};
  for (const rules of Object.values(supp)) for (const [rule, n] of Object.entries(rules)) t[rule] = (t[rule] || 0) + n;
  return t;
}

const FIX_SUPP = "fix the new violation instead of suppressing it (never `eslint --suppress-all` for new code); " +
  "after fixing old ones run `npx eslint --prune-suppressions` and commit eslint-suppressions.json";

/** Plan guard G11: what grew in head against base, plus entries for files that are gone (root = head's folder). */
export function suppressionProblems(base, head, { root } = {}) {
  const out = [];
  for (const [file, rules] of Object.entries(head)) {
    for (const [rule, n] of Object.entries(rules)) {
      const was = base[file] && base[file][rule];
      if (was !== undefined && n > was) out.push(`${file}: ${rule} suppressed ${was} -> ${n}`);
    }
    if (root && !existsSync(join(root, file))) {
      out.push(`${file}: listed but the file no longer exists (eslint --prune-suppressions only sees linted files; ` +
        "delete the entry, or move it with a rename)");
    }
  }
  const bt = suppressionTotals(base);
  const ht = suppressionTotals(head);
  for (const rule of Object.keys(ht).sort()) {
    if (ht[rule] > (bt[rule] || 0)) out.push(`${rule}: ${bt[rule] || 0} -> ${ht[rule]} suppressed in total`);
  }
  return out;
}

function suppressionMain(basePath, headPath) {
  if (!existsSync(basePath)) {
    console.error(`check-kit-copy: --suppressions base file ${basePath} does not exist`);
    return 2;
  }
  let base, head;
  try {
    base = readSuppressions(readFileSync(basePath, "utf8"), basePath);
    head = existsSync(headPath) ? readSuppressions(readFileSync(headPath, "utf8"), headPath) : {};
  } catch (e) {
    console.error(`check-kit-copy: ${e.message}`);
    return 1;
  }
  const sum = (s) => Object.values(suppressionTotals(s)).reduce((a, b) => a + b, 0);
  const problems = suppressionProblems(base, head, { root: dirname(resolve(headPath)) });
  if (problems.length) {
    console.error(`check-kit-copy: ESLint suppressions grew or are stale (${headPath} vs ${basePath}): ` +
      `${problems.length} problem(s)`);
    for (const p of problems) console.error(`  - ${p}`);
    console.error(`  fix: ${FIX_SUPP}`);
    return 1;
  }
  console.log(`check-kit-copy: ESLint suppressions did not grow: ${sum(base)} -> ${sum(head)} suppressed violations ` +
    `in ${Object.keys(head).length} file(s): OK`);
  return 0;
}

function parseArgs(argv) {
  const opts = {
    supp: null, self: false, css: null, dir: dirname(fileURLToPath(import.meta.url)), profile: null, lock: undefined,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const val = () => {
      if (i + 1 >= argv.length) throw new Error(`${a} needs a value`);
      return argv[++i];
    };
    if (a === "--self") opts.self = true;
    else if (a === "--css") opts.css = resolve(val());
    else if (a === "--suppressions") opts.supp = [resolve(val()), resolve(val())];
    else if (a === "--dir") opts.dir = resolve(val());
    else if (a === "--profile") opts.profile = val();
    else if (a === "--lock") opts.lock = resolve(val());
    else throw new Error(`unknown argument ${a}`);
  }
  return opts;
}

export function main(argv) {
  let opts;
  try { opts = parseArgs(argv); } catch (e) {
    console.error(`check-kit-copy: ${e.message}`);
    return 2;
  }
  if (opts.supp) return suppressionMain(...opts.supp);
  if (opts.css) {
    if (!existsSync(opts.css) || !statSync(opts.css).isDirectory()) {
      console.error(`check-kit-copy: --css ${opts.css} is not a folder`);
      return 2;
    }
    const problems = cssDefinitionProblems(opts.css, join(opts.dir, "tokens.css"));
    if (problems.length) {
      console.error(`check-kit-copy: --aa-* definitions outside the kit core under ${opts.css}: ${problems.length}`);
      for (const p of problems) console.error(`  - ${p}`);
      console.error(`  fix: ${FIX_CSS}`);
      return 1;
    }
    console.log(`check-kit-copy: no --aa-* definition outside ${join(opts.dir, "tokens.css")} under ${opts.css}: OK`);
    return 0;
  }
  const lockPath = opts.lock === undefined ? findLock(opts.dir) : opts.lock;
  const problems = opts.self ? selfProblems(opts.dir) : copyProblems(opts.dir, { profile: opts.profile, lockPath });
  const what = opts.self ? "kit core (canonical self-check)" : `kit core copy${lockPath ? ` (${lockPath})` : ""}`;
  if (problems.length) {
    console.error(`check-kit-copy: ${what} at ${opts.dir}: ${problems.length} problem(s)`);
    for (const p of problems) console.error(`  - ${p}`);
    return 1;
  }
  console.log(`check-kit-copy: ${what} at ${opts.dir}: OK`);
  return 0;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exit(main(process.argv.slice(2)));
}
